import json
import tempfile
import unittest
from pathlib import Path

from gp_sql_analyzer.catalog_stats import (
    build_catalog_report,
    build_catalog_report_from_details,
)
from gp_sql_analyzer.complexity import analyze_corpus
from gp_sql_analyzer.schema import MappingSchemaProvider


class CatalogStatisticsTests(unittest.TestCase):
    def _benchmark_report(self):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        queries = root / "queries"
        ddl = root / "schema"
        queries.mkdir()
        ddl.mkdir()
        (ddl / "item.sql").write_text(
            "CREATE TABLE item ("
            "i_color VARCHAR(20), i_brand VARCHAR(20), i_unused INTEGER);",
            encoding="utf-8",
        )
        (ddl / "sales.sql").write_text(
            "CREATE TABLE sales (s_id INTEGER, i_color VARCHAR(20));",
            encoding="utf-8",
        )
        (queries / "01.sql").write_text(
            "SELECT * FROM item "
            "WHERE i_color ILIKE '%purple%' AND i_brand IN ('A', 'B')",
            encoding="utf-8",
        )
        (queries / "02.sql").write_text(
            "SELECT * FROM item WHERE i_color ILIKE '%purple%'",
            encoding="utf-8",
        )
        (queries / "03.sql").write_text(
            "SELECT * FROM item JOIN sales ON s_id = i_unused WHERE i_color = 'red'",
            encoding="utf-8",
        )
        corpus = analyze_corpus(
            queries,
            schema_directory=ddl,
            default_schema="tpcds",
            source_label="fixture",
            source_commit="abc123",
        )
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "item": ["i_color", "i_brand", "i_unused"],
                    "sales": ["s_id", "i_color"],
                }
            },
            default_schema="tpcds",
        )
        return temporary, build_catalog_report(
            corpus,
            schema,
            generated_at="2026-07-21T00:00:00Z",
        )

    def test_includes_every_schema_column_and_zero_usage(self) -> None:
        temporary, report = self._benchmark_report()
        self.addCleanup(temporary.cleanup)

        payload = report.to_dict()
        self.assertEqual(payload["summary"]["table_count"], 2)
        self.assertEqual(payload["summary"]["column_count"], 5)
        self.assertEqual(len(report.column_rows()), 5)

        sales_id = next(
            row
            for row in report.column_rows()
            if row["qualified_name"] == "tpcds.sales.s_id"
        )
        self.assertEqual(sales_id["condition_count"], 0)
        self.assertEqual(sales_id["top_values"], [])

    def test_aggregates_values_masks_operators_and_contexts_per_column(self) -> None:
        temporary, report = self._benchmark_report()
        self.addCleanup(temporary.cleanup)

        color = next(
            row
            for row in report.column_rows()
            if row["qualified_name"] == "tpcds.item.i_color"
        )
        purple = next(item for item in color["top_values"] if item["value"] == "%purple%")

        self.assertEqual(color["condition_count"], 2)
        self.assertEqual(color["distinct_query_count"], 2)
        self.assertEqual(color["context_counts"], {"WHERE": 2})
        self.assertEqual(color["operator_counts"], {"ILIKE": 2})
        self.assertEqual(color["pattern_family_counts"], {"like_contains": 2})
        self.assertEqual(purple["source_row_count"], 2)
        self.assertEqual(purple["distinct_query_count"], 2)
        self.assertEqual(purple["pattern_family"], "like_contains")
        self.assertIn("01.sql", purple["example_query_ids"])

    def test_ambiguous_lineage_is_visible_but_not_added_to_columns(self) -> None:
        temporary, report = self._benchmark_report()
        self.addCleanup(temporary.cleanup)

        payload = report.to_dict()
        item_color = next(
            row
            for row in report.column_rows()
            if row["qualified_name"] == "tpcds.item.i_color"
        )
        sales_color = next(
            row
            for row in report.column_rows()
            if row["qualified_name"] == "tpcds.sales.i_color"
        )

        self.assertEqual(item_color["condition_count"], 2)
        self.assertEqual(sales_color["condition_count"], 0)
        self.assertEqual(payload["summary"]["lineage_status_counts"]["ambiguous"], 1)
        self.assertEqual(payload["quality"]["groups"][0]["values"], ["red"])
        self.assertIn("tpcds.item.i_color", payload["quality"]["groups"][0]["base_columns"])

    def test_postprocesses_details_without_sql_and_preserves_source_weights(self) -> None:
        schema = MappingSchemaProvider(
            {"tpcds": {"item": ["i_color", "i_unused"]}},
            default_schema="tpcds",
        )
        details = [
            {
                "query_id": "q1",
                "source_row_count": 3,
                "base_columns": ["tpcds.item.i_color"],
                "lineage_status": "resolved",
                "lineage_reason": None,
                "clause_context": "WHERE",
                "operator_or_function": "ILIKE",
                "value_role": "pattern",
                "raw_literal": "%purple%",
                "extracted_value": "purple",
                "pattern_template": "%&CHARACTER%",
                "pattern_family": "like_contains",
                "pattern_format": "like_contains",
                "regex_features": {},
            },
            {
                "query_id": "q2",
                "source_row_count": 1,
                "base_columns": ["tpcds.item.i_color"],
                "lineage_status": "resolved",
                "lineage_reason": None,
                "clause_context": "JOIN_ON",
                "operator_or_function": "~*",
                "value_role": "regex_pattern",
                "raw_literal": "^(red|blue)$",
                "extracted_value": "^(red|blue)$",
                "pattern_template": "&CHARACTER",
                "pattern_family": "regex",
                "pattern_format": "alternation+anchored_end+anchored_start+case_insensitive+groups",
                "regex_features": {"alternation": True, "anchored_start": True},
            },
            {
                "query_id": "q3",
                "source_row_count": 4,
                "base_columns": ["tpcds.item.i_color", "tpcds.other.i_color"],
                "lineage_status": "ambiguous",
                "lineage_reason": "matches multiple sources",
                "clause_context": "WHERE",
                "operator_or_function": "=",
                "value_role": "comparison_value",
                "raw_literal": "red",
                "extracted_value": "red",
                "pattern_template": "&CHARACTER",
                "pattern_family": "exact_value",
                "pattern_format": "exact_value",
                "regex_features": {},
            },
        ]

        report = build_catalog_report_from_details(
            details,
            schema,
            source_label="analytics.query_log",
            generated_at="2026-07-21T00:00:00Z",
        )
        payload = report.to_dict()
        color = next(
            row
            for row in report.column_rows()
            if row["qualified_name"] == "tpcds.item.i_color"
        )

        self.assertEqual(color["condition_count"], 2)
        self.assertEqual(color["source_row_count"], 4)
        self.assertEqual(color["context_counts"], {"JOIN_ON": 1, "WHERE": 3})
        purple = next(item for item in color["top_values"] if item["value"] == "purple")
        self.assertEqual(purple["source_row_count"], 3)
        self.assertEqual(purple["raw_examples"], ["%purple%"])
        self.assertEqual(len(color["top_patterns"]), 2)
        self.assertEqual(payload["summary"]["lineage_status_counts"]["ambiguous"], 4)
        self.assertEqual(payload["quality"]["source_row_count"], 4)
        json.dumps(payload, ensure_ascii=False)

    def test_preserves_regex_metadata_from_literal_analyzer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queries = root / "queries"
            ddl = root / "schema"
            queries.mkdir()
            ddl.mkdir()
            (queries / "regex.sql").write_text(
                "SELECT substring(i_color FROM '^(red|blue)$') FROM item",
                encoding="utf-8",
            )
            (ddl / "item.sql").write_text(
                "CREATE TABLE item (i_color VARCHAR(20));", encoding="utf-8"
            )
            corpus = analyze_corpus(
                queries, schema_directory=ddl, default_schema="tpcds"
            )
            schema = MappingSchemaProvider(
                {"tpcds": {"item": ["i_color"]}}, default_schema="tpcds"
            )
            report = build_catalog_report(corpus, schema)

        color = report.column_rows()[0]
        pattern = color["top_patterns"][0]
        self.assertEqual(pattern["pattern_family"], "regex")
        self.assertTrue(pattern["regex_features"]["anchored_start"])
        self.assertTrue(pattern["regex_features"]["anchored_end"])


if __name__ == "__main__":
    unittest.main()
