# Weather staging and frozen selection

I use `load_forecast.weather_production` for the production collection path. The historical
`weather_collector` command keeps its existing cutoff guard and backfill behavior.

I stage both D11 candidate runs, one available file/index pair at a time, without writing a
`raw.gfs_days` row. Partial runs can therefore enter `raw.gfs_points` before the freeze. Downloads
reuse the byte-range, ETag, LastModified, decoding and retry checks of the historical collector.
Each step commits separately. Duplicate point identities are skipped by the existing primary key.
An interrupted pass can be rerun; it downloads available pairs again. No scheduler is installed.

From the repository root, with the shared environment and collector write credentials loaded:

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_production stage --day 2026-10-09 --variables TMP
& ../load-forecast-fr-venv/Scripts/python.exe -m load_forecast.weather_production select --day 2026-10-09 --variables TMP
```

I run staging during collection and selection at or after 12:00 Paris on D-1. Selection refuses
before that time, even before opening the database connection in the CLI. A late invocation still
uses the original freeze. Selection reads stored data and makes no HTTP request. The requested
variables define completeness; TMP is the current model input. I can stage and select all supported
variables by listing `TMP RH UGRD VGRD DSWRF TCDC` in both commands.

For each required step, variable and point, I require both file and index LastModified strictly
before the freeze. I choose the revision with the latest eligible file LastModified. Missing points
or a tie between different identities reject the run. I try primary, fallback, then weather missing.
Staged rows already passed the nonempty file/index checks. Historical stored rows remain usable.

The append-only `raw.gfs_days.original_metadata` stores the cutoff, variable set, rejected point keys,
exact point identities and available staged object metadata. Its SHA256 is the selection hash.
Older collector rows did not retain size and ETag on points, so those object details are absent when
a new selection uses older points; the stored file/index LastModified and pinned identities still
apply. I do not rewrite historical manifests. A subsequent forecast run must retain the returned
`(delivery_day, selection_hash)` in its own run record. The forecast run schema is a later package.

`read_pinned_temperature(connection, day, selection_hash)` reads that exact manifest, verifies its
hash and resolves each `(run_init_time, end_step, variable, point, message_hash)` identity. It ignores
other day manifests and point revisions, and refuses missing or ambiguous pinned rows. Interpolation
uses the existing Celsius feature logic and produces 46 or 50 slots on DST days. The historical
range reader still refuses ambiguous selections; production replay uses the pinned reader.

I verified partial staging before the freeze, early selection refusal, fallback on a tie, latest
eligible revision selection, file/index boundary exclusion, missing weather, manifest corruption,
and replay after a later point revision and a second day manifest with synthetic data. I have not
run the new production collector against the external source or changed database infrastructure.
