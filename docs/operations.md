# Native Windows operations

I run Prefect and its deployment runner natively on Windows (D17). The seven deployments use
Europe/Paris cron schedules; the independent deadline task runs at 12:30 on the Windows clock.
The shared PostgreSQL and MLflow services are managed separately. These commands do not manage Docker.

## 1. Prepare once

I use PowerShell from the repository to install the pinned dependencies in the shared environment:

```powershell
$WorkspacePath = (Get-Location).Path
$PythonPath = (Resolve-Path '../load-forecast-fr-venv/Scripts/python.exe').Path
& $PythonPath -m pip install -r requirements.txt
. ./scripts/load_env.ps1
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
```

I apply `infra/postgres/forecast.sql` with the database owner before enabling schedules. For an
existing installation I follow its migration instructions; the A/B keys must be present.
I then apply `infra/postgres/forecast_scores.sql` once with that owner, adding score identities
and the append-only `forecast.monitoring` table. The runtime needs INSERT/SELECT on both tables.
I apply `infra/postgres/ingestion_index.sql` separately, outside a transaction, with that owner.
The production role has SELECT on input tables and INSERT/SELECT on forecast tables, plus the
snapshot-building privileges described in the snapshot setup. The application never applies these files.

I configure the following environment values in my local `.env`, loaded by `scripts/load_env.ps1`.
Credentials remain in environment variables. I choose a durable absolute `LFF_RUNTIME_DIR` outside
the source checkout, accessible to both the runner and the scheduled task:

```text
LFF_RUNTIME_DIR=C:/load-forecast-runtime
LFF_SNAPSHOT_SCHEMA=<my dedicated writable development schema>
LFF_BASE_CLEAN_TABLE=analytics.load_30min
PREFECT_API_URL=http://127.0.0.1:4200/api
PREFECT_SERVER_EPHEMERAL_ENABLED=false
LFF_PRODUCTION_EXPERIMENT_ID=<existing production MLflow experiment ID>
LFF_AUTHOR=<my configured author>
```

The experiment ID belongs to a separate production experiment on the configured
`MLFLOW_TRACKING_URI`, with proxied MLflow artifacts. I create it in MLflow before enabling schedules.
Each primary attempt records its delivery date, variant, commit, status and models there.
The database settings are `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_PORT`
and `POSTGRES_DB`, as for the collectors.

I provision trusted warm-start `ProductionState` pickles at
`$env:LFF_RUNTIME_DIR/checkpoints/A.pkl` and `B.pkl`, with independent issued histories and the
correct `variant`, production period and frozen settings. Their last issued day precedes the first
scheduled delivery day. These are the reconstructed production checkpoints, never the synthetic
rehearsal states. Section 5 builds them. Fallback bounds are frozen on first preparation from the 2021-2025 definitive
consumption minimum and maximum. I verify those bounds and a full week of complete fallback
preparations before go-live. Scoring uses the reviewed `monitoring.py` functions: the choice-file
forecast is the headline, with selected model B and shadow model A on the same UTC slots.
The daily scoring flow revisits every delivered day through yesterday; the weekly refresh also
revisits older days, appending final consolidated scores beside provisional real-time scores.
It reads stored raw actuals with D12 filtering, latest ingestion per source and slot, and no filling.
RTE* uses the prior full calendar year on the consolidated scale; insufficient coverage is recorded
as unavailable. A `none` choice has no headline score, even when a model completed later.

I read `forecast.scores.metrics` for `served`, `model` (B), `model_a`, `rte_d1` and `rte_star`.
`score_id` identifies the choice, input versions and metrics, so reruns add no duplicate rows.
New actual versions append rows; I select the latest `scored_at` per day and vintage.
`forecast.monitoring` archives CUSUM and 28-day relative skill for served B, model B and model A,
separately by actual vintage. I select the latest `computed_at` per day, vintage, variant and series.
Earlier revisions rebuild subsequent states; unchanged input prefixes retain their identities.
The stored state includes h = 11.46, k = 0.5, missing runs, resets and alerts. Real-time CUSUM
is displayed only (`cusum_alert` is false); consolidated CUSUM may alert. Relative skill alerts
use the reviewed 28-calendar-day window with at least 21 paired days. Alerts request review,
never retrain automatically. PostgreSQL scoring transactions serialize daily and weekly runs.

## 2. Start the server and register deployments

I reload `.env` after editing it. From the same PowerShell window:

```powershell
. ./scripts/load_env.ps1
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
$env:LFF_CODE_COMMIT = (git rev-parse HEAD).Trim()
$env:PREFECT_API_URL = 'http://127.0.0.1:4200/api'
$env:PREFECT_SERVER_EPHEMERAL_ENABLED = 'false'
New-Item -ItemType Directory -Force -Path "$env:LFF_RUNTIME_DIR/logs" | Out-Null
$ServerProcess = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'prefect', 'server', 'start', '--host', '127.0.0.1') -WindowStyle Hidden -PassThru -RedirectStandardOutput "$env:LFF_RUNTIME_DIR/logs/server.out.log" -RedirectStandardError "$env:LFF_RUNTIME_DIR/logs/server.err.log"
Invoke-RestMethod "$env:PREFECT_API_URL/health"
$RunnerProcess = Start-Process -FilePath $PythonPath -ArgumentList @('-m', 'load_forecast.flows', 'serve') -WorkingDirectory $WorkspacePath -WindowStyle Hidden -PassThru -RedirectStandardOutput "$env:LFF_RUNTIME_DIR/logs/runner.out.log" -RedirectStandardError "$env:LFF_RUNTIME_DIR/logs/runner.err.log"
```

If the health request precedes startup, I repeat it before starting the runner. `serve` registers all
seven deployments and polls their schedules. A runner must stay alive; registering alone executes
nothing. I inspect its log and `http://127.0.0.1:4200` for registration and flow failures.
One run per deployment is allowed at a time. Collectors retry with a delay; a new daily attempt is
refused at or after 12:20. Independent A/B state histories run in parallel; only B claims delivery.

Sunday fitting runs in separate hidden processes, so a daily-flow crash does not kill its late refit.
The immutable weekly refit contains its own identity, completion timestamp and Sunday snapshot hash.
Monday may use old models at 12:20; the next day's eligible refit replaces only batch models and the
reset Kalman filter. Issued forecasts, ML-Poly and conformal history remain selected by state runs.
If no refit completed before the next freeze, I fit again from the stored Sunday inputs.
I retain local `inputs`, `refits`, `refit_attempts`, `runs`, `serving` and `incidents` directories with
the database archives. Pickles are trusted local inputs and must never come from untrusted sources.

## 3. Create the independent 12:30 task

The Windows time zone must be `Romance Standard Time`, matching Paris. I check it before registering:

```powershell
if ((Get-TimeZone).Id -ne 'Romance Standard Time') { throw 'Set the Windows clock to the Paris time zone before scheduling.' }
$DeadlineScript = Join-Path $WorkspacePath 'scripts/deadline_check.ps1'
$DeadlineArguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -WorkspacePath "{1}" -PythonPath "{2}"' -f $DeadlineScript, $WorkspacePath, $PythonPath
$DeadlineAction = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $DeadlineArguments -WorkingDirectory $WorkspacePath
$DeadlineTrigger = New-ScheduledTaskTrigger -Daily -At '12:30'
$DeadlineTrigger.StartBoundary = (Get-Date).ToString('yyyy-MM-dd') + 'T12:30:00'
$DeadlineSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$DeadlinePrincipal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'LoadForecastDeadline' -Action $DeadlineAction -Trigger $DeadlineTrigger -Settings $DeadlineSettings -Principal $DeadlinePrincipal
Get-ScheduledTask -TaskName 'LoadForecastDeadline'
[xml]$DeadlineXml = Export-ScheduledTask -TaskName 'LoadForecastDeadline'
$Boundary = $DeadlineXml.Task.Triggers.CalendarTrigger.StartBoundary
if ($Boundary -notmatch '^\d{4}-\d{2}-\d{2}T12:30:00$') { throw 'Deadline boundary must be local Paris time, without Z or offset.' }
$Boundary
```

This configuration requires my Windows session to remain logged in and the PC awake. It grants no
whole-PC outage guarantee. The task reloads `.env` and calls `load_forecast.deadline`; it imports no
Prefect and opens no database. It reads prepared files and exclusively creates the day's choice.
A previously claimed primary stays served. With no choice, the check may publish a fallback only
from 12:30:00 inclusive to 12:31:00 exclusive Paris time, including the final publication check.
After that window it records `none` with a reason, a local incident and exit code 2, so a PC waking
late cannot serve a late forecast. Catch-up is disabled. The boundary has no UTC suffix or offset,
so it follows the Windows Paris clock across daylight saving changes. Missing
fallback means `none`, a local incident, an error log and exit code 2. A check failure exits 1 and
can be retried. It never computes a forecast. SQL reconciliation runs later through Prefect.
Preparation failures log an error and persist an incident immediately, then fail the flow visibly.

## 4. Rehearse and stop

The offline rehearsal invokes all seven flow bodies with synthetic data and private disposable SQL
tables, real model fits, A/B archives and the filesystem deadline protocol. It bypasses Prefect's
server and substitutes the collector/MLflow adapters. It is not a live publication-delay measurement
or a shadow week on production data. I choose a past production delivery day:

```powershell
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
& $PythonPath -m load_forecast.flows dry-run --day 2026-10-09
& $PythonPath -m pytest -q
& $PythonPath -m ruff check src tests scripts
```

I disable the independent task before stopping the runner. These commands use only the processes
started in this PowerShell session:

```powershell
Disable-ScheduledTask -TaskName 'LoadForecastDeadline'
Stop-ScheduledTask -TaskName 'LoadForecastDeadline'
taskkill.exe /PID $RunnerProcess.Id /T /F
taskkill.exe /PID $ServerProcess.Id /T /F
# To remove the registration permanently:
Unregister-ScheduledTask -TaskName 'LoadForecastDeadline' -Confirm:$false
```

These commands stop the two process trees, including active flow/refit children. I use them only
for shutdown: a forced stop can leave a start without a result or an unfinished refit. I inspect
incidents and retry unfinished work after restart. I do not delete choice files or selected state
runs to retry a delivery; their first-writer decisions are immutable.

### 4.1 Recover orphan flow runs

The watchdog calls `python -m load_forecast.ops cancel-orphans --older-than 900` on a
healthy pass, cancelling only stale Pending runs. Running and Cancelling are always preserved
while the runner is alive, regardless of their age. On restart it uses `--older-than 0 --restart`,
after the old runner has stopped and the server health check succeeds, before starting the new
runner. A failed cleanup writes `logs/orphan_recovery.log` and a JSON incident in `incidents`,
then the runner starts anyway. On a healthy pass the failure is also logged and the watchdog
exits successfully. Diagnostic storage errors produce a warning and do not prevent startup.
I can run the same command manually with `PREFECT_API_URL` explicitly configured; it never starts an implicit
server. Only the seven project deployment names with the matching production flow entrypoint
are eligible. Scheduled runs, including AwaitingConcurrencySlot, are always left alone.
Only explicit restart recovery includes Running and Cancelling; it requires age zero and a
stopped runner. Age means time spent in the current state. The command re-reads each run before
cancellation and leaves a state that changed since listing alone. Recovery releases the deployment slots
through Prefect's API and does not change concurrency limits or schedules.

The 900-second threshold applies only to Pending runs. The longest completed run in the Prefect
history checked on 21 October 2026 took 640.75 seconds, but a legitimate forecast with a refit can
exceed 900 seconds. Its Running state is never an age-based cancellation target on a healthy pass.
On restart, zero is safe only after the old runner and its children have stopped.

Primary archives record `flags.degraded_inputs` as a list of reasons if any expert uses its
backup chain or the last consumption slot precedes the existing D2 boundary (09:30 Paris on
D-1). The runtime raises a local incident after the serving attempt, so alert persistence cannot
delay or veto publication. The public page labels the affected delivery dates "Degraded inputs".
If the quality diagnosis itself fails, the archive records `['unknown']`, the error is logged,
and the same alert path runs after serving. A diagnostic failure never fails the forecast.
Older immutable archives get the same label from their stored expert backups and consumption
timestamp. Forecast values, model settings and serving choices are unchanged.

Scoring waits for finite consumption and ingestion provenance at every half-hour slot of each
local delivery day: 48 normally, 46 in spring and 50 in autumn. A later daily or weekly run
automatically revisits incomplete days and appends the corrected scores and monitoring history;
earlier rows stay in the append-only archive. After three calendar days in Paris, a day with no
complete actual vintage gets one local `scoring_incomplete` incident, retained across retries.
It remains unscored until complete. A complete real-time day does not trigger an incident merely
because consolidated actuals have not been published yet. To revisit the complete history through
20 October using the production writer environment, I run:

```powershell
& $PythonPath -c "from datetime import date; from load_forecast.pipeline import make_runtime; print(make_runtime().score(date(2026, 10, 20)))"
```

This operation needs the production role's forecast schema privileges. It does not collect data,
retrain models, alter a delivery, or publish the site.

## 5. Warm start and file retention

### 5.1 Build the checkpoints

The checkpoints come from a replay of the production function from the cold start of Monday
2026-01-05 (D20 P2 A) to the last day whose inputs are complete. 2026-01-01 to 2026-01-04 stay
without forecast. I run each variant in its own process, then combine them:

```powershell
. ./scripts/load_env.ps1
$env:PYTHONPATH = Join-Path $WorkspacePath 'src'
& $PythonPath scripts/warm_start_replay.py run --variant A
& $PythonPath scripts/warm_start_replay.py run --variant B
& $PythonPath scripts/warm_start_replay.py combine
```

The run refuses uncommitted code, because its manifest names the commit that produced the state.
It first rebuilds ML-Poly and the conformal pool from the archived 2023 to 2025 expert forecasts and
stops unless the weights and aggregates equal the archive (docs/production_design.md section 2.4).
Then it replays every day with Monday refits, quarterly Kalman variances (cold in the first
quarter), filter restarts and daily ML-Poly and pool updates, exactly as the daily flow calls them.
Two rules of the replay differ from a live day, both forced by the stored data:
- consumption: every stored row was ingested after the freeze of every replayed day, so the
  ingestion filter of P1 would leave no history. The replay uses the backtest rule (D2 cut only):
  the documented revision leak;
- revision mapping (D21): offsets are estimated at each day's freeze from rows stored before it,
  as in production. No consolidated month was stored before any replayed freeze, so the offsets
  are zero and variant B equals variant A on every replayed day. The variants separate from the
  first live day, when the mapping finds the stored July and August months.

Outputs under `LFF_RUNTIME_DIR`:
- `checkpoints/A.pkl`, `checkpoints/B.pkl` and their `*.manifest.json` (commit, settings hash,
  last day, first scheduled day, variant, checkpoint SHA-256, refit and Kalman quarter);
- `replay/replay_forecasts.parquet`: one row per variant and slot, with the aggregate, the four
  experts, their backup flags, the weights, the interval and the pool size;
- `replay/replay_scores.parquet`: one row per variant and day, scored with the production scoring
  on the actual vintage of the day (real-time days without RTE*);
- `replay/report_<variant>.json`, `replay/summary.json` and the progress logs.
Every replayed row has `status = replay`. A replayed day was never served and is never reported as
a production result.

If go-live is later than the day after the checkpoint, I extend the checkpoints once the missing
days are collected, instead of leaving a gap: `run --variant A --resume` (then B, then `combine`).
The previous checkpoint moves to `checkpoints/history/`. A checkpoint only moves forward.

### 5.2 What the pipeline writes

Sizes measured on the replay (pickle, one variant):

| File | Written | Size |
|------|---------|------|
| `refits/<variant>/<monday>.pkl` | once a week | models 87 to 99 MB plus Kalman part 5.5 to 6.3 MB, growing with the history |
| `runs/<run_id>/models_<refit>_<id>.pkl` | every run | a second copy of the week's models, 87 to 99 MB |
| `runs/<run_id>/state_<day>.pkl` | every run | 6.7 to 7.6 MB (ML-Poly, pool history, Kalman window and filter) |
| `inputs/<day>.pkl` | every day, shared by A and B | about 15 MB (clean history and pinned temperature since 2021) |
| `checkpoints/<variant>.pkl` | once | 107 MB |

LightGBM (6,400 rounds) is most of a model file. Without pruning, two variants write about
2 x 365 x (95 + 7) MB for the runs, 2 x 52 x 100 MB for the refits and 365 x 15 MB for the inputs:
about 85 GB a year, which would fill the free disk of this PC in about a year and a half.

### 5.3 Retention rule

What must stay is what a reproduction of a past forecast and the late refit rule (D24) read:
- the models actually used on a day, identified by `refit_id` in its run result;
- the state the day started from, that is the state file of the selected state run of the day before;
- the frozen inputs of the day, which can be rebuilt from the append-only raw tables (clean snapshot
  at F and the pinned weather identities of `forecast.run_weather`) and checked against the stored
  `feature_hash` and snapshot hash;
- for D24, this week's refit of each variant and the Monday frozen inputs.

I keep:
1. Forever: one model file per variant and week, `refits/<variant>/<monday>.pkl`, plus any
   `refit_attempts/<id>.pkl` whose `refit_id` a state run used. This is the only full copy of the
   models; a LightGBM refit is not guaranteed to be bit-identical when refitted later.
2. Forever: the state file of every selected state run of each variant, and the checkpoints with
   their history and manifests.
3. 14 days: whole run folders, including their copy of the models. After 14 days I delete only the
   `models_*.pkl` copy in a run folder, after checking that a kept refit file (rule 1) holds models
   with the same identity. To reload such a state I load its models from that refit file; the copy
   in the run folder is never the only one. The latest selected state run of each variant always
   keeps its full folder, because the next daily run loads it.
4. 14 days: `inputs/<day>.pkl`, except the Monday inputs of the current and previous week (D24).
   Older inputs are rebuilt on demand and must give the stored hashes.
5. Run folders of runs that never became a state run (late primaries, duplicates): 28 days, then
   deleted, because nothing reads them; their database rows stay.
6. `serving`, `incidents` and the database archives are not pruned.

Steady disk use with this rule: about 2 x 52 x 100 MB = 10.4 GB a year for refits and
2 x 365 x 7.5 MB = 5.5 GB a year for states, about 16 GB a year in total, plus a constant window of
about 2 x 14 x 95 MB + 16 x 15 MB = 2.9 GB for recent run models and inputs. The size per refit grows
slowly with the training history (13 % over 2026 so far).

### 5.4 Weekly pruning

The Monday 02:00 Paris `consolidated_refresh` flow runs `prune` after collection, refresh and
scoring succeed. I inspect a plan without deleting files with:

```powershell
& $PythonPath -m load_forecast.pipeline prune --dry-run
# Apply the same retention rules manually:
& $PythonPath -m load_forecast.pipeline prune
```

The weekly flow also accepts `dry_run=true` for its pruning step; collection and scoring still
run normally. Retention logs each planned or actual deletion and its reason. It keeps all refits
and refit attempts conservatively, all selected state files and the full latest selected folder
of each variant. Older duplicate model files are removed only after their original SHA-256 is
verified and a kept refit has the same identity and prediction parameters (OLS coefficients,
GAM coefficients, knots, bounds and tail slopes, and LightGBM trees). Fitted-object pickle bytes
can differ because of internal caches and object sharing. A small immutable `refit_<identity>.pkl`
reference beside the state pins its original model hash, the kept refit archive hash and the
prediction-parameter signature. Loading an older selected state verifies that reference and
the kept archive before resolving its models. A missing or mismatched refit refuses
the entire plan before deletion. Unknown file names are kept; unknown input dates fail clearly.
Unselected folders older than 28 days are removed only when no unfinished database run protects
them. A PostgreSQL advisory transaction lock shared with run completion prevents selected-state
decisions changing during pruning, without adding UPDATE or DELETE privileges to the application.
Linked paths and Windows junctions are refused. Checkpoints, their history, serving files,
incidents, database rows and refit artifacts are never deleted. Current and previous Monday
inputs stay for D24, alongside the rolling 14-day input window. Pruning failure fails the weekly
flow visibly and leaves scoring already committed; a rerun safely resumes retention.

## 6. Daily static site publication

I set `LFF_SITE_CLONE` to an absolute path to a dedicated clean clone already on `gh-pages`,
and `LFF_REPLAY_DIR` to the directory containing `replay_forecasts.parquet` and
`replay_scores.parquet` (for example `C:/load-forecast-replay/replay`). I set
`LFF_CLEAN_TABLE=analytics.load_30min`. The exporter never loads `.env` itself.
I set `LFF_DATABASE_URL` to a separate read-only connection when `POSTGRES_USER` is the production
writer. For a standalone exporter configured with reader credentials, that URL is optional and
it uses explicit `POSTGRES_USER` and `POSTGRES_PASSWORD`. The site reader needs USAGE on `forecast` and the clean
schema and SELECT on choices, values, fallbacks, scores, run_results, experts and the configured
clean relation. Input-quality labels read run_results and experts for the selected primary runs.
The application never grants those permissions.

I publish twice each day: after the 08:00 Paris scoring and at the end of the noon forecast
flow, after reconciliation, when the delivery day's primary or fallback choice exists.
The noon publication makes the newly served next-day forecast visible without waiting for
the next morning's scores. A missing choice or a `none` choice skips that publication.
`publish_site` writes `site/data/latest.json` from stored served choices, scores and replay
archives, then invokes `scripts/publish_site.ps1` with the configured clone.
The clone must have a clean working tree and already be on the publication branch; the
script refuses to switch branches or commit unrelated work. It copies the site, commits changed
files and publishes through Git Credential Manager. Git errors propagate to the publication
adapter. A previously created local commit is retried even if the next export has no changes.
`Runtime.publish_site(day, dry_run=True)` exports and commits locally but skips publication.
The offline pipeline rehearsal substitutes publication and never touches a clone.

Publication has a 180-second subprocess timeout. Configuration, database, export and git failures
record `site_publication_failed:<exception type>` in the runtime incidents directory and return
a failed publication result, while the forecast or scoring flow remains successful. Raw exceptions
and captured subprocess output are not logged because they may include connection details.
I inspect incidents and runner logs for a stale site; publication cannot undo scoring or alter
the serving choice. Automated tests exercise a disposable clone with a local bare remote and
publication disabled; live authentication and remote publication are deployment checks.

## 7. Windows ports

At boot, Windows (Hyper-V and WSL) reserves random TCP port ranges. If a range covers a published port, Docker starts the container without its host binding: `docker compose ps` shows `5432/tcp` instead of `127.0.0.1:5432->5432/tcp`, and Prefect fails with a refused connection. This happened on 2026-10-24 and made the 2026-10-25 delivery a fallback.

The start script recreates the containers once when 5432 or 5000 is not reachable. The lasting fix is to reserve both ports, once, in an administrator PowerShell, with the containers stopped:

```powershell
cd C:\Users\Mikail\dev\load-forecast-fr
docker compose --env-file .env -f infra/docker-compose.infra.yml stop
net stop winnat
netsh int ipv4 add excludedportrange protocol=tcp startport=5432 numberofports=1
netsh int ipv4 add excludedportrange protocol=tcp startport=5000 numberofports=1
net start winnat
docker compose --env-file .env -f infra/docker-compose.infra.yml up -d --wait
```

`netsh int ipv4 show excludedportrange protocol=tcp` lists the reservations; administered ones are marked with `*`. The Prefect port is chosen at each start among 4200 and 8420 to 8424 and needs no reservation.
