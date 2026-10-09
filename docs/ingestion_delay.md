# First ingestion of the 09:30 consumption slot

I measure `min(ingested_at) - (value_time + 30 minutes)` for the 09:30 Paris slot in the
real-time eCO2mix dataset, requiring non-null consumption. A row holding only a forecast does not
count as consumption availability. Revisions cannot move the first ingestion time forward.
Dates are Paris dates; storage and subtraction use aware UTC timestamps. The end date is exclusive.

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.ingestion_delay --start 2026-07-01 --end 2026-10-08
```

I ran this command on 2026-10-08. It uses a repeatable-read, read-only PostgreSQL transaction.
It makes no external source request. `--daily` also prints each slot and its first ingestion time.

| Measure | Result |
|---|---:|
| Expected days | 99 |
| Observed days | 95 |
| Missing days | 4 |
| Negative delay days | 0 |
| First ingestion within 120 minutes of slot end | 0 |
| Minimum delay (minutes) | 787.40 |
| Median delay (minutes) | 68467.34 |
| 90th percentile delay (minutes) | 122611.30 |
| 95th percentile delay (minutes) | 129379.30 |
| Maximum delay (minutes) | 136147.30 |

Percentiles use linear interpolation. I keep historical catch-up loads in the distribution rather
than discarding slow days. These large delays describe when this archive first collected the values.
The daily output has no slots for 2026-10-04 through 2026-10-07. All 95 first ingestion timestamps
fall between 2026-10-03 21:07:18.055587 UTC and 21:07:23.876688 UTC, one short collection batch.
They cannot establish the publication delay or the performance of a future 30-minute collector.
First ingestion is an upper bound on publication delay, and missed collection can make that bound
very loose. I keep D2 unchanged. A useful live estimate needs regular collection and a later report.

The query is in `src/load_forecast/ingestion_delay.py`:

```sql
SELECT value_time, min(ingested_at) AS first_ingested_at
FROM raw.eco2mix
WHERE source_dataset = 'eco2mix-national-tr' AND consommation IS NOT NULL
  AND value_time >= %s AND value_time < %s
  AND (value_time AT TIME ZONE 'Europe/Paris')::time = TIME '09:30'
GROUP BY value_time ORDER BY value_time;
```

The two parameters are the UTC boundaries of the requested Paris dates. Tests check slot-end
subtraction, DST boundaries, absent days, duplicate refusal and retention of negative delays.
