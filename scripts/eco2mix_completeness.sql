-- Q1: Filled values among retained original source records, including conflicting
-- timestamps and distinct revisions. Q2 separately measures unique grid coverage.
-- Paris calendar years; 30 min means on-grid values, never interpolation.
WITH grids AS (
    SELECT *, 15 AS step_minutes FROM raw.eco2mix
    UNION ALL
    SELECT *, 30 AS step_minutes FROM raw.eco2mix
    WHERE extract(minute FROM value_time AT TIME ZONE 'UTC')::int % 30 = 0
)
SELECT source_dataset, extract(year FROM value_time AT TIME ZONE 'Europe/Paris')::int
       AS year, step_minutes, count(*) AS rows,
       count(consommation) AS consumption_filled,
       round(100.0 * count(consommation) / count(*), 3) AS consumption_pct,
       count(prevision_j1) AS d1_filled,
       round(100.0 * count(prevision_j1) / count(*), 3) AS d1_pct,
       count(prevision_j) AS d_filled,
       round(100.0 * count(prevision_j) / count(*), 3) AS d_pct
FROM grids
GROUP BY source_dataset, year, step_minutes
ORDER BY source_dataset, year, step_minutes;
