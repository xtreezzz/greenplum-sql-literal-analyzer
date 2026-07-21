# Benchmark sources

The stress corpus is the TPC-DS SQL query set distributed in the DuckDB repository. Downloads are pinned to DuckDB commit [`9ebdd1ee5279885dd2a89d4ac8f37034c05de203`](https://github.com/duckdb/duckdb/commit/9ebdd1ee5279885dd2a89d4ac8f37034c05de203), rather than the moving `main` branch.

Primary sources:

- [TPC-DS benchmark homepage](https://www.tpc.org/tpcds/)
- [TPC-DS 4.0 specification](https://www.tpc.org/TPC_Documents_Current_Versions/pdf/TPC-DS_v4.0.0.pdf)
- [DuckDB TPC-DS query directory](https://github.com/duckdb/duckdb/tree/9ebdd1ee5279885dd2a89d4ac8f37034c05de203/extension/tpcds/dsdgen/queries)
- [DuckDB TPC-DS schema directory](https://github.com/duckdb/duckdb/tree/9ebdd1ee5279885dd2a89d4ac8f37034c05de203/extension/tpcds/dsdgen/schema)
- [DuckDB MIT license](https://github.com/duckdb/duckdb/blob/9ebdd1ee5279885dd2a89d4ac8f37034c05de203/LICENSE)

The selected checksum-pinned smoke set is Q02, Q14, Q34, Q47, Q64, and Q91. It covers `UNION ALL`, `INTERSECT`, repeated CTE references, derived tables, nested subqueries, large multi-table joins, CASE expressions, and windows. The optional `--all` download contains all 99 queries.

The golden tests use TPC-DS table and column names and reduced PostgreSQL-parseable structures derived from those queries. TPC-DS does not materially exercise Greenplum POSIX-regex operators, so regex cases transform predicates over TPC-DS columns such as `item.i_color` and `item.i_product_name`; they are analyzer correctness fixtures, not official TPC benchmark queries or benchmark results.
