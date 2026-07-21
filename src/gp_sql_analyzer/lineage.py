from __future__ import annotations

from collections.abc import Iterable

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope

from .models import ColumnRef, LineageResult
from .schema import SchemaProvider, TableRef


class LineageResolver:
    """Resolve SQLGlot columns to physical sources without guessing ambiguity."""

    def __init__(self, expression: exp.Expression, schema: SchemaProvider) -> None:
        root = build_scope(expression)
        if root is None:
            raise ValueError("statement has no query scope")
        self.root = root
        self.schema = schema
        self._column_scopes: dict[int, Scope] = {}
        for scope in root.traverse():
            for column in scope.columns:
                self._column_scopes[id(column)] = scope

    def resolve(self, column: exp.Column) -> LineageResult:
        scope = self._column_scopes.get(id(column), self.root)
        return self._resolve_column(column, scope, set()).normalized()

    def _resolve_column(
        self,
        column: exp.Column,
        scope: Scope | None,
        visited: set[tuple[int, str]],
    ) -> LineageResult:
        if scope is None:
            return LineageResult("unresolved", reason="column is outside a query scope")

        column_name = column.name.casefold()
        table_alias = column.table.casefold() if column.table else None
        if table_alias:
            search_scope: Scope | None = scope
            while search_scope is not None:
                selected = search_scope.selected_sources.get(table_alias)
                if selected is not None:
                    return self._resolve_source(selected[1], column_name, visited)
                search_scope = search_scope.parent
            return LineageResult(
                "unresolved", reason=f"source alias {table_alias!r} is not visible"
            )

        search_scope = scope
        while search_scope is not None:
            candidates: list[tuple[object, bool | None]] = []
            for _, source in search_scope.selected_sources.values():
                contains = self._source_contains(source, column_name)
                if contains is not False:
                    candidates.append((source, contains))

            known = [source for source, contains in candidates if contains is True]
            possible = known or [source for source, _ in candidates]
            if len(possible) == 1:
                return self._resolve_source(possible[0], column_name, visited)
            if len(possible) > 1:
                merged = self._merge(
                    (self._resolve_source(source, column_name, visited) for source in possible),
                    force_ambiguous=True,
                )
                return LineageResult(
                    "ambiguous",
                    merged.columns,
                    reason=f"unqualified column {column_name!r} matches multiple sources",
                ).normalized()
            search_scope = search_scope.parent

        return LineageResult("unresolved", reason=f"no source contains {column_name!r}")

    def _source_contains(self, source: object, column_name: str) -> bool | None:
        if isinstance(source, exp.Table):
            candidates = self.schema.resolve_table(
                source.name,
                schema=source.db or None,
                catalog=source.catalog or None,
            )
            answers = [self.schema.has_column(candidate, column_name) for candidate in candidates]
            if any(answer is True for answer in answers):
                return True
            if answers and all(answer is False for answer in answers):
                return False
            return None
        if isinstance(source, Scope):
            names = self._scope_output_names(source)
            if column_name in names:
                return True
            if "*" in names:
                return None
            return False
        return False

    def _resolve_source(
        self,
        source: object,
        column_name: str,
        visited: set[tuple[int, str]],
    ) -> LineageResult:
        if isinstance(source, exp.Table):
            return self._resolve_table(source, column_name)
        if isinstance(source, Scope):
            return self._resolve_scope_output(source, column_name, visited)
        return LineageResult("unresolved", reason="unsupported SQL source")

    def _resolve_table(self, table: exp.Table, column_name: str) -> LineageResult:
        candidates = self.schema.resolve_table(
            table.name,
            schema=table.db or None,
            catalog=table.catalog or None,
        )
        eligible = [
            candidate
            for candidate in candidates
            if self.schema.has_column(candidate, column_name) is not False
        ]
        columns = tuple(
            sorted(
                ColumnRef(candidate.catalog, candidate.schema, candidate.table, column_name)
                for candidate in eligible
            )
        )
        if len(columns) == 1:
            return LineageResult("resolved", columns)
        if len(columns) > 1:
            return LineageResult(
                "ambiguous", columns, reason="physical table exists in multiple schemas"
            )
        return LineageResult(
            "unresolved", reason=f"table {table.sql()!r} has no column {column_name!r}"
        )

    def _resolve_scope_output(
        self,
        scope: Scope,
        column_name: str,
        visited: set[tuple[int, str]],
    ) -> LineageResult:
        visit_key = (id(scope), column_name)
        if visit_key in visited:
            return LineageResult("unresolved", reason="recursive lineage cycle")
        next_visited = visited | {visit_key}

        if isinstance(scope.expression, exp.SetOperation):
            if not scope.union_scopes:
                return LineageResult("unresolved", reason="set operation has no branches")
            output_index = self._output_index(scope.union_scopes[0], column_name)
            if output_index is None:
                return LineageResult(
                    "unresolved", reason=f"set operation does not export {column_name!r}"
                )
            return self._merge(
                self._resolve_projection(branch, output_index, next_visited)
                for branch in scope.union_scopes
            )

        if not isinstance(scope.expression, exp.Select):
            return LineageResult("unresolved", reason="source is not a SELECT")

        output_index = self._output_index(scope, column_name)
        if output_index is None:
            stars = [
                projection
                for projection in scope.expression.expressions
                if projection.is_star
            ]
            if stars:
                results = []
                for star in stars:
                    table_alias = star.table if isinstance(star, exp.Column) else None
                    synthetic = exp.column(column_name, table=table_alias)
                    results.append(self._resolve_column(synthetic, scope, next_visited))
                return self._merge(results)
            return LineageResult("unresolved", reason=f"source does not export {column_name!r}")
        return self._resolve_projection(scope, output_index, next_visited)

    @staticmethod
    def _scope_output_names(scope: Scope) -> set[str]:
        if scope.outer_columns:
            return {name.casefold() for name in scope.outer_columns}
        expression = scope.expression
        if isinstance(expression, exp.SetOperation) and scope.union_scopes:
            return LineageResolver._scope_output_names(scope.union_scopes[0])
        if isinstance(expression, exp.Select):
            return {
                projection.alias_or_name.casefold()
                for projection in expression.expressions
                if projection.alias_or_name
            }
        return set()

    @staticmethod
    def _output_index(scope: Scope, column_name: str) -> int | None:
        if scope.outer_columns:
            for index, name in enumerate(scope.outer_columns):
                if name.casefold() == column_name:
                    return index
            return None
        expression = scope.expression
        if not isinstance(expression, exp.Select):
            return None
        for index, projection in enumerate(expression.expressions):
            if projection.alias_or_name.casefold() == column_name:
                return index
        return None

    def _resolve_projection(
        self,
        scope: Scope,
        output_index: int,
        visited: set[tuple[int, str]],
    ) -> LineageResult:
        expression = scope.expression
        if not isinstance(expression, exp.Select) or output_index >= len(expression.expressions):
            return LineageResult("unresolved", reason="missing set-operation projection")
        projection = expression.expressions[output_index]
        if isinstance(projection, exp.Star):
            return LineageResult("unresolved", reason="star projection needs catalog expansion")
        columns = list(projection.find_all(exp.Column))
        if not columns:
            return LineageResult("unresolved", reason="projection has no source column")
        return self._merge(self._resolve_column(column, scope, visited) for column in columns)

    @staticmethod
    def _merge(
        results: Iterable[LineageResult],
        *,
        force_ambiguous: bool = False,
    ) -> LineageResult:
        materialized = list(results)
        columns = tuple(sorted({column for result in materialized for column in result.columns}))
        if force_ambiguous or any(result.status == "ambiguous" for result in materialized):
            status = "ambiguous"
        elif len(columns) > 1:
            status = "multi_source"
        elif len(columns) == 1:
            status = "resolved"
        else:
            status = "unresolved"
        reasons = sorted({result.reason for result in materialized if result.reason})
        return LineageResult(status, columns, "; ".join(reasons) or None).normalized()
