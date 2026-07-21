from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


LineageStatus = Literal["resolved", "multi_source", "ambiguous", "unresolved"]
PredicateOrigin = Literal["original_literal", "null_check"]


@dataclass(frozen=True, order=True, slots=True)
class ColumnRef:
    catalog: str | None
    schema: str | None
    table: str
    column: str

    @property
    def qualified_name(self) -> str:
        return ".".join(
            part for part in (self.catalog, self.schema, self.table, self.column) if part
        )


@dataclass(frozen=True, slots=True)
class LineageResult:
    status: LineageStatus
    columns: tuple[ColumnRef, ...] = ()
    reason: str | None = None

    def normalized(self) -> "LineageResult":
        columns = tuple(sorted(set(self.columns)))
        status: LineageStatus = self.status
        if status == "resolved" and len(columns) > 1:
            status = "multi_source"
        return LineageResult(status=status, columns=columns, reason=self.reason)


@dataclass(frozen=True, slots=True)
class QueryRecord:
    query_id: str
    query_text: str
    query_text_template: str
    source_row_count: int = 1


@dataclass(frozen=True, slots=True)
class Occurrence:
    query_id: str
    query_hash: str
    template_hash: str
    source_row_count: int
    lineage: LineageResult
    clause_context: str
    operator_or_function: str
    value_role: str
    raw_literal: str
    extracted_value: str
    pattern_template: str
    pattern_family: str
    pattern_format: str
    regex_features: dict[str, Any]
    ast_path: str

    def to_dict(self) -> dict[str, Any]:
        lineage = self.lineage.normalized()
        only = lineage.columns[0] if len(lineage.columns) == 1 else None
        return {
            "query_id": self.query_id,
            "query_hash": self.query_hash,
            "template_hash": self.template_hash,
            "source_row_count": self.source_row_count,
            "schema_name": only.schema if only else None,
            "table_name": only.table if only else None,
            "column_name": only.column if only else None,
            "base_columns": [column.qualified_name for column in lineage.columns],
            "lineage_status": lineage.status,
            "lineage_reason": lineage.reason,
            "clause_context": self.clause_context,
            "operator_or_function": self.operator_or_function,
            "value_role": self.value_role,
            "raw_literal": self.raw_literal,
            "extracted_value": self.extracted_value,
            "pattern_template": self.pattern_template,
            "pattern_family": self.pattern_family,
            "pattern_format": self.pattern_format,
            "regex_features": dict(sorted(self.regex_features.items())),
            "ast_path": self.ast_path,
        }


@dataclass(frozen=True, slots=True)
class LiteralUsage:
    lineage: LineageResult
    clause_context: str
    operator_or_function: str
    value_role: str
    values: tuple[str, ...]
    pattern_families: tuple[str, ...]
    pattern_formats: tuple[str, ...]
    condition_count: int
    literal_count: int


@dataclass(frozen=True, slots=True)
class PredicateUsage:
    lineage: LineageResult
    clause_context: str
    operator_or_function: str
    value_role: str
    raw_literal: str
    extracted_value: str
    pattern_template: str | None
    pattern_family: str
    pattern_format: str
    regex_features: dict[str, Any]
    ast_path: str
    origin: PredicateOrigin


@dataclass(frozen=True, slots=True)
class AnalysisError:
    query_id: str
    stage: str
    error_type: str
    message: str
    sql_fragment: str

    def to_dict(self) -> dict[str, str]:
        return {
            "query_id": self.query_id,
            "stage": self.stage,
            "error_type": self.error_type,
            "message": self.message,
            "sql_fragment": self.sql_fragment,
        }


@dataclass(slots=True)
class AnalysisResult:
    occurrences: list[Occurrence] = field(default_factory=list)
    errors: list[AnalysisError] = field(default_factory=list)
