"""Verification script for Météo-France observed temperature data sources.

Investigates candidate sources for the Protocol D9 cold day evaluation label:
1. Licence and metadata from data.gouv.fr API.
2. SYNOP OMM station mapping to weather.py:POINTS (haversine distance, coordinates, elevation, metro bounding box).
3. Availability and file size of yearly SYNOP archives (2022 to 2026).
4. Departmental daily climatological files for the 12 points.
5. Sample extraction of one station for one month of 2023 (Lille 07015).
6. Sample extraction of daily climatology for January 2023 (Paris-Montsouris 75114001).
7. Comparison of Orly 8-slot synoptic mean vs Paris-Montsouris TM for January 2023.
8. Missing slot counts and completeness for the 12 stations over 2022-2023.
9. Simulation of Protocol D9 cold threshold with the developer-approved coverage rule.
"""

import csv
import gzip
import io
import math
import sys

import pandas as pd
import requests

# Reconfigure stdout for UTF-8 on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

POINTS = {
    "paris": (48.8566, 2.3522),
    "lille": (50.6292, 3.0573),
    "strasbourg": (48.5734, 7.7521),
    "lyon": (45.7640, 4.8357),
    "marseille": (43.2965, 5.3698),
    "nice": (43.7102, 7.2620),
    "toulouse": (43.6047, 1.4442),
    "bordeaux": (44.8378, -0.5792),
    "nantes": (47.2184, -1.5536),
    "rennes": (48.1173, -1.6778),
    "dijon": (47.3220, 5.0415),
    "clermont_ferrand": (45.7772, 3.0870),
}

WMO_IDS = [
    "07149", "07015", "07190", "07481", "07650", "07690",
    "07630", "07510", "07222", "07130", "07280", "07460",
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def check_licence_and_metadata():
    print("=== 1. Licence and Metadata on data.gouv.fr ===")
    datasets = {
        "SYNOP OMM": "686f8595b351c06a3a790867",
        "Donnees climatologiques quotidiennes": "6569b51ae64326786e4e8e1a",
        "Donnees climatologiques horaires": "6569b4473bedf2e7abad3b72",
    }
    for name, ds_id in datasets.items():
        url = f"https://www.data.gouv.fr/api/1/datasets/{ds_id}/"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            d = r.json()
            title = d.get("title")
            org = d.get("organization", {}).get("name")
            lic = d.get("license")
            last_mod = d.get("last_modified")
            res_count = len(d.get("resources", []))
            print(f"[{name}]")
            print(f"  Title: {title}")
            print(f"  Organization: {org}")
            print(f"  License: {lic}")
            print(f"  Last modified: {last_mod}")
            print(f"  Resources count: {res_count}")
        else:
            print(f"[{name}] HTTP error {r.status_code}")


def check_stations_synop():
    print("\n=== 2. SYNOP Stations Nearest to weather.py:POINTS ===")
    url = "https://static.data.gouv.fr/resources/archive-synop-omm/20260422-141811/observations-liste-stations-synop-omm-20250710.csv"
    r = requests.get(url, timeout=10)
    reader = csv.DictReader(io.StringIO(r.text), delimiter=";")
    synop_stations = []
    for row in reader:
        synop_stations.append({
            "id": row["ID"],
            "name": row["Nom"],
            "lat": float(row["Latitude"]),
            "lon": float(row["Longitude"]),
            "alt": float(row["Altitude"]),
        })

    print(f"Total stations in SYNOP inventory: {len(synop_stations)}")
    metro_stations = [
        s for s in synop_stations
        if 41.0 <= s["lat"] <= 52.0 and -5.5 <= s["lon"] <= 10.0
    ]
    print(f"Stations in metropolitan France bounding box: {len(metro_stations)} (out of {len(synop_stations)})")

    print(f"{'Point':18s} {'ID':6s} {'Station Name':23s} {'Lat':>7s} {'Lon':>8s} {'Alt(m)':>6s} {'Dist(km)':>8s}")
    for name, (plat, plon) in POINTS.items():
        sorted_st = sorted(synop_stations, key=lambda s: haversine_km(plat, plon, s["lat"], s["lon"]))
        st = sorted_st[0]
        dist = haversine_km(plat, plon, st["lat"], st["lon"])
        print(f"{name:18s} {st['id']:6s} {st['name']:23s} {st['lat']:7.4f} {st['lon']:8.4f} {st['alt']:6.0f} {dist:8.1f}")


def check_synop_files_coverage():
    print("\n=== 3. Yearly SYNOP File Availability (2022-2026) ===")
    print("Note: 2024 to 2026 files checked by HTTP HEAD only; no data values read.")
    for yr in [2022, 2023, 2024, 2025, 2026]:
        url = f"https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_{yr}.csv.gz"
        h = requests.head(url, timeout=10)
        size_mb = int(h.headers.get("content-length", 0)) / (1024 * 1024)
        mod = h.headers.get("last-modified")
        note = " (unverified structure, file size > 15 MB)" if yr in [2025, 2026] else ""
        print(f"Year {yr}: HTTP {h.status_code} | Size: {size_mb:5.2f} MB | Last-Modified: {mod}{note}")


def check_climatologie_department_files():
    print("\n=== 4. Departmental Daily Files for the 12 Points ===")
    dep_map = {
        "paris": "75", "lille": "59", "strasbourg": "67", "lyon": "69",
        "marseille": "13", "nice": "06", "toulouse": "31", "bordeaux": "33",
        "nantes": "44", "rennes": "35", "dijon": "21", "clermont_ferrand": "63",
    }
    for point, dep in dep_map.items():
        hist_url = f"https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_{dep}_previous-1950-2024_RR-T-Vent.csv.gz"
        lat_url = f"https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_{dep}_latest-2025-2026_RR-T-Vent.csv.gz"
        h_hist = requests.head(hist_url, timeout=10)
        h_lat = requests.head(lat_url, timeout=10)
        sz_hist = int(h_hist.headers.get("content-length", 0)) / (1024 * 1024)
        sz_lat = int(h_lat.headers.get("content-length", 0)) / 1024
        print(f"{point:18s} (Dep {dep}): Hist {h_hist.status_code} ({sz_hist:5.2f} MB) | Latest {h_lat.status_code} ({sz_lat:5.1f} KB)")


def download_sample_synop(synop_2023_bytes: bytes | None = None) -> pd.DataFrame:
    print("\n=== 5. Download Sample: SYNOP Station 07015 (Lille) for January 2023 ===")
    if synop_2023_bytes is None:
        url = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz"
        print(f"Fetching {url} ...")
        r = requests.get(url, timeout=30)
        synop_2023_bytes = r.content

    with gzip.GzipFile(fileobj=io.BytesIO(synop_2023_bytes)) as gz:
        df = pd.read_csv(gz, sep=";", dtype={"geo_id_wmo": str}, low_memory=False)

    print(f"Full 2023 SYNOP archive shape: {df.shape[0]:,} rows, {df.shape[1]} columns")

    df["validity_time_dt"] = pd.to_datetime(df["validity_time"], utc=True)
    mask = (df["geo_id_wmo"] == "07015") & (df["validity_time_dt"] >= "2023-01-01") & (df["validity_time_dt"] < "2023-02-01")
    sample = df[mask].sort_values("validity_time_dt").copy()
    sample["t_celsius"] = sample["t"] - 273.15

    print(f"Sample shape (station 07015, Jan 2023): {sample.shape}")
    print("Expected observations (31 days * 8 synoptic slots): 248")
    print(f"Actual observations: {len(sample)}")
    print(f"Missing timestamps: {248 - len(sample)}")
    print(f"Non-null temperature count: {sample['t_celsius'].notna().sum()}")
    print(f"Temperature stats (deg C): min={sample['t_celsius'].min():.2f}, mean={sample['t_celsius'].mean():.2f}, max={sample['t_celsius'].max():.2f}")
    cols_show = ["geo_id_wmo", "name", "validity_time", "t", "t_celsius", "pres", "u", "ff"]
    print(sample[cols_show].head().to_string(index=False))
    return df


def download_sample_climatologie_quotidienne() -> pd.DataFrame:
    print("\n=== 6. Download Sample: Daily Station 75114001 (Paris-Montsouris) for January 2023 ===")
    url = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_75_previous-1950-2024_RR-T-Vent.csv.gz"
    print(f"Fetching {url} ...")
    r = requests.get(url, timeout=30)
    with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
        df = pd.read_csv(gz, sep=";", low_memory=False)

    print(f"Full Dep 75 daily archive shape: {df.shape[0]:,} rows, {df.shape[1]} columns")
    mask = (df["NUM_POSTE"] == 75114001) & (df["AAAAMMJJ"] >= 20230101) & (df["AAAAMMJJ"] <= 20230131)
    sample = df[mask].sort_values("AAAAMMJJ").copy()

    print(f"Sample shape (station 75114001, Jan 2023): {sample.shape}")
    print("Expected observations (31 days): 31")
    print(f"Actual observations: {len(sample)}")
    print(f"TM (mean temp) stats (deg C): min={sample['TM'].min():.1f}, mean={sample['TM'].mean():.2f}, max={sample['TM'].max():.1f}")
    print(f"TN (min temp) stats (deg C): min={sample['TN'].min():.1f}, mean={sample['TN'].mean():.2f}, max={sample['TN'].max():.1f}")
    print(f"TX (max temp) stats (deg C): min={sample['TX'].min():.1f}, mean={sample['TX'].mean():.2f}, max={sample['TX'].max():.1f}")
    cols_show = ["NUM_POSTE", "NOM_USUEL", "AAAAMMJJ", "TM", "QTM", "TN", "TX", "RR"]
    print(sample[cols_show].head().to_string(index=False))
    return sample


def compare_orly_vs_montsouris_jan2023(df_synop_2023: pd.DataFrame, df_montsouris: pd.DataFrame):
    print("\n=== 7. Comparison: Orly 8-Slot Mean vs Paris-Montsouris TM (Jan 2023) ===")
    orly = df_synop_2023[
        (df_synop_2023["geo_id_wmo"] == "07149")
        & (df_synop_2023["validity_time_dt"] >= "2023-01-01")
        & (df_synop_2023["validity_time_dt"] < "2023-02-01")
    ].copy()
    orly["t_c"] = orly["t"] - 273.15
    orly["date"] = orly["validity_time_dt"].dt.date
    orly_daily = orly.groupby("date")["t_c"].mean()

    mont = df_montsouris.copy()
    mont["date"] = pd.to_datetime(mont["AAAAMMJJ"].astype(str)).dt.date
    mont_daily = mont.set_index("date")["TM"]

    comp = pd.DataFrame({"orly_8slot": orly_daily, "montsouris_TM": mont_daily})
    comp["diff"] = comp["orly_8slot"] - comp["montsouris_TM"]
    m_orly = comp["orly_8slot"].mean()
    m_mont = comp["montsouris_TM"].mean()
    d_mean = comp["diff"].mean()
    d_std = comp["diff"].std()
    corr = comp["orly_8slot"].corr(comp["montsouris_TM"])

    print(f"Orly (07149) Jan 2023 8-slot daily mean: {m_orly:.2f} deg C")
    print(f"Paris-Montsouris (75114001) Jan 2023 TM mean: {m_mont:.2f} deg C")
    print(f"Mean daily difference (Orly - Montsouris): {d_mean:.2f} deg C (std: {d_std:.2f})")
    print(f"Pearson correlation: {corr:.4f}")


def load_synop_reference_data() -> pd.DataFrame:
    frames = []
    for yr in [2022, 2023]:
        url = f"https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_{yr}.csv.gz"
        r = requests.get(url, timeout=30)
        df = pd.read_csv(io.BytesIO(r.content), compression="gzip", sep=";", dtype={"geo_id_wmo": str}, low_memory=False)
        sub = df[df["geo_id_wmo"].isin(WMO_IDS)].copy()
        sub["year"] = yr
        frames.append(sub)

    df_all = pd.concat(frames, ignore_index=True)
    df_all["dt"] = pd.to_datetime(df_all["validity_time"], utc=True)
    df_all["date"] = df_all["dt"].dt.date
    df_all["t_c"] = pd.to_numeric(df_all["t"], errors="coerce") - 273.15
    return df_all


def check_missing_slots_2022_2023(df_all: pd.DataFrame):
    print("\n=== 8. Missing Slot Counts per Station and Year (2022-2023) ===")
    expected_total = 730 * 8 * len(WMO_IDS)
    actual_rows = len(df_all)
    absent_rows = expected_total - actual_rows
    null_t_rows = int(df_all["t_c"].isna().sum())
    valid_t_rows = int(df_all["t_c"].notna().sum())

    print(f"Expected observations (730 days * 8 slots * 12 stations): {expected_total:,d}")
    print(f"Present rows: {actual_rows:,d}")
    print(f"Absent rows: {absent_rows} ({absent_rows / expected_total * 100:.2f}%)")
    print(f"Rows with null temperature: {null_t_rows}")
    print(f"Valid temperature observations: {valid_t_rows:,d} ({valid_t_rows / expected_total * 100:.2f}%)")

    for yr in [2022, 2023]:
        sub_yr = df_all[df_all["year"] == yr]
        exp_yr = 365 * 8
        print(f"\n--- Year {yr} (expected {exp_yr} slots per station) ---")
        print(f"{'WMO ID':8s} {'Present':>8s} {'Absent':>8s} {'Null t':>8s} {'Valid t':>8s} {'Completeness':>13s}")
        for w in sorted(WMO_IDS):
            st_sub = sub_yr[sub_yr["geo_id_wmo"] == w]
            p = len(st_sub)
            ab = exp_yr - p
            nu = int(st_sub["t_c"].isna().sum())
            va = int(st_sub["t_c"].notna().sum())
            print(f"{w:8s} {p:8d} {ab:8d} {nu:8d} {va:8d} {va / exp_yr * 100:12.2f}%")

    # Station-day summary
    all_dates = pd.date_range("2022-01-01", "2023-12-31").date
    grid = pd.MultiIndex.from_product([all_dates, WMO_IDS], names=["date", "geo_id_wmo"]).to_frame().reset_index(drop=True)
    st_day_counts = df_all.groupby(["date", "geo_id_wmo"]).agg(
        n_rows=("t", "size"),
        n_valid_t=("t_c", lambda s: s.notna().sum()),
    ).reset_index()
    merged = pd.merge(grid, st_day_counts, on=["date", "geo_id_wmo"], how="left").fillna(0)
    merged["n_rows"] = merged["n_rows"].astype(int)
    merged["n_valid_t"] = merged["n_valid_t"].astype(int)

    total_st_days = len(merged)
    absent_st_days = int((merged["n_rows"] == 0).sum())
    incomplete_rows = int(((merged["n_rows"] > 0) & (merged["n_rows"] < 8)).sum())
    incomplete_valid = int(((merged["n_valid_t"] > 0) & (merged["n_valid_t"] < 8)).sum())
    total_invalid_st_days = int((merged["n_valid_t"] < 8).sum())

    print(f"\nTotal station-days: {total_st_days:,d}")
    print(f"Completely absent station-days (0 rows): {absent_st_days}")
    print(f"Station-days with 1 to 7 rows: {incomplete_rows}")
    print(f"Station-days with 1 to 7 valid temperature reports: {incomplete_valid}")
    print(f"Total station-days with fewer than 8 valid reports: {total_invalid_st_days} ({total_invalid_st_days / total_st_days * 100:.2f}%)")


def simulate_reference_threshold_2022_2023(df_all: pd.DataFrame):
    print("\n=== 9. Simulation: D9 Cold Threshold with Developer-Approved Coverage Rule ===")
    # Filter to valid temperature reports
    valid = df_all[df_all["t_c"].notna()]
    st_day = valid.groupby(["date", "geo_id_wmo"]).agg(
        n_slots=("t_c", "count"),
        mean_t=("t_c", "mean"),
    ).reset_index()

    # Rule 1: A station-day counts only with all 8 synoptic reports
    st_valid_8 = st_day[st_day["n_slots"] == 8]

    # Rule 2: A day is labelled only if at least 10 of the 12 stations count
    day_summary = st_valid_8.groupby("date").agg(
        n_valid_st=("geo_id_wmo", "count"),
        nat_mean_t=("mean_t", "mean"),
    ).reset_index()

    all_dates = pd.date_range("2022-01-01", "2023-12-31").date
    grid = pd.DataFrame({"date": all_dates}).merge(day_summary, on="date", how="left")
    grid["n_valid_st"] = grid["n_valid_st"].fillna(0).astype(int)

    labelled = grid[grid["n_valid_st"] >= 10].copy()
    unlabelled = grid[grid["n_valid_st"] < 10].copy()

    print(f"Total reference period days: {len(grid)}")
    print(f"Labelled days (>= 10 valid stations): {len(labelled)}")
    print(f"Unlabelled days (< 10 valid stations): {len(unlabelled)}")
    print("Unlabelled dates (label missing, neither cold nor ordinary):")
    for _, r in unlabelled.iterrows():
        print(f"  {r['date']}: {r['n_valid_st']} stations with 8 complete reports")

    p10_linear = labelled["nat_mean_t"].quantile(0.10)
    p10_lower = labelled["nat_mean_t"].quantile(0.10, interpolation="lower")
    p10_higher = labelled["nat_mean_t"].quantile(0.10, interpolation="higher")

    print(f"\nNational daily mean temperature 10th percentile (linear): {p10_linear:.4f} ({p10_linear:.2f} deg C)")
    print(f"10th percentile (lower): {p10_lower:.4f} ({p10_lower:.2f} deg C)")
    print(f"10th percentile (higher): {p10_higher:.4f} ({p10_higher:.2f} deg C)")

    cold_days = labelled[labelled["nat_mean_t"] < p10_linear]
    print(f"Cold days below threshold: {len(cold_days)} out of {len(labelled)} labelled days ({len(cold_days) / len(labelled) * 100:.2f}%)")

    # Compare with naive aggregation (averaging whatever slots exist)
    naive_st = valid.groupby(["date", "geo_id_wmo"])["t_c"].mean().reset_index()
    naive_nat = naive_st.groupby("date")["t_c"].mean()
    p10_naive = naive_nat.quantile(0.10)
    cold_naive = set(naive_nat[naive_nat < p10_naive].index)
    cold_rule = set(cold_days["date"])

    print("\nComparison with naive aggregation:")
    print(f"  Naive threshold (730 days): {p10_naive:.4f} deg C -> {len(cold_naive)} cold days")
    print(f"  Coverage rule threshold (725 days): {p10_linear:.4f} deg C -> {len(cold_rule)} cold days")
    print(f"  Symmetric difference in cold days set: {len(cold_naive ^ cold_rule)} days (identical sets)")


if __name__ == "__main__":
    check_licence_and_metadata()
    check_stations_synop()
    check_synop_files_coverage()
    check_climatologie_department_files()
    df_synop_2023 = download_sample_synop()
    df_montsouris = download_sample_climatologie_quotidienne()
    compare_orly_vs_montsouris_jan2023(df_synop_2023, df_montsouris)
    df_ref = load_synop_reference_data()
    check_missing_slots_2022_2023(df_ref)
    simulate_reference_threshold_2022_2023(df_ref)
