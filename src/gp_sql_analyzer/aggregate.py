from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .models import Occurrence


@dataclass(slots=True)
class _Group:
    source_row_count: int = 0
    occurrence_count: int = 0
    query_hashes: set[str] = field(default_factory=set)
    template_hashes: set[str] = field(default_factory=set)
    query_ids: set[str] = field(default_factory=set)


class UsageAggregator:
    def __init__(self, *, example_limit: int = 3) -> None:
        if example_limit < 0:
            raise ValueError("example_limit must be non-negative")
        self.example_limit = example_limit
        self._groups: dict[tuple[Any, ...], _Group] = {}
        self._column_totals: dict[tuple[str, ...], int] = {}

    @staticmethod
    def _key(occurrence: Occurrence) -> tuple[Any, ...]:
        columns = tuple(
            column.qualified_name for column in occurrence.lineage.normalized().columns
        )
        features = json.dumps(
            occurrence.regex_features,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            columns,
            occurrence.lineage.status,
            occurrence.clause_context,
            occurrence.operator_or_function,
            occurrence.value_role,
            occurrence.pattern_family,
            occurrence.pattern_format,
            occurrence.pattern_template,
            occurrence.raw_literal,
            occurrence.extracted_value,
            features,
        )

    def add(self, occurrence: Occurrence) -> None:
        key = self._key(occurrence)
        group = self._groups.setdefault(key, _Group())
        group.source_row_count += occurrence.source_row_count
        group.occurrence_count += 1
        group.query_hashes.add(occurrence.query_hash)
        group.template_hashes.add(occurrence.template_hash)
        group.query_ids.add(occurrence.query_id)
        columns = key[0]
        self._column_totals[columns] = (
            self._column_totals.get(columns, 0) + occurrence.source_row_count
        )

    def rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for key, group in sorted(self._groups.items()):
            (
                columns,
                lineage_status,
                context,
                operator,
                value_role,
                family,
                pattern_format,
                pattern_template,
                raw_literal,
                extracted_value,
                features_json,
            ) = key
            total = self._column_totals[columns]
            rows.append(
                {
                    "base_columns": list(columns),
                    "lineage_status": lineage_status,
                    "clause_context": context,
                    "operator_or_function": operator,
                    "value_role": value_role,
                    "pattern_family": family,
                    "pattern_format": pattern_format,
                    "pattern_template": pattern_template,
                    "raw_literal": raw_literal,
                    "extracted_value": extracted_value,
                    "regex_features": json.loads(features_json),
                    "source_row_count": group.source_row_count,
                    "occurrence_count": group.occurrence_count,
                    "distinct_query_count": len(group.query_hashes),
                    "distinct_template_count": len(group.template_hashes),
                    "share_of_column": group.source_row_count / total if total else 0.0,
                    "example_query_ids": sorted(group.query_ids)[: self.example_limit],
                }
            )
        return rows
