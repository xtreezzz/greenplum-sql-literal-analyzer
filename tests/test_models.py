import unittest

from gp_sql_analyzer.models import ColumnRef, LineageResult, Occurrence


class ModelTests(unittest.TestCase):
    def test_column_names_are_fully_qualified_when_catalog_is_available(self) -> None:
        column = ColumnRef("warehouse", "tpcds", "item", "i_color")

        self.assertEqual(column.qualified_name, "warehouse.tpcds.item.i_color")

    def test_occurrence_serialization_is_deterministic(self) -> None:
        columns = (
            ColumnRef(None, "tpcds", "item", "i_color"),
            ColumnRef(None, "archive", "item", "i_color"),
        )
        occurrence = Occurrence(
            query_id="q64",
            query_hash="query-hash",
            template_hash="template-hash",
            source_row_count=3,
            lineage=LineageResult("multi_source", columns),
            clause_context="WHERE",
            operator_or_function="IN",
            value_role="list_value",
            raw_literal="purple",
            extracted_value="purple",
            pattern_template="&CHARACTER",
            pattern_family="exact_value",
            pattern_format="exact_value",
            regex_features={},
            ast_path="statement[0].where.this.expressions[0]",
        )

        payload = occurrence.to_dict()

        self.assertEqual(
            payload["base_columns"], ["archive.item.i_color", "tpcds.item.i_color"]
        )
        self.assertEqual(payload["source_row_count"], 3)
        self.assertEqual(payload["lineage_status"], "multi_source")


if __name__ == "__main__":
    unittest.main()
