import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from gp_sql_analyzer.dataframe import analyze_dataframe


def schema_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("warehouse", "tpcds", "item", "i_color"),
            ("warehouse", "tpcds", "item", "i_unused"),
            ("warehouse", "tpcds", "store_sales", "ss_item_sk"),
            ("warehouse", "tpcds", "store_sales", "ss_net_profit"),
            ("warehouse", "tpcds", "store_sales", "ss_store_sk"),
            ("warehouse", "tpcds", "store_sales", "ss_addr_sk"),
        ],
        columns=["table_catalog", "table_schema", "table_name", "column_name"],
    )


Q44_STYLE = """
SELECT ss_item_sk
FROM store_sales AS ss1
WHERE ss_store_sk = 4
GROUP BY ss_item_sk
HAVING avg(ss_net_profit) > 0.9 * (
  SELECT avg(ss_net_profit)
  FROM store_sales
  WHERE ss_store_sk = 4 AND ss_addr_sk IS NULL
  GROUP BY ss_store_sk
)
"""


class DataFrameAnalysisTests(unittest.TestCase):
    def test_preserves_one_output_row_per_input_and_isolates_errors(self) -> None:
        queries = pd.DataFrame(
            [
                {
                    "query_id": "q-mask",
                    "query_text": (
                        "SELECT * FROM item WHERE i_color ILIKE '%purple%'"
                    ),
                    "query_text_template": (
                        "SELECT * FROM item WHERE i_color ILIKE '%&CHARACTER%'"
                    ),
                    "source_row_count": 3,
                },
                {
                    "query_id": "q44",
                    "query_text": Q44_STYLE,
                    "query_text_template": Q44_STYLE,
                    "source_row_count": 1,
                },
                {
                    "query_id": "broken",
                    "query_text": "SELECT FROM",
                    "query_text_template": "SELECT FROM",
                    "source_row_count": 1,
                },
            ],
            index=[101, 205, 999],
        )

        result = analyze_dataframe(
            queries, schema_df=schema_frame(), default_schema="tpcds"
        )

        self.assertEqual(len(result.row_analysis_df), len(queries))
        self.assertEqual(result.row_analysis_df.index.tolist(), [101, 205, 999])
        self.assertEqual(result.row_analysis_df.iloc[0]["analysis_status"], "ok")
        self.assertEqual(result.row_analysis_df.iloc[2]["analysis_status"], "error")
        self.assertEqual(len(result.errors_df), 1)
        mask_analysis = result.row_analysis_df.iloc[0]["analysis"]
        self.assertEqual(len(mask_analysis), 1)
        self.assertEqual(mask_analysis[0]["origin"], "template")
        self.assertEqual(mask_analysis[0]["extracted_value"], "purple")
        self.assertEqual(
            mask_analysis[0]["base_columns"],
            ["warehouse.tpcds.item.i_color"],
        )

        q44_analysis = result.row_analysis_df.iloc[1]["analysis"]
        q44_pairs = {
            (row["operator_or_function"], row["extracted_value"])
            for row in q44_analysis
        }
        self.assertIn(("=", "4"), q44_pairs)
        self.assertIn(("IS NULL", "NULL"), q44_pairs)
        self.assertIn((">", "0.9"), q44_pairs)
        self.assertFalse(
            any("SELECT" in row["extracted_value"].upper() for row in q44_analysis)
        )

    def test_builds_weighted_resolved_aggregate_without_duplicates(self) -> None:
        queries = pd.DataFrame(
            [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM item WHERE i_color ILIKE '%purple%'",
                    "query_text_template": "SELECT * FROM item WHERE i_color ILIKE '%&CHARACTER%'",
                    "source_row_count": 3,
                },
                {
                    "query_id": "q2",
                    "query_text": "SELECT * FROM item WHERE i_color ILIKE '%purple%'",
                    "query_text_template": "SELECT * FROM item WHERE i_color ILIKE '%&CHARACTER%'",
                    "source_row_count": 2,
                },
            ]
        )

        result = analyze_dataframe(
            queries, schema_df=schema_frame(), default_schema="tpcds"
        )

        self.assertEqual(len(result.details_df), 2)
        self.assertEqual(len(result.aggregate_df), 1)
        row = result.aggregate_df.iloc[0]
        self.assertEqual(row["catalog_name"], "warehouse")
        self.assertEqual(row["schema_name"], "tpcds")
        self.assertEqual(row["table_name"], "item")
        self.assertEqual(row["column_name"], "i_color")
        self.assertEqual(row["extracted_value"], "purple")
        self.assertEqual(row["clause_context"], "WHERE")
        self.assertEqual(row["operator_or_function"], "ILIKE")
        self.assertEqual(row["pattern_family"], "like_contains")
        self.assertEqual(row["source_row_count"], 5)
        self.assertEqual(row["occurrence_count"], 2)
        self.assertEqual(row["distinct_query_count"], 2)
        self.assertEqual(row["share_of_column"], 1.0)
        self.assertEqual(row["example_query_ids"], ["q1", "q2"])

    def test_schema_is_optional_and_missing_columns_are_rejected(self) -> None:
        queries = pd.DataFrame(
            [
                {
                    "query_text": "SELECT * FROM item WHERE i_color = 'red'",
                    "query_text_template": (
                        "SELECT * FROM item WHERE i_color = '&CHARACTER'"
                    ),
                }
            ]
        )

        result = analyze_dataframe(queries, default_schema="tpcds")

        self.assertEqual(len(result.row_analysis_df), 1)
        self.assertEqual(
            result.row_analysis_df.iloc[0]["analysis"][0]["base_columns"],
            ["tpcds.item.i_color"],
        )
        with self.assertRaisesRegex(ValueError, "query_text_template"):
            analyze_dataframe(queries.drop(columns=["query_text_template"]))

    def test_validates_weights_catalogs_and_html_target(self) -> None:
        queries = pd.DataFrame(
            [
                {
                    "query_text": "SELECT 1",
                    "query_text_template": "SELECT 1",
                    "source_row_count": 0,
                }
            ]
        )
        with self.assertRaisesRegex(ValueError, "source_row_count"):
            analyze_dataframe(queries)

        two_catalogs = schema_frame().copy()
        two_catalogs.loc[0, "table_catalog"] = "archive"
        with self.assertRaisesRegex(ValueError, "one table_catalog"):
            analyze_dataframe(
                queries.assign(source_row_count=1), schema_df=two_catalogs
            )

        with self.assertRaisesRegex(ValueError, "output_dir"):
            analyze_dataframe(
                queries.assign(source_row_count=1), build_html=True
            )

    def test_writes_files_and_keeps_html_optional(self) -> None:
        queries = pd.DataFrame(
            [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM item WHERE i_color = 'red'",
                    "query_text_template": (
                        "SELECT * FROM item WHERE i_color = '&CHARACTER'"
                    ),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            result = analyze_dataframe(
                queries,
                schema_df=schema_frame(),
                default_schema="tpcds",
                output_dir=output,
            )

            expected = {
                "row_analysis",
                "details",
                "errors",
                "aggregate",
                "catalog_json",
                "catalog_columns",
                "schema",
            }
            self.assertEqual(set(result.artifact_paths), expected)
            self.assertFalse((output / "catalog-stats.html").exists())
            row_payload = json.loads(
                (output / "row_analysis.jsonl").read_text().splitlines()[0]
            )
            self.assertEqual(row_payload["analysis"][0]["extracted_value"], "red")

            with_html = analyze_dataframe(
                queries,
                schema_df=schema_frame(),
                default_schema="tpcds",
                output_dir=output / "html",
                build_html=True,
            )
            html_path = with_html.artifact_paths["html"]
            self.assertTrue(html_path.exists())
            self.assertIn(
                "warehouse.tpcds.item.i_color",
                html_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
