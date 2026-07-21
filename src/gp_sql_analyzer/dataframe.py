from __future__ import annotations

import hashlib
import importlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .analyzer import SQLAnalyzer
from .catalog_html import render_catalog_html
from .catalog_stats import CatalogReport, build_catalog_report_from_details
from .io import JsonlWriter
from .models import Occurrence, PredicateUsage, QueryRecord
from .schema import MappingSchemaProvider


DETAIL_COLUMNS = [
    "query_id",
    "query_hash",
    "template_hash",
    "source_row_count",
    "catalog_name",
    "schema_name",
    "table_name",
    "column_name",
    "base_columns",
    "lineage_status",
    "lineage_reason",
    "clause_context",
    "operator_or_function",
    "value_role",
    "raw_literal",
    "extracted_value",
    "pattern_template",
    "pattern_family",
    "pattern_format",
    "regex_features",
    "ast_path",
    "origin",
]

ERROR_COLUMNS = [
    "query_id",
    "stage",
    "error_type",
    "message",
    "sql_fragment",
]

AGGREGATE_GROUP_COLUMNS = [
    "catalog_name",
    "schema_name",
    "table_name",
    "column_name",
    "extracted_value",
    "clause_context",
    "operator_or_function",
    "value_role",
    "pattern_family",
    "pattern_format",
]

AGGREGATE_COLUMNS = [
    *AGGREGATE_GROUP_COLUMNS,
    "qualified_name",
    "source_row_count",
    "occurrence_count",
    "distinct_query_count",
    "distinct_template_count",
    "share_of_column",
    "example_query_ids",
]


@dataclass(slots=True)
class DataFrameAnalysis:
    row_analysis_df: Any
    aggregate_df: Any
    details_df: Any
    errors_df: Any
    catalog_columns_df: Any
    catalog_tables_df: Any
    catalog_report: dict[str, Any]
    artifact_paths: dict[str, Path] = field(default_factory=dict)


def _pandas():
    try:
        return importlib.import_module("pandas")
    except ImportError as error:
        raise RuntimeError(
            "DataFrame analysis requires pandas; "
            "install gp-sql-analyzer[notebook]"
        ) from error


def schema_from_dataframe(
    schema_df: Any | None,
    *,
    default_schema: str | None = None,
) -> MappingSchemaProvider:
    if schema_df is None:
        return MappingSchemaProvider({}, default_schema=default_schema)

    required = {"table_schema", "table_name", "column_name"}
    missing = sorted(required - set(schema_df.columns))
    if missing:
        raise ValueError(
            "schema_df is missing required columns: " + ", ".join(missing)
        )

    catalogs: set[str] = set()
    mapping: dict[str, dict[str, list[str]]] = {}
    for _, row in schema_df.iterrows():
        schema_name = str(row["table_schema"])
        table_name = str(row["table_name"])
        column_name = str(row["column_name"])
        mapping.setdefault(schema_name, {}).setdefault(table_name, []).append(
            column_name
        )
        if "table_catalog" in schema_df.columns:
            value = row["table_catalog"]
            if value is not None and str(value).casefold() not in {"nan", "<na>"}:
                catalogs.add(str(value))
    if len(catalogs) > 1:
        raise ValueError("schema_df must contain at most one table_catalog")
    catalog = next(iter(catalogs), None)
    return MappingSchemaProvider(
        mapping,
        default_schema=default_schema,
        catalog=catalog,
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _lineage_fields(lineage) -> dict[str, Any]:
    normalized = lineage.normalized()
    only = normalized.columns[0] if len(normalized.columns) == 1 else None
    return {
        "catalog_name": only.catalog if only else None,
        "schema_name": only.schema if only else None,
        "table_name": only.table if only else None,
        "column_name": only.column if only else None,
        "base_columns": [column.qualified_name for column in normalized.columns],
        "lineage_status": normalized.status,
        "lineage_reason": normalized.reason,
    }


def _occurrence_row(occurrence: Occurrence) -> dict[str, Any]:
    row = occurrence.to_dict()
    row["catalog_name"] = (
        occurrence.lineage.columns[0].catalog
        if len(occurrence.lineage.columns) == 1
        else None
    )
    row["origin"] = "template"
    return row


def _predicate_row(
    usage: PredicateUsage,
    *,
    record: QueryRecord,
) -> dict[str, Any]:
    return {
        "query_id": record.query_id,
        "query_hash": _hash(record.query_text),
        "template_hash": _hash(record.query_text_template),
        "source_row_count": record.source_row_count,
        **_lineage_fields(usage.lineage),
        "clause_context": usage.clause_context,
        "operator_or_function": usage.operator_or_function,
        "value_role": usage.value_role,
        "raw_literal": usage.raw_literal,
        "extracted_value": usage.extracted_value,
        "pattern_template": usage.pattern_template,
        "pattern_family": usage.pattern_family,
        "pattern_format": usage.pattern_format,
        "regex_features": dict(sorted(usage.regex_features.items())),
        "ast_path": usage.ast_path,
        "origin": usage.origin,
    }


def _dedupe_signature(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        tuple(row.get("base_columns") or []),
        row.get("lineage_status"),
        row.get("clause_context"),
        row.get("operator_or_function"),
        row.get("raw_literal"),
    )


@dataclass(slots=True)
class _AggregateGroup:
    source_row_count: int = 0
    occurrence_count: int = 0
    query_ids: set[str] = field(default_factory=set)
    template_hashes: set[str] = field(default_factory=set)


def _aggregate_rows(
    rows: Iterable[Mapping[str, Any]], *, example_limit: int
) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], _AggregateGroup] = {}
    column_totals: Counter[tuple[Any, ...]] = Counter()
    for row in rows:
        base_columns = list(row.get("base_columns") or [])
        if row.get("lineage_status") != "resolved" or len(base_columns) != 1:
            continue
        key = tuple(row.get(column) for column in AGGREGATE_GROUP_COLUMNS)
        weight = int(row.get("source_row_count") or 1)
        group = groups.setdefault(key, _AggregateGroup())
        group.source_row_count += weight
        group.occurrence_count += 1
        group.query_ids.add(str(row.get("query_id") or ""))
        group.template_hashes.add(str(row.get("template_hash") or ""))
        column_key = key[:4]
        column_totals[column_key] += weight

    output: list[dict[str, Any]] = []
    for key, group in groups.items():
        values = dict(zip(AGGREGATE_GROUP_COLUMNS, key))
        qualified_name = ".".join(
            str(values[name])
            for name in ("catalog_name", "schema_name", "table_name", "column_name")
            if values[name]
        )
        total = column_totals[key[:4]]
        output.append(
            {
                **values,
                "qualified_name": qualified_name,
                "source_row_count": group.source_row_count,
                "occurrence_count": group.occurrence_count,
                "distinct_query_count": len(group.query_ids),
                "distinct_template_count": len(group.template_hashes),
                "share_of_column": (
                    group.source_row_count / total if total else 0.0
                ),
                "example_query_ids": sorted(group.query_ids)[:example_limit],
            }
        )
    return sorted(
        output,
        key=lambda row: (
            row["qualified_name"],
            -row["source_row_count"],
            str(row["extracted_value"]),
            str(row["clause_context"]),
            str(row["operator_or_function"]),
        ),
    )


def _validate_queries(queries_df: Any) -> None:
    required = {"query_text", "query_text_template"}
    missing = sorted(required - set(queries_df.columns))
    if missing:
        raise ValueError(
            "queries_df is missing required columns: " + ", ".join(missing)
        )
    if "source_row_count" not in queries_df.columns:
        return
    for value in queries_df["source_row_count"].tolist():
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("source_row_count must contain positive integers") from error
        if parsed <= 0 or float(value) != parsed:
            raise ValueError("source_row_count must contain positive integers")


def _write_dataframe_jsonl(dataframe: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dataframe.to_json(
        path,
        orient="records",
        lines=True,
        force_ascii=False,
        date_format="iso",
    )


def _write_artifacts(
    *,
    output_dir: Path,
    row_analysis_df: Any,
    details_df: Any,
    errors_df: Any,
    aggregate_df: Any,
    report: CatalogReport,
    schema: MappingSchemaProvider,
    build_html: bool,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "row_analysis": output_dir / "row_analysis.jsonl",
        "details": output_dir / "details.jsonl",
        "errors": output_dir / "errors.jsonl",
        "aggregate": output_dir / "aggregate.jsonl",
        "catalog_json": output_dir / "catalog-stats.json",
        "catalog_columns": output_dir / "catalog-columns.jsonl",
        "schema": output_dir / "schema.json",
    }
    _write_dataframe_jsonl(row_analysis_df, paths["row_analysis"])
    _write_dataframe_jsonl(details_df, paths["details"])
    _write_dataframe_jsonl(errors_df, paths["errors"])
    _write_dataframe_jsonl(aggregate_df, paths["aggregate"])
    paths["catalog_json"].write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    with JsonlWriter(paths["catalog_columns"]) as writer:
        for row in report.column_rows():
            writer.write(row)
    paths["schema"].write_text(
        json.dumps(schema.to_snapshot(), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    if build_html:
        html_path = output_dir / "catalog-stats.html"
        html_path.write_text(render_catalog_html(report), encoding="utf-8")
        paths["html"] = html_path
    return paths


def analyze_dataframe(
    queries_df: Any,
    *,
    schema_df: Any | None = None,
    default_schema: str | None = None,
    dialect: str = "postgres",
    placeholder: str = "&CHARACTER",
    include_original_literals: bool = True,
    include_null_checks: bool = True,
    output_dir: str | Path | None = None,
    build_html: bool = False,
    top_limit: int = 20,
    example_limit: int = 5,
    source_label: str = "pandas.DataFrame",
) -> DataFrameAnalysis:
    pd = _pandas()
    _validate_queries(queries_df)
    if build_html and output_dir is None:
        raise ValueError("output_dir is required when build_html=True")
    schema = schema_from_dataframe(schema_df, default_schema=default_schema)
    analyzer = SQLAnalyzer(schema, dialect=dialect, placeholder=placeholder)

    row_records: list[dict[str, Any]] = []
    detail_rows: list[dict[str, Any]] = []
    error_rows: list[dict[str, Any]] = []
    for position, (index, source_row) in enumerate(queries_df.iterrows(), start=1):
        source = source_row.to_dict()
        query_id_value = source.get("query_id")
        if query_id_value is None or str(query_id_value).casefold() in {"nan", "<na>"}:
            query_id = str(index) if index is not None else f"row-{position}"
        else:
            query_id = str(query_id_value)
        weight = int(source.get("source_row_count", 1))
        record = QueryRecord(
            query_id=query_id,
            query_text=str(source["query_text"]),
            query_text_template=str(source["query_text_template"]),
            source_row_count=weight,
        )
        result = analyzer.analyze_record(record)
        row_errors = [error.to_dict() for error in result.errors]
        template_rows = [_occurrence_row(item) for item in result.occurrences]
        row_details = list(template_rows)
        parse_failed = any(error["stage"] == "parse" for error in row_errors)
        if not parse_failed and (include_original_literals or include_null_checks):
            try:
                predicate_usages = analyzer.analyze_predicate_usages(
                    record.query_text,
                    include_literals=include_original_literals,
                    include_null_checks=include_null_checks,
                )
            except Exception as error:
                row_errors.append(
                    {
                        "query_id": query_id,
                        "stage": "predicate",
                        "error_type": type(error).__name__,
                        "message": str(error).splitlines()[0][:240],
                        "sql_fragment": " ".join(record.query_text.split())[:240],
                    }
                )
            else:
                template_signatures = {
                    _dedupe_signature(row) for row in template_rows
                }
                for usage in predicate_usages:
                    row = _predicate_row(usage, record=record)
                    if (
                        usage.origin == "original_literal"
                        and _dedupe_signature(row) in template_signatures
                    ):
                        continue
                    row_details.append(row)

        status_counts = Counter(
            str(row["lineage_status"]) for row in row_details
        )
        if parse_failed and not row_details:
            analysis_status = "error"
        elif row_errors or any(status != "resolved" for status in status_counts):
            analysis_status = "partial"
        else:
            analysis_status = "ok"
        row_records.append(
            {
                **source,
                "analysis": row_details,
                "analysis_count": len(row_details),
                "resolved_count": status_counts["resolved"],
                "multi_source_count": status_counts["multi_source"],
                "ambiguous_count": status_counts["ambiguous"],
                "unresolved_count": status_counts["unresolved"],
                "analysis_status": analysis_status,
                "analysis_errors": row_errors,
            }
        )
        detail_rows.extend(row_details)
        error_rows.extend(row_errors)

    row_analysis_df = pd.DataFrame(row_records, index=queries_df.index.copy())
    details_df = pd.DataFrame(detail_rows, columns=DETAIL_COLUMNS)
    errors_df = pd.DataFrame(error_rows, columns=ERROR_COLUMNS)
    aggregate_rows = _aggregate_rows(detail_rows, example_limit=example_limit)
    aggregate_df = pd.DataFrame(aggregate_rows, columns=AGGREGATE_COLUMNS)
    report = build_catalog_report_from_details(
        detail_rows,
        schema,
        source_label=source_label,
        dialect=dialect,
        top_limit=top_limit,
        example_limit=example_limit,
    )
    catalog_payload = report.to_dict()
    catalog_columns_df = pd.DataFrame(report.column_rows())
    catalog_tables_df = pd.DataFrame(
        [
            {key: value for key, value in table.items() if key != "columns"}
            for table in catalog_payload["tables"]
        ]
    )
    artifact_paths: dict[str, Path] = {}
    if output_dir is not None:
        artifact_paths = _write_artifacts(
            output_dir=Path(output_dir),
            row_analysis_df=row_analysis_df,
            details_df=details_df,
            errors_df=errors_df,
            aggregate_df=aggregate_df,
            report=report,
            schema=schema,
            build_html=build_html,
        )
    return DataFrameAnalysis(
        row_analysis_df=row_analysis_df,
        aggregate_df=aggregate_df,
        details_df=details_df,
        errors_df=errors_df,
        catalog_columns_df=catalog_columns_df,
        catalog_tables_df=catalog_tables_df,
        catalog_report=catalog_payload,
        artifact_paths=artifact_paths,
    )
