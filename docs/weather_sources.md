# Weather Forecast Data Sources for Day-Ahead Load Forecasting in France

This document investigates and compares candidate sources of archived numerical weather prediction
(NWP) forecasts for metropolitan France. The primary objective is to select a data source for the
walk-forward day-ahead electricity load backtest (2022-2025) and daily production forecasting.

Under the project evaluation protocol (docs/protocol.md), day-ahead load forecasts for delivery day D
are issued at 12:00 Europe/Paris on day D-1. In accordance with Section 2.1 and the forecast-time
leakage checklist (Item 2), weather features must strictly represent operational weather forecasts
published and available before the 12:00 Europe/Paris cutoff. Posterior observations, reanalyses
(such as ERA5), and forecasts from NWP cycles initialized or published after the cutoff are strictly
forbidden.

---

## 1. Candidate Sources Evaluation

### Candidate 1: NOAA Global Forecast System (GFS) 0.25 Degree on AWS Open Data

- **Provider:** US National Oceanic and Atmospheric Administration (NOAA / NCEP), hosted on AWS
  Open Data.
- **Documentation:**
  - AWS Registry of Open Data: https://registry.opendata.aws/noaa-gfs-bdp-pds/
  - NOAA NCEI Archive: https://www.ncei.noaa.gov/products/weather-climate-models/global-forecast
- **Model:** NOAA GFS 0.25 Degree (atmospheric model, 0.25 x 0.25 degree latitude-longitude grid).
- **Temporal Coverage:**
  - AWS S3 bucket (`s3://noaa-gfs-bdp-pds` / HTTP `https://noaa-gfs-bdp-pds.s3.amazonaws.com`) contains
    archive folders starting from 2021-01-01.
  - However, on the dates checked prior to 2021-03-23 (2021-01-01, 2021-03-22), the bucket contains only WAFS
    aviation products (`WAFS_blended_*.grib2`).
  - The standard operational atmospheric layout (`gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.fFFF`)
    and 1-hourly forecast steps out to f120 begin on **2021-03-23** (operational rollout of GFS version 16.0).
  - This start date precedes the 2022-01-01 start of the training period by more than 9 months.
- **Available Variables Relevant to Load Forecasting:**
  - 2-meter temperature (`TMP:2 m above ground`, instantaneous)
  - 10-meter wind vectors (`UGRD:10 m above ground`, `VGRD:10 m above ground`, instantaneous)
  - 2-meter relative humidity (`RH:2 m above ground`, instantaneous)
  - Downward short-wave solar radiation flux (`DSWRF:surface`, accumulated average resetting every 6 hours)
  - Total cloud cover (`TCDC:entire atmosphere`, instantaneous and averaged lines present)
- **Operational Cycles and Lead Times:**
  - 4 cycles daily at 00, 06, 12, 18 UTC.
  - For the 12:00 Europe/Paris cutoff on D-1, the 00:00 UTC cycle of D-1 is the primary operational run.
  - Required lead steps: `f021` to `f047` (27 hourly files). Step `f021` is required to difference
    the accumulated `DSWRF` radiation flux for step `f022`.
- **Publication Delay and Operational Margins:**
  - S3 `LastModified` timestamps on sample dates (2024-01-15, 2024-07-15, 2026-03-01) show that the 00:00 UTC cycle finishes
    dissemination of lead steps up to `f047` between 03:43 and 03:48 UTC (delay between 3 hours 43 minutes and
    3 hours 48 minutes).
  - Cutoff in UTC: 10:00 UTC in summer (CEST, UTC+2) and 11:00 UTC in winter (CET, UTC+1).
  - The 00:00 UTC run provides an observed operational safety margin of **6 hours 10 minutes** in summer and
    **7 hours 10 minutes** in winter on the tested dates.
- **Evaluation of the D-1 06:00 UTC Run (Rejected Option):**
  - The 06:00 UTC cycle is initialized 6 hours later, offering fresher atmospheric states.
  - Measured S3 dissemination of the 06:00 UTC `f047` step finishes between 09:41 and 09:45 UTC
    (delay of 221 to 225 minutes across the tested dates).
  - In summer (10:00 UTC cutoff), this leaves an observed buffer of only **15 to 19 minutes** before the cutoff
    on the sampled dates (11 to 19 minutes in a wider sample by the reviewer).
  - A 15-minute margin on a public cloud mirror presents significant operational risk: any transient
    network delay or NCEP computation lag would cause missing forecast inputs.
  - Furthermore, using 06 UTC in winter (~75 minute margin) and 00 UTC in summer would create an artificial
    seasonal switch in the data pipeline.
  - Therefore, the 06 UTC run is rejected for the baseline v1 system. It remains a candidate for a future
    controlled one-change experiment (for winter months only).
- **Efficient Extraction via HTTP Byte-Range Requests:**
  - Full-globe GRIB2 files are approximately 540 MB per forecast hour (measured ~536 to 544 MB across sample dates).
  - Each GRIB2 file is paired with an `.idx` index file on S3 listing byte offsets for every variable record.
  - A client fetches the `.idx` file (tens of kilobytes), locates the desired variable line, and issues
    an HTTP GET request with a `Range: bytes=start-end` header.
  - For 2-meter temperature (`TMP:2 m above ground`), downloading one global step requires 877 KB.
  - Measured selected GRIB ranges for all 6 variables across 27 steps total 132.6 to 132.9 MB per
    delivery day, plus 1.1 MB of indexes (two samples, commands and exact bytes in section 7).
  - At about 134 MB per day, 1461 days require approximately 196 GB; the 2022-day archive scan period
    requires approximately 271 GB. These are payload estimates, excluding retries and HTTP overhead. Grid points
    corresponding to French population centres should be extracted and persisted once in PostgreSQL.
- **Operational Fallback Rule for Late or Missing 00 UTC Runs:**
  - If the D-1 00:00 UTC run is not completely disseminated by the 12:00 Europe/Paris cutoff, the ingestion
    pipeline falls back to the D-2 18:00 UTC run (initialized 6 hours prior).
  - The required lead steps for the fallback run are `f027` to `f053`.
  - The pipeline logs a warning, records `run_init_time`, `source_url`, and ingestion timestamp in the
    append-only raw database table, and alerts monitoring.
- **Licence and Cost:**
  - US Public Domain (NOAA Open Data Dissemination policy).
  - Free public access. Supports anonymous HTTP requests without API keys or credentials.
- **Backtest and Production Parity:**
  - Identical data path: backtest and live production query the same S3 bucket using identical `.idx`
    parsing and HTTP range request logic.
  - S3 `LastModified` timestamps on the sampled dates (2024-01-15, 2024-07-15, 2026-03-01) match original operational
    dissemination times on the run date (the arrival times match the run date; contents were not compared).

---

### Candidate 2: Open-Meteo Previous Runs API

- **Provider:** Open-Meteo GmbH
- **Documentation:** https://open-meteo.com/en/docs/previous-runs-api
- **Access Endpoint:** `https://previous-runs-api.open-meteo.com/v1/forecast`
- **Underlying Models:** Multi-model access including NOAA GFS (`models=gfs_seamless`), DWD ICON,
  and ECMWF IFS.
- **Intended Mechanism:**
  - Appends run-offset suffixes to variable names (for example `temperature_2m_previous_day1`,
    `temperature_2m_previous_day2`).
- **Critical Findings and Limitations:**
  1. *Forecast-Time Leakage in `_previous_day1`:*
     The official definition of `_previous_day1` is the forecast generated approximately 24 hours
     before the valid timestamp. For delivery day D evening hours (such as 20:00 Europe/Paris / 19:00 UTC),
     24 hours prior to valid time is D-1 19:00 UTC. The underlying forecast value is therefore sourced
     from the D-1 18:00 UTC GFS run. Because the D-1 18:00 UTC cycle is initialized and published 6 to 8
     hours after the 12:00 Europe/Paris D-1 cutoff, using `_previous_day1` across the full day injects
     post-cutoff NWP runs into evening slots.
  2. *Loss of Fresh Information in `_previous_day2`:*
     To avoid post-cutoff runs, one could fall back to `_previous_day2` (lead time 48 hours before valid
     time). However, this discards the D-1 00:00 UTC run (which is available by 03:50 UTC, well before
     the cutoff) and forces the model to use 48-hour-old weather information, putting it at a major
     disadvantage against RTE D-1.
  3. *Missing Variables Prior to March 2024:*
     Empirical queries against the live Previous Runs API demonstrate that non-temperature variables
     (`wind_speed_10m_previous_day1`, `direct_normal_irradiance_previous_day1`, `cloud_cover_previous_day1`)
     return `null` on the two dates tested prior to March 2024 (2022-01-01 and 2023-01-01). Only 2-meter
     temperature is partially archived back to 2021, and temperature contains data gaps on tested dates
     (such as missing values observed on 2024-01-15). Consequently, this API cannot support the 2022-2023
     training window required by protocol decision D5.
- **Licence and Quotas:**
  - Creative Commons Attribution 4.0 International (CC-BY 4.0).
  - Free tier is restricted to non-commercial use only.
  - Quotas: 600 calls/minute, 5,000 calls/hour, 10,000 calls/day, 300,000 calls/month.
  - API calls are weighted: querying more than 10 variables or more than 14 days consumes multiple call
    credits.

---

### Candidate 3: TIGGE (THORPEX Interactive Grand Global Ensemble)

- **Provider:** ECMWF in collaboration with WMO (World Meteorological Organization).
- **Documentation:**
  - ECMWF TIGGE Portal: https://www.ecmwf.int/en/research/projects/tigge
  - ECMWF Data Services: https://apps.ecmwf.int/datasets/data/tigge/
- **Models:** Ensemble and deterministic control forecasts from 10 global operational centres,
  including ECMWF, NCEP (GFS), Meteo-France (ARPEGE), UK Met Office, and JMA.
- **Temporal Coverage:** Continuous archive from October 2006 to present.
- **Variables Relevant to Load Forecasting:** 2-meter temperature, 10-meter wind components, surface
  solar radiation downwards, total cloud cover, surface pressure.
- **Spatial Resolution:** Native resolution depends on contributing centre; archived at 0.5 or 0.25 degree.
- **Issue Times:** Operational cycles at 00:00 and 12:00 UTC.
- **Licence:** Free for non-commercial research and educational purposes (CC BY 4.0 or CC BY-NC-ND
  depending on the contributing centre; source: https://confluence.ecmwf.int/display/TIGGE; not checked).
- **Critical Limitation for Production:**
  - Under the international TIGGE data policy, data is made available with a mandatory 48-hour delay (source:
    https://confluence.ecmwf.int/display/TIGGE/TIGGE+data+portal; not checked).
  - Because of this 48-hour delay, a forecast run initialized at 00:00 UTC on D-1 is released on D+1,
    making real-time automated production at 12:00 Europe/Paris on D-1 completely impossible.
  - Using TIGGE in backtesting would violate the requirement that backtest and production share the same
    data path. TIGGE serves as an academic benchmark reference only.

---

### Candidate 4: Meteo-France Open Data (AROME & ARPEGE)

- **Provider:** Meteo-France
- **Documentation and Portals:**
  - Open Data Portal: https://meteo.data.gouv.fr/
  - API Portal: https://portail-api.meteofrance.com/
- **Models:**
  - AROME France: convective-scale model at 0.01 degree (~1.1 km) resolution.
  - ARPEGE: synoptic-scale model at 0.1 degree (~10 km) resolution over Europe.
- **Operational Cycles and Lead Times:**
  - AROME runs every 3 hours (00, 03, 06, 09, 12, 15, 18, 21 UTC; not checked).
  - The 00:00 UTC cycle provides lead times up to 48 or 51 hours, which completely reaches 24:00
    Europe/Paris on day D (lead 47 hours in winter; source: Meteo-France documentation; not checked).
  - Publication delay of the 00:00 UTC run is approximately 3 to 4 hours (available around 03:30 to 04:00 UTC; not checked).
- **Licence:** Licence Ouverte / Open Licence version 2.0 (Etalab; not checked), permitting commercial and non-commercial reuse.
- **Critical Limitation for Backtesting:**
  - Public REST and WCS endpoints on `portail-api.meteofrance.com` and `data.gouv.fr` retain only a rolling
    window of recent runs (typically ~14 days of forecast packages; not checked).
  - Multi-year archives of past AROME runs back to 2022 are not accessible through open REST APIs (not checked).
    Retrieving historical runs requires commercial access or massive offline bulk archives.

---

### Candidate 5: ECMWF Open Data on AWS (`s3://ecmwf-forecasts`)

- **Provider:** European Centre for Medium-Range Weather Forecasts (ECMWF)
- **Documentation:**
  - ECMWF Open Data: https://www.ecmwf.int/en/forecasts/datasets/open-data
  - AWS Registry of Open Data: https://registry.opendata.aws/ecmwf-opendata/
- **Model:** ECMWF Integrated Forecasting System (IFS) High Resolution (HRES) 0.25 / 0.4 degree.
- **Temporal Coverage:**
  - The public AWS bucket (`s3://ecmwf-forecasts`) holds historical runs starting from **2023-01-18**
    (`0p4-beta` in 2023, `ifs/` and `aifs/` in 2025). Earlier dates (such as 2022-06-01) are not present.
  - While this provides approximately 3.5 years of archived runs, it does not cover the 2022 training window
    required by protocol decision D5.
- **Licence:** Creative Commons Attribution 4.0 International (CC-BY 4.0).
- **Role in Project:**
  - Due to the absence of 2022 data, ECMWF IFS cannot serve as the primary baseline weather source.
  - It represents a strong candidate for a future one-change model comparison experiment starting in 2023.

---

### Candidate 6: DWD Open Data (ICON-EU)

- **Provider:** Deutscher Wetterdienst (DWD)
- **Documentation:** https://opendata.dwd.de/
- **Model:** ICON-EU (regional European nest, ~6.5 km resolution).
- **Licence:** GeoNutzV (permissive German open data licence, compatible with CC-BY 4.0).
- **Limitation for Backtesting:**
  - The open FTP/HTTP servers maintain only a rolling archive of 24 to 48 hours (documented by DWD; not checked).
    Historical NWP runs are purged daily.

---

### Excluded Source: Open-Meteo Historical Forecast API

- **Endpoint:** `https://historical-forecast-api.open-meteo.com/v1/forecast`
- **Exclusion Reason:**
  - The official Open-Meteo documentation specifies: "Each run's first few hours are stitched into a
    continuous hourly timeseries."
  - For day D, values for each hour are assembled from the nearest operational cycle (lead times 0 to 6
    hours). For example, 18:00 Europe/Paris on day D is taken from the day D 12:00 UTC cycle, which is run
    and published 30 hours after the day D-1 12:00 cutoff.
  - This incorporates near-observed weather information into the feature set and causes massive
    forecast-time leakage. It cannot be used in this project.

---

## 2. Summary Comparison Matrix

| Source / Provider | Temporal Archive | Operational Run Control | Variables Available (2022-2025) | Measured Availability Delay | Production Feasibility | Data Path Parity | Licence & Access Cost |
|---|---|---|---|---|---|---|---|
| **NOAA GFS 0.25 deg (AWS S3)** | 2021-03-23 to present (continuous) | Full control of cycle (00z) and hourly lead steps (f021-f047) | All core variables (T2m, Wind U/V, Solar DSWRF, RH, Cloud) | ~3h 45m (available before 03:50 UTC) | Yes (12:00 D-1 scheduled run) | Complete parity (identical S3 range requests) | US Public Domain, Free, No Auth |
| **Open-Meteo Previous Runs API** | GFS 2m temp from 2021; others March 2024+ | Indirect via previous_dayN offsets; leads to leakage or stale data | Only T2m before 2024 on sample dates; Wind/Solar/Cloud null on sample dates | Provider dependent (undocumented) | Yes | Partial (same API endpoint) | CC-BY 4.0, Non-commercial only, Quotas |
| **TIGGE (ECMWF)** | 2006 to present (not checked) | Controlled cycles (00z, 12z) | Full atmospheric variables | 48-hour mandatory delay (documented, not checked) | No (48h delay rules out D-1 production) | No (different operational path needed) | CC-BY / CC-BY-NC, Free for Research (not checked) |
| **Meteo-France AROME (Direct)** | Rolling ~14 days (not checked) | Controlled cycles (00z, 03z, 06z, etc.) | High-res surface fields (1.1 km) | ~3h 30m to 4h (not checked) | Yes | No (requires different historical source) | Open Licence v2.0, Free key required (not checked) |
| **ECMWF Open Data (AWS S3)** | 2023-01-18 to present | Controlled cycles (00z, 06z, 12z, 18z) | Core surface parameters | ~3h 30m to 4h (not checked) | Yes | Partial (archive starts 2023) | CC-BY 4.0, Free, No Auth |
| **Open-Meteo Historical Forecast** | Continuous from 2021 (documented, not checked) | None (stitches lead 0-6h from all runs) | All core variables | Not applicable (stitched series) | No | No (severe leakage in backtest) | Excluded |

---

## 3. Recommended Source and Operational Specification

### Recommendation: NOAA GFS 0.25 Degree via AWS Open Data (`s3://noaa-gfs-bdp-pds`)

The recommended source for weather forecast data in `load-forecast-fr` is **NOAA GFS 0.25 Degree on AWS
Open Data**, retrieved using HTTP Byte-Range requests against the `.idx` index files.

---

### 3.1 Model Run and Lead Times for Delivery Day D

- **Cutoff Time:** 12:00 Europe/Paris on day D-1.
  - In winter (CET = UTC+1): 11:00 UTC on day D-1.
  - In summer (CEST = UTC+2): 10:00 UTC on day D-1.
- **Selected NWP Cycle:** D-1 00:00 UTC operational cycle (`00z`).
- **Required Lead Times by Calendar Regime:**

  1. *Winter Days (CET, UTC+1, 24 local hours):*
     - Delivery day starts at D 00:00 Europe/Paris = D-1 23:00 UTC (lead `f023`).
     - Delivery day ends at D 24:00 Europe/Paris = D 23:00 UTC (lead `f047`).
     - Required lead steps for instantaneous fields: `f023` to `f047` (25 hourly files).
     - For radiation flux (`DSWRF`), step `f022` is also needed to compute the 1-hour flux for `f023`.

  2. *Summer Days (CEST, UTC+2, 24 local hours):*
     - Delivery day starts at D 00:00 Europe/Paris = D-1 22:00 UTC (lead `f022`).
     - Delivery day ends at D 24:00 Europe/Paris = D 22:00 UTC (lead `f046`).
     - Required lead steps for instantaneous fields: `f022` to `f046` (25 hourly files).
     - For radiation flux (`DSWRF`), step `f021` is also needed to compute the 1-hour flux for `f022`.

  3. *Spring Daylight Saving Transition (23 local hours, last Sunday of March):*
     - D 00:00 Europe/Paris is in CET (D-1 23:00 UTC, lead `f023`).
     - At 02:00 local time, clocks advance to 03:00 (CEST).
     - D 24:00 Europe/Paris is in CEST (D 22:00 UTC, lead `f046`).
     - Required lead steps: `f022` to `f046` (with `f021` for radiation).

  4. *Autumn Daylight Saving Transition (25 local hours, last Sunday of October):*
     - Day D-1 cutoff occurs during CEST (10:00 UTC).
     - D 00:00 Europe/Paris is in CEST (D-1 22:00 UTC, lead `f022`).
     - At 03:00 local time, clocks retreat to 02:00 (CET).
     - D 24:00 Europe/Paris is in CET (D 23:00 UTC, lead `f047`).
     - Required lead steps: `f021` to `f047`.

- **Universal Horizon Rule:**
  Fetching hourly lead steps **`f021` through `f047`** (27 files per run) completely covers every 30-minute
  slot of any delivery day D across the entire calendar, including all daylight saving transition days
  and all accumulated radiation differencing requirements.

#### De-accumulation Rule for Downward Short-Wave Radiation (`DSWRF:surface`)

In GFS GRIB2 output, solar radiation (`DSWRF`) is stored as an average flux (W/m^2) accumulated since the
last 6-hour interval reset (at forecast hours 00, 06, 12, 18, 24, 30, 36, 42, 48).

- Let $t$ be the target forecast step in hours (for example $t = 22$).
- Let $t_{reset} = 6 \cdot \lfloor (t - 1) / 6 \rfloor$ be the previous reset hour (for $t = 22$, $t_{reset} = 18$).
- Let $k = t - t_{reset}$ be the elapsed hours since reset ($k \in \{1, 2, 3, 4, 5, 6\}$).
- Let $A_k$ be the accumulated average reported in file $f_{t}$ (for example `18-22 hour ave fcst`).
- The 1-hour average flux $F_{t-1 \to t}$ is obtained by:
  - If $k = 1$: $F_{t-1 \to t} = A_1$ (single hour average already).
  - If $k > 1$: $F_{t-1 \to t} = k \cdot A_k - (k - 1) \cdot A_{k-1}$.
- Example for summer slot 1 (D-1 22:00 UTC, lead step `f022`):
  $t = 22$, $t_{reset} = 18$, $k = 4$.
  $F_{21 \to 22} = 4 \cdot A_{22} - 3 \cdot A_{21}$.
  Because $A_{21}$ is stored in the `f021` file, step `f021` is mandatory to un-accumulate step `f022`.

#### Cloud Cover (`TCDC:entire atmosphere`) Selection Rule

In GFS `.idx` files, each hourly step contains two records for total cloud cover:
1. Instantaneous forecast: `:TCDC:entire atmosphere:<step> hour fcst:`
2. Averaged forecast: `:TCDC:entire atmosphere:<reset>-<step> hour ave fcst:`

To avoid interval differencing and remain consistent with instantaneous temperature, humidity, and wind
fields, the ingestion pipeline explicitly selects the **instantaneous line** by matching the exact regex
pattern `:TCDC:entire atmosphere:\d+ hour fcst:`.

#### 30-Minute Grid Interpolation Rule

Electricity load is modeled at 30-minute resolution (protocol decision D1). Weather features for intermediate
half-hour slots are computed by linear interpolation between the two adjacent hourly forecast steps belonging
to the same 00 UTC run. No steps from different cycles are mixed.

For interval-averaged variables (such as hourly mean `DSWRF`), the hourly average $F_{t-1 \to t}$ represents
the mean flux over the hour ending at step $t$. Before interpolation to the 30-minute grid, it is timestamped
at $t - 30$ minutes rather than at $t$, preventing a systematic 30-minute phase lead in solar radiation features.

---

### 3.2 Real Publication Delay and Operational Robustness

Publication delay was evaluated using S3 `LastModified` timestamps across multiple seasons.

- **Measured Dissemination Timestamps (00 UTC Cycle, Step `f047`):**
  - Winter (2024-01-15 00:00 UTC run): available at `2024-01-15T03:43:20Z` (delay: 3 hours 43 minutes)
  - Summer (2024-07-15 00:00 UTC run): available at `2024-07-15T03:47:51Z` (delay: 3 hours 48 minutes)
  - Recent (2026-03-01 00:00 UTC run): available at `2026-03-01T03:46:53Z` (delay: 3 hours 47 minutes)
- **Operational Margins:**
  - The 00:00 UTC cycle finishes dissemination before **03:50 UTC** across all tested periods.
  - In summer (cutoff 10:00 UTC), the files are ready **6 hours 10 minutes** before the cutoff.
  - In winter (cutoff 11:00 UTC), the files are ready **7 hours 10 minutes** before the cutoff.
- **Operational Fallback Protocol:**
  - In the rare event of upstream NOAA dissemination delays where the 00:00 UTC run is incomplete at
    the 12:00 Europe/Paris cutoff, the system falls back to the D-2 18:00 UTC cycle.
  - Required fallback lead steps: `f027` to `f053`.
  - The ingestion pipeline stores `run_init_time`, `source_url`, and the ingestion timestamp in the raw
    database table, and emits an alert.

---

### 3.3 Archive Start Date and Variable Availability

The operational GFS version 16.0 directory layout (`gfs.YYYYMMDD/HH/atmos/`) on AWS S3 began on
**2021-03-23**. All files prior to this date in `s3://noaa-gfs-bdp-pds` on the dates checked
(2021-01-01, 2021-03-22) are WAFS aviation datasets.

The presence of all six core variables was verified across sample dates spanning 2021 to 2025:

| Variable Identifier in `.idx` | GRIB2 Parameter Name | Record Type | Units | Relevance to Electricity Load |
|---|---|---|---|---|
| `TMP:2 m above ground` | 2-meter Temperature | Instantaneous | Kelvin | Heating/cooling demand, thermal inertia |
| `RH:2 m above ground` | 2-meter Relative humidity | Instantaneous | % | Perceived temperature, heat index |
| `UGRD:10 m above ground` | 10-meter U-wind component | Instantaneous | m/s | Wind power generation, convective cooling |
| `VGRD:10 m above ground` | 10-meter V-wind component | Instantaneous | m/s | Wind power generation, convective cooling |
| `DSWRF:surface` | Downward short-wave radiation | 6-hour reset average | W/m^2 | Solar generation, daylight lighting demand |
| `TCDC:entire atmosphere` | Total cloud cover | Instantaneous | % | Lighting load, solar shielding |

All six variables are verified present in every sample file from 2021-03-23 through 2025-01-15. This
temporal range fully encompasses the 2022-2023 training window, the 2024 validation year, and the
locked 2025 test year.

---

### 3.4 Data Path Parity Between Backtest and Production

1. **Identical Query Logic:**
   - Both backtest and production pipelines invoke the same retrieval function.
   - For delivery day D, the function queries `.idx` files for steps `f021` to `f047` of the D-1 00 UTC run,
     parses byte ranges, and downloads exact GRIB2 records via HTTP range requests.
2. **Deterministic Object Naming:**
   - Historical dates and live production runs share the identical URL path template:
     `https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{YYYYMMDD}/00/atmos/gfs.t00z.pgrb2.0p25.{step}`.
3. **Archive Fidelity:**
   - S3 `LastModified` timestamps on the sampled dates match operational dissemination times on the run date
     (the arrival times match the run date; contents were not compared).

---

## 4. Verification Commands and Real Execution Outputs

All empirical findings in this document were verified using reproducible Python scripts adhering to
strict TLS verification rules (using `requests` and `certifi`).

### 4.1 Verification Script: NOAA GFS Archive, Variables, Semantics, Delays, and Byte Ranges

The script `scripts/source_research/verify_weather_sources.py` executes the complete verification sequence:

```python
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
```

**Real Execution Output:**

```text
--- 1. Archive Start Date and Layout Transition ---
Date 20210101: HTTP 200
  sample key: gfs.20210101/00/WAFS_blended_2021010100f06.grib2
  sample key: gfs.20210101/00/WAFS_blended_2021010100f06.grib2.idx
  sample key: gfs.20210101/00/WAFS_blended_2021010100f09.grib2
Date 20210322: HTTP 200
  sample key: gfs.20210322/00/WAFS_blended_2021032200f06.grib2
  sample key: gfs.20210322/00/WAFS_blended_2021032200f09.grib2
  sample key: gfs.20210322/00/WAFS_blended_2021032200f12.grib2
Date 20210323: HTTP 200
  sample key: gfs.20210323/00/atmos/bufr.t00z/bufr.000001.2021032300
  sample key: gfs.20210323/00/atmos/bufr.t00z/bufr.000002.2021032300
  sample key: gfs.20210323/00/atmos/bufr.t00z/bufr.000003.2021032300

--- 2. Six Core Variables Across Sample Dates ---
Date 20210323 Step f024 (HTTP 200, 743 total records):
  :TMP:2 m above ground: -> 1 match(es)
    line 581: TMP (2 m above ground / 24 hour fcst)
  :RH:2 m above ground: -> 1 match(es)
    line 584: RH (2 m above ground / 24 hour fcst)
  :UGRD:10 m above ground: -> 1 match(es)
    line 588: UGRD (10 m above ground / 24 hour fcst)
  :VGRD:10 m above ground: -> 1 match(es)
    line 589: VGRD (10 m above ground / 24 hour fcst)
  :DSWRF:surface: -> 1 match(es)
    line 653: DSWRF (surface / 18-24 hour ave fcst)
  :TCDC:entire atmosphere: -> 2 match(es)
    line 636: TCDC (entire atmosphere / 24 hour fcst)
    line 637: TCDC (entire atmosphere / 18-24 hour ave fcst)
Date 20220101 Step f030 (HTTP 200, 743 total records):
  :TMP:2 m above ground: -> 1 match(es)
    line 581: TMP (2 m above ground / 30 hour fcst)
  :RH:2 m above ground: -> 1 match(es)
    line 584: RH (2 m above ground / 30 hour fcst)
  :UGRD:10 m above ground: -> 1 match(es)
    line 588: UGRD (10 m above ground / 30 hour fcst)
  :VGRD:10 m above ground: -> 1 match(es)
    line 589: VGRD (10 m above ground / 30 hour fcst)
  :DSWRF:surface: -> 1 match(es)
    line 653: DSWRF (surface / 24-30 hour ave fcst)
  :TCDC:entire atmosphere: -> 2 match(es)
    line 636: TCDC (entire atmosphere / 30 hour fcst)
    line 637: TCDC (entire atmosphere / 24-30 hour ave fcst)
Date 20230715 Step f030 (HTTP 200, 743 total records):
  :TMP:2 m above ground: -> 1 match(es)
    line 581: TMP (2 m above ground / 30 hour fcst)
  :RH:2 m above ground: -> 1 match(es)
    line 584: RH (2 m above ground / 30 hour fcst)
  :UGRD:10 m above ground: -> 1 match(es)
    line 588: UGRD (10 m above ground / 30 hour fcst)
  :VGRD:10 m above ground: -> 1 match(es)
    line 589: VGRD (10 m above ground / 30 hour fcst)
  :DSWRF:surface: -> 1 match(es)
    line 653: DSWRF (surface / 24-30 hour ave fcst)
  :TCDC:entire atmosphere: -> 2 match(es)
    line 636: TCDC (entire atmosphere / 30 hour fcst)
    line 637: TCDC (entire atmosphere / 24-30 hour ave fcst)
Date 20250115 Step f030 (HTTP 200, 743 total records):
  :TMP:2 m above ground: -> 1 match(es)
    line 581: TMP (2 m above ground / 30 hour fcst)
  :RH:2 m above ground: -> 1 match(es)
    line 584: RH (2 m above ground / 30 hour fcst)
  :UGRD:10 m above ground: -> 1 match(es)
    line 588: UGRD (10 m above ground / 30 hour fcst)
  :VGRD:10 m above ground: -> 1 match(es)
    line 589: VGRD (10 m above ground / 30 hour fcst)
  :DSWRF:surface: -> 1 match(es)
    line 653: DSWRF (surface / 24-30 hour ave fcst)
  :TCDC:entire atmosphere: -> 2 match(es)
    line 636: TCDC (entire atmosphere / 30 hour fcst)
    line 637: TCDC (entire atmosphere / 24-30 hour ave fcst)

--- 3. Radiation (DSWRF) Reset and Cloud Cover (TCDC) Selection ---
Step f021:
  DSWRF: line 653 -> DSWRF (surface / 18-21 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 21 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 18-21 hour ave fcst)
Step f022:
  DSWRF: line 653 -> DSWRF (surface / 18-22 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 22 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 18-22 hour ave fcst)
Step f023:
  DSWRF: line 653 -> DSWRF (surface / 18-23 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 23 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 18-23 hour ave fcst)
Step f024:
  DSWRF: line 653 -> DSWRF (surface / 18-24 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 24 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 18-24 hour ave fcst)
Step f025:
  DSWRF: line 653 -> DSWRF (surface / 24-25 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 25 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 24-25 hour ave fcst)
Step f026:
  DSWRF: line 653 -> DSWRF (surface / 24-26 hour ave fcst)
  TCDC:  line 636 -> TCDC (entire atmosphere / 26 hour fcst)
  TCDC:  line 637 -> TCDC (entire atmosphere / 24-26 hour ave fcst)

--- 4. Publication Delay Verification (00 UTC vs 06 UTC Runs) ---
Date 20240115 Run 00z Step f047: Published = 2024-01-15T03:43:20.000Z
Date 20240115 Run 06z Step f047: Published = 2024-01-15T09:41:10.000Z
Date 20240715 Run 00z Step f047: Published = 2024-07-15T03:47:51.000Z
Date 20240715 Run 06z Step f047: Published = 2024-07-15T09:42:17.000Z
Date 20260301 Run 00z Step f047: Published = 2026-03-01T03:46:53.000Z
Date 20260301 Run 06z Step f047: Published = 2026-03-01T09:45:10.000Z

--- 5. Byte-Range Request Verification (.idx to partial GRIB) ---
Index line: 581:420375042:d=2022010100:TMP:2 m above ground:24 hour fcst:
Byte range: 420375042-421252194 (877153 bytes)
Partial GET: HTTP 206
Downloaded bytes: 877153
Magic header: b'GRIB'
Magic trailer: b'7777'
```

---

### 4.2 Verification Script: Open-Meteo Previous Runs API Limitations

The script `scripts/source_research/verify_openmeteo_previous_runs.py` verifies the historical limits of the Previous
Runs API:

```python
"""Verification script for Open-Meteo Previous Runs API limitations."""

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
```

**Real Execution Output:**

```text
--- Open-Meteo Previous Runs API: Historical Variables ---
Date 2022-01-01 at 12:00 UTC: T_2m=12.3, Wind=None, Direct_Radiation=None, Cloud_Cover=None
Date 2023-01-01 at 12:00 UTC: T_2m=12.2, Wind=None, Direct_Radiation=None, Cloud_Cover=None
Date 2024-03-01 at 12:00 UTC: T_2m=7.7, Wind=22.2, Direct_Radiation=16.4, Cloud_Cover=100
```

---

## 5. Proposed Revision of Protocol Rule D11

With the acceptance of this analysis, Decision D11 in `docs/protocol.md` can transition from
provisional to approved:

### D11. Weather issue-time rule (Approved Text)

- **Decision:** Weather inputs for delivery day D come from the NOAA GFS 0.25 degree 00 UTC run of D-1,
  steps `f021` to `f047` (`f021` only to difference DSWRF). DSWRF is the hourly mean rebuilt as
  $k \cdot A_k - (k-1) \cdot A_{k-1}$ from the running mean since the last 6-hour reset. For TCDC the instantaneous
  record is used. Hourly values are linearly interpolated to the 30 minute grid with steps of the same run
  only. An hourly mean $F_{t-1 \to t}$ is timestamped at $t - 30$ min before interpolation.
- **Completeness test:** A run is complete at the cutoff if every needed step file and its `.idx` exist with
  S3 `LastModified` strictly before the cutoff in UTC. Backtest and production apply this same test; the
  backtest reads the archived `LastModified`.
- **Fallback:** If the test fails, the 18 UTC run of D-2, steps `f027` to `f053`, is used under the same test.
  If that fails too, the day is reported as "weather missing" and handled by an explicit rule.
  Every day stores `run_init_time`, `source_url` and a fallback flag, and every backtest report gives the
  number of fallback days.
- **Reason:** The 00 UTC run is fully published about 3.7 hours after its start, more than 6 hours before the
  summer cutoff (observed on the sampled dates). The 06 UTC run lands only 11 to 19 minutes before the
  summer cutoff and is kept as a later one-change experiment. Using the same run, the same steps and the
  same completeness test in backtest and production keeps the backtest weather exactly as good as the
  production weather, no better.

## 6. Implemented collector and complete availability scan

Measured on 2026-10-04. `src/load_forecast/weather.py` implements the approved
protocol section 2.1: 00 UTC on D-1, f021-f047, or 18 UTC on D-2, f027-f053.
The first step supports radiation differencing for the next step. These are the
approved task/protocol ranges; earlier f026 fallback references in this source
research document are superseded for the collector.

The same selection function checks all 27 GRIB files and all 27 indexes for
existence, nonzero size and S3 LastModified strictly before the UTC cutoff.
Missing and late objects both fail the test. Transport/server failures are retried
with bounded exponential backoff and fail the collection if retries are exhausted;
they are not counted as missing weather. Pagination is followed. Downloads require
unchanged ETags and LastModified, HTTP 206 and the exact Content-Range and GRIB
message length. The instantaneous TCDC record is selected. Native DSWRF running
means remain unchanged in raw storage; no interpolation or feature construction
is performed by this collector.

Fixed point names and coordinates live in `weather.py:POINTS`: Paris, Lille,
Strasbourg, Lyon, Marseille, Nice, Toulouse, Bordeaux, Nantes, Rennes, Dijon and
Clermont-Ferrand. Each is sampled at its nearest grid cell; the actual cell
coordinates are stored. These are fixed representatives, with no weighting or
claim that they define an optimal national temperature.

Decoder: `eccodes==2.49.0` is pinned in `requirements.txt`. The Windows CPython 3.13
wheel installed successfully; `python -m eccodes selfcheck` found ecCodes 2.49.0
and its bundled DLL. [ECMWF installation documentation](https://github.com/ecmwf/eccodes-python#installation)
explains bundled binaries; [release files](https://pypi.org/project/eccodes/2.49.0/#files)
include the cp313 win_amd64 wheel. No separate decoder installation was needed.

`src/load_forecast/weather_collector.py` writes append-only `raw.gfs_days`
(cutoff, selected run, source URL, fallback flag, missing/late keys, original S3
metadata) and `raw.gfs_points` (native values, units, intervals, UTC valid time,
grid cell, index record, byte range, GRIB hash and publication/ingestion times).
Identical manifests and messages use `ON CONFLICT DO NOTHING`; changed messages
are retained as distinct revisions. A complete decoded batch is validated before
an atomic insert. A day with neither run available is stored as `weather missing`
with a null selected run and no invented points. The future forecasting pipeline
still needs an explicit missing-weather policy.

From the repository root, install the pinned requirements in the shared environment
and set PostgreSQL process environment variables as for the eCO2mix collector.
The root `.env` is not automatically loaded by the module. NOAA HTTP access is
anonymous. By default the collector targets tomorrow's Paris delivery day; run it
at the approved noon cutoff or later. The collector checks the current UTC time
against `forecast_cutoff(delivery_day)` before selecting any run. Before cutoff,
it logs why the day was skipped and writes neither a manifest nor points, including
in scan-only mode. If every requested day is early, it does not connect to PostgreSQL.
The UTC time is captured once per invocation; a skipped day needs a later rerun.
At exactly the cutoff, collection is allowed. The summary's `days` is the requested
count; `skipped_days` lists early days separately from primary, fallback and missing.

```powershell
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --start 2024-07-16 --end 2024-07-17
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --scan-only --start 2021-03-23 --end 2026-10-05 --workers 8
```

Dates are delivery days; end is exclusive. The scan downloads object listings
only. It tests archived metadata compliance with D11, not variable content in
every file or proof of the historical operational publication instant.
The local ignored launcher read root `.env` into the process without displaying
credentials. My original scan used an ignored launcher. The command below is its maintained
equivalent, not a new scan in this cleanup session. Load the environment first
and create an ignored log directory. External requests belong only to a
collector task or developer-managed ingestion:

```powershell
. ./scripts/load_env.ps1
$env:PYTHONPATH = 'src'
New-Item -ItemType Directory -Force data/audit/gfs_scan_012 | Out-Null
```

Maintained scan command:

```powershell
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --scan-only --start 2021-03-23 --end 2026-10-05 --workers 8 > data/audit/gfs_scan_012/scan.log
```

Exit 0; final output:

```json
{"days": 2022, "fallback": 4, "fallback_days": ["2021-06-15", "2022-01-18", "2023-01-21", "2023-05-02"], "missing_days": ["2021-03-23", "2022-12-01"], "primary": 2016, "weather missing": 2}
```

Delivery day 2021-03-23 requires runs before the atmospheric archive starts.
For 2022-12-01, both candidate runs fail the metadata completeness test.
Neither day is silently dropped or counted as a fallback. Independent PostgreSQL
verification originally used an ignored audit helper. Its maintained equivalent,
`../load-forecast-fr-venv/Scripts/python.exe scripts/audit_weather.py`, reads the
current snapshot in a read-only transaction. The original verification executed
the following query and returned fallback 4, primary 2016, weather missing 2:

```sql
SELECT status, count(*) FROM raw.gfs_days GROUP BY status ORDER BY status;
```

The original decoder and radiation checks used the local launcher.
Maintained equivalents (collector tasks only):

```powershell
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --start 2024-07-16 --end 2024-07-17 --workers 1
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --start 2024-01-16 --end 2024-01-17 --workers 1
```

Both exited 0 and inserted 1944 points (27 steps x 6 variables x 12 points).
Raw DSWRF ranged from 0 to 904.7 W/m2 in July and 0 to 401.7 W/m2 in January.
All 24 points whose averaging intervals lie wholly within 00-02 UTC were exactly
zero in each sample; no raw DSWRF was negative. A running mean ending at night can
include daytime radiation, so endpoint night alone is not a valid zero-flux test.
`SELECT count(*) FROM raw.gfs_points` returned 3888 after both samples.
The database test reinserts existing real day and point originals twice and checks
zero inserts, unchanged counts/ingestion timestamps, actual timestamptz types and
UTC sessions. It is opt-in with `TEST_WEATHER_DATABASE=1`; unit tests include the
missing-file completeness/fallback case. No models or evaluation-year experiments
were run, and no Docker service was started or changed.

## 7. Measured download volume and cutoff enforcement

Measured on 2026-10-04 using the collector's D11 selection and exact variable
index parser. The script fetches S3 listings and all 27 indexes, sums the six
selected inclusive byte ranges per step, and counts actual index response bytes.
It downloads no GRIB fields and writes nothing to PostgreSQL. The 12 sampled
points share each global field download, so they do not multiply transfer bytes.
ETag and LastModified checks reject indexes that change during measurement.

Exact commands executed from the repository root, both exit 0:

```powershell
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe scripts/measure_weather_volume.py 2024-07-16
& ../load-forecast-fr-venv/Scripts/python.exe scripts/measure_weather_volume.py 2026-10-04
```

Outputs:

```json
{"delivery_day": "2024-07-16", "fallback": false, "grib_bytes": 132935400, "index_bytes": 1113916, "run_init_time": "2024-07-15T00:00:00+00:00", "total_bytes": 134049316}
{"delivery_day": "2026-10-04", "fallback": false, "grib_bytes": 132625984, "index_bytes": 1114078, "run_init_time": "2026-10-03T00:00:00+00:00", "total_bytes": 133740062}
```

MB and GB here use decimal units (1 MB = 1,000,000 bytes). The two samples total
about 134 MB per day including indexes. Multiplying by the 2022 requested days in
my original scan gives a planning estimate of about 271 GB. Missing days reduce
actual transfer; archive encoding, fallback runs, retries and metadata traffic
can change it. This is a two-sample estimate, not a measured full-history download.

Cutoff tests run the collector with a controlled clock and mocked HTTP/database
boundaries: one microsecond before cutoff calls neither selection nor storage;
at cutoff and one microsecond after, primary selection reaches both day and point
inserts. They cover the 23-hour and 25-hour delivery days, the days after both
transitions, scan-only skips, and a mixed range with an eligible and a future day.

## 8. TMP-first resumable backfill

Run selection itself now rejects calls before `forecast_cutoff(delivery_day)` in
`weather.py:select_run`, before making an HTTP request. The collector captures one
aware UTC clock value and passes it through selection. It still logs early days
as skipped. The selection uses LastModified strictly before the cutoff even when
collection runs later, keeping production and historical collection consistent.

The date range is inclusive `--start`, exclusive `--end`, in Paris delivery dates.
`--variables` accepts one or more of TMP, RH, UGRD, VGRD, DSWRF, TCDC; its default
remains all six for daily collection. The developer chose TMP first for backfill.
The D11 completeness test still checks all 54 files/indexes for the selected run;
variable selection changes only the fields downloaded and stored.

`--resume` checks PostgreSQL before inspecting days over HTTP. A day is skipped
only if a stored selected run has all 27 steps at all 12 fixed points for every
requested variable. Scan manifests, partial point batches and weather-missing
days do not satisfy this test. A second pass with other variables is therefore
collected independently. Existing stored selections are reused without rechecking
archive metadata on skipped days; use a run without `--resume` for an explicit
archive revision check. Different message hashes remain append-only revisions.

No GRIB file is written to disk. `weather.py:fetch_message` returns response bytes;
`extract_run` decodes each message in memory, and `decode_points` releases the
ecCodes handle in `finally`. Python releases message buffers after their last
reference is replaced or the function exits. There is consequently no disk file
to delete. Each complete day's manifest and points are committed atomically in
`weather_collector.py:store_day`; a failure before that transaction commits leaves
that day's points unstored. Earlier committed days remain available on restart.

### Seven consecutive days measured on 2026-10-04

The historical backfill measurement used the ignored launcher. This tracked
module command is its maintained equivalent, for a collector task only.

```powershell
New-Item -ItemType Directory -Force data/audit/tmp_sample_017 | Out-Null
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --start 2024-07-08 --end 2024-07-15 --variables TMP --resume --measure --workers 1 > data/audit/tmp_sample_017/measurement.log
```

Exit 0. Each day selected primary and inserted 324 points; existing scan manifests
inserted zero additional day rows. The final JSON contained these measurements:

| Delivery day | GRIB bytes | Index bytes | Total bytes | Download/decode/store seconds | Point tuple bytes |
|---|---:|---:|---:|---:|---:|
| 2024-07-08 | 14073643 | 1113962 | 15187605 | 40.551 | 138672 |
| 2024-07-09 | 14019925 | 1113958 | 15133883 | 41.379 | 138672 |
| 2024-07-10 | 13970632 | 1113984 | 15084616 | 42.157 | 138672 |
| 2024-07-11 | 13898791 | 1114041 | 15012832 | 39.793 | 138672 |
| 2024-07-12 | 13801870 | 1114020 | 14915890 | 39.981 | 138672 |
| 2024-07-13 | 13817991 | 1113968 | 14931959 | 38.516 | 138672 |
| 2024-07-14 | 13874883 | 1113983 | 14988866 | 37.994 | 138672 |

Total successful GRIB and index response bodies: 105255651 bytes (105.26 MB).
Total invocation duration, including selection: 282.204 seconds, about 40.31 s/day.
These transfer counters exclude object listings, headers and retried response bodies.
For 2021-03-23 through 2026-10-04 inclusive (2022 days), simple multiplication
gives about 30.40 GB and 22.64 hours with one worker on this machine/connection.
This is a seven-day extrapolation, not a full-history download. The two known
weather-missing days reduce stored data; different seasons, fallbacks, retries
and archive revisions can change both volume and runtime. The full backfill was
not started.

Point tuple size comes from `stored_row_bytes` using the following parameterized
query, with each selected run, `['TMP']`, steps 21/47 and the fixed point names:

```sql
SELECT coalesce(sum(pg_column_size(p)), 0) FROM raw.gfs_points p
WHERE run_init_time = %s AND variable = ANY(%s)
  AND end_step BETWEEN %s AND %s AND point = ANY(%s);
```

The seven days each use 138672 tuple bytes, or 428 bytes per point and 0.139 MB/day.
That extrapolates to 280.39 MB of point tuples for 2022 days. This excludes page
overhead, indexes, TOAST and shared day manifests, so it is not a disk-space budget.
`--measure` also reports `database_bytes_before/after` from
`pg_total_relation_size('raw.gfs_points') + pg_total_relation_size('raw.gfs_days')`,
including those allocations across all stored weather days. The first measurement
ran before those total-allocation counters were added, so no initial seven-day
allocation delta was measured. A real resume check with the same command (without
redirection) returned all seven `stored_days`, no measurements, 0.052 seconds and
12091392 bytes both before and after. The real database pytest also confirms that
requesting TMP plus RH does not skip these TMP-only days, and that a TMP resume
makes no HTTP calls and leaves the point count unchanged.

### Developer command, stop and resume

From the developer's repository root, use collector writer credentials in the
current process environment. `lff_agent` cannot write `raw`; use the ignored writer
`.env` with `scripts/load_env.ps1`. Then run:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_collector --start 2021-03-23 --end 2026-10-05 --variables TMP --resume --workers 1
```

Press Ctrl+C to stop. Resume by running the identical command: completed TMP days
are skipped, and the interrupted day is retried. Atomic inserts and existing
content keys prevent duplicates even if interruption follows a successful commit.
Read the final `missing_days` and `skipped_days` lists; they need explicit handling
or a later retry. For the later second pass, replace `--variables TMP` with
`--variables RH UGRD VGRD DSWRF TCDC` and keep the same dates and `--resume`.
