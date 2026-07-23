import ast
import base64
import builtins
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import typing
import unittest
import zipfile
from io import BytesIO
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import nbformat

from scripts.embed_notebook_analyzer import (
    EMBEDDED_PACKAGE,
    NOTEBOOK,
    PAYLOAD_CELL_ID,
    ROOT,
    SOURCE_MODULES,
    build_payload,
    payload_cell_source,
    update_notebook,
)


class EmbeddedNotebookPayloadTests(unittest.TestCase):
    def test_notebook_payload_cell_is_generated_from_current_sources(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        matches = [
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        ]

        self.assertEqual(len(matches), 1)
        self.assertEqual(
            matches[0].source,
            payload_cell_source(build_payload(ROOT)),
        )

    def test_generator_check_accepts_synchronized_notebook(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "embed_notebook_analyzer.py"),
                "--check",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

    def test_loader_rejects_corrupted_payload_by_sha256(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        loader_cell = next(
            cell
            for cell in notebook.cells
            if cell.cell_type == "code" and "def load_embedded_analyzer" in cell.source
        )
        namespace: dict[str, object] = {}
        exec(
            compile(payload_cell.source, str(NOTEBOOK), "exec"),
            namespace,
        )
        encoded = str(namespace["EMBEDDED_ANALYZER_ZIP_B64"])
        replacement = "A" if encoded[0] != "A" else "B"
        namespace["EMBEDDED_ANALYZER_ZIP_B64"] = replacement + encoded[1:]

        with self.assertRaisesRegex(RuntimeError, "SHA-256"):
            exec(
                compile(loader_cell.source, str(NOTEBOOK), "exec"),
                namespace,
            )

    def test_loader_cleans_partial_import_after_base_exception(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        loader_cell = next(
            cell
            for cell in notebook.cells
            if cell.cell_type == "code" and "def load_embedded_analyzer" in cell.source
        )
        real_import = builtins.__import__
        real_temporary_directory = tempfile.TemporaryDirectory

        for failure in (
            RuntimeError("simulated runtime import failure"),
            SyntaxError("simulated syntax import failure"),
        ):
            with self.subTest(error_type=type(failure).__name__):
                namespace: dict[str, object] = {}
                exec(
                    compile(payload_cell.source, str(NOTEBOOK), "exec"),
                    namespace,
                )
                trackers = []
                imported_path: list[str] = []
                unrelated_name = "unrelated_embedded_module"
                unrelated_module = ModuleType(unrelated_name)
                sys.modules[unrelated_name] = unrelated_module

                class TrackingTemporaryDirectory:
                    def __init__(self, *args, **kwargs):
                        self.inner = real_temporary_directory(*args, **kwargs)
                        self.name = self.inner.name
                        self.cleanup_called = False
                        trackers.append(self)

                    def cleanup(self):
                        self.cleanup_called = True
                        self.inner.cleanup()

                def failing_import(
                    name,
                    globals_=None,
                    locals_=None,
                    fromlist=(),
                    level=0,
                ):
                    if name == "_embedded_gp_sql_analyzer.dataframe":
                        imported_path.append(sys.path[0])
                        sys.modules["_embedded_gp_sql_analyzer"] = ModuleType(
                            "_embedded_gp_sql_analyzer"
                        )
                        sys.modules["_embedded_gp_sql_analyzer.partial"] = ModuleType(
                            "_embedded_gp_sql_analyzer.partial"
                        )
                        raise failure
                    return real_import(name, globals_, locals_, fromlist, level)

                try:
                    with (
                        patch.object(
                            tempfile,
                            "TemporaryDirectory",
                            TrackingTemporaryDirectory,
                        ),
                        patch.object(
                            builtins,
                            "__import__",
                            side_effect=failing_import,
                        ),
                    ):
                        with self.assertRaises(type(failure)) as raised:
                            exec(
                                compile(loader_cell.source, str(NOTEBOOK), "exec"),
                                namespace,
                            )

                    self.assertIs(raised.exception, failure)
                    self.assertEqual(len(trackers), 1)
                    self.assertTrue(trackers[0].cleanup_called)
                    self.assertFalse(Path(trackers[0].name).exists())
                    self.assertEqual(len(imported_path), 1)
                    self.assertNotIn(imported_path[0], sys.path)
                    self.assertNotIn("_embedded_gp_sql_analyzer", sys.modules)
                    self.assertNotIn(
                        "_embedded_gp_sql_analyzer.partial",
                        sys.modules,
                    )
                    self.assertIs(sys.modules[unrelated_name], unrelated_module)

                    exec(
                        compile(loader_cell.source, str(NOTEBOOK), "exec"),
                        namespace,
                    )
                    self.assertTrue(callable(namespace["analyze_dataframe"]))
                finally:
                    archive_path = namespace.get("_EMBEDDED_ANALYZER_ZIP_PATH")
                    if archive_path in sys.path:
                        sys.path.remove(archive_path)
                    temporary_directory = namespace.get(
                        "_EMBEDDED_ANALYZER_TEMP_DIR"
                    )
                    if temporary_directory is not None:
                        temporary_directory.cleanup()
                    for module_name in list(sys.modules):
                        if (
                            module_name == "_embedded_gp_sql_analyzer"
                            or module_name.startswith(
                                "_embedded_gp_sql_analyzer."
                            )
                        ):
                            del sys.modules[module_name]
                    sys.modules.pop(unrelated_name, None)

    def test_update_notebook_changes_only_payload_cell(self) -> None:
        source_notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell
            for cell in source_notebook.cells
            if cell.get("id") == PAYLOAD_CELL_ID
        )
        payload_cell.source = "# stale"
        payload_cell.execution_count = 7
        payload_cell.outputs = [
            nbformat.v4.new_output("stream", name="stdout", text="stale\n")
        ]
        original_metadata = copy.deepcopy(source_notebook.metadata)
        untouched_cells = copy.deepcopy(
            [
                cell
                for cell in source_notebook.cells
                if cell.get("id") != PAYLOAD_CELL_ID
            ]
        )
        original_payload_metadata = copy.deepcopy(payload_cell.metadata)
        original_mode = 0o640

        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook_path = Path(temporary_directory) / NOTEBOOK.name
            nbformat.write(source_notebook, notebook_path)
            notebook_path.chmod(original_mode)

            self.assertTrue(update_notebook(notebook_path))

            updated = nbformat.read(notebook_path, as_version=4)
            updated_mode = notebook_path.stat().st_mode & 0o777

        updated_payload = next(
            cell for cell in updated.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        self.assertEqual(
            updated_payload.source,
            payload_cell_source(build_payload(ROOT)),
        )
        self.assertIsNone(updated_payload.execution_count)
        self.assertEqual(updated_payload.outputs, [])
        self.assertEqual(updated_payload.metadata, original_payload_metadata)
        self.assertEqual(updated.metadata, original_metadata)
        self.assertEqual(
            [
                cell
                for cell in updated.cells
                if cell.get("id") != PAYLOAD_CELL_ID
            ],
            untouched_cells,
        )
        self.assertEqual(updated_mode, original_mode)

    def test_update_notebook_replace_failure_preserves_original(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        payload_cell.source = "# stale"

        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook_path = Path(temporary_directory) / NOTEBOOK.name
            nbformat.write(notebook, notebook_path)
            original_bytes = notebook_path.read_bytes()
            original_entries = set(notebook_path.parent.iterdir())

            with (
                patch.object(
                    os,
                    "replace",
                    side_effect=OSError("simulated replace failure"),
                ),
                self.assertRaisesRegex(OSError, "simulated replace failure"),
            ):
                update_notebook(notebook_path)

            self.assertEqual(notebook_path.read_bytes(), original_bytes)
            self.assertEqual(
                set(notebook_path.parent.iterdir()),
                original_entries,
            )

    def test_update_notebook_validation_failure_preserves_original(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        payload_cell.source = "# stale"
        invalid_candidate = {
            "cells": [
                {
                    "cell_type": "code",
                    "id": "schema-invalid-cell",
                    "metadata": {},
                    "source": "print('readable but invalid')",
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }

        def write_invalid_candidate(_notebook, temporary_file):
            json.dump(invalid_candidate, temporary_file)

        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook_path = Path(temporary_directory) / NOTEBOOK.name
            nbformat.write(notebook, notebook_path)
            original_bytes = notebook_path.read_bytes()
            original_entries = set(notebook_path.parent.iterdir())

            with (
                patch.object(
                    nbformat,
                    "write",
                    side_effect=write_invalid_candidate,
                ),
                patch.object(os, "replace", wraps=os.replace) as replace_mock,
                self.assertRaises(nbformat.validator.NotebookValidationError),
            ):
                update_notebook(notebook_path)

            replace_mock.assert_not_called()
            self.assertEqual(notebook_path.read_bytes(), original_bytes)
            self.assertEqual(
                set(notebook_path.parent.iterdir()),
                original_entries,
            )

    def test_update_notebook_check_rejects_stale_payload(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        payload_cell = next(
            cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
        )
        payload_cell.source = "# stale"

        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook_path = Path(temporary_directory) / NOTEBOOK.name
            nbformat.write(notebook, notebook_path)

            with self.assertRaisesRegex(RuntimeError, "regenerat"):
                update_notebook(notebook_path, check=True)

    def test_update_notebook_requires_exactly_one_payload_cell(self) -> None:
        notebook = nbformat.v4.new_notebook(
            cells=[nbformat.v4.new_code_cell("print('no payload')")]
        )

        with tempfile.TemporaryDirectory() as temporary_directory:
            notebook_path = Path(temporary_directory) / NOTEBOOK.name
            nbformat.write(notebook, notebook_path)

            with self.assertRaisesRegex(RuntimeError, PAYLOAD_CELL_ID):
                update_notebook(notebook_path)

    def test_payload_is_deterministic_and_has_complete_manifest(self) -> None:
        first = build_payload(ROOT)
        second = build_payload(ROOT)

        self.assertEqual(first.archive, second.archive)
        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.manifest, tuple(SOURCE_MODULES))

    def test_payload_zip_contains_exact_source_modules(self) -> None:
        payload = build_payload(ROOT)
        expected_names = [
            f"{EMBEDDED_PACKAGE}/{relative_path}" for relative_path in SOURCE_MODULES
        ]

        with zipfile.ZipFile(BytesIO(payload.archive)) as archive:
            self.assertEqual(archive.namelist(), expected_names)
            for relative_path in SOURCE_MODULES:
                info = archive.getinfo(f"{EMBEDDED_PACKAGE}/{relative_path}")
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED)
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.external_attr, 0o100644 << 16)
                self.assertEqual(
                    archive.read(f"{EMBEDDED_PACKAGE}/{relative_path}"),
                    (ROOT / "src" / "gp_sql_analyzer" / relative_path).read_bytes(),
                )

    def test_payload_cell_source_embeds_exact_payload(self) -> None:
        payload = build_payload(ROOT)
        source = payload_cell_source(payload)

        namespace: dict[str, object] = {}
        exec(compile(source, "<embedded-analyzer-payload>", "exec"), namespace)

        self.assertEqual(namespace["EMBEDDED_ANALYZER_FORMAT"], 1)
        self.assertEqual(namespace["EMBEDDED_ANALYZER_MANIFEST"], payload.manifest)
        self.assertEqual(namespace["EMBEDDED_ANALYZER_SHA256"], payload.sha256)
        self.assertEqual(namespace["EMBEDDED_ANALYZER_ZIP_B64"], payload.base64_text)
        self.assertEqual(
            base64.b64decode(namespace["EMBEDDED_ANALYZER_ZIP_B64"]), payload.archive
        )
        self.assertEqual(hashlib.sha256(payload.archive).hexdigest(), payload.sha256)

    def test_catalog_stats_avoids_runtime_complexity_import(self) -> None:
        source_path = ROOT / "src" / "gp_sql_analyzer" / "catalog_stats.py"
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        class RuntimeComplexityImports(ast.NodeVisitor):
            def __init__(self) -> None:
                self.imports: list[ast.ImportFrom] = []

            def visit_If(self, node: ast.If) -> None:
                if isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                    for statement in node.orelse:
                        self.visit(statement)
                    return
                self.generic_visit(node)

            def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
                if node.module == "complexity":
                    self.imports.append(node)

        visitor = RuntimeComplexityImports()
        visitor.visit(tree)
        self.assertEqual(visitor.imports, [])
        self.assertIn("if TYPE_CHECKING:", source)

    def test_catalog_stats_public_type_hints_resolve_at_runtime(self) -> None:
        from gp_sql_analyzer import catalog_stats

        self.assertIn("corpus", typing.get_type_hints(catalog_stats.build_catalog_report))


if __name__ == "__main__":
    unittest.main()
