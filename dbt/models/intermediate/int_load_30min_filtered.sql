{{ config(
    materialized='ephemeral' if var('snapshot_relation', none) is not none else 'view'
) }}

{% if var('ingested_before', none) is not none %}
WITH known_rows AS (
    SELECT *
    FROM {{ source('raw', 'eco2mix') }}
    WHERE ingested_at < '{{ var("ingested_before") }}'::timestamptz
)
{% endif %}
SELECT
    value_time,
    consommation,
    prevision_j1,
    prevision_j,
    source_dataset,
    original_values,
    ingested_at,
    payload_hash
FROM {% if var('ingested_before', none) is not none %}known_rows{% else %}{{ source('raw', 'eco2mix') }}{% endif %}
WHERE
    -- Keep only UTC half-hour timestamps (minutes 00 and 30)
    extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
    -- Keep records from delivery date 2021-03-23 onwards (delivery day begins at 2021-03-22 23:00 UTC)
    AND (value_time AT TIME ZONE 'Europe/Paris')::date >= '2021-03-23'
    {% if var('delivery_end_exclusive', none) is not none %}
    -- Bound validation builds before deduplication; leave the default build unchanged.
    AND value_time < '{{ var("delivery_end_exclusive") }}'::date AT TIME ZONE 'Europe/Paris'
    {% endif %}
    -- Apply protocol D12: drop spring 02:xx phantom artefacts where local heure does not match Paris time
    AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
