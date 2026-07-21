from __future__ import annotations

import os
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .models import QueryRecord
from .placeholders import DEFAULT_PLACEHOLDER
from .schema import MappingSchemaProvider


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")


class ConnectionLike(Protocol):
    def cursor(self, name: str | None = None): ...


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    dsn: str | None = None
    host: str | None = None
    port: int | None = None
    dbname: str | None = None
    user: str | None = None
    password: str | None = None
    sslmode: str | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "ConnectionConfig":
        values = os.environ if environ is None else environ
        port = int(values["GP_PORT"]) if values.get("GP_PORT") else None
        return cls(
            dsn=values.get("GP_DSN"),
            host=values.get("GP_HOST"),
            port=port,
            dbname=values.get("GP_DBNAME"),
            user=values.get("GP_USER"),
            password=values.get("GP_PASSWORD"),
            sslmode=values.get("GP_SSLMODE"),
        )

    def connect_kwargs(self) -> dict[str, object]:
        if self.dsn:
            return {"dsn": self.dsn}
        return {
            key: value
            for key, value in {
                "host": self.host,
                "port": self.port,
                "dbname": self.dbname,
                "user": self.user,
                "password": self.password,
                "sslmode": self.sslmode,
            }.items()
            if value is not None
        }


def connect_greenplum(environ: Mapping[str, str] | None = None):
    try:
        import psycopg2
    except ImportError as error:
        raise RuntimeError(
            "Greenplum mode requires the optional psycopg2-binary dependency"
        ) from error
    connection = psycopg2.connect(**ConnectionConfig.from_env(environ).connect_kwargs())
    connection.set_session(readonly=True, autocommit=False)
    return connection


def quote_identifier(identifier: str) -> str:
    if not _IDENTIFIER.fullmatch(identifier):
        raise ValueError(f"unsafe SQL identifier: {identifier!r}")
    return f'"{identifier}"'


def quote_qualified_name(name: str) -> str:
    parts = name.split(".")
    if not 1 <= len(parts) <= 3:
        raise ValueError("qualified name must contain one to three identifiers")
    return ".".join(quote_identifier(part) for part in parts)


@dataclass(frozen=True, slots=True)
class SourceQueryConfig:
    table: str
    query_column: str = "query_text"
    template_column: str = "query_text_template"
    id_column: str | None = None
    since_column: str | None = None
    since_value: Any | None = None
    min_id: Any | None = None
    max_id: Any | None = None
    limit: int | None = None
    preaggregate: bool = True
    placeholder: str = DEFAULT_PLACEHOLDER


def build_source_query(config: SourceQueryConfig) -> tuple[str, list[Any]]:
    table = quote_qualified_name(config.table)
    query_column = quote_identifier(config.query_column)
    template_column = quote_identifier(config.template_column)
    id_column = quote_identifier(config.id_column) if config.id_column else None

    if (config.since_column is None) != (config.since_value is None):
        raise ValueError("since_column and since_value must be provided together")
    if config.limit is not None and config.limit <= 0:
        raise ValueError("limit must be positive")
    if (config.min_id is not None or config.max_id is not None) and id_column is None:
        raise ValueError("id_column is required for min_id/max_id filters")

    fallback_id = f"md5(COALESCE({query_column}, '') || CHR(31) || COALESCE({template_column}, ''))"
    base_id = f"CAST({id_column} AS text)" if id_column else fallback_id
    query_id = f"MIN({base_id})" if config.preaggregate else base_id
    source_count = "COUNT(*)::bigint" if config.preaggregate else "1::bigint"

    predicates = [
        f"{query_column} IS NOT NULL",
        f"{template_column} IS NOT NULL",
        f"{template_column} LIKE %s",
    ]
    params: list[Any] = [f"%{config.placeholder}%"]
    if config.since_column is not None:
        predicates.append(f"{quote_identifier(config.since_column)} >= %s")
        params.append(config.since_value)
    if config.min_id is not None:
        predicates.append(f"{id_column} >= %s")
        params.append(config.min_id)
    if config.max_id is not None:
        predicates.append(f"{id_column} <= %s")
        params.append(config.max_id)

    sql = (
        f"SELECT {query_id} AS query_id, {query_column} AS query_text, "
        f"{template_column} AS query_text_template, {source_count} AS source_row_count "
        f"FROM {table} WHERE " + " AND ".join(predicates)
    )
    if config.preaggregate:
        sql += f" GROUP BY {query_column}, {template_column}"
    if config.limit is not None:
        sql += " ORDER BY query_id LIMIT %s"
        params.append(config.limit)
    return sql, params


def iter_greenplum_records(
    connection: ConnectionLike,
    config: SourceQueryConfig,
    *,
    batch_size: int,
) -> Iterator[list[QueryRecord]]:
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    sql, params = build_source_query(config)
    with connection.cursor(name="gp_sql_analyzer_source") as cursor:
        cursor.itersize = batch_size
        cursor.execute(sql, params)
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            yield [
                QueryRecord(
                    query_id=str(row[0]),
                    query_text=str(row[1]),
                    query_text_template=str(row[2]),
                    source_row_count=int(row[3]),
                )
                for row in rows
            ]


def load_catalog_schema(
    connection: ConnectionLike,
    *,
    schemas: Sequence[str] | None = None,
    default_schema: str | None = None,
) -> MappingSchemaProvider:
    sql = """
        SELECT current_database(), n.nspname, c.relname, a.attname
        FROM pg_catalog.pg_class AS c
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
        WHERE c.relkind IN ('r', 'v', 'm', 'f')
          AND a.attnum > 0
          AND NOT a.attisdropped
    """
    schema_filter = list(schemas) if schemas else None
    params: tuple[object, ...] = ()
    if schema_filter is not None:
        sql += " AND n.nspname = ANY(%s)"
        params = (schema_filter,)
    sql += " ORDER BY n.nspname, c.relname, a.attnum"
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        rows = cursor.fetchall()

    mapping: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    catalogs: set[str] = set()
    for catalog, schema, table, column in rows:
        catalogs.add(str(catalog))
        mapping[str(schema)][str(table)].append(str(column))
    catalog_name = next(iter(catalogs)) if len(catalogs) == 1 else None
    return MappingSchemaProvider(
        mapping,
        default_schema=default_schema,
        catalog=catalog_name,
    )
