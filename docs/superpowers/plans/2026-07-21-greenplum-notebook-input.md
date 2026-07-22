# Greenplum-first Notebook Input Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `notebooks/sql_catalog_from_dataframe.ipynb` read a configured Greenplum table itself, build a catalog snapshot, run SQLGlot analysis, and expose every resulting DataFrame.

**Architecture:** The notebook uses the existing read-only Greenplum adapter instead of embedding ad-hoc SQL. `iter_greenplum_records` performs server-side preaggregation and batching; `load_catalog_schema` provides the catalog snapshot; `analyze_dataframe` remains the single SQLGlot analysis entry point. Tests validate notebook structure and Python syntax without connecting to a real database.

**Tech Stack:** Python 3.11, Jupyter/nbformat, pandas, psycopg2, SQLGlot, unittest.

---

## File map

- Modify `tests/test_notebook.py`: define the Greenplum-first notebook contract and compile every code cell.
- Modify `notebooks/sql_catalog_from_dataframe.ipynb`: replace the demo-first flow with configuration, Greenplum load, catalog load, SQLGlot analysis, and result views.
- Modify `README.md`: describe the notebook as a self-contained Greenplum workflow while preserving the Python DataFrame API example.

### Task 1: Lock the notebook contract with a failing test

**Files:**
- Modify: `tests/test_notebook.py`
- Test: `tests/test_notebook.py`

- [ ] **Step 1: Replace the existing notebook assertion with the Greenplum-first contract**

Replace the test method with the complete contract below:

```python
    def test_notebook_loads_greenplum_before_sqlglot_analysis(self) -> None:
        notebook = nbformat.read(NOTEBOOK, as_version=4)
        source = "\n".join(cell.source for cell in notebook.cells)

        errors = [
            output
            for cell in notebook.cells
            if cell.cell_type == "code"
            for output in cell.get("outputs", [])
            if output.get("output_type") == "error"
        ]

        self.assertFalse(errors)
        self.assertTrue(all(cell.get("id") for cell in notebook.cells))

        for cell in notebook.cells:
            if cell.cell_type == "code":
                compile(cell.source, str(NOTEBOOK), "exec")

        self.assertIn('SOURCE_TABLE = "analytics.query_log"', source)
        self.assertIn("connect_greenplum()", source)
        self.assertIn("SourceQueryConfig(", source)
        self.assertIn("preaggregate=True", source)
        self.assertIn("iter_greenplum_records(", source)
        self.assertIn("load_catalog_schema(", source)
        self.assertLess(
            source.index("iter_greenplum_records("),
            source.index("result = analyze_dataframe("),
        )
        self.assertNotIn("q44_style =", source)

        for assignment in (
            "queries_df =",
            "schema_df =",
            "row_analysis_df = result.row_analysis_df",
            "details_df = result.details_df",
            "aggregate_df = result.aggregate_df",
            "catalog_tables_df = result.catalog_tables_df",
            "catalog_columns_df = result.catalog_columns_df",
            "errors_df = result.errors_df",
        ):
            self.assertIn(assignment, source)

        self.assertIn("build_html=BUILD_HTML", source)
        self.assertNotIn("GP_PASSWORD =", source)
```

- [ ] **Step 2: Run the notebook test and verify RED**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_notebook -v
```

Expected: FAIL because the current notebook has no `SOURCE_TABLE` configuration, loads Greenplum after analysis, and does not assign all result DataFrames.

- [ ] **Step 3: Commit the failing contract**

```bash
git add tests/test_notebook.py
git commit -m "Test Greenplum-first notebook flow"
```

### Task 2: Rebuild the notebook around Greenplum input

**Files:**
- Modify: `notebooks/sql_catalog_from_dataframe.ipynb`
- Test: `tests/test_notebook.py`

- [ ] **Step 1: Replace the demo setup with imports and explicit configuration**

The import cell must initialize the local package and import only the public workflow functions:

```python
from pathlib import Path
import sys

import pandas as pd
from IPython.display import display

ROOT = Path.cwd().resolve()
if not (ROOT / "src" / "gp_sql_analyzer").is_dir():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT / "src"))

from gp_sql_analyzer.dataframe import analyze_dataframe
from gp_sql_analyzer.greenplum import (
    SourceQueryConfig,
    connect_greenplum,
    iter_greenplum_records,
    load_catalog_schema,
)
```

Add one configuration cell before any database access:

```python
SOURCE_TABLE = "analytics.query_log"
DEFAULT_SCHEMA = "public"
BATCH_SIZE = 500
LIMIT = None
OUTPUT_DIR = ROOT / "reports" / "greenplum"
BUILD_HTML = False
```

Its markdown must state that connection values come from `GP_DSN` or `GP_HOST`, `GP_PORT`, `GP_DBNAME`, `GP_USER`, `GP_PASSWORD`, and `GP_SSLMODE`; the password must never be assigned in the notebook.

- [ ] **Step 2: Add one read-only Greenplum loading cell before analysis**

Use the existing validated adapter and always close the connection:

```python
source_config = SourceQueryConfig(
    table=SOURCE_TABLE,
    limit=LIMIT,
    preaggregate=True,
)

connection = connect_greenplum()
try:
    query_records = [
        record
        for batch in iter_greenplum_records(
            connection,
            source_config,
            batch_size=BATCH_SIZE,
        )
        for record in batch
    ]
    schema_provider = load_catalog_schema(
        connection,
        default_schema=DEFAULT_SCHEMA,
    )
finally:
    connection.close()

queries_df = pd.DataFrame(
    [
        {
            "query_id": record.query_id,
            "query_text": record.query_text,
            "query_text_template": record.query_text_template,
            "source_row_count": record.source_row_count,
        }
        for record in query_records
    ],
    columns=[
        "query_id",
        "query_text",
        "query_text_template",
        "source_row_count",
    ],
)

schema_df = pd.DataFrame(
    [
        {
            "table_catalog": table.catalog,
            "table_schema": table.schema,
            "table_name": table.table,
            "column_name": column,
        }
        for table in schema_provider.tables
        for column in sorted(table.columns or ())
    ],
    columns=["table_catalog", "table_schema", "table_name", "column_name"],
)

if queries_df.empty:
    raise RuntimeError(
        "Greenplum returned no query/template pairs containing &CHARACTER"
    )

print(f"Уникальных пар SQL/шаблон: {len(queries_df):,}")
print(f"Исходных строк: {queries_df['source_row_count'].sum():,}")
print(f"Колонок в снимке каталога: {len(schema_df):,}")
```

- [ ] **Step 3: Add one clearly labelled SQLGlot analysis cell**

```python
result = analyze_dataframe(
    queries_df,
    schema_df=schema_df,
    default_schema=DEFAULT_SCHEMA,
    output_dir=OUTPUT_DIR,
    build_html=BUILD_HTML,
    example_limit=20,
    source_label=SOURCE_TABLE,
)

row_analysis_df = result.row_analysis_df
details_df = result.details_df
aggregate_df = result.aggregate_df
catalog_tables_df = result.catalog_tables_df
catalog_columns_df = result.catalog_columns_df
errors_df = result.errors_df

assert len(row_analysis_df) == len(queries_df)
print(f"Разобрано пар: {len(row_analysis_df):,}")
print(f"Найдено употреблений: {len(details_df):,}")
print(f"Строк в агрегате: {len(aggregate_df):,}")
```

The preceding markdown must explicitly say that `analyze_dataframe` parses both SQL texts with SQLGlot AST and does not execute them.

- [ ] **Step 4: Keep concise result cells for every output**

Add displays for:

```python
display(row_analysis_df.head(20))
display(details_df.head(50))
display(aggregate_df.head(50))
display(catalog_tables_df.head(50))
display(catalog_columns_df.head(50))
display(errors_df.head(50))

print("Созданные файлы:")
for name, path in result.artifact_paths.items():
    print(f"  {name:16s} {path}")
```

Remove the embedded TPC-DS demo queries and the disabled Greenplum cell. Clear stale execution counts and outputs so the committed notebook does not present synthetic data as a real database run.

- [ ] **Step 5: Run the notebook test and verify GREEN**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_notebook -v
```

Expected: PASS; all code cells compile, Greenplum loading precedes analysis, and all DataFrame assignments exist.

- [ ] **Step 6: Commit the notebook**

```bash
git add notebooks/sql_catalog_from_dataframe.ipynb
git commit -m "Make notebook load Greenplum directly"
```

### Task 3: Update the usage documentation and verify the project

**Files:**
- Modify: `README.md`
- Test: `tests/test_notebook.py`

- [ ] **Step 1: Document the notebook-first workflow**

Change the Pandas/Jupyter section to lead with:

```markdown
Готовый notebook `notebooks/sql_catalog_from_dataframe.ipynb` самостоятельно читает Greenplum. Укажите `SOURCE_TABLE`, задайте параметры подключения через переменные окружения и выполните ячейки сверху вниз. Greenplum предварительно группирует одинаковые пары `query_text/query_text_template`, а `source_row_count` сохраняет частоту исходных строк.
```

Keep the existing `analyze_dataframe(queries_df, ...)` example as the secondary API for users who already have a pandas DataFrame.

- [ ] **Step 2: Run the notebook and Greenplum tests**

Run:

```bash
PYTHONPATH=src python3 -m unittest tests.test_notebook tests.test_greenplum tests.test_dataframe -v
```

Expected: all selected tests PASS without connecting to Greenplum.

- [ ] **Step 3: Run the full test suite**

Run:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Expected: all tests PASS with zero failures and zero errors.

- [ ] **Step 4: Verify notebook JSON and forbidden credential assignment**

Run:

```bash
python3 -m json.tool notebooks/sql_catalog_from_dataframe.ipynb
rg -n "GP_PASSWORD\s*=" notebooks/sql_catalog_from_dataframe.ipynb README.md
```

Expected: JSON validation exits 0; `rg` exits 1 with no matches.

- [ ] **Step 5: Commit the documentation**

```bash
git add README.md
git commit -m "Document Greenplum notebook workflow"
```
