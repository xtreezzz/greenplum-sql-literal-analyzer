import unittest

from gp_sql_analyzer.catalog_html import render_catalog_html
from gp_sql_analyzer.catalog_stats import build_catalog_report_from_details
from gp_sql_analyzer.schema import MappingSchemaProvider


class CatalogHtmlTests(unittest.TestCase):
    def _report(self):
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "item": ["i_color", "i_unused"],
                    "sales": ["s_amount"],
                }
            },
            default_schema="tpcds",
        )
        details = [
            {
                "query_id": "q-mask",
                "source_row_count": 3,
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
            },
            {
                "query_id": "q-ambiguous",
                "source_row_count": 1,
                "base_columns": ["tpcds.item.i_color", "tpcds.sales.i_color"],
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
        return build_catalog_report_from_details(
            details,
            schema,
            source_label="analytics.query_log",
            generated_at="2026-07-21T00:00:00Z",
        )

    def test_renders_every_table_column_value_and_quality_group(self) -> None:
        html = render_catalog_html(self._report())

        self.assertIn("Статистика использования SQL", html)
        self.assertIn("analytics.query_log", html)
        self.assertEqual(html.count('class="table-card"'), 2)
        self.assertIn("tpcds.item.i_color", html)
        self.assertIn("tpcds.item.i_unused", html)
        self.assertIn("purple", html)
        self.assertIn("%&amp;CHARACTER%", html)
        self.assertIn("like_contains", html)
        self.assertIn("Неоднозначные и неразрешённые случаи", html)
        self.assertIn("matches multiple sources", html)

    def test_is_self_contained_searchable_and_filterable(self) -> None:
        html = render_catalog_html(self._report())

        self.assertIn('id="catalog-search"', html)
        self.assertIn('id="activity-filter"', html)
        self.assertIn('id="context-filter"', html)
        self.assertIn('id="pattern-filter"', html)
        self.assertIn('id="visible-column-count"', html)
        self.assertIn("addEventListener", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link href", html)

    def test_escapes_values_and_embeds_search_data(self) -> None:
        schema = MappingSchemaProvider(
            {"tpcds": {"item": ["i_color"]}}, default_schema="tpcds"
        )
        report = build_catalog_report_from_details(
            [
                {
                    "query_id": "q-xss",
                    "base_columns": ["tpcds.item.i_color"],
                    "lineage_status": "resolved",
                    "clause_context": "SELECT",
                    "operator_or_function": "=",
                    "raw_literal": "<script>alert(1)</script>",
                    "extracted_value": "<script>alert(1)</script>",
                    "pattern_family": "exact_value",
                    "pattern_format": "exact_value",
                    "regex_features": {},
                }
            ],
            schema,
        )

        html = render_catalog_html(report)

        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertIn('data-contexts="SELECT"', html)


if __name__ == "__main__":
    unittest.main()
