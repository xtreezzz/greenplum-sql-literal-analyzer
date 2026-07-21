import unittest

import sqlglot
from sqlglot import exp

from gp_sql_analyzer.lineage import LineageResolver
from gp_sql_analyzer.schema import MappingSchemaProvider
from tests.tpcds import TPCDS_SCHEMA


def resolve_named_column(sql: str, column_sql: str):
    expression = sqlglot.parse_one(sql, read="postgres")
    column = next(column for column in expression.find_all(exp.Column) if column.sql() == column_sql)
    resolver = LineageResolver(
        expression,
        MappingSchemaProvider(TPCDS_SCHEMA, default_schema="tpcds"),
    )
    return resolver.resolve(column)


class LineageTests(unittest.TestCase):
    def test_resolves_table_alias_to_benchmark_column(self) -> None:
        result = resolve_named_column(
            "SELECT * FROM item AS i WHERE i.i_color = 'purple'", "i.i_color"
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.item.i_color")

    def test_resolves_chained_cte_projection_to_physical_column(self) -> None:
        result = resolve_named_column(
            """
            WITH item_base AS (
                SELECT i_item_sk, i_category AS category FROM item
            ), item_filtered AS (
                SELECT category FROM item_base WHERE category = 'Books'
            )
            SELECT * FROM item_filtered AS f WHERE f.category = 'Music'
            """,
            "f.category",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.item.i_category")

    def test_union_output_preserves_all_physical_sources(self) -> None:
        result = resolve_named_column(
            """
            WITH channel_items AS (
                SELECT ws_item_sk AS item_sk FROM web_sales
                UNION ALL
                SELECT cs_item_sk FROM catalog_sales
            )
            SELECT * FROM channel_items WHERE item_sk = '42'
            """,
            "item_sk",
        )

        self.assertEqual(result.status, "multi_source")
        self.assertEqual(
            [column.qualified_name for column in result.columns],
            ["tpcds.catalog_sales.cs_item_sk", "tpcds.web_sales.ws_item_sk"],
        )

    def test_unqualified_column_from_two_ctes_is_ambiguous(self) -> None:
        result = resolve_named_column(
            """
            WITH sold AS (SELECT ss_item_sk AS item_sk FROM store_sales),
                 returned AS (SELECT sr_item_sk AS item_sk FROM store_returns)
            SELECT * FROM sold AS s JOIN returned AS r ON s.item_sk = r.item_sk
            WHERE item_sk = '42'
            """,
            "item_sk",
        )

        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(len(result.columns), 2)

    def test_correlated_column_is_resolved_in_parent_scope(self) -> None:
        result = resolve_named_column(
            """
            SELECT * FROM customer AS c
            WHERE EXISTS (
                SELECT 1 FROM customer_address AS ca
                WHERE ca.ca_address_sk = c.c_current_addr_sk
                  AND ca.ca_city = 'Edgewood'
            )
            """,
            "c.c_current_addr_sk",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.customer.c_current_addr_sk")

    def test_resolves_explicit_cte_column_alias(self) -> None:
        result = resolve_named_column(
            """
            WITH labeled(color) AS (SELECT i_color FROM item)
            SELECT * FROM labeled WHERE color = 'purple'
            """,
            "color",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.item.i_color")

    def test_resolves_derived_table_projection(self) -> None:
        result = resolve_named_column(
            """
            SELECT * FROM (SELECT i_color AS color FROM item) AS derived
            WHERE derived.color = 'purple'
            """,
            "derived.color",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.item.i_color")

    def test_resolves_qualified_star_without_leaking_other_join_sources(self) -> None:
        result = resolve_named_column(
            """
            SELECT *
            FROM (
                SELECT i.*
                FROM item AS i
                JOIN store AS s ON i.i_item_sk = s.s_store_sk
            ) AS derived
            WHERE derived.i_color = 'purple'
            """,
            "derived.i_color",
        )

        self.assertEqual(result.status, "resolved")
        self.assertEqual(result.columns[0].qualified_name, "tpcds.item.i_color")

    def test_same_alias_name_in_nested_scope_does_not_leak(self) -> None:
        expression = sqlglot.parse_one(
            """
            SELECT * FROM item AS source
            WHERE source.i_color = 'purple'
              AND EXISTS (
                SELECT 1 FROM store AS source WHERE source.s_state = 'TN'
              )
            """,
            read="postgres",
        )
        resolver = LineageResolver(
            expression, MappingSchemaProvider(TPCDS_SCHEMA, default_schema="tpcds")
        )
        columns = {
            column.name: resolver.resolve(column).columns[0].qualified_name
            for column in expression.find_all(exp.Column)
            if column.name in {"i_color", "s_state"}
        }

        self.assertEqual(columns["i_color"], "tpcds.item.i_color")
        self.assertEqual(columns["s_state"], "tpcds.store.s_state")


if __name__ == "__main__":
    unittest.main()
