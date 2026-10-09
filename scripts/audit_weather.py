"""Read-only GFS selection and point counts, replacing the local helper."""

from load_forecast.eco2mix import connect_database


def main() -> None:
    with connect_database() as connection, connection.transaction():
        connection.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        selections = connection.execute('SELECT status, count(*) FROM raw.gfs_days '
                                       'GROUP BY status ORDER BY status').fetchall()
        points = connection.execute('SELECT count(*) FROM raw.gfs_points').fetchone()[0]
        exceptions = connection.execute("SELECT delivery_day, status, original_metadata "
                                        "FROM raw.gfs_days WHERE status <> 'primary' "
                                        "ORDER BY delivery_day").fetchall()
    print('Selections:', selections)
    print('Points:', points)
    for day, status, metadata in exceptions:
        print(day, status, {run: len(keys) for run, keys in metadata['failures'].items()})


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Read-only weather audit failed; diagnostics suppressed') from None
