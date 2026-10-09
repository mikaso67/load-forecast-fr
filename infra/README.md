# Local infrastructure

The developer manages the containers. From the repository root, copy `.env.example`
to `.env`, fill `POSTGRES_USER` and `POSTGRES_PASSWORD`, then run:

```powershell
docker compose --env-file .env -f infra/docker-compose.infra.yml config --quiet
docker compose --env-file .env -f infra/docker-compose.infra.yml up -d --build
docker compose --env-file .env -f infra/docker-compose.infra.yml ps
```

The build installs MLflow 3.16.1 and Psycopg 3.3.6 on `python:3.13.1-slim`.
PostgreSQL uses `postgres:16.4`. These are exact version tags, not digest pins.
The first startup requires internet access to build the image.

PostgreSQL is available at `127.0.0.1:5432` with the database and credentials in
`.env`. MLflow is available at <http://localhost:5000>. Both ports bind to the local
computer. Keep `POSTGRES_DB=load_forecast` for the data database. On an empty data
volume, `infra/postgres/init/001-create-mlflow.sql` creates a separate `mlflow`
database owned by `POSTGRES_USER`, with the same role and password as `load_forecast`.
MLflow waits for PostgreSQL readiness and creates its metadata tables in `mlflow`.
The healthcheck uses TCP at `127.0.0.1`. PostgreSQL's temporary initialization
server listens on a Unix socket, so it cannot pass this check before the init
scripts finish. `.gitattributes` enforces LF for shell scripts, SQL files and all
files under `infra/`, keeping initialization files usable in Linux containers.
Database sessions use UTC. MLflow has no authentication; anyone able to reach the
server can access it. The localhost binding is intended for local development.

MLflow metadata lives in the `postgres_data` named volume. Uploaded artifacts live
in `mlflow_artifacts` and are served through MLflow, so clients do not need a shared
filesystem. Artifacts are outside PostgreSQL backups: back up both the database
and artifact volumes to preserve runs and their model files.
Set `MLFLOW_TRACKING_URI=http://127.0.0.1:5000` in the client environment
before logging runs.

Every Compose command explicitly loads the root `.env` with `--env-file .env`. Credentials
are passed through PostgreSQL's `PG*` environment variables; the backend URI has no
credentials to escape. An empty username or password makes Compose fail immediately.
The PostgreSQL image initializes the role and database only when its data volume is
empty. Editing `.env` later does not change an existing database's credentials
or rerun initialization scripts. An existing volume from the previous setup will
not automatically gain the `mlflow` database.

For this setup rework, the developer recreates the volumes before startup.
`down -v` deletes both database contents and artifacts; preserve anything needed first.
Only the developer runs these commands:

```powershell
docker compose --env-file .env -f infra/docker-compose.infra.yml down -v
docker compose --env-file .env -f infra/docker-compose.infra.yml up -d --build
```

Developer verification:
Run these commands from the repository root. Enter the `POSTGRES_USER` value
from that root `.env` when prompted. Compose loads `.env` for its configuration;
it does not set variables in the PowerShell process.

```powershell
docker compose --env-file .env -f infra/docker-compose.infra.yml ps
$postgresUser = Read-Host 'Enter POSTGRES_USER from the repository-root .env'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -U $postgresUser -d load_forecast -c 'SHOW timezone'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -U $postgresUser -d load_forecast -c 'SELECT datname FROM pg_database ORDER BY datname'
Invoke-WebRequest http://localhost:5000/health
```

Confirm both containers are healthy, the database queries succeed, the timezone
is UTC, both `load_forecast` and `mlflow` are listed, and `/health` returns HTTP 200.
The direct `psql` forms pass each SQL statement as one argument in Windows
PowerShell, without a nested shell or embedded quote characters.
Then open <http://localhost:5000> and verify that a client can log a run and upload
an artifact. Container startup, SQL access, and artifact upload still need the
developer's runtime verification; parsing YAML alone does not verify them.

## Development database access (developer only)

Run `infra/postgres/roles.sql` once against the existing stack, from the repository
root with the administrator's root `.env`. It is deliberately outside `postgres/init`.
The script creates `lff_agent` and takes its password from a psql variable populated
by `LFF_AGENT_PASSWORD`. The password never appears in the file or command arguments.
It fails atomically if the role already exists, the password is missing or empty,
or the database is not `load_forecast`. Review existing memberships and ownership
before changing an existing role; do not blindly rerun this provisioning script.

I grant this role read access to `raw`, `clean` and `analytics`, and ownership of `dev`,
including existing dbt tables, views and sequences in that schema. Ownership permits dbt
to replace its models. I select the development schema with `LFF_DEV_SCHEMA`; the `dev`
target defaults to schema `dev`. Existing development schemas keep their tables and grants.
Future `raw` and `clean` tables receive SELECT through the administrator's default
privileges. Run this as the role that creates source tables. Other table creators
need equivalent `ALTER DEFAULT PRIVILEGES FOR ROLE ...` grants before creating tables.

PostgreSQL grants CONNECT and TEMPORARY to PUBLIC by default. The script removes
PUBLIC access to `mlflow`, PUBLIC CREATE/TEMPORARY on `load_forecast`, and PUBLIC
CREATE on `public`. MLflow keeps access through its database owner (the existing
administrator). Other non-owner MLflow clients would need explicit grants.
These changes apply when the developer executes the script, not at container startup.

The following PowerShell block prompts privately for the new password, forwards it
as an environment variable, and clears it afterwards. Do not enable command tracing
or psql query echoing when provisioning credentials. The SQL disables server statement
logging for its transaction. Compose loads the administrator's root `.env`;
`$postgresUser` is entered separately as in the earlier verification commands.

```powershell
$postgresUser = Read-Host 'Enter POSTGRES_USER from the administrator repository-root .env'
$devSecret = Read-Host 'Choose the lff_agent password' -AsSecureString
$devCredential = New-Object System.Management.Automation.PSCredential -ArgumentList 'lff_agent', $devSecret
try {
    $env:LFF_AGENT_PASSWORD = $devCredential.GetNetworkCredential().Password
    Get-Content -Raw -LiteralPath infra/postgres/roles.sql | docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T -e LFF_AGENT_PASSWORD postgres psql -X -U $postgresUser -d load_forecast -v ON_ERROR_STOP=1 -f -
    if ($LASTEXITCODE -ne 0) { throw 'Development role provisioning failed' }
} finally {
    Remove-Item Env:LFF_AGENT_PASSWORD -ErrorAction SilentlyContinue
    $devCredential = $null
    $devSecret = $null
}
```

I give each development folder its own ignored root `.env` containing the following keys.
The developer fills `POSTGRES_PASSWORD` with the password chosen above, without
committing it. Keep the administrator `.env` in the developer's folder for stack
management. Development credentials do not replace the container administrator credentials.

```dotenv
POSTGRES_HOST=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_DB=load_forecast
POSTGRES_USER=lff_agent
POSTGRES_PASSWORD=
MLFLOW_TRACKING_URI=http://127.0.0.1:5000
DBT_TARGET=dev
LFF_DEV_SCHEMA=dev
LFF_AUTHOR=developer
```

Use single quotes around passwords containing dollar signs or hash signs in `.env`.
Clients must load their folder's `.env` into their process environment; Compose
`--env-file` does not do that for Python or dbt. dbt must use the matching target
schema from `LFF_DEV_SCHEMA`. Collectors write raw tables and need
separate developer-managed writer credentials, since this login is read-only there.
MLflow clients use the HTTP tracking server rather than direct database credentials.

Developer runtime verification, from the administrator repository root:
`-h 127.0.0.1 -W` tests password authentication over TCP rather than socket trust.
Enter the development password at each psql prompt. The first command must succeed;
the next two must fail with permission denied. The INSERT requests no rows, and
the DROP is enclosed in a transaction that rolls back even if permissions are wrong.
The MLflow connection must fail. The final query must show false, true and false
respectively for raw CREATE, development CREATE and MLflow CONNECT.

```powershell
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -X -h 127.0.0.1 -U lff_agent -W -d load_forecast -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM raw.eco2mix'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -X -h 127.0.0.1 -U lff_agent -W -d load_forecast -v ON_ERROR_STOP=1 -c 'INSERT INTO raw.eco2mix SELECT * FROM raw.eco2mix WHERE false'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -X -h 127.0.0.1 -U lff_agent -W -d load_forecast -v ON_ERROR_STOP=1 -c 'BEGIN; DROP TABLE raw.eco2mix; ROLLBACK'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -X -h 127.0.0.1 -U lff_agent -W -d mlflow -v ON_ERROR_STOP=1 -c 'SELECT 1'
docker compose --env-file .env -f infra/docker-compose.infra.yml exec postgres psql -X -h 127.0.0.1 -U lff_agent -W -d load_forecast -v ON_ERROR_STOP=1
```

At the final interactive psql prompt, run this SQL (quoted SQL stays inside psql):

```sql
SELECT has_schema_privilege(current_user, 'raw', 'CREATE') AS raw_create,
       has_schema_privilege(current_user, 'dev', 'CREATE') AS dev_create,
       has_database_privilege(current_user, 'mlflow', 'CONNECT') AS mlflow_connect;
```

Only the developer runs these probes. Keep the entire DROP transaction in one `-c`
argument: a permission error closes the connection and rolls back, while unexpected
success reaches the explicit ROLLBACK. Do not execute the DROP outside that transaction.
I load my ignored development `.env` before building the dbt project:

```powershell
& ../load-forecast-fr-venv/Scripts/dbt.exe build --project-dir dbt --profiles-dir dbt --target dev
```

The project configures `dev` and `prod` in `dbt/profiles.yml`. I record the SELECT,
denied writes, denied MLflow connection and dbt outputs in my verification notes.

References: [MLflow tracking server](https://mlflow.org/docs/latest/self-hosting/architecture/tracking-server/),
[PostgreSQL environment variables](https://www.postgresql.org/docs/16/libpq-envars.html),
[Compose interpolation](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/).

## Load a development environment

From my repository root, dot-source the loader before running
Python or dbt. It reads the current folder's ignored `.env`, changes only the
current process environment and emits no values. Child processes inherit it.
I keep my own `POSTGRES_USER=lff_agent` credentials and dbt target.

```powershell
. .\scripts\load_env.ps1
$env:PYTHONPATH = 'src'
& ../load-forecast-fr-venv/Scripts/dbt.exe debug --project-dir dbt --profiles-dir dbt --target dev
& ../load-forecast-fr-venv/Scripts/dbt.exe build --project-dir dbt --profiles-dir dbt --target dev
& ../load-forecast-fr-venv/Scripts/python.exe -m pytest tests/test_cutoff.py -q
```

I set `LFF_DEV_SCHEMA` to my existing development schema before using target `dev`.
Target `prod` always builds into `analytics`; I reserve it for the production build.
I use the shared interpreter and `-m package.module` for Python modules; `PYTHONPATH=src`
makes project modules importable. Collectors need separate writer credentials because
the development login cannot write raw tables. I keep those credentials separate.

Values enclosed in matching single or double quotes are literal: outer quotes
are removed, internal whitespace, dollar signs, hash signs, equals signs and
backslashes are preserved. The loader never evaluates expressions, substitutes
variables or decodes escape sequences. Multiline values are not supported.
Unquoted surrounding whitespace is trimmed; a hash preceded by whitespace
begins an inline comment. Blank lines, full-line comments and an optional
`export` prefix are accepted. Duplicate names use the last assignment; empty
values clear the variable on Windows runtimes that cannot retain empty values.
An unreadable/missing file or malformed assignment fails clearly without showing
its contents. The whole file is parsed before assignments are applied. Existing
process values with matching names are replaced; unrelated variables stay intact.

Repeatable fake-file checks, without reading any real `.env`:

```powershell
& .\tests\check_load_env.ps1
powershell.exe -NoProfile -File tests/check_load_env.ps1
```

Developer acceptance check, with my ignored development `.env`:

```powershell
. .\scripts\load_env.ps1
& ../load-forecast-fr-venv/Scripts/dbt.exe debug --project-dir dbt --profiles-dir dbt --target dev
```

Confirm that dbt reports `user: lff_agent` and a successful connection. This
developer check is outstanding; the fake-file checks do not prove authentication.

## Database backups and scratch restore (developer only)

Run from the developer's main repository with its administrator `.env` and the
existing healthy stack. The script does not start, stop or reconfigure containers.
Load the administrator username into the process, then back up both databases:

```powershell
. .\scripts\load_env.ps1
& .\scripts\backup_db.ps1
```

The default root is `$HOME\backups\load-forecast-fr`, outside the repository.
Each invocation creates a UTC timestamp plus unique suffix folder containing
`load_forecast.dump`, `mlflow.dump` and an empty `backup.complete` marker. Use
`-BackupRoot 'D:\backups\load-forecast-fr'` to choose another location outside the
repository, or `-PostgresUser $postgresUser` to supply the administrator username
without loading its `.env` into the process. Compose still uses the main `.env`.
The read-only development role is refused. No password is put in command arguments.

Both archives use `docker compose --env-file .env -f infra/docker-compose.infra.yml
exec -T postgres pg_dump -U <administrator> -Fc -d <database>`. The script copies
the native stdout **byte stream** directly to a file; ordinary PowerShell 5.1
redirection or `Get-Content` pipelines would corrupt this binary format. Native
stderr is drained concurrently and suppressed; failures show only a generic
database name and leave `.partial` files for inspection. A zero exit code and
`PGDMP` header are required before the pair is marked complete. This header check
does not prove restorability: perform the restore test below.

Only after both dumps succeed, retention keeps the newest seven completed sets.
It deletes only the two named archives and marker from recognized older folders;
it refuses junctions, symbolic links and unexpected contents. Unrelated folders
and failed/incomplete sets are retained and do not count toward the seven.
Successful output contains only archive file names and sizes in bytes. Database
dumps exclude the MLflow artifact volume; its files need a separate backup.
Each database dump has its own consistent snapshot, not a shared snapshot across
both databases.

### Restore acceptance check without touching production tables

Keep collector writes idle during this verification so the live source count
still matches the backup snapshot. Record the source row count, run the backup,
and pick the latest completed set. Run this in the main folder with the
administrator environment loaded as above:

```powershell
$ErrorActionPreference = 'Stop'
$sourceCount = docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres psql -X -At -U $env:POSTGRES_USER -d load_forecast -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM raw.eco2mix'
if ($LASTEXITCODE -ne 0) { throw 'Source count failed' }
& .\scripts\backup_db.ps1
$backupRoot = Join-Path $HOME 'backups/load-forecast-fr'
$backupFolder = Get-ChildItem -LiteralPath $backupRoot -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'backup.complete') } | Sort-Object Name -Descending | Select-Object -First 1
$restoreId = [guid]::NewGuid().ToString('N')
$scratchLoad = 'lff_restore_load_' + $restoreId
$scratchMlflow = 'lff_restore_mlflow_' + $restoreId
docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres createdb -U $env:POSTGRES_USER $scratchLoad
if ($LASTEXITCODE -ne 0) { throw 'Scratch load database creation failed' }
docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres createdb -U $env:POSTGRES_USER $scratchMlflow
if ($LASTEXITCODE -ne 0) { throw 'Scratch MLflow database creation failed' }
docker compose --env-file .env -f infra/docker-compose.infra.yml cp (Join-Path $backupFolder.FullName 'load_forecast.dump') "postgres:/tmp/${restoreId}_load.dump"
if ($LASTEXITCODE -ne 0) { throw 'Load archive copy failed' }
docker compose --env-file .env -f infra/docker-compose.infra.yml cp (Join-Path $backupFolder.FullName 'mlflow.dump') "postgres:/tmp/${restoreId}_mlflow.dump"
if ($LASTEXITCODE -ne 0) { throw 'MLflow archive copy failed' }
docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres pg_restore -U $env:POSTGRES_USER --exit-on-error -d $scratchLoad "/tmp/${restoreId}_load.dump"
if ($LASTEXITCODE -ne 0) { throw 'Scratch load restore failed' }
docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres pg_restore -U $env:POSTGRES_USER --exit-on-error -d $scratchMlflow "/tmp/${restoreId}_mlflow.dump"
if ($LASTEXITCODE -ne 0) { throw 'Scratch MLflow restore failed' }
$restoredCount = docker compose --env-file .env -f infra/docker-compose.infra.yml exec -T postgres psql -X -At -U $env:POSTGRES_USER -d $scratchLoad -v ON_ERROR_STOP=1 -c 'SELECT count(*) FROM raw.eco2mix'
if ($LASTEXITCODE -ne 0) { throw 'Restored count failed' }
if ([string]$sourceCount -cne [string]$restoredCount) { throw 'Source and restored row counts differ' }
"raw.eco2mix source=$sourceCount restored=$restoredCount"
```

If using a custom backup root, set `$backupRoot` to that same path. The generated
scratch names are distinct from `load_forecast` and `mlflow`; no `--clean`, DROP
or restoration into the live databases is used. Scratch databases and copied
archives remain for the developer to inspect. Count equality verifies row count,
not every value; successful restoration of both archives is also required.
Record the command outputs and row counts for acceptance. The live backup and
scratch restore are developer checks and were not executed by me.

Repeatable tests use a compiled fake Docker executable and temporary fake files,
with no connection to containers or real `.env`:

```powershell
& .\tests\check_backup_db.ps1
powershell.exe -NoProfile -File tests/check_backup_db.ps1
```
