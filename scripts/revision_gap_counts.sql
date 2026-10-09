-- Count stored vintages without selecting consumption values or model outputs.
SELECT source_dataset, count(*) AS raw_rows,
       count(DISTINCT value_time) AS timestamps,
       count(DISTINCT value_time) FILTER (WHERE consommation IS NOT NULL)
           AS filled_timestamps,
       min(value_time) AS first_utc, max(value_time) AS last_utc,
       max(ingested_at) AS last_ingestion_utc
FROM raw.eco2mix
WHERE source_dataset IN ('eco2mix-national-tr', 'eco2mix-national-cons-def')
GROUP BY source_dataset
ORDER BY source_dataset;

-- Collapse append-only revisions before counting, preventing many-to-many joins.
WITH vintages AS (
    SELECT value_time,
           bool_or(source_dataset = 'eco2mix-national-tr') AS realtime,
           bool_or(source_dataset = 'eco2mix-national-cons-def') AS consolidated,
           bool_or(source_dataset = 'eco2mix-national-tr'
                   AND consommation IS NOT NULL) AS realtime_filled,
           bool_or(source_dataset = 'eco2mix-national-cons-def'
                   AND consommation IS NOT NULL) AS consolidated_filled
    FROM raw.eco2mix
    WHERE source_dataset IN ('eco2mix-national-tr', 'eco2mix-national-cons-def')
    GROUP BY value_time
), periods AS (
    SELECT *, CASE
        WHEN value_time AT TIME ZONE 'Europe/Paris' < timestamp '2026-01-01'
        THEN to_char(value_time AT TIME ZONE 'Europe/Paris', 'YYYY')
        ELSE to_char(value_time AT TIME ZONE 'Europe/Paris', 'YYYY-MM')
    END AS period
    FROM vintages
)
SELECT coalesce(period, 'ALL') AS paris_period,
       count(*) FILTER (WHERE realtime) AS realtime_timestamps,
       count(*) FILTER (WHERE consolidated) AS consolidated_timestamps,
       count(*) FILTER (WHERE realtime AND consolidated) AS overlap_timestamps,
       count(*) FILTER (WHERE realtime_filled AND consolidated_filled)
           AS filled_overlap_timestamps,
       count(*) FILTER (WHERE realtime AND consolidated
                        AND extract(minute FROM value_time) IN (0, 30))
           AS half_hour_overlap_timestamps
FROM periods
GROUP BY GROUPING SETS ((period), ())
ORDER BY period NULLS LAST;
