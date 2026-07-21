from __future__ import annotations

from pathlib import Path

import sqlglot
from sqlglot import ErrorLevel, exp

from .schema import MappingSchemaProvider


def load_ddl_schema(
    directory: Path,
    *,
    dialect: str = "postgres",
    default_schema: str = "public",
) -> MappingSchemaProvider:
    mapping: dict[str, dict[str, list[str]]] = {}
    for path in sorted(directory.rglob("*.sql")):
        statements = sqlglot.parse(
            path.read_text(encoding="utf-8"),
            read=dialect,
            error_level=ErrorLevel.RAISE,
        )
        for statement in statements:
            if not isinstance(statement, exp.Create) or not isinstance(
                statement.this, exp.Schema
            ):
                continue
            table = statement.this.this
            if not isinstance(table, exp.Table):
                continue
            schema_name = table.db or default_schema
            columns = [
                column.name
                for column in statement.this.expressions
                if isinstance(column, exp.ColumnDef)
            ]
            mapping.setdefault(schema_name, {})[table.name] = columns
    return MappingSchemaProvider(mapping, default_schema=default_schema)
