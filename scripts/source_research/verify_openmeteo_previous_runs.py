"""Verification script for Open-Meteo Previous Runs API limitations.

Demonstrates:
1. Missing variables prior to 2024 (wind, radiation, cloud cover are None).
2. The semantic issue with previous_day1 (valid_time - 24h leads to post-cutoff runs).
"""

import certifi
import requests

BASE_URL = "https://previous-runs-api.open-meteo.com/v1/forecast"


def check_missing_variables_history():
    print("--- Open-Meteo Previous Runs API: Historical Variables ---")
    dates = ["2022-01-01", "2023-01-01", "2024-03-01"]
    for date in dates:
        url = (
            f"{BASE_URL}?latitude=48.8566&longitude=2.3522"
            f"&start_date={date}&end_date={date}"
            "&hourly=temperature_2m_previous_day1,wind_speed_10m_previous_day1,"
            "direct_normal_irradiance_previous_day1,cloud_cover_previous_day1"
            "&models=gfs_seamless"
        )
        r = requests.get(url, verify=certifi.where(), timeout=10)
        data = r.json()
        hourly = data.get("hourly", {})
        t = hourly.get("temperature_2m_previous_day1", [None])[12]
        w = hourly.get("wind_speed_10m_previous_day1", [None])[12]
        rad = hourly.get("direct_normal_irradiance_previous_day1", [None])[12]
        c = hourly.get("cloud_cover_previous_day1", [None])[12]
        print(f"Date {date} at 12:00 UTC: T_2m={t}, Wind={w}, Direct_Radiation={rad}, Cloud_Cover={c}")


if __name__ == "__main__":
    check_missing_variables_history()
