{{ config(
    materialized='table'
) }}

WITH source_data AS (
    SELECT
        value_time,
        consommation,
        prevision_j1,
        prevision_j,
        source_dataset,
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
FROM source_data
WHERE source_priority = 1
ORDER BY value_time
