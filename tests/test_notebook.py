import builtins
import importlib
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, patch

import nbformat
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "sql_catalog_from_dataframe.ipynb"
EXPECTED_ANALYZER_ARCHIVE_URL = (
    "https://github.com/xtreezzz/greenplum-sql-literal-analyzer/"
    "archive/refs/heads/main.zip"
)
ANALYZER_REQUIREMENT = (
    f"gp-sql-analyzer @ {EXPECTED_ANALYZER_ARCHIVE_URL}"
)
PANDAS_REQUIREMENT = "pandas>=2,<3"
RESULT_FRAME_NAMES = (
    "row_analysis_df",
    "details_df",
    "aggregate_df",
    "catalog_tables_df",
    "catalog_columns_df",
    "errors_df",
)
MISSING = object()


def read_notebook() -> nbformat.NotebookNode:
    return nbformat.read(NOTEBOOK, as_version=4)


def code_cell_by_marker(
    notebook: nbformat.NotebookNode,
    marker: str,
) -> nbformat.NotebookNode:
    matches = [
        cell
        for cell in notebook.cells
        if cell.cell_type == "code" and marker in cell.source
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected exactly one code cell containing {marker!r}; "
            f"found {len(matches)}"
        )
    return matches[0]


def fixture_dataframes() -> tuple[pd.DataFrame, pd.DataFrame]:
    queries_df = pd.DataFrame(
        [
            {
                "query_id": "dds",
                "query_text": (
                    "SELECT d.dt FROM prod_dds.calendar_date AS d "
                    "WHERE d.dt = DATE '2026-01-15'"
                ),
                "query_text_template": (
                    "SELECT d.dt FROM prod_dds.calendar_date AS d "
                    "WHERE d.dt = DATE '&CHARACTER'"
                ),
                "source_row_count": 3,
            },
            {
                "query_id": "emart",
                "query_text": (
                    "SELECT c.dt FROM prod_emart.calendar_date AS c "
                    "WHERE c.dt >= DATE '2026-02-01'"
                ),
                "query_text_template": (
                    "SELECT c.dt FROM prod_emart.calendar_date AS c "
                    "WHERE c.dt >= DATE '&CHARACTER'"
                ),
                "source_row_count": 2,
            },
        ],
        index=[101, 205],
    )
    schema_df = pd.DataFrame(
        [
            {
                "table_schema": "prod_dds",
                "table_name": "calendar_date",
                "column_name": "dt",
            },
            {
                "table_schema": "prod_emart",
                "table_name": "calendar_date",
                "column_name": "dt",
            },
        ],
        index=[11, 22],
    )
    return queries_df, schema_df


class DataFrameNotebookTests(unittest.TestCase):
    def _exercise_dependency_bootstrap(
        self,
        *,
        auto_install: bool,
        fail_first_only: bool | None,
        import_target: str = "gp_sql_analyzer.dataframe",
        import_error_type: type[ImportError] = ModuleNotFoundError,
        install_error: subprocess.CalledProcessError | None = None,
        pandas_versions: tuple[object, ...] = ("2.2.3",),
        pandas_loaded: bool = False,
        loaded_pandas_version: object = "2.2.3",
        imported_pandas_version: object = "2.2.3",
    ) -> tuple[
        dict[str, object],
        list[str],
        list[list[str]],
        Exception | None,
    ]:
        notebook = read_notebook()
        dependency_source = code_cell_by_marker(
            notebook,
            "def import_analyzer",
        ).source
        config_source = code_cell_by_marker(notebook, "QUERY_DF_NAME =").source
        namespace: dict[str, object] = {"__name__": "__main__"}
        exec(compile(config_source, str(NOTEBOOK), "exec"), namespace)
        namespace["AUTO_INSTALL"] = auto_install

        imported_module = ModuleType("gp_sql_analyzer.dataframe")
        expected_analyzer = Mock(name="analyze_dataframe")
        imported_module.analyze_dataframe = expected_analyzer
        loaded_pandas_module = ModuleType("pandas")
        if loaded_pandas_version is not MISSING:
            loaded_pandas_module.__version__ = str(loaded_pandas_version)
        imported_pandas_module = ModuleType("pandas")
        if imported_pandas_version is not MISSING:
            imported_pandas_module.__version__ = str(imported_pandas_version)
        active_pandas_module = loaded_pandas_module if pandas_loaded else None
        import_attempts: list[str] = []
        dependency_events: list[str] = []
        version_checks: list[object] = []
        pandas_import_count = 0
        real_import = builtins.__import__
        real_import_module = importlib.import_module
        real_distribution_version = importlib_metadata.version

        def controlled_dependency_import(name: str) -> None:
            import_attempts.append(name)
            if fail_first_only is not None and (
                not fail_first_only or len(import_attempts) == 1
            ):
                raise import_error_type(f"Simulated broken import: {name}")

        remaining_versions = list(pandas_versions)

        def controlled_version(distribution_name: str) -> str:
            if distribution_name != "pandas":
                return real_distribution_version(distribution_name)
            if len(remaining_versions) > 1:
                version_or_error = remaining_versions.pop(0)
            else:
                version_or_error = remaining_versions[0]
            version_checks.append(version_or_error)
            if isinstance(version_or_error, BaseException):
                dependency_events.append(
                    f"version-error:{type(version_or_error).__name__}"
                )
                raise version_or_error
            dependency_events.append(f"version:{version_or_error}")
            return str(version_or_error)

        def controlled_import(
            name: str,
            globals_: dict[str, object] | None = None,
            locals_: dict[str, object] | None = None,
            fromlist: tuple[str, ...] = (),
            level: int = 0,
        ) -> object:
            nonlocal active_pandas_module, pandas_import_count
            if name == "pandas":
                pandas_import_count += 1
                dependency_events.append("import:pandas")
            if name == import_target:
                controlled_dependency_import(name)
                if name == "gp_sql_analyzer.dataframe":
                    return imported_module
            if name == "pandas":
                if active_pandas_module is None:
                    active_pandas_module = imported_pandas_module
                return active_pandas_module
            return real_import(name, globals_, locals_, fromlist, level)

        def controlled_import_module(
            name: str,
            package: str | None = None,
        ) -> object:
            if name == import_target:
                controlled_dependency_import(name)
                if name == "gp_sql_analyzer.dataframe":
                    return imported_module
            return real_import_module(name, package)

        run_mock = Mock(
            name="subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0),
        )
        check_call_mock = Mock(
            name="subprocess.check_call",
            return_value=0,
            side_effect=install_error,
        )
        error: Exception | None = None
        try:
            with patch.dict(sys.modules, {}, clear=False) as patched_modules:
                if pandas_loaded:
                    patched_modules["pandas"] = loaded_pandas_module
                else:
                    patched_modules.pop("pandas", None)
                with (
                    patch.object(
                        builtins,
                        "__import__",
                        side_effect=controlled_import,
                    ),
                    patch.object(
                        importlib,
                        "import_module",
                        side_effect=controlled_import_module,
                    ),
                    patch.object(
                        importlib_metadata,
                        "version",
                        side_effect=controlled_version,
                    ),
                    patch.object(subprocess, "run", run_mock),
                    patch.object(subprocess, "check_call", check_call_mock),
                ):
                    exec(
                        compile(dependency_source, str(NOTEBOOK), "exec"),
                        namespace,
                    )
        except Exception as caught:
            error = caught

        install_commands: list[list[str]] = []
        for subprocess_mock in (run_mock, check_call_mock):
            for call in subprocess_mock.call_args_list:
                command = call.args[0] if call.args else call.kwargs.get("args")
                if command is not None:
                    install_commands.append(list(command))
        namespace["_expected_analyzer"] = expected_analyzer
        namespace["_subprocess_run_calls"] = run_mock.call_count
        namespace["_subprocess_check_call_calls"] = check_call_mock.call_count
        namespace["_pandas_version_checks"] = version_checks
        namespace["_pandas_import_count"] = pandas_import_count
        namespace["_dependency_events"] = dependency_events
        namespace["_loaded_pandas_module"] = loaded_pandas_module
        namespace["_active_pandas_module"] = active_pandas_module
        return namespace, import_attempts, install_commands, error

    def _execute_portable_cells(
        self,
        *,
        queries: object = MISSING,
        schema: object = MISSING,
        config_overrides: dict[str, object] | None = None,
    ) -> dict[str, object]:
        notebook = read_notebook()
        sources = [
            code_cell_by_marker(notebook, marker).source
            for marker in (
                "QUERY_DF_NAME =",
                "def import_analyzer",
                "def resolve_dataframe",
                "result = analyze_dataframe(",
            )
        ]
        namespace: dict[str, object] = {"__name__": "__main__"}
        if queries is not MISSING:
            namespace["my_queries_df"] = queries
        if schema is not MISSING:
            namespace["my_schema_df"] = schema

        exec(compile(sources[0], str(NOTEBOOK), "exec"), namespace)
        namespace.update(config_overrides or {})
        for source in sources[1:]:
            exec(compile(source, str(NOTEBOOK), "exec"), namespace)
        return namespace

    def test_notebook_has_portable_dataframe_static_contract(self) -> None:
        notebook = read_notebook()
        code_cells = [cell for cell in notebook.cells if cell.cell_type == "code"]
        code_source = "\n".join(cell.source for cell in code_cells)
        stored_errors = [
            output
            for cell in code_cells
            for output in cell.get("outputs", [])
            if output.get("output_type") == "error"
        ]

        self.assertFalse(stored_errors, "Notebook must not store error outputs")
        self.assertTrue(
            all(cell.get("id") for cell in notebook.cells),
            "Every notebook cell must have an id",
        )
        for cell in code_cells:
            compile(cell.source, str(NOTEBOOK), "exec")

        config_source = code_cell_by_marker(notebook, "QUERY_DF_NAME =").source
        config: dict[str, object] = {}
        exec(compile(config_source, str(NOTEBOOK), "exec"), config)
        self.assertEqual(config.get("QUERY_DF_NAME"), "my_queries_df")
        self.assertEqual(config.get("SCHEMA_DF_NAME"), "my_schema_df")
        self.assertEqual(config.get("DEFAULT_SCHEMA"), "public")
        self.assertIsNone(config.get("OUTPUT_DIR"))
        self.assertIs(config.get("BUILD_HTML"), False)
        self.assertIs(config.get("AUTO_INSTALL"), True)
        self.assertEqual(
            config.get("ANALYZER_ARCHIVE_URL"),
            EXPECTED_ANALYZER_ARCHIVE_URL,
        )

        for expected in (
            "def import_analyzer",
            "def resolve_dataframe",
            "globals()[variable_name]",
            "result = analyze_dataframe(",
            "row_analysis_df = result.row_analysis_df",
            "details_df = result.details_df",
            "aggregate_df = result.aggregate_df",
            "catalog_tables_df = result.catalog_tables_df",
            "catalog_columns_df = result.catalog_columns_df",
            "errors_df = result.errors_df",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, code_source)

        for forbidden in (
            "connect_greenplum",
            "SourceQueryConfig",
            "iter_greenplum_records",
            "load_catalog_schema",
            "SOURCE_TABLE",
            'ROOT / "src"',
            "ROOT / 'src'",
            "sys.path.insert",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code_source)

    def test_dependency_bootstrap_installs_then_retries_import(self) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=True,
            )
        )

        self.assertIsNone(error)
        self.assertEqual(len(install_commands), 1)
        install_command = install_commands[0]
        self.assertEqual(
            install_command[:4],
            [sys.executable, "-m", "pip", "install"],
        )
        self.assertIn(ANALYZER_REQUIREMENT, install_command[4:])
        self.assertIn(PANDAS_REQUIREMENT, install_command[4:])
        self.assertLess(
            install_command.index(ANALYZER_REQUIREMENT),
            install_command.index(PANDAS_REQUIREMENT),
        )
        self.assertEqual(
            import_attempts,
            ["gp_sql_analyzer.dataframe", "gp_sql_analyzer.dataframe"],
        )
        self.assertIs(
            namespace.get("analyze_dataframe"),
            namespace["_expected_analyzer"],
        )
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)

    def test_dependency_bootstrap_disabled_fails_without_pip(self) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=False,
                fail_first_only=False,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("gp-sql-analyzer", str(error))
        self.assertIn(PANDAS_REQUIREMENT, str(error))
        self.assertIn("AUTO_INSTALL=True", str(error))
        self.assertIn(sys.executable, str(error))
        self.assertEqual(import_attempts, ["gp_sql_analyzer.dataframe"])
        self.assertEqual(install_commands, [])
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 0)

    def test_dependency_bootstrap_installs_after_broken_pandas_import(self) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=True,
                import_target="pandas",
                import_error_type=ImportError,
            )
        )

        self.assertIsNone(error)
        self.assertEqual(import_attempts, ["pandas", "pandas"])
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(
            install_commands[0],
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                ANALYZER_REQUIREMENT,
                PANDAS_REQUIREMENT,
            ],
        )
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)

    def test_dependency_bootstrap_disabled_wraps_broken_pandas_import(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=False,
                fail_first_only=False,
                import_target="pandas",
                import_error_type=ImportError,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, ImportError)
        message = str(error)
        self.assertIn("AUTO_INSTALL=True", message)
        self.assertIn(sys.executable, message)
        self.assertIn(ANALYZER_REQUIREMENT, message)
        self.assertIn(PANDAS_REQUIREMENT, message)
        self.assertIn("dependencies", message.lower())
        self.assertNotIn("gp-sql-analyzer is not installed", message)
        self.assertEqual(import_attempts, ["pandas"])
        self.assertEqual(install_commands, [])
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 0)

    def test_dependency_bootstrap_wraps_install_failure(self) -> None:
        install_error = subprocess.CalledProcessError(
            returncode=7,
            cmd=[sys.executable, "-m", "pip", "install"],
        )
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=False,
                install_error=install_error,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error.__cause__, install_error)
        message = str(error)
        self.assertIn(sys.executable, message)
        self.assertIn(ANALYZER_REQUIREMENT, message)
        self.assertIn(PANDAS_REQUIREMENT, message)
        self.assertIn("dependencies", message.lower())
        self.assertEqual(import_attempts, ["gp_sql_analyzer.dataframe"])
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)

    def test_dependency_bootstrap_wraps_retry_import_error(self) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=False,
                import_error_type=ImportError,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, ImportError)
        message = str(error)
        self.assertIn(sys.executable, message)
        self.assertIn(ANALYZER_REQUIREMENT, message)
        self.assertIn(PANDAS_REQUIREMENT, message)
        self.assertIn("dependencies", message.lower())
        self.assertIn("after installation", message.lower())
        self.assertEqual(
            import_attempts,
            ["gp_sql_analyzer.dataframe", "gp_sql_analyzer.dataframe"],
        )
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)

    def test_dependency_bootstrap_accepts_compatible_pandas_without_pip(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("2.3.1",),
            )
        )

        self.assertIsNone(error)
        self.assertEqual(namespace["_pandas_version_checks"], ["2.3.1"])
        self.assertEqual(install_commands, [])
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 0)
        self.assertGreaterEqual(namespace["_pandas_import_count"], 1)
        events = namespace["_dependency_events"]
        self.assertLess(events.index("version:2.3.1"), events.index("import:pandas"))
        self.assertEqual(import_attempts, ["gp_sql_analyzer.dataframe"])

    def test_dependency_bootstrap_upgrades_unloaded_incompatible_pandas(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("1.5.3", "2.2.3"),
                pandas_loaded=False,
            )
        )

        self.assertIsNone(error)
        self.assertEqual(
            namespace["_pandas_version_checks"],
            ["1.5.3", "2.2.3"],
        )
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)
        self.assertEqual(namespace["_pandas_import_count"], 1)
        self.assertEqual(
            namespace["_dependency_events"][:3],
            ["version:1.5.3", "version:2.2.3", "import:pandas"],
        )
        self.assertEqual(import_attempts, ["gp_sql_analyzer.dataframe"])

    def test_dependency_bootstrap_requires_restart_for_loaded_old_pandas(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("2.2.3",),
                pandas_loaded=True,
                loaded_pandas_version="1.5.3",
            )
        )

        self.assertIsInstance(error, RuntimeError)
        message = str(error)
        self.assertIn("1.5.3", message)
        self.assertIn("restart", message.lower())
        self.assertIn("DataFrame", message)
        self.assertIn(sys.executable, message)
        self.assertIn(ANALYZER_REQUIREMENT, message)
        self.assertIn(PANDAS_REQUIREMENT, message)
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)
        self.assertEqual(namespace["_pandas_import_count"], 0)
        self.assertEqual(import_attempts, [])
        self.assertNotIn("pd", namespace)
        self.assertNotIn("analyze_dataframe", namespace)

    def test_dependency_bootstrap_rejects_incompatible_pandas_without_auto_install(
        self,
    ) -> None:
        for pandas_loaded in (False, True):
            with self.subTest(pandas_loaded=pandas_loaded):
                namespace, import_attempts, install_commands, error = (
                    self._exercise_dependency_bootstrap(
                        auto_install=False,
                        fail_first_only=None,
                        pandas_versions=(
                            ("2.2.3",) if pandas_loaded else ("1.5.3",)
                        ),
                        pandas_loaded=pandas_loaded,
                        loaded_pandas_version="1.5.3",
                    )
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn("1.5.3", message)
                self.assertIn(sys.executable, message)
                self.assertIn(ANALYZER_REQUIREMENT, message)
                self.assertIn(PANDAS_REQUIREMENT, message)
                if pandas_loaded:
                    self.assertIn("restart", message.lower())
                    self.assertIn("DataFrame", message)
                self.assertEqual(install_commands, [])
                self.assertEqual(namespace["_subprocess_run_calls"], 0)
                self.assertEqual(namespace["_subprocess_check_call_calls"], 0)
                self.assertEqual(namespace["_pandas_import_count"], 0)
                self.assertEqual(import_attempts, [])

    def test_dependency_bootstrap_uses_loaded_compatible_pandas_over_metadata(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("1.5.3",),
                pandas_loaded=True,
                loaded_pandas_version="2.3.3",
            )
        )

        self.assertIsNone(error)
        self.assertIs(namespace["pd"], namespace["_loaded_pandas_module"])
        self.assertEqual(namespace["pd"].__version__, "2.3.3")
        self.assertEqual(namespace["_pandas_version_checks"], [])
        self.assertEqual(install_commands, [])
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 0)
        self.assertEqual(import_attempts, ["gp_sql_analyzer.dataframe"])

    def test_dependency_bootstrap_restarts_for_loaded_pandas_without_version(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("2.2.3",),
                pandas_loaded=True,
                loaded_pandas_version=MISSING,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        message = str(error)
        self.assertIn("restart", message.lower())
        self.assertIn("DataFrame", message)
        self.assertIn(sys.executable, message)
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_pandas_import_count"], 0)
        self.assertEqual(import_attempts, [])
        self.assertNotIn("pd", namespace)
        self.assertNotIn("analyze_dataframe", namespace)

    def test_dependency_bootstrap_restarts_for_incompatible_imported_pandas(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("2.2.3",),
                pandas_loaded=False,
                imported_pandas_version="1.5.3",
            )
        )

        self.assertIsInstance(error, RuntimeError)
        message = str(error)
        self.assertIn("1.5.3", message)
        self.assertIn("restart", message.lower())
        self.assertIn("DataFrame", message)
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_pandas_import_count"], 1)
        self.assertEqual(import_attempts, [])
        self.assertNotIn("pd", namespace)
        self.assertNotIn("analyze_dataframe", namespace)

    def test_dependency_bootstrap_rejects_incompatible_pandas_after_pip(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                auto_install=True,
                fail_first_only=None,
                pandas_versions=("1.5.3", "3.0.0"),
                pandas_loaded=False,
            )
        )

        self.assertIsInstance(error, RuntimeError)
        message = str(error)
        self.assertIn("3.0.0", message)
        self.assertIn(sys.executable, message)
        self.assertIn(ANALYZER_REQUIREMENT, message)
        self.assertIn(PANDAS_REQUIREMENT, message)
        self.assertEqual(len(install_commands), 1)
        self.assertEqual(namespace["_subprocess_run_calls"], 0)
        self.assertEqual(namespace["_subprocess_check_call_calls"], 1)
        self.assertEqual(namespace["_pandas_import_count"], 0)
        self.assertEqual(import_attempts, [])

    def test_dependency_bootstrap_rejects_unknown_pandas_version(self) -> None:
        unknown_versions = (
            importlib_metadata.PackageNotFoundError("pandas"),
            "not-a-version",
        )
        for unknown_version in unknown_versions:
            with self.subTest(unknown_version=repr(unknown_version)):
                namespace, import_attempts, install_commands, error = (
                    self._exercise_dependency_bootstrap(
                        auto_install=False,
                        fail_first_only=None,
                        pandas_versions=(unknown_version,),
                        pandas_loaded=False,
                    )
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn(sys.executable, message)
                self.assertIn(ANALYZER_REQUIREMENT, message)
                self.assertIn(PANDAS_REQUIREMENT, message)
                self.assertEqual(install_commands, [])
                self.assertEqual(namespace["_subprocess_run_calls"], 0)
                self.assertEqual(namespace["_subprocess_check_call_calls"], 0)
                self.assertEqual(namespace["_pandas_import_count"], 0)
                self.assertEqual(import_attempts, [])

    def test_notebook_analyzes_named_dataframes_without_mutating_inputs(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        original_queries = queries_df.copy(deep=True)
        original_schema = schema_df.copy(deep=True)

        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )

        self.assertEqual(len(namespace["row_analysis_df"]), 2)
        details_df = namespace["details_df"]
        expected_lineage = {
            "dds": "prod_dds.calendar_date.dt",
            "emart": "prod_emart.calendar_date.dt",
        }
        self.assertEqual(set(details_df["query_id"]), set(expected_lineage))
        for query_id, expected_column in expected_lineage.items():
            with self.subTest(query_id=query_id):
                query_details = details_df.loc[details_df["query_id"] == query_id]
                self.assertFalse(query_details.empty)
                self.assertEqual(
                    set(query_details["lineage_status"]),
                    {"resolved"},
                )
                self.assertTrue(
                    all(
                        base_columns == [expected_column]
                        for base_columns in query_details["base_columns"]
                    )
                )
        for name in RESULT_FRAME_NAMES:
            with self.subTest(result_frame=name):
                self.assertIsInstance(namespace[name], pd.DataFrame)
        pd.testing.assert_frame_equal(queries_df, original_queries, check_exact=True)
        pd.testing.assert_frame_equal(schema_df, original_schema, check_exact=True)

    def test_notebook_names_missing_query_dataframe(self) -> None:
        _, schema_df = fixture_dataframes()

        with self.assertRaisesRegex(NameError, "my_queries_df"):
            self._execute_portable_cells(schema=schema_df)

    def test_notebook_names_missing_query_template_column(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        queries_df = queries_df.drop(columns=["query_text_template"])

        with self.assertRaises(ValueError) as raised:
            self._execute_portable_cells(
                queries=queries_df,
                schema=schema_df,
            )
        self.assertIn("my_queries_df", str(raised.exception))
        self.assertIn("query_text_template", str(raised.exception))

    def test_notebook_rejects_non_dataframe_query_object(self) -> None:
        _, schema_df = fixture_dataframes()

        with self.assertRaisesRegex(TypeError, "my_queries_df.*DataFrame"):
            self._execute_portable_cells(
                queries=[{"query_text": "SELECT 1"}],
                schema=schema_df,
            )

    def test_notebook_names_missing_schema_dataframe(self) -> None:
        queries_df, _ = fixture_dataframes()

        with self.assertRaisesRegex(NameError, "my_schema_df"):
            self._execute_portable_cells(queries=queries_df)

    def test_notebook_rejects_non_dataframe_schema_object(self) -> None:
        queries_df, _ = fixture_dataframes()

        with self.assertRaisesRegex(TypeError, "my_schema_df.*DataFrame"):
            self._execute_portable_cells(
                queries=queries_df,
                schema=[{"table_schema": "prod_dds"}],
            )

    def test_notebook_names_missing_schema_required_column(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        schema_df = schema_df.drop(columns=["column_name"])

        with self.assertRaises(ValueError) as raised:
            self._execute_portable_cells(
                queries=queries_df,
                schema=schema_df,
            )
        self.assertIn("my_schema_df", str(raised.exception))
        self.assertIn("column_name", str(raised.exception))

    def test_notebook_allows_schema_dataframe_to_be_disabled(self) -> None:
        queries_df, _ = fixture_dataframes()

        namespace = self._execute_portable_cells(
            queries=queries_df,
            config_overrides={"SCHEMA_DF_NAME": None},
        )

        self.assertIsNone(namespace["schema_df"])
        self.assertEqual(len(namespace["row_analysis_df"]), 2)

    def test_notebook_executes_outside_repository_with_installed_package(self) -> None:
        notebook = read_notebook()
        config_cell = code_cell_by_marker(notebook, "QUERY_DF_NAME =")
        config_cell.source += "\nAUTO_INSTALL = False\n"
        dependency_cell = code_cell_by_marker(notebook, "def import_analyzer")
        dependency_index = notebook.cells.index(dependency_cell)
        queries_df, schema_df = fixture_dataframes()
        fixture_source = (
            "import pandas as pd\n"
            f"my_queries_df = pd.DataFrame({queries_df.to_dict(orient='records')!r})\n"
            f"my_schema_df = pd.DataFrame({schema_df.to_dict(orient='records')!r})\n"
        )
        assertions_source = "\n".join(
            [
                "_result_frames = {",
                *[f"    {name!r}: {name}," for name in RESULT_FRAME_NAMES],
                "}",
                "for _name, _frame in _result_frames.items():",
                "    assert isinstance(_frame, pd.DataFrame), _name",
                "assert AUTO_INSTALL is False",
                "import gp_sql_analyzer as _installed_package",
                "from pathlib import Path as _Path",
                "import os as _os",
                "_installed_root = _Path(_os.environ['PYTHONPATH']).resolve()",
                "_package_path = _Path(_installed_package.__file__).resolve()",
                "assert _package_path.is_relative_to(_installed_root)",
                "assert len(row_analysis_df) == 2",
                "_physical_columns = {",
                "    column",
                "    for columns in details_df['base_columns']",
                "    for column in columns",
                "}",
                "assert _physical_columns == {",
                "    'prod_dds.calendar_date.dt',",
                "    'prod_emart.calendar_date.dt',",
                "}",
            ]
        )
        notebook.cells.insert(
            dependency_index + 1,
            nbformat.v4.new_code_cell(fixture_source),
        )
        notebook.cells.append(nbformat.v4.new_code_cell(assertions_source))

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            site_path = temporary_path / "site"
            notebook_path = temporary_path / NOTEBOOK.name
            executed_path = temporary_path / "executed.ipynb"
            nbformat.write(notebook, notebook_path)

            environment = os.environ.copy()
            environment["PIP_NO_INDEX"] = "1"
            environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
            environment["PYTHONPATH"] = str(site_path)
            install = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--no-deps",
                    "--no-build-isolation",
                    "--target",
                    str(site_path),
                    str(ROOT),
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            self.assertEqual(
                install.returncode,
                0,
                install.stdout + install.stderr,
            )

            execution = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "jupyter",
                    "nbconvert",
                    "--to",
                    "notebook",
                    "--execute",
                    "--ExecutePreprocessor.timeout=120",
                    "--output",
                    executed_path.name,
                    notebook_path.name,
                ],
                cwd=temporary_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            self.assertEqual(
                execution.returncode,
                0,
                execution.stdout + execution.stderr,
            )
            executed_notebook = nbformat.read(executed_path, as_version=4)
            stored_errors = [
                output
                for cell in executed_notebook.cells
                if cell.cell_type == "code"
                for output in cell.get("outputs", [])
                if output.get("output_type") == "error"
            ]
            self.assertFalse(stored_errors)


if __name__ == "__main__":
    unittest.main()
