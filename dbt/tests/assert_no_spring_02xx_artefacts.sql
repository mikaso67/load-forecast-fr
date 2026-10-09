-- Test that no spring 02:xx artefact rows remain in clean.load_30min.
-- Verifies:
-- 1. Every spring DST day (23 local hours) has exactly 46 half-hour slots.
-- 2. At 01:00 and 01:30 UTC, the clean forecast matches the real 03:xx CEST row,
--    not the phantom 02:xx artefact row where the two raw forecasts differ.

WITH spring_dst_days AS (
    SELECT
        (value_time AT TIME ZONE 'Europe/Paris')::date AS dst_date
    FROM {{ ref('load_30min') }}
    WHERE extract(month FROM (value_time AT TIME ZONE 'Europe/Paris')) = 3
      AND extract(day FROM (value_time AT TIME ZONE 'Europe/Paris')) >= 25
      AND extract(dow FROM (value_time AT TIME ZONE 'Europe/Paris')) = 0
    GROUP BY dst_date
),

bad_slot_counts AS (
    SELECT
        min(c.value_time) AS value_time,
        'spring_dst_day_has_wrong_slot_count' AS failure_reason
    FROM {{ ref('load_30min') }} c
    JOIN spring_dst_days d
      ON (c.value_time AT TIME ZONE 'Europe/Paris')::date = d.dst_date
    GROUP BY d.dst_date
    HAVING count(*) != 46
),

phantom_forecast_matches AS (
    SELECT
        c.value_time,
        'phantom_forecast_value_detected' AS failure_reason
    FROM {{ ref('load_30min') }} c
    JOIN {{ source('raw', 'eco2mix') }} phantom
      ON c.value_time = phantom.value_time
     AND c.source_dataset = phantom.source_dataset
     AND c.prevision_j1 = phantom.prevision_j1
    JOIN {{ source('raw', 'eco2mix') }} real_row
      ON c.value_time = real_row.value_time
     AND c.source_dataset = real_row.source_dataset
     AND phantom.payload_hash != real_row.payload_hash
    WHERE phantom.original_values->>'heure' LIKE '02:%'
      AND real_row.original_values->>'heure' LIKE '03:%'
      AND phantom.prevision_j1 != real_row.prevision_j1
)

SELECT * FROM bad_slot_counts
UNION ALL
SELECT * FROM phantom_forecast_matches
