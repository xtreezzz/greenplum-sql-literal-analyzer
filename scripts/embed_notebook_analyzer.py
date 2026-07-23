"""Build a deterministic embedded copy of the notebook SQL analyzer."""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import nbformat


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "sql_catalog_from_dataframe.ipynb"
SOURCE_ROOT = ROOT / "src" / "gp_sql_analyzer"
EMBEDDED_PACKAGE = "_embedded_gp_sql_analyzer"
PAYLOAD_CELL_ID = "embedded-analyzer-payload"
SOURCE_MODULES = (
    "__init__.py",
    "analyzer.py",
    "catalog_html.py",
    "catalog_stats.py",
    "dataframe.py",
    "io.py",
    "lineage.py",
    "models.py",
    "patterns.py",
    "placeholders.py",
    "schema.py",
)


@dataclass(frozen=True)
class EmbeddedPayload:
    archive: bytes
    base64_text: str
    sha256: str
    manifest: tuple[str, ...]


def build_payload(root: Path = ROOT) -> EmbeddedPayload:
    """Return a deterministic ZIP archive of the notebook runtime modules."""
    stream = BytesIO()
    with zipfile.ZipFile(
        stream,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        for relative_path in SOURCE_MODULES:
            member = zipfile.ZipInfo(f"{EMBEDDED_PACKAGE}/{relative_path}")
            member.date_time = (1980, 1, 1, 0, 0, 0)
            member.compress_type = zipfile.ZIP_STORED
            member.create_system = 3
            member.external_attr = 0o100644 << 16
            archive.writestr(
                member,
                (root / "src" / "gp_sql_analyzer" / relative_path).read_bytes(),
            )
    archive_bytes = stream.getvalue()
    return EmbeddedPayload(
        archive=archive_bytes,
        base64_text=base64.b64encode(archive_bytes).decode("ascii"),
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        manifest=SOURCE_MODULES,
    )


def payload_cell_source(payload: EmbeddedPayload) -> str:
    """Render the deterministic notebook cell containing *payload*."""
    chunks = [payload.base64_text[index : index + 88] for index in range(0, len(payload.base64_text), 88)]
    encoded_lines = "\n".join(f'    "{chunk}"' for chunk in chunks)
    return (
        "# Generated file payload. Do not edit manually; regenerate from source modules.\n"
        "EMBEDDED_ANALYZER_FORMAT = 1\n"
        f"EMBEDDED_ANALYZER_MANIFEST = {payload.manifest!r}\n"
        f'EMBEDDED_ANALYZER_SHA256 = "{payload.sha256}"\n'
        "EMBEDDED_ANALYZER_ZIP_B64 = (\n"
        f"{encoded_lines}\n"
        ")\n"
    )


def _write_notebook_atomically(
    notebook: nbformat.NotebookNode,
    notebook_path: Path,
) -> None:
    """Validate and atomically replace *notebook_path* with *notebook*."""
    original_mode = stat.S_IMODE(notebook_path.stat().st_mode)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{notebook_path.name}.",
        suffix=".tmp",
        dir=notebook_path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            descriptor = -1
            nbformat.write(notebook, temporary_file)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        candidate = nbformat.read(temporary_path, as_version=4)
        nbformat.validate(candidate)
        temporary_path.chmod(original_mode)
        os.replace(temporary_path, notebook_path)

        directory_descriptor = None
        try:
            directory_descriptor = os.open(notebook_path.parent, os.O_RDONLY)
            os.fsync(directory_descriptor)
        except OSError:
            pass
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def update_notebook(
    notebook_path: Path = NOTEBOOK,
    *,
    check: bool = False,
) -> bool:
    """Synchronize the notebook payload cell with the current source modules."""
    notebook_path = Path(notebook_path)
    notebook = nbformat.read(notebook_path, as_version=4)
    matches = [
        cell for cell in notebook.cells if cell.get("id") == PAYLOAD_CELL_ID
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one notebook cell with id {PAYLOAD_CELL_ID!r}; "
            f"found {len(matches)}."
        )

    expected = payload_cell_source(build_payload(ROOT))
    payload_cell = matches[0]
    if payload_cell.source == expected:
        return False
    if check:
        raise RuntimeError(
            "Embedded analyzer payload is stale; regenerate it with "
            "`PYTHONPATH=src python3 scripts/embed_notebook_analyzer.py`."
        )

    payload_cell.source = expected
    payload_cell.execution_count = None
    payload_cell.outputs = []
    _write_notebook_atomically(notebook, notebook_path)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize the embedded notebook analyzer payload."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when the notebook payload is stale",
    )
    arguments = parser.parse_args()

    try:
        changed = update_notebook(check=arguments.check)
    except RuntimeError as error:
        parser.exit(1, f"{error}\n")

    if arguments.check:
        print("Embedded analyzer payload is synchronized.")
    elif changed:
        print(f"Updated embedded analyzer payload in {NOTEBOOK}.")
    else:
        print("Embedded analyzer payload is already synchronized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
