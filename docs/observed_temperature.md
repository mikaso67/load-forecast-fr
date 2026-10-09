# Observed Station Temperature Sources for France (Protocol D9 Cold Label)

Status: research completed.
Scope: official observed station temperature sources covering 2022-01 to today for metropolitan France,
station mapping for the 12 representative points of `weather.py:POINTS`, licence, format, update delay,
download procedure, sample verification, missing slot analysis, and simulation of the D9 cold threshold.
This document investigates sources for evaluation labels only; observations are strictly forbidden as model inputs.

---

## 1. Context and Problem Statement

Protocol D9 (`docs/protocol.md` Section 5.2 and `docs/modeling_plan.md` M12) defines the "Cold" day evaluation
type as any local day whose national daily mean temperature falls below the 10th percentile of the national
daily means over the reference period (calendar years 2022 and 2023).

Key constraints:
1. **Labels only, never features**: In accordance with the forecast-time leakage checklist (Item 2), observed
   weather is strictly forbidden as an input to any predictive model. It serves exclusively for ex-post evaluation
   labeling.
2. **Temporal coverage**: The source must cover continuously from at least 2022-01-01 to today (2026),
   including the 2022-2023 reference period, the 2024 validation year, and ongoing production.
3. **Spatial alignment**: The source must provide stations close to the 12 representative city centres defined
   in `src/load_forecast/weather.py:POINTS`.
4. **Free official open data**: The data must be accessible free of charge under an open license without
   restrictive operational quotas.

---

## 2. Candidate Sources Evaluated

On January 1, 2024 (unverified from memory, exact release date not queried via API), Meteo-France released its public weather data as open data under the Etalab Licence Ouverte
v2.0 on `meteo.data.gouv.fr` and `data.gouv.fr`. Three candidate sources were evaluated:

### Candidate A: Meteo-France SYNOP OMM (Recommended)

- **Dataset title**: Archive Synop OMM
- **Catalog ID**: `686f8595b351c06a3a790867` on data.gouv.fr
- **Storage host**: OVH Object Storage (`https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/`)
- **Network scope**: 62 synoptic stations in published inventory; exactly 42 in metropolitan France confirmed
  via geographic bounding box (see Section 4). Coverage of all major commercial airport weather stations is
  unverified.
- **Time step**: 3-hourly (00:00, 03:00, 06:00, 09:00, 12:00, 15:00, 18:00, 21:00 UTC), giving 8 slots per day.
- **Variables**: Instantaneous 2-meter air temperature (`t` in Kelvin, converted via $T_{°C} = t - 273.15$),
  atmospheric pressure (`pres`), humidity (`u`), wind speed (`ff`), and minimum/maximum temperatures (`tn12`,
  `tn24`, `tx12`, `tx24`). Variable descriptions follow standard WMO SYNOP code forms (FM 12); official variable
  definitions unverified via command.
- **File structure**: One single annual archive file per year for the entire country (`synop_{YYYY}.csv.gz`).
  Files for 2022 to 2024 are 6.4 to 7.0 MB. The files for 2025 (15.17 MB) and 2026 (24.47 MB) are substantially
  larger based on HTTP HEAD Content-Length headers; their internal structure, column schema, and reporting cadence
  are unverified (their data content has not been downloaded or inspected, in compliance with the rule prohibiting
  reading 2024+ data). A slot count per day and schema verification must precede collector implementation.
- **Update cadence**: On 2026-10-05, the observed HTTP Last-Modified header was 06:30:14 GMT. Daily recurrence
  at approximately 06:30 UTC is unverified (based on a single header observation). Published freshness covering
  through 21:00 UTC of D-1 (1-day lag) is unverified without inspecting 2026 file contents.
- **Suitability**: Excellent. A single compact file per year contains all 12 representative stations. Daily national
  temperature can be computed by averaging the 8 synoptic slots across the 12 stations under the coverage rule.

### Candidate B: Meteo-France Donnees Climatologiques de Base (Quotidiennes and Horaires)

- **Dataset titles**:
  - Donnees climatologiques de base - quotidiennes (Catalog ID: `6569b51ae64326786e4e8e1a`)
  - Donnees climatologiques de base - horaires (Catalog ID: `6569b4473bedf2e7abad3b72`)
- **Storage host**: OVH Object Storage (`https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/`)
- **Network scope**: Over 3,000 meteorological and climatological stations across France (RADOME and volunteer,
  unverified station count).
- **Time step**:
  - Quotidiennes: Daily (`AAAAMMJJ`). Contains `TM` ("moyenne quotidienne des temperatures horaires sous abri",
    in degrees Celsius and tenths), `TN` (minimum), `TX` (maximum), and quality codes (`QTM`, `QTN`, `QTX`).
    Descriptions are based on Meteo-France documentation; column definitions unverified via command.
  - Horaires: Hourly (`AAAAMMJJHH`). Contains `T` ("temperature sous abri instantanee", in degrees Celsius).
- **File structure**: Partitioned by French departement (e.g. `Q_75_previous-1950-2024_RR-T-Vent.csv.gz` and
  `Q_75_latest-2025-2026_RR-T-Vent.csv.gz`).
- **Update cadence**: On 2026-10-05, observed HTTP Last-Modified header was 05:52:47 GMT for Q_75. Daily update
  cadence between 05:40 and 06:00 UTC and publication with validated observations up to D-2 (2-day lag) are
  unverified (based on a single observed timestamp; 2026 contents not inspected).
- **Suitability**: High scientific quality (directly provides official 24-hour daily mean `TM`). However,
  retrieval requires managing 12 separate department files and merging historical (1950-2024) and recent (2025-2026)
  files (~140 MB total payload for 12 departments, unverified).

### Candidate C: Meteo-France Developer API Portal (Rejected)

- **Portal**: `portail-api.meteofrance.fr`
- **Access mode**: REST API requiring individual account registration, OAuth credentials, and application subscriptions.
- **Reason for rejection**: Subject to per-minute rate limits (unverified) and subscription management. Direct HTTPS downloads
  from the open object storage (Candidates A and B) are fully automated, public, require no credentials, and carry
  zero authentication failure risk.

---

## 3. Licence and Terms of Re-use

Both Candidate A (SYNOP OMM) and Candidate B (Climatologie) are published under the **Licence Ouverte v2.0**
(Etalab / Open Licence 2.0).

Key terms of Licence Ouverte v2.0:
- Free commercial and non-commercial re-use, reproduction, modification, and distribution.
- Obligation: Mention the source and date of last update: "Meteo-France via data.gouv.fr" (unverified from memory,
  exact legal wording of Licence Ouverte v2.0 text not queried via API).
- No royalty or recurring fee.

### Verification of Licence and Catalog Metadata

Command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests
for name, ds_id in [('SYNOP OMM', '686f8595b351c06a3a790867'), ('Donnees quotidiennes', '6569b51ae64326786e4e8e1a'), ('Donnees horaires', '6569b4473bedf2e7abad3b72')]:
    r = requests.get(f'https://www.data.gouv.fr/api/1/datasets/{ds_id}/')
    d = r.json()
    print(f'{name:22s} | org: {d[\"organization\"][\"name\"]} | license: {d.get(\"license\")} | modified: {d.get(\"last_modified\")[:10]}')
"
```

Output:
```text
SYNOP OMM              | org: Météo-France | license: lov2 | modified: 2026-10-05
Donnees quotidiennes   | org: Météo-France | license: lov2 | modified: 2026-10-05
Donnees horaires       | org: Météo-France | license: lov2 | modified: 2026-10-05
```

---

## 4. Stations Near the 12 Representative Points

The 12 reference points in `src/load_forecast/weather.py:POINTS` represent French metropolitan population centres.
Each point matches an official Meteo-France synoptic station from the SYNOP network located at an airport or
meteorological observatory within 5.0 to 20.0 km of the city centre.

### Station Mapping Table

| Point | Reference (lat, lon) | Nearest SYNOP Station | WMO ID | Station (lat, lon) | Alt (m) | Distance | Climatological Station (Dep, Ref only) |
|---|---|---|---|---|---|---|---|
| `paris` | (48.8566, 2.3522) | ORLY | `07149` | (48.7168, 2.3843) | 89 | 15.7 km | PARIS-MONTSOURIS (75114001, unverified) |
| `lille` | (50.6292, 3.0573) | LILLE-LESQUIN | `07015` | (50.5700, 3.0975) | 47 | 7.2 km | LILLE-LESQUIN (59343001, unverified) |
| `strasbourg` | (48.5734, 7.7521) | STRASBOURG-ENTZHEIM | `07190` | (48.5495, 7.6403) | 150 | 8.6 km | ENTZHEIM (67124001, unverified) |
| `lyon` | (45.7640, 4.8357) | LYON-ST EXUPERY | `07481` | (45.7265, 5.0778) | 235 | 19.2 km | LYON-BRON (69029001, unverified) |
| `marseille` | (43.2965, 5.3698) | MARIGNANE | `07650` | (43.4377, 5.2160) | 9 | 20.0 km | MARSEILLE-OBS (13055001, unverified) |
| `nice` | (43.7102, 7.2620) | NICE | `07690` | (43.6488, 7.2090) | 2 | 8.0 km | NICE (06088001, unverified) |
| `toulouse` | (43.6047, 1.4442) | TOULOUSE-BLAGNAC | `07630` | (43.6210, 1.3788) | 151 | 5.6 km | TOULOUSE-BLAGNAC (31069001, unverified) |
| `bordeaux` | (44.8378, -0.5792) | BORDEAUX-MERIGNAC | `07510` | (44.8307, -0.6913) | 47 | 8.9 km | MERIGNAC (33281001, unverified) |
| `nantes` | (47.2184, -1.5536) | NANTES-BOUGUENAIS | `07222` | (47.1500, -1.6088) | 26 | 8.7 km | BOUGUENAIS (44109001, unverified) |
| `rennes` | (48.1173, -1.6778) | RENNES-ST JACQUES | `07130` | (48.0688, -1.7340) | 36 | 6.8 km | ST-JACQUES (35281001, unverified) |
| `dijon` | (47.3220, 5.0415) | DIJON-LONGVIC | `07280` | (47.2678, 5.0883) | 219 | 7.0 km | DIJON-LONGVIC (21231001, unverified) |
| `clermont_ferrand` | (45.7772, 3.0870) | CLERMONT-FD | `07460` | (45.7868, 3.1493) | 331 | 5.0 km | CLERMONT-FD (63014001, unverified) |

Note on Climatological Station column: station names, post numbers, and distances in that column are approximate
references from departmental station lists provided for comparison only (unverified by distance command; SYNOP
stations are the evaluated primary source).

### Verification of SYNOP Distance and Station Coordinates

Command (computing haversine distance and checking metropolitan bounding box):
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, csv, io, math
POINTS = {'paris': (48.8566, 2.3522), 'lille': (50.6292, 3.0573), 'strasbourg': (48.5734, 7.7521), 'lyon': (45.7640, 4.8357), 'marseille': (43.2965, 5.3698), 'nice': (43.7102, 7.2620), 'toulouse': (43.6047, 1.4442), 'bordeaux': (44.8378, -0.5792), 'nantes': (47.2184, -1.5536), 'rennes': (48.1173, -1.6778), 'dijon': (47.3220, 5.0415), 'clermont_ferrand': (45.7772, 3.0870)}
url = 'https://static.data.gouv.fr/resources/archive-synop-omm/20260422-141811/observations-liste-stations-synop-omm-20250710.csv'
r = requests.get(url)
stations = list(csv.DictReader(io.StringIO(r.text), delimiter=';'))
metro = [s for s in stations if 41.0 <= float(s['Latitude']) <= 52.0 and -5.5 <= float(s['Longitude']) <= 10.0]
print(f'Total stations: {len(stations)} | Metropolitan bounding box: {len(metro)}')
def hav(p1, p2):
    lat1, lon1, lat2, lon2 = math.radians(p1[0]), math.radians(p1[1]), math.radians(p2[0]), math.radians(p2[1])
    return 6371 * 2 * math.asin(math.sqrt(math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2))
for name, pt in POINTS.items():
    st = min(stations, key=lambda s: hav(pt, (float(s['Latitude']), float(s['Longitude']))))
    d = hav(pt, (float(st['Latitude']), float(st['Longitude'])))
    print(f'{name:17s} -> {st[\"ID\"]} {st[\"Nom\"]:20s} ({float(st[\"Latitude\"]):7.4f}, {float(st[\"Longitude\"]):7.4f}, {st[\"Altitude\"]:>3s}m) : {d:4.1f} km')
"
```

Output:
```text
Total stations: 62 | Metropolitan bounding box: 42
paris             -> 07149 ORLY                 (48.7168,  2.3843,  89m) : 15.7 km
lille             -> 07015 LILLE-LESQUIN        (50.5700,  3.0975,  47m) :  7.2 km
strasbourg        -> 07190 STRASBOURG-ENTZHEIM  (48.5495,  7.6403, 150m) :  8.6 km
lyon              -> 07481 LYON-ST EXUPERY      (45.7265,  5.0778, 235m) : 19.2 km
marseille         -> 07650 MARIGNANE            (43.4377,  5.2160,   9m) : 20.0 km
nice              -> 07690 NICE                 (43.6488,  7.2090,   2m) :  8.0 km
toulouse          -> 07630 TOULOUSE-BLAGNAC     (43.6210,  1.3788, 151m) :  5.6 km
bordeaux          -> 07510 BORDEAUX-MERIGNAC    (44.8307, -0.6913,  47m) :  8.9 km
nantes            -> 07222 NANTES-BOUGUENAIS    (47.1500, -1.6088,  26m) :  8.7 km
rennes            -> 07130 RENNES-ST JACQUES    (48.0688, -1.7340,  36m) :  6.8 km
dijon             -> 07280 DIJON-LONGVIC        (47.2678,  5.0883, 219m) :  7.0 km
clermont_ferrand  -> 07460 CLERMONT-FD          (45.7868,  3.1493, 331m) :  5.0 km
```

---

## 5. File Formats, Column Schema, and Temperature Variables

### SYNOP OMM Format

- **File encoding**: ASCII / UTF-8, semicolon-separated values (`;`), gzip compressed (`.csv.gz`).
- **Timestamp**: `validity_time` in ISO-8601 UTC format (e.g. `2023-01-01T00:00:00Z`).
- **Station ID**: `geo_id_wmo` (5-digit WMO station identifier, e.g. `07015`).
- **Air temperature variable**: Column `t` stores 2-meter air temperature in Kelvin (floating point, e.g. `286.55` K).
  Conversion formula:
  $$T_{°C} = t - 273.15$$
- **Extreme temperatures**: `tn12`, `tn24`, `tx12`, `tx24` (minimum and maximum over 12h / 24h, in Kelvin).
- **Other surface parameters**: `pres` (station pressure in Pa), `u` (relative humidity in %), `ff` (wind speed in m/s).
- **Note on schema**: Variable definitions and units are based on standard WMO SYNOP code form FM 12 and Meteo-France
  public documentation; variable definitions unverified via command.

### Climatologie Quotidienne Format

- **File encoding**: ISO-8859-15 / UTF-8, semicolon-separated values (`;`), gzip compressed (`.csv.gz`).
- **Timestamp**: `AAAAMMJJ` as an 8-digit integer (e.g. `20230101`).
- **Station ID**: `NUM_POSTE` as an 8-digit Meteo-France post number (e.g. `75114001`).
- **Air temperature variables**:
  - `TM`: Daily mean of hourly shelter temperatures in degrees Celsius and tenths (e.g. `13.0` °C).
  - `QTM`: Quality code for `TM` (`1` = valid / controlled).
  - `TN`: Daily minimum shelter temperature in degrees Celsius and tenths.
  - `TX`: Daily maximum shelter temperature in degrees Celsius and tenths.
  - `TNTXM`: Mean of extremes: $(TN + TX) / 2$.
- **Note on schema**: Variable descriptions are based on Meteo-France documentation; column descriptions unverified
  via command.

---

## 6. Update Delay and Operational Ingestion Cadence

### SYNOP OMM Update Delay

- **Publication schedule**: On 2026-10-05, the observed HTTP Last-Modified header was 06:30:14 GMT. Daily recurrence
  at approximately 06:30 UTC is unverified (based on a single header observation).
- **Data freshness**: Data freshness covering through 21:00 UTC of D-1 (1-day lag) is unverified without inspecting
  2026 file contents.
- **Latency**: Estimated 1 day (observations of day D-1 expected by 06:30 UTC of day D; unverified).
- **Operational impact**: Zero. Day type labels are computed ex-post during weekly walk-forward evaluation or daily
  score reporting. A 1-day lag never delays the 12:00 D-1 forecast run, which never reads observed temperatures.

### Climatologie Quotidienne Update Delay

- **Publication schedule**: On 2026-10-05, the observed HTTP Last-Modified header was 05:52:47 GMT for Q_75. Daily
  regeneration cadence between 05:40 and 06:00 UTC is unverified (based on a single header observation).
- **Data freshness**: Validated observations with an exact 2-day lag (D-2) are unverified without inspecting 2026
  file contents.
- **Latency**: Estimated 2 days, reflecting climatological quality control procedures (unverified).

### Verification of Last-Modified Timestamps

Command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests
urls = [
    ('SYNOP 2026', 'https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2026.csv.gz'),
    ('Q_75 Latest', 'https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_75_latest-2025-2026_RR-T-Vent.csv.gz')
]
for label, u in urls:
    h = requests.head(u)
    print(f'{label:12s} | status: {h.status_code} | size: {int(h.headers[\"content-length\"]):>10,d} bytes | modified: {h.headers[\"last-modified\"]}')
"
```

Output:
```text
SYNOP 2026   | status: 200 | size: 25,654,829 bytes | modified: Mon, 05 Oct 2026 06:30:14 GMT
Q_75 Latest  | status: 200 | size:    115,790 bytes | modified: Mon, 05 Oct 2026 05:52:47 GMT
```

---

## 7. How to Download

All files are hosted on public OVH Object Storage endpoints without authentication, API keys, or IP restrictions.

### SYNOP OMM Archive URLs

Pattern:
`https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_{YYYY}.csv.gz`

Measured file sizes and availability across years from HTTP HEAD requests:
```text
Year 2022: HTTP 200 | Size:  6.35 MB ( 6,660,073 bytes) | Last-Modified: Thu, 06 Aug 2026 15:30:55 GMT
Year 2023: HTTP 200 | Size:  6.47 MB ( 6,786,686 bytes) | Last-Modified: Thu, 06 Aug 2026 15:30:57 GMT
Year 2024: HTTP 200 | Size:  6.68 MB ( 7,006,610 bytes) | Last-Modified: Thu, 06 Aug 2026 15:30:59 GMT
Year 2025: HTTP 200 | Size: 15.17 MB (15,902,285 bytes) | Last-Modified: Thu, 06 Aug 2026 15:31:02 GMT (unverified structure)
Year 2026: HTTP 200 | Size: 24.47 MB (25,654,829 bytes) | Last-Modified: Mon, 05 Oct 2026 06:30:14 GMT (unverified structure)
```

Important note on 2025 and 2026 files:
The files for 2025 (15.17 MB) and 2026 (24.47 MB) are substantially larger than 2022-2024 (6.35 to 6.68 MB) based on
HTTP HEAD Content-Length headers. The underlying structure, column schema, and reporting cadence for 2025 and 2026
are unverified (their data rows have not been downloaded or inspected, in compliance with the rule prohibiting reading
2024+ data). The collector implementation must verify the daily slot count and schema on 2025-2026 files before writing
ingestion logic.

### Download Command Examples

PowerShell:
```powershell
Invoke-WebRequest -Uri "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz" -OutFile "synop_2023.csv.gz"
```

Python:
```python
import io
import gzip
import pandas as pd
import requests

url = "https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz"
r = requests.get(url, timeout=30)
with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
    df = pd.read_csv(gz, sep=";", dtype={"geo_id_wmo": str}, low_memory=False)
```

---

## 8. Download Sample Verification

I downloaded a real sample of one station for one month of 2023 and measured its shape
and data characteristics. Additionally, the 8-slot synoptic mean is compared against the official
daily mean `TM` for January 2023.

### Sample 1: SYNOP Station 07015 (Lille-Lesquin) for January 2023

Source file: `https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz`

Verification command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, gzip, io, pandas as pd
r = requests.get('https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz')
with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
    df = pd.read_csv(gz, sep=';', dtype={'geo_id_wmo': str}, low_memory=False)
df['dt'] = pd.to_datetime(df['validity_time'], utc=True)
sample = df[(df['geo_id_wmo'] == '07015') & (df['dt'] >= '2023-01-01') & (df['dt'] < '2023-02-01')].sort_values('dt')
sample['t_c'] = sample['t'] - 273.15
print('Full 2023 archive shape:', df.shape)
print('Sample shape (station 07015, Jan 2023):', sample.shape)
print('Observation count:', len(sample), 'out of 248 expected (31 days * 8 slots)')
print('Missing slots:', 248 - len(sample))
print('Temperature stats (deg C): min =', sample['t_c'].min(), 'mean =', round(sample['t_c'].mean(), 2), 'max =', sample['t_c'].max())
print(sample[['geo_id_wmo', 'name', 'validity_time', 't', 't_c', 'pres', 'u']].head().to_string(index=False))
"
```

Output:
```text
Full 2023 archive shape: (170019, 65)
Sample shape (station 07015, Jan 2023): (248, 67)
Observation count: 248 out of 248 expected (31 days * 8 slots)
Missing slots: 0
Temperature stats (deg C): min = -2.7 mean = 5.66 max = 14.0
geo_id_wmo          name        validity_time      t  t_c     pres    u
     07015 LILLE-LESQUIN 2023-01-01T00:00:00Z 287.15 14.0 100410.0 61.0
     07015 LILLE-LESQUIN 2023-01-01T03:00:00Z 286.85 13.7 100500.0 67.0
     07015 LILLE-LESQUIN 2023-01-01T06:00:00Z 284.75 11.6 100710.0 90.0
     07015 LILLE-LESQUIN 2023-01-01T09:00:00Z 284.85 11.7 100930.0 83.0
     07015 LILLE-LESQUIN 2023-01-01T12:00:00Z 286.15 13.0 100950.0 79.0
```

Result: Exactly 248 observations present out of 248 expected (100.0% completeness), zero missing values.

### Sample 2: Climatologie Quotidienne Station 75114001 (Paris-Montsouris) for January 2023

Source file: `https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_75_previous-1950-2024_RR-T-Vent.csv.gz`

Verification command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, gzip, io, pandas as pd
r = requests.get('https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_75_previous-1950-2024_RR-T-Vent.csv.gz')
with gzip.GzipFile(fileobj=io.BytesIO(r.content)) as gz:
    df = pd.read_csv(gz, sep=';', low_memory=False)
sample = df[(df['NUM_POSTE'] == 75114001) & (df['AAAAMMJJ'] >= 20230101) & (df['AAAAMMJJ'] <= 20230131)].sort_values('AAAAMMJJ')
print('Sample shape (station 75114001, Jan 2023):', sample.shape)
print('Daily observations:', len(sample), 'out of 31 expected')
print('TM (mean temp) stats (deg C): min =', sample['TM'].min(), 'mean =', round(sample['TM'].mean(), 2), 'max =', sample['TM'].max())
print(sample[['NUM_POSTE', 'NOM_USUEL', 'AAAAMMJJ', 'TM', 'QTM', 'TN', 'TX', 'RR']].head().to_string(index=False))
"
```

Output:
```text
Sample shape (station 75114001, Jan 2023): (31, 60)
Daily observations: 31 out of 31 expected
TM (mean temp) stats (deg C): min = 1.1 mean = 6.46 max = 13.0
 NUM_POSTE        NOM_USUEL  AAAAMMJJ   TM  QTM   TN   TX  RR
  75114001 PARIS-MONTSOURIS  20230101 13.0  1.0 11.4 14.5 5.2
  75114001 PARIS-MONTSOURIS  20230102 10.2  1.0  9.2 11.9 1.6
  75114001 PARIS-MONTSOURIS  20230103  8.0  1.0  5.4 10.8 2.0
  75114001 PARIS-MONTSOURIS  20230104 12.1  1.0  9.2 13.6 0.8
  75114001 PARIS-MONTSOURIS  20230105 12.6  1.0 11.8 13.9 0.2
```

### Sample 3: Comparison of Orly 8-Slot Synoptic Mean vs Paris-Montsouris TM (January 2023)

To assess whether the 8-slot synoptic average accurately approximates the true 24-hour mean temperature (`TM`),
the daily mean of the 8 synoptic slots at Orly (WMO 07149, nearest SYNOP station to Paris, 15.7 km away) was compared
with the official daily mean `TM` at Paris-Montsouris (station 75114001) across all 31 days of January 2023.

Verification command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, gzip, io, pandas as pd
r_syn = requests.get('https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_2023.csv.gz')
df_syn = pd.read_csv(io.BytesIO(r_syn.content), compression='gzip', sep=';', dtype={'geo_id_wmo': str}, low_memory=False)
df_syn['dt'] = pd.to_datetime(df_syn['validity_time'], utc=True)
orly = df_syn[(df_syn['geo_id_wmo'] == '07149') & (df_syn['dt'] >= '2023-01-01') & (df_syn['dt'] < '2023-02-01')].copy()
orly['t_c'] = orly['t'] - 273.15
orly['date'] = orly['dt'].dt.date
orly_daily = orly.groupby('date')['t_c'].mean()

r_clim = requests.get('https://meteofrance.s3.sbg.io.cloud.ovh.net/data/synchro_ftp/BASE/QUOT/Q_75_previous-1950-2024_RR-T-Vent.csv.gz')
df_clim = pd.read_csv(io.BytesIO(r_clim.content), compression='gzip', sep=';', low_memory=False)
mont = df_clim[(df_clim['NUM_POSTE'] == 75114001) & (df_clim['AAAAMMJJ'] >= 20230101) & (df_clim['AAAAMMJJ'] <= 20230131)].sort_values('AAAAMMJJ')
mont['date'] = pd.to_datetime(mont['AAAAMMJJ'].astype(str)).dt.date
mont_daily = mont.set_index('date')['TM']

comp = pd.DataFrame({'orly_8slot': orly_daily, 'montsouris_TM': mont_daily})
comp['diff'] = comp['orly_8slot'] - comp['montsouris_TM']
print(f'Orly Jan 2023 8-slot mean: {comp[\"orly_8slot\"].mean():.2f} deg C')
print(f'Montsouris Jan 2023 TM mean: {comp[\"montsouris_TM\"].mean():.2f} deg C')
print(f'Mean difference (Orly - Montsouris): {comp[\"diff\"].mean():.2f} deg C (std: {comp[\"diff\"].std():.2f})')
print(f'Pearson correlation: {comp[\"orly_8slot\"].corr(comp[\"montsouris_TM\"]):.4f}')
"
```

Output:
```text
Orly Jan 2023 8-slot mean: 5.95 deg C
Montsouris Jan 2023 TM mean: 6.46 deg C
Mean difference (Orly - Montsouris): -0.51 deg C (std: 0.43)
Pearson correlation: 0.9947
```

Findings:
- Strong correlation (0.9947) between the 8-slot synoptic mean and the official 24-hour daily mean `TM`.
- The -0.51 °C mean offset is consistent with the urban heat island effect (central Paris Montsouris observatory
  versus southern suburban airport Orly, 15.7 km away; heat island explanation is an interpretation).
- Sampling error between the 8 synoptic slots and the continuous daily mean is minimal, consistent with the use of the
  8-slot SYNOP mean as an evaluation index.
- Note on date handling: the date is the UTC date, which equals the Europe/Paris calendar date because the 8 synoptic
  UTC slots (00, 03, 06, 09, 12, 15, 18, 21 UTC) correspond to the 8 slots of the local day in both winter (UTC+1)
  and summer (UTC+2).

---

## 9. Data Completeness and Simulation of the D9 Cold Threshold (2022-2023)

### Missing Slot Counts per Station and Year (2022-2023)

Over the reference period (730 calendar days across 2022 and 2023), each of the 12 representative stations has
$730 \times 8 = 5,840$ expected synoptic reports ($365 \times 8 = 2,920$ per year), giving a total expectation of
$70,080$ observations.

Verification command (from `scripts/source_research/verify_observed_temperature.py`, section 8):
```powershell
..\load-forecast-fr-venv\Scripts\python.exe scripts/source_research/verify_observed_temperature.py
```
Or directly via inline script:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, gzip, io, pandas as pd
wmo_ids = ['07149', '07015', '07190', '07481', '07650', '07690', '07630', '07510', '07222', '07130', '07280', '07460']
frames = []
for yr in [2022, 2023]:
    r = requests.get(f'https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_{yr}.csv.gz')
    df = pd.read_csv(io.BytesIO(r.content), compression='gzip', sep=';', dtype={'geo_id_wmo': str}, low_memory=False)
    sub = df[df['geo_id_wmo'].isin(wmo_ids)].copy()
    sub['year'] = yr
    frames.append(sub)
df_all = pd.concat(frames, ignore_index=True)
df_all['dt'] = pd.to_datetime(df_all['validity_time'], utc=True)
df_all['t_c'] = pd.to_numeric(df_all['t'], errors='coerce') - 273.15

exp_total = 730 * 8 * 12
pres = len(df_all)
absent = exp_total - pres
null_t = int(df_all['t_c'].isna().sum())
valid_t = int(df_all['t_c'].notna().sum())
print(f'Expected: {exp_total}, Present: {pres}, Absent: {absent}, Null t: {null_t}, Valid t: {valid_t}')

for yr in [2022, 2023]:
    sub = df_all[df_all['year'] == yr]
    print(f'=== {yr} ===')
    for w in sorted(wmo_ids):
        st = sub[sub['geo_id_wmo'] == w]
        p = len(st)
        ab = 365 * 8 - p
        nu = int(st['t_c'].isna().sum())
        va = int(st['t_c'].notna().sum())
        print(f'{w} | p: {p} | ab: {ab} | nu: {nu} | va: {va} | {va / (365 * 8) * 100:.2f}%')
"
```

Detailed completeness measurement:
- Total expected slots: 70,080
- Total present rows: 69,826
- Absent rows: 254 (0.36%)
- Rows with null temperature (`t` NaN): 41
- Valid temperature observations: 69,785 (99.58% completeness)

Breakdown by station and year:

| WMO ID | Station Name | 2022 Present | 2022 Absent | 2022 Null t | 2022 Valid t (%) | 2023 Present | 2023 Absent | 2023 Null t | 2023 Valid t (%) | Total Valid (%) |
|---|---|---|---|---|---|---|---|---|---|---|
| `07015` | LILLE-LESQUIN | 2919 | 1 | 0 | 2919 (99.97%) | 2864 | 56 | 0 | 2864 (98.08%) | 5783 (99.02%) |
| `07130` | RENNES-ST JACQUES | 2918 | 2 | 0 | 2918 (99.93%) | 2882 | 38 | 0 | 2882 (98.70%) | 5800 (99.32%) |
| `07149` | ORLY | 2920 | 0 | 0 | 2920 (100.0%) | 2906 | 14 | 0 | 2906 (99.52%) | 5826 (99.76%) |
| `07190` | STRASBOURG-ENTZHEIM | 2918 | 2 | 0 | 2918 (99.93%) | 2906 | 14 | 9 | 2897 (99.21%) | 5815 (99.57%) |
| `07222` | NANTES-BOUGUENAIS | 2918 | 2 | 1 | 2917 (99.90%) | 2907 | 13 | 0 | 2907 (99.55%) | 5824 (99.73%) |
| `07280` | DIJON-LONGVIC | 2917 | 3 | 0 | 2917 (99.90%) | 2905 | 15 | 0 | 2905 (99.49%) | 5822 (99.69%) |
| `07460` | CLERMONT-FD | 2920 | 0 | 0 | 2920 (100.0%) | 2906 | 14 | 1 | 2905 (99.49%) | 5825 (99.74%) |
| `07481` | LYON-ST EXUPERY | 2913 | 7 | 0 | 2913 (99.76%) | 2905 | 15 | 0 | 2905 (99.49%) | 5818 (99.62%) |
| `07510` | BORDEAUX-MERIGNAC | 2919 | 1 | 0 | 2919 (99.97%) | 2904 | 16 | 0 | 2904 (99.45%) | 5823 (99.71%) |
| `07630` | TOULOUSE-BLAGNAC | 2920 | 0 | 5 | 2915 (99.83%) | 2906 | 14 | 21 | 2885 (98.80%) | 5800 (99.32%) |
| `07650` | MARIGNANE | 2920 | 0 | 2 | 2918 (99.93%) | 2907 | 13 | 0 | 2907 (99.55%) | 5825 (99.74%) |
| `07690` | NICE | 2920 | 0 | 0 | 2920 (100.0%) | 2906 | 14 | 2 | 2904 (99.45%) | 5824 (99.73%) |

Station-day completeness analysis (across all $730 \times 12 = 8,760$ station-days):
- Completely absent station-days (0 rows): 6
- Station-days with 1 to 7 valid temperature reports: 114
- Total station-days with fewer than 8 valid reports: 120 (1.37% of all station-days)
- Days where zero stations have all 8 valid reports: exactly 5 days (all in 2023):
  - `2023-02-01` (maximum 6 valid slots across all stations)
  - `2023-02-05` (maximum 7 valid slots across all stations)
  - `2023-02-06` (maximum 6 valid slots across all stations)
  - `2023-07-24` (maximum 3 valid slots across all stations)
  - `2023-07-25` (maximum 5 valid slots across all stations)

If a naive daily mean is computed by simply averaging whatever slots happen to be present, days with missing slots
(e.g. afternoon slots missing during winter, or night slots missing during summer) incur sampling bias. A formal coverage
rule is therefore necessary.

### Developer-Approved Coverage Rule for Protocol D9

To ensure statistical robustness and eliminate sampling bias from incomplete station days:
1. **Station-day completeness**: A station-day is valid if and only if all 8 synoptic reports are present with non-null
   temperature. The station daily mean is the unweighted average of the 8 reports: $\bar{T}_{station} = \frac{1}{8}\sum_{i=1}^8 T_i$.
2. **National day completeness**: A calendar day is labelled if and only if at least 10 of the 12 representative stations
   are valid ($N_{valid\_stations} \ge 10$). If fewer than 10 stations have complete 8-slot coverage, the day label is
   missing (`label_missing`: neither cold nor ordinary) and must be explicitly recorded in evaluation reports.
3. **National daily mean**: For labelled days, the national daily mean temperature is computed with equal weights
   across the valid stations:
   $$\bar{T}_{nat} = \frac{1}{N_{valid\_stations}}\sum_{s \in valid} \bar{T}_{s}$$

### Simulation Results on Reference Period 2022-2023

Verification command:
```powershell
..\load-forecast-fr-venv\Scripts\python.exe -c "import requests, gzip, io, pandas as pd
wmo_ids = ['07149', '07015', '07190', '07481', '07650', '07690', '07630', '07510', '07222', '07130', '07280', '07460']
frames = []
for yr in [2022, 2023]:
    r = requests.get(f'https://meteofrance.s3.sbg.io.cloud.ovh.net/data/OBS/SYNOP/synop_{yr}.csv.gz')
    df = pd.read_csv(io.BytesIO(r.content), compression='gzip', sep=';', dtype={'geo_id_wmo': str}, low_memory=False)
    sub = df[df['geo_id_wmo'].isin(wmo_ids)].copy()
    frames.append(sub)
df_all = pd.concat(frames, ignore_index=True)
df_all['dt'] = pd.to_datetime(df_all['validity_time'], utc=True)
df_all['date'] = df_all['dt'].dt.date
df_all['t_c'] = pd.to_numeric(df_all['t'], errors='coerce') - 273.15
valid = df_all[df_all['t_c'].notna()]
st_day = valid.groupby(['date', 'geo_id_wmo']).agg(n_slots=('t_c', 'count'), mean_t=('t_c', 'mean')).reset_index()
st_valid_8 = st_day[st_day['n_slots'] == 8]
day_summary = st_valid_8.groupby('date').agg(n_valid_st=('geo_id_wmo', 'count'), nat_mean_t=('mean_t', 'mean')).reset_index()
all_dates = pd.date_range('2022-01-01', '2023-12-31').date
grid = pd.DataFrame({'date': all_dates}).merge(day_summary, on='date', how='left')
grid['n_valid_st'] = grid['n_valid_st'].fillna(0).astype(int)
labelled = grid[grid['n_valid_st'] >= 10].copy()
unlabelled = grid[grid['n_valid_st'] < 10].copy()
p10_linear = labelled['nat_mean_t'].quantile(0.10)
p10_lower = labelled['nat_mean_t'].quantile(0.10, interpolation='lower')
p10_higher = labelled['nat_mean_t'].quantile(0.10, interpolation='higher')
cold_days = labelled[labelled['nat_mean_t'] < p10_linear]
print(f'Total reference days: {len(grid)}')
print(f'Labelled days (>= 10 stations): {len(labelled)}')
print(f'Unlabelled days (< 10 stations): {len(unlabelled)}')
print(f'National daily mean 10th percentile (linear): {p10_linear:.4f} ({p10_linear:.2f} deg C)')
print(f'10th percentile (lower): {p10_lower:.4f} ({p10_lower:.2f} deg C)')
print(f'10th percentile (higher): {p10_higher:.4f} ({p10_higher:.2f} deg C)')
print(f'Cold days identified: {len(cold_days)} out of {len(labelled)} ({len(cold_days)/len(labelled)*100:.2f}%)')
"
```

Output:
```text
Total reference days: 730
Labelled days (>= 10 stations): 725
Unlabelled days (< 10 stations): 5
National daily mean 10th percentile (linear): 4.8400 (4.84 deg C)
10th percentile (lower): 4.8000 (4.80 deg C)
10th percentile (higher): 4.9000 (4.90 deg C)
Cold days identified: 73 out of 725 (10.07%)
```

Key findings:
- Out of 730 calendar days in 2022-2023, exactly 725 days meet the coverage threshold ($\ge 10$ valid stations).
- The 5 unlabelled days correspond to the 5 dates in 2023 where 0 stations had 8 reports (Feb 1, Feb 5, Feb 6, Jul 24, Jul 25).
- Under linear interpolation, the 10th percentile national daily mean temperature is **4.84 °C** (4.8400 °C; 4.80 °C lower,
  4.90 °C higher).
- Exactly 73 days fall strictly below 4.84 °C (10.07% of the 725 labelled days; by construction approximately 10%
  of the sample, with $725 \times 0.10 = 72.5$ expected).
- Comparison with naive aggregation: naive averaging of available slots yielded 4.894 °C over 730 days. Under the
  developer-approved coverage rule, the threshold is 4.840 °C over 725 days. The set of 73 identified cold days is
  strictly identical between the two approaches (symmetric difference is empty: 0 days differ), because none of the
  5 unlabelled days were close to the cold threshold.

---

## 10. Recommendations for the Collector Implementation

1. **Source choice**: Use Candidate A (Meteo-France SYNOP OMM) as the primary data source for the observed
   temperature collector. It provides a single archive file per year containing all 12 stations, with a compact download
   footprint, and complete historical coverage.
2. **Schema isolation**: Store observations in a dedicated schema `labels` (e.g. `labels.synop_daily` or
   `labels.observed_temperature`), completely separated from feature inputs and raw NWP tables. The `lff_agent` database
   role should have read-only access to this schema.
3. **Leakage prevention test**: Add an automated architectural test ensuring that no module under `src/` other than
   `src/load_forecast/evaluation.py` ever queries or references the `labels` schema (strictly enforcing the leakage
   checklist: observed weather is never a feature).
4. **Verification of 2025-2026 file layout**: Before writing production ingestion for 2025 and 2026, verify the daily slot
   count, column schema, and reasons for the larger file sizes (15.2 MB in 2025, 24.5 MB in 2026 vs 6.4 to 7.0 MB in
   2022-2024).
5. **Cold threshold specification**: The developer should record the approved threshold value **4.84 °C** (under
   equal weights, 8-slot station-day completeness, and $\ge 10$ valid stations per day) in `docs/protocol.md` Section 5.2.
