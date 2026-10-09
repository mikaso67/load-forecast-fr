-- Q4: Conflicting originals at the same source UTC time, aggregated by Paris year.
WITH duplicates AS (
    SELECT source_dataset, value_time, count(*) AS originals
    FROM raw.eco2mix GROUP BY source_dataset, value_time HAVING count(*) > 1
)
SELECT source_dataset,
       extract(year FROM value_time AT TIME ZONE 'Europe/Paris')::int AS year,
       min(value_time) AS first_duplicate_utc, max(value_time) AS last_duplicate_utc,
       count(*) AS conflicting_timestamps, sum(originals) AS original_records
FROM duplicates GROUP BY source_dataset, year ORDER BY source_dataset, year;

-- Q5: Stored bounds and latest consumption age at first ingestion of that value.
WITH bounds AS (
    SELECT source_dataset, count(*) AS raw_rows,
           count(DISTINCT value_time) AS unique_timestamps,
           min(value_time) AS first_utc, max(value_time) AS last_utc,
           max(value_time) FILTER (WHERE consommation IS NOT NULL) AS latest_consumption_utc
    FROM raw.eco2mix GROUP BY source_dataset
)
SELECT b.*, min(r.ingested_at) AS latest_consumption_first_ingested_utc,
       min(r.ingested_at) - b.latest_consumption_utc AS age_at_ingestion
FROM bounds b JOIN raw.eco2mix r ON r.source_dataset = b.source_dataset
     AND r.value_time = b.latest_consumption_utc AND r.consommation IS NOT NULL
GROUP BY b.source_dataset, b.raw_rows, b.unique_timestamps,
         b.first_utc, b.last_utc, b.latest_consumption_utc
ORDER BY b.source_dataset;

-- Q6: Reserved HTTP attempts, including failures and retries.
SELECT date_trunc('month', attempted_at, 'UTC') AS month_utc, count(*) AS reserved_calls
FROM raw.eco2mix_api_calls GROUP BY month_utc ORDER BY month_utc;
