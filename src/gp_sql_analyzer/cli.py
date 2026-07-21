from __future__ import annotations

import argparse
import json
import time
import tracemalloc
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Sequence

from .aggregate import UsageAggregator
from .analyzer import SQLAnalyzer
from .benchmarks import benchmark_corpus
from .catalog_html import render_catalog_html
from .catalog_stats import (
    CatalogReport,
    build_catalog_report,
    build_catalog_report_from_details,
)
from .complexity import analyze_corpus
from .ddl_schema import load_ddl_schema
from .html_report import render_html
from .io import JsonlWriter, iter_jsonl_records
from .greenplum import (
    SourceQueryConfig,
    connect_greenplum,
    iter_greenplum_records,
    load_catalog_schema,
)
from .models import QueryRecord
from .schema import MappingSchemaProvider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gp-sql-analyzer")
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze", help="analyze original/template SQL pairs")
    source = analyze.add_mutually_exclusive_group(required=True)
    source.add_argument("--input-jsonl", type=Path)
    source.add_argument("--source-table")
    analyze.add_argument("--schema-json", type=Path)
    analyze.add_argument("--default-schema")
    analyze.add_argument("--catalog-schema", action="append", default=[])
    analyze.add_argument("--id-column")
    analyze.add_argument("--since-column")
    analyze.add_argument("--since-value")
    analyze.add_argument("--min-id")
    analyze.add_argument("--max-id")
    analyze.add_argument("--limit", type=int)
    analyze.add_argument("--no-preaggregate", action="store_true")
    analyze.add_argument("--output-dir", type=Path, required=True)
    analyze.add_argument("--batch-size", type=int, default=500)
    analyze.add_argument("--example-limit", type=int, default=3)
    analyze.add_argument("--dialect", default="postgres")
    benchmark = subparsers.add_parser(
        "benchmark", help="measure parser coverage on a SQL corpus"
    )
    benchmark.add_argument("--corpus-dir", type=Path, required=True)
    benchmark.add_argument("--output-json", type=Path, required=True)
    benchmark.add_argument("--dialect", default="postgres")
    html_report = subparsers.add_parser(
        "html-report", help="rank and explain every query in a self-contained HTML report"
    )
    html_report.add_argument("--corpus-dir", type=Path, required=True)
    html_report.add_argument("--output-html", type=Path, required=True)
    html_report.add_argument("--dialect", default="postgres")
    html_report.add_argument("--source-label", default="TPC-DS")
    html_report.add_argument("--schema-dir", type=Path)
    html_report.add_argument("--default-schema", default="tpcds")
    catalog_report = subparsers.add_parser(
        "catalog-report",
        help="aggregate a SQL corpus into per-table/per-column catalog statistics",
    )
    catalog_report.add_argument("--corpus-dir", type=Path, required=True)
    catalog_report.add_argument("--schema-dir", type=Path, required=True)
    catalog_report.add_argument("--output-json", type=Path, required=True)
    catalog_report.add_argument("--output-jsonl", type=Path, required=True)
    catalog_report.add_argument("--output-html", type=Path, required=True)
    catalog_report.add_argument("--dialect", default="postgres")
    catalog_report.add_argument("--source-label", default="TPC-DS")
    catalog_report.add_argument("--default-schema", default="tpcds")
    catalog_report.add_argument("--top-limit", type=int, default=20)
    catalog_report.add_argument("--example-limit", type=int, default=5)
    catalog_postprocess = subparsers.add_parser(
        "catalog-postprocess",
        help="build catalog statistics from persisted details.jsonl without parsing SQL",
    )
    catalog_postprocess.add_argument("--details-jsonl", type=Path, required=True)
    catalog_postprocess.add_argument("--schema-json", type=Path, required=True)
    catalog_postprocess.add_argument("--output-json", type=Path, required=True)
    catalog_postprocess.add_argument("--output-jsonl", type=Path, required=True)
    catalog_postprocess.add_argument("--output-html", type=Path, required=True)
    catalog_postprocess.add_argument("--default-schema")
    catalog_postprocess.add_argument("--catalog-name")
    catalog_postprocess.add_argument("--dialect", default="postgres")
    catalog_postprocess.add_argument("--source-label", default="details.jsonl")
    catalog_postprocess.add_argument("--source-commit")
    catalog_postprocess.add_argument("--top-limit", type=int, default=20)
    catalog_postprocess.add_argument("--example-limit", type=int, default=5)
    return parser


def _iter_detail_rows(path: Path) -> Iterable[dict[str, object]]:
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number}: expected a JSON object")
            yield value


def _write_catalog_artifacts(
    report: CatalogReport,
    *,
    output_json: Path,
    output_jsonl: Path,
    output_html: Path,
) -> None:
    for path in (output_json, output_jsonl, output_html):
        path.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=False)
        + "\n",
        encoding="utf-8",
    )
    with JsonlWriter(output_jsonl) as writer:
        for row in report.column_rows():
            writer.write(row)
    output_html.write_text(render_catalog_html(report), encoding="utf-8")


def _run_batches(
    batches: Iterable[list[QueryRecord]],
    analyzer: SQLAnalyzer,
    output_dir: Path,
    *,
    example_limit: int,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    aggregator = UsageAggregator(example_limit=example_limit)
    lineage_counts: Counter[str] = Counter()
    records_seen = 0
    records_parsed = 0
    source_rows_seen = 0
    occurrence_count = 0
    error_count = 0
    started = time.perf_counter()
    already_tracing = tracemalloc.is_tracing()
    if not already_tracing:
        tracemalloc.start()

    with JsonlWriter(output_dir / "details.jsonl") as details_writer, JsonlWriter(
        output_dir / "errors.jsonl"
    ) as errors_writer:
        for batch in batches:
            for record in batch:
                records_seen += 1
                source_rows_seen += record.source_row_count
                result = analyzer.analyze_record(record)
                parse_failed = any(error.stage == "parse" for error in result.errors)
                if not parse_failed:
                    records_parsed += 1
                for occurrence in result.occurrences:
                    occurrence_count += 1
                    lineage_counts[occurrence.lineage.status] += 1
                    aggregator.add(occurrence)
                    details_writer.write(occurrence.to_dict())
                for error in result.errors:
                    error_count += 1
                    errors_writer.write(error.to_dict())

    with JsonlWriter(output_dir / "summary.jsonl") as summary_writer:
        for row in aggregator.rows():
            summary_writer.write(row)

    _, peak_memory = tracemalloc.get_traced_memory()
    if not already_tracing:
        tracemalloc.stop()
    elapsed = time.perf_counter() - started
    metrics: dict[str, object] = {
        "records_seen": records_seen,
        "records_parsed": records_parsed,
        "source_rows_seen": source_rows_seen,
        "parse_success_rate": records_parsed / records_seen if records_seen else 0.0,
        "occurrences": occurrence_count,
        "errors": error_count,
        "lineage_status_counts": dict(sorted(lineage_counts.items())),
        "elapsed_seconds": elapsed,
        "throughput_records_per_second": records_seen / elapsed if elapsed else 0.0,
        "peak_memory_bytes": peak_memory,
    }
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metrics


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "benchmark":
        report = benchmark_corpus(args.corpus_dir, dialect=args.dialect)
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "html-report":
        corpus = analyze_corpus(
            args.corpus_dir,
            dialect=args.dialect,
            source_label=args.source_label,
            schema_directory=args.schema_dir,
            default_schema=args.default_schema,
        )
        args.output_html.parent.mkdir(parents=True, exist_ok=True)
        args.output_html.write_text(render_html(corpus), encoding="utf-8")
        summary = {
            "files_seen": corpus.files_seen,
            "files_parsed": corpus.files_parsed,
            "errors": len(corpus.errors),
            "output_html": str(args.output_html),
            "top_query": corpus.queries[0].name if corpus.queries else None,
            "top_score": corpus.queries[0].score if corpus.queries else None,
            "literal_conditions": sum(
                usage.condition_count
                for query in corpus.queries
                for usage in query.literal_usages
            ),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "catalog-report":
        corpus = analyze_corpus(
            args.corpus_dir,
            dialect=args.dialect,
            source_label=args.source_label,
            schema_directory=args.schema_dir,
            default_schema=args.default_schema,
        )
        schema = load_ddl_schema(
            args.schema_dir,
            dialect=args.dialect,
            default_schema=args.default_schema,
        )
        report = build_catalog_report(
            corpus,
            schema,
            top_limit=args.top_limit,
            example_limit=args.example_limit,
        )
        _write_catalog_artifacts(
            report,
            output_json=args.output_json,
            output_jsonl=args.output_jsonl,
            output_html=args.output_html,
        )
        report_summary = report.summary
        summary = {
            "files_seen": corpus.files_seen,
            "files_parsed": corpus.files_parsed,
            "errors": len(corpus.errors),
            "table_count": report_summary["table_count"],
            "column_count": report_summary["column_count"],
            "active_column_count": report_summary["active_column_count"],
            "lineage_resolution_rate": report_summary["lineage_resolution_rate"],
            "output_json": str(args.output_json),
            "output_jsonl": str(args.output_jsonl),
            "output_html": str(args.output_html),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command == "catalog-postprocess":
        schema_payload = json.loads(args.schema_json.read_text(encoding="utf-8"))
        if isinstance(schema_payload, dict) and isinstance(
            schema_payload.get("schemas"), dict
        ):
            mapping = schema_payload["schemas"]
            default_schema = args.default_schema or schema_payload.get("default_schema")
            catalog_name = args.catalog_name or schema_payload.get("catalog_name")
        else:
            mapping = schema_payload
            default_schema = args.default_schema
            catalog_name = args.catalog_name
        schema = MappingSchemaProvider(
            mapping,
            default_schema=default_schema,
            catalog=catalog_name,
        )
        report = build_catalog_report_from_details(
            _iter_detail_rows(args.details_jsonl),
            schema,
            source_label=args.source_label,
            source_commit=args.source_commit,
            dialect=args.dialect,
            top_limit=args.top_limit,
            example_limit=args.example_limit,
        )
        _write_catalog_artifacts(
            report,
            output_json=args.output_json,
            output_jsonl=args.output_jsonl,
            output_html=args.output_html,
        )
        report_summary = report.summary
        status_counts = report_summary["lineage_status_counts"]
        summary = {
            "query_count": report_summary["query_count"],
            "table_count": report_summary["table_count"],
            "column_count": report_summary["column_count"],
            "active_column_count": report_summary["active_column_count"],
            "source_row_count": sum(status_counts.values()),
            "lineage_resolution_rate": report_summary["lineage_resolution_rate"],
            "output_json": str(args.output_json),
            "output_jsonl": str(args.output_jsonl),
            "output_html": str(args.output_html),
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    if args.command != "analyze":
        raise AssertionError(f"unsupported command {args.command}")
    connection = None
    if args.input_jsonl is not None:
        if args.schema_json is None:
            parser.error("--schema-json is required with --input-jsonl")
        mapping = json.loads(args.schema_json.read_text(encoding="utf-8"))
        schema = MappingSchemaProvider(mapping, default_schema=args.default_schema)
        batches = iter_jsonl_records(args.input_jsonl, batch_size=args.batch_size)
    else:
        connection = connect_greenplum()
        schema = load_catalog_schema(
            connection,
            schemas=args.catalog_schema or None,
            default_schema=args.default_schema,
        )
        source_config = SourceQueryConfig(
            table=args.source_table,
            id_column=args.id_column,
            since_column=args.since_column,
            since_value=args.since_value,
            min_id=args.min_id,
            max_id=args.max_id,
            limit=args.limit,
            preaggregate=not args.no_preaggregate,
        )
        batches = iter_greenplum_records(
            connection, source_config, batch_size=args.batch_size
        )
    analyzer = SQLAnalyzer(schema, dialect=args.dialect)
    try:
        metrics = _run_batches(
            batches,
            analyzer,
            args.output_dir,
            example_limit=args.example_limit,
        )
        (args.output_dir / "schema.json").write_text(
            json.dumps(
                schema.to_snapshot(),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    finally:
        if connection is not None:
            connection.close()
    print(json.dumps(metrics, ensure_ascii=False, sort_keys=True))
    return 0
