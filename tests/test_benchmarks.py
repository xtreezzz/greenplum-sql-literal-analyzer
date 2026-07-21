import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import certifi

import gp_sql_analyzer.benchmarks as benchmarks
from gp_sql_analyzer.benchmarks import (
    DUCKDB_COMMIT,
    SELECTED_QUERY_CHECKSUMS,
    benchmark_corpus,
    query_url,
)


# Reduced, PostgreSQL-parseable structures from TPC-DS Q14 and Q47. The full
# pinned upstream queries are exercised by the online benchmark command.
TPCDS_COMPLEX_EXCERPT = """
WITH cross_items AS (
    SELECT i_item_sk AS item_sk FROM item
    WHERE i_item_sk IN (
        SELECT ss_item_sk FROM store_sales
        INTERSECT
        SELECT cs_item_sk FROM catalog_sales
    )
), channel_sales AS (
    SELECT ss_item_sk AS item_sk, ss_sales_price AS sales FROM store_sales
    UNION ALL
    SELECT ws_item_sk, ws_ext_sales_price FROM web_sales
), ranked AS (
    SELECT item_sk, sales,
           rank() OVER (PARTITION BY item_sk ORDER BY sales DESC) AS rn
    FROM channel_sales
)
SELECT a.item_sk
FROM ranked AS a
JOIN ranked AS b ON a.item_sk = b.item_sk
WHERE a.item_sk IN (SELECT item_sk FROM cross_items)
"""


class BenchmarkTests(unittest.TestCase):
    @patch("gp_sql_analyzer.benchmarks.urllib.request.urlopen")
    @patch("gp_sql_analyzer.benchmarks.ssl.create_default_context")
    def test_downloader_uses_verified_certifi_ca_bundle(self, create_context, urlopen) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b"SELECT 1"
        urlopen.return_value = response
        tls_context = object()
        create_context.return_value = tls_context

        data = benchmarks._download(query_url(2))

        self.assertEqual(data, b"SELECT 1")
        create_context.assert_called_once_with(cafile=certifi.where())
        self.assertIs(urlopen.call_args.kwargs["context"], tls_context)

    def test_urls_are_pinned_and_selected_queries_have_checksums(self) -> None:
        self.assertIn(DUCKDB_COMMIT, query_url(64))
        self.assertNotIn("/main/", query_url(64))
        self.assertEqual(set(SELECTED_QUERY_CHECKSUMS), {2, 14, 34, 47, 64, 91})

    def test_complexity_report_detects_analytical_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "14-47-derived.sql").write_text(
                TPCDS_COMPLEX_EXCERPT, encoding="utf-8"
            )

            report = benchmark_corpus(Path(directory), dialect="postgres")

        self.assertEqual(report["files_seen"], 1)
        self.assertEqual(report["files_parsed"], 1)
        self.assertGreaterEqual(report["construct_counts"]["ctes"], 3)
        self.assertGreaterEqual(report["construct_counts"]["subqueries"], 1)
        self.assertGreaterEqual(report["construct_counts"]["set_operations"], 2)
        self.assertGreaterEqual(report["construct_counts"]["joins"], 1)
        self.assertGreaterEqual(report["construct_counts"]["windows"], 1)
        self.assertGreater(report["throughput_files_per_second"], 0)


if __name__ == "__main__":
    unittest.main()
