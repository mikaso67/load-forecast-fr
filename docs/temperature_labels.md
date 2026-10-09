# Observed temperature labels

`src/load_forecast/labels.py` keeps Meteo-France SYNOP observations in
`labels.synop`, separate from forecast inputs. Only the developer runs the load.
No feature or model module imports this module or queries the labels schema;
`tests/test_labels.py` enforces this boundary for the current source tree.

Each row keeps the station ID, UTC timestamp, temperature in Kelvin, source file
URL, ingestion time and every original CSV field as a string in JSONB. The primary
key includes a hash of the original row. Repeated loads skip identical rows and
preserve changed source versions without updates or deletes. Each annual archive
is validated before its rows are inserted in one transaction. HTTP timeouts, 429
and 5xx responses receive bounded exponential retries. There is no new dependency.

## Daily labels

`daily_labels(observations, start, end)` returns one row per Europe/Paris date in
the half-open range, including missing dates. `read_labels(connection, start, end)`
reads the same range from PostgreSQL and calls that function.

The result contains `national_temperature_c`, `counting_stations` and nullable
boolean `cold`. Only the eight distinct reports at 00, 03, 06, 09, 12, 15, 18 and
21 UTC count. All eight fall inside the same Paris date, including the 23-hour
and 25-hour DST days. Extra hourly reports are stored but excluded from the mean.
A station with a missing or null report does not count. At least ten of the twelve
specified stations must count; their means receive equal weights. Otherwise the
temperature and cold flag are missing, while the station count stays available.
Cold means strictly below the fixed D9 threshold of 4.84 C. Celsius means are
rounded to ten decimal places to remove floating-point subtraction noise at the
threshold. This does not change the source temperature precision.

Identical station/time/temperature versions count once. Conflicting temperatures,
including a null versus a valid value, raise an explicit error. A source revision
selection policy needs developer approval before such versions can be scored.

## Developer load and access grant

Run these commands from the developer's main repository after accepting the
change, with its administrator `.env`. The shared stack must already be running.
Development credentials cannot create or write the labels schema.

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.labels --years 2022 2023 2024 2025 2026
if ($LASTEXITCODE -ne 0) { throw 'SYNOP load failed' }
Get-Content -Raw -LiteralPath infra/postgres/labels_access.sql | docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres psql -X -U $env:POSTGRES_USER -d load_forecast -v ON_ERROR_STOP=1 -f -
if ($LASTEXITCODE -ne 0) { throw 'Labels access grant failed' }
```

Repeat the load command to resume after a failure or refresh the current year.
Completed years are downloaded again but identical rows are skipped. Ctrl+C stops
the process; the current archive transaction rolls back if interrupted before
commit. Archives are decompressed in memory, with no persistent CSV or GRIB file.

The separate grant script follows `roles.sql` but can run with an existing
`lff_agent`. Run it as the same administrator that creates the collector table.
It grants schema USAGE and table SELECT, revokes schema CREATE and table writes,
and grants SELECT on future tables created by that administrator. It does not
change credentials or provision a new role.

After the load and grant, run from the repository root using its development `.env`:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe scripts/check_labels.py --database
```

This recomputes labels only on 2022-2023, asserts 725 labelled and 73 cold days,
and reports stored row versions and distinct station timestamps per archive year.
The per-year query is:

```sql
SELECT extract(year FROM value_time AT TIME ZONE 'UTC')::int AS year,
       count(*) AS stored_versions,
       count(DISTINCT (station_id, value_time)) AS station_times
FROM labels.synop
GROUP BY 1 ORDER BY 1;
```

Database counts and live permission checks are pending the developer's load.
Repeat the collector and confirm `inserted=0` on unchanged archives to check
database idempotence. No database writes or role changes were run by me.

## Archive verification

The read-only archive check uses the finished parser and daily label function:

```powershell
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe scripts/check_labels.py
```

Measured output on 2026-10-05:

```text
archive=2022 selected_rows=35022
archive=2023 selected_rows=34804
labelled=725 missing=5 cold=73
reference_p10_c=4.840000
```

Newer archive schema and reporting hours can be checked without scoring any model
or computing labels on the locked test year:

```powershell
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe scripts/check_labels.py --inspect-years 2025 2026
```

These checks download observed temperature archives solely for collector format
verification. They do not read consumption, predictions, or any test-year metric.

Measured output on 2026-10-05:

```text
archive=2025 selected_rows=36347 stations=12 utc_hours=[0, 3, 6, 9, 12, 15, 18, 21] off_hour_reports=0 conflicting_station_times=0
archive=2026 selected_rows=26538 stations=12 utc_hours=[0, 3, 6, 9, 12, 15, 18, 21] off_hour_reports=0 conflicting_station_times=0
```
