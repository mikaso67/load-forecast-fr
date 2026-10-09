-- Q2: Complete validation year, both sources combined, no model evaluation.
WITH grid AS (
    SELECT generate_series('2024-01-01 00:00 Europe/Paris'::timestamptz,
                           '2025-01-01 00:00 Europe/Paris'::timestamptz
                           - interval '15 minutes', interval '15 minutes') AS t
), observed AS (
    SELECT value_time, bool_or(consommation IS NOT NULL) AS consumption,
           bool_or(prevision_j1 IS NOT NULL) AS d1, bool_or(prevision_j IS NOT NULL) AS d
    FROM raw.eco2mix GROUP BY value_time
), grids AS (
    SELECT *, 15 AS step_minutes FROM grid
    UNION ALL
    SELECT *, 30 AS step_minutes FROM grid
    WHERE extract(minute FROM t AT TIME ZONE 'UTC')::int % 30 = 0
)
SELECT step_minutes, count(*) AS expected,
       count(value_time) AS present,
       count(*) FILTER (WHERE value_time IS NULL) AS missing_timestamps,
       count(*) FILTER (WHERE NOT coalesce(consumption, false)) AS missing_consumption,
       count(*) FILTER (WHERE NOT coalesce(d1, false)) AS missing_d1,
       count(*) FILTER (WHERE NOT coalesce(d, false)) AS missing_d
FROM grids LEFT JOIN observed ON value_time = t
GROUP BY step_minutes ORDER BY step_minutes;
