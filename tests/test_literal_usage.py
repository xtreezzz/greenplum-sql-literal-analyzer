import unittest

from gp_sql_analyzer.analyzer import SQLAnalyzer
from gp_sql_analyzer.schema import MappingSchemaProvider


Q66_LITERAL_EXCERPT = """
SELECT *
FROM (
    SELECT 'DHL,BARIAN' AS ship_carriers
    FROM web_sales, ship_mode
    WHERE ws_ship_mode_sk = sm_ship_mode_sk
      AND sm_carrier IN ('DHL', 'BARIAN')
    UNION ALL
    SELECT 'DHL,BARIAN' AS ship_carriers
    FROM catalog_sales, ship_mode
    WHERE cs_ship_mode_sk = sm_ship_mode_sk
      AND sm_carrier IN ('DHL', 'BARIAN')
) AS channels
"""


class LiteralUsageTests(unittest.TestCase):
    def setUp(self) -> None:
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "web_sales": ["ws_ship_mode_sk"],
                    "catalog_sales": ["cs_ship_mode_sk"],
                    "ship_mode": ["sm_ship_mode_sk", "sm_carrier"],
                }
            },
            default_schema="tpcds",
        )
        self.analyzer = SQLAnalyzer(schema)

    def test_groups_q66_in_values_by_physical_column_and_context(self) -> None:
        usages = self.analyzer.analyze_literal_usages(Q66_LITERAL_EXCERPT)

        carrier = next(
            usage
            for usage in usages
            if usage.operator_or_function == "IN"
            and usage.values == ("DHL", "BARIAN")
        )
        self.assertEqual(carrier.clause_context, "WHERE")
        self.assertEqual(
            [column.qualified_name for column in carrier.lineage.columns],
            ["tpcds.ship_mode.sm_carrier"],
        )
        self.assertEqual(carrier.lineage.status, "resolved")
        self.assertEqual(carrier.condition_count, 2)
        self.assertEqual(carrier.literal_count, 4)
        self.assertEqual(carrier.pattern_families, ("exact_value",))

    def test_keeps_select_constant_separate_from_column_filter(self) -> None:
        usages = self.analyzer.analyze_literal_usages(Q66_LITERAL_EXCERPT)

        selected = next(
            usage
            for usage in usages
            if usage.operator_or_function == "SELECT_LITERAL"
        )
        self.assertEqual(selected.clause_context, "SELECT")
        self.assertEqual(selected.values, ("DHL,BARIAN",))
        self.assertEqual(selected.lineage.status, "unresolved")
        self.assertEqual(selected.condition_count, 2)
        self.assertEqual(selected.literal_count, 2)

    def test_classifies_regex_pattern_for_resolved_column(self) -> None:
        usages = self.analyzer.analyze_literal_usages(
            "SELECT * FROM ship_mode WHERE sm_carrier ~* '^(DHL|UPS)$'"
        )

        regex = next(
            usage for usage in usages if usage.operator_or_function == "~*"
        )
        self.assertEqual(regex.values, ("^(DHL|UPS)$",))
        self.assertEqual(regex.pattern_families, ("regex",))
        self.assertIn("anchored", regex.pattern_formats[0])
        self.assertEqual(
            regex.lineage.columns[0].qualified_name,
            "tpcds.ship_mode.sm_carrier",
        )

    def test_between_keeps_arithmetic_bound_as_one_expression(self) -> None:
        schema = MappingSchemaProvider(
            {"tpcds": {"date_dim": ["d_month_seq"]}},
            default_schema="tpcds",
        )
        analyzer = SQLAnalyzer(schema)

        usages = analyzer.analyze_literal_usages(
            "SELECT * FROM date_dim "
            "WHERE d_month_seq BETWEEN 1200 AND 1200 + 11"
        )

        between = next(
            usage for usage in usages if usage.operator_or_function == "BETWEEN"
        )
        self.assertEqual(between.values, ("1200", "1200 + 11"))
        self.assertEqual(between.value_role, "range")
        self.assertEqual(between.condition_count, 1)
        self.assertEqual(between.literal_count, 3)
        self.assertEqual(
            between.lineage.columns[0].qualified_name,
            "tpcds.date_dim.d_month_seq",
        )

    def test_positional_substring_is_not_reported_as_regex(self) -> None:
        schema = MappingSchemaProvider(
            {"tpcds": {"customer_address": ["ca_zip"]}},
            default_schema="tpcds",
        )
        analyzer = SQLAnalyzer(schema)

        usages = analyzer.analyze_literal_usages(
            "SELECT substring(ca_zip FROM 1 FOR 5) FROM customer_address"
        )

        start = next(
            usage for usage in usages if usage.operator_or_function == "SUBSTRING_START"
        )
        length = next(
            usage for usage in usages if usage.operator_or_function == "SUBSTRING_LENGTH"
        )
        self.assertEqual(start.values, ("1",))
        self.assertEqual(length.values, ("5",))
        self.assertEqual(start.pattern_families, ("exact_value",))
        self.assertEqual(length.pattern_families, ("exact_value",))

    def test_extracts_individual_literals_and_null_checks(self) -> None:
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "store_sales": [
                        "ss_store_sk",
                        "ss_addr_sk",
                        "ss_ticket_number",
                        "ss_quantity",
                    ]
                }
            },
            default_schema="tpcds",
        )
        analyzer = SQLAnalyzer(schema)

        usages = analyzer.analyze_predicate_usages(
            "SELECT * FROM store_sales "
            "WHERE ss_store_sk = 4 "
            "AND ss_addr_sk IS NULL "
            "AND ss_ticket_number IS NOT NULL "
            "AND ss_quantity BETWEEN 1200 AND 1200 + 11"
        )

        by_operator_and_value = {
            (usage.operator_or_function, usage.extracted_value): usage
            for usage in usages
        }
        self.assertEqual(
            set(by_operator_and_value),
            {
                ("=", "4"),
                ("IS NULL", "NULL"),
                ("IS NOT NULL", "NULL"),
                ("BETWEEN", "1200"),
                ("BETWEEN", "1200 + 11"),
            },
        )
        null_check = by_operator_and_value[("IS NULL", "NULL")]
        self.assertEqual(null_check.origin, "null_check")
        self.assertEqual(null_check.value_role, "null_check")
        self.assertEqual(null_check.pattern_family, "null_check")
        self.assertEqual(
            null_check.lineage.columns[0].qualified_name,
            "tpcds.store_sales.ss_addr_sk",
        )

    def test_scalar_subquery_is_not_serialized_as_the_literal_value(self) -> None:
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "store_sales": [
                        "ss_item_sk",
                        "ss_net_profit",
                        "ss_store_sk",
                        "ss_addr_sk",
                    ]
                }
            },
            default_schema="tpcds",
        )
        analyzer = SQLAnalyzer(schema)
        sql = """
        SELECT ss_item_sk
        FROM store_sales AS ss1
        GROUP BY ss_item_sk
        HAVING avg(ss_net_profit) > 0.9 * (
          SELECT avg(ss_net_profit)
          FROM store_sales
          WHERE ss_store_sk = 4 AND ss_addr_sk IS NULL
          GROUP BY ss_store_sk
        )
        """

        usages = analyzer.analyze_predicate_usages(sql)
        values = {
            (usage.operator_or_function, usage.extracted_value)
            for usage in usages
        }

        self.assertIn((">", "0.9"), values)
        self.assertIn(("=", "4"), values)
        self.assertIn(("IS NULL", "NULL"), values)
        self.assertFalse(
            any("SELECT" in usage.extracted_value.upper() for usage in usages)
        )


if __name__ == "__main__":
    unittest.main()
