from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

import sqlglot
from sqlglot import ErrorLevel, exp

from .analyzer import SQLAnalyzer
from .benchmarks import DUCKDB_COMMIT
from .ddl_schema import load_ddl_schema
from .models import LiteralUsage
from .schema import MappingSchemaProvider, SchemaProvider


TIER_LABELS = {
    "extreme": "Экстремальная",
    "high": "Высокая",
    "medium": "Средняя",
    "basic": "Базовая",
    "error": "Ошибка разбора",
}


@dataclass(frozen=True)
class QueryComplexity:
    name: str
    sql: str
    parsed: bool
    error: str | None = None
    statement_count: int = 0
    statement_types: tuple[str, ...] = ()
    node_count: int = 0
    max_depth: int = 0
    cte_count: int = 0
    cte_names: tuple[str, ...] = ()
    subquery_count: int = 0
    max_subquery_depth: int = 0
    set_operation_count: int = 0
    set_operation_types: tuple[str, ...] = ()
    join_count: int = 0
    join_types: tuple[str, ...] = ()
    window_count: int = 0
    window_functions: tuple[str, ...] = ()
    case_count: int = 0
    select_count: int = 0
    table_reference_count: int = 0
    table_names: tuple[str, ...] = ()
    table_reference_counts: tuple[tuple[str, int], ...] = ()
    aggregate_functions: tuple[str, ...] = ()
    where_count: int = 0
    having_count: int = 0
    group_count: int = 0
    order_count: int = 0
    literal_usages: tuple[LiteralUsage, ...] = ()
    score: int = -1
    rank: int = 0
    tier: str = "error"

    @property
    def tier_label(self) -> str:
        return TIER_LABELS[self.tier]


@dataclass(frozen=True)
class CorpusComplexity:
    source_label: str
    source_commit: str
    dialect: str
    files_seen: int
    files_parsed: int
    errors: tuple[str, ...]
    queries: tuple[QueryComplexity, ...]
    aggregate_counts: tuple[tuple[str, int], ...]
    table_counts: tuple[tuple[str, int], ...]


def _unique(items: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for item in items if item))


def _expression_depth(expression: exp.Expression) -> int:
    children = list(expression.iter_expressions())
    if not children:
        return 1
    return 1 + max(_expression_depth(child) for child in children)


def _subquery_depth(expression: exp.Expression) -> int:
    own = 1 if isinstance(expression, exp.Subquery) else 0
    children = list(expression.iter_expressions())
    if not children:
        return own
    return own + max(_subquery_depth(child) for child in children)


def _set_operation_name(node: exp.SetOperation) -> str:
    name = node.key.upper()
    if isinstance(node, exp.Union) and node.args.get("distinct") is False:
        return "UNION ALL"
    return name


def _join_name(node: exp.Join) -> str:
    side = str(node.args.get("side") or "").upper()
    kind = str(node.args.get("kind") or "").upper()
    if kind == "CROSS":
        return "CROSS"
    if side:
        return f"{side} {kind or 'OUTER'}".strip()
    return kind or "INNER"


def _function_name(node: exp.Expression) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.upper()
    sql_name = getattr(node, "sql_name", None)
    if callable(sql_name):
        name = sql_name()
        if name:
            return str(name).upper()
    return node.key.upper()


def complexity_score(query: QueryComplexity) -> int:
    if not query.parsed:
        return -1
    return round(
        0.1 * query.node_count
        + 4.0 * query.cte_count
        + 6.0 * query.subquery_count
        + 6.0 * query.set_operation_count
        + 2.0 * query.join_count
        + 4.0 * query.window_count
        + 2.0 * query.case_count
        + 3.0 * query.max_depth
    )


def analyze_query(
    name: str,
    sql: str,
    *,
    dialect: str = "postgres",
    schema: SchemaProvider | None = None,
) -> QueryComplexity:
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
    except Exception as error:
        return QueryComplexity(
            name=name,
            sql=sql,
            parsed=False,
            error=f"{type(error).__name__}: {str(error).splitlines()[0]}",
        )

    nodes = [node for statement in statements for node in statement.walk()]
    cte_names = _unique(
        [node.alias_or_name for node in nodes if isinstance(node, exp.CTE)]
    )
    cte_name_set = {name.casefold() for name in cte_names}
    table_references = [node.name for node in nodes if isinstance(node, exp.Table)]
    base_tables = [
        table for table in table_references if table.casefold() not in cte_name_set
    ]
    table_counts = Counter(base_tables)
    windows = [node for node in nodes if isinstance(node, exp.Window)]
    aggregates = [node for node in nodes if isinstance(node, exp.AggFunc)]
    set_operations = [node for node in nodes if isinstance(node, exp.SetOperation)]
    joins = [node for node in nodes if isinstance(node, exp.Join)]
    schema_provider = schema or MappingSchemaProvider({}, default_schema=None)
    literal_usages = SQLAnalyzer(
        schema_provider,
        dialect=dialect,
    ).analyze_literal_usages(sql)
    query = QueryComplexity(
        name=name,
        sql=sql,
        parsed=True,
        statement_count=len(statements),
        statement_types=tuple(type(statement).__name__.upper() for statement in statements),
        node_count=len(nodes),
        max_depth=max((_expression_depth(statement) for statement in statements), default=0),
        cte_count=sum(isinstance(node, exp.CTE) for node in nodes),
        cte_names=cte_names,
        subquery_count=sum(isinstance(node, exp.Subquery) for node in nodes),
        max_subquery_depth=max(
            (_subquery_depth(statement) for statement in statements), default=0
        ),
        set_operation_count=len(set_operations),
        set_operation_types=tuple(_set_operation_name(node) for node in set_operations),
        join_count=len(joins),
        join_types=tuple(_join_name(node) for node in joins),
        window_count=len(windows),
        window_functions=_unique([_function_name(node.this) for node in windows]),
        case_count=sum(isinstance(node, exp.Case) for node in nodes),
        select_count=sum(isinstance(node, exp.Select) for node in nodes),
        table_reference_count=len(table_references),
        table_names=_unique(base_tables),
        table_reference_counts=tuple(table_counts.items()),
        aggregate_functions=_unique([_function_name(node) for node in aggregates]),
        where_count=sum(isinstance(node, exp.Where) for node in nodes),
        having_count=sum(isinstance(node, exp.Having) for node in nodes),
        group_count=sum(isinstance(node, exp.Group) for node in nodes),
        order_count=sum(isinstance(node, exp.Order) for node in nodes),
        literal_usages=literal_usages,
        score=0,
        tier="basic",
    )
    return replace(query, score=complexity_score(query))


def _tier_for_rank(rank: int, parsed: bool) -> str:
    if not parsed:
        return "error"
    if rank <= 10:
        return "extreme"
    if rank <= 30:
        return "high"
    if rank <= 60:
        return "medium"
    return "basic"


def analyze_corpus(
    directory: Path,
    *,
    dialect: str = "postgres",
    source_label: str = "TPC-DS",
    source_commit: str = DUCKDB_COMMIT,
    schema_directory: Path | None = None,
    default_schema: str = "tpcds",
) -> CorpusComplexity:
    files = sorted(directory.rglob("*.sql"))
    discovered_schema = schema_directory
    if discovered_schema is None and (directory.parent / "schema").is_dir():
        discovered_schema = directory.parent / "schema"
    schema: SchemaProvider
    if discovered_schema is not None:
        schema = load_ddl_schema(
            discovered_schema,
            dialect=dialect,
            default_schema=default_schema,
        )
    else:
        schema = MappingSchemaProvider({}, default_schema=default_schema)
    queries = [
        analyze_query(
            str(path.relative_to(directory)),
            path.read_text(encoding="utf-8"),
            dialect=dialect,
            schema=schema,
        )
        for path in files
    ]
    queries.sort(key=lambda query: (-query.score, query.name))
    ranked = tuple(
        replace(
            query,
            rank=rank,
            tier=_tier_for_rank(rank, query.parsed),
        )
        for rank, query in enumerate(queries, start=1)
    )
    counts: Counter[str] = Counter()
    tables: Counter[str] = Counter()
    for query in ranked:
        counts.update(
            {
                "CTE": query.cte_count,
                "Подзапросы": query.subquery_count,
                "Set operations": query.set_operation_count,
                "JOIN": query.join_count,
                "Окна": query.window_count,
                "CASE": query.case_count,
            }
        )
        tables.update(dict(query.table_reference_counts))
    errors = tuple(
        f"{query.name}: {query.error}" for query in ranked if query.error is not None
    )
    return CorpusComplexity(
        source_label=source_label,
        source_commit=source_commit,
        dialect=dialect,
        files_seen=len(files),
        files_parsed=sum(query.parsed for query in ranked),
        errors=errors,
        queries=ranked,
        aggregate_counts=tuple(counts.items()),
        table_counts=tuple(tables.most_common()),
    )
