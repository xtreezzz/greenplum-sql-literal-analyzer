from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

import sqlglot
from sqlglot import ErrorLevel, exp

from .lineage import LineageResolver
from .models import (
    AnalysisError,
    AnalysisResult,
    LiteralUsage,
    LineageResult,
    Occurrence,
    PredicateUsage,
    QueryRecord,
)
from .patterns import PatternInfo, classify_pattern
from .placeholders import DEFAULT_PLACEHOLDER, extract_placeholder_values
from .schema import SchemaProvider


@dataclass(frozen=True, slots=True)
class Operation:
    name: str
    subject: exp.Expression | None
    value_role: str
    classify_as: str | None = None


_BINARY_OPERATORS: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.EQ, "="),
    (exp.NEQ, "!="),
    (exp.GT, ">"),
    (exp.GTE, ">="),
    (exp.LT, "<"),
    (exp.LTE, "<="),
    (exp.Like, "LIKE"),
    (exp.ILike, "ILIKE"),
    (exp.SimilarTo, "SIMILAR TO"),
    (exp.RegexpLike, "~"),
    (exp.RegexpILike, "~*"),
)


class SQLAnalyzer:
    def __init__(
        self,
        schema: SchemaProvider,
        *,
        dialect: str = "postgres",
        placeholder: str = DEFAULT_PLACEHOLDER,
        template_cache_size: int = 2048,
    ) -> None:
        self.schema = schema
        self.dialect = dialect
        self.placeholder = placeholder
        self._parse_template = lru_cache(maxsize=template_cache_size)(self._parse_template_uncached)

    def analyze_record(self, record: QueryRecord) -> AnalysisResult:
        result = AnalysisResult()
        try:
            originals = self._parse(record.query_text)
            templates = self._parse_template(record.query_text_template)
        except Exception as error:
            result.errors.append(self._error(record, "parse", error, record.query_text))
            return result

        if len(originals) != len(templates):
            result.errors.append(
                AnalysisError(
                    query_id=record.query_id,
                    stage="align",
                    error_type="StatementCountMismatch",
                    message=f"original has {len(originals)} statements, template has {len(templates)}",
                    sql_fragment=self._safe_fragment(record.query_text_template),
                )
            )
            return result

        query_hash = self._hash(record.query_text)
        template_hash = self._hash(record.query_text_template)
        for statement_index, (original, template) in enumerate(zip(originals, templates)):
            try:
                resolver = LineageResolver(original, self.schema)
            except Exception as error:
                result.errors.append(self._error(record, "lineage", error, original.sql()))
                continue

            original_paths = dict(self._walk_paths(original, f"statement[{statement_index}]"))
            template_paths = list(self._walk_paths(template, f"statement[{statement_index}]"))
            for ast_path, template_node in template_paths:
                if not (
                    isinstance(template_node, exp.Literal)
                    and template_node.is_string
                    and self.placeholder in str(template_node.this)
                ):
                    continue

                original_node = original_paths.get(ast_path)
                if not isinstance(original_node, exp.Literal) or not original_node.is_string:
                    result.errors.append(
                        AnalysisError(
                            query_id=record.query_id,
                            stage="align",
                            error_type="AstShapeMismatch",
                            message=f"template literal has no original literal at {ast_path}",
                            sql_fragment=self._safe_fragment(template_node.sql()),
                        )
                    )
                    continue

                operation = self._find_operation(original_node)
                lineage = self._resolve_subject(operation.subject, resolver)
                context = self._clause_context(original_node)
                classification_operator = operation.classify_as or operation.name
                pattern: PatternInfo = classify_pattern(
                    classification_operator, str(original_node.this)
                )
                matches = extract_placeholder_values(
                    str(template_node.this),
                    str(original_node.this),
                    self.placeholder,
                )
                for placeholder_index, match in enumerate(matches):
                    result.occurrences.append(
                        Occurrence(
                            query_id=record.query_id,
                            query_hash=query_hash,
                            template_hash=template_hash,
                            source_row_count=record.source_row_count,
                            lineage=lineage,
                            clause_context=context,
                            operator_or_function=operation.name,
                            value_role=operation.value_role,
                            raw_literal=str(original_node.this),
                            extracted_value=match.value,
                            pattern_template=str(template_node.this),
                            pattern_family=pattern.family,
                            pattern_format=pattern.format_signature,
                            regex_features=pattern.regex_features,
                            ast_path=f"{ast_path}.placeholder[{placeholder_index}]",
                        )
                    )
        return result

    def analyze_records(self, records: Iterable[QueryRecord]) -> AnalysisResult:
        combined = AnalysisResult()
        for record in records:
            result = self.analyze_record(record)
            combined.occurrences.extend(result.occurrences)
            combined.errors.extend(result.errors)
        return combined

    def analyze_literal_usages(self, sql: str) -> tuple[LiteralUsage, ...]:
        """Group query literals by predicate, physical lineage and clause context."""
        condition_groups: dict[
            tuple[int, int, str, str],
            dict[str, object],
        ] = {}
        for statement_index, statement in enumerate(self._parse(sql)):
            resolver = LineageResolver(statement, self.schema)
            for literal in statement.find_all(exp.Literal):
                owner = self._literal_operation_owner(literal)
                if owner is None:
                    continue
                operation = self._find_operation(literal)
                value_role = (
                    "range" if isinstance(owner, exp.Between) else operation.value_role
                )
                key = (
                    statement_index,
                    id(owner),
                    operation.name,
                    value_role,
                )
                classification_operator = operation.classify_as or operation.name
                raw_value = str(literal.this)
                value = self._literal_display_value(literal, owner)
                pattern = classify_pattern(classification_operator, raw_value)
                group = condition_groups.get(key)
                if group is None:
                    group = {
                        "lineage": self._resolve_subject(operation.subject, resolver),
                        "context": self._clause_context(literal),
                        "operator": operation.name,
                        "role": value_role,
                        "values": [],
                        "families": [],
                        "formats": [],
                        "literal_count": 0,
                    }
                    condition_groups[key] = group
                values = group["values"]
                families = group["families"]
                formats = group["formats"]
                assert isinstance(values, list)
                assert isinstance(families, list)
                assert isinstance(formats, list)
                if value not in values:
                    values.append(value)
                if pattern.family not in families:
                    families.append(pattern.family)
                if pattern.format_signature not in formats:
                    formats.append(pattern.format_signature)
                group["literal_count"] = int(group["literal_count"]) + 1

        aggregated: dict[tuple[object, ...], LiteralUsage] = {}
        for group in condition_groups.values():
            lineage = group["lineage"]
            assert isinstance(lineage, LineageResult)
            values = tuple(str(value) for value in group["values"])
            families = tuple(str(value) for value in group["families"])
            formats = tuple(str(value) for value in group["formats"])
            signature = (
                lineage.status,
                lineage.columns,
                lineage.reason,
                group["context"],
                group["operator"],
                group["role"],
                values,
                families,
                formats,
            )
            existing = aggregated.get(signature)
            literal_count = int(group["literal_count"])
            if existing is None:
                aggregated[signature] = LiteralUsage(
                    lineage=lineage,
                    clause_context=str(group["context"]),
                    operator_or_function=str(group["operator"]),
                    value_role=str(group["role"]),
                    values=values,
                    pattern_families=families,
                    pattern_formats=formats,
                    condition_count=1,
                    literal_count=literal_count,
                )
            else:
                aggregated[signature] = LiteralUsage(
                    lineage=existing.lineage,
                    clause_context=existing.clause_context,
                    operator_or_function=existing.operator_or_function,
                    value_role=existing.value_role,
                    values=existing.values,
                    pattern_families=existing.pattern_families,
                    pattern_formats=existing.pattern_formats,
                    condition_count=existing.condition_count + 1,
                    literal_count=existing.literal_count + literal_count,
                )

        context_order = {
            "WHERE": 0,
            "JOIN_ON": 1,
            "HAVING": 2,
            "CASE": 3,
            "SELECT": 4,
            "OTHER": 5,
        }
        return tuple(
            sorted(
                aggregated.values(),
                key=lambda usage: (
                    0 if usage.lineage.status == "resolved" else 1,
                    context_order.get(usage.clause_context, 9),
                    tuple(column.qualified_name for column in usage.lineage.columns),
                    usage.operator_or_function,
                    usage.values,
                ),
            )
        )

    def analyze_predicate_usages(
        self,
        sql: str,
        *,
        include_literals: bool = True,
        include_null_checks: bool = True,
    ) -> tuple[PredicateUsage, ...]:
        """Return individual predicate values in source order.

        Unlike ``analyze_literal_usages``, this representation is intended for
        row-level output. Repeated AST literals that form one logical boundary
        expression (for example ``1200 + 11``) are emitted once.
        """
        usages: list[PredicateUsage] = []
        for statement_index, statement in enumerate(self._parse(sql)):
            resolver = LineageResolver(statement, self.schema)
            paths = {
                id(node): path
                for path, node in self._walk_paths(
                    statement, f"statement[{statement_index}]"
                )
            }
            seen: set[tuple[object, ...]] = set()
            if include_literals:
                for literal in statement.find_all(exp.Literal):
                    owner = self._literal_operation_owner(literal)
                    if owner is None:
                        continue
                    operation = self._find_operation(literal)
                    value_role = (
                        "range" if isinstance(owner, exp.Between) else operation.value_role
                    )
                    raw_value = str(literal.this)
                    display_value = self._predicate_display_value(literal, owner)
                    dedupe_key = (
                        id(owner),
                        operation.name,
                        value_role,
                        display_value,
                    )
                    if dedupe_key in seen:
                        continue
                    seen.add(dedupe_key)
                    classification_operator = operation.classify_as or operation.name
                    pattern = classify_pattern(classification_operator, raw_value)
                    usages.append(
                        PredicateUsage(
                            lineage=self._resolve_subject(operation.subject, resolver),
                            clause_context=self._clause_context(literal),
                            operator_or_function=operation.name,
                            value_role=value_role,
                            raw_literal=(
                                display_value
                                if display_value != raw_value
                                else raw_value
                            ),
                            extracted_value=display_value,
                            pattern_template=None,
                            pattern_family=pattern.family,
                            pattern_format=pattern.format_signature,
                            regex_features=pattern.regex_features,
                            ast_path=paths.get(id(literal), ""),
                            origin="original_literal",
                        )
                    )

            if include_null_checks:
                for null_predicate in statement.find_all(exp.Is):
                    if not isinstance(null_predicate.args.get("expression"), exp.Null):
                        continue
                    operator = self._negated_name(
                        null_predicate, "IS NULL", "IS NOT NULL"
                    )
                    subject = null_predicate.args.get("this")
                    if not isinstance(subject, exp.Expression):
                        subject = None
                    usages.append(
                        PredicateUsage(
                            lineage=self._resolve_subject(subject, resolver),
                            clause_context=self._clause_context(null_predicate),
                            operator_or_function=operator,
                            value_role="null_check",
                            raw_literal="NULL",
                            extracted_value="NULL",
                            pattern_template=None,
                            pattern_family="null_check",
                            pattern_format="null_check",
                            regex_features={},
                            ast_path=paths.get(id(null_predicate), ""),
                            origin="null_check",
                        )
                    )

        return tuple(sorted(usages, key=lambda usage: usage.ast_path))

    def _parse(self, sql: str) -> tuple[exp.Expression, ...]:
        return tuple(
            sqlglot.parse(sql, read=self.dialect, error_level=ErrorLevel.RAISE)
        )

    def _parse_template_uncached(self, sql: str) -> tuple[exp.Expression, ...]:
        return self._parse(sql)

    @staticmethod
    def _literal_operation_owner(literal: exp.Literal) -> exp.Expression | None:
        operation_types = tuple(expression_type for expression_type, _ in _BINARY_OPERATORS)
        node: exp.Expression = literal
        while node.parent is not None:
            node = node.parent
            if isinstance(
                node,
                operation_types
                + (exp.In, exp.Between, exp.RegexpReplace, exp.Substring),
            ):
                return node
            if isinstance(node, exp.Anonymous) and node.name.upper().startswith("REGEXP"):
                return node
            if isinstance(node, exp.Select):
                child: exp.Expression = literal
                while child.parent is not node and child.parent is not None:
                    child = child.parent
                return child if child in node.expressions else None
        return None

    def _literal_display_value(
        self,
        literal: exp.Literal,
        owner: exp.Expression,
    ) -> str:
        candidates: list[exp.Expression] = []
        if isinstance(owner, exp.Between):
            candidates.extend(
                expression
                for expression in (owner.args.get("low"), owner.args.get("high"))
                if isinstance(expression, exp.Expression)
            )
        elif isinstance(owner, exp.In):
            candidates.extend(owner.expressions)
        elif any(isinstance(owner, expression_type) for expression_type, _ in _BINARY_OPERATORS):
            candidates.extend(
                expression
                for expression in (
                    owner.args.get("this"),
                    owner.args.get("expression"),
                )
                if isinstance(expression, exp.Expression)
            )
        for expression in candidates:
            if not self._contains(expression, literal):
                continue
            if isinstance(expression, exp.Literal):
                return str(expression.this)
            return expression.sql(dialect=self.dialect)
        return str(literal.this)

    def _predicate_display_value(
        self,
        literal: exp.Literal,
        owner: exp.Expression,
    ) -> str:
        if any(isinstance(owner, expression_type) for expression_type, _ in _BINARY_OPERATORS):
            candidates = [
                expression
                for expression in (
                    owner.args.get("this"),
                    owner.args.get("expression"),
                )
                if isinstance(expression, exp.Expression)
                and self._contains(expression, literal)
            ]
            for candidate in candidates:
                subqueries = list(candidate.find_all(exp.Subquery))
                literal_is_inside_subquery = any(
                    self._contains(subquery, literal) for subquery in subqueries
                )
                if subqueries and not literal_is_inside_subquery:
                    return str(literal.this)
        return self._literal_display_value(literal, owner)

    @classmethod
    def _walk_paths(
        cls,
        expression: exp.Expression,
        path: str,
    ) -> Iterable[tuple[str, exp.Expression]]:
        yield path, expression
        keys = list(expression.args)
        if "with" in keys:
            keys.insert(0, keys.pop(keys.index("with")))
        for key in keys:
            value = expression.args[key]
            if isinstance(value, exp.Expression):
                yield from cls._walk_paths(value, f"{path}.{key}")
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    if isinstance(child, exp.Expression):
                        yield from cls._walk_paths(child, f"{path}.{key}[{index}]")

    @staticmethod
    def _contains(
        container: exp.Expression | list[exp.Expression] | None,
        target: exp.Expression,
    ) -> bool:
        if container is None:
            return False
        if isinstance(container, list):
            return any(SQLAnalyzer._contains(child, target) for child in container)
        return container is target or any(node is target for node in container.walk())

    def _find_operation(self, literal: exp.Literal) -> Operation:
        node: exp.Expression | None = literal.parent
        while node is not None:
            if isinstance(node, exp.In) and self._contains(node.args.get("expressions"), literal):
                return Operation(
                    self._negated_name(node, "IN", "NOT IN"), node.this, "list_value"
                )
            if isinstance(node, exp.Between):
                if self._contains(node.args.get("low"), literal):
                    return Operation("BETWEEN", node.this, "range_low", classify_as="=")
                if self._contains(node.args.get("high"), literal):
                    return Operation("BETWEEN", node.this, "range_high", classify_as="=")

            for expression_type, operator_name in _BINARY_OPERATORS:
                if isinstance(node, expression_type):
                    left = node.args.get("this")
                    right = node.args.get("expression")
                    if self._contains(right, literal):
                        subject = left
                    elif self._contains(left, literal):
                        subject = right
                    else:
                        continue
                    negative_name = {
                        "LIKE": "NOT LIKE",
                        "ILIKE": "NOT ILIKE",
                        "SIMILAR TO": "NOT SIMILAR TO",
                        "~": "!~",
                        "~*": "!~*",
                    }.get(operator_name, f"NOT {operator_name}")
                    role = "pattern" if operator_name in {"LIKE", "ILIKE", "SIMILAR TO", "~", "~*"} else "comparison_value"
                    return Operation(
                        self._negated_name(node, operator_name, negative_name), subject, role
                    )

            if isinstance(node, exp.RegexpReplace):
                if self._contains(node.args.get("expression"), literal):
                    modifiers = node.args.get("modifiers")
                    case_insensitive = isinstance(modifiers, exp.Literal) and "i" in str(
                        modifiers.this
                    ).casefold()
                    return Operation(
                        "REGEXP_REPLACE",
                        node.this,
                        "regex_pattern",
                        classify_as="REGEXP_I" if case_insensitive else None,
                    )
                if self._contains(node.args.get("replacement"), literal):
                    return Operation(
                        "REGEXP_REPLACE", node.this, "replacement_value", classify_as="="
                    )

            if isinstance(node, exp.Substring):
                if self._contains(node.args.get("start"), literal):
                    if literal.is_string:
                        return Operation(
                            "SUBSTRING_REGEX",
                            node.this,
                            "regex_pattern",
                            classify_as="REGEXP",
                        )
                    return Operation(
                        "SUBSTRING_START",
                        node.this,
                        "start_position",
                        classify_as="=",
                    )
                if self._contains(node.args.get("length"), literal):
                    return Operation(
                        "SUBSTRING_LENGTH",
                        node.this,
                        "length_value",
                        classify_as="=",
                    )

            if isinstance(node, exp.Anonymous) and node.name.upper().startswith("REGEXP"):
                arguments = list(node.expressions)
                subject = arguments[0] if arguments else None
                role = "regex_pattern" if len(arguments) > 1 and self._contains(arguments[1], literal) else "function_value"
                flags = arguments[2] if len(arguments) > 2 else None
                case_insensitive = isinstance(flags, exp.Literal) and "i" in str(
                    flags.this
                ).casefold()
                classify_as = (
                    "REGEXP_I" if role == "regex_pattern" and case_insensitive else None
                )
                if role != "regex_pattern":
                    classify_as = "="
                return Operation(node.name.upper(), subject, role, classify_as=classify_as)

            node = node.parent

        projection: exp.Expression = literal
        while projection.parent is not None and not isinstance(
            projection.parent, exp.Select
        ):
            projection = projection.parent
        if isinstance(projection.parent, exp.Select):
            return Operation("SELECT_LITERAL", projection, "literal", classify_as="=")
        return Operation("LITERAL", None, "literal", classify_as="=")

    @staticmethod
    def _negated_name(node: exp.Expression, positive: str, negative: str) -> str:
        parent = node.parent
        while isinstance(parent, exp.Paren):
            parent = parent.parent
        return negative if isinstance(parent, exp.Not) else positive

    @staticmethod
    def _clause_context(expression: exp.Expression) -> str:
        node = expression.parent
        while node is not None:
            if isinstance(node, exp.Case):
                return "CASE"
            if isinstance(node, exp.Where):
                return "WHERE"
            if isinstance(node, exp.Join):
                return "JOIN_ON"
            if isinstance(node, exp.Having):
                return "HAVING"
            if isinstance(node, exp.Select):
                return "SELECT"
            node = node.parent
        return "OTHER"

    @staticmethod
    def _resolve_subject(
        subject: exp.Expression | None,
        resolver: LineageResolver,
    ) -> LineageResult:
        if subject is None:
            return LineageResult("unresolved", reason="literal has no subject expression")
        columns = list(subject.find_all(exp.Column))
        if isinstance(subject, exp.Column) and subject not in columns:
            columns.insert(0, subject)
        if not columns:
            return LineageResult("unresolved", reason="subject expression has no columns")
        results = [resolver.resolve(column) for column in columns]
        base_columns = tuple(
            sorted({base_column for result in results for base_column in result.columns})
        )
        if any(result.status == "ambiguous" for result in results):
            status = "ambiguous"
        elif len(base_columns) > 1:
            status = "multi_source"
        elif len(base_columns) == 1:
            status = "resolved"
        else:
            status = "unresolved"
        reasons = sorted({result.reason for result in results if result.reason})
        return LineageResult(status, base_columns, "; ".join(reasons) or None).normalized()

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _safe_fragment(sql: str, limit: int = 240) -> str:
        compact = " ".join(sql.split())
        redacted: list[str] = []
        index = 0
        while index < len(compact):
            if compact[index] == "$":
                delimiter_end = compact.find("$", index + 1)
                if delimiter_end != -1:
                    tag = compact[index + 1 : delimiter_end]
                    valid_tag = not tag or (
                        (tag[0].isalpha() or tag[0] == "_")
                        and all(character.isalnum() or character == "_" for character in tag)
                    )
                    if valid_tag:
                        delimiter = compact[index : delimiter_end + 1]
                        closing = compact.find(delimiter, delimiter_end + 1)
                        redacted.append(f"{delimiter}…{delimiter}")
                        index = (
                            closing + len(delimiter) if closing != -1 else len(compact)
                        )
                        continue
            if compact[index] != "'":
                redacted.append(compact[index])
                index += 1
                continue
            redacted.append("'…'")
            index += 1
            while index < len(compact):
                if compact[index] != "'":
                    index += 1
                elif index + 1 < len(compact) and compact[index + 1] == "'":
                    index += 2
                else:
                    index += 1
                    break
        return "".join(redacted)[:limit]

    def _error(
        self,
        record: QueryRecord,
        stage: str,
        error: Exception,
        sql: str,
    ) -> AnalysisError:
        return AnalysisError(
            query_id=record.query_id,
            stage=stage,
            error_type=type(error).__name__,
            message=self._safe_fragment(str(error).splitlines()[0]),
            sql_fragment=self._safe_fragment(sql),
        )
