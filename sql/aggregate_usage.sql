-- Execute after loading detail rows into analytics.sql_literal_usage_detail.
-- base_columns and regex_features are stored as deterministic JSON text.

CREATE OR REPLACE VIEW analytics.sql_literal_usage_summary AS
WITH grouped AS (
    SELECT
        base_columns,
        lineage_status,
        clause_context,
        operator_or_function,
        value_role,
        pattern_family,
        pattern_format,
        pattern_template,
        extracted_value,
        regex_features,
        SUM(source_row_count) AS source_row_count,
        COUNT(*) AS occurrence_count,
        COUNT(DISTINCT query_hash) AS distinct_query_count,
        COUNT(DISTINCT template_hash) AS distinct_template_count,
        MIN(query_id) AS example_query_id_1,
        MAX(query_id) AS example_query_id_2
    FROM analytics.sql_literal_usage_detail
    GROUP BY
        base_columns,
        lineage_status,
        clause_context,
        operator_or_function,
        value_role,
        pattern_family,
        pattern_format,
        pattern_template,
        extracted_value,
        regex_features
), with_totals AS (
    SELECT
        grouped.*,
        SUM(source_row_count) OVER (PARTITION BY base_columns) AS column_total
    FROM grouped
)
SELECT
    with_totals.*,
    ROUND(source_row_count::numeric / NULLIF(column_total, 0), 8) AS share_of_column
FROM with_totals;
