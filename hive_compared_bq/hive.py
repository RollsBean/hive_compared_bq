"""

Copyright 2017 bol.com. All Rights Reserved


Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import logging
import sys
import time
# noinspection PyProtectedMember
from hive_compared_bq import _Table
import pyhs2  # TODO switch to another module since this one is deprecated and does not support Python 3
# see notes in : https://github.com/BradRuderman/pyhs2


class THive(_Table):
    """Hive implementation of the _Table object"""

    def __init__(self, database, table, parent, hs2_server, jar_path):
        _Table.__init__(self, database, table, parent)
        self.server = hs2_server
        self.connection = self._create_connection()
        self.jarPath = jar_path

    def get_type(self):
        return "hive"

    def _create_connection(self):
        """Connect to the table and return the connection object that we will use to launch queries"""
        return pyhs2.connect(host=self.server, port=10000, authMechanism="KERBEROS", database=self.database)

    def get_ddl_columns(self):
        if len(self._ddl_columns) > 0:
            return self._ddl_columns

        is_col_def = True
        cur = self.connection.cursor()
        cur.execute("describe " + self.full_name)
        all_columns = []
        while cur.hasMoreRows:
            row = cur.fetchone()
            if row is None:
                continue
            col_name = row[0]
            col_type = row[1]

            if col_name == "" or col_name == "None":
                continue
            if col_name.startswith('#'):
                if "Partition Information" in col_name:
                    is_col_def = False
                continue

            my_dic = {"name": col_name, "type": col_type}
            if is_col_def:
                all_columns.append(my_dic)
            else:
                self._ddl_partitions.append(my_dic)
        cur.close()

        self.filter_columns_from_cli(all_columns)

        return self._ddl_columns

    def get_column_statistics(self, query, selected_columns):
        cur = self.connection.cursor()
        cur.execute(query)
        while cur.hasMoreRows:
            fetched = cur.fetchone()
            if fetched is not None:
                for idx, col in enumerate(selected_columns):
                    value_column = fetched[idx]
                    col["Counter"][value_column] += 1  # TODO what happens with NULL?
        cur.close()

    def create_sql_groupby_count(self):
        where_condition = ""
        if self.where_condition is not None:
            where_condition = "WHERE " + self.where_condition
        query = "SELECT hash( cast( %s as STRING)) %% %i AS gb, count(*) AS count FROM %s %s GROUP BY " \
                "hash( cast( %s as STRING)) %% %i" \
                % (self.get_groupby_column(), self.tc.number_of_group_by, self.full_name, where_condition,
                   self.get_groupby_column(), self.tc.number_of_group_by)
        logging.debug("Hive query is: %s", query)

        return query

    def create_sql_show_bucket_columns(self, extra_columns_str, buckets_values):
        gb_column = self.get_groupby_column()
        where_condition = ""
        if self.where_condition is not None:
            where_condition = self.where_condition + " AND"
        hive_query = "SELECT hash( cast( %s as STRING)) %% %i as bucket, %s, %s FROM %s WHERE %s " \
                     "hash( cast( %s as STRING)) %% %i IN (%s)" \
                     % (gb_column, self.tc.number_of_group_by, gb_column, extra_columns_str, self.full_name,
                        where_condition, gb_column, self.tc.number_of_group_by, buckets_values)
        logging.debug("Hive query to show the buckets and the extra columns is: %s", hive_query)

        return hive_query

    def create_sql_intermediate_checksums(self):
        column_blocks = self.get_column_blocks(self.get_ddl_columns())
        number_of_blocks = len(column_blocks)
        logging.debug("%i column_blocks (with a size of %i columns) have been considered: %s", number_of_blocks,
                      self.tc.block_size, str(column_blocks))

        # Generate the concatenations for the column_blocks
        hive_basic_shas = ""
        for idx, block in enumerate(column_blocks):
            hive_basic_shas += "base64( unhex( SHA1( concat( "
            for col in block:
                name = col["name"]
                hive_value_name = name
                if col["type"] == 'date':
                    hive_value_name = "cast( %s as STRING)" % name
                elif col["type"] == 'float' or col["type"] == 'double':
                    hive_value_name = "cast( floor( %s * 10000 ) as bigint)" % name
                elif col["type"] == 'string' and name in self.decodeCP1252_columns:
                    hive_value_name = "DecodeCP1252( %s)" % name
                hive_basic_shas += "CASE WHEN %s IS NULL THEN 'n_%s' ELSE %s END, '|'," % (name, name[:2],
                                                                                           hive_value_name)
            hive_basic_shas = hive_basic_shas[:-6] + ")))) as block_%i,\n" % idx
        hive_basic_shas = hive_basic_shas[:-2]

        where_condition = ""
        if self.where_condition is not None:
            where_condition = "WHERE " + self.where_condition

        hive_query = "WITH blocks AS (\nSELECT hash( cast( %s as STRING)) %% %i as gb,\n%s\nFROM %s %s\n),\n" \
                     % (self.get_groupby_column(), self.tc.number_of_group_by, hive_basic_shas, self.full_name,
                        where_condition)  # 1st CTE with the basic block shas
        list_blocks = ", ".join(["block_%i" % i for i in range(number_of_blocks)])
        hive_query += "full_lines AS(\nSELECT gb, base64( unhex( SHA1( concat( %s)))) as row_sha, %s FROM blocks\n)\n" \
                      % (list_blocks, list_blocks)  # 2nd CTE to get all the info of a row
        hive_list_shas = ", ".join(["base64( unhex( SHA1( concat_ws( '|', sort_array( collect_list( block_%i)))))) as "
                                    "block_%i_gb " % (i, i) for i in range(number_of_blocks)])
        hive_query += "SELECT gb, base64( unhex( SHA1( concat_ws( '|', sort_array( collect_list( row_sha)))))) as " \
                      "row_sha_gb, %s FROM full_lines GROUP BY gb" % hive_list_shas  # final query where all the shas
        # are grouped by row-blocks
        logging.debug("##### Final Hive query is:\n%s\n", hive_query)

        return hive_query

    def delete_temporary_table(self, table_name):
        self.query("DROP TABLE " + table_name).close()

    def query(self, query):
        """Execute the received query in Hive and return the cursor which is ready to be fetched and MUST be closed after

        :type query: str
        :param query: query to execute in Hive

        :rtype: :class:`pyhs2.cursor.Cursor`
        :returns: the cursor for this query

        :raises: IOError if the query has some execution errors
        """
        logging.debug("Launching Hive query")
        #  TODO split number should be done in function of file format (ORC, Avro...) and number of columns
        #  split_maxsize = 256000000
        # split_maxsize = 64000000
        split_maxsize = 8000000
        # split_maxsize = 16000000
        try:
            cur = self.connection.cursor()
            cur.execute("set mapreduce.input.fileinputformat.split.maxsize = %i" % split_maxsize)
            cur.execute("set hive.fetch.task.conversion=minimal")  # force a MapReduce, because simple 'fetch' queries
            # on a large table may generate some timeout otherwise
            cur.execute(query)
        except:
            raise IOError("There was a problem in executing the query in Hive: %s", sys.exc_info()[1])
        logging.debug("Fetching Hive results")
        return cur

    def launch_query_dict_result(self, query, result_dic, all_columns_from_2=False):
        try:
            cur = self.query(query)
            while cur.hasMoreRows:
                row = cur.fetchone()
                if row is not None:
                    if not all_columns_from_2:
                        result_dic[row[0]] = row[1]
                    else:
                        result_dic[row[0]] = row[2:]
        except:
            result_dic["error"] = sys.exc_info()[1]
            raise
        finally:
            cur.close()
        logging.debug("All %i Hive rows fetched", len(result_dic))

    def launch_query_csv_compare_result(self, query, rows):
        cur = self.query(query)
        while cur.hasMoreRows:
            row = cur.fetchone()
            if row is not None:
                line = "^ " + " | ".join([str(col) for col in row]) + " $"
                rows.append(line)
        logging.debug("All %i Hive rows fetched", len(rows))
        cur.close()

    def launch_query_with_intermediate_table(self, query, result):
        try:
            cur = self.query("add jar " + self.jarPath)  # must be in a separated execution
            cur.execute("create temporary function SHA1 as 'org.apache.hadoop.hive.ql.udf.UDFSha1'")
            cur.execute("create temporary function DecodeCP1252 as "
                        "'org.apache.hadoop.hive.ql.udf.generic.GenericUDFDecodeCP1252'")
        except:
            result["error"] = sys.exc_info()[1]
            raise

        if "error" in result:
            cur.close()
            return  # let's stop the thread if some error popped up elsewhere

        tmp_table = "%s.temp_hiveCmpBq_%s_%s" % (self.database, self.full_name.replace('.', '_'),
                                                 str(time.time()).replace('.', '_'))
        cur.execute("CREATE TABLE " + tmp_table + " AS\n" + query)
        cur.close()
        result["names_sha_tables"][self.get_id_string()] = tmp_table  # we confirm this table has been created
        result["cleaning"].append((tmp_table, self))

        logging.debug("The temporary table for Hive is " + tmp_table)

        if "error" in result:  # A problem happened in the other query of the other table (usually BQ, since it is
            # faster than Hive) so there is no need to pursue or have the temp table
            return

        projection_hive_row_sha = "SELECT gb, row_sha_gb FROM %s" % tmp_table
        self.launch_query_dict_result(projection_hive_row_sha, result["sha_dictionaries"][self.get_id_string()])
