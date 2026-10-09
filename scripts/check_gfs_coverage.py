"""EXP-003 precondition: GFS selection and TMP coverage for every 2021-03-24..2024-12-31 day."""

from load_forecast.eco2mix import connect_database

QUERIES = {
    'status by manifest': """SELECT status, COUNT(*), MIN(delivery_day), MAX(delivery_day)
        FROM raw.gfs_days WHERE delivery_day BETWEEN '2021-03-24' AND '2024-12-31'
        GROUP BY status""",
    'distinct days (expected 1379)': """SELECT COUNT(DISTINCT delivery_day) FROM raw.gfs_days
        WHERE delivery_day BETWEEN '2021-03-24' AND '2024-12-31'""",
    'days whose run has no TMP rows': """SELECT COUNT(*) FROM raw.gfs_days d
        WHERE d.delivery_day BETWEEN '2021-03-24' AND '2024-12-31'
        AND d.status IN ('primary','fallback') AND NOT EXISTS (
            SELECT 1 FROM raw.gfs_points p
            WHERE p.run_init_time = d.run_init_time AND p.variable = 'TMP')""",
}

with connect_database() as connection:
    print(connection.execute('SELECT current_user').fetchone()[0])
    for name, query in QUERIES.items():
        print(name, connection.execute(query).fetchall())

    gaps = connection.execute("""SELECT to_char(delivery_day, 'YYYY-MM') m, COUNT(*) FROM raw.gfs_days d
        WHERE d.delivery_day BETWEEN '2021-03-24' AND '2024-12-31'
        AND d.status IN ('primary','fallback') AND NOT EXISTS (
            SELECT 1 FROM raw.gfs_points p
            WHERE p.run_init_time = d.run_init_time AND p.variable = 'TMP')
        GROUP BY 1 ORDER BY 1""").fetchall()
    print('days without TMP by month', gaps)
