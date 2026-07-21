# DataFrame SQL Analysis Notebook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a pandas-first API and executable Jupyter Notebook that preserve one output row per `analytics.query_log` input row and produce a separate aggregation by physical column, value, context, operator and pattern.

**Architecture:** Extend `SQLAnalyzer` with individual predicate events for original literals and NULL checks, while retaining template-aligned occurrences as the highest-fidelity source. A new `dataframe.py` adapter validates DataFrames, builds optional schema mappings, deduplicates the three event sources, returns typed DataFrame results and writes artifacts only when requested. The notebook is a thin executable client of this tested API.

**Tech Stack:** Python 3.11, SQLGlot 25.x, pandas 2.x, Jupyter/nbformat/nbclient, standard-library JSON/HTML, unittest.

---

### Task 1: Individual original-predicate events

**Files:**

- Modify: `src/gp_sql_analyzer/models.py`
- Modify: `src/gp_sql_analyzer/analyzer.py`
- Modify: `tests/test_literal_usage.py`

- [ ] **Step 1: Write failing tests for individual literals and NULL checks**

Add tests that call `SQLAnalyzer.analyze_predicate_usages()` and assert:

```python
sql = """
SELECT *
FROM store_sales
WHERE ss_store_sk = 4
  AND ss_addr_sk IS NULL
  AND ss_ticket_number IS NOT NULL
  AND ss_quantity BETWEEN 1200 AND 1200 + 11
"""
usages = analyzer.analyze_predicate_usages(sql)

assert [(u.operator_or_function, u.extracted_value) for u in usages] == [
    ("=", "4"),
    ("IS NULL", "NULL"),
    ("IS NOT NULL", "NULL"),
    ("BETWEEN", "1200"),
    ("BETWEEN", "1200 + 11"),
]
assert usages[1].lineage.columns[0].qualified_name == (
    "tpcds.store_sales.ss_addr_sk"
)
assert usages[1].origin == "null_check"
```

Add a scalar-subquery regression:

```python
sql = """
SELECT ss_item_sk
FROM store_sales ss1
GROUP BY ss_item_sk
HAVING avg(ss_net_profit) > 0.9 * (
  SELECT avg(ss_net_profit)
  FROM store_sales
  WHERE ss_store_sk = 4 AND ss_addr_sk IS NULL
  GROUP BY ss_store_sk
)
"""
values = {(u.operator_or_function, u.extracted_value) for u in usages}
assert (">", "0.9") in values
assert ("=", "4") in values
assert ("IS NULL", "NULL") in values
assert not any("SELECT" in u.extracted_value for u in usages)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=src python -m unittest tests.test_literal_usage -v
```

Expected: fail because `PredicateUsage` and `analyze_predicate_usages` do not exist.

- [ ] **Step 3: Add the typed event model**

Add to `models.py`:

```python
PredicateOrigin = Literal["original_literal", "null_check"]

@dataclass(frozen=True, slots=True)
class PredicateUsage:
    lineage: LineageResult
    clause_context: str
    operator_or_function: str
    value_role: str
    raw_literal: str
    extracted_value: str
    pattern_template: str | None
    pattern_family: str
    pattern_format: str
    regex_features: dict[str, Any]
    ast_path: str
    origin: PredicateOrigin
```

- [ ] **Step 4: Implement individual predicate extraction**

In `SQLAnalyzer.analyze_predicate_usages(sql)`:

- parse each statement and create a `LineageResolver`;
- traverse literal nodes and their nearest supported predicate owner;
- deduplicate repeated literal nodes that form one logical expression boundary;
- preserve an arithmetic `BETWEEN` bound as one expression;
- when a comparison side contains a scalar subquery, return only the direct
  literal outside the subquery instead of serializing the whole subquery;
- traverse `exp.Is` nodes whose right side is `exp.Null` and detect a wrapping
  `exp.Not`;
- resolve the subject, clause context and pattern metadata;
- sort by statement/path order.

- [ ] **Step 5: Run the focused tests and verify GREEN**

Run the command from Step 2. Expected: all literal-usage tests pass.

### Task 2: DataFrame schema adapter and row-preserving result

**Files:**

- Create: `src/gp_sql_analyzer/dataframe.py`
- Create: `tests/test_dataframe.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing DataFrame contract tests**

Create a three-row `queries_df`: one valid template query, one Q44-style query,
and one broken SQL. Create `schema_df` with `table_catalog`, `table_schema`,
`table_name`, `column_name`.

Assert:

```python
result = analyze_dataframe(queries_df, schema_df=schema_df)

self.assertEqual(len(result.row_analysis_df), len(queries_df))
self.assertEqual(result.row_analysis_df.index.tolist(), queries_df.index.tolist())
self.assertEqual(result.row_analysis_df.iloc[0]["analysis_status"], "ok")
self.assertEqual(result.row_analysis_df.iloc[2]["analysis_status"], "error")
self.assertEqual(len(result.errors_df), 1)
self.assertIn("analysis", result.row_analysis_df.columns)
self.assertEqual(
    result.row_analysis_df.iloc[0]["analysis"][0]["base_columns"],
    ["warehouse.tpcds.item.i_color"],
)
```

Add validation tests for a missing `query_text_template`, non-positive
`source_row_count`, multiple catalogs in one `schema_df`, and `build_html=True`
without `output_dir`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
PYTHONPATH=src python -m unittest tests.test_dataframe -v
```

Expected: import fails because `gp_sql_analyzer.dataframe` does not exist.

- [ ] **Step 3: Add pandas as a notebook extra**

Extend `pyproject.toml`:

```toml
notebook = [
  "pandas>=2,<3",
  "jupyterlab>=4,<5",
  "nbformat>=5,<6",
  "nbclient>=0.10,<1",
]
```

Keep the base installation free of pandas.

- [ ] **Step 4: Implement schema conversion and result type**

Create:

```python
@dataclass(slots=True)
class DataFrameAnalysis:
    row_analysis_df: Any
    aggregate_df: Any
    details_df: Any
    errors_df: Any
    catalog_columns_df: Any
    catalog_tables_df: Any
    catalog_report: dict[str, Any]
    artifact_paths: dict[str, Path]

def schema_from_dataframe(schema_df, *, default_schema=None):
    ...

def analyze_dataframe(
    queries_df,
    *,
    schema_df=None,
    default_schema=None,
    dialect="postgres",
    placeholder="&CHARACTER",
    include_original_literals=True,
    include_null_checks=True,
    output_dir=None,
    build_html=False,
    top_limit=20,
    example_limit=5,
) -> DataFrameAnalysis:
    ...
```

Import pandas inside `analyze_dataframe` and raise:

```python
RuntimeError(
    "DataFrame analysis requires pandas; install gp-sql-analyzer[notebook]"
)
```

when it is unavailable.

- [ ] **Step 5: Implement one-input-row/one-output-row processing**

For every `queries_df.iterrows()` record:

1. build `QueryRecord` with the original index-derived id when `query_id` is
   absent;
2. call `analyze_record()` for template events;
3. call `analyze_predicate_usages()` for original literals/NULL checks;
4. convert events into a common mapping;
5. deduplicate original events already represented by a template event using
   `(base_columns, lineage_status, context, operator, raw_literal)`;
6. append the nested list and counters to the copied input row;
7. isolate all errors per row.

Status rules:

- `error`: parse failed and no analysis was produced;
- `partial`: any error or non-resolved event exists;
- `ok`: otherwise, including a valid query with an empty analysis list.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: row-preserving tests pass.

### Task 3: Resolved aggregate DataFrame

**Files:**

- Modify: `src/gp_sql_analyzer/dataframe.py`
- Modify: `tests/test_dataframe.py`

- [ ] **Step 1: Write failing aggregation tests**

Use duplicated DataFrame rows with weights 3 and 2. Assert `aggregate_df` has
one row for:

```text
warehouse.tpcds.item.i_color + purple + WHERE + ILIKE + like_contains
```

and:

```python
self.assertEqual(row["source_row_count"], 5)
self.assertEqual(row["occurrence_count"], 2)
self.assertEqual(row["distinct_query_count"], 2)
self.assertEqual(row["share_of_column"], 1.0)
```

Assert ambiguous/unresolved events do not enter `aggregate_df` but remain in
`details_df` and `row_analysis_df`.

- [ ] **Step 2: Run the focused aggregation tests and verify RED**

Expected: fail because the aggregate is empty or missing required columns.

- [ ] **Step 3: Implement deterministic aggregation**

Group resolved one-column detail rows by:

```python
group_columns = [
    "catalog_name", "schema_name", "table_name", "column_name",
    "extracted_value", "clause_context", "operator_or_function",
    "value_role", "pattern_family", "pattern_format",
]
```

Aggregate weighted and unweighted counts, distinct query ids/templates and
sorted example ids. Compute `share_of_column` from weighted counts. Sort by
qualified column, descending weighted count, then value.

- [ ] **Step 4: Build catalog DataFrames from the same details**

Call `build_catalog_report_from_details()`. Convert `column_rows()` to
`catalog_columns_df`; convert each table mapping without its nested `columns`
field to `catalog_tables_df`. No SQL reparsing is permitted in this step.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run all `tests.test_dataframe` tests.

### Task 4: Optional artifact and HTML output

**Files:**

- Modify: `src/gp_sql_analyzer/dataframe.py`
- Modify: `tests/test_dataframe.py`

- [ ] **Step 1: Write failing output tests**

With a temporary `output_dir` and default `build_html=False`, assert creation of:

- `row_analysis.jsonl`;
- `details.jsonl`;
- `errors.jsonl`;
- `aggregate.jsonl`;
- `catalog-stats.json`;
- `catalog-columns.jsonl`;
- `schema.json`;

Assert `catalog-stats.html` is absent. Repeat with `build_html=True` and assert
the HTML exists and contains a known physical column/value.

- [ ] **Step 2: Run the output tests and verify RED**

Expected: files are absent.

- [ ] **Step 3: Implement output writing**

Use existing `JsonlWriter`, `CatalogReport.to_dict()`,
`MappingSchemaProvider.to_snapshot()` and `render_catalog_html()`. Record only
created paths in `artifact_paths`. Do not write anything when `output_dir` is
`None`.

- [ ] **Step 4: Run output tests and verify GREEN**

Run all DataFrame tests.

### Task 5: Executable notebook

**Files:**

- Create: `notebooks/sql_catalog_from_dataframe.ipynb`
- Create: `artifacts/notebook-demo/` outputs generated by the notebook
- Modify: `README.md`

- [ ] **Step 1: Create the notebook cells**

Include:

1. repository-path bootstrap and imports;
2. a `queries_df` fixture using TPC-DS tables with a CTE/subquery, Q66-style
   `IN`, LIKE, regex, `BETWEEN 1200 AND 1200 + 11`, and NULL check;
3. a `schema_df` fixture in `pg_catalog` column format;
4. one `analyze_dataframe(..., build_html=False)` call;
5. displays of `row_analysis_df`, `aggregate_df`, `errors_df`,
   `catalog_tables_df`, `catalog_columns_df`;
6. filters for pattern families and quality statuses;
7. an optional, disabled cell showing read-only Greenplum loading using
   environment variables and `pandas.read_sql_query`;
8. a final optional cell with `build_html=True`.

- [ ] **Step 2: Add notebook documentation**

Document the minimal real-data replacement:

```python
queries_df = your_dataframe[["query_text", "query_text_template"]].copy()
result = analyze_dataframe(queries_df, schema_df=schema_df)
row_analysis_df = result.row_analysis_df
aggregate_df = result.aggregate_df
```

Explain that `schema_df` can come from `information_schema.columns` or
`pg_catalog`, and that no analyzed SQL is executed.

- [ ] **Step 3: Execute the notebook**

Run with an isolated output copy using `nbclient` or `jupyter nbconvert
--execute`. Expected: every executable cell succeeds, output DataFrames are
visible, and no HTML file is created by the primary call.

- [ ] **Step 4: Verify notebook invariants**

Use `nbformat` to assert zero error outputs, the expected cell count, presence
of `row_analysis_df` and `aggregate_df` displays, and no embedded credentials.

### Task 6: Completion verification

**Files:** all changed files and generated notebook artifacts.

- [ ] **Step 1: Run the full automated suite**

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Re-run the notebook from a clean kernel**

Expected: zero exceptions, row count parity, non-empty aggregate and optional
HTML absence in the main flow.

- [ ] **Step 3: Inspect the final notebook and artifact paths**

Confirm the notebook is readable, uses no hidden local inputs, keeps HTML
optional, and links only to files it actually created.

## Self-review

- Spec coverage: row-preserving output, resolved aggregate grain, optional
  schema, all three predicate origins, optional files/HTML, Greenplum example
  and notebook execution map to Tasks 1–6.
- Placeholder scan: no implementation steps are deferred or described as
  generic follow-up work.
- Type consistency: `DataFrameAnalysis` fields and `analyze_dataframe`
  arguments are identical across tests, implementation and notebook cells.
- Scope: the notebook is a client of the package rather than a second parser.
