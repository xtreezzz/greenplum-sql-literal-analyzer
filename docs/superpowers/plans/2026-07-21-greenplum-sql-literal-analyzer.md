# Greenplum SQL Literal Analyzer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an end-to-end Python tool that maps `&CHARACTER` substitutions in Greenplum query logs to physical columns, classifies masks and regexes, and reports detailed and aggregate usage.

**Architecture:** Parse original/template pairs with SQLGlot, align placeholder-bearing AST literals by structural path, and resolve subject columns recursively through SQLGlot scopes. Keep database I/O, schema metadata, analysis, aggregation, and reporting as separate modules so the core remains testable without Greenplum.

**Tech Stack:** Python 3.11, SQLGlot, standard-library `unittest`, optional `psycopg2`, Greenplum 6 / PostgreSQL SQL.

---

### Task 1: Core contracts and placeholder/pattern logic

**Files:** `pyproject.toml`, `src/gp_sql_analyzer/models.py`, `src/gp_sql_analyzer/placeholders.py`, `src/gp_sql_analyzer/patterns.py`, `tests/test_placeholders.py`, `tests/test_patterns.py`

- [ ] Write failing tests for single/multiple placeholders, escaped LIKE wildcards, LIKE families, regex features, and deterministic serialization.
- [ ] Run `python3 -m unittest tests.test_placeholders tests.test_patterns -v` and confirm the missing-module failures.
- [ ] Implement immutable record models, literal alignment, LIKE classification, and regex feature extraction.
- [ ] Re-run the focused tests and require all cases to pass.

### Task 2: AST matching, context, and physical lineage

**Files:** `src/gp_sql_analyzer/schema.py`, `src/gp_sql_analyzer/lineage.py`, `src/gp_sql_analyzer/analyzer.py`, `tests/test_analyzer.py`, `tests/test_lineage.py`

- [ ] Write failing golden tests based only on TPC-DS tables/columns for aliases, reversed predicates, JOIN/WHERE/SELECT/HAVING/CASE, CTE chains, derived tables, correlated subqueries, `UNION ALL`, ambiguous columns, regex operators, and regex functions.
- [ ] Implement cached mapping/catalog schema providers and a scope-aware recursive lineage resolver with `resolved`, `multi_source`, `ambiguous`, and `unresolved` outcomes.
- [ ] Implement original/template parsing, AST-path matching, operation/context detection, error isolation, and query/template hashing.
- [ ] Run the two focused test modules and require the complete golden set to pass.

### Task 3: Streaming, aggregation, reports, and CLI

**Files:** `src/gp_sql_analyzer/aggregate.py`, `src/gp_sql_analyzer/greenplum.py`, `src/gp_sql_analyzer/io.py`, `src/gp_sql_analyzer/cli.py`, `src/gp_sql_analyzer/__main__.py`, `sql/aggregate_usage.sql`, `tests/test_aggregate.py`, `tests/test_greenplum.py`, `tests/test_cli.py`

- [ ] Write failing tests for weighted counts, distinct hashes, shares, deterministic examples, safe identifiers, parameterized filters, batch isolation, JSONL output, and a local CLI run.
- [ ] Implement optional exact-pair preaggregation in Greenplum, server-side cursor batching, pg_catalog metadata loading, streaming analysis, JSONL reports, metrics, and CLI configuration through environment variables.
- [ ] Run the focused tests and an end-to-end local fixture through `python3 -m gp_sql_analyzer`.

### Task 4: Reproducible complex benchmark corpus

**Files:** `scripts/fetch_benchmarks.py`, `tests/fixtures/tpcds_cases.json`, `tests/test_benchmarks.py`, `BENCHMARK_SOURCES.md`

- [ ] Pin DuckDB commit `9ebdd1ee5279885dd2a89d4ac8f37034c05de203` and define official URLs for TPC-DS Q02, Q14, Q34, Q47, Q64, Q91 plus all 99 optional stress queries and schema DDLs.
- [ ] Use focused benchmark-derived cases for offline correctness; keep full upstream SQL downloadable rather than copying the corpus into this repository.
- [ ] Verify the selected corpus structurally contains multiple CTEs, nested subqueries, set operations, repeated CTE references, joins, and window functions.
- [ ] Run a no-crash benchmark and emit parse rate, lineage status distribution, elapsed time, throughput, and peak memory.

### Task 5: Documentation and completion audit

**Files:** `README.md`, `.env.example`, `tests/test_error_isolation.py`

- [ ] Document architecture, Greenplum setup, source-table fields, mapping schema format, commands, output contracts, benchmark provenance, and limitations that require a real Greenplum 6 instance.
- [ ] Run `python3 -m unittest discover -s tests -v` and a local end-to-end example.
- [ ] Search for the original invented country example and require zero matches.
- [ ] Audit every requirement from the attached objective against code, tests, reports, and documentation; leave any real-Greenplum-only checks explicitly unverified.
