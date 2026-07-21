# TPC-DS HTML Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, self-contained HTML report that ranks all 99 pinned TPC-DS queries by structural AST complexity and explains how each query was parsed.

**Architecture:** Add a focused complexity-analysis module that converts each SQL file into typed metrics, then a separate renderer that emits server-rendered HTML with inline CSS and JavaScript. Expose the feature as a new `html-report` CLI subcommand and generate the checked artifact from the already pinned corpus.

**Tech Stack:** Python 3.11, SQLGlot 25.x, standard-library dataclasses/HTML/JSON, inline HTML/CSS/JavaScript, unittest/pytest.

---

### Task 1: Per-query AST complexity model

**Files:**
- Create: `src/gp_sql_analyzer/complexity.py`
- Create: `tests/test_complexity.py`

- [ ] **Step 1: Write failing tests for AST metrics and scoring**

Create a complex SQL fixture with two CTEs, a subquery, a join, a set operation, a window, a `CASE`, grouping and ordering. Assert `analyze_query()` returns exact construct counts, CTE/table/function names, a positive depth, and a score equal to `complexity_score(metrics)`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_complexity.py -q`

Expected: collection fails because `gp_sql_analyzer.complexity` does not exist.

- [ ] **Step 3: Implement the minimal typed analyzer**

Add `QueryComplexity` and `CorpusComplexity` dataclasses, an AST walker, depth and subquery-depth helpers, the documented score formula, error capture, stable unique-name collection, corpus ranking, and rank-derived tiers.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_complexity.py -q`

Expected: all complexity tests pass.

### Task 2: Self-contained HTML renderer

**Files:**
- Create: `src/gp_sql_analyzer/html_report.py`
- Create: `tests/test_html_report.py`

- [ ] **Step 1: Write failing rendering tests**

Build a two-query `CorpusComplexity`, render it, and assert the HTML includes the report title, source commit, visible score formula, cards in descending score order, escaped SQL, metric labels, search/filter controls, and no `http://`, `https://`, `<script src`, or `<link href` dependencies.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/test_html_report.py -q`

Expected: collection fails because `gp_sql_analyzer.html_report` does not exist.

- [ ] **Step 3: Implement semantic HTML and inline assets**

Render summary cards, aggregate bar charts, corpus caveat, filter toolbar, ranked query cards, parse explanations and escaped SQL. Add inline JavaScript for text/tier filtering and expand/collapse controls, plus responsive and print CSS.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/test_html_report.py -q`

Expected: all renderer tests pass.

### Task 3: CLI integration

**Files:**
- Modify: `src/gp_sql_analyzer/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `README.md`

- [ ] **Step 1: Write a failing CLI integration test**

Invoke `main(["html-report", "--corpus-dir", ..., "--output-html", ...])` on two SQL files and assert return code `0`, an existing HTML output and a JSON stdout summary with two parsed files.

- [ ] **Step 2: Run the test and verify RED**

Run: `python -m pytest tests/test_cli.py -q`

Expected: argparse rejects `html-report` as an invalid command.

- [ ] **Step 3: Add the CLI command and concise usage documentation**

Add `--corpus-dir`, `--output-html`, `--dialect` and optional `--source-label`. Call the corpus analyzer and renderer, write UTF-8 HTML, and print machine-readable metrics. Document the exact full-corpus command and clarify that AST complexity is not execution cost.

- [ ] **Step 4: Run CLI tests and verify GREEN**

Run: `python -m pytest tests/test_cli.py -q`

Expected: all CLI tests pass.

### Task 4: Generate and verify the full report

**Files:**
- Create: `artifacts/benchmark/tpcds-analysis.html`

- [ ] **Step 1: Generate from the pinned 99-query corpus**

Run: `PYTHONPATH=src python -m gp_sql_analyzer html-report --corpus-dir /tmp/gp-sql-analyzer-tpcds/queries --output-html artifacts/benchmark/tpcds-analysis.html --dialect postgres --source-label "TPC-DS · DuckDB 9ebdd1ee"`

Expected: stdout reports `99` seen, `99` parsed and `0` errors.

- [ ] **Step 2: Verify artifact invariants**

Run an automated check that parses embedded card/rank attributes and confirms 99 unique queries, monotonically decreasing scores, complete SQL blocks, and no external assets.

- [ ] **Step 3: Run the complete test suite**

Run: `python -m pytest -q`

Expected: all tests pass with zero failures.

- [ ] **Step 4: Perform browser visual QA**

Open the generated file locally, inspect the overview, first-ranked expanded card, filters and a narrow viewport. Correct any overflow, illegible contrast, broken disclosure state or JavaScript error, then repeat automated tests.

## Self-review

- Spec coverage: every acceptance criterion maps to Tasks 1–4.
- Placeholder scan: the plan contains no deferred implementation placeholders.
- Type consistency: analysis produces `CorpusComplexity`; rendering consumes the same type; CLI composes the two without duplicating AST logic.
- Scope: the report is isolated from the existing literal/template analyzer and uses only pinned benchmark SQL.
