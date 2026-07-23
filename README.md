# SQL literal analyzer notebook

Один переносимый Jupyter notebook для анализа `query_text` и
`query_text_template` из pandas DataFrame.

Notebook использует AST SQLGlot, чтобы определить:

- физическую схему, таблицу и колонку;
- значение, маску `LIKE`/`ILIKE` либо regex;
- место использования: `WHERE`, `JOIN ON`, `HAVING`, `SELECT`, `CASE`;
- качество lineage: `resolved`, `multi_source`, `ambiguous`, `unresolved`;
- частоты значений в разрезе схемы, таблицы и колонки.

SQL только разбирается и никогда не исполняется. Подключения к Greenplum в
notebook нет: входные DataFrame пользователь создаёт самостоятельно.

## Установка

На устройстве должны быть Python и Jupyter. Установите зависимости в окружение
Jupyter kernel:

```bash
python -m pip install "pandas>=2,<3" "sqlglot>=25.34,<26" jupyter
```

Затем откройте:

```bash
jupyter notebook notebooks/sql_catalog_from_dataframe.ipynb
```

Notebook не устанавливает зависимости автоматически. Код анализатора находится
в обычных видимых Python-ячейках и не импортирует отдельный проектный пакет.

## Входные DataFrame

В конфигурационной ячейке задаются имена переменных:

```python
QUERY_DF_NAME = "my_queries_df"
SCHEMA_DF_NAME = "my_schema_df"  # Можно задать None.
DEFAULT_SCHEMA = "public"
OUTPUT_DIR = None
BUILD_HTML = False
```

`my_queries_df` должен содержать:

- `query_text` — исходный SQL;
- `query_text_template` — SQL, где значения заменены на `&CHARACTER`;
- `query_id` — опциональный идентификатор;
- `source_row_count` — опциональная частота заранее сгруппированной пары.

Пример:

```python
import pandas as pd

my_queries_df = pd.DataFrame(
    [
        {
            "query_id": "q1",
            "query_text": (
                "SELECT * FROM prod_dds.calendar_date d "
                "WHERE d.name ILIKE '%sia%' "
                "AND d.dt BETWEEN 1200 AND 1200 + 11"
            ),
            "query_text_template": (
                "SELECT * FROM prod_dds.calendar_date d "
                "WHERE d.name ILIKE '%&CHARACTER%' "
                "AND d.dt BETWEEN 1200 AND 1200 + 11"
            ),
        }
    ]
)
```

`my_schema_df` опционален, но нужен для точного lineage. Обязательные колонки:

- `schema_name` либо `table_schema`;
- `table_name`;
- `column_name`;
- `table_catalog` — опционально.

Одинаковые таблица и колонка в разных схемах задаются отдельными строками:

```python
my_schema_df = pd.DataFrame(
    [
        ("prod_dds", "calendar_date", "dt"),
        ("prod_dds", "calendar_date", "name"),
        ("prod_emart", "calendar_date", "dt"),
    ],
    columns=["schema_name", "table_name", "column_name"],
)
```

Входные DataFrame не изменяются.

## Результаты

После выполнения доступны:

- `row_analysis_df` — один JSON-подобный разбор на входную строку;
- `details_df` — отдельное найденное значение или условие;
- `aggregate_df` — частоты по физической колонке, значению и контексту;
- `catalog_tables_df` — статистика по таблицам;
- `catalog_columns_df` — статистика по всем колонкам, включая неиспользованные;
- `errors_df` — ошибки отдельных запросов.

В `aggregate_df` попадают только однозначно разрешённые физические колонки.
Неоднозначные результаты остаются в построчном разборе и не получают
выдуманную таблицу.

Если `OUTPUT_DIR=None`, файлы не создаются. При заданном каталоге записываются
JSON/JSONL-результаты; HTML добавляется только при `BUILD_HTML=True`.

## Примеры

- `examples/tpcds_input.jsonl` — сложный TPC-DS запрос с CTE, `UNION ALL`,
  `JOIN`, `ILIKE` и regex;
- `examples/tpcds_schema.json` — использованный снимок схемы TPC-DS.

Notebook также обрабатывает вложенные и коррелированные подзапросы, CTE,
derived tables, `IN`, `BETWEEN`, `IS NULL`, маски и регулярные выражения.
Например, граница `1200 + 11` в `BETWEEN` остаётся одним выражением, а не
ошибочно превращается в отдельное значение `11`.
