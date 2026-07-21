import unittest
import sys
from unittest.mock import MagicMock, patch

from gp_sql_analyzer.greenplum import (
    ConnectionConfig,
    connect_greenplum,
    SourceQueryConfig,
    build_source_query,
    iter_greenplum_records,
    load_catalog_schema,
    quote_qualified_name,
)


class FakeCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = None
        self.itersize = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchmany(self, size):
        chunk, self.rows = self.rows[:size], self.rows[size:]
        return chunk

    def fetchall(self):
        return list(self.rows)


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.cursor_names = []
        self.last_cursor = None

    def cursor(self, name=None):
        self.cursor_names.append(name)
        self.last_cursor = FakeCursor(self.rows)
        return self.last_cursor


class GreenplumTests(unittest.TestCase):
    def test_connection_is_forced_to_read_only_transaction(self) -> None:
        psycopg2 = MagicMock()
        connection = psycopg2.connect.return_value

        with patch.dict(sys.modules, {"psycopg2": psycopg2}):
            actual = connect_greenplum({"GP_DBNAME": "warehouse"})

        self.assertIs(actual, connection)
        psycopg2.connect.assert_called_once_with(dbname="warehouse")
        connection.set_session.assert_called_once_with(readonly=True, autocommit=False)

    def test_connection_config_uses_only_documented_environment_variables(self) -> None:
        config = ConnectionConfig.from_env(
            {
                "GP_HOST": "gp.example",
                "GP_PORT": "5432",
                "GP_DBNAME": "warehouse",
                "GP_USER": "analyst",
                "GP_PASSWORD": "secret",
                "GP_SSLMODE": "require",
                "UNRELATED": "ignored",
            }
        )

        self.assertEqual(
            config.connect_kwargs(),
            {
                "host": "gp.example",
                "port": 5432,
                "dbname": "warehouse",
                "user": "analyst",
                "password": "secret",
                "sslmode": "require",
            },
        )

    def test_qualified_names_are_quoted_and_injection_is_rejected(self) -> None:
        self.assertEqual(
            quote_qualified_name("analytics.query_log"),
            '"analytics"."query_log"',
        )
        with self.assertRaises(ValueError):
            quote_qualified_name("query_log; DROP TABLE item")

    def test_source_query_parameterizes_filters_and_can_preaggregate(self) -> None:
        sql, params = build_source_query(
            SourceQueryConfig(
                table="analytics.query_log",
                id_column="query_id",
                since_column="created_at",
                since_value="2026-01-01",
                min_id=10,
                max_id=20,
                limit=100,
                preaggregate=True,
            )
        )

        self.assertIn("GROUP BY", sql)
        self.assertIn("ORDER BY query_id LIMIT %s", sql)
        self.assertIn('MIN(CAST("query_id" AS text))', sql)
        self.assertNotIn("2026-01-01", sql)
        self.assertEqual(params, ["%&CHARACTER%", "2026-01-01", 10, 20, 100])

    def test_server_side_cursor_yields_weighted_batches(self) -> None:
        connection = FakeConnection(
            [
                ("q1", "SELECT 1", "SELECT '&CHARACTER'", 4),
                ("q2", "SELECT 2", "SELECT '&CHARACTER'", 1),
            ]
        )

        batches = list(
            iter_greenplum_records(
                connection,
                SourceQueryConfig(table="analytics.query_log"),
                batch_size=1,
            )
        )

        self.assertEqual(connection.cursor_names, ["gp_sql_analyzer_source"])
        self.assertEqual([batch[0].source_row_count for batch in batches], [4, 1])

    def test_catalog_rows_build_cached_mapping_provider(self) -> None:
        connection = FakeConnection(
            [
                ("warehouse", "tpcds", "item", "i_item_sk"),
                ("warehouse", "tpcds", "item", "i_color"),
            ]
        )

        provider = load_catalog_schema(
            connection, schemas=["tpcds"], default_schema="tpcds"
        )
        table = provider.resolve_table("item")[0]

        self.assertTrue(provider.has_column(table, "i_color"))
        self.assertIn("a.attnum > 0", connection.last_cursor.executed[0])
        self.assertEqual(connection.last_cursor.executed[1], (["tpcds"],))


if __name__ == "__main__":
    unittest.main()
