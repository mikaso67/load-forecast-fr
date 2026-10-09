"""Measure one delivery day's collector payload using S3 listings and indexes only."""

import argparse
import json
from datetime import UTC, date, datetime

import requests

from load_forecast.cutoff import forecast_cutoff
from load_forecast.weather import measure_run_volume, select_run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("delivery_day", type=date.fromisoformat)
    args = parser.parse_args()
    if datetime.now(UTC) < forecast_cutoff(args.delivery_day):
        parser.error("Delivery day's cutoff has not arrived")
    with requests.Session() as session:
        run, objects, _ = select_run(session, args.delivery_day)
        if run is None:
            parser.error("Neither candidate run is complete")
        volume = measure_run_volume(session, run, objects)
    print(json.dumps({"delivery_day": args.delivery_day.isoformat(),
                      "run_init_time": run.init_time.isoformat(), "fallback": run.fallback,
                      **volume}, sort_keys=True))


if __name__ == "__main__":
    main()
