"""Verification script for NOAA GFS weather source on AWS S3.

Checks:
1. Bucket layout and archive start date (2021-03-23 for GFS 0.25 atmospheric data).
2. Presence of all six core variables across key historical dates.
3. Radiation (DSWRF) 6-hour reset, f021 lead requirement, and TCDC instantaneous line selection.
4. Publication timestamps (LastModified) for 00 UTC and 06 UTC runs across seasons.
5. Byte-range partial retrieval of 2m temperature via .idx parsing.
"""

import xml.etree.ElementTree as ET

import certifi
import requests

S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def check_archive_start():
    print("--- 1. Archive Start Date and Layout Transition ---")
    dates = ["20210101", "20210322", "20210323"]
    for d in dates:
        url = f"{S3_BASE}/?prefix=gfs.{d}/00/&max-keys=5"
        r = requests.get(url, verify=certifi.where(), timeout=10)
        root = ET.fromstring(r.text)
        keys = [c.find("s3:Key", NS).text for c in root.findall("s3:Contents", NS)]
        print(f"Date {d}: HTTP {r.status_code}")
        for k in keys[:3]:
            print(f"  sample key: {k}")


def check_six_variables():
    print("\n--- 2. Six Core Variables Across Sample Dates ---")
    dates_steps = [
        ("20210323", "f024"),
        ("20220101", "f030"),
        ("20230715", "f030"),
        ("20250115", "f030"),
    ]
    vars_needed = [
        ":TMP:2 m above ground:",
        ":RH:2 m above ground:",
        ":UGRD:10 m above ground:",
        ":VGRD:10 m above ground:",
        ":DSWRF:surface:",
        ":TCDC:entire atmosphere:",
    ]
    for d, step in dates_steps:
        url = f"{S3_BASE}/gfs.{d}/00/atmos/gfs.t00z.pgrb2.0p25.{step}.idx"
        r = requests.get(url, verify=certifi.where(), timeout=10)
        lines = r.text.strip().split("\n")
        print(f"Date {d} Step {step} (HTTP {r.status_code}, {len(lines)} total records):")
        for v in vars_needed:
            matches = [line for line in lines if v in line]
            print(f"  {v} -> {len(matches)} match(es)")
            for m in matches:
                parts = m.split(":")
                print(f"    line {parts[0]}: {parts[3]} ({parts[4]} / {parts[5]})")


def check_dswrf_and_tcdc_semantics():
    print("\n--- 3. Radiation (DSWRF) Reset and Cloud Cover (TCDC) Selection ---")
    steps = ["f021", "f022", "f023", "f024", "f025", "f026"]
    for step in steps:
        url = f"{S3_BASE}/gfs.20230715/00/atmos/gfs.t00z.pgrb2.0p25.{step}.idx"
        r = requests.get(url, verify=certifi.where(), timeout=10)
        lines = r.text.strip().split("\n")
        dswrf = [line for line in lines if ":DSWRF:surface:" in line]
        tcdc = [line for line in lines if ":TCDC:entire atmosphere:" in line]
        print(f"Step {step}:")
        for m in dswrf:
            parts = m.split(":")
            print(f"  DSWRF: line {parts[0]} -> {parts[3]} ({parts[4]} / {parts[5]})")
        for m in tcdc:
            parts = m.split(":")
            print(f"  TCDC:  line {parts[0]} -> {parts[3]} ({parts[4]} / {parts[5]})")


def check_publication_delay():
    print("\n--- 4. Publication Delay Verification (00 UTC vs 06 UTC Runs) ---")
    sample_dates = ["20240115", "20240715", "20260301"]
    for d in sample_dates:
        for run, max_step in [("00", "f047"), ("06", "f047")]:
            url = f"{S3_BASE}/?prefix=gfs.{d}/{run}/atmos/gfs.t{run}z.pgrb2.0p25.{max_step}&max-keys=5"
            r = requests.get(url, verify=certifi.where(), timeout=10)
            root = ET.fromstring(r.text)
            for c in root.findall("s3:Contents", NS):
                k = c.find("s3:Key", NS).text
                if not k.endswith(".idx"):
                    lm = c.find("s3:LastModified", NS).text
                    print(f"Date {d} Run {run}z Step {max_step}: Published = {lm}")


def check_byte_range_request():
    print("\n--- 5. Byte-Range Request Verification (.idx to partial GRIB) ---")
    idx_url = f"{S3_BASE}/gfs.20220101/00/atmos/gfs.t00z.pgrb2.0p25.f024.idx"
    r_idx = requests.get(idx_url, verify=certifi.where(), timeout=10)
    lines = r_idx.text.strip().split("\n")

    target_line_idx = None
    for i, line in enumerate(lines):
        if ":TMP:2 m above ground:" in line:
            target_line_idx = i
            break

    if target_line_idx is None:
        print("Variable TMP:2 m above ground not found in .idx file!")
        return

    line_curr = lines[target_line_idx]
    line_next = lines[target_line_idx + 1]
    start_byte = int(line_curr.split(":")[1])
    end_byte = int(line_next.split(":")[1]) - 1

    print(f"Index line: {line_curr}")
    print(f"Byte range: {start_byte}-{end_byte} ({end_byte - start_byte + 1} bytes)")

    grib_url = f"{S3_BASE}/gfs.20220101/00/atmos/gfs.t00z.pgrb2.0p25.f024"
    headers = {"Range": f"bytes={start_byte}-{end_byte}"}
    r_grib = requests.get(grib_url, headers=headers, verify=certifi.where(), timeout=10)
    print(f"Partial GET: HTTP {r_grib.status_code}")
    print(f"Downloaded bytes: {len(r_grib.content)}")
    print(f"Magic header: {r_grib.content[:4]}")
    print(f"Magic trailer: {r_grib.content[-4:]}")


if __name__ == "__main__":
    check_archive_start()
    check_six_variables()
    check_dswrf_and_tcdc_semantics()
    check_publication_delay()
    check_byte_range_request()
