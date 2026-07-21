import unittest

from gp_sql_analyzer.analyzer import SQLAnalyzer
from gp_sql_analyzer.models import QueryRecord
from gp_sql_analyzer.schema import MappingSchemaProvider
from tests.tpcds import TPCDS_SCHEMA


def analyzer() -> SQLAnalyzer:
    return SQLAnalyzer(MappingSchemaProvider(TPCDS_SCHEMA, default_schema="tpcds"))


class SQLAnalyzerTests(unittest.TestCase):
    def test_extracts_tpcds_like_and_exact_values(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-item",
                """
                SELECT i.i_item_id FROM item AS i
                WHERE i.i_color ILIKE '%purple%'
                   OR i.i_category = 'Books'
                """,
                """
                SELECT i.i_item_id FROM item AS i
                WHERE i.i_color ILIKE '%&CHARACTER%'
                   OR i.i_category = '&CHARACTER'
                """,
                source_row_count=7,
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(len(result.occurrences), 2)
        by_column = {item.lineage.columns[0].column: item for item in result.occurrences}
        color = by_column["i_color"]
        self.assertEqual(color.extracted_value, "purple")
        self.assertEqual(color.pattern_family, "like_contains")
        self.assertEqual(color.clause_context, "WHERE")
        self.assertEqual(color.operator_or_function, "ILIKE")
        self.assertEqual(color.source_row_count, 7)
        self.assertEqual(by_column["i_category"].pattern_family, "exact_value")

    def test_traces_filter_through_two_tpcds_ctes(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-q47",
                """
                WITH item_base AS (
                    SELECT i_item_sk, i_category AS category, i_brand AS brand FROM item
                ), item_filtered AS (
                    SELECT category, brand FROM item_base WHERE brand = 'ought'
                )
                SELECT * FROM item_filtered AS f
                WHERE f.category ~* '^(Books|Music)$'
                """,
                """
                WITH item_base AS (
                    SELECT i_item_sk, i_category AS category, i_brand AS brand FROM item
                ), item_filtered AS (
                    SELECT category, brand FROM item_base WHERE brand = '&CHARACTER'
                )
                SELECT * FROM item_filtered AS f
                WHERE f.category ~* '^(&CHARACTER|Music)$'
                """,
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(
            [item.lineage.columns[0].qualified_name for item in result.occurrences],
            ["tpcds.item.i_brand", "tpcds.item.i_category"],
        )
        regex = result.occurrences[1]
        self.assertEqual(regex.extracted_value, "Books")
        self.assertEqual(regex.operator_or_function, "~*")
        self.assertTrue(regex.regex_features["anchored_start"])

    def test_detects_join_select_having_and_case_contexts(self) -> None:
        original = """
            SELECT regexp_replace(i.i_product_name, '^item[0-9]+$', 'product', 'gi'),
                   CASE WHEN i.i_color = 'purple' THEN i.i_category ELSE 'other' END
            FROM store_sales AS ss
            JOIN store AS s
              ON ss.ss_store_sk = s.s_store_sk AND s.s_state = 'TN'
            JOIN item AS i ON ss.ss_item_sk = i.i_item_sk
            GROUP BY i.i_product_name, i.i_color, i.i_category
            HAVING i.i_category LIKE 'Books%'
        """
        template = """
            SELECT regexp_replace(i.i_product_name, '^&CHARACTER[0-9]+$', 'product', 'gi'),
                   CASE WHEN i.i_color = '&CHARACTER' THEN i.i_category ELSE 'other' END
            FROM store_sales AS ss
            JOIN store AS s
              ON ss.ss_store_sk = s.s_store_sk AND s.s_state = '&CHARACTER'
            JOIN item AS i ON ss.ss_item_sk = i.i_item_sk
            GROUP BY i.i_product_name, i.i_color, i.i_category
            HAVING i.i_category LIKE '&CHARACTER%'
        """

        result = analyzer().analyze_record(QueryRecord("tpcds-derived-contexts", original, template))

        self.assertEqual(result.errors, [])
        contexts = {
            (item.extracted_value, item.clause_context, item.operator_or_function)
            for item in result.occurrences
        }
        self.assertIn(("item", "SELECT", "REGEXP_REPLACE"), contexts)
        self.assertIn(("purple", "CASE", "="), contexts)
        self.assertIn(("TN", "JOIN_ON", "="), contexts)
        self.assertIn(("Books", "HAVING", "LIKE"), contexts)
        self.assertNotIn(("product", "SELECT", "REGEXP_REPLACE"), contexts)
        regexp_replace = next(
            item for item in result.occurrences if item.operator_or_function == "REGEXP_REPLACE"
        )
        self.assertTrue(regexp_replace.regex_features["case_insensitive"])

    def test_handles_in_between_and_multiple_placeholders(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-q64",
                """
                SELECT * FROM item AS i
                WHERE i.i_color IN ('purple', 'burlywood')
                  AND i.i_product_name BETWEEN 'A' AND 'M'
                  AND i.i_product_name ~ '^(catalog)-(returns)$'
                """,
                """
                SELECT * FROM item AS i
                WHERE i.i_color IN ('&CHARACTER', '&CHARACTER')
                  AND i.i_product_name BETWEEN '&CHARACTER' AND '&CHARACTER'
                  AND i.i_product_name ~ '^(&CHARACTER)-(&CHARACTER)$'
                """,
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(
            [item.extracted_value for item in result.occurrences],
            ["purple", "burlywood", "A", "M", "catalog", "returns"],
        )
        self.assertEqual(
            [item.value_role for item in result.occurrences[:4]],
            ["list_value", "list_value", "range_low", "range_high"],
        )

    def test_parse_error_is_returned_without_raising(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord("broken", "SELECT FROM WHERE", "SELECT '&CHARACTER' FROM")
        )

        self.assertEqual(result.occurrences, [])
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(result.errors[0].stage, "parse")
        self.assertLessEqual(len(result.errors[0].sql_fragment), 240)

    def test_handles_reversed_casted_and_negated_predicates(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-predicates",
                """
                SELECT * FROM item AS i
                WHERE 'purple' = lower(CAST(i.i_color AS text))
                   OR i.i_product_name !~* '^legacy'
                """,
                """
                SELECT * FROM item AS i
                WHERE '&CHARACTER' = lower(CAST(i.i_color AS text))
                   OR i.i_product_name !~* '^&CHARACTER'
                """,
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(
            [item.lineage.columns[0].qualified_name for item in result.occurrences],
            ["tpcds.item.i_color", "tpcds.item.i_product_name"],
        )
        self.assertEqual(result.occurrences[1].operator_or_function, "!~*")

    def test_handles_postgres_regex_functions_in_select(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-regex-functions",
                """
                SELECT regexp_matches(i_product_name, '^item[0-9]+$', 'i'),
                       substring(i_product_name FROM '^catalog.*$')
                FROM item
                """,
                """
                SELECT regexp_matches(i_product_name, '^&CHARACTER[0-9]+$', 'i'),
                       substring(i_product_name FROM '^&CHARACTER.*$')
                FROM item
                """,
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(
            [item.operator_or_function for item in result.occurrences],
            ["REGEXP_MATCHES", "SUBSTRING_REGEX"],
        )
        self.assertTrue(all(item.pattern_family == "regex" for item in result.occurrences))
        self.assertTrue(all(item.clause_context == "SELECT" for item in result.occurrences))
        self.assertTrue(result.occurrences[0].regex_features["case_insensitive"])

    def test_select_constant_is_unresolved_but_expression_literal_uses_its_column(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "tpcds-derived-select-values",
                "SELECT 'store' AS channel, i_category || '-' || 'suffix' FROM item",
                "SELECT '&CHARACTER' AS channel, i_category || '-' || '&CHARACTER' FROM item",
            )
        )

        self.assertEqual(result.errors, [])
        self.assertEqual(result.occurrences[0].lineage.status, "unresolved")
        self.assertEqual(result.occurrences[0].lineage.columns, ())
        self.assertEqual(
            result.occurrences[1].lineage.columns[0].qualified_name,
            "tpcds.item.i_category",
        )

    def test_parse_error_fragment_redacts_unterminated_literal(self) -> None:
        result = analyzer().analyze_record(
            QueryRecord(
                "broken-secret",
                "SELECT * FROM item WHERE i_color = 'super-secret",
                "SELECT * FROM item WHERE i_color = '&CHARACTER",
            )
        )

        self.assertEqual(len(result.errors), 1)
        self.assertNotIn("super-secret", result.errors[0].sql_fragment)
        self.assertNotIn("super-secret", result.errors[0].message)

    def test_safe_fragment_redacts_postgres_dollar_quoted_literal(self) -> None:
        fragment = SQLAnalyzer._safe_fragment(
            "SELECT $tag$super-secret$tag$, 'other-secret'"
        )

        self.assertNotIn("super-secret", fragment)
        self.assertNotIn("other-secret", fragment)


if __name__ == "__main__":
    unittest.main()
