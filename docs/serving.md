# Stored forecasts and serving choices

I use `src/load_forecast/serving.py` to store completed forecasts and publish one final
choice per delivery day. The local choice file is the serving authority. PostgreSQL
keeps an insert-only audit and mirror. This package does not schedule jobs, compute
fallbacks or change the existing API; those integrations follow separately.

I apply `infra/postgres/forecast.sql` manually as the administrator of `load_forecast`.
It creates a new schema and two group roles, `lff_forecast_writer` and
`lff_forecast_reader`. I grant the appropriate group to the production login myself.
The writer receives USAGE, SELECT and INSERT; the reader receives USAGE and SELECT.
Neither owns tables or receives UPDATE, DELETE, TRUNCATE or CREATE. The runtime login
must have no administrator membership or inherited broader grants. The migration
fails if the schema already exists, so it cannot silently change an existing store.
It contains no login password and is not run by the Python module.

I pass a SQLAlchemy engine to `ForecastStore`. The store never creates tables.
`start_run` inserts an event before input selection. `complete_run` inserts the result,
weather identities, values, experts, states and the first `state_runs` row for the day
and variant in one transaction. `fail_run` inserts a failed result without outputs. A crashed run
has only its start. `replay_run` returns only a completed run. `state_runs` is keyed on
`(delivery_day, variant)`; a concurrent completed primary of the same variant loses with
`ON CONFLICT DO NOTHING`. A and B retain independent histories (D23). A replay never
claims that key. Serving a fallback does not change the primary used for online updates.

The run archive is a JSON-compatible dictionary with `kind`, `id`, `delivery_day`, `variant`,
`start`, `result`, `values`, `experts`, `weather` and `states`. Each nested row has the
columns of its table, excluding `run_id` in the four lists and `variant` in `values`.
Flags and state payloads are dictionaries. The archive and its start must name the same variant, A or B.
Stored forecast rows also carry that variant, constrained to their run by a foreign key;
`replay_run` checks it and returns the archive's original row shape.
Only B can publish a primary choice. A is stored as a shadow and never served.
The file reader also rejects a primary choice referencing A.
I keep timestamps as aware UTC ISO strings, dates as ISO dates,
values in MW and absent optional fields as explicit nulls. The state payload holds
the full weights, regrets, Kalman parameters and absorbed-day snapshot identities,
so reconstruction needs no new model fit. I obtain the publication archive through
`replay_run` after committing, which also gives a stable row order for its hash.
`complete_run` accepts a primary or replay; the fallback table holds separately
prepared fallback archives.

I reject incomplete primary and replay archives before storing any result or state
claim. Every forecast slot must have the same non-empty expert set, states must be
non-empty, and weather rows must name one pinned selection and run. The allowed
weather statuses are `primary`, `fallback` and `weather missing` (the selected-run
status vocabulary); only the last
allows an empty weather list. I require `max_ingested_at < F` and a last consumption
slot no later than `F - 2 h 30 min`. A null consumption timestamp requires the
explicit `flags.consumption_missing = True` declaration. Every weather row carries
`file_last_modified` and `index_last_modified` as UTC ISO strings, alongside its
identity; these publication times and `run_init_time` must all precede F. Rejected
archives raise and log their validation error. These checks validate declared audit
metadata; the input readers remain responsible for supplying truthful metadata.

```python
from pathlib import Path

from load_forecast.serving import ForecastStore, ServingAuthority

# engine and start metadata come from the production flow.
store = ForecastStore(engine)
store.start_run(start)
# The production flow now selects frozen inputs and computes the archive.
store.complete_run(archive)
committed = store.replay_run(archive["id"])
authority = ServingAuthority(Path(serving_directory))
choice = authority.publish_primary(committed)
```

I keep `serving_directory` outside the checkout and include it in backups. A fallback
archive has `kind="fallback"`, `id`, `delivery_day`, `prepared_at`, `models`,
`completeness={"complete": True}` and `values`. Preparation must apply the approved
plausibility bounds before setting that flag. The module checks finite values and
exact coverage of every UTC half-hour of the Paris day, including 46 and 50 slots.
It rejects fallback intervals. I call `store_fallback` then `prepare_fallback` ahead
of the deadline. Retries keep the first prepared fallback.

`prepare_values` writes a self-contained archive to `values/<content hash>.json`.
The writer flushes and fsyncs a unique temporary file before publishing it without
replacement. The same protocol publishes `choice/YYYY-MM-DD.json`: `os.rename` on
Windows, `os.link` then temporary unlink on POSIX. The primary checks the local clock
again immediately before publication. At 12:30 Paris D-1, `deadline_check` reads an
existing choice or publishes the prepared fallback; absent, corrupt or incomplete
fallbacks produce a final `none` choice and log the reason. Other filesystem read
errors receive at most three immediate attempts, with no new attempt after a
250 ms monotonic budget; a persistent error is logged and raised so the scheduler
can retry without a final choice. Individual filesystem calls cannot be interrupted
by this retry budget. A late primary obeys an existing choice and
cannot replace it. A missing choice after the deadline is never claimed by a primary.
The deadline check performs no SQL and no forecast computation.

Readers ignore temporary files. `read_choice` checks the choice hash and its date;
`read_served` also verifies the archive hash, identity, safe local path and complete
slot grid. A corrupt final choice raises an error and requires an operational alert;
it is never silently replaced. The scheduler must alert on a `none` choice too.
If a process dies after fsync and before publication, its temporary file is ignored
and the deadline check can win. `cleanup_temporary`, called after successful
reconciliation, removes only temporary files older than one hour. Final files are
never deleted or overwritten. The directory must remain private to the serving
processes; hashes detect corruption, not a malicious writer with directory access.

`reconcile` reads the file and imports any absent run or fallback records, inserts
the choice and attempts the day mirror. Exact repeated imports are idempotent;
conflicting immutable forecast records raise and roll back the import. If the mirror
already names another hash, it remains unchanged and an incident stores both hashes.
Repeated reconciliation records one incident per disagreement.
`served_for_scoring` requires the file's hash to exist in `forecast.choices` and
verifies the stored forecast against the file archive. It never selects through the
mirror. Online updates select `forecast.state_runs` separately.

I run `python -m pytest tests/test_serving.py -q` for local verification. The tests
execute the table DDL in SQLite using an attached `forecast` database and foreign
keys. SQLite authorization simulates the runtime grant restrictions; a static check
checks the PostgreSQL grants. It does not certify live PostgreSQL privileges. Separate
processes race the two file publishers, and a subprocess exits after writing its
temporary file. Concurrent SQLite transactions check the day-only state key.
Database outage tests make every engine connection fail after committing the primary;
the file serving path still works.

Before go-live I verify the migration and effective privileges on PostgreSQL with the
production logins, then repeat the atomic publication and failure checks on the actual
deployment filesystem. Windows file fsync is exercised locally. I repeat tests of
simultaneous publishers, a paused primary and a
writer that exits between fsync and publication. I check INSERT succeeds and UPDATE,
DELETE and TRUNCATE fail for the runtime login on a scratch PostgreSQL database.
This Python API does not provide directory fsync on Windows. Power loss and whole-PC failure remain limits
of this single-machine guarantee, as specified in the production design.
