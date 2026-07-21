import unittest

from gp_sql_analyzer.complexity import CorpusComplexity, analyze_query
from gp_sql_analyzer.html_report import render_html
from gp_sql_analyzer.schema import MappingSchemaProvider


class HtmlReportTests(unittest.TestCase):
    def test_report_shows_values_physical_columns_operators_and_contexts(self) -> None:
        schema = MappingSchemaProvider(
            {
                "tpcds": {
                    "web_sales": ["ws_ship_mode_sk"],
                    "catalog_sales": ["cs_ship_mode_sk"],
                    "ship_mode": ["sm_ship_mode_sk", "sm_carrier"],
                }
            },
            default_schema="tpcds",
        )
        query = analyze_query(
            "66.sql",
            """
            SELECT * FROM (
              SELECT 'DHL,BARIAN' AS ship_carriers
              FROM web_sales, ship_mode
              WHERE ws_ship_mode_sk = sm_ship_mode_sk
                AND sm_carrier IN ('DHL', 'BARIAN')
              UNION ALL
              SELECT 'DHL,BARIAN' AS ship_carriers
              FROM catalog_sales, ship_mode
              WHERE cs_ship_mode_sk = sm_ship_mode_sk
                AND sm_carrier IN ('DHL', 'BARIAN')
            ) AS channels
            """,
            schema=schema,
        )
        query = query.__class__(**{
            **query.__dict__, "rank": 1, "tier": "extreme"
        })
        corpus = CorpusComplexity(
            source_label="fixture",
            source_commit="abc",
            dialect="postgres",
            files_seen=1,
            files_parsed=1,
            errors=(),
            queries=(query,),
            aggregate_counts=(("CTE", 0),),
            table_counts=(("ship_mode", 2),),
        )

        html = render_html(corpus)

        self.assertIn("Значения и условия", html)
        self.assertIn("tpcds.ship_mode.sm_carrier", html)
        self.assertIn("DHL", html)
        self.assertIn("BARIAN", html)
        self.assertIn("WHERE", html)
        self.assertIn(">IN<", html)
        self.assertIn("2 условия", html)
        self.assertIn("Без базовой колонки", html)
        self.assertIn("SELECT_LITERAL", html)

    def test_report_is_ranked_escaped_and_self_contained(self) -> None:
        simple = analyze_query("01.sql", "SELECT * FROM item WHERE price < 100")
        complex_query = analyze_query(
            "02.sql",
            """
            WITH sales AS (
                SELECT item_sk, sum(amount) AS revenue
                FROM store_sales
                GROUP BY item_sk
            )
            SELECT item_sk, rank() OVER (ORDER BY revenue DESC)
            FROM sales
            WHERE revenue > (SELECT avg(amount) FROM web_sales)
            """,
        )
        ranked = (
            complex_query.__class__(**{
                **complex_query.__dict__, "rank": 1, "tier": "extreme"
            }),
            simple.__class__(**{
                **simple.__dict__, "rank": 2, "tier": "high"
            }),
        )
        corpus = CorpusComplexity(
            source_label="TPC-DS fixture",
            source_commit="abc123",
            dialect="postgres",
            files_seen=2,
            files_parsed=2,
            errors=(),
            queries=ranked,
            aggregate_counts=(("CTE", 1), ("Подзапросы", 1), ("JOIN", 0)),
            table_counts=(("store_sales", 1), ("web_sales", 1), ("item", 1)),
        )

        html = render_html(corpus)

        self.assertIn("Разбор сложности TPC-DS", html)
        self.assertIn("TPC-DS fixture", html)
        self.assertIn("abc123", html)
        self.assertIn("0.1 × узлы AST", html)
        self.assertIn("Структурная сложность", html)
        self.assertIn("Макс. глубина", html)
        self.assertIn("Как разобран запрос", html)
        self.assertIn("id=\"query-search\"", html)
        self.assertIn("id=\"tier-filter\"", html)
        self.assertIn("data-rank=\"1\"", html)
        self.assertEqual(html.count("class=\"query-card"), 2)
        self.assertLess(html.index("02.sql"), html.index("01.sql"))
        self.assertIn("price &lt; 100", html)
        self.assertNotIn("<script src", html)
        self.assertNotIn("<link ", html)

    def test_report_keeps_parse_errors_visible(self) -> None:
        broken = analyze_query("99.sql", "SELECT FROM")
        broken = broken.__class__(**{**broken.__dict__, "rank": 1})
        corpus = CorpusComplexity(
            source_label="fixture",
            source_commit="abc",
            dialect="postgres",
            files_seen=1,
            files_parsed=0,
            errors=("99.sql: ParseError",),
            queries=(broken,),
            aggregate_counts=(),
            table_counts=(),
        )

        html = render_html(corpus)

        self.assertIn("Ошибка разбора", html)
        self.assertIn("ParseError", html)
        self.assertIn("SELECT FROM", html)

    def test_responsive_grid_items_are_allowed_to_shrink(self) -> None:
        query = analyze_query("01.sql", "SELECT 1")
        query = query.__class__(**{
            **query.__dict__, "rank": 1, "tier": "extreme"
        })
        corpus = CorpusComplexity(
            source_label="fixture",
            source_commit="abc",
            dialect="postgres",
            files_seen=1,
            files_parsed=1,
            errors=(),
            queries=(query,),
            aggregate_counts=(("CTE", 0),),
            table_counts=(),
        )

        html = render_html(corpus)

        self.assertIn(".overview > * { min-width: 0; }", html)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", html)


if __name__ == "__main__":
    unittest.main()
