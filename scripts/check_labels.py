"""reference recomputation or read-only verification after the developer load."""

import argparse
from datetime import UTC, date, datetime

import pandas as pd
import requests

from load_forecast.eco2mix import connect_database
from load_forecast.labels import daily_labels, fetch_archive, parse_archive, read_labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", action="store_true")
    parser.add_argument("--inspect-years", type=int, nargs="+")
    args = parser.parse_args()
    if args.inspect_years:
        if args.database:
            parser.error("Choose either archive inspection or database verification")
        with requests.Session() as session:
            for year in args.inspect_years:
                rows = parse_archive(fetch_archive(session, year), year, datetime.now(UTC))
                times = pd.DatetimeIndex([row[1] for row in rows])
                observations = pd.DataFrame([row[:3] for row in rows],
                                            columns=["station_id", "value_time", "temperature_k"])
                distinct = observations.drop_duplicates()
                conflicting = distinct.duplicated(["station_id", "value_time"], keep=False)
                conflict_count = len(distinct.loc[conflicting].drop_duplicates(
                    ["station_id", "value_time"],
                ))
                print(f"archive={year} selected_rows={len(rows)} "
                      f"stations={len({row[0] for row in rows})} "
                      f"utc_hours={sorted(set(times.hour))} "
                      f"off_hour_reports={int(((times.minute != 0) | (times.second != 0)).sum())} "
                      f"conflicting_station_times={conflict_count}")
        return
    if args.database:
        with connect_database() as connection:
            frame = read_labels(connection, date(2022, 1, 1), date(2024, 1, 1))
            for row in connection.execute("""
                SELECT extract(year FROM value_time AT TIME ZONE 'UTC')::int AS year,
                       count(*) AS stored_versions,
                       count(DISTINCT (station_id, value_time)) AS station_times
                FROM labels.synop GROUP BY 1 ORDER BY 1
            """):
                print(f"year={row[0]} stored_versions={row[1]} station_times={row[2]}")
    else:
        rows = []
        with requests.Session() as session:
            for year in (2022, 2023):
                batch = parse_archive(fetch_archive(session, year), year, datetime.now(UTC))
                rows.extend(row[:3] for row in batch)
                print(f"archive={year} selected_rows={len(batch)}")
        observations = pd.DataFrame(rows, columns=["station_id", "value_time", "temperature_k"])
        frame = daily_labels(observations, date(2022, 1, 1), date(2024, 1, 1))
    labelled = int(frame.cold.notna().sum())
    cold = int(frame.cold.sum())
    print(f"labelled={labelled} missing={len(frame) - labelled} cold={cold}")
    print(f"reference_p10_c={frame.national_temperature_c.quantile(0.1):.6f}")
    if (labelled, cold) != (725, 73):
        raise SystemExit("Reference counts differ from D9; review source revisions and coverage")


if __name__ == "__main__":
    main()
