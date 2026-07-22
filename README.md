# Greenplum SQL literal analyzer

Инструмент сопоставляет строковые значения из `query_text` с плейсхолдерами `&CHARACTER` из `query_text_template`, определяет физические колонки через AST/lineage и строит отчёты по значениям, `LIKE/ILIKE`-маскам и regex.

SQL не разбирается регулярными выражениями. Оба текста парсятся SQLGlot в PostgreSQL-диалекте; строковые литералы сопоставляются по одинаковому пути AST. Небольшая регулярка используется только после парсинга — для выделения значения внутри уже найденной пары строковых литералов.

## Что поддерживается

- `WHERE`, `JOIN ON`, `SELECT`, `HAVING`, `CASE`;
- `=`, `!=`, сравнения, `IN`, `BETWEEN` и обратная запись литерала слева;
- `LIKE`, `ILIKE`, отрицательные варианты и классификация exact/prefix/suffix/contains/complex;
- `SIMILAR TO`, `~`, `~*`, `!~`, `!~*`, `regexp_replace`, `regexp_matches`, regex-форма `substring`;
- алиасы, derived tables, цепочки CTE, явные имена колонок CTE, вложенные и коррелированные подзапросы, `UNION ALL`, повторное использование CTE и shadowing алиасов;
- статусы `resolved`, `multi_source`, `ambiguous`, `unresolved` без угадывания неоднозначной колонки;
- чтение Greenplum серверным курсором, exact-pair preaggregation и локальный JSONL-режим.

## Установка

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[greenplum,notebook]'
```

Эта установка нужна для основного notebook-сценария. Для CLI без Jupyter/Pandas достаточно `.[greenplum]`. Проверенный стек: Python 3.11, SQLGlot 25.34.x, psycopg2 2.9.x.

## Быстрый локальный запуск

Все демонстрационные таблицы и колонки взяты из TPC-DS:

```bash
.venv/bin/python -m gp_sql_analyzer analyze \
  --input-jsonl examples/tpcds_input.jsonl \
  --schema-json examples/tpcds_schema.json \
  --default-schema tpcds \
  --output-dir reports/example
```

Входной JSONL содержит `query_id`, `query_text`, `query_text_template` и необязательный `source_row_count` — вес предварительно сгруппированной пары.

## Greenplum 6

Заполните переменные из `.env.example` и запускайте read-only анализ:

```bash
.venv/bin/python -m gp_sql_analyzer analyze \
  --source-table analytics.query_log \
  --id-column query_id \
  --default-schema public \
  --catalog-schema public \
  --batch-size 500 \
  --output-dir reports/greenplum
```

Доступны ограничители `--since-column/--since-value`, `--min-id`, `--max-id`, `--limit` и `--no-preaggregate`. Имена SQL-объектов проходят строгую проверку, значения фильтров передаются параметрами. Пароли не записываются в код или отчёты.

Метаданные колонок читаются один раз из `pg_catalog`. Если в `search_path` есть одинаковые таблицы либо неквалифицированная колонка подходит нескольким источникам, результат получает `ambiguous`, а не случайно выбранную таблицу.

## Pandas и Jupyter

Основной путь — notebook [`notebooks/sql_catalog_from_dataframe.ipynb`](notebooks/sql_catalog_from_dataframe.ipynb):

1. Задайте `SOURCE_TABLE` и параметры подключения Greenplum в окружении: `GP_DSN` либо `GP_HOST`, `GP_PORT`, `GP_DBNAME`, `GP_USER`, `GP_PASSWORD`, `GP_SSLMODE`.
2. Настройте ограничитель источника и выполните ячейки сверху вниз.

Greenplum на стороне сервера предварительно агрегирует одинаковые пары `query_text`/`query_text_template`; `source_row_count` сохраняет исходную частоту строк. По умолчанию notebook отказывается от неограниченного сканирования. Укажите `SINCE_COLUMN` и `SINCE_VALUE`, либо `ID_COLUMN` с `MIN_ID`/`MAX_ID`; иначе явно задайте `ALLOW_FULL_SCAN=True` только после оценки стоимости для кластера.

`BATCH_SIZE` влияет только на размер порции получения данных. Результаты материализуются в памяти целиком. Для запуска только в памяти установите `OUTPUT_DIR=None`; `BUILD_HTML=False` по умолчанию.

`analyze_dataframe(queries_df, ...)` — дополнительный API для случаев, когда DataFrame уже подготовлен. Он принимает обязательные `query_text`, `query_text_template` и необязательные `query_id`, `source_row_count`:

```python
from gp_sql_analyzer.dataframe import analyze_dataframe

result = analyze_dataframe(
    queries_df,
    schema_df=schema_df,          # необязательно, но повышает точность lineage
    default_schema="public",
    output_dir="reports/run-1",  # необязательно
    build_html=False,             # HTML создаётся только по явному запросу
)

row_analysis_df = result.row_analysis_df  # одна строка на входную строку
aggregate_df = result.aggregate_df        # schema/table/column/value/context/operator/pattern
details_df = result.details_df            # одно найденное употребление на строку
```

`schema_df` ожидает `table_schema`, `table_name`, `column_name` и необязательный `table_catalog`. В `aggregate_df` входят только однозначно разрешённые физические колонки; `ambiguous`, `multi_source` и `unresolved` сохраняются в построчном разборе и `details_df`.

## Результаты

В `--output-dir` создаются:

- `details.jsonl` — каждое вхождение плейсхолдера, колонка/колонки lineage, контекст, оператор, исходный литерал, извлечённое значение, тип и признаки regex;
- `summary.jsonl` — частоты по значениям и форматам, число строк источника, уникальных запросов и шаблонов, доля внутри колонки и примеры query ID;
- `errors.jsonl` — изолированные ошибки parse/align/lineage с сокращённым и очищенным фрагментом;
- `metrics.json` — parse rate, распределение lineage-статусов, скорость, время и peak memory.
- `schema.json` — использованный снимок `catalog/schema/table/column`, необходимый для последующей агрегации с нулевыми колонками.

`sql/create_output_tables.sql` и `sql/aggregate_usage.sql` дают опциональную схему хранения и распределённую агрегацию в Greenplum. Анализатор сам ничего не создаёт в базе.

## Benchmark

Загрузить закреплённые запросы и DDL TPC-DS:

```bash
PYTHONPATH=src python3 scripts/fetch_benchmarks.py benchmarks/tpcds --all
PYTHONPATH=src python3 -m gp_sql_analyzer benchmark \
  --corpus-dir benchmarks/tpcds/queries \
  --output-json reports/tpcds-full.json
```

Текущий полный прогон находится в `artifacts/benchmark/tpcds-full.json`: 99 из 99 файлов разобраны, обнаружены 66 CTE, 147 подзапросов, 46 set operation, 551 JOIN и 27 оконных выражений. Источники, pin и оговорки описаны в `BENCHMARK_SOURCES.md`.

### Интерактивный HTML по сложности запросов

Сформировать автономный отчёт со всеми запросами, начиная с самых структурно сложных:

```bash
PYTHONPATH=src python3 -m gp_sql_analyzer html-report \
  --corpus-dir benchmarks/tpcds/queries \
  --schema-dir benchmarks/tpcds/schema \
  --default-schema tpcds \
  --output-html reports/tpcds-analysis.html \
  --source-label "TPC-DS · DuckDB 9ebdd1ee"
```

Внутри есть общая статистика, поиск и фильтры, рейтинг, метрики AST, объяснение разбора, таблица `значение → оператор → физическая колонка → WHERE/JOIN/SELECT` и полный SQL каждого запроса. DDL из `--schema-dir` нужен для надёжного разрешения неквалифицированных колонок. Если рядом с каталогом `queries` уже лежит каталог `schema`, он подхватывается автоматически. Балл измеряет синтаксическую насыщенность (CTE, подзапросы, JOIN, set operations, окна, CASE и глубину AST), но не заменяет `EXPLAIN` и не предсказывает стоимость выполнения в Greenplum.

### Статистика для дата-каталога

Сформировать агрегацию по всем физическим таблицам и колонкам TPC-DS:

```bash
PYTHONPATH=src python3 -m gp_sql_analyzer catalog-report \
  --corpus-dir benchmarks/tpcds/queries \
  --schema-dir benchmarks/tpcds/schema \
  --default-schema tpcds \
  --output-json reports/tpcds-catalog-stats.json \
  --output-jsonl reports/tpcds-catalog-columns.jsonl \
  --output-html reports/tpcds-catalog-stats.html
```

Создаются три представления одних агрегированных данных:

- `catalog-stats.json` — иерархия `таблица → колонка` со сводкой и отдельным блоком качества lineage;
- `catalog-columns.jsonl` — одна строка на колонку для пакетной загрузки в дата-каталог;
- `catalog-stats.html` — автономный просмотр с поиском и фильтрами по активности, секции SQL и типу паттерна.

Для каждой колонки доступны частота условий, число исходных строк и запросов, `WHERE/JOIN_ON/HAVING/CASE/SELECT`, операторы, топ значений, LIKE/ILIKE-масок и regex-форматов. В JSON входят все колонки DDL; нулевой счётчик означает, что анализ не нашёл литерал, однозначно связанный с этой колонкой. `ambiguous`, `multi_source` и `unresolved` остаются в `quality` и не искажают популярность конкретных колонок.

После основного анализа `analytics.query_log` повторно разбирать SQL для новых срезов не нужно. Каталожные файлы строятся из сохранённого `details.jsonl` и того же снимка схемы:

```bash
PYTHONPATH=src python3 -m gp_sql_analyzer catalog-postprocess \
  --details-jsonl reports/greenplum/details.jsonl \
  --schema-json reports/greenplum/schema.json \
  --default-schema public \
  --source-label analytics.query_log \
  --output-json reports/greenplum/catalog-stats.json \
  --output-jsonl reports/greenplum/catalog-columns.jsonl \
  --output-html reports/greenplum/catalog-stats.html
```

`source_row_count` сохраняет частоту предварительно сгруппированных пар `query_text/query_text_template`, поэтому топы отражают исходное число строк, а не только число уникальных SQL-текстов. Для нулевых колонок `schema.json` должен содержать полный снимок вида `schema → table → [columns]`.

## Тесты

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Golden-тесты используют TPC-DS-схему и проверяют ожидаемые колонки/значения для CTE, derived tables, коррелированных запросов, `UNION ALL`, shadowing, JOIN/WHERE/SELECT/HAVING/CASE, масок и regex.

## Что ещё требует реального Greenplum

Локально проверены SQL generation, параметризация, `pg_catalog` mapping и server-side batching через тестовые соединения. На целевом Greenplum 6 всё ещё необходимо проверить права конкретного пользователя, фактический `search_path`, совместимость типов ID/даты в выбранных фильтрах, стоимость `GROUP BY query_text, query_text_template` и план распределения для реального объёма `analytics.query_log`.

Ограничения MVP: поддерживается строковый плейсхолдер `&CHARACTER`; AST исходника и шаблона должны отличаться только содержимым соответствующих литералов; определения view и динамический SQL внутри строк не разворачиваются до базовых таблиц.
