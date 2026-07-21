import unittest

from gp_sql_analyzer.aggregate import UsageAggregator
from gp_sql_analyzer.models import ColumnRef, LineageResult, Occurrence


def occurrence(
    query_id: str,
    query_hash: str,
    template_hash: str,
    value: str,
    weight: int,
) -> Occurrence:
    return Occurrence(
        query_id=query_id,
        query_hash=query_hash,
        template_hash=template_hash,
        source_row_count=weight,
        lineage=LineageResult(
            "resolved", (ColumnRef(None, "tpcds", "item", "i_color"),)
        ),
        clause_context="WHERE",
        operator_or_function="IN",
        value_role="list_value",
        raw_literal=value,
        extracted_value=value,
        pattern_template="&CHARACTER",
        pattern_family="exact_value",
        pattern_format="exact_value",
        regex_features={},
        ast_path=f"statement[0].where.{query_id}",
    )


class UsageAggregatorTests(unittest.TestCase):
    def test_counts_source_rows_and_distinct_queries_and_templates(self) -> None:
        aggregator = UsageAggregator(example_limit=2)
        aggregator.add(occurrence("q1", "hash-1", "template-a", "purple", 3))
        aggregator.add(occurrence("q2", "hash-2", "template-a", "purple", 1))
        aggregator.add(occurrence("q3", "hash-3", "template-b", "burlywood", 1))

        rows = aggregator.rows()

        purple = next(row for row in rows if row["extracted_value"] == "purple")
        self.assertEqual(purple["source_row_count"], 4)
        self.assertEqual(purple["occurrence_count"], 2)
        self.assertEqual(purple["distinct_query_count"], 2)
        self.assertEqual(purple["distinct_template_count"], 1)
        self.assertEqual(purple["raw_literal"], "purple")
        self.assertEqual(purple["share_of_column"], 0.8)
        self.assertEqual(purple["example_query_ids"], ["q1", "q2"])

    def test_output_order_is_deterministic(self) -> None:
        aggregator = UsageAggregator()
        aggregator.add(occurrence("q2", "h2", "t2", "purple", 1))
        aggregator.add(occurrence("q1", "h1", "t1", "burlywood", 1))

        values = [row["extracted_value"] for row in aggregator.rows()]

        self.assertEqual(values, ["burlywood", "purple"])


if __name__ == "__main__":
    unittest.main()
