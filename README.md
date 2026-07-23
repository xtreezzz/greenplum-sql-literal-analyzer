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

Эта установка нужна для работы из клона репозитория, включая локальный Jupyter. Для CLI без Jupyter/Pandas достаточно `.[greenplum]`. Переносимый notebook ниже умеет самостоятельно установить зависимости и не требует клона репозитория. Проверенный стек: Python 3.11, SQLGlot 25.34.x, psycopg2 2.9.x.

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

## Pandas и Jupyter: переносимый основной workflow

Основной notebook-сценарий — [`notebooks/sql_catalog_from_dataframe.ipynb`](notebooks/sql_catalog_from_dataframe.ipynb). Файл можно скопировать и запускать вне репозитория. Он работает только с заранее подготовленными pandas DataFrame: не подключается к Greenplum и не выполняет SQL, а только разбирает переданный текст SQL. Для чтения данных непосредственно из Greenplum используйте отдельный CLI-сценарий из раздела «Greenplum 6».

### Зависимости

В notebook уже встроен полный AST-анализатор. Он никогда не устанавливает и не импортирует пакет этого репозитория и не скачивает архивы проекта. Нужны только `pandas>=2,<3` и `sqlglot>=25.34,<26` из PyPI. При `AUTO_INSTALL=True` notebook устанавливает или обновляет обе зависимости в окружении текущего Python-ядра, только если несовместимый модуль ещё не загружен; при совместимых версиях сеть и `pip` не используются. Если несовместимый или неопределяемый `pandas` либо `sqlglot` уже загружен (особенно после создания DataFrame), notebook его не подменяет: установите совместимые версии, перезапустите kernel и заново создайте или загрузите входные DataFrame. При `AUTO_INSTALL=False` установите эти две зависимости заранее в окружение ядра.

### Входные DataFrame и конфигурация

До ячейки загрузки входов создайте pandas DataFrame и укажите имена переменных в конфигурации. Точные значения по умолчанию:

```python
QUERY_DF_NAME = "my_queries_df"
SCHEMA_DF_NAME = "my_schema_df"
DEFAULT_SCHEMA = "public"
OUTPUT_DIR = None
BUILD_HTML = False
AUTO_INSTALL = True
```

`my_queries_df` обязателен. В нём нужны колонки `query_text` и `query_text_template`; `query_id` и `source_row_count` необязательны. Значение `source_row_count` должно быть положительным целым числом. Оно хранит исходную частоту, если одинаковые пары SQL уже были сгруппированы до запуска notebook.

`my_schema_df` необязателен, но рекомендуется для точного lineage. В нём нужны `schema_name` **или** `table_schema`, а также `table_name`, `column_name`; `table_catalog` можно добавить опционально. Если присутствуют оба имени схемы, в каждой строке они должны быть одинаковыми валидными непустыми строками — иначе notebook выдаст ошибку валидации. Одна строка описывает одну физическую колонку; во всём DataFrame допустимо не более одного различного значения `table_catalog`, не равного `null`/`NaN`. Если метаданных схемы нет, задайте `SCHEMA_DF_NAME = None`. Входные DataFrame не изменяются.

Минимальный пример с двумя физическими колонками и одной парой исходного SQL/шаблона:

```python
import pandas as pd

my_schema_df = pd.DataFrame(
    [
        ("prod_dds", "calendar_date", "dt"),
        ("prod_emart", "calendar_date", "dt"),
    ],
    columns=["schema_name", "table_name", "column_name"],
)

my_queries_df = pd.DataFrame(
    [
        {
            "query_id": "q1",
            "query_text": (
                "SELECT dt FROM prod_dds.calendar_date "
                "WHERE dt = DATE '2026-01-01';"
            ),
            "query_text_template": (
                "SELECT dt FROM prod_dds.calendar_date "
                "WHERE dt = DATE '&CHARACTER';"
            ),
        }
    ]
)
```

После создания DataFrame задайте имена в `QUERY_DF_NAME` и `SCHEMA_DF_NAME`, при необходимости настройте `AUTO_INSTALL`, `BUILD_HTML` и `OUTPUT_DIR`, затем выполните **Run All**. Notebook сам не читает Greenplum: для этого пользователь отдельно формирует DataFrame; текущий workflow принимает именно DataFrame.

### Результаты notebook

После анализа в памяти доступны шесть DataFrame:

- `row_analysis_df` — одна строка на каждую входную строку;
- `details_df` — отдельные найденные употребления литералов и их lineage;
- `aggregate_df` — агрегация только по однозначно разрешённым физическим колонкам со статусом `resolved`;
- `catalog_tables_df` — табличная сводка каталога;
- `catalog_columns_df` — статистика по физическим колонкам;
- `errors_df` — изолированные ошибки разбора и анализа.

Статусы `ambiguous`, `multi_source` и `unresolved` не угадываются и не попадают в `aggregate_df`: они остаются в `row_analysis_df` и `details_df` для проверки качества lineage.

По умолчанию `OUTPUT_DIR=None`, поэтому результаты остаются в памяти и файлы не создаются. Если задать путь в `OUTPUT_DIR`, notebook и библиотечный вызов `analyze_dataframe(..., output_dir=...)` записывают именно следующие файлы:

- `row_analysis.jsonl`;
- `details.jsonl`;
- `errors.jsonl`;
- `aggregate.jsonl`;
- `catalog-stats.json`;
- `catalog-columns.jsonl`;
- `schema.json`.

`catalog-stats.html` добавляется к ним только когда одновременно задан `OUTPUT_DIR` и установлено `BUILD_HTML=True`.

### Обновление встроенного анализатора (maintainers)

Встроенный payload генерируется из исходников. После изменения анализатора выполните:

```bash
PYTHONPATH=src python3 scripts/embed_notebook_analyzer.py
PYTHONPATH=src python3 scripts/embed_notebook_analyzer.py --check
```

Генератор атомарно обновляет только стабильную ячейку с payload; не редактируйте сгенерированную ячейку вручную.

## Результаты CLI `analyze`

Этот набор относится только к пакетному запуску `python -m gp_sql_analyzer analyze --output-dir ...`, а не к notebook или `analyze_dataframe`. CLI создаёт:

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
