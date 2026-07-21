from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol


def normalize_identifier(value: str | None) -> str | None:
    return value.casefold() if value else None


@dataclass(frozen=True, order=True, slots=True)
class TableRef:
    catalog: str | None
    schema: str | None
    table: str
    columns: frozenset[str] | None = None

    @property
    def qualified_name(self) -> str:
        return ".".join(part for part in (self.catalog, self.schema, self.table) if part)


class SchemaProvider(Protocol):
    default_schema: str | None

    def resolve_table(
        self,
        table: str,
        *,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[TableRef, ...]: ...

    def has_column(self, table: TableRef, column: str) -> bool | None: ...


class MappingSchemaProvider:
    """Cached schema snapshot backed by ``schema -> table -> columns`` mappings."""

    def __init__(
        self,
        mapping: Mapping[str, Mapping[str, Iterable[str]]],
        *,
        default_schema: str | None = None,
        catalog: str | None = None,
    ) -> None:
        self.default_schema = normalize_identifier(default_schema)
        self.catalog = normalize_identifier(catalog)
        self._tables: dict[tuple[str | None, str, str], TableRef] = {}
        for schema_name, tables in mapping.items():
            normalized_schema = normalize_identifier(schema_name)
            assert normalized_schema is not None
            for table_name, columns in tables.items():
                normalized_table = normalize_identifier(table_name)
                assert normalized_table is not None
                ref = TableRef(
                    catalog=self.catalog,
                    schema=normalized_schema,
                    table=normalized_table,
                    columns=frozenset(column.casefold() for column in columns),
                )
                self._tables[(self.catalog, normalized_schema, normalized_table)] = ref

    def resolve_table(
        self,
        table: str,
        *,
        schema: str | None = None,
        catalog: str | None = None,
    ) -> tuple[TableRef, ...]:
        normalized_table = table.casefold()
        normalized_schema = normalize_identifier(schema) or self.default_schema
        normalized_catalog = normalize_identifier(catalog) or self.catalog

        matches = [
            ref
            for (known_catalog, known_schema, known_table), ref in self._tables.items()
            if known_table == normalized_table
            and (normalized_schema is None or known_schema == normalized_schema)
            and (normalized_catalog is None or known_catalog == normalized_catalog)
        ]
        if matches:
            return tuple(sorted(matches))

        if normalized_schema is not None:
            return (
                TableRef(
                    catalog=normalized_catalog,
                    schema=normalized_schema,
                    table=normalized_table,
                    columns=None,
                ),
            )
        return ()

    def has_column(self, table: TableRef, column: str) -> bool | None:
        if table.columns is None:
            return None
        return column.casefold() in table.columns

    @property
    def tables(self) -> tuple[TableRef, ...]:
        """Return the complete, deterministic inventory behind this snapshot."""
        return tuple(sorted(self._tables.values()))

    def to_snapshot(self) -> dict[str, object]:
        schemas: dict[str, dict[str, list[str]]] = {}
        for table in self.tables:
            if table.schema is None:
                continue
            schemas.setdefault(table.schema, {})[table.table] = sorted(
                table.columns or ()
            )
        return {
            "format_version": "1.0",
            "catalog_name": self.catalog,
            "default_schema": self.default_schema,
            "schemas": schemas,
        }
