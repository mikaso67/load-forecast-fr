{{ config(
    materialized='table',
    alias=var('snapshot_relation', 'load_30min')
) }}

WITH source_data AS (
    SELECT
        value_time,
        consommation,
        prevision_j1,
        prevision_j,
        source_dataset,
        {% if var('snapshot_relation', none) is not none %}
        ingested_at,
        payload_hash,
        {% endif %}
        ROW_NUMBER() OVER (
            PARTITION BY value_time
            ORDER BY
                CASE source_dataset
                    WHEN 'eco2mix-national-cons-def' THEN 1
                    WHEN 'eco2mix-national-tr' THEN 2
                    ELSE 3
                END,
                ingested_at DESC
        ) AS source_priority
    FROM {{ ref('int_load_30min_filtered') }}
)

SELECT
    value_time,
    consommation,
    prevision_j1,
    prevision_j,
    source_dataset
    {% if var('snapshot_relation', none) is not none %}
    , ingested_at, payload_hash
    {% endif %}
FROM source_data
WHERE source_priority = 1
ORDER BY value_time
