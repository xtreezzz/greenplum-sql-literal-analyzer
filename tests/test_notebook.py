import ast
import builtins
import importlib
import json
import os
import signal
import subprocess
import sys
import symtable
import tempfile
import time
import unittest
from importlib import metadata as importlib_metadata
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock, call, patch

import nbformat
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "sql_catalog_from_dataframe.ipynb"
PANDAS_REQUIREMENT = "pandas>=2,<3"
SQLGLOT_REQUIREMENT = "sqlglot>=25.34,<26"
DEPENDENCY_REQUIREMENTS = (PANDAS_REQUIREMENT, SQLGLOT_REQUIREMENT)
RUNTIME_STATE_MODULE_NAME = "_gp_sql_analyzer_notebook_runtime_state_v1"
RUNTIME_STATE_OWNER_MARKER = (
    "_gp_sql_analyzer_notebook_runtime_owner_v1"
)
RUNTIME_STATE_OWNER_TOKEN_KEY = (
    "_gp_sql_analyzer_notebook_owner_token_v1"
)
RUN_EPOCH_GLOBAL_KEY = "__gp-sql-analyzer:run-token:v1__"
TRUSTED_SYS_GLOBAL_KEY = "__gp-sql-analyzer:trusted-sys:v1__"
EXPLICIT_RESERVED_MAGIC_NAMES = frozenset(
    {
        "__builtins__",
        "__doc__",
        "__import__",
        "__loader__",
        "__name__",
        "__package__",
        "__spec__",
    }
)
PRE_CONFIG_BINDING_NAMES = frozenset(
    {
        "QUERY_DF_NAME",
        "SCHEMA_DF_NAME",
        "NOTEBOOK_RESERVED_INPUT_NAMES",
        "DEFAULT_SCHEMA",
        "OUTPUT_DIR",
        "BUILD_HTML",
        "AUTO_INSTALL",
        "PANDAS_REQUIREMENT",
        "SQLGLOT_REQUIREMENT",
        "DEPENDENCY_REQUIREMENTS",
        "_raise_invalid_notebook_config",
    }
)
RESULT_FRAME_NAMES = (
    "row_analysis_df",
    "details_df",
    "aggregate_df",
    "catalog_tables_df",
    "catalog_columns_df",
    "errors_df",
)
READINESS_TOKEN_NAMES = (
    "_DEPENDENCIES_READY_TOKEN",
    "_ANALYZER_READY_TOKEN",
    "_INPUTS_READY_TOKEN",
    "_ANALYSIS_READY_TOKEN",
)
ANALYZER_AND_DOWNSTREAM_STATE_NAMES = (
    "analyze_dataframe",
    "_ANALYZER_READY_TOKEN",
    "input_queries_df",
    "input_schema_df",
    "schema_df",
    "_INPUTS_READY_TOKEN",
    "result",
    *RESULT_FRAME_NAMES,
    "_ANALYSIS_READY_TOKEN",
)
INPUT_AND_DOWNSTREAM_STATE_NAMES = (
    "input_queries_df",
    "input_schema_df",
    "schema_df",
    "_INPUTS_READY_TOKEN",
    "result",
    *RESULT_FRAME_NAMES,
    "_ANALYSIS_READY_TOKEN",
)
RESULT_STATE_NAMES = ("result", *RESULT_FRAME_NAMES, "_ANALYSIS_READY_TOKEN")
MISSING = object()
NOTEBOOK_EXECUTION_DRIVER = """
import sys
from pathlib import Path

import nbformat
from nbclient import NotebookClient

notebook_path = Path(sys.argv[1]).resolve()
executed_path = Path(sys.argv[2]).resolve()
notebook = nbformat.read(notebook_path, as_version=4)
client = NotebookClient(
    notebook,
    timeout=180,
    kernel_name="codex-current-python",
    allow_errors=False,
)
try:
    client.execute(cwd=str(notebook_path.parent))
finally:
    nbformat.write(notebook, executed_path)
"""
HTML_EXTERNAL_RESOURCE_CHECK_SOURCE = r"""
import re
from html.parser import HTMLParser


def _is_external_reference(value):
    normalized = value.strip().lower()
    return (
        normalized.startswith("http:")
        or normalized.startswith("https:")
        or normalized.startswith("//")
    )


class _ExternalResourceParser(HTMLParser):
    _RESOURCE_ATTRIBUTES = {
        "script": {"src"},
        "link": {"href"},
        "img": {"src", "srcset"},
        "image": {"href", "xlink:href"},
        "source": {"src", "srcset"},
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.references = []

    def handle_starttag(self, tag, attrs):
        resource_attributes = self._RESOURCE_ATTRIBUTES.get(tag.lower(), set())
        for attribute, value in attrs:
            if value is None or attribute.lower() not in resource_attributes:
                continue
            candidates = (
                [part.strip().split()[0] for part in value.split(",") if part.strip()]
                if attribute.lower() == "srcset"
                else [value]
            )
            self.references.extend(
                candidate
                for candidate in candidates
                if _is_external_reference(candidate)
            )

    handle_startendtag = handle_starttag


def external_resource_references(html):
    parser = _ExternalResourceParser()
    parser.feed(html)
    references = list(parser.references)
    for match in re.finditer(
        r"url\(\s*(['\"]?)(.*?)\1\s*\)",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        candidate = match.group(2).strip()
        if _is_external_reference(candidate):
            references.append(candidate)
    return references
"""
PROJECT_PACKAGE_BLOCKER_DEFINITION_SOURCE = """
import importlib.abc


class _ProjectPackageBlocker(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if (
            fullname == "gp_sql_analyzer"
            or fullname.startswith("gp_sql_analyzer.")
        ):
            raise ModuleNotFoundError(
                "project package is blocked in standalone test",
                name=fullname,
            )
        return None
"""


def parse_git_worktree_roots(
    porcelain: bytes,
    *,
    required_root: Path,
) -> tuple[Path, ...]:
    resolved_root = required_root.resolve()
    roots: list[Path] = []
    for field in porcelain.split(b"\0"):
        if not field.startswith(b"worktree "):
            continue
        worktree_path = Path(os.fsdecode(field.removeprefix(b"worktree ")))
        if not worktree_path.is_absolute():
            raise AssertionError(
                f"Git reported a non-absolute worktree path: {worktree_path}"
            )
        resolved_path = worktree_path.resolve()
        if resolved_path not in roots:
            roots.append(resolved_path)
    if resolved_root not in roots:
        roots.insert(0, resolved_root)
    return tuple(roots)


def _git_diagnostic_text(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode(
        sys.getfilesystemencoding(),
        errors="backslashreplace",
    )


def _git_worktree_error(
    command: list[str],
    reason: str,
    *,
    stdout: bytes | str | None = None,
    stderr: bytes | str | None = None,
) -> AssertionError:
    return AssertionError(
        f"Git worktree command failed ({reason}): {' '.join(command)}\n"
        f"stdout:\n{_git_diagnostic_text(stdout)}\n"
        f"stderr:\n{_git_diagnostic_text(stderr)}"
    )


def git_worktree_roots(repository_root: Path) -> tuple[Path, ...]:
    environment = os.environ.copy()
    environment["GIT_TERMINAL_PROMPT"] = "0"
    command = ["git", "worktree", "list", "--porcelain", "-z"]
    try:
        execution = subprocess.run(
            command,
            cwd=repository_root,
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise _git_worktree_error(
            command,
            f"timed out after {error.timeout} seconds",
            stdout=error.output,
            stderr=error.stderr,
        ) from error
    except OSError as error:
        raise _git_worktree_error(
            command,
            f"{type(error).__name__}: {error}",
        ) from error
    if execution.returncode:
        raise _git_worktree_error(
            command,
            f"exit status {execution.returncode}",
            stdout=execution.stdout,
            stderr=execution.stderr,
        )
    return parse_git_worktree_roots(
        execution.stdout,
        required_root=repository_root,
    )


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


def code_cell_by_id(
    notebook: nbformat.NotebookNode,
    cell_id: str,
) -> nbformat.NotebookNode:
    matches = [
        cell
        for cell in notebook.cells
        if cell.cell_type == "code" and cell.get("id") == cell_id
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"Expected exactly one code cell with id {cell_id!r}; "
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


def remove_embedded_private_modules() -> None:
    for module_name in list(sys.modules):
        if (
            module_name == "_embedded_gp_sql_analyzer"
            or module_name.startswith("_embedded_gp_sql_analyzer.")
        ):
            del sys.modules[module_name]


def remove_sys_path_entry(path: str) -> None:
    while path in sys.path:
        sys.path.remove(path)


def notebook_top_level_binding_names(
    notebook: nbformat.NotebookNode,
) -> set[str]:
    class TopLevelBindingCollector(ast.NodeVisitor):
        def __init__(self) -> None:
            self.names: set[str] = set()

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                self.names.add(node.id)

        def visit_Import(self, node: ast.Import) -> None:
            for alias in node.names:
                self.names.add(alias.asname or alias.name.split(".", 1)[0])

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            for alias in node.names:
                if alias.name != "*":
                    self.names.add(alias.asname or alias.name)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self.names.add(node.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self.names.add(node.name)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            pass

        def visit_ListComp(self, node: ast.ListComp) -> None:
            pass

        visit_SetComp = visit_ListComp
        visit_DictComp = visit_ListComp
        visit_GeneratorExp = visit_ListComp

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if isinstance(node.name, str):
                self.names.add(node.name)
            for statement in node.body:
                self.visit(statement)

    collector = TopLevelBindingCollector()
    for cell in notebook.cells:
        if cell.cell_type != "code":
            continue
        for statement in ast.parse(cell.source).body:
            collector.visit(statement)
    return collector.names


def notebook_global_dependency_names(
    notebook: nbformat.NotebookNode,
) -> set[str]:
    names: set[str] = set()

    def collect(table: symtable.SymbolTable) -> None:
        for symbol in table.get_symbols():
            if symbol.is_global() and symbol.is_referenced():
                names.add(symbol.get_name())
        for child in table.get_children():
            collect(child)

    for index, cell in enumerate(notebook.cells):
        if cell.cell_type != "code":
            continue
        table = symtable.symtable(
            cell.source,
            f"{NOTEBOOK}:cell-{index}",
            "exec",
        )
        collect(table)
    return names


def notebook_reserved_input_names(
    notebook: nbformat.NotebookNode,
) -> set[str]:
    config_tree = ast.parse(
        code_cell_by_marker(notebook, "QUERY_DF_NAME =").source
    )
    reserved_assignments = [
        statement
        for statement in config_tree.body
        if isinstance(statement, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "NOTEBOOK_RESERVED_INPUT_NAMES"
            for target in statement.targets
        )
    ]
    if len(reserved_assignments) != 1:
        raise AssertionError(
            "Config must define one explicit NOTEBOOK_RESERVED_INPUT_NAMES set"
        )
    reserved_expression = reserved_assignments[0].value
    if (
        isinstance(reserved_expression, ast.Call)
        and isinstance(reserved_expression.func, ast.Name)
        and reserved_expression.func.id == "frozenset"
        and len(reserved_expression.args) == 1
    ):
        reserved_expression = reserved_expression.args[0]
    return set(ast.literal_eval(reserved_expression))


class DataFrameNotebookTests(unittest.TestCase):
    def _run_subprocess_command(
        self,
        command: list[str],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        popen_options: dict[str, object] = {}
        if os.name == "posix":
            popen_options["start_new_session"] = True
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
            popen_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **popen_options,
        )

        def stop_process_tree(*, force: bool) -> None:
            if os.name == "posix":
                signal_to_send = signal.SIGKILL if force else signal.SIGTERM
                try:
                    os.killpg(process.pid, signal_to_send)
                except ProcessLookupError:
                    pass
            elif force:
                process.kill()
            else:
                process.terminate()

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as timeout_error:
            stop_process_tree(force=False)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                stop_process_tree(force=True)
                stdout, stderr = process.communicate()
            raise AssertionError(
                f"Notebook execution timed out after {timeout} seconds.\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            ) from timeout_error
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr,
        )

    def _external_resource_references(self, html: str) -> list[str]:
        namespace: dict[str, object] = {}
        exec(HTML_EXTERNAL_RESOURCE_CHECK_SOURCE, namespace)
        checker = namespace["external_resource_references"]
        return checker(html)

    def _resolve_notebook_dataframes(
        self,
        queries_df: pd.DataFrame,
        schema_df: pd.DataFrame,
    ) -> dict[str, object]:
        notebook = read_notebook()
        namespace: dict[str, object] = {
            "__name__": "__main__",
            "pd": pd,
            "my_queries_df": queries_df,
            "my_schema_df": schema_df,
        }
        config_source = code_cell_by_marker(
            notebook,
            "QUERY_DF_NAME =",
        ).source
        exec(compile(config_source, str(NOTEBOOK), "exec"), namespace)
        run_token = namespace.get("_NOTEBOOK_RUN_TOKEN", object())
        namespace["_NOTEBOOK_RUN_TOKEN"] = run_token
        namespace["_DEPENDENCIES_READY_TOKEN"] = run_token
        namespace["_ANALYZER_READY_TOKEN"] = run_token
        resolver_source = code_cell_by_marker(
            notebook,
            "def resolve_dataframe",
        ).source
        exec(compile(resolver_source, str(NOTEBOOK), "exec"), namespace)
        return namespace

    def _portable_sources(self) -> dict[str, str]:
        notebook = read_notebook()
        return {
            "config": code_cell_by_marker(notebook, "QUERY_DF_NAME =").source,
            "payload": code_cell_by_id(
                notebook,
                "embedded-analyzer-payload",
            ).source,
            "bootstrap": code_cell_by_marker(
                notebook,
                "def dependency_is_compatible",
            ).source,
            "loader": code_cell_by_marker(
                notebook,
                "def load_embedded_analyzer",
            ).source,
            "resolver": code_cell_by_marker(
                notebook,
                "def resolve_dataframe",
            ).source,
            "analysis": code_cell_by_marker(
                notebook,
                "result = analyze_dataframe(",
            ).source,
            "results": code_cell_by_id(
                notebook,
                "portable-results",
            ).source,
        }

    def _config_source_with_input_names(
        self,
        source: str,
        *,
        query_name: str,
        schema_name: str | None,
    ) -> str:
        schema_literal = "None" if schema_name is None else repr(schema_name)
        return source.replace(
            'QUERY_DF_NAME = "my_queries_df"',
            f"QUERY_DF_NAME = {query_name!r}",
            1,
        ).replace(
            'SCHEMA_DF_NAME = "my_schema_df"',
            f"SCHEMA_DF_NAME = {schema_literal}",
            1,
        )

    def _capture_cell_error(
        self,
        source: str,
        namespace: dict[str, object],
    ) -> BaseException | None:
        try:
            exec(compile(source, str(NOTEBOOK), "exec"), namespace)
        except BaseException as error:
            return error
        return None

    def _assert_state_absent(
        self,
        namespace: dict[str, object],
        names: tuple[str, ...],
    ) -> None:
        for name in names:
            with self.subTest(stale_name=name):
                self.assertNotIn(name, namespace)

    def _assert_analysis_rejected(
        self,
        sources: dict[str, str],
        namespace: dict[str, object],
    ) -> None:
        error = self._capture_cell_error(sources["analysis"], namespace)
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("Run All", str(error))

    def _assert_user_state_preserved(
        self,
        namespace: dict[str, object],
        *,
        queries_df: pd.DataFrame,
        schema_df: pd.DataFrame,
        pandas_binding: object,
        sqlglot_binding: object,
    ) -> None:
        self.assertIs(namespace["my_queries_df"], queries_df)
        self.assertIs(namespace["my_schema_df"], schema_df)
        self.assertIs(namespace["pd"], pandas_binding)
        self.assertIs(namespace["sqlglot"], sqlglot_binding)

    def _capture_embedded_runtime(
        self,
        namespace: dict[str, object],
    ) -> tuple[object, str, Mock]:
        temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
        embedded_path = str(namespace["_EMBEDDED_ANALYZER_ZIP_PATH"])
        self.assertTrue(Path(str(temporary_directory.name)).is_dir())
        self.assertIn(embedded_path, sys.path)
        self.assertTrue(
            any(
                module_name == "_embedded_gp_sql_analyzer"
                or module_name.startswith("_embedded_gp_sql_analyzer.")
                for module_name in sys.modules
            )
        )
        cleanup_mock = Mock(wraps=temporary_directory.cleanup)
        temporary_directory.cleanup = cleanup_mock
        return temporary_directory, embedded_path, cleanup_mock

    def _assert_embedded_runtime_cleaned(
        self,
        namespace: dict[str, object],
        *,
        temporary_directory: object,
        embedded_path: str,
        cleanup_mock: Mock,
    ) -> None:
        cleanup_mock.assert_called_once_with()
        self.assertFalse(Path(str(temporary_directory.name)).exists())
        self.assertNotIn(embedded_path, sys.path)
        self.assertNotIn("_EMBEDDED_ANALYZER_TEMP_DIR", namespace)
        self.assertNotIn("_EMBEDDED_ANALYZER_ZIP_PATH", namespace)
        self.assertFalse(
            any(
                module_name == "_embedded_gp_sql_analyzer"
                or module_name.startswith("_embedded_gp_sql_analyzer.")
                for module_name in sys.modules
            )
        )

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX")
    def test_notebook_driver_timeout_kills_the_process_group(self) -> None:
        command = [sys.executable, "-c", "pass"]
        process = Mock(pid=4321, returncode=-signal.SIGKILL)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(command, 0.01),
            subprocess.TimeoutExpired(command, 5),
            ("partial stdout\n", "partial stderr\n"),
        ]

        with (
            patch.object(subprocess, "Popen", return_value=process) as popen,
            patch.object(os, "killpg") as kill_process_group,
        ):
            with self.assertRaises(AssertionError) as raised:
                self._run_subprocess_command(
                    command,
                    cwd=ROOT,
                    environment={},
                    timeout=0.01,
                )

        self.assertIn("partial stdout", str(raised.exception))
        self.assertIn("partial stderr", str(raised.exception))
        self.assertEqual(
            kill_process_group.call_args_list,
            [
                call(process.pid, signal.SIGTERM),
                call(process.pid, signal.SIGKILL),
            ],
        )
        self.assertIs(popen.call_args.kwargs["start_new_session"], True)

    @unittest.skipUnless(os.name == "posix", "process-group semantics are POSIX")
    def test_notebook_driver_timeout_stops_a_real_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            heartbeat_path = temporary_path / "child-heartbeat"
            child_source = "\n".join(
                [
                    "import sys",
                    "import time",
                    "from pathlib import Path",
                    f"path = Path({str(heartbeat_path)!r})",
                    "for heartbeat in range(1000):",
                    "    path.write_text(str(heartbeat), encoding='utf-8')",
                    "    time.sleep(0.02)",
                ]
            )
            parent_source = "\n".join(
                [
                    "import subprocess",
                    "import sys",
                    "import time",
                    f"child_source = {child_source!r}",
                    "child = subprocess.Popen([sys.executable, '-c', child_source])",
                    "print(child.pid, flush=True)",
                    "time.sleep(60)",
                ]
            )

            with self.assertRaisesRegex(AssertionError, "timed out"):
                self._run_subprocess_command(
                    [sys.executable, "-c", parent_source],
                    cwd=temporary_path,
                    environment=os.environ.copy(),
                    timeout=0.4,
                )

            self.assertTrue(heartbeat_path.exists())
            stopped_heartbeat = heartbeat_path.read_text(encoding="utf-8")
            time.sleep(0.1)
            self.assertEqual(
                heartbeat_path.read_text(encoding="utf-8"),
                stopped_heartbeat,
            )

    def test_html_external_resource_detection_covers_tags_and_css(self) -> None:
        external_documents = {
            "script": "<script src=https://cdn.example/app.js></script>",
            "link": "<link rel='stylesheet' href='//cdn.example/app.css'>",
            "image": '<img src="http://cdn.example/pixel.png">',
            "svg image": (
                "<svg><image href='https://cdn.example/vector.png'></image></svg>"
            ),
            "source": (
                "<source srcset='local.png 1x, "
                "https://cdn.example/large.png 2x'>"
            ),
            "style element": (
                "<style>.hero { background: url( //cdn.example/hero.png ) }</style>"
            ),
            "style attribute": (
                "<div style=\"background:url('https://cdn.example/card.png')\">"
            ),
        }
        for source, html in external_documents.items():
            with self.subTest(source=source):
                self.assertTrue(self._external_resource_references(html))

        self.assertEqual(
            self._external_resource_references(
                "<style>.safe{background:url(data:image/png;base64,AA==)}</style>"
                "<script>window.ready = true;</script>"
                "<img src='data:image/png;base64,AA=='>"
                "<a href='#local'>local</a>"
            ),
            [],
        )

    def test_project_package_blocker_handles_an_available_fake_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_root = Path(temporary_directory)
            fake_package = fake_root / "gp_sql_analyzer"
            fake_package.mkdir()
            (fake_package / "__init__.py").write_text("", encoding="utf-8")
            (fake_package / "dataframe.py").write_text("", encoding="utf-8")
            saved_project_modules = {
                module_name: module
                for module_name, module in sys.modules.items()
                if (
                    module_name == "gp_sql_analyzer"
                    or module_name.startswith("gp_sql_analyzer.")
                )
            }
            for module_name in saved_project_modules:
                del sys.modules[module_name]
            sys.path.insert(0, str(fake_root))
            try:
                self.assertIsNotNone(
                    importlib.machinery.PathFinder.find_spec(
                        "gp_sql_analyzer"
                    )
                )
                namespace: dict[str, object] = {}
                exec(PROJECT_PACKAGE_BLOCKER_DEFINITION_SOURCE, namespace)
                blocker_type = namespace["_ProjectPackageBlocker"]
                blocker = blocker_type()

                for module_name in (
                    "gp_sql_analyzer",
                    "gp_sql_analyzer.dataframe",
                ):
                    with self.subTest(module_name=module_name):
                        with self.assertRaises(ModuleNotFoundError) as raised:
                            blocker.find_spec(module_name)
                        self.assertEqual(raised.exception.name, module_name)
                self.assertIsNone(
                    blocker.find_spec("_embedded_gp_sql_analyzer")
                )

                sys.meta_path.insert(0, blocker)
                try:
                    with self.assertRaises(ModuleNotFoundError):
                        importlib.util.find_spec("gp_sql_analyzer")
                finally:
                    sys.meta_path.remove(blocker)
            finally:
                sys.path.remove(str(fake_root))
                for module_name in list(sys.modules):
                    if (
                        module_name == "gp_sql_analyzer"
                        or module_name.startswith("gp_sql_analyzer.")
                    ):
                        del sys.modules[module_name]
                sys.modules.update(saved_project_modules)

    def test_worktree_porcelain_parser_uses_roots_not_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            base_path = Path(temporary_directory)
            main_root = base_path / "Main Repository Ω"
            feature_root = base_path / "Feature Worktree Ж"
            porcelain = b"\0".join(
                [
                    b"worktree " + os.fsencode(main_root),
                    b"HEAD 1111111111111111111111111111111111111111",
                    b"branch refs/heads/main",
                    b"",
                    b"worktree " + os.fsencode(feature_root),
                    b"HEAD 2222222222222222222222222222222222222222",
                    b"detached",
                    b"",
                ]
            )

            roots = parse_git_worktree_roots(
                porcelain,
                required_root=main_root,
            )

            self.assertEqual(
                roots,
                (main_root.resolve(), feature_root.resolve()),
            )
            self.assertNotIn(main_root.parent.resolve(), roots)
            self.assertEqual(
                parse_git_worktree_roots(
                    b"worktree " + os.fsencode(feature_root) + b"\0",
                    required_root=main_root,
                ),
                (main_root.resolve(), feature_root.resolve()),
            )

            if os.name == "posix":
                newline_root = base_path / "Feature\nWorktree"
                self.assertEqual(
                    parse_git_worktree_roots(
                        b"worktree " + os.fsencode(newline_root) + b"\0",
                        required_root=main_root,
                    ),
                    (main_root.resolve(), newline_root.resolve()),
                )

    def test_git_worktree_discovery_wraps_nonzero_exit(self) -> None:
        command = ["git", "worktree", "list", "--porcelain", "-z"]
        completed = subprocess.CompletedProcess(
            command,
            7,
            stdout=b"partial stdout",
            stderr=b"fatal: partial stderr",
        )
        with patch.object(
            subprocess,
            "run",
            return_value=completed,
        ) as run_command:
            with self.assertRaises(AssertionError) as raised:
                git_worktree_roots(ROOT)

        message = str(raised.exception)
        self.assertIn("git worktree list --porcelain -z", message)
        self.assertIn("exit status 7", message)
        self.assertIn("partial stdout", message)
        self.assertIn("partial stderr", message)
        self.assertEqual(run_command.call_args.args[0], command)
        self.assertNotIn("text", run_command.call_args.kwargs)
        self.assertNotIn("shell", run_command.call_args.kwargs)

    def test_git_worktree_discovery_wraps_missing_git(self) -> None:
        with patch.object(
            subprocess,
            "run",
            side_effect=FileNotFoundError("git executable is missing"),
        ):
            with self.assertRaises(AssertionError) as raised:
                git_worktree_roots(ROOT)

        message = str(raised.exception)
        self.assertIn("git worktree list --porcelain -z", message)
        self.assertIn("git executable is missing", message)

    def test_git_worktree_discovery_wraps_timeout_with_partial_output(
        self,
    ) -> None:
        command = ["git", "worktree", "list", "--porcelain", "-z"]
        timeout_error = subprocess.TimeoutExpired(
            command,
            10,
            output=b"partial stdout",
            stderr=b"partial stderr",
        )
        with patch.object(subprocess, "run", side_effect=timeout_error):
            with self.assertRaises(AssertionError) as raised:
                git_worktree_roots(ROOT)

        message = str(raised.exception)
        self.assertIn("git worktree list --porcelain -z", message)
        self.assertIn("timed out", message)
        self.assertIn("partial stdout", message)
        self.assertIn("partial stderr", message)

    def test_schema_name_only_is_normalized_without_mutating_source(self) -> None:
        queries_df, _ = fixture_dataframes()
        schema_df = pd.DataFrame(
            [
                {
                    "schema_name": " prod_dds ",
                    "table_name": " calendar_date ",
                    "column_name": " dt ",
                }
            ],
            index=["alias-row"],
        )
        original_schema = schema_df.copy(deep=True)

        namespace = self._resolve_notebook_dataframes(queries_df, schema_df)

        pd.testing.assert_frame_equal(
            namespace["input_schema_df"],
            pd.DataFrame(
                [
                    {
                        "table_schema": "prod_dds",
                        "table_name": "calendar_date",
                        "column_name": "dt",
                    }
                ],
                index=["alias-row"],
            ),
        )
        pd.testing.assert_frame_equal(schema_df, original_schema, check_exact=True)

    def test_table_schema_only_remains_supported_and_is_normalized(self) -> None:
        queries_df, _ = fixture_dataframes()
        schema_df = pd.DataFrame(
            [
                {
                    "table_schema": " prod_dds ",
                    "table_name": " calendar_date ",
                    "column_name": " dt ",
                }
            ],
            index=["table-schema-row"],
        )

        namespace = self._resolve_notebook_dataframes(queries_df, schema_df)

        self.assertEqual(
            namespace["input_schema_df"].to_dict(orient="records"),
            [
                {
                    "table_schema": "prod_dds",
                    "table_name": "calendar_date",
                    "column_name": "dt",
                }
            ],
        )

    def test_equal_schema_columns_are_accepted_after_normalization(self) -> None:
        queries_df, _ = fixture_dataframes()
        schema_df = pd.DataFrame(
            [
                {
                    "table_schema": "prod_dds ",
                    "schema_name": " prod_dds",
                    "table_name": "calendar_date",
                    "column_name": "dt",
                }
            ],
            index=[17],
        )
        original_schema = schema_df.copy(deep=True)

        namespace = self._resolve_notebook_dataframes(queries_df, schema_df)

        normalized = namespace["input_schema_df"]
        self.assertNotIn("schema_name", normalized.columns)
        self.assertEqual(normalized.loc[17, "table_schema"], "prod_dds")
        pd.testing.assert_frame_equal(schema_df, original_schema, check_exact=True)

    def test_conflicting_schema_columns_name_the_rows(self) -> None:
        queries_df, _ = fixture_dataframes()
        schema_df = pd.DataFrame(
            [
                {
                    "table_schema": "prod_dds",
                    "schema_name": "prod_emart",
                    "table_name": "calendar_date",
                    "column_name": "dt",
                }
            ],
            index=["conflict-row"],
        )

        with self.assertRaises(ValueError) as raised:
            self._resolve_notebook_dataframes(queries_df, schema_df)

        message = str(raised.exception)
        self.assertIn("table_schema", message)
        self.assertIn("schema_name", message)
        self.assertIn("conflict-row", message)

    def test_invalid_schema_identifiers_name_the_column_and_rows(self) -> None:
        queries_df, _ = fixture_dataframes()
        for column_name in (
            "table_schema",
            "schema_name",
            "table_name",
            "column_name",
        ):
            for invalid_value in (None, pd.NA, 7, " \t"):
                with self.subTest(
                    column_name=column_name,
                    invalid_value=invalid_value,
                ):
                    row = {
                        "schema_name": "prod_dds",
                        "table_name": "calendar_date",
                        "column_name": "dt",
                    }
                    row[column_name] = invalid_value
                    schema_df = pd.DataFrame([row], index=["invalid-row"])

                    with self.assertRaises(ValueError) as raised:
                        self._resolve_notebook_dataframes(queries_df, schema_df)

                    message = str(raised.exception)
                    self.assertIn(column_name, message)
                    self.assertIn("invalid-row", message)

    def _execute_notebook_in_subprocess(
        self,
        notebook: nbformat.NotebookNode,
        *,
        temporary_path: Path,
        pythonpath: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        notebook_path = temporary_path / NOTEBOOK.name
        executed_path = temporary_path / "executed.ipynb"
        nbformat.write(notebook, notebook_path)

        jupyter_data = temporary_path / "jupyter-data"
        kernelspec = (
            jupyter_data / "kernels" / "codex-current-python" / "kernel.json"
        )
        kernelspec.parent.mkdir(parents=True)
        kernelspec.write_text(
            json.dumps(
                {
                    "argv": [
                        sys.executable,
                        "-m",
                        "ipykernel_launcher",
                        "-f",
                        "{connection_file}",
                    ],
                    "display_name": "Codex current Python",
                    "language": "python",
                }
            ),
            encoding="utf-8",
        )
        notebook.metadata["kernelspec"] = {
            "display_name": "Codex current Python",
            "language": "python",
            "name": "codex-current-python",
        }
        nbformat.write(notebook, notebook_path)

        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        if pythonpath is not None:
            environment["PYTHONPATH"] = str(pythonpath)
        environment["PIP_NO_INDEX"] = "1"
        environment["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
        environment["JUPYTER_PATH"] = str(jupyter_data)
        environment["JUPYTER_RUNTIME_DIR"] = str(
            temporary_path / "jupyter-runtime"
        )
        environment["IPYTHONDIR"] = str(temporary_path / "ipython")
        return self._run_subprocess_command(
            [
                sys.executable,
                "-c",
                NOTEBOOK_EXECUTION_DRIVER,
                notebook_path.name,
                executed_path.name,
            ],
            cwd=temporary_path,
            environment=environment,
            timeout=240,
        )

    def _prepare_integration_notebook(
        self,
        *,
        temporary_path: Path,
        expect_project_package: bool,
        build_html: bool,
    ) -> nbformat.NotebookNode:
        notebook = read_notebook()
        output_path = temporary_path / "artifacts"
        repository_paths = git_worktree_roots(ROOT)
        config_cell = code_cell_by_marker(notebook, "QUERY_DF_NAME =")
        config_cell.source += "\n".join(
            [
                "",
                'QUERY_DF_NAME = "standalone_queries_df"',
                'SCHEMA_DF_NAME = "standalone_schema_df"',
                "AUTO_INSTALL = False",
                f"OUTPUT_DIR = {str(output_path)!r}",
                f"BUILD_HTML = {build_html!r}",
                "",
            ]
        )
        dependency_cell = code_cell_by_marker(
            notebook,
            "def dependency_is_compatible",
        )
        dependency_index = notebook.cells.index(dependency_cell)
        if expect_project_package:
            package_isolation_source = [
                '_project_spec = importlib.util.find_spec("gp_sql_analyzer")',
                "assert _project_spec is not None",
            ]
        else:
            package_isolation_source = [
                "for _module_name in list(sys.modules):",
                "    if (",
                '        _module_name == "gp_sql_analyzer"',
                '        or _module_name.startswith("gp_sql_analyzer.")',
                "    ):",
                "        del sys.modules[_module_name]",
                "",
                *PROJECT_PACKAGE_BLOCKER_DEFINITION_SOURCE.splitlines(),
                "_project_package_blocker = _ProjectPackageBlocker()",
                "for _blocked_name in (",
                '    "gp_sql_analyzer",',
                '    "gp_sql_analyzer.dataframe",',
                "):",
                "    try:",
                "        _project_package_blocker.find_spec(_blocked_name)",
                "    except ModuleNotFoundError as _blocked_import:",
                "        assert _blocked_import.name == _blocked_name",
                "    else:",
                "        raise AssertionError(f\"did not block {_blocked_name}\")",
                "assert (",
                "    _project_package_blocker.find_spec(",
                '        "_embedded_gp_sql_analyzer"',
                "    )",
                "    is None",
                ")",
                "sys.meta_path.insert(0, _project_package_blocker)",
                "try:",
                '    importlib.util.find_spec("gp_sql_analyzer")',
                "except ModuleNotFoundError as _blocked_import:",
                '    assert _blocked_import.name == "gp_sql_analyzer"',
                "else:",
                "    raise AssertionError(",
                '        "gp_sql_analyzer import blocker did not run"',
                "    )",
            ]
        fixture_source = "\n".join(
            [
                "import importlib.util",
                "import sys",
                "from pathlib import Path",
                "",
                "_repository_paths = (",
                *[
                    f"    Path({str(repository_path)!r}).resolve(),"
                    for repository_path in repository_paths
                ],
                ")",
                "assert all(",
                "    Path.cwd().resolve() != repository_path",
                "    for repository_path in _repository_paths",
                ")",
                "assert all(",
                "    not entry",
                "    or all(",
                "        Path(entry).resolve() != repository_path",
                "        and repository_path not in Path(entry).resolve().parents",
                "        for repository_path in _repository_paths",
                "    )",
                "    for entry in sys.path",
                ")",
                *package_isolation_source,
                "",
                "import pandas as pd",
                "",
                "standalone_queries_df = pd.DataFrame([",
                "    {",
                '        "query_id": "dds",',
                '        "query_text": (',
                '            "SELECT d.dt FROM prod_dds.calendar_date AS d "',
                "            \"WHERE d.dt = DATE '2026-01-15'\"",
                "        ),",
                '        "query_text_template": (',
                '            "SELECT d.dt FROM prod_dds.calendar_date AS d "',
                "            \"WHERE d.dt = DATE '&CHARACTER'\"",
                "        ),",
                "    },",
                "    {",
                '        "query_id": "emart",',
                '        "query_text": (',
                '            "SELECT e.dt FROM prod_emart.calendar_date AS e "',
                "            \"WHERE e.dt >= DATE '2026-02-01'\"",
                "        ),",
                '        "query_text_template": (',
                '            "SELECT e.dt FROM prod_emart.calendar_date AS e "',
                "            \"WHERE e.dt >= DATE '&CHARACTER'\"",
                "        ),",
                "    },",
                "])",
                "standalone_schema_df = pd.DataFrame([",
                "    {",
                '        "schema_name": "prod_dds",',
                '        "table_name": "calendar_date",',
                '        "column_name": "dt",',
                "    },",
                "    {",
                '        "schema_name": "prod_emart",',
                '        "table_name": "calendar_date",',
                '        "column_name": "dt",',
                "    },",
                "])",
                "_original_queries_df = standalone_queries_df.copy(deep=True)",
                "_original_schema_df = standalone_schema_df.copy(deep=True)",
            ]
        )
        notebook.cells.insert(
            dependency_index,
            nbformat.v4.new_code_cell(
                fixture_source,
                id="standalone-integration-fixture",
            ),
        )

        assertions = [
            f"for _name in {RESULT_FRAME_NAMES!r}:",
            "    assert _name in globals(), _name",
            "    assert isinstance(globals()[_name], pd.DataFrame), _name",
            "assert len(row_analysis_df) == 2",
            "_resolved = {",
            "    (_row.query_id, tuple(_row.base_columns))",
            "    for _row in details_df.itertuples(index=False)",
            "    if _row.lineage_status == 'resolved'",
            "}",
            "assert {",
            "    ('dds', ('prod_dds.calendar_date.dt',)),",
            "    ('emart', ('prod_emart.calendar_date.dt',)),",
            "} <= _resolved, _resolved",
            "_catalog_columns = {",
            "    (",
            "        _row.qualified_name,",
            "        _row.usage_status,",
            "        _row.distinct_query_count,",
            "    )",
            "    for _row in catalog_columns_df.itertuples(index=False)",
            "}",
            "assert _catalog_columns == {",
            "    ('prod_dds.calendar_date.dt', 'active', 1),",
            "    ('prod_emart.calendar_date.dt', 'active', 1),",
            "}, _catalog_columns",
            "pd.testing.assert_frame_equal(",
            "    standalone_queries_df, _original_queries_df, check_exact=True",
            ")",
            "pd.testing.assert_frame_equal(",
            "    standalone_schema_df, _original_schema_df, check_exact=True",
            ")",
            "import _embedded_gp_sql_analyzer as _embedded_package",
            "assert 'embedded-analyzer.zip' in str(Path(_embedded_package.__file__))",
            "assert 'gp_sql_analyzer' not in sys.modules",
            "assert 'gp_sql_analyzer.dataframe' not in sys.modules",
        ]
        if build_html:
            assertions.extend(
                [
                    "_expected_artifacts = {",
                    "    'row_analysis': 'row_analysis.jsonl',",
                    "    'details': 'details.jsonl',",
                    "    'errors': 'errors.jsonl',",
                    "    'aggregate': 'aggregate.jsonl',",
                    "    'catalog_json': 'catalog-stats.json',",
                    "    'catalog_columns': 'catalog-columns.jsonl',",
                    "    'schema': 'schema.json',",
                    "    'html': 'catalog-stats.html',",
                    "}",
                    "assert set(result.artifact_paths) == set(_expected_artifacts)",
                    f"_output_path = Path({str(output_path)!r}).resolve()",
                    "for _artifact_name, _filename in _expected_artifacts.items():",
                    "    _artifact_path = Path(",
                    "        result.artifact_paths[_artifact_name]",
                    "    ).resolve()",
                    "    assert _artifact_path == _output_path / _filename",
                    "    assert _artifact_path.exists(), _artifact_path",
                    "_html = result.artifact_paths['html'].read_text(",
                    "    encoding='utf-8'",
                    ")",
                    "assert len(_html) > 1000",
                    "assert '<!doctype html>' in _html.lower()",
                    "assert '<style>' in _html.lower()",
                    "assert '<script>' in _html.lower()",
                    *HTML_EXTERNAL_RESOURCE_CHECK_SOURCE.splitlines(),
                    "_external_references = external_resource_references(_html)",
                    "assert not _external_references, _external_references",
                ]
            )
        notebook.cells.append(
            nbformat.v4.new_code_cell(
                "\n".join(assertions),
                id="standalone-integration-assertions",
            )
        )
        return notebook

    def _exercise_dependency_bootstrap(
        self,
        *,
        auto_install: bool = True,
        loaded_versions: dict[str, object] | None = None,
        imported_versions: dict[str, object] | None = None,
        metadata_versions: dict[str, tuple[object, ...]] | None = None,
        import_failures: dict[str, int] | None = None,
        install_error: subprocess.CalledProcessError | None = None,
    ) -> tuple[dict[str, object], list[str], list[list[str]], Exception | None]:
        notebook = read_notebook()
        dependency_source = code_cell_by_marker(
            notebook,
            "def dependency_is_compatible",
        ).source
        config_source = code_cell_by_marker(notebook, "QUERY_DF_NAME =").source
        namespace: dict[str, object] = {"__name__": "__main__"}
        exec(compile(config_source, str(NOTEBOOK), "exec"), namespace)
        namespace["AUTO_INSTALL"] = auto_install

        loaded_versions = loaded_versions or {}
        imported_versions = imported_versions or {
            "pandas": "2.3.3",
            "sqlglot": "25.34.1",
        }
        metadata_versions = metadata_versions or {
            "pandas": ("2.3.3",),
            "sqlglot": ("25.34.1",),
        }
        remaining_failures = dict(import_failures or {})
        metadata_remaining = {
            name: list(versions) for name, versions in metadata_versions.items()
        }
        imported_modules: dict[str, ModuleType] = {}
        loaded_modules: dict[str, ModuleType] = {}

        def module_with_version(name: str, version: object) -> ModuleType:
            module = ModuleType(name)
            if version is not MISSING:
                module.__version__ = str(version)
            return module

        for name, version in loaded_versions.items():
            loaded_modules[name] = module_with_version(name, version)
        for name, version in imported_versions.items():
            imported_modules[name] = module_with_version(name, version)

        import_attempts: list[str] = []
        version_checks: list[tuple[str, object]] = []
        real_import_module = importlib.import_module
        real_distribution_version = importlib_metadata.version

        def controlled_version(distribution_name: str) -> str:
            if distribution_name not in metadata_remaining:
                return real_distribution_version(distribution_name)
            values = metadata_remaining[distribution_name]
            value = values.pop(0) if len(values) > 1 else values[0]
            version_checks.append((distribution_name, value))
            if isinstance(value, BaseException):
                raise value
            return str(value)

        def controlled_import_module(
            name: str,
            package: str | None = None,
        ) -> object:
            if name in ("pandas", "sqlglot"):
                import_attempts.append(name)
                failures_left = remaining_failures.get(name, 0)
                if failures_left:
                    remaining_failures[name] = failures_left - 1
                    raise ImportError(f"Simulated broken import: {name}")
                module = imported_modules[name]
                sys.modules[name] = module
                return module
            return real_import_module(name, package)

        check_call_mock = Mock(
            name="subprocess.check_call",
            return_value=0,
            side_effect=install_error,
        )
        error: Exception | None = None
        try:
            with patch.dict(sys.modules, {}, clear=False) as patched_modules:
                for name in ("pandas", "sqlglot"):
                    patched_modules.pop(name, None)
                patched_modules.update(loaded_modules)
                with (
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
                    patch.object(
                        subprocess,
                        "check_call",
                        check_call_mock,
                    ),
                ):
                    exec(
                        compile(dependency_source, str(NOTEBOOK), "exec"),
                        namespace,
                    )
        except Exception as caught:
            error = caught

        install_commands = [
            list(call.args[0])
            for call in check_call_mock.call_args_list
            if call.args
        ]
        namespace["_version_checks"] = version_checks
        namespace["_loaded_modules"] = loaded_modules
        namespace["_imported_modules"] = imported_modules
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
            code_cell_by_marker(notebook, "QUERY_DF_NAME =").source,
            code_cell_by_id(notebook, "embedded-analyzer-payload").source,
            code_cell_by_marker(
                notebook,
                "def dependency_is_compatible",
            ).source,
            code_cell_by_marker(notebook, "def load_embedded_analyzer").source,
            code_cell_by_marker(notebook, "def resolve_dataframe").source,
            code_cell_by_marker(notebook, "result = analyze_dataframe(").source,
        ]
        namespace: dict[str, object] = {"__name__": "__main__"}
        if queries is not MISSING:
            namespace["my_queries_df"] = queries
        if schema is not MISSING:
            namespace["my_schema_df"] = schema

        exec(compile(sources[0], str(NOTEBOOK), "exec"), namespace)
        namespace.update(config_overrides or {})
        for source in sources[1:4]:
            exec(compile(source, str(NOTEBOOK), "exec"), namespace)

        temporary_directory = namespace.get("_EMBEDDED_ANALYZER_TEMP_DIR")
        embedded_path = namespace.get("_EMBEDDED_ANALYZER_ZIP_PATH")
        if temporary_directory is not None:
            self.addCleanup(temporary_directory.cleanup)
        if isinstance(embedded_path, str):
            self.addCleanup(
                lambda path=embedded_path: (
                    sys.path.remove(path) if path in sys.path else None
                )
            )
        self.addCleanup(remove_embedded_private_modules)
        for source in sources[4:]:
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
        self.assertEqual(config.get("PANDAS_REQUIREMENT"), PANDAS_REQUIREMENT)
        self.assertEqual(config.get("SQLGLOT_REQUIREMENT"), SQLGLOT_REQUIREMENT)
        self.assertEqual(
            config.get("DEPENDENCY_REQUIREMENTS"),
            DEPENDENCY_REQUIREMENTS,
        )

        for expected in (
            'QUERY_DF_NAME = "my_queries_df"',
            'SCHEMA_DF_NAME = "my_schema_df"',
            f'PANDAS_REQUIREMENT = "{PANDAS_REQUIREMENT}"',
            f'SQLGLOT_REQUIREMENT = "{SQLGLOT_REQUIREMENT}"',
            "EMBEDDED_ANALYZER_ZIP_B64",
            "EMBEDDED_ANALYZER_SHA256",
            "def load_embedded_analyzer",
            "from _embedded_gp_sql_analyzer.dataframe import",
            "def resolve_dataframe",
            "globals()[variable_name]",
            "result = analyze_dataframe(",
            "row_analysis_df = result.row_analysis_df",
            "details_df = result.details_df",
            "aggregate_df = result.aggregate_df",
            "catalog_tables_df = result.catalog_tables_df",
            "catalog_columns_df = result.catalog_columns_df",
            "errors_df = result.errors_df",
            '"_gp_sql_analyzer_notebook_runtime_owner_v1"',
            f'"{RUN_EPOCH_GLOBAL_KEY}"',
            f'"{TRUSTED_SYS_GLOBAL_KEY}"',
            "_DEPENDENCIES_READY_TOKEN",
            "_ANALYZER_READY_TOKEN",
            "_INPUTS_READY_TOKEN",
            "_ANALYSIS_READY_TOKEN",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, code_source)

        for forbidden in (
            "ANALYZER_ARCHIVE_URL",
            "github.com/xtreezzz",
            "gp-sql-analyzer @",
            "from gp_sql_analyzer",
            "connect_greenplum",
            "SourceQueryConfig",
            "iter_greenplum_records",
            "load_catalog_schema",
            "SOURCE_TABLE",
            'ROOT / "src"',
            "ROOT / 'src'",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, code_source)

    def test_every_top_level_notebook_binding_is_reserved_for_inputs(
        self,
    ) -> None:
        notebook = read_notebook()
        reserved_names = notebook_reserved_input_names(notebook)

        top_level_bindings = notebook_top_level_binding_names(notebook)
        self.assertEqual(
            top_level_bindings - reserved_names,
            set(),
            "Every top-level notebook write must be rejected as an input name",
        )
        self.assertNotIn("my_queries_df", top_level_bindings)
        self.assertNotIn("my_schema_df", top_level_bindings)

    def test_every_notebook_global_dependency_is_reserved_for_inputs(
        self,
    ) -> None:
        notebook = read_notebook()
        reserved_names = notebook_reserved_input_names(notebook)
        required_names = (
            notebook_top_level_binding_names(notebook)
            | notebook_global_dependency_names(notebook)
            | EXPLICIT_RESERVED_MAGIC_NAMES
        )

        self.assertEqual(
            required_names - reserved_names,
            set(),
            "Every notebook global read and write must be reserved for inputs",
        )
        self.assertNotIn("my_queries_df", required_names)
        self.assertNotIn("my_schema_df", required_names)

    def test_config_first_statement_rotates_private_epoch_before_snapshot(
        self,
    ) -> None:
        notebook = read_notebook()
        config_source = code_cell_by_marker(
            notebook,
            "QUERY_DF_NAME =",
        ).source
        config_tree = ast.parse(config_source)
        guard_index = next(
            index
            for index, statement in enumerate(config_tree.body)
            if isinstance(statement, ast.If)
            and "QUERY_DF_NAME in NOTEBOOK_RESERVED_INPUT_NAMES"
            in ast.unparse(statement.test)
        )

        first_statement = config_tree.body[0]
        first_statement_source = ast.unparse(first_statement)
        second_statement = config_tree.body[1]
        second_statement_source = ast.unparse(second_statement)
        self.assertIsInstance(first_statement, ast.Expr)
        self.assertIn(RUN_EPOCH_GLOBAL_KEY, first_statement_source)
        self.assertNotIn("pre_config_bindings", first_statement_source)
        self.assertEqual(
            {
                node.attr
                for node in ast.walk(first_statement)
                if isinstance(node, ast.Attribute)
            },
            {"__globals__", "__setitem__"},
        )
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__setitem__"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == RUN_EPOCH_GLOBAL_KEY
                and isinstance(node.args[1], ast.List)
                and not node.args[1].elts
                for node in ast.walk(first_statement)
            ),
            "The absolute first expression must rotate the hidden epoch",
        )
        self.assertIsInstance(second_statement, ast.Expr)
        self.assertIn("pre_config_bindings", second_statement_source)
        self.assertIn(TRUSTED_SYS_GLOBAL_KEY, second_statement_source)
        self.assertIn(
            "(lambda: None).__builtins__['__import__']('sys')",
            second_statement_source,
        )
        self.assertNotIn("__defaults__", second_statement_source)
        self.assertNotIn("__subclasses__", config_source)
        self.assertNotIn("create_module", second_statement_source)
        self.assertTrue(
            any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "__setitem__"
                and len(node.args) == 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "run_token"
                and RUN_EPOCH_GLOBAL_KEY in ast.unparse(node.args[1])
                for node in ast.walk(second_statement)
            ),
            "The registry token must mirror the hidden epoch",
        )

        prefix_notebook = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(
                    ast.unparse(
                        ast.Module(
                            body=config_tree.body[:guard_index],
                            type_ignores=[],
                        )
                    )
                )
            ]
        )
        self.assertEqual(
            notebook_top_level_binding_names(prefix_notebook),
            PRE_CONFIG_BINDING_NAMES,
        )

        first_statement_notebook = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(
                    ast.unparse(first_statement)
                )
            ]
        )
        self.assertEqual(
            notebook_top_level_binding_names(first_statement_notebook),
            set(),
            "The epoch expression must not bind a notebook global",
        )
        self.assertEqual(
            notebook_global_dependency_names(first_statement_notebook),
            set(),
            "The epoch expression must not depend on global names",
        )

        second_statement_notebook = nbformat.v4.new_notebook(
            cells=[
                nbformat.v4.new_code_cell(
                    ast.unparse(second_statement)
                )
            ]
        )
        self.assertEqual(
            notebook_top_level_binding_names(second_statement_notebook),
            set(),
            "The snapshot expression must not bind a notebook global",
        )
        self.assertEqual(
            notebook_global_dependency_names(second_statement_notebook),
            set(),
            "The snapshot expression must not depend on global names",
        )
        readiness_guard = next(
            statement
            for statement in config_tree.body
            if isinstance(statement, ast.FunctionDef)
            and statement.name == "_require_notebook_stage"
        )
        readiness_guard_source = ast.unparse(readiness_guard)
        self.assertIn(RUN_EPOCH_GLOBAL_KEY, readiness_guard_source)
        self.assertNotIn("globals()", readiness_guard_source)

    def test_config_first_statement_preserves_live_sys_streams(self) -> None:
        notebook = read_notebook()
        config_source = code_cell_by_marker(
            notebook,
            "QUERY_DF_NAME =",
        ).source
        prelude_source = ast.unparse(
            ast.Module(
                body=ast.parse(config_source).body[:2],
                type_ignores=[],
            )
        )
        probe_source = "\n".join(
            (
                "import sys",
                "_stdout_before = sys.stdout",
                "_stderr_before = sys.stderr",
                f"exec({prelude_source!r}, {{}})",
                "assert sys.stdout is _stdout_before",
                "assert sys.stderr is _stderr_before",
                "sys.stderr.write = sys.stderr.write",
            )
        )

        execution = subprocess.run(
            [sys.executable, "-c", probe_source],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            execution.returncode,
            0,
            execution.stdout + execution.stderr,
        )

    def test_config_does_not_inspect_unrelated_subclass_metadata(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        metadata_reads: list[str] = []

        class HostileMetadata(type):
            def __getattribute__(cls, name):
                if name in {"__module__", "__name__"}:
                    metadata_reads.append(name)
                    raise AssertionError(
                        f"Notebook inspected unrelated class metadata: {name}"
                    )
                return super().__getattribute__(name)

        class UnrelatedNotebookClass(metaclass=HostileMetadata):
            pass

        import_shadow = object()
        shadow_bindings = {
            "object": queries_df,
            "globals": schema_df,
            "__import__": import_shadow,
        }
        namespace["unrelated_notebook_class"] = UnrelatedNotebookClass
        namespace.update(shadow_bindings)
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        previous_run_token = runtime_state.run_token
        collision_source = self._config_source_with_input_names(
            sources["config"],
            query_name="object",
            schema_name="my_schema_df",
        )

        collision_error = self._capture_cell_error(
            collision_source,
            namespace,
        )

        self.assertIsInstance(collision_error, ValueError)
        self.assertIsNot(runtime_state.run_token, previous_run_token)
        self.assertEqual(metadata_reads, [])
        for name, binding in shadow_bindings.items():
            self.assertIs(namespace[name], binding)
        self._assert_analysis_rejected(sources, namespace)

    def test_private_epoch_invalidates_dual_builtins_helper_collision(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        previous_epoch = namespace.get(RUN_EPOCH_GLOBAL_KEY, MISSING)
        namespace["__builtins__"] = queries_df
        namespace["_raise_invalid_notebook_config"] = schema_df
        collision_source = self._config_source_with_input_names(
            sources["config"],
            query_name="__builtins__",
            schema_name="_raise_invalid_notebook_config",
        )

        collision_error = self._capture_cell_error(
            collision_source,
            namespace,
        )

        self.assertIsInstance(collision_error, ValueError)
        self.assertIs(namespace["__builtins__"], queries_df)
        self.assertIs(
            namespace["_raise_invalid_notebook_config"],
            schema_df,
        )
        self.assertIn(RUN_EPOCH_GLOBAL_KEY, namespace)
        current_epoch = namespace[RUN_EPOCH_GLOBAL_KEY]
        self.assertIsNot(current_epoch, previous_epoch)
        self.assertIs(runtime_state.run_token, current_epoch)
        self._assert_analysis_rejected(sources, namespace)
        self.assertIs(namespace["__builtins__"], queries_df)
        self.assertIs(
            namespace["_raise_invalid_notebook_config"],
            schema_df,
        )

    def test_private_epoch_does_not_trust_shadowed_helper_globals(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        previous_epoch = namespace.get(RUN_EPOCH_GLOBAL_KEY, MISSING)
        shadow_bindings = {
            "object": queries_df,
            "globals": schema_df,
            "__import__": object(),
            "_raise_invalid_notebook_config": object(),
            "_configured_notebook_input_names": object(),
            "_get_embedded_analyzer_runtime_state": object(),
            "_NOTEBOOK_RUN_TOKEN": object(),
        }
        namespace.update(shadow_bindings)
        collision_source = self._config_source_with_input_names(
            sources["config"],
            query_name="object",
            schema_name="_raise_invalid_notebook_config",
        )

        collision_error = self._capture_cell_error(
            collision_source,
            namespace,
        )

        self.assertIsInstance(collision_error, ValueError)
        current_epoch = namespace.get(RUN_EPOCH_GLOBAL_KEY, MISSING)
        self.assertIsNot(current_epoch, MISSING)
        self.assertIsNot(current_epoch, previous_epoch)
        self.assertIs(
            sys.modules[RUNTIME_STATE_MODULE_NAME].run_token,
            current_epoch,
        )
        for name, binding in shadow_bindings.items():
            self.assertIs(namespace[name], binding)
        self._assert_analysis_rejected(sources, namespace)

    def test_first_config_with_poisoned_builtins_anchors_epoch(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace: dict[str, object] = {
            "__name__": "__main__",
            "__builtins__": queries_df,
            "my_queries_df": queries_df,
            "my_schema_df": schema_df,
        }
        config_source = self._portable_sources()["config"]

        error = self._capture_cell_error(config_source, namespace)

        self.assertIsNotNone(error)
        self.assertIn("__builtins__", str(error))
        self.assertIn("Run All", str(error))
        self.assertIs(namespace["__builtins__"], queries_df)
        self.assertIs(namespace["my_queries_df"], queries_df)
        self.assertIs(namespace["my_schema_df"], schema_df)
        self.assertIn(RUN_EPOCH_GLOBAL_KEY, namespace)
        self.assertNotIn(TRUSTED_SYS_GLOBAL_KEY, namespace)
        self._assert_state_absent(namespace, READINESS_TOKEN_NAMES)

    def test_config_rejects_nonidentifier_dataframe_names(
        self,
    ) -> None:
        self.assertFalse(RUN_EPOCH_GLOBAL_KEY.isidentifier())
        self.assertFalse(TRUSTED_SYS_GLOBAL_KEY.isidentifier())
        cases = (
            ("QUERY_DF_NAME", "query-data"),
            ("SCHEMA_DF_NAME", "schema:data"),
        )

        for config_key, invalid_name in cases:
            with self.subTest(
                config_key=config_key,
                invalid_name=invalid_name,
            ):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                user_binding = (
                    queries_df
                    if config_key == "QUERY_DF_NAME"
                    else schema_df
                )
                namespace[invalid_name] = user_binding
                previous_epoch = namespace.get(
                    RUN_EPOCH_GLOBAL_KEY,
                    MISSING,
                )
                trusted_sys = namespace.get(
                    TRUSTED_SYS_GLOBAL_KEY,
                    MISSING,
                )
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=(
                        invalid_name
                        if config_key == "QUERY_DF_NAME"
                        else "my_queries_df"
                    ),
                    schema_name=(
                        invalid_name
                        if config_key == "SCHEMA_DF_NAME"
                        else "my_schema_df"
                    ),
                )

                error = self._capture_cell_error(
                    config_source,
                    namespace,
                )

                self.assertIsInstance(error, ValueError)
                self.assertIn(config_key, str(error))
                self.assertIn("valid Python identifier", str(error))
                self.assertIs(namespace[invalid_name], user_binding)
                self.assertIn(TRUSTED_SYS_GLOBAL_KEY, namespace)
                self.assertIs(
                    namespace[TRUSTED_SYS_GLOBAL_KEY],
                    trusted_sys,
                )
                current_epoch = namespace[RUN_EPOCH_GLOBAL_KEY]
                self.assertIsNot(current_epoch, previous_epoch)
                self.assertIs(
                    sys.modules[RUNTIME_STATE_MODULE_NAME].run_token,
                    current_epoch,
                )
                self._assert_analysis_rejected(sources, namespace)

    def test_config_restores_every_pre_guard_input_binding(
        self,
    ) -> None:
        for index, reserved_name in enumerate(
            sorted(PRE_CONFIG_BINDING_NAMES)
        ):
            config_key = (
                "SCHEMA_DF_NAME"
                if reserved_name == "SCHEMA_DF_NAME" or index % 2
                else "QUERY_DF_NAME"
            )
            with self.subTest(
                config_key=config_key,
                reserved_name=reserved_name,
            ):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                user_binding = (
                    queries_df
                    if config_key == "QUERY_DF_NAME"
                    else schema_df
                )
                namespace[reserved_name] = user_binding
                pandas_binding = namespace["pd"]
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                previous_runtime_token = runtime_state.run_token
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=(
                        reserved_name
                        if config_key == "QUERY_DF_NAME"
                        else "my_queries_df"
                    ),
                    schema_name=(
                        reserved_name
                        if config_key == "SCHEMA_DF_NAME"
                        else "my_schema_df"
                    ),
                )

                error = self._capture_cell_error(config_source, namespace)

                self.assertIsInstance(error, ValueError)
                self.assertIn(config_key, str(error))
                self.assertIn(repr(reserved_name), str(error))
                self.assertIs(namespace[reserved_name], user_binding)
                self.assertIs(namespace["pd"], pandas_binding)
                self.assertIsNot(
                    runtime_state.run_token,
                    previous_runtime_token,
                )
                self.assertFalse(
                    hasattr(runtime_state, "pre_config_bindings")
                )
                self._assert_analysis_rejected(sources, namespace)
                self.assertIs(namespace[reserved_name], user_binding)

    def test_config_restores_both_selected_pre_guard_bindings(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        namespace["DEFAULT_SCHEMA"] = queries_df
        namespace["OUTPUT_DIR"] = schema_df
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        config_source = self._config_source_with_input_names(
            sources["config"],
            query_name="DEFAULT_SCHEMA",
            schema_name="OUTPUT_DIR",
        )

        error = self._capture_cell_error(config_source, namespace)

        self.assertIsInstance(error, ValueError)
        self.assertIs(namespace["DEFAULT_SCHEMA"], queries_df)
        self.assertIs(namespace["OUTPUT_DIR"], schema_df)
        self.assertFalse(hasattr(runtime_state, "pre_config_bindings"))
        self._assert_analysis_rejected(sources, namespace)

    def test_config_removes_absent_pre_guard_binding_after_collision(
        self,
    ) -> None:
        sources = self._portable_sources()
        namespace: dict[str, object] = {"__name__": "__main__"}
        sys.modules.pop(RUNTIME_STATE_MODULE_NAME, None)
        config_source = self._config_source_with_input_names(
            sources["config"],
            query_name="QUERY_DF_NAME",
            schema_name=None,
        )

        error = self._capture_cell_error(config_source, namespace)

        self.assertIsInstance(error, ValueError)
        self.assertNotIn("QUERY_DF_NAME", namespace)
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        self.assertFalse(hasattr(runtime_state, "pre_config_bindings"))
        self.assertTrue(hasattr(runtime_state, "run_token"))

    def test_successful_config_clears_pre_config_snapshot(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        pandas_binding = object()
        namespace: dict[str, object] = {
            "__name__": "__main__",
            "my_queries_df": queries_df,
            "my_schema_df": schema_df,
            "pd": pandas_binding,
        }
        sources = self._portable_sources()

        error = self._capture_cell_error(sources["config"], namespace)

        self.assertIsNone(error)
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        self.assertFalse(hasattr(runtime_state, "pre_config_bindings"))
        self.assertIn(TRUSTED_SYS_GLOBAL_KEY, namespace)
        self.assertIn(RUN_EPOCH_GLOBAL_KEY, namespace)
        self.assertIs(namespace[TRUSTED_SYS_GLOBAL_KEY], sys)
        self.assertIs(
            namespace["_NOTEBOOK_RUN_TOKEN"],
            namespace[RUN_EPOCH_GLOBAL_KEY],
        )
        self.assertIs(
            runtime_state.run_token,
            namespace[RUN_EPOCH_GLOBAL_KEY],
        )
        self.assertEqual(namespace["QUERY_DF_NAME"], "my_queries_df")
        self.assertEqual(namespace["SCHEMA_DF_NAME"], "my_schema_df")
        self.assertEqual(namespace["DEFAULT_SCHEMA"], "public")
        self.assertIsNone(namespace["OUTPUT_DIR"])
        self.assertIs(namespace["BUILD_HTML"], False)
        self.assertIs(namespace["AUTO_INSTALL"], True)
        self.assertEqual(namespace["PANDAS_REQUIREMENT"], PANDAS_REQUIREMENT)
        self.assertEqual(namespace["SQLGLOT_REQUIREMENT"], SQLGLOT_REQUIREMENT)
        self.assertEqual(
            namespace["DEPENDENCY_REQUIREMENTS"],
            DEPENDENCY_REQUIREMENTS,
        )
        self.assertIs(namespace["pd"], pandas_binding)

    def test_dependency_compatibility_contract(self) -> None:
        namespace, _, _, error = self._exercise_dependency_bootstrap()
        self.assertIsNone(error)
        compatible = namespace["dependency_is_compatible"]

        for dependency, version in (
            ("pandas", "2.0.0"),
            ("pandas", "2.99.0"),
            ("pandas", "2.0.0.post1"),
            ("pandas", "2.0.0+local.1"),
            ("sqlglot", "25.34.0"),
            ("sqlglot", "25.999.0"),
            ("sqlglot", "25.34.0.post1"),
            ("sqlglot", "25.34.0+local.1"),
        ):
            with self.subTest(dependency=dependency, version=version):
                self.assertTrue(compatible(dependency, version))

        for dependency, version in (
            ("pandas", "1.5.3"),
            ("pandas", "3.0.0"),
            ("sqlglot", "25.33.9"),
            ("sqlglot", "26.0.0"),
        ):
            with self.subTest(dependency=dependency, version=version):
                self.assertFalse(compatible(dependency, version))

        for dependency, version in (
            ("pandas", "2.0.0rc1"),
            ("pandas", "2.0.0.dev1"),
            ("sqlglot", "25.34.0rc1"),
            ("sqlglot", "25.34.0.dev1"),
            ("pandas", "not-a-version"),
            ("sqlglot", "25.34.0 trailing"),
        ):
            with self.subTest(dependency=dependency, version=version):
                with self.assertRaises(ValueError):
                    compatible(dependency, version)

        with self.assertRaises(ValueError):
            compatible("unknown", "1.2.3")

    def test_dependency_bootstrap_accepts_compatible_metadata_and_modules(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap()
        )

        self.assertIsNone(error)
        self.assertEqual(
            namespace["_version_checks"],
            [("pandas", "2.3.3"), ("sqlglot", "25.34.1")],
        )
        self.assertEqual(import_attempts, ["pandas", "sqlglot"])
        self.assertEqual(install_commands, [])
        self.assertIs(namespace["pd"], namespace["_imported_modules"]["pandas"])
        self.assertIs(
            namespace["sqlglot"],
            namespace["_imported_modules"]["sqlglot"],
        )

    def test_dependency_bootstrap_uses_loaded_compatible_modules(self) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                loaded_versions={
                    "pandas": "2.3.3",
                    "sqlglot": "25.34.1",
                },
                metadata_versions={
                    "pandas": ("1.5.3",),
                    "sqlglot": ("24.0.0",),
                },
            )
        )

        self.assertIsNone(error)
        self.assertEqual(namespace["_version_checks"], [])
        self.assertEqual(import_attempts, [])
        self.assertEqual(install_commands, [])
        self.assertIs(namespace["pd"], namespace["_loaded_modules"]["pandas"])
        self.assertIs(
            namespace["sqlglot"],
            namespace["_loaded_modules"]["sqlglot"],
        )

    def test_dependency_bootstrap_installs_both_dependencies_with_current_python(
        self,
    ) -> None:
        namespace, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                metadata_versions={
                    "pandas": (
                        importlib_metadata.PackageNotFoundError("pandas"),
                    ),
                    "sqlglot": ("24.0.0",),
                },
            )
        )

        self.assertIsNone(error)
        self.assertEqual(
            install_commands,
            [
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    PANDAS_REQUIREMENT,
                    SQLGLOT_REQUIREMENT,
                ]
            ],
        )
        self.assertEqual(import_attempts, ["pandas", "sqlglot"])

    def test_dependency_bootstrap_disabled_fails_without_pip(self) -> None:
        for dependency, version in (
            ("pandas", "1.5.3"),
            ("sqlglot", "25.33.9"),
        ):
            with self.subTest(dependency=dependency):
                versions = {
                    "pandas": ("2.3.3",),
                    "sqlglot": ("25.34.1",),
                }
                versions[dependency] = (version,)
                _, import_attempts, install_commands, error = (
                    self._exercise_dependency_bootstrap(
                        auto_install=False,
                        metadata_versions=versions,
                    )
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn(dependency, message)
                self.assertIn(version, message)
                self.assertIn("AUTO_INSTALL=True", message)
                self.assertIn(sys.executable, message)
                self.assertIn(PANDAS_REQUIREMENT, message)
                self.assertIn(SQLGLOT_REQUIREMENT, message)
                self.assertNotIn(dependency, import_attempts)
                self.assertEqual(install_commands, [])

    def test_dependency_bootstrap_wraps_install_failure(self) -> None:
        install_error = subprocess.CalledProcessError(
            returncode=7,
            cmd=[sys.executable, "-m", "pip", "install"],
        )
        _, _, install_commands, error = self._exercise_dependency_bootstrap(
            metadata_versions={
                "pandas": ("1.5.3",),
                "sqlglot": ("25.34.1",),
            },
            install_error=install_error,
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIs(error.__cause__, install_error)
        self.assertIn("Could not install", str(error))
        self.assertIn(sys.executable, str(error))
        self.assertEqual(len(install_commands), 1)

    def test_dependency_bootstrap_installs_then_retries_broken_import(self) -> None:
        _, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                import_failures={"sqlglot": 1},
            )
        )

        self.assertIsNone(error)
        self.assertEqual(
            import_attempts,
            ["pandas", "sqlglot", "pandas", "sqlglot"],
        )
        self.assertEqual(len(install_commands), 1)

    def test_dependency_bootstrap_wraps_retry_import_failure(self) -> None:
        _, import_attempts, install_commands, error = (
            self._exercise_dependency_bootstrap(
                import_failures={"pandas": 2},
            )
        )

        self.assertIsInstance(error, RuntimeError)
        self.assertIsInstance(error.__cause__, ImportError)
        self.assertIn("after installation", str(error))
        self.assertEqual(import_attempts, ["pandas", "sqlglot", "pandas"])
        self.assertEqual(len(install_commands), 1)

    def test_dependency_bootstrap_requires_restart_for_loaded_bad_version(
        self,
    ) -> None:
        for dependency, version in (
            ("pandas", "1.5.3"),
            ("sqlglot", "25.33.9"),
            ("pandas", MISSING),
            ("sqlglot", MISSING),
        ):
            with self.subTest(dependency=dependency, version=version):
                loaded = {
                    "pandas": "2.3.3",
                    "sqlglot": "25.34.1",
                }
                loaded[dependency] = version
                _, _, install_commands, error = self._exercise_dependency_bootstrap(
                    loaded_versions=loaded,
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn(dependency, message)
                self.assertIn("restart", message.lower())
                self.assertIn("DataFrame", message)
                self.assertEqual(install_commands, [])

    def test_dependency_bootstrap_requires_restart_for_fresh_bad_module(
        self,
    ) -> None:
        for dependency, version in (
            ("pandas", "1.5.3"),
            ("sqlglot", "25.33.9"),
        ):
            with self.subTest(dependency=dependency):
                imported = {
                    "pandas": "2.3.3",
                    "sqlglot": "25.34.1",
                }
                imported[dependency] = version
                _, _, install_commands, error = self._exercise_dependency_bootstrap(
                    imported_versions=imported,
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn(dependency, message)
                self.assertIn(version, message)
                self.assertIn("restart", message.lower())
                self.assertIn("DataFrame", message)
                self.assertEqual(install_commands, [])

    def test_dependency_bootstrap_rejects_incompatible_module_after_install(
        self,
    ) -> None:
        for dependency, version in (
            ("pandas", "3.0.0"),
            ("sqlglot", "26.0.0"),
        ):
            with self.subTest(dependency=dependency):
                imported = {
                    "pandas": "2.3.3",
                    "sqlglot": "25.34.1",
                }
                imported[dependency] = version
                metadata_versions = {
                    "pandas": ("2.3.3",),
                    "sqlglot": ("25.34.1",),
                }
                metadata_versions[dependency] = (
                    importlib_metadata.PackageNotFoundError(dependency),
                )
                _, _, install_commands, error = self._exercise_dependency_bootstrap(
                    imported_versions=imported,
                    metadata_versions=metadata_versions,
                )

                self.assertIsInstance(error, RuntimeError)
                message = str(error)
                self.assertIn(dependency, message)
                self.assertIn(version, message)
                self.assertIn("after installation", message.lower())
                self.assertEqual(len(install_commands), 1)

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

    def test_config_rejects_reserved_input_names_without_mutating_bindings(
        self,
    ) -> None:
        cases = (
            ("QUERY_DF_NAME", "result", "result", "my_schema_df"),
            ("QUERY_DF_NAME", "details_df", "details_df", "my_schema_df"),
            ("SCHEMA_DF_NAME", "schema_df", "my_queries_df", "schema_df"),
        )

        for config_key, reserved_name, query_name, schema_name in cases:
            with self.subTest(config_key=config_key, reserved_name=reserved_name):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                user_binding = object()
                namespace[reserved_name] = user_binding
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                previous_run_token = runtime_state.run_token
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=query_name,
                    schema_name=schema_name,
                )

                error = self._capture_cell_error(config_source, namespace)

                self.assertIsInstance(error, ValueError)
                self.assertIn(config_key, str(error))
                self.assertIn(repr(reserved_name), str(error))
                self.assertIs(namespace[reserved_name], user_binding)
                self.assertIsNot(
                    runtime_state.run_token,
                    previous_run_token,
                )
                self._assert_analysis_rejected(sources, namespace)
                self.assertIs(namespace[reserved_name], user_binding)
                self.assertIs(namespace["my_queries_df"], queries_df)
                self.assertIs(namespace["my_schema_df"], schema_df)

    def test_config_guard_precedes_representative_top_level_writes(
        self,
    ) -> None:
        cases = (
            ("QUERY_DF_NAME", "sys"),
            ("SCHEMA_DF_NAME", "Path"),
            ("QUERY_DF_NAME", "EMBEDDED_ANALYZER_MANIFEST"),
            ("SCHEMA_DF_NAME", "artifact_name"),
            ("QUERY_DF_NAME", "_cleanup_embedded_analyzer_runtime"),
            ("SCHEMA_DF_NAME", "result"),
            ("QUERY_DF_NAME", "_EMBEDDED_ANALYZER_ZIP_PATH"),
            ("SCHEMA_DF_NAME", "_EMBEDDED_ANALYZER_TEMP_DIR"),
        )

        for config_key, reserved_name in cases:
            with self.subTest(config_key=config_key, reserved_name=reserved_name):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                user_binding = (
                    queries_df
                    if config_key == "QUERY_DF_NAME"
                    else schema_df
                )
                namespace[reserved_name] = user_binding
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                previous_runtime_token = getattr(
                    runtime_state,
                    "run_token",
                    namespace["_NOTEBOOK_RUN_TOKEN"],
                )
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=(
                        reserved_name
                        if config_key == "QUERY_DF_NAME"
                        else "my_queries_df"
                    ),
                    schema_name=(
                        reserved_name
                        if config_key == "SCHEMA_DF_NAME"
                        else "my_schema_df"
                    ),
                )

                error = self._capture_cell_error(config_source, namespace)

                self.assertIsInstance(error, ValueError)
                self.assertIn(config_key, str(error))
                self.assertIn(repr(reserved_name), str(error))
                self.assertIs(namespace[reserved_name], user_binding)
                self.assertIsNot(
                    getattr(runtime_state, "run_token", previous_runtime_token),
                    previous_runtime_token,
                )
                self._assert_analysis_rejected(sources, namespace)
                self.assertIs(namespace[reserved_name], user_binding)

    def test_config_rejects_shadowed_global_dependencies_before_using_them(
        self,
    ) -> None:
        reserved_names = (
            "object",
            "globals",
            "len",
            "isinstance",
            "__import__",
            "__builtins__",
            "zip",
        )

        for index, reserved_name in enumerate(reserved_names):
            with self.subTest(reserved_name=reserved_name):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                user_binding = queries_df if index % 2 == 0 else schema_df
                config_key = (
                    "QUERY_DF_NAME"
                    if index % 2 == 0
                    else "SCHEMA_DF_NAME"
                )
                namespace[reserved_name] = user_binding
                previous_runtime_state = sys.modules[
                    RUNTIME_STATE_MODULE_NAME
                ]
                previous_run_token = previous_runtime_state.run_token
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=(
                        reserved_name
                        if config_key == "QUERY_DF_NAME"
                        else "my_queries_df"
                    ),
                    schema_name=(
                        reserved_name
                        if config_key == "SCHEMA_DF_NAME"
                        else "my_schema_df"
                    ),
                )

                error = self._capture_cell_error(config_source, namespace)

                self.assertIsInstance(error, ValueError)
                self.assertIn(config_key, str(error))
                self.assertIn(repr(reserved_name), str(error))
                self.assertIs(namespace[reserved_name], user_binding)
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                self.assertIs(runtime_state, previous_runtime_state)
                self.assertIsNot(
                    runtime_state.run_token,
                    previous_run_token,
                )
                self._assert_analysis_rejected(sources, namespace)
                self.assertIs(namespace[reserved_name], user_binding)

    def test_config_rejects_equal_input_names_without_mutating_binding(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        previous_run_token = runtime_state.run_token
        config_source = self._config_source_with_input_names(
            sources["config"],
            query_name="my_queries_df",
            schema_name="my_queries_df",
        )

        error = self._capture_cell_error(config_source, namespace)

        self.assertIsInstance(error, ValueError)
        self.assertIn("QUERY_DF_NAME", str(error))
        self.assertIn("SCHEMA_DF_NAME", str(error))
        self.assertIn("'my_queries_df'", str(error))
        self.assertIs(namespace["my_queries_df"], queries_df)
        self.assertIs(namespace["my_schema_df"], schema_df)
        self.assertIsNot(runtime_state.run_token, previous_run_token)
        self._assert_analysis_rejected(sources, namespace)
        self.assertIs(namespace["my_queries_df"], queries_df)

    def test_config_validation_failure_cleans_embedded_runtime(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        temporary_directory, embedded_path, cleanup_mock = (
            self._capture_embedded_runtime(namespace)
        )
        user_binding = object()
        namespace["result"] = user_binding
        unrelated_name = "_embedded_gp_sql_analyzer_unrelated"
        unrelated_module = ModuleType(unrelated_name)
        public_module = ModuleType("gp_sql_analyzer")
        config_source = self._config_source_with_input_names(
            sources["config"],
            query_name="result",
            schema_name="my_schema_df",
        )

        with patch.dict(
            sys.modules,
            {
                unrelated_name: unrelated_module,
                "gp_sql_analyzer": public_module,
            },
        ):
            error = self._capture_cell_error(config_source, namespace)

            self.assertIsInstance(error, ValueError)
            self._assert_embedded_runtime_cleaned(
                namespace,
                temporary_directory=temporary_directory,
                embedded_path=embedded_path,
                cleanup_mock=cleanup_mock,
            )
            self.assertIs(sys.modules[unrelated_name], unrelated_module)
            self.assertIs(sys.modules["gp_sql_analyzer"], public_module)

        namespace["_cleanup_embedded_analyzer_runtime"]()
        namespace["_cleanup_embedded_analyzer_runtime"]()
        self.assertIs(namespace["result"], user_binding)
        self._assert_analysis_rejected(sources, namespace)
        self.assertIs(namespace["result"], user_binding)

    def test_config_collision_cleanup_error_restores_snapshot_and_invalidates_run(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        self.addCleanup(sys.modules.pop, RUNTIME_STATE_MODULE_NAME, None)
        sources = self._portable_sources()
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        previous_run_token = runtime_state.run_token
        temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
        cleanup_failure = OSError("simulated cleanup failure")
        cleanup_mock = Mock(side_effect=cleanup_failure)
        temporary_directory.cleanup = cleanup_mock
        namespace["DEFAULT_SCHEMA"] = queries_df
        namespace.pop("OUTPUT_DIR")
        config_source = self._config_source_with_input_names(
            sources["config"],
            query_name="DEFAULT_SCHEMA",
            schema_name="OUTPUT_DIR",
        )

        error = self._capture_cell_error(config_source, namespace)

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("reserved", str(error).lower())
        self.assertIn("cleanup", str(error).lower())
        self.assertIs(error.__cause__, cleanup_failure)
        cleanup_mock.assert_called_once_with()
        self.assertIs(namespace["DEFAULT_SCHEMA"], queries_df)
        self.assertNotIn("OUTPUT_DIR", namespace)
        self.assertFalse(hasattr(runtime_state, "pre_config_bindings"))
        self.assertIsNot(runtime_state.run_token, previous_run_token)
        self._assert_analysis_rejected(sources, namespace)
        self._assert_user_state_preserved(
            namespace,
            queries_df=queries_df,
            schema_df=schema_df,
            pandas_binding=pd,
            sqlglot_binding=namespace["sqlglot"],
        )

    def test_cleanup_error_still_clears_owned_runtime_state(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        self.addCleanup(sys.modules.pop, RUNTIME_STATE_MODULE_NAME, None)
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        embedded_path = str(runtime_state.archive_path)
        cleanup_failure = OSError("simulated cleanup failure")
        temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
        sys.path.insert(0, embedded_path)
        self.addCleanup(remove_sys_path_entry, embedded_path)
        private_name = "_embedded_gp_sql_analyzer.cleanup_failure_probe"
        private_module = ModuleType(private_name)
        late_private_name = "_embedded_gp_sql_analyzer.late_cleanup_probe"
        late_private_module = ModuleType(late_private_name)
        unrelated_name = "_embedded_gp_sql_analyzer_unrelated_cleanup_probe"
        unrelated_module = ModuleType(unrelated_name)

        def fail_cleanup() -> None:
            sys.modules[late_private_name] = late_private_module
            raise cleanup_failure

        cleanup_mock = Mock(side_effect=fail_cleanup)
        temporary_directory.cleanup = cleanup_mock

        with (
            patch.dict(
                sys.modules,
                {
                    private_name: private_module,
                    unrelated_name: unrelated_module,
                },
            ),
            patch.object(importlib, "invalidate_caches") as invalidate_caches,
        ):
            error = self._capture_cell_error(
                "_cleanup_embedded_analyzer_runtime()",
                namespace,
            )

            self.assertIsInstance(error, RuntimeError)
            self.assertIn("cleanup", str(error).lower())
            self.assertIs(error.__cause__, cleanup_failure)
            cleanup_mock.assert_called_once_with()
            self.assertIsNone(runtime_state.archive_path)
            self.assertIsNone(runtime_state.temp_handle)
            self.assertNotIn(embedded_path, sys.path)
            self.assertNotIn("_EMBEDDED_ANALYZER_TEMP_DIR", namespace)
            self.assertNotIn("_EMBEDDED_ANALYZER_ZIP_PATH", namespace)
            self.assertNotIn(private_name, sys.modules)
            self.assertNotIn(late_private_name, sys.modules)
            self.assertIs(sys.modules[unrelated_name], unrelated_module)
            invalidate_caches.assert_called()

    def test_stage_cleanup_errors_invalidate_readiness_and_block_downstream(
        self,
    ) -> None:
        cases = (
            (
                "config",
                ("_DEPENDENCIES_READY_TOKEN",)
                + ANALYZER_AND_DOWNSTREAM_STATE_NAMES,
            ),
            (
                "bootstrap",
                ("_DEPENDENCIES_READY_TOKEN",)
                + ANALYZER_AND_DOWNSTREAM_STATE_NAMES,
            ),
            ("loader", ANALYZER_AND_DOWNSTREAM_STATE_NAMES),
        )

        for stage, invalidated_names in cases:
            with self.subTest(stage=stage):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                self.addCleanup(
                    sys.modules.pop,
                    RUNTIME_STATE_MODULE_NAME,
                    None,
                )
                sources = self._portable_sources()
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                previous_run_token = runtime_state.run_token
                temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
                embedded_path = str(runtime_state.archive_path)
                cleanup_failure = OSError(f"{stage} cleanup failure")
                cleanup_mock = Mock(side_effect=cleanup_failure)
                temporary_directory.cleanup = cleanup_mock
                pandas_binding = namespace["pd"]
                sqlglot_binding = namespace["sqlglot"]

                error = self._capture_cell_error(sources[stage], namespace)

                self.assertIsInstance(error, RuntimeError)
                self.assertIn("cleanup", str(error).lower())
                self.assertIs(error.__cause__, cleanup_failure)
                cleanup_mock.assert_called_once_with()
                self._assert_state_absent(namespace, invalidated_names)
                self._assert_analysis_rejected(sources, namespace)
                self._assert_user_state_preserved(
                    namespace,
                    queries_df=queries_df,
                    schema_df=schema_df,
                    pandas_binding=pandas_binding,
                    sqlglot_binding=sqlglot_binding,
                )
                self.assertIsNone(runtime_state.archive_path)
                self.assertIsNone(runtime_state.temp_handle)
                self.assertNotIn(embedded_path, sys.path)
                if stage == "config":
                    self.assertIsNot(
                        runtime_state.run_token,
                        previous_run_token,
                    )

    def test_cleanup_control_flow_errors_clear_state_and_propagate(self) -> None:
        for cleanup_failure in (
            KeyboardInterrupt("simulated interrupt"),
            SystemExit("simulated exit"),
        ):
            with self.subTest(error_type=type(cleanup_failure).__name__):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                self.addCleanup(
                    sys.modules.pop,
                    RUNTIME_STATE_MODULE_NAME,
                    None,
                )
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                embedded_path = str(runtime_state.archive_path)
                temporary_directory = namespace[
                    "_EMBEDDED_ANALYZER_TEMP_DIR"
                ]
                cleanup_mock = Mock(side_effect=cleanup_failure)
                temporary_directory.cleanup = cleanup_mock

                error = self._capture_cell_error(
                    "_cleanup_embedded_analyzer_runtime()",
                    namespace,
                )

                self.assertIs(error, cleanup_failure)
                cleanup_mock.assert_called_once_with()
                self.assertIsNone(runtime_state.archive_path)
                self.assertIsNone(runtime_state.temp_handle)
                self.assertNotIn(embedded_path, sys.path)
                self.assertNotIn("_EMBEDDED_ANALYZER_TEMP_DIR", namespace)
                self.assertNotIn("_EMBEDDED_ANALYZER_ZIP_PATH", namespace)

    def test_config_replaces_untrusted_exact_registry_without_touching_resources(
        self,
    ) -> None:
        for marker in (
            MISSING,
            "wrong-notebook-owner",
            RUNTIME_STATE_OWNER_MARKER,
        ):
            with self.subTest(marker=marker):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                foreign_directory = tempfile.TemporaryDirectory(
                    prefix="foreign-notebook-registry-"
                )
                self.addCleanup(foreign_directory.cleanup)
                foreign_archive = (
                    Path(foreign_directory.name) / "foreign-runtime.zip"
                )
                foreign_archive.write_bytes(b"foreign registry resource")
                foreign_path = str(foreign_archive)
                sys.path.insert(0, foreign_path)
                self.addCleanup(remove_sys_path_entry, foreign_path)
                cleanup_counter = Mock()
                foreign_state = ModuleType(RUNTIME_STATE_MODULE_NAME)
                foreign_state.archive_path = foreign_path
                foreign_state.temp_handle = cleanup_counter
                foreign_state.run_token = object()
                if marker is not MISSING:
                    foreign_state.owner_marker = marker
                    foreign_state.owner_token = object()
                sys.modules[RUNTIME_STATE_MODULE_NAME] = foreign_state
                self.addCleanup(
                    sys.modules.pop,
                    RUNTIME_STATE_MODULE_NAME,
                    None,
                )

                error = self._capture_cell_error(
                    sources["config"],
                    namespace,
                )

                self.assertIsNone(error)
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                self.assertIsNot(runtime_state, foreign_state)
                self.assertIs(type(runtime_state), ModuleType)
                self.assertEqual(
                    runtime_state.owner_marker,
                    RUNTIME_STATE_OWNER_MARKER,
                )
                self.assertIs(
                    runtime_state.owner_token,
                    builtins.__dict__[RUNTIME_STATE_OWNER_TOKEN_KEY],
                )
                cleanup_counter.cleanup.assert_not_called()
                self.assertIn(foreign_path, sys.path)
                self.assertEqual(
                    foreign_archive.read_bytes(),
                    b"foreign registry resource",
                )
                self.assertIs(foreign_state.temp_handle, cleanup_counter)
                self.assertEqual(foreign_state.archive_path, foreign_path)
                self._assert_user_state_preserved(
                    namespace,
                    queries_df=queries_df,
                    schema_df=schema_df,
                    pandas_binding=namespace["pd"],
                    sqlglot_binding=namespace["sqlglot"],
                )

    def test_standalone_loader_replaces_untrusted_registry(self) -> None:
        sources = self._portable_sources()
        namespace: dict[str, object] = {"__name__": "__main__"}
        exec(compile(sources["payload"], str(NOTEBOOK), "exec"), namespace)
        foreign_directory = tempfile.TemporaryDirectory(
            prefix="foreign-standalone-loader-"
        )
        self.addCleanup(foreign_directory.cleanup)
        foreign_archive = Path(foreign_directory.name) / "foreign.zip"
        foreign_archive.write_bytes(b"foreign standalone resource")
        foreign_path = str(foreign_archive)
        sys.path.insert(0, foreign_path)
        self.addCleanup(remove_sys_path_entry, foreign_path)
        cleanup_counter = Mock()
        foreign_state = ModuleType(RUNTIME_STATE_MODULE_NAME)
        foreign_state.owner_marker = RUNTIME_STATE_OWNER_MARKER
        foreign_state.owner_token = object()
        foreign_state.archive_path = foreign_path
        foreign_state.temp_handle = cleanup_counter
        sys.modules[RUNTIME_STATE_MODULE_NAME] = foreign_state
        self.addCleanup(
            sys.modules.pop,
            RUNTIME_STATE_MODULE_NAME,
            None,
        )

        error = self._capture_cell_error(sources["loader"], namespace)

        self.assertIsNone(error)
        runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
        self.assertIsNot(runtime_state, foreign_state)
        self.assertEqual(
            runtime_state.owner_marker,
            RUNTIME_STATE_OWNER_MARKER,
        )
        self.assertIs(
            runtime_state.owner_token,
            builtins.__dict__[RUNTIME_STATE_OWNER_TOKEN_KEY],
        )
        cleanup_counter.cleanup.assert_not_called()
        self.assertIn(foreign_path, sys.path)
        self.assertEqual(
            foreign_archive.read_bytes(),
            b"foreign standalone resource",
        )
        owned_handle = runtime_state.temp_handle
        owned_path = runtime_state.archive_path
        self.addCleanup(owned_handle.cleanup)
        self.addCleanup(remove_sys_path_entry, owned_path)

    def test_cleanup_preserves_mismatched_registry_resources(self) -> None:
        cases = ("arbitrary_handle", "mismatched_real_handle")

        for case in cases:
            with self.subTest(case=case):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]

                if case == "arbitrary_handle":
                    resource_directory = tempfile.TemporaryDirectory(
                        prefix="foreign-registry-resource-"
                    )
                    self.addCleanup(resource_directory.cleanup)
                    resource_path = str(
                        Path(resource_directory.name) / "arbitrary.zip"
                    )
                    Path(resource_path).write_bytes(b"arbitrary resource")
                    temp_handle = Mock()
                else:
                    temp_handle = tempfile.TemporaryDirectory(
                        prefix="mismatched-owned-runtime-"
                    )
                    original_cleanup = temp_handle.cleanup
                    self.addCleanup(original_cleanup)
                    resource_path = str(
                        Path(temp_handle.name) / "wrong-runtime-name.zip"
                    )
                    Path(resource_path).write_bytes(b"mismatched resource")
                    temp_handle.cleanup = Mock()

                sys.path.insert(0, resource_path)
                self.addCleanup(remove_sys_path_entry, resource_path)
                runtime_state.archive_path = resource_path
                runtime_state.temp_handle = temp_handle
                namespace["_EMBEDDED_ANALYZER_ZIP_PATH"] = resource_path
                namespace["_EMBEDDED_ANALYZER_TEMP_DIR"] = temp_handle

                error = self._capture_cell_error(
                    "_cleanup_embedded_analyzer_runtime()",
                    namespace,
                )

                self.assertIsNone(error)
                temp_handle.cleanup.assert_not_called()
                self.assertIsNone(runtime_state.archive_path)
                self.assertIsNone(runtime_state.temp_handle)
                self.assertIn(resource_path, sys.path)
                self.assertTrue(Path(resource_path).is_file())
                self.assertIs(
                    namespace["_EMBEDDED_ANALYZER_TEMP_DIR"],
                    temp_handle,
                )
                self.assertEqual(
                    namespace["_EMBEDDED_ANALYZER_ZIP_PATH"],
                    resource_path,
                )

    def test_runtime_handle_name_collisions_preserve_user_dataframes_and_cleanup(
        self,
    ) -> None:
        cases = (
            (
                "QUERY_DF_NAME",
                "_EMBEDDED_ANALYZER_ZIP_PATH",
                "_EMBEDDED_ANALYZER_ZIP_PATH",
                "my_schema_df",
            ),
            (
                "QUERY_DF_NAME",
                "_EMBEDDED_ANALYZER_TEMP_DIR",
                "_EMBEDDED_ANALYZER_TEMP_DIR",
                "my_schema_df",
            ),
            (
                "SCHEMA_DF_NAME",
                "_EMBEDDED_ANALYZER_ZIP_PATH",
                "my_queries_df",
                "_EMBEDDED_ANALYZER_ZIP_PATH",
            ),
            (
                "SCHEMA_DF_NAME",
                "_EMBEDDED_ANALYZER_TEMP_DIR",
                "my_queries_df",
                "_EMBEDDED_ANALYZER_TEMP_DIR",
            ),
        )

        for config_key, reserved_name, query_name, schema_name in cases:
            with self.subTest(config_key=config_key, reserved_name=reserved_name):
                queries_df, schema_df = fixture_dataframes()
                namespace = self._execute_portable_cells(
                    queries=queries_df,
                    schema=schema_df,
                )
                sources = self._portable_sources()
                temporary_directory, embedded_path, cleanup_mock = (
                    self._capture_embedded_runtime(namespace)
                )
                private_origins = [
                    origin
                    for module_name, module in sys.modules.items()
                    if (
                        module_name == "_embedded_gp_sql_analyzer"
                        or module_name.startswith("_embedded_gp_sql_analyzer.")
                    )
                    for origin in (
                        getattr(module, "__file__", None),
                        getattr(getattr(module, "__spec__", None), "origin", None),
                    )
                    if isinstance(origin, str)
                ]
                self.assertTrue(
                    any(embedded_path in origin for origin in private_origins)
                )

                user_binding = (
                    queries_df
                    if config_key == "QUERY_DF_NAME"
                    else schema_df
                )
                namespace[reserved_name] = user_binding
                runtime_state = sys.modules[RUNTIME_STATE_MODULE_NAME]
                previous_run_token = runtime_state.run_token
                sys.path.insert(0, embedded_path)
                self.addCleanup(remove_sys_path_entry, embedded_path)

                unrelated_directory = tempfile.TemporaryDirectory(
                    prefix="notebook-unrelated-"
                )
                self.addCleanup(unrelated_directory.cleanup)
                unrelated_paths = (
                    str(
                        Path(unrelated_directory.name)
                        / "ordinary-parent"
                        / "embedded-analyzer.zip"
                    ),
                    str(
                        Path(unrelated_directory.name)
                        / "embedded-sql-analyzer-lookalike"
                        / "other.zip"
                    ),
                )
                for unrelated_path in unrelated_paths:
                    sys.path.insert(0, unrelated_path)
                    self.addCleanup(remove_sys_path_entry, unrelated_path)

                unrelated_name = "_embedded_gp_sql_analyzer_unrelated"
                unrelated_module = ModuleType(unrelated_name)
                public_module = ModuleType("gp_sql_analyzer")
                config_source = self._config_source_with_input_names(
                    sources["config"],
                    query_name=query_name,
                    schema_name=schema_name,
                )

                with patch.dict(
                    sys.modules,
                    {
                        unrelated_name: unrelated_module,
                        "gp_sql_analyzer": public_module,
                    },
                ):
                    error = self._capture_cell_error(config_source, namespace)

                    self.assertIsInstance(error, ValueError)
                    self.assertIn(config_key, str(error))
                    self.assertIn(repr(reserved_name), str(error))
                    self.assertIs(namespace[reserved_name], user_binding)
                    self.assertNotIn(embedded_path, sys.path)
                    self.assertFalse(
                        Path(str(temporary_directory.name)).exists()
                    )
                    self.assertFalse(
                        any(
                            module_name == "_embedded_gp_sql_analyzer"
                            or module_name.startswith(
                                "_embedded_gp_sql_analyzer."
                            )
                            for module_name in sys.modules
                        )
                    )
                    self.assertIs(
                        sys.modules[unrelated_name],
                        unrelated_module,
                    )
                    self.assertIs(
                        sys.modules["gp_sql_analyzer"],
                        public_module,
                    )
                    for unrelated_path in unrelated_paths:
                        self.assertIn(unrelated_path, sys.path)

                if reserved_name == "_EMBEDDED_ANALYZER_ZIP_PATH":
                    cleanup_mock.assert_called_once_with()
                    self.assertNotIn(
                        "_EMBEDDED_ANALYZER_TEMP_DIR",
                        namespace,
                    )
                else:
                    cleanup_mock.assert_called_once_with()
                    self.assertNotIn(
                        "_EMBEDDED_ANALYZER_ZIP_PATH",
                        namespace,
                    )
                self.assertIsNot(
                    runtime_state.run_token,
                    previous_run_token,
                )
                self._assert_analysis_rejected(sources, namespace)
                self.assertIs(namespace[reserved_name], user_binding)
                self.assertIs(namespace["my_queries_df"], queries_df)
                self.assertIs(namespace["my_schema_df"], schema_df)

                corrected_config_error = self._capture_cell_error(
                    sources["config"],
                    namespace,
                )
                self.assertIsNone(corrected_config_error)
                self.assertIs(namespace.get(reserved_name), user_binding)

                for stage in (
                    "payload",
                    "bootstrap",
                    "loader",
                    "resolver",
                    "analysis",
                ):
                    stage_error = self._capture_cell_error(
                        sources[stage],
                        namespace,
                    )
                    self.assertIsNone(stage_error, f"{stage}: {stage_error}")

                runtime_state = sys.modules.get(RUNTIME_STATE_MODULE_NAME)
                self.assertIsInstance(runtime_state, ModuleType)
                recovered_temporary_directory = runtime_state.temp_handle
                recovered_embedded_path = runtime_state.archive_path
                self.addCleanup(recovered_temporary_directory.cleanup)
                self.addCleanup(
                    remove_sys_path_entry,
                    recovered_embedded_path,
                )
                self.assertIs(namespace.get(reserved_name), user_binding)
                self.assertIn(recovered_embedded_path, sys.path)
                self.assertTrue(
                    Path(recovered_temporary_directory.name).is_dir()
                )
                self.assertEqual(
                    len(namespace["row_analysis_df"]),
                    len(queries_df),
                )

    def test_cleanup_preserves_unowned_exact_lookalike_runtime_path(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        runtime_state = sys.modules.get(RUNTIME_STATE_MODULE_NAME)

        lookalike_directory = tempfile.TemporaryDirectory(
            prefix="embedded-sql-analyzer-"
        )
        self.addCleanup(lookalike_directory.cleanup)
        lookalike_archive = (
            Path(lookalike_directory.name) / "embedded-analyzer.zip"
        )
        lookalike_archive.write_bytes(b"not the notebook runtime")
        lookalike_path = str(lookalike_archive)
        sys.path.insert(0, lookalike_path)
        self.addCleanup(remove_sys_path_entry, lookalike_path)

        error = self._capture_cell_error(sources["config"], namespace)

        self.assertIsNone(error)
        self.assertIn(lookalike_path, sys.path)
        self.assertTrue(lookalike_archive.is_file())
        self.assertTrue(Path(lookalike_directory.name).is_dir())
        self.assertIs(
            sys.modules.get(RUNTIME_STATE_MODULE_NAME),
            runtime_state,
        )

    def test_cleanup_preserves_spoofed_private_module_origin_resources(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        owned_temporary_directory, owned_path, owned_cleanup_mock = (
            self._capture_embedded_runtime(namespace)
        )

        foreign_directory = tempfile.TemporaryDirectory(
            prefix="embedded-sql-analyzer-"
        )
        self.addCleanup(foreign_directory.cleanup)
        foreign_archive = (
            Path(foreign_directory.name) / "embedded-analyzer.zip"
        )
        foreign_archive.write_bytes(b"foreign archive sentinel")
        foreign_sentinel = Path(foreign_directory.name) / "keep-me"
        foreign_sentinel.write_text("foreign sentinel", encoding="utf-8")
        foreign_path = str(foreign_archive)
        sys.path.insert(0, foreign_path)
        self.addCleanup(remove_sys_path_entry, foreign_path)

        foreign_module = ModuleType("_embedded_gp_sql_analyzer")
        foreign_module.__file__ = (
            f"{foreign_path}/_embedded_gp_sql_analyzer/__init__.py"
        )
        foreign_module.__spec__ = importlib.util.spec_from_loader(
            "_embedded_gp_sql_analyzer",
            loader=None,
            origin=foreign_module.__file__,
        )
        sys.modules["_embedded_gp_sql_analyzer"] = foreign_module

        error = self._capture_cell_error(sources["config"], namespace)

        self.assertIsNone(error)
        owned_cleanup_mock.assert_called_once_with()
        self.assertFalse(Path(owned_temporary_directory.name).exists())
        self.assertNotIn(owned_path, sys.path)
        self.assertNotIn("_embedded_gp_sql_analyzer", sys.modules)
        self.assertIn(foreign_path, sys.path)
        self.assertEqual(
            foreign_archive.read_bytes(),
            b"foreign archive sentinel",
        )
        self.assertEqual(
            foreign_sentinel.read_text(encoding="utf-8"),
            "foreign sentinel",
        )
        self.assertTrue(Path(foreign_directory.name).is_dir())

    def test_cleanup_upgrades_prior_runtime_from_global_temp_handle(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        temporary_directory, embedded_path, cleanup_mock = (
            self._capture_embedded_runtime(namespace)
        )
        sys.modules.pop(RUNTIME_STATE_MODULE_NAME, None)

        error = self._capture_cell_error(sources["config"], namespace)

        self.assertIsNone(error)
        self.assertFalse(Path(temporary_directory.name).exists())
        cleanup_mock.assert_called_once_with()
        self.assertNotIn(embedded_path, sys.path)
        self.assertNotIn("_EMBEDDED_ANALYZER_ZIP_PATH", namespace)
        self.assertNotIn("_EMBEDDED_ANALYZER_TEMP_DIR", namespace)
        self.assertFalse(
            any(
                module_name == "_embedded_gp_sql_analyzer"
                or module_name.startswith("_embedded_gp_sql_analyzer.")
                for module_name in sys.modules
            )
        )

    def test_bootstrap_failure_invalidates_all_downstream_state(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        pandas_binding = namespace["pd"]
        sqlglot_binding = namespace["sqlglot"]
        temporary_directory, embedded_path, cleanup_mock = (
            self._capture_embedded_runtime(namespace)
        )
        incompatible_pandas = ModuleType("pandas")
        incompatible_pandas.__version__ = "1.5.3"
        unrelated_name = "_embedded_gp_sql_analyzer_unrelated"
        unrelated_module = ModuleType(unrelated_name)
        public_module = ModuleType("gp_sql_analyzer")

        with patch.dict(
            sys.modules,
            {
                "pandas": incompatible_pandas,
                unrelated_name: unrelated_module,
                "gp_sql_analyzer": public_module,
            },
        ):
            error = self._capture_cell_error(
                sources["bootstrap"],
                namespace,
            )
            self._assert_embedded_runtime_cleaned(
                namespace,
                temporary_directory=temporary_directory,
                embedded_path=embedded_path,
                cleanup_mock=cleanup_mock,
            )
            self.assertIs(sys.modules[unrelated_name], unrelated_module)
            self.assertIs(sys.modules["gp_sql_analyzer"], public_module)

        self.assertIsInstance(error, RuntimeError)
        self._assert_state_absent(
            namespace,
            (
                "_DEPENDENCIES_READY_TOKEN",
                *ANALYZER_AND_DOWNSTREAM_STATE_NAMES,
            ),
        )
        self._assert_analysis_rejected(sources, namespace)
        self._assert_user_state_preserved(
            namespace,
            queries_df=queries_df,
            schema_df=schema_df,
            pandas_binding=pandas_binding,
            sqlglot_binding=sqlglot_binding,
        )

    def test_corrupt_payload_invalidates_analyzer_and_downstream_state(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        pandas_binding = namespace["pd"]
        sqlglot_binding = namespace["sqlglot"]
        temporary_directory, embedded_path, cleanup_mock = (
            self._capture_embedded_runtime(namespace)
        )
        encoded = str(namespace["EMBEDDED_ANALYZER_ZIP_B64"])
        replacement = "A" if encoded[0] != "A" else "B"
        namespace["EMBEDDED_ANALYZER_ZIP_B64"] = replacement + encoded[1:]
        unrelated_name = "_embedded_gp_sql_analyzer_unrelated"
        unrelated_module = ModuleType(unrelated_name)
        public_module = ModuleType("gp_sql_analyzer")

        with patch.dict(
            sys.modules,
            {
                unrelated_name: unrelated_module,
                "gp_sql_analyzer": public_module,
            },
        ):
            error = self._capture_cell_error(sources["loader"], namespace)
            self._assert_embedded_runtime_cleaned(
                namespace,
                temporary_directory=temporary_directory,
                embedded_path=embedded_path,
                cleanup_mock=cleanup_mock,
            )
            self.assertIs(sys.modules[unrelated_name], unrelated_module)
            self.assertIs(sys.modules["gp_sql_analyzer"], public_module)

        self.assertIsInstance(error, RuntimeError)
        self.assertIn("SHA-256", str(error))
        self._assert_state_absent(
            namespace,
            ANALYZER_AND_DOWNSTREAM_STATE_NAMES,
        )
        self._assert_analysis_rejected(sources, namespace)
        self._assert_user_state_preserved(
            namespace,
            queries_df=queries_df,
            schema_df=schema_df,
            pandas_binding=pandas_binding,
            sqlglot_binding=sqlglot_binding,
        )
        namespace["_cleanup_embedded_analyzer_runtime"]()
        namespace["_cleanup_embedded_analyzer_runtime"]()

        exec(compile(sources["payload"], str(NOTEBOOK), "exec"), namespace)
        recovery_error = self._capture_cell_error(sources["loader"], namespace)
        self.assertIsNone(recovery_error)
        recovered_temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
        recovered_embedded_path = namespace["_EMBEDDED_ANALYZER_ZIP_PATH"]
        self.addCleanup(recovered_temporary_directory.cleanup)
        self.addCleanup(
            lambda: (
                sys.path.remove(recovered_embedded_path)
                if recovered_embedded_path in sys.path
                else None
            )
        )
        self.assertNotEqual(recovered_embedded_path, embedded_path)
        self.assertTrue(Path(recovered_temporary_directory.name).is_dir())
        self.assertIn(recovered_embedded_path, sys.path)
        for stage in ("resolver", "analysis"):
            stage_error = self._capture_cell_error(sources[stage], namespace)
            self.assertIsNone(stage_error, f"{stage}: {stage_error}")
        self.assertEqual(len(namespace["row_analysis_df"]), len(queries_df))

    def test_resolver_failure_invalidates_inputs_and_outputs(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        pandas_binding = namespace["pd"]
        sqlglot_binding = namespace["sqlglot"]
        del namespace["my_queries_df"]

        error = self._capture_cell_error(sources["resolver"], namespace)

        self.assertIsInstance(error, NameError)
        self._assert_state_absent(
            namespace,
            INPUT_AND_DOWNSTREAM_STATE_NAMES,
        )
        self._assert_analysis_rejected(sources, namespace)
        self.assertNotIn("my_queries_df", namespace)
        self.assertIs(namespace["my_schema_df"], schema_df)
        self.assertIs(namespace["pd"], pandas_binding)
        self.assertIs(namespace["sqlglot"], sqlglot_binding)

    def test_analyzer_failure_clears_prior_results_and_results_cell_rejects(
        self,
    ) -> None:
        queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        pandas_binding = namespace["pd"]
        sqlglot_binding = namespace["sqlglot"]
        analyzer_error = RuntimeError("simulated analyzer failure")
        namespace["analyze_dataframe"] = Mock(side_effect=analyzer_error)

        error = self._capture_cell_error(sources["analysis"], namespace)

        self.assertIs(error, analyzer_error)
        self._assert_state_absent(namespace, RESULT_STATE_NAMES)
        results_error = self._capture_cell_error(
            sources["results"],
            namespace,
        )
        self.assertIsInstance(results_error, RuntimeError)
        self.assertIn("Run All", str(results_error))
        self._assert_user_state_preserved(
            namespace,
            queries_df=queries_df,
            schema_df=schema_df,
            pandas_binding=pandas_binding,
            sqlglot_binding=sqlglot_binding,
        )

    def test_successful_run_after_failure_uses_new_inputs(self) -> None:
        old_queries_df, schema_df = fixture_dataframes()
        namespace = self._execute_portable_cells(
            queries=old_queries_df,
            schema=schema_df,
        )
        sources = self._portable_sources()
        del namespace["my_queries_df"]
        resolver_error = self._capture_cell_error(
            sources["resolver"],
            namespace,
        )
        self.assertIsInstance(resolver_error, NameError)

        new_queries_df = old_queries_df.iloc[[0]].copy(deep=True)
        new_queries_df.loc[:, "query_id"] = "new"
        namespace["my_queries_df"] = new_queries_df
        for stage in (
            "config",
            "payload",
            "bootstrap",
            "loader",
            "resolver",
            "analysis",
        ):
            error = self._capture_cell_error(sources[stage], namespace)
            self.assertIsNone(error, f"{stage}: {error}")

        new_temporary_directory = namespace["_EMBEDDED_ANALYZER_TEMP_DIR"]
        new_embedded_path = namespace["_EMBEDDED_ANALYZER_ZIP_PATH"]
        self.addCleanup(new_temporary_directory.cleanup)
        self.addCleanup(
            lambda: (
                sys.path.remove(new_embedded_path)
                if new_embedded_path in sys.path
                else None
            )
        )
        self.assertEqual(set(namespace["row_analysis_df"]["query_id"]), {"new"})
        self.assertNotIn(
            "dds",
            set(namespace["row_analysis_df"]["query_id"]),
        )
        self.assertIn("_NOTEBOOK_RUN_TOKEN", namespace)
        run_token = namespace["_NOTEBOOK_RUN_TOKEN"]
        for readiness_name in READINESS_TOKEN_NAMES:
            self.assertIn(readiness_name, namespace)
            self.assertIs(namespace[readiness_name], run_token)
        self.assertIs(namespace["my_queries_df"], new_queries_df)
        self.assertIs(namespace["my_schema_df"], schema_df)

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

    def test_portable_execution_cleanup_removes_only_private_modules(self) -> None:
        queries_df, schema_df = fixture_dataframes()
        unrelated_name = "_embedded_gp_sql_analyzer_unrelated"
        unrelated_module = ModuleType(unrelated_name)
        sys.modules[unrelated_name] = unrelated_module
        try:
            self._execute_portable_cells(queries=queries_df, schema=schema_df)

            self.assertIn("_embedded_gp_sql_analyzer", sys.modules)
            self.doCleanups()
            self.assertFalse(
                any(
                    module_name == "_embedded_gp_sql_analyzer"
                    or module_name.startswith("_embedded_gp_sql_analyzer.")
                    for module_name in sys.modules
                )
            )
            self.assertIs(sys.modules[unrelated_name], unrelated_module)
        finally:
            self.doCleanups()
            sys.modules.pop(unrelated_name, None)

    def test_notebook_executes_outside_repository_without_project_package(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            self.assertFalse(temporary_path.is_relative_to(ROOT))
            notebook = self._prepare_integration_notebook(
                temporary_path=temporary_path,
                expect_project_package=False,
                build_html=True,
            )
            execution = self._execute_notebook_in_subprocess(
                notebook,
                temporary_path=temporary_path,
                pythonpath=None,
            )
            self.assertEqual(
                execution.returncode,
                0,
                execution.stdout + execution.stderr,
            )

    def test_notebook_embedded_analyzer_ignores_installed_name_shadowing(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory).resolve()
            fake_root = temporary_path / "fake-site-packages"
            fake_package = fake_root / "gp_sql_analyzer"
            fake_package.mkdir(parents=True)
            marker_path = temporary_path / "fake-package-imported"
            (fake_package / "__init__.py").write_text(
                "\n".join(
                    [
                        "from pathlib import Path",
                        f"Path({str(marker_path)!r}).write_text(",
                        "    'imported', encoding='utf-8'",
                        ")",
                    ]
                ),
                encoding="utf-8",
            )
            (fake_package / "dataframe.py").write_text(
                'raise AssertionError("installed package must not be imported")\n',
                encoding="utf-8",
            )
            notebook = self._prepare_integration_notebook(
                temporary_path=temporary_path,
                expect_project_package=True,
                build_html=False,
            )

            execution = self._execute_notebook_in_subprocess(
                notebook,
                temporary_path=temporary_path,
                pythonpath=fake_root,
            )

            self.assertEqual(
                execution.returncode,
                0,
                execution.stdout + execution.stderr,
            )
            self.assertFalse(marker_path.exists())


if __name__ == "__main__":
    unittest.main()
