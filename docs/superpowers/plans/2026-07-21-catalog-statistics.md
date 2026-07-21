# Catalog Statistics Implementation Plan

**Goal:** Generate data-catalog-ready per-table/per-column JSON, JSONL and a
self-contained HTML report from all TPC-DS queries, while keeping aggregation
reusable for production `details.jsonl` rows.

**Architecture:** Introduce a canonical typed catalog report. Two adapters feed
it: benchmark `LiteralUsage` events plus a DDL inventory, and persisted detail
rows plus an optional schema inventory. Serializers emit hierarchical JSON and
flat one-row-per-column JSONL. The HTML renderer consumes only the canonical
report.

**Tech stack:** Python 3.11, SQLGlot 25.x, standard-library dataclasses/JSON/HTML,
inline CSS/JavaScript, unittest.

## Task 1: Schema inventory and canonical model

**Files:**

- Modify `src/gp_sql_analyzer/schema.py`
- Create `src/gp_sql_analyzer/catalog_stats.py`
- Create `tests/test_catalog_stats.py`

1. Write failing tests that require every DDL column, including unused ones,
   resolved-only aggregation, deterministic top values, context/operator counts
   and an explicit quality section.
2. Run the focused tests and confirm RED.
3. Expose a stable schema inventory and implement typed report builders for
   benchmark usage events.
4. Run the focused tests and confirm GREEN.

## Task 2: Persisted-details postprocessing

**Files:**

- Modify `src/gp_sql_analyzer/catalog_stats.py`
- Modify `tests/test_catalog_stats.py`

1. Add failing tests for `details.jsonl`-shaped mappings, including
   `source_row_count`, LIKE/regex metadata, examples and ambiguous lineage.
2. Confirm RED.
3. Implement a row adapter that produces the same canonical report without
   parsing SQL.
4. Confirm GREEN and assert equivalence with matching benchmark events.

## Task 3: JSON/JSONL and HTML renderers

**Files:**

- Create `src/gp_sql_analyzer/catalog_html.py`
- Create `tests/test_catalog_html.py`

1. Write failing tests for hierarchy, all columns, top values/masks, quality,
   search/filter controls, escaped content and absence of external assets.
2. Confirm RED.
3. Implement JSON-safe serialization, flat column rows and a self-contained
   Russian HTML renderer that consumes only the canonical report.
4. Confirm GREEN.

## Task 4: CLI composition and documentation

**Files:**

- Modify `src/gp_sql_analyzer/cli.py`
- Modify `tests/test_cli.py`
- Modify `README.md`

1. Add failing CLI tests for `catalog-report` from corpus/DDL and
   `catalog-postprocess` from details/schema JSON.
2. Confirm RED.
3. Implement both commands, deterministic file output and machine-readable
   stdout summaries.
4. Document the benchmark and future Greenplum workflow; confirm GREEN.

## Task 5: Full-corpus generation and verification

**Files:**

- Create `artifacts/benchmark/tpcds-catalog-stats.json`
- Create `artifacts/benchmark/tpcds-catalog-columns.jsonl`
- Create `artifacts/benchmark/tpcds-catalog-stats.html`

1. Generate all three artifacts from the pinned 99-query corpus and 24 DDLs.
2. Verify 99/99 parsing, expected table/column inventory, one JSONL row per
   column, resolved-only popularity and presence of known Q66/Q99 examples.
3. Run the complete test suite.
4. Open the HTML locally and verify desktop/mobile layout, search/filter
   behavior, no overflow and no JavaScript errors.

## Self-review

- Every acceptance criterion maps to Tasks 1–5.
- Production postprocessing is a first-class adapter, not a benchmark-only
  shortcut.
- HTML depends on the canonical report rather than reparsing SQL.
- Ambiguity is visible but excluded from trusted column popularity.
- No external network or runtime dependency is introduced.
