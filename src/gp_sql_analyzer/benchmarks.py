from __future__ import annotations

import hashlib
import json
import ssl
import time
import tracemalloc
import urllib.request
from collections import Counter
from pathlib import Path

import certifi
import sqlglot
from sqlglot import ErrorLevel, exp


DUCKDB_COMMIT = "9ebdd1ee5279885dd2a89d4ac8f37034c05de203"
DUCKDB_RAW_ROOT = f"https://raw.githubusercontent.com/duckdb/duckdb/{DUCKDB_COMMIT}"
TPCDS_ROOT = f"{DUCKDB_RAW_ROOT}/extension/tpcds/dsdgen"
SELECTED_QUERY_CHECKSUMS = {
    2: "20de387d99e0ea6ef34cc333d29021862887e413fb29f492d6f4d79f86817d1c",
    14: "a10708d77bfb2da2b7c2b2b876a51a5141641809af4ca243099faa6e9ea4f0f3",
    34: "3765075f5566834c9e45be8b0d3728c4599f614306df0ded4cedae86daa3594c",
    47: "52a53fd26b6e8a4e0ef6bdc3f87799338f8c161af0b4cc31e23d68ca6072a9f3",
    64: "1948dcb9b0d4b108f5a7696d39332b292090128801b513884323fc4012df52d7",
    91: "ba4bc7ae40d24da58abc28e13431e4526091c149b7b6b7fa5c3c2336624fef89",
}
TPCDS_SCHEMA_TABLES = (
    "call_center",
    "catalog_page",
    "catalog_returns",
    "catalog_sales",
    "customer",
    "customer_address",
    "customer_demographics",
    "date_dim",
    "household_demographics",
    "income_band",
    "inventory",
    "item",
    "promotion",
    "reason",
    "ship_mode",
    "store",
    "store_returns",
    "store_sales",
    "time_dim",
    "warehouse",
    "web_page",
    "web_returns",
    "web_sales",
    "web_site",
)


def query_url(number: int) -> str:
    if not 1 <= number <= 99:
        raise ValueError("TPC-DS query number must be between 1 and 99")
    return f"{TPCDS_ROOT}/queries/{number:02d}.sql"


def schema_url(table: str) -> str:
    if table not in TPCDS_SCHEMA_TABLES:
        raise ValueError(f"unknown TPC-DS table {table!r}")
    return f"{TPCDS_ROOT}/schema/{table}.sql"


def _download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "gp-sql-analyzer/0.1"})
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        return response.read()


def fetch_tpcds(
    destination: Path,
    *,
    full: bool = False,
    include_schema: bool = True,
) -> dict[str, object]:
    destination.mkdir(parents=True, exist_ok=True)
    query_directory = destination / "queries"
    query_directory.mkdir(exist_ok=True)
    query_numbers = range(1, 100) if full else sorted(SELECTED_QUERY_CHECKSUMS)
    files: list[dict[str, str]] = []
    for number in query_numbers:
        url = query_url(number)
        data = _download(url)
        checksum = hashlib.sha256(data).hexdigest()
        expected = SELECTED_QUERY_CHECKSUMS.get(number)
        if expected is not None and checksum != expected:
            raise ValueError(
                f"checksum mismatch for TPC-DS Q{number:02d}: {checksum} != {expected}"
            )
        path = query_directory / f"{number:02d}.sql"
        path.write_bytes(data)
        files.append({"path": str(path.relative_to(destination)), "url": url, "sha256": checksum})

    if include_schema:
        schema_directory = destination / "schema"
        schema_directory.mkdir(exist_ok=True)
        for table in TPCDS_SCHEMA_TABLES:
            url = schema_url(table)
            data = _download(url)
            path = schema_directory / f"{table}.sql"
            path.write_bytes(data)
            files.append(
                {
                    "path": str(path.relative_to(destination)),
                    "url": url,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )

    manifest: dict[str, object] = {
        "benchmark": "TPC-DS",
        "upstream_repository": "https://github.com/duckdb/duckdb",
        "commit": DUCKDB_COMMIT,
        "full_query_set": full,
        "files": files,
    }
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def benchmark_corpus(directory: Path, *, dialect: str = "postgres") -> dict[str, object]:
    files = sorted(directory.rglob("*.sql"))
    counts: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    parsed_files = 0
    statement_count = 0
    table_references = 0
    started = time.perf_counter()
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()

    for path in files:
        try:
            statements = sqlglot.parse(
                path.read_text(encoding="utf-8"),
                read=dialect,
                error_level=ErrorLevel.RAISE,
            )
        except Exception as error:
            errors.append(
                {
                    "file": str(path),
                    "error_type": type(error).__name__,
                    "message": str(error).splitlines()[0][:240],
                }
            )
            continue
        parsed_files += 1
        statement_count += len(statements)
        for statement in statements:
            nodes = list(statement.walk())
            counts["ctes"] += sum(isinstance(node, exp.CTE) for node in nodes)
            counts["subqueries"] += sum(isinstance(node, exp.Subquery) for node in nodes)
            counts["set_operations"] += sum(
                isinstance(node, exp.SetOperation) for node in nodes
            )
            counts["joins"] += sum(isinstance(node, exp.Join) for node in nodes)
            counts["windows"] += sum(isinstance(node, exp.Window) for node in nodes)
            counts["case_expressions"] += sum(isinstance(node, exp.Case) for node in nodes)
            table_references += sum(isinstance(node, exp.Table) for node in nodes)

    _, peak_memory = tracemalloc.get_traced_memory()
    if not already_tracing:
        tracemalloc.stop()
    elapsed = time.perf_counter() - started
    return {
        "files_seen": len(files),
        "files_parsed": parsed_files,
        "parse_success_rate": parsed_files / len(files) if files else 0.0,
        "statements_parsed": statement_count,
        "table_references": table_references,
        "construct_counts": dict(sorted(counts.items())),
        "errors": errors,
        "elapsed_seconds": elapsed,
        "throughput_files_per_second": len(files) / elapsed if elapsed else 0.0,
        "peak_memory_bytes": peak_memory,
    }
