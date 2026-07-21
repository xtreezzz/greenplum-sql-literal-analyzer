import unittest

from gp_sql_analyzer.schema import MappingSchemaProvider
from tests.tpcds import TPCDS_SCHEMA


class MappingSchemaTests(unittest.TestCase):
    def test_resolves_unqualified_table_in_default_schema(self) -> None:
        provider = MappingSchemaProvider(TPCDS_SCHEMA, default_schema="tpcds")

        candidates = provider.resolve_table("item")

        self.assertEqual([candidate.qualified_name for candidate in candidates], ["tpcds.item"])
        self.assertTrue(provider.has_column(candidates[0], "i_color"))
        self.assertFalse(provider.has_column(candidates[0], "missing"))

    def test_without_default_schema_preserves_multiple_table_candidates(self) -> None:
        provider = MappingSchemaProvider(
            {
                "tpcds": {"item": ["i_color"]},
                "archive": {"item": ["i_color"]},
            }
        )

        candidates = provider.resolve_table("item")

        self.assertEqual(
            [candidate.qualified_name for candidate in candidates],
            ["archive.item", "tpcds.item"],
        )

    def test_exports_round_trip_snapshot_with_catalog_and_default_schema(self) -> None:
        provider = MappingSchemaProvider(
            {"tpcds": {"item": ["i_color", "i_item_sk"]}},
            default_schema="tpcds",
            catalog="warehouse",
        )

        snapshot = provider.to_snapshot()

        self.assertEqual(snapshot["catalog_name"], "warehouse")
        self.assertEqual(snapshot["default_schema"], "tpcds")
        self.assertEqual(
            snapshot["schemas"],
            {"tpcds": {"item": ["i_color", "i_item_sk"]}},
        )


if __name__ == "__main__":
    unittest.main()
