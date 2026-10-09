-- Q3: Missing timestamp blocks over the requested date range, both sources combined.
-- Bind start/end UTC instants with psycopg; the end is exclusive.
WITH grid AS (
    SELECT generate_series(%(start)s::timestamptz,
                           %(end)s::timestamptz - interval '15 minutes',
                           interval '15 minutes') AS t
), missing AS (
    SELECT t, t - row_number() OVER (ORDER BY t) * interval '15 minutes' AS block
    FROM grid WHERE NOT EXISTS (SELECT 1 FROM raw.eco2mix WHERE value_time = t)
)
SELECT min(t) AS first_missing_utc, max(t) AS last_missing_utc, count(*) AS missing_points
FROM missing GROUP BY block ORDER BY first_missing_utc;
