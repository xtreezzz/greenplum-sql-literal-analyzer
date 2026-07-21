-- Optional Greenplum 6 tables for loading the JSONL detail/error results after
-- conversion to tabular rows. The analyzer itself is read-only by default.

CREATE TABLE IF NOT EXISTS analytics.sql_literal_usage_detail (
    query_id text,
    query_hash text,
    template_hash text,
    source_row_count bigint,
    base_columns text,
    lineage_status text,
    lineage_reason text,
    clause_context text,
    operator_or_function text,
    value_role text,
    raw_literal text,
    extracted_value text,
    pattern_template text,
    pattern_family text,
    pattern_format text,
    regex_features text,
    ast_path text
)
DISTRIBUTED BY (template_hash);

CREATE TABLE IF NOT EXISTS analytics.sql_literal_usage_error (
    query_id text,
    stage text,
    error_type text,
    message text,
    sql_fragment text
)
DISTRIBUTED BY (query_id);
