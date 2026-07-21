import tempfile
import unittest
from pathlib import Path

from gp_sql_analyzer.complexity import (
    analyze_corpus,
    analyze_query,
    complexity_score,
)


COMPLEX_SQL = """
WITH base AS (
    SELECT ss_item_sk AS item_sk,
           CASE WHEN ss_quantity > 10 THEN ss_ext_sales_price ELSE 0 END AS revenue
    FROM store_sales AS ss
    JOIN date_dim AS d ON d.d_date_sk = ss.ss_sold_date_sk
    WHERE d.d_year = 2001
), ranked AS (
    SELECT item_sk, revenue,
           rank() OVER (PARTITION BY item_sk ORDER BY revenue DESC) AS revenue_rank
    FROM base
), online AS (
    SELECT ws_item_sk AS item_sk
    FROM web_sales
    GROUP BY ws_item_sk
)
SELECT item_sk, sum(revenue) AS total_revenue
FROM ranked
WHERE item_sk IN (SELECT item_sk FROM online)
GROUP BY item_sk
HAVING sum(revenue) > 100
UNION ALL
SELECT cs_item_sk, sum(cs_ext_sales_price)
FROM catalog_sales
GROUP BY cs_item_sk
ORDER BY 1
"""


class QueryComplexityTests(unittest.TestCase):
    def test_corpus_uses_ddl_schema_to_resolve_literal_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queries = root / "queries"
            schema = root / "schema"
            queries.mkdir()
            schema.mkdir()
            (queries / "66.sql").write_text(
                "SELECT * FROM ship_mode WHERE sm_carrier IN ('DHL', 'BARIAN')",
                encoding="utf-8",
            )
            (schema / "ship_mode.sql").write_text(
                "CREATE TABLE ship_mode (sm_carrier VARCHAR(20));",
                encoding="utf-8",
            )

            corpus = analyze_corpus(
                queries,
                schema_directory=schema,
                default_schema="tpcds",
            )

        usage = corpus.queries[0].literal_usages[0]
        self.assertEqual(usage.values, ("DHL", "BARIAN"))
        self.assertEqual(
            usage.lineage.columns[0].qualified_name,
            "tpcds.ship_mode.sm_carrier",
        )

    def test_analyze_query_extracts_structural_metrics_and_names(self) -> None:
        query = analyze_query("14.sql", COMPLEX_SQL, dialect="postgres")

        self.assertTrue(query.parsed)
        self.assertIsNone(query.error)
        self.assertEqual(query.cte_count, 3)
        self.assertEqual(query.cte_names, ("base", "ranked", "online"))
        self.assertEqual(query.subquery_count, 1)
        self.assertEqual(query.set_operation_count, 1)
        self.assertEqual(query.set_operation_types, ("UNION ALL",))
        self.assertEqual(query.join_count, 1)
        self.assertEqual(query.join_types, ("INNER",))
        self.assertEqual(query.window_count, 1)
        self.assertEqual(query.window_functions, ("RANK",))
        self.assertEqual(query.case_count, 1)
        self.assertEqual(query.select_count, 6)
        self.assertEqual(query.where_count, 2)
        self.assertEqual(query.having_count, 1)
        self.assertEqual(query.group_count, 3)
        self.assertEqual(query.order_count, 2)
        self.assertCountEqual(query.table_names, (
            "store_sales",
            "date_dim",
            "web_sales",
            "catalog_sales",
        ))
        self.assertEqual(query.aggregate_functions, ("SUM",))
        self.assertGreater(query.node_count, 0)
        self.assertGreater(query.max_depth, 0)
        self.assertGreaterEqual(query.max_subquery_depth, 1)
        self.assertEqual(query.score, complexity_score(query))

    def test_corpus_is_ranked_by_score_and_keeps_parse_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "01.sql").write_text("SELECT 1", encoding="utf-8")
            (root / "14.sql").write_text(COMPLEX_SQL, encoding="utf-8")
            (root / "99.sql").write_text("SELECT FROM", encoding="utf-8")

            corpus = analyze_corpus(
                root,
                dialect="postgres",
                source_label="fixture",
                source_commit="abc123",
            )

        self.assertEqual(corpus.files_seen, 3)
        self.assertEqual(corpus.files_parsed, 2)
        self.assertEqual(len(corpus.errors), 1)
        self.assertEqual([query.name for query in corpus.queries], [
            "14.sql",
            "01.sql",
            "99.sql",
        ])
        self.assertEqual([query.rank for query in corpus.queries], [1, 2, 3])
        self.assertEqual(corpus.queries[0].tier, "extreme")
        self.assertFalse(corpus.queries[-1].parsed)
        self.assertIn("99.sql", corpus.errors[0])


if __name__ == "__main__":
    unittest.main()
