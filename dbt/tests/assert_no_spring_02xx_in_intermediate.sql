-- Test that no spring 02:xx phantom artefact rows remain in int_load_30min_filtered.
-- Verifies Protocol D12: on the 23-hour spring DST Sunday, records with local wall time 02:xx
-- must be filtered out before deduplication.

SELECT
    value_time,
    source_dataset,
    original_values->>'heure' AS phantom_heure
FROM {{ ref('int_load_30min_filtered') }}
WHERE extract(month FROM (value_time AT TIME ZONE 'Europe/Paris')) = 3
  AND extract(day FROM (value_time AT TIME ZONE 'Europe/Paris')) >= 25
  AND extract(dow FROM (value_time AT TIME ZONE 'Europe/Paris')) = 0
  AND original_values->>'heure' LIKE '02:%'
