from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, TextIO

from .models import QueryRecord


class JsonlWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None

    def __enter__(self) -> "JsonlWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")
        return self

    def write(self, payload: Mapping[str, Any]) -> None:
        if self._handle is None:
            raise RuntimeError("JsonlWriter must be used as a context manager")
        serialized = json.dumps(
            dict(payload), ensure_ascii=False, sort_keys=True, separators=(", ", ": ")
        )
        self._handle.write(serialized + "\n")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def iter_jsonl_records(
    path: str | Path,
    *,
    batch_size: int,
) -> Iterator[list[QueryRecord]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    batch: list[QueryRecord] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            try:
                record = QueryRecord(
                    query_id=str(payload.get("query_id") or f"line-{line_number}"),
                    query_text=str(payload["query_text"]),
                    query_text_template=str(payload["query_text_template"]),
                    source_row_count=int(payload.get("source_row_count", 1)),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"invalid JSONL record at line {line_number}: {error}") from error
            batch.append(record)
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch
