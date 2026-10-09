# Production orchestration design

I design the daily production orchestration for `load-forecast-fr`. This document defines the operational pipeline steps around developer decision D16, identifies an open question on missing-lag handling, and compares three orchestrator options for execution on a single Windows development machine.

## 1. Daily production steps under decision D16

Developer decision D16 (protocol section 2.4, approved 2026-10-06) established that the 12:00 Europe/Paris cutoff on day D-1 is an information cutoff rather than a forecast delivery time. Forecast inputs are frozen at 12:00 Paris, and the forecast is computed after the freeze and delivered before 12:30 Paris.

The daily production pipeline consists of the following ordered steps:

### 1.1 Step 1: Morning GFS weather staging (Candidate acquisition)
- Action: Download candidate NOAA GFS 0.25 degree forecast files for delivery day D (steps f021 to f047) using HTTP byte-range requests on `.idx` files. The collector can also pre-download the 18 UTC run of D-2 (steps f027 to f053) as a candidate fallback.
- Operational staging and selection: Candidate acquisition may run in the morning, but no selection decision is made before 12:00 Europe/Paris. At 12:00 Paris, run selection is evaluated strictly under protocol D11.
- Collector code constraint: In `src/load_forecast/weather.py:144-145` and `weather_collector.py:222-227`, the current implementation raises `ValueError` or skips collection when `now < forecast_cutoff(delivery_day)`. Morning staging requires future collector work to allow pre-fetching while deferring selection until 12:00 Paris.

### 1.2 Step 2: Morning eCO2mix consumption collection
- Action: Query the ODRE API for real-time half-hourly national consumption (`eco2mix-national-tr`) and append records to `raw.eco2mix`.
- Operational collection and selection: Collection may run in the morning, but consumption selection is deferred to 12:00 Europe/Paris under protocol D2.
- Protocol rule: Protocol decision D2 restricts consumption inputs to half-hours ending at least 2 hours before cutoff. For a 12:00 Paris cutoff, the latest usable half-hour ends at 10:00 Paris.

### 1.3 Step 3: dbt clean layer build
- Action: Execute `dbt build --select clean` to transform `raw.eco2mix` into `clean.load_30min`.
- Dependencies: Requires Step 2 (eCO2mix collection) to complete before cutoff.
- Protocol rule: Protocol decision D12 eliminates phantom spring 02:xx clock-change records, preserves autumn repeated hours as given, samples half-hourly slots (:00 and :30), and prioritizes consolidated records over real-time telemetry. On spring DST transition, the clean model produces 46 half-hour slots. On autumn DST transition, the wall-clock day contains 50 half-hour slots (48 actual records in `clean.load_30min` due to missing raw hours; common-slot scoring skips absent slots).

### 1.4 At 12:00 Paris: Cutoff and information freeze
At 12:00 Europe/Paris on day D-1, the orchestrator freezes all input features using the exact backtest rules:
1. Weather selection: Evaluated strictly at 12:00 Paris using the D11 completeness test (`LastModified < 12:00 Paris` converted to UTC). If the 00 UTC run is complete, it is selected. If not, the 18 UTC run of D-2 is evaluated under the same strict test. If both fail, delivery day D is flagged as `weather_missing`.
2. Consumption selection: Frozen at the last usable slot ending at or before 10:00 Paris under D2.
3. Calendar selection: Evaluated deterministically for delivery day D.

### 1.5 Step 4: Weekly Monday refit
- Action: Refit batch forecasting models (SlotOLS, GAM, LightGBM) across all 48 local half-hour slots using the expanding historical training dataset up to the frozen cutoff.
- Cadence: Executed on Mondays only after the 12:00 Paris freeze. Skipped Tuesday through Sunday.
- Protocol rule: Protocol decision D6 specifies weekly refits on Monday. Cutoff rules restrict training data to consumption ending on or before 10:00 Paris and weather forecasts issued before 12:00 Paris.

### 1.6 Step 5: Daily forecast generation and parallel fallback computation
- Action: Generate predictions for delivery day D (48 slots ordinary, 46 spring DST, 50 autumn DST) using frozen inputs and active model checkpoints. Compute point forecasts and 80% conformal prediction intervals. Update online model states (Kalman filter, aggregation weights) using losses available at cutoff.
- Weather fallback (M11): If weather is flagged `weather_missing`, the primary model routes to the EXP-002 calendar linear model (modeling plan M11).
- Late fallback computation (D16): Right after the 12:00 freeze, the orchestrator computes the fallback forecast (EXP-002 calendar model, then seasonal naive baseline) in parallel with the main model. This ensures a fallback forecast is ready if the main forecast is delayed.
- Persistence: Predictions are persisted to PostgreSQL and logged to MLflow.

### 1.7 Step 6: Forecast delivery and API serving
- Action: Verify that the newly written forecast is available and serve it via `GET /forecast/{delivery_date}`.
- Deadline: Delivered strictly before 12:30 Europe/Paris on D-1 (decision D16).
- Late fallback delivery (D16): If the primary forecast is not ready at 12:30 Paris, the precomputed fallback forecast is served at 12:30 Paris, the run is flagged as late, and an operational alert is raised.
- Future serving work: In `src/load_forecast/api/app.py:68-76`, the current implementation generates predictions on demand. Future serving work is required to read persisted predictions from PostgreSQL.

### 1.8 Step 7: Drift check and monitoring
- Action: Evaluate model residuals for historical days where consolidated consumption has arrived. Compute trailing MAE, mean bias, and tracking signals against raw RTE D-1, RTE* a2, and SYNOP observed temperatures. Track drift metrics in MLflow.
- Timing: Executed after delivery, outside the delivery critical path.

Conclusion: Decision D16 establishes an information freeze at 12:00 Paris and delivery before 12:30 Paris, ensuring identical inputs between backtest and production.

## 2. Open question for the developer: Missing 7-day lag handling

In the backtest implementation (`src/load_forecast/evaluation.py:398-432`, `baseline.py:33`, and `backtest.py:57-70`), an absent historical UTC slot leaves the 7-day seasonal lag as NaN. In common-slot backtest scoring (`tests/test_baseline.py:16-27`), slots with missing predictions are excluded from evaluation.

In operational production, protocol section 3.1 requires that every day be forecast. If an operational run encounters a missing 7-day historical lag in raw consumption, leaving NaN values would result in an incomplete forecast delivery.

The developer must decide how production handles a missing 7-day lag so that backtest and production behavior match:
1. Option A: Route slots with missing 7-day lags to the EXP-002 calendar linear model (which does not depend on consumption lags).
2. Option B: Adopt a defined imputation rule (such as the last observed consumption value known at cutoff) applied identically in both backtest and production.
3. Option C: Leave missing slots unserved and raise an operational alert, keeping backtest and production strictly aligned on NaN exclusion.

## 3. Option comparison

I evaluate three orchestration architectures for this project: Prefect, Apache Airflow, and native Windows Task Scheduler. The target environment is a single Windows developer workstation running Docker Compose for PostgreSQL 16.4 and MLflow 3.16.1.

### 3.1 Prefect (Prefect 3.x)

- Setup cost:
  - Installation: Installable via `pip install prefect` in the shared Python virtual environment [1].
  - Infrastructure: Runs natively on Windows using PowerShell or via the official Docker Compose deployment [1, 2].
  - Execution model: When run natively on Windows, Prefect executes directly as a Windows process or as a Windows service managed via NSSM [1]. It supports local `Process` work pools executing Python directly on the host, or `Docker` work pools [1, 4].
  - Docker Compose alternative: The official Docker Compose configuration provides 5 services (`postgres`, `redis`, `prefect-server`, `prefect-services`, and `prefect-worker`) exposing port 4200 [2].
  - Database: Uses SQLite by default (`%USERPROFILE%\.prefect\prefect.db`), or connects to PostgreSQL via `PREFECT_SERVER_DATABASE_CONNECTION_URL` [1]. For multi-server scaled deployments, Prefect documentation specifies PostgreSQL 14.9 or higher and Redis [3].
  - Memory footprint: Executes locally as a Python process [unverified memory estimate; architecture documented in 1].

- Retries and alerting:
  - Task and flow retries: Declared via Python decorators (`@flow(retries=...)` and `@task(retries=3, retry_delay_seconds=60)`) with configurable delay intervals [5].
  - Alerting: Includes an Automations engine in the UI and CLI [6]. It triggers webhooks, emails, or Slack messages on flow run failures or cancellations [6].

- Run history:
  - Web UI: Dashboard accessible at `http://127.0.0.1:4200` [1].
  - Features: Automatically displays dynamic task dependency graphs inferred from Python function calls, execution logs, and flow run parameters [7, 8].

- Fit with Docker and Windows:
  - Windows compatibility: Native Windows support. CLI, server, and worker processes run natively in PowerShell [1]. Can run as a Windows service via NSSM [1]. Does not require WSL2 or Linux containers.
  - Docker integration: Can be added as services in Docker Compose or executed as a local Windows process connecting to Docker PostgreSQL [1, 2].

- What a recruiter sees:
  - In my judgment as author, Prefect demonstrates a modern Python-first data orchestration stack commonly used in production ML platforms [author's judgment].
  - In my judgment as author, it signals competence in declarative workflows and operational observability without unnecessary infrastructure bloat [author's judgment].

Conclusion: Prefect offers native Windows execution, low memory footprint [unverified memory estimate], and automated DAG tracking with minimal setup friction [1, 7, 8].

### 3.2 Apache Airflow (Docker Compose deployment)

- Setup cost:
  - Deployment model: Compared against the official Apache Airflow Docker Compose deployment (Airflow 3.3.2 using CeleryExecutor) [10].
  - Infrastructure: The official Compose configuration defines multiple services: scheduler, triggerer, api-server, dag-processor, worker, message broker (Redis), and a dedicated postgres database initialized with an init container [10].
  - Database: Requires a dedicated PostgreSQL metadata database initialized via `airflow-init` [10].
  - Resource guidance: Official documentation explicitly states: allocate at least 4 GB memory (8 GB ideally recommended) to the Docker Engine for this Compose setup [10]. This represents the Docker Engine memory allocation recommendation for the multi-container stack, not a measured RAM consumption of a standalone core process [10].
  - Configuration: Requires setting environment variables, UID mapping, and volume mounts for DAGs, logs, and plugins [10].

- Retries and alerting:
  - Task retries: Configured on operators via `retries` and `retry_delay` arguments [11].
  - Alerting: Includes callback functions such as `on_failure_callback` and `on_retry_callback` [12].

- Run history:
  - Web UI: Web dashboard accessible on port 8080 (`http://localhost:8080`) [10].
  - Features: Provides Grid view, Graph view, task run history, code view, and event logs [13].

- Fit with Docker and Windows:
  - Windows compatibility: Airflow is designed for POSIX-compliant systems and is not natively supported on Windows [9]. The official prerequisites documentation states that Windows users must run Airflow using WSL2 or Linux-based containers [9].
  - Docker Compose: Runs alongside PostgreSQL and MLflow, adding 5 or more containers to the workstation [10].

- What a recruiter sees:
  - In my judgment as author, Airflow represents the widely recognized enterprise standard for traditional data engineering [author's judgment].
  - In my judgment as author, an interviewer may question whether a multi-container Airflow cluster is overengineered for seven daily steps on a single PC [author's judgment].

Conclusion: Airflow provides enterprise workflow governance but recommends 4 to 8 GB Docker allocation [10] and requires WSL2 or Linux containers on Windows [9].

### 3.3 Windows Task Scheduler

- Setup cost:
  - Software installation: Zero external software installation required. Built directly into the Windows operating system as the `Schedule` service [14].
  - Infrastructure: Runs as a native Windows service (`Schedule` service, `svchost.exe`) managed via `schtasks.exe` or the Task Scheduler management console (`taskschd.msc`) [14, 15].
  - Database: Zero metadata database requirements [14].
  - Memory footprint: Executes as native OS service without separate orchestrator web servers [unverified memory overhead; 14].
  - Workflow definition: Requires a top-level PowerShell wrapper script that sequences Python module calls [author's proposed operational pattern].

- Retries and alerting:
  - Retries: Supports automatic task restart on failure via `RestartInterval` and `RestartCount` in task settings [16]. Step-level retry logic must be coded in scripts [author's proposed operational pattern].
  - Alerting: Modern Windows Task Scheduler interfaces do not provide built-in webhook integrations, and email action functionality is deprecated by Microsoft in current Windows versions [unverified action status; 14]. Alerting must be handled within PowerShell/Python scripts [author's proposed operational pattern].

- Run history:
  - History tracking: Logged in the Windows Event Viewer under `Applications and Services Logs > Microsoft > Windows > TaskScheduler > Operational` and in the Task Scheduler MMC snap-in History tab [17].
  - Dashboard: Lacks a dedicated web dashboard, DAG dependency graphs, and centralized log indexing [unverified comparison with web-based orchestrators; 14, 17].
  - Logs: Script output must be redirected manually to flat text files or MLflow [author's proposed operational pattern].

- Fit with Docker and Windows:
  - Windows compatibility: 100% native integration with Windows PowerShell.
  - Docker Compose: Interacts cleanly with Docker containers by executing scripts that target published container ports (`localhost:5432`, `localhost:5000`) [author's proposed operational pattern].

- What a recruiter sees:
  - In my judgment as author, Task Scheduler demonstrates pragmatic simplicity and restraint for single-machine deployment [author's judgment].
  - In my judgment as author, it lacks data engineering abstractions, which some recruiters may view as basic shell scripting rather than production workflow orchestration [author's judgment].

Conclusion: Windows Task Scheduler provides native OS execution without running extra orchestrator web servers [unverified memory overhead], but lacks a web dashboard and DAG visualization [unverified comparison; 14, 17].

## 4. Comparison matrix

I summarize the three alternatives across technical and operational dimensions.

| Dimension | Prefect (3.x) | Apache Airflow (Docker Compose) | Windows Task Scheduler |
|---|---|---|---|
| Native Windows support | Yes (PowerShell, CLI, process worker) [1] | No (requires WSL2 or Linux containers) [9] | Yes (native OS service) [14] |
| RAM footprint | Process execution [unverified memory estimate; 1] | 4 to 8 GB Docker allocation recommendation [10] | OS service overhead [unverified memory overhead; 14] |
| Container count | 0 (local process) [1] or 5 (official Compose) [2] | 5+ dedicated containers [10] | 0 containers [14] |
| Setup complexity | Low (`pip install prefect`, local server) [1] | High (multi-service Compose, init step) [10] | Minimal (`schtasks` or GUI) [15] |
| Metadata database | PostgreSQL (shared container) or SQLite [1, 3] | Dedicated PostgreSQL database [10] | None [14] |
| DAG and step retries | Built-in decorators with configurable delays [5] | Built-in operator arguments with delays [11] | Script-level code required [author's pattern] |
| Failure alerting | Native Automations (webhooks, email, Slack) [6] | Callbacks and notification providers [12] | Custom script exception handlers [author's pattern] |
| Web UI and history | Yes (`http://localhost:4200`) [1, 7, 8] | Yes (`http://localhost:8080`) [10, 13] | No web UI (history in Task Scheduler MMC snap-in History tab and Event Viewer) [14, 17] |
| Recruiter perception | Modern ML engineering stack [author's judgment] | Traditional enterprise standard [author's judgment] | Pragmatic shell automation [author's judgment] |

Conclusion: Prefect delivers DAG visualization and automated retries with a lightweight process footprint [unverified memory estimate], Airflow offers enterprise features at a 4 to 8 GB Docker allocation recommendation [10], and Task Scheduler offers OS execution with history in the Task Scheduler snap-in and Event Viewer [14, 17].

## 5. Trade-off

Windows Task Scheduler provides a lightweight option executing a sequenced PowerShell script natively without extra server processes [unverified resource overhead], with run history tracked in the Task Scheduler MMC snap-in and Windows Event Viewer [14, 17]. Apache Airflow delivers full enterprise DAG governance and extensive industry recognition, yet the official Compose deployment (CeleryExecutor, Airflow 3.3.2) recommends allocating at least 4 GB (ideally 8 GB) to Docker Engine [10] and cannot run natively on Windows without WSL2 or Linux containers [9]. Prefect balances these extremes by running natively on Windows without requiring Linux containers while providing programmatic task dependencies, automated retries, and a web UI [1, 5, 7]; the developer decides which trade-off fits the project roadmap.

## 6. References

- [1] Prefect Technologies. (2024). *How to run Prefect on Windows*. Prefect Documentation. https://docs.prefect.io/v3/how-to-guides/self-hosted/server-windows
- [2] Prefect Technologies. (2024). *How to run the Prefect Server via Docker Compose*. Prefect Documentation. https://docs.prefect.io/v3/how-to-guides/self-hosted/docker-compose
- [3] Prefect Technologies. (2024). *How to scale self-hosted Prefect*. Prefect Documentation. https://docs.prefect.io/v3/advanced/self-hosted
- [4] Prefect Technologies. (2024). *Work Pools*. Prefect Documentation. https://docs.prefect.io/v3/concepts/work-pools
- [5] Prefect Technologies. (2024). *How to automatically rerun your workflow when it fails*. Prefect Documentation. https://docs.prefect.io/v3/how-to-guides/workflows/retries
- [6] Prefect Technologies. (2024). *How to create automations*. Prefect Documentation. https://docs.prefect.io/v3/how-to-guides/automations/creating-automations
- [7] Prefect Technologies. (2024). *Flows*. Prefect Documentation. https://docs.prefect.io/v3/concepts/flows
- [8] Prefect Technologies. (2024). *Getting Started*. Prefect Documentation. https://docs.prefect.io/v3/get-started/index
- [9] Apache Software Foundation. (2024). *Prerequisites*. Apache Airflow Documentation. https://airflow.apache.org/docs/apache-airflow/stable/installation/prerequisites.html
- [10] Apache Software Foundation. (2024). *Running Airflow in Docker*. Apache Airflow Documentation. https://airflow.apache.org/docs/apache-airflow/stable/howto/docker-compose/index.html
- [11] Apache Software Foundation. (2024). *Tasks*. Apache Airflow Documentation. https://airflow.apache.org/docs/apache-airflow/stable/core-concepts/tasks.html
- [12] Apache Software Foundation. (2024). *Callbacks*. Apache Airflow Documentation. https://airflow.apache.org/docs/apache-airflow/stable/administration-and-deployment/logging-monitoring/callbacks.html
- [13] Apache Software Foundation. (2024). *UI Overview*. Apache Airflow Documentation. https://airflow.apache.org/docs/apache-airflow/stable/ui.html
- [14] Microsoft Corporation. (2023). *About the Task Scheduler*. Microsoft Learn. https://learn.microsoft.com/en-us/windows/win32/taskschd/about-the-task-scheduler
- [15] Microsoft Corporation. (2023). *schtasks commands*. Microsoft Learn. https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks
- [16] Microsoft Corporation. (2023). *TaskSettings.RestartInterval property*. Microsoft Learn. https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-restartinterval
- [17] Microsoft Corporation. (2023). *Troubleshoot scheduled tasks not running*. Microsoft Learn. https://learn.microsoft.com/en-us/troubleshoot/windows-server/system-management-components/troubleshoot-scheduled-tasks-not-running
