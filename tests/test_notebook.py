import ast
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import nbformat
import pandas as pd

from gp_sql_analyzer.greenplum import SourceQueryConfig


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "sql_catalog_from_dataframe.ipynb"


class DataFrameNotebookTests(unittest.TestCase):
    def _greenplum_load_context(
        self,
        **config_overrides: object,
    ) -> tuple[str, dict[str, object]]:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        config_source = next(
            cell.source
            for cell in notebook.cells
            if cell.cell_type == "code" and "SOURCE_TABLE =" in cell.source
        )
        load_source = next(
            cell.source
            for cell in notebook.cells
            if cell.cell_type == "code" and "source_config = SourceQueryConfig(" in cell.source
        )

        namespace: dict[str, object] = {
            "ROOT": ROOT,
            "pd": pd,
            "SourceQueryConfig": SourceQueryConfig,
        }
        exec(compile(config_source, str(NOTEBOOK), "exec"), namespace)
        namespace.update(config_overrides)

        connection = Mock(name="connection")
        namespace.update(
            {
                "connect_greenplum": Mock(
                    name="connect_greenplum",
                    return_value=connection,
                ),
                "iter_greenplum_records": Mock(
                    name="iter_greenplum_records",
                    return_value=[
                        [
                            SimpleNamespace(
                                query_id="query-1",
                                query_text="SELECT 'value'",
                                query_text_template="SELECT '&CHARACTER'",
                                source_row_count=1,
                            )
                        ]
                    ],
                ),
                "load_catalog_schema": Mock(
                    name="load_catalog_schema",
                    return_value=SimpleNamespace(tables=()),
                ),
            }
        )
        return load_source, namespace

    def test_notebook_default_refuses_unbounded_source_before_connection(self) -> None:
        load_source, namespace = self._greenplum_load_context()

        with self.assertRaisesRegex(RuntimeError, "unbounded Greenplum scan is refused"):
            exec(compile(load_source, str(NOTEBOOK), "exec"), namespace)

        namespace["connect_greenplum"].assert_not_called()

    def test_notebook_rejects_truthy_non_boolean_full_scan_opt_in(self) -> None:
        load_source, namespace = self._greenplum_load_context(
            ALLOW_FULL_SCAN="False"
        )

        with self.assertRaisesRegex(
            (TypeError, ValueError),
            "ALLOW_FULL_SCAN must be a boolean",
        ):
            exec(compile(load_source, str(NOTEBOOK), "exec"), namespace)

        namespace["connect_greenplum"].assert_not_called()

    def test_notebook_literal_true_opts_into_full_scan(self) -> None:
        load_source, namespace = self._greenplum_load_context(ALLOW_FULL_SCAN=True)

        exec(compile(load_source, str(NOTEBOOK), "exec"), namespace)

        namespace["connect_greenplum"].assert_called_once_with()
        self.assertIs(namespace["source_config"].preaggregate, True)
        self.assertIsNone(namespace["source_config"].since_column)
        self.assertIsNone(namespace["source_config"].min_id)
        self.assertIsNone(namespace["source_config"].max_id)

    def test_notebook_bounded_filter_reaches_connection_with_exact_config(self) -> None:
        load_source, namespace = self._greenplum_load_context(
            ID_COLUMN="event_id",
            SINCE_COLUMN="created_at",
            SINCE_VALUE="2026-07-01T00:00:00Z",
            MIN_ID=100,
            MAX_ID=200,
        )

        exec(compile(load_source, str(NOTEBOOK), "exec"), namespace)

        namespace["connect_greenplum"].assert_called_once_with()
        self.assertEqual(
            namespace["source_config"],
            SourceQueryConfig(
                table="analytics.query_log",
                id_column="event_id",
                since_column="created_at",
                since_value="2026-07-01T00:00:00Z",
                min_id=100,
                max_id=200,
                limit=None,
                preaggregate=True,
            ),
        )

    def test_notebook_requires_a_bounded_greenplum_source(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        code_source = "\n".join(
            cell.source for cell in notebook.cells if cell.cell_type == "code"
        )
        code_tree = ast.parse(code_source, filename=str(NOTEBOOK))

        assignments = {
            target.id: statement.value
            for statement in code_tree.body
            if isinstance(statement, ast.Assign)
            for target in statement.targets
            if isinstance(target, ast.Name)
        }
        self.assertIsInstance(assignments.get("ALLOW_FULL_SCAN"), ast.Constant)
        self.assertIs(assignments["ALLOW_FULL_SCAN"].value, False)
        for name in (
            "ID_COLUMN",
            "SINCE_COLUMN",
            "SINCE_VALUE",
            "MIN_ID",
            "MAX_ID",
        ):
            with self.subTest(config=name):
                self.assertIsInstance(assignments.get(name), ast.Constant)
                self.assertIsNone(assignments[name].value)

        source_config_call = next(
            node
            for node in ast.walk(code_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SourceQueryConfig"
        )
        source_config_keywords = {
            keyword.arg: keyword.value
            for keyword in source_config_call.keywords
            if keyword.arg is not None
        }
        for name in (
            "id_column",
            "since_column",
            "since_value",
            "min_id",
            "max_id",
        ):
            with self.subTest(source_config=name):
                self.assertIsInstance(source_config_keywords.get(name), ast.Name)
                self.assertEqual(
                    source_config_keywords[name].id,
                    name.upper(),
                    f"{name} must be passed through to SourceQueryConfig",
                )

        connect_call = next(
            node
            for node in ast.walk(code_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "connect_greenplum"
        )
        guards = [
            node
            for node in ast.walk(code_tree)
            if isinstance(node, ast.If)
            and node.lineno < connect_call.lineno
            and "ALLOW_FULL_SCAN" in ast.unparse(node.test)
            and any(
                isinstance(child, ast.Raise)
                and isinstance(child.exc, ast.Call)
                and child.exc.args
                and isinstance(child.exc.args[0], ast.Constant)
                and "unbounded Greenplum scan is refused" in child.exc.args[0].value
                for child in ast.walk(node)
            )
        ]
        self.assertTrue(
            guards,
            "Notebook must refuse an unbounded Greenplum scan before connecting",
        )

        self.assertIn(
            "for candidate in (Path.cwd().resolve(), *Path.cwd().resolve().parents):",
            code_source,
            "ROOT discovery must search current-directory ancestors",
        )
        self.assertIn(
            'if str(ROOT / "src") not in sys.path:',
            code_source,
            "ROOT discovery must avoid duplicate sys.path entries",
        )
        self.assertIn(
            "Unable to locate project src/gp_sql_analyzer",
            code_source,
            "ROOT discovery must fail clearly outside the project",
        )

        markdown_source = "\n".join(
            cell.source for cell in notebook.cells if cell.cell_type == "markdown"
        )
        self.assertIn("BATCH_SIZE controls fetch size only", markdown_source)
        self.assertIn("ALLOW_FULL_SCAN=True", markdown_source)
        self.assertIn("query_records and DataFrames are materialized eagerly", markdown_source)
        self.assertIn("OUTPUT_DIR writes/replaces JSON artifacts", markdown_source)
        self.assertIn("OUTPUT_DIR=None", markdown_source)

    def test_notebook_loads_greenplum_before_sqlglot_analysis(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        code_source = "\n".join(
            cell.source for cell in notebook.cells if cell.cell_type == "code"
        )
        errors = [
            output
            for cell in notebook.cells
            if cell.cell_type == "code"
            for output in cell.get("outputs", [])
            if output.get("output_type") == "error"
        ]

        self.assertFalse(errors)
        self.assertTrue(all(cell.get("id") for cell in notebook.cells))
        for cell in notebook.cells:
            if cell.cell_type == "code":
                compile(cell.source, str(NOTEBOOK), "exec")
        code_tree = ast.parse(code_source, filename=str(NOTEBOOK))

        for expected in (
            'SOURCE_TABLE = "analytics.query_log"',
            "connect_greenplum()",
            "SourceQueryConfig(",
            "preaggregate=True",
            "iter_greenplum_records(",
            "load_catalog_schema(",
            "queries_df =",
            "schema_df =",
            "row_analysis_df = result.row_analysis_df",
            "details_df = result.details_df",
            "aggregate_df = result.aggregate_df",
            "catalog_tables_df = result.catalog_tables_df",
            "catalog_columns_df = result.catalog_columns_df",
            "errors_df = result.errors_df",
            "result = analyze_dataframe(",
            "build_html=BUILD_HTML",
        ):
            with self.subTest(expected=expected):
                self.assertTrue(
                    expected in code_source,
                    f"Missing notebook contract: {expected}",
                )

        analysis_call = "result = analyze_dataframe("
        self.assertTrue(
            analysis_call in code_source,
            f"Missing notebook contract: {analysis_call}",
        )
        analysis_index = code_source.index(analysis_call)
        for preparation_step in (
            "connect_greenplum()",
            "SourceQueryConfig(",
            "iter_greenplum_records(",
            "load_catalog_schema(",
            "queries_df =",
            "schema_df =",
        ):
            with self.subTest(preparation_step=preparation_step):
                if preparation_step not in code_source:
                    self.fail(f"Missing notebook contract: {preparation_step}")
                else:
                    self.assertLess(
                        code_source.index(preparation_step),
                        analysis_index,
                        f"Preparation must precede analysis: {preparation_step}",
                    )

        self.assertFalse("q44_style =" in code_source, "Demo q44 fixture must be absent")
        analysis_line = code_source[:analysis_index].count("\n") + 1
        build_html_assignments = [
            statement
            for statement in code_tree.body
            if statement.lineno < analysis_line
            and isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "BUILD_HTML"
                for target in (
                    statement.targets
                    if isinstance(statement, ast.Assign)
                    else [statement.target]
                )
            )
        ]
        self.assertTrue(
            build_html_assignments
            and isinstance(build_html_assignments[-1], (ast.Assign, ast.AnnAssign))
            and isinstance(build_html_assignments[-1].value, ast.Constant)
            and build_html_assignments[-1].value.value is False,
            "BUILD_HTML must be top-level False before analysis",
        )
        self.assertFalse(
            any(
                isinstance(node, ast.Name)
                and node.id == "GP_PASSWORD"
                and isinstance(node.ctx, ast.Store)
                for node in ast.walk(code_tree)
            ),
            "GP_PASSWORD must not be assigned in the notebook",
        )


if __name__ == "__main__":
    unittest.main()
