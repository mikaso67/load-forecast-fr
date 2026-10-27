-- Paired half-hours with both a real-time and a consolidated consumption value.
-- Same row rules as the clean layer: UTC half-hours, D12 wall-clock check, latest stored version
-- per source and timestamp. Versions are counted so revised rows stay visible.
WITH filtered AS (
    SELECT source_dataset, value_time, consommation, prevision_j1, ingested_at,
           count(*) OVER (PARTITION BY source_dataset, value_time) AS versions,
           row_number() OVER (PARTITION BY source_dataset, value_time
                              ORDER BY ingested_at DESC) AS latest
    FROM raw.eco2mix
    WHERE source_dataset IN ('eco2mix-national-tr', 'eco2mix-national-cons-def')
      AND value_time >= %(start)s AND value_time < %(end)s
      AND extract(minute FROM value_time AT TIME ZONE 'UTC')::int IN (0, 30)
      AND original_values->>'heure' = to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')
), latest AS (
    SELECT * FROM filtered WHERE latest = 1 AND consommation IS NOT NULL
)
SELECT r.value_time,
       r.consommation AS real_time,
       c.consommation AS consolidated,
       r.prevision_j1 AS rte_d1_real_time_row,
       c.prevision_j1 AS rte_d1_consolidated_row,
       r.ingested_at AS real_time_ingested_at,
       c.ingested_at AS consolidated_ingested_at,
       r.versions AS real_time_versions,
       c.versions AS consolidated_versions
FROM latest AS r
JOIN latest AS c USING (value_time)
WHERE r.source_dataset = 'eco2mix-national-tr'
  AND c.source_dataset = 'eco2mix-national-cons-def'
ORDER BY r.value_time;

SELECT source_dataset, month, first_ingested_at, record_count
FROM raw.eco2mix_first_appearance
ORDER BY source_dataset, month;

SELECT attempted_at, source_dataset, range_start, range_end
FROM raw.eco2mix_api_calls
WHERE source_dataset = 'eco2mix-national-cons-def'
  AND range_start < %(end)s AND range_end > %(start)s
ORDER BY attempted_at, range_start
