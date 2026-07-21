import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gp_sql_analyzer.cli import main
from tests.test_benchmarks import TPCDS_COMPLEX_EXCERPT
from tests.tpcds import TPCDS_SCHEMA


class CliTests(unittest.TestCase):
    def test_catalog_report_writes_json_jsonl_and_html_from_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            schema = root / "ddl"
            corpus.mkdir()
            schema.mkdir()
            (corpus / "66.sql").write_text(
                "SELECT * FROM ship_mode WHERE sm_carrier IN ('DHL', 'BARIAN')",
                encoding="utf-8",
            )
            (schema / "ship_mode.sql").write_text(
                "CREATE TABLE ship_mode (sm_carrier VARCHAR(20), sm_unused INTEGER);",
                encoding="utf-8",
            )
            json_path = root / "catalog.json"
            jsonl_path = root / "columns.jsonl"
            html_path = root / "catalog.html"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "catalog-report",
                        "--corpus-dir",
                        str(corpus),
                        "--schema-dir",
                        str(schema),
                        "--default-schema",
                        "tpcds",
                        "--output-json",
                        str(json_path),
                        "--output-jsonl",
                        str(jsonl_path),
                        "--output-html",
                        str(html_path),
                    ]
                )

            summary = json.loads(stdout.getvalue())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            columns = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
            html = html_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["files_parsed"], 1)
        self.assertEqual(summary["table_count"], 1)
        self.assertEqual(summary["column_count"], 2)
        self.assertEqual(payload["summary"]["active_column_count"], 1)
        self.assertEqual(len(columns), 2)
        carrier = next(row for row in columns if row["column_name"] == "sm_carrier")
        self.assertEqual(
            [item["value"] for item in carrier["top_values"]], ["BARIAN", "DHL"]
        )
        self.assertIn("tpcds.ship_mode.sm_carrier", html)

    def test_catalog_postprocess_builds_same_artifacts_without_sql(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            details_path = root / "details.jsonl"
            schema_path = root / "schema.json"
            json_path = root / "catalog.json"
            jsonl_path = root / "columns.jsonl"
            html_path = root / "catalog.html"
            schema_path.write_text(
                json.dumps({"tpcds": {"item": ["i_color", "i_unused"]}}),
                encoding="utf-8",
            )
            details_path.write_text(
                json.dumps(
                    {
                        "query_id": "q1",
                        "source_row_count": 5,
                        "base_columns": ["tpcds.item.i_color"],
                        "lineage_status": "resolved",
                        "clause_context": "WHERE",
                        "operator_or_function": "ILIKE",
                        "value_role": "pattern",
                        "raw_literal": "%purple%",
                        "extracted_value": "purple",
                        "pattern_template": "%&CHARACTER%",
                        "pattern_family": "like_contains",
                        "pattern_format": "like_contains",
                        "regex_features": {},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "catalog-postprocess",
                        "--details-jsonl",
                        str(details_path),
                        "--schema-json",
                        str(schema_path),
                        "--default-schema",
                        "tpcds",
                        "--source-label",
                        "analytics.query_log",
                        "--output-json",
                        str(json_path),
                        "--output-jsonl",
                        str(jsonl_path),
                        "--output-html",
                        str(html_path),
                    ]
                )

            summary = json.loads(stdout.getvalue())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            jsonl_exists = jsonl_path.exists()
            html_exists = html_path.exists()

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["source_row_count"], 5)
        self.assertEqual(payload["metadata"]["source_label"], "analytics.query_log")
        self.assertEqual(payload["summary"]["column_count"], 2)
        self.assertTrue(jsonl_exists)
        self.assertTrue(html_exists)

    def test_html_report_accepts_schema_directory_for_literal_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            schema = root / "ddl"
            corpus.mkdir()
            schema.mkdir()
            (corpus / "66.sql").write_text(
                "SELECT * FROM ship_mode WHERE sm_carrier IN ('DHL', 'BARIAN')",
                encoding="utf-8",
            )
            (schema / "ship_mode.sql").write_text(
                "CREATE TABLE ship_mode (sm_carrier VARCHAR(20));",
                encoding="utf-8",
            )
            report_path = root / "report.html"

            with redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "html-report",
                        "--corpus-dir",
                        str(corpus),
                        "--schema-dir",
                        str(schema),
                        "--default-schema",
                        "tpcds",
                        "--output-html",
                        str(report_path),
                    ]
                )

            html = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn("tpcds.ship_mode.sm_carrier", html)

    def test_html_report_command_writes_ranked_self_contained_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "01.sql").write_text("SELECT 1", encoding="utf-8")
            (corpus / "02.sql").write_text(
                "WITH x AS (SELECT * FROM item) SELECT * FROM x",
                encoding="utf-8",
            )
            report_path = root / "report.html"
            stdout = io.StringIO()

            with redirect_stdout(stdout):
                exit_code = main(
                    [
                        "html-report",
                        "--corpus-dir",
                        str(corpus),
                        "--output-html",
                        str(report_path),
                        "--source-label",
                        "fixture",
                    ]
                )

            summary = json.loads(stdout.getvalue())
            html = report_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["files_seen"], 2)
        self.assertEqual(summary["files_parsed"], 2)
        self.assertEqual(summary["errors"], 0)
        self.assertIn("data-rank=\"1\"", html)
        self.assertIn("fixture", html)

    def test_benchmark_command_writes_complexity_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corpus = root / "corpus"
            corpus.mkdir()
            (corpus / "complex.sql").write_text(
                TPCDS_COMPLEX_EXCERPT, encoding="utf-8"
            )
            report_path = root / "benchmark.json"

            exit_code = main(
                [
                    "benchmark",
                    "--corpus-dir",
                    str(corpus),
                    "--output-json",
                    str(report_path),
                ]
            )
            report = json.loads(report_path.read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["files_parsed"], 1)
        self.assertGreaterEqual(report["construct_counts"]["ctes"], 3)

    def test_local_end_to_end_writes_three_reports_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.jsonl"
            schema_path = root / "schema.json"
            output_dir = root / "report"
            schema_path.write_text(json.dumps(TPCDS_SCHEMA), encoding="utf-8")
            records = [
                {
                    "query_id": "q64-mask",
                    "query_text": "SELECT * FROM item WHERE i_color ILIKE '%purple%'",
                    "query_text_template": "SELECT * FROM item WHERE i_color ILIKE '%&CHARACTER%'",
                    "source_row_count": 3,
                },
                {
                    "query_id": "broken",
                    "query_text": "SELECT FROM WHERE",
                    "query_text_template": "SELECT '&CHARACTER' FROM",
                },
            ]
            input_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            exit_code = main(
                [
                    "analyze",
                    "--input-jsonl",
                    str(input_path),
                    "--schema-json",
                    str(schema_path),
                    "--default-schema",
                    "tpcds",
                    "--output-dir",
                    str(output_dir),
                    "--batch-size",
                    "1",
                ]
            )

            details = [json.loads(line) for line in (output_dir / "details.jsonl").read_text().splitlines()]
            errors = [json.loads(line) for line in (output_dir / "errors.jsonl").read_text().splitlines()]
            summary = [json.loads(line) for line in (output_dir / "summary.jsonl").read_text().splitlines()]
            metrics = json.loads((output_dir / "metrics.json").read_text())
            schema_snapshot = json.loads((output_dir / "schema.json").read_text())

        self.assertEqual(exit_code, 0)
        self.assertEqual(details[0]["base_columns"], ["tpcds.item.i_color"])
        self.assertEqual(details[0]["extracted_value"], "purple")
        self.assertEqual(len(errors), 1)
        self.assertEqual(summary[0]["source_row_count"], 3)
        self.assertEqual(metrics["records_seen"], 2)
        self.assertEqual(metrics["records_parsed"], 1)
        self.assertEqual(metrics["parse_success_rate"], 0.5)
        self.assertEqual(metrics["lineage_status_counts"], {"resolved": 1})
        self.assertGreaterEqual(metrics["peak_memory_bytes"], 0)
        self.assertEqual(schema_snapshot["default_schema"], "tpcds")
        self.assertIn("item", schema_snapshot["schemas"]["tpcds"])

    @patch("gp_sql_analyzer.cli.connect_greenplum")
    def test_greenplum_mode_loads_catalog_and_streams_source(self, connect) -> None:
        class Cursor:
            def __init__(self, rows):
                self.rows = list(rows)
                self.itersize = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, sql, params=()):
                self.executed = (sql, params)

            def fetchall(self):
                return list(self.rows)

            def fetchmany(self, size):
                rows, self.rows = self.rows[:size], self.rows[size:]
                return rows

        class Connection:
            def __init__(self):
                self.calls = 0
                self.closed = False

            def cursor(self, name=None):
                self.calls += 1
                if self.calls == 1:
                    return Cursor(
                        [
                            ("warehouse", "tpcds", "item", "i_item_id"),
                            ("warehouse", "tpcds", "item", "i_color"),
                        ]
                    )
                return Cursor(
                    [
                        (
                            "q64",
                            "SELECT i_item_id FROM item WHERE i_color = 'purple'",
                            "SELECT i_item_id FROM item WHERE i_color = '&CHARACTER'",
                            2,
                        )
                    ]
                )

            def close(self):
                self.closed = True

        connection = Connection()
        connect.return_value = connection
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "report"

            exit_code = main(
                [
                    "analyze",
                    "--source-table",
                    "analytics.query_log",
                    "--default-schema",
                    "tpcds",
                    "--catalog-schema",
                    "tpcds",
                    "--output-dir",
                    str(output_dir),
                ]
            )

            details = [json.loads(line) for line in (output_dir / "details.jsonl").read_text().splitlines()]

        self.assertEqual(exit_code, 0)
        self.assertEqual(details[0]["base_columns"], ["warehouse.tpcds.item.i_color"])
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
