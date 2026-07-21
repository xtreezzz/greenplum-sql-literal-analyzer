from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .complexity import CorpusComplexity
from .models import ColumnRef, LineageResult
from .patterns import classify_pattern
from .schema import MappingSchemaProvider, TableRef


FORMAT_VERSION = "1.0"
DEFAULT_EXAMPLE_LIMIT = 5
DEFAULT_TOP_LIMIT = 20


def _ordered_counts(counter: Counter[str]) -> dict[str, int]:
    return {
        key: value
        for key, value in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    }


def _generated_at(value: str | None) -> str:
    if value is not None:
        return value
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


@dataclass(frozen=True, slots=True)
class _EventValue:
    value: str
    raw_literal: str
    pattern_template: str | None
    pattern_family: str
    pattern_format: str
    regex_features: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class _UsageEvent:
    query_id: str
    source_row_count: int
    lineage: LineageResult
    clause_context: str
    operator: str
    value_role: str
    values: tuple[_EventValue, ...]
    condition_count: int = 1
    literal_count: int = 1


@dataclass(slots=True)
class _ValueAccumulator:
    value: str
    pattern_family: str
    pattern_format: str
    pattern_template: str | None
    regex_features: dict[str, Any]
    condition_count: int = 0
    source_row_count: int = 0
    query_ids: set[str] = field(default_factory=set)
    raw_examples: set[str] = field(default_factory=set)
    contexts: Counter[str] = field(default_factory=Counter)
    operators: Counter[str] = field(default_factory=Counter)

    def to_dict(self, *, example_limit: int) -> dict[str, Any]:
        return {
            "value": self.value,
            "pattern_family": self.pattern_family,
            "pattern_format": self.pattern_format,
            "pattern_template": self.pattern_template,
            "regex_features": dict(sorted(self.regex_features.items())),
            "condition_count": self.condition_count,
            "source_row_count": self.source_row_count,
            "distinct_query_count": len(self.query_ids),
            "context_counts": _ordered_counts(self.contexts),
            "operator_counts": _ordered_counts(self.operators),
            "raw_examples": sorted(self.raw_examples)[:example_limit],
            "example_query_ids": sorted(self.query_ids)[:example_limit],
        }


@dataclass(slots=True)
class _ColumnAccumulator:
    ref: ColumnRef
    condition_count: int = 0
    literal_count: int = 0
    source_row_count: int = 0
    weighted_literal_count: int = 0
    query_ids: set[str] = field(default_factory=set)
    contexts: Counter[str] = field(default_factory=Counter)
    operators: Counter[str] = field(default_factory=Counter)
    pattern_families: Counter[str] = field(default_factory=Counter)
    pattern_formats: Counter[str] = field(default_factory=Counter)
    values: dict[tuple[str, str, str, str | None], _ValueAccumulator] = field(
        default_factory=dict
    )

    def add(self, event: _UsageEvent) -> None:
        weighted_conditions = event.condition_count * event.source_row_count
        self.condition_count += event.condition_count
        self.literal_count += event.literal_count
        self.source_row_count += weighted_conditions
        self.weighted_literal_count += event.literal_count * event.source_row_count
        self.query_ids.add(event.query_id)
        self.contexts[event.clause_context] += weighted_conditions
        self.operators[event.operator] += weighted_conditions
        for value in event.values:
            self.pattern_families[value.pattern_family] += weighted_conditions
            self.pattern_formats[value.pattern_format] += weighted_conditions
            key = (
                value.value,
                value.pattern_family,
                value.pattern_format,
                value.pattern_template,
            )
            accumulator = self.values.get(key)
            if accumulator is None:
                accumulator = _ValueAccumulator(
                    value=value.value,
                    pattern_family=value.pattern_family,
                    pattern_format=value.pattern_format,
                    pattern_template=value.pattern_template,
                    regex_features=dict(value.regex_features),
                )
                self.values[key] = accumulator
            accumulator.condition_count += event.condition_count
            accumulator.source_row_count += weighted_conditions
            accumulator.query_ids.add(event.query_id)
            accumulator.raw_examples.add(value.raw_literal)
            accumulator.contexts[event.clause_context] += weighted_conditions
            accumulator.operators[event.operator] += weighted_conditions

    def value_rows(self, *, top_limit: int, example_limit: int) -> list[dict[str, Any]]:
        values = sorted(
            self.values.values(),
            key=lambda item: (
                -item.source_row_count,
                -item.condition_count,
                item.value,
                item.pattern_family,
                item.pattern_format,
            ),
        )
        return [item.to_dict(example_limit=example_limit) for item in values[:top_limit]]

    def to_dict(self, *, top_limit: int, example_limit: int) -> dict[str, Any]:
        top_values = self.value_rows(top_limit=top_limit, example_limit=example_limit)
        return {
            "catalog_name": self.ref.catalog,
            "schema_name": self.ref.schema,
            "table_name": self.ref.table,
            "column_name": self.ref.column,
            "qualified_name": self.ref.qualified_name,
            "usage_status": "active" if self.condition_count else "unused",
            "condition_count": self.condition_count,
            "literal_count": self.literal_count,
            "source_row_count": self.source_row_count,
            "weighted_literal_count": self.weighted_literal_count,
            "distinct_query_count": len(self.query_ids),
            "context_counts": _ordered_counts(self.contexts),
            "operator_counts": _ordered_counts(self.operators),
            "pattern_family_counts": _ordered_counts(self.pattern_families),
            "pattern_format_counts": _ordered_counts(self.pattern_formats),
            "top_values": top_values,
            "top_patterns": [
                row for row in top_values if row["pattern_family"] != "exact_value"
            ],
            "example_query_ids": sorted(self.query_ids)[:example_limit],
        }


@dataclass(slots=True)
class _QualityAccumulator:
    status: str
    columns: tuple[str, ...]
    reason: str | None
    context: str
    operator: str
    value_role: str
    values: tuple[str, ...]
    condition_count: int = 0
    literal_count: int = 0
    source_row_count: int = 0
    query_ids: set[str] = field(default_factory=set)

    def add(self, event: _UsageEvent) -> None:
        self.condition_count += event.condition_count
        self.literal_count += event.literal_count
        self.source_row_count += event.condition_count * event.source_row_count
        self.query_ids.add(event.query_id)

    def to_dict(self, *, example_limit: int) -> dict[str, Any]:
        return {
            "lineage_status": self.status,
            "base_columns": list(self.columns),
            "lineage_reason": self.reason,
            "clause_context": self.context,
            "operator_or_function": self.operator,
            "value_role": self.value_role,
            "values": list(self.values),
            "condition_count": self.condition_count,
            "literal_count": self.literal_count,
            "source_row_count": self.source_row_count,
            "distinct_query_count": len(self.query_ids),
            "example_query_ids": sorted(self.query_ids)[:example_limit],
        }


@dataclass(frozen=True, slots=True)
class CatalogReport:
    metadata: Mapping[str, Any]
    summary: Mapping[str, Any]
    tables: tuple[Mapping[str, Any], ...]
    quality: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "metadata": dict(self.metadata),
            "summary": dict(self.summary),
            "tables": [dict(table) for table in self.tables],
            "quality": dict(self.quality),
        }

    def column_rows(self) -> list[dict[str, Any]]:
        return [
            dict(column)
            for table in self.tables
            for column in table.get("columns", [])
        ]


def _column_from_qualified_name(
    value: str, *, default_schema: str | None
) -> ColumnRef | None:
    parts = [part.casefold() for part in value.split(".") if part]
    if len(parts) >= 4:
        return ColumnRef(parts[-4], parts[-3], parts[-2], parts[-1])
    if len(parts) == 3:
        return ColumnRef(None, parts[0], parts[1], parts[2])
    if len(parts) == 2:
        return ColumnRef(None, default_schema, parts[0], parts[1])
    return None


def _inventory(schema: MappingSchemaProvider) -> dict[ColumnRef, _ColumnAccumulator]:
    accumulators: dict[ColumnRef, _ColumnAccumulator] = {}
    for table in schema.tables:
        for column in sorted(table.columns or ()):
            ref = ColumnRef(table.catalog, table.schema, table.table, column)
            accumulators[ref] = _ColumnAccumulator(ref)
    return accumulators


def _build_report(
    events: Iterable[_UsageEvent],
    schema: MappingSchemaProvider,
    *,
    source_label: str,
    source_commit: str | None,
    dialect: str,
    query_count: int | None,
    parsed_query_count: int | None,
    generated_at: str | None,
    top_limit: int,
    example_limit: int,
) -> CatalogReport:
    columns = _inventory(schema)
    quality_groups: dict[tuple[Any, ...], _QualityAccumulator] = {}
    status_counts: Counter[str] = Counter()
    seen_query_ids: set[str] = set()

    for event in events:
        seen_query_ids.add(event.query_id)
        normalized = event.lineage.normalized()
        weighted_conditions = event.condition_count * event.source_row_count
        status_counts[normalized.status] += weighted_conditions
        if normalized.status == "resolved" and len(normalized.columns) == 1:
            ref = normalized.columns[0]
            accumulator = columns.get(ref)
            if accumulator is None:
                accumulator = _ColumnAccumulator(ref)
                columns[ref] = accumulator
            accumulator.add(event)
            continue

        column_names = tuple(column.qualified_name for column in normalized.columns)
        values = tuple(value.value for value in event.values)
        key = (
            normalized.status,
            column_names,
            normalized.reason,
            event.clause_context,
            event.operator,
            event.value_role,
            values,
        )
        group = quality_groups.get(key)
        if group is None:
            group = _QualityAccumulator(
                status=normalized.status,
                columns=column_names,
                reason=normalized.reason,
                context=event.clause_context,
                operator=event.operator,
                value_role=event.value_role,
                values=values,
            )
            quality_groups[key] = group
        group.add(event)

    table_accumulators: dict[tuple[str | None, str | None, str], list[_ColumnAccumulator]] = {}
    for accumulator in columns.values():
        key = (accumulator.ref.catalog, accumulator.ref.schema, accumulator.ref.table)
        table_accumulators.setdefault(key, []).append(accumulator)

    table_rows: list[dict[str, Any]] = []
    for (catalog, schema_name, table_name), table_columns in sorted(
        table_accumulators.items(),
        key=lambda item: tuple(part or "" for part in item[0]),
    ):
        table_columns.sort(key=lambda item: item.ref.column)
        table_queries = {query for column in table_columns for query in column.query_ids}
        contexts: Counter[str] = Counter()
        operators: Counter[str] = Counter()
        families: Counter[str] = Counter()
        for column in table_columns:
            contexts.update(column.contexts)
            operators.update(column.operators)
            families.update(column.pattern_families)
        qualified_name = ".".join(
            part for part in (catalog, schema_name, table_name) if part
        )
        table_rows.append(
            {
                "catalog_name": catalog,
                "schema_name": schema_name,
                "table_name": table_name,
                "qualified_name": qualified_name,
                "column_count": len(table_columns),
                "active_column_count": sum(
                    column.condition_count > 0 for column in table_columns
                ),
                "condition_count": sum(column.condition_count for column in table_columns),
                "literal_count": sum(column.literal_count for column in table_columns),
                "source_row_count": sum(
                    column.source_row_count for column in table_columns
                ),
                "distinct_query_count": len(table_queries),
                "context_counts": _ordered_counts(contexts),
                "operator_counts": _ordered_counts(operators),
                "pattern_family_counts": _ordered_counts(families),
                "columns": [
                    column.to_dict(top_limit=top_limit, example_limit=example_limit)
                    for column in table_columns
                ],
            }
        )

    quality_rows = [
        group.to_dict(example_limit=example_limit)
        for group in sorted(
            quality_groups.values(),
            key=lambda group: (
                -group.source_row_count,
                group.status,
                group.columns,
                group.context,
                group.operator,
                group.values,
            ),
        )
    ]
    total_columns = len(columns)
    active_columns = sum(column.condition_count > 0 for column in columns.values())
    resolved_source_rows = status_counts.get("resolved", 0)
    all_source_rows = sum(status_counts.values())
    quality_source_rows = all_source_rows - resolved_source_rows
    summary = {
        "query_count": query_count if query_count is not None else len(seen_query_ids),
        "parsed_query_count": (
            parsed_query_count if parsed_query_count is not None else len(seen_query_ids)
        ),
        "table_count": len(table_rows),
        "column_count": total_columns,
        "active_column_count": active_columns,
        "unused_column_count": total_columns - active_columns,
        "resolved_condition_count": sum(
            column.condition_count for column in columns.values()
        ),
        "resolved_literal_count": sum(column.literal_count for column in columns.values()),
        "resolved_source_row_count": resolved_source_rows,
        "quality_source_row_count": quality_source_rows,
        "lineage_status_counts": _ordered_counts(status_counts),
        "lineage_resolution_rate": (
            resolved_source_rows / all_source_rows if all_source_rows else 1.0
        ),
    }
    quality = {
        "group_count": len(quality_rows),
        "source_row_count": quality_source_rows,
        "groups": quality_rows,
    }
    metadata = {
        "format_version": FORMAT_VERSION,
        "source_label": source_label,
        "source_commit": source_commit,
        "dialect": dialect,
        "generated_at": _generated_at(generated_at),
        "top_limit": top_limit,
        "example_limit": example_limit,
    }
    return CatalogReport(metadata, summary, tuple(table_rows), quality)


def _corpus_events(corpus: CorpusComplexity) -> Iterable[_UsageEvent]:
    for query in corpus.queries:
        for usage in query.literal_usages:
            values: list[_EventValue] = []
            for raw_value in usage.values:
                pattern = classify_pattern(usage.operator_or_function, raw_value)
                family = pattern.family
                pattern_format = pattern.format_signature
                regex_features = pattern.regex_features
                if len(usage.pattern_families) == 1:
                    family = usage.pattern_families[0]
                if len(usage.pattern_formats) == 1:
                    pattern_format = usage.pattern_formats[0]
                if family in {"regex", "similar_to"} and not regex_features:
                    classifier = (
                        "SIMILAR TO"
                        if family == "similar_to"
                        else (
                            "REGEXP_I"
                            if "case_insensitive" in pattern_format
                            else "REGEXP"
                        )
                    )
                    regex_features = classify_pattern(
                        classifier, raw_value
                    ).regex_features
                values.append(
                    _EventValue(
                        value=raw_value,
                        raw_literal=raw_value,
                        pattern_template=None,
                        pattern_family=family,
                        pattern_format=pattern_format,
                        regex_features=regex_features,
                    )
                )
            yield _UsageEvent(
                query_id=query.name,
                source_row_count=1,
                lineage=usage.lineage,
                clause_context=usage.clause_context,
                operator=usage.operator_or_function,
                value_role=usage.value_role,
                values=tuple(values),
                condition_count=usage.condition_count,
                literal_count=usage.literal_count,
            )


def build_catalog_report(
    corpus: CorpusComplexity,
    schema: MappingSchemaProvider,
    *,
    generated_at: str | None = None,
    top_limit: int = DEFAULT_TOP_LIMIT,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> CatalogReport:
    return _build_report(
        _corpus_events(corpus),
        schema,
        source_label=corpus.source_label,
        source_commit=corpus.source_commit,
        dialect=corpus.dialect,
        query_count=corpus.files_seen,
        parsed_query_count=corpus.files_parsed,
        generated_at=generated_at,
        top_limit=top_limit,
        example_limit=example_limit,
    )


def _detail_events(
    rows: Iterable[Mapping[str, Any]], schema: MappingSchemaProvider
) -> Iterable[_UsageEvent]:
    for index, row in enumerate(rows):
        column_names = list(row.get("base_columns") or [])
        if not column_names and row.get("table_name") and row.get("column_name"):
            parts = [
                row.get("schema_name") or schema.default_schema,
                row.get("table_name"),
                row.get("column_name"),
            ]
            column_names = [".".join(str(part) for part in parts if part)]
        columns = tuple(
            column
            for value in column_names
            if (column := _column_from_qualified_name(
                str(value), default_schema=schema.default_schema
            ))
            is not None
        )
        status = str(row.get("lineage_status") or "unresolved")
        lineage = LineageResult(
            status,  # type: ignore[arg-type]
            columns,
            str(row["lineage_reason"]) if row.get("lineage_reason") else None,
        ).normalized()
        raw_literal = str(row.get("raw_literal") or "")
        extracted_value = str(row.get("extracted_value") or raw_literal)
        operator = str(row.get("operator_or_function") or "UNKNOWN")
        pattern = classify_pattern(operator, raw_literal)
        family = str(row.get("pattern_family") or pattern.family)
        pattern_format = str(row.get("pattern_format") or pattern.format_signature)
        regex_features = row.get("regex_features")
        if not isinstance(regex_features, Mapping):
            regex_features = pattern.regex_features
        yield _UsageEvent(
            query_id=str(row.get("query_id") or f"row-{index + 1}"),
            source_row_count=max(1, int(row.get("source_row_count") or 1)),
            lineage=lineage,
            clause_context=str(row.get("clause_context") or "OTHER"),
            operator=operator,
            value_role=str(row.get("value_role") or "value"),
            values=(
                _EventValue(
                    value=extracted_value,
                    raw_literal=raw_literal,
                    pattern_template=(
                        str(row["pattern_template"])
                        if row.get("pattern_template") is not None
                        else None
                    ),
                    pattern_family=family,
                    pattern_format=pattern_format,
                    regex_features=dict(regex_features),
                ),
            ),
        )


def build_catalog_report_from_details(
    rows: Iterable[Mapping[str, Any]],
    schema: MappingSchemaProvider,
    *,
    source_label: str = "details.jsonl",
    source_commit: str | None = None,
    dialect: str = "postgres",
    generated_at: str | None = None,
    top_limit: int = DEFAULT_TOP_LIMIT,
    example_limit: int = DEFAULT_EXAMPLE_LIMIT,
) -> CatalogReport:
    return _build_report(
        _detail_events(rows, schema),
        schema,
        source_label=source_label,
        source_commit=source_commit,
        dialect=dialect,
        query_count=None,
        parsed_query_count=None,
        generated_at=generated_at,
        top_limit=top_limit,
        example_limit=example_limit,
    )
