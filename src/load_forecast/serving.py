"""Immutable local serving choices, with an insert-only database mirror.

The caller stores a completed run before publishing its archive. Publication and
deadline enforcement need no database. All consumers resolve the local file first.
"""

import hashlib
import json
import logging
import math
import os
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, text

from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff

Document = dict[str, Any]
LOGGER = logging.getLogger(__name__)
DEADLINE_WINDOW = timedelta(minutes=1)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    if isinstance(value, dict) and "experts" in value and "states" in value:
        # Row order and SQL floating-point coercion do not change a forecast.
        value = json.loads(_json(value))
        for row in value["values"]:
            for key in ("point", "lower", "upper"):
                if row.get(key) is not None:
                    row[key] = float(row[key])
        for row in value["experts"]:
            row["value"] = float(row["value"])
        for key, fields in (("values", ("value_time",)),
                            ("experts", ("value_time", "expert")),
                            ("weather", ("end_step", "variable", "point")),
                            ("states", ("component", "state_key"))):
            value[key].sort(key=lambda row: tuple(row[field] for field in fields))
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc(value: str | datetime) -> datetime:
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected an aware timestamp")
    return value.astimezone(UTC)


def _stored_time(value: str) -> datetime:
    instant = _utc(value)
    if value != instant.isoformat():
        raise ValueError("Stored timestamps must use canonical UTC ISO strings")
    return instant


def _now() -> datetime:
    return datetime.now(UTC)


def delivery_deadline(day: date) -> datetime:
    """Return 12:30 Paris D-1 in UTC, including clock changes."""
    return forecast_cutoff(day) + timedelta(minutes=30)


def validate_values(day: date, values: list[Document]) -> None:
    """Require every UTC half-hour of the local day exactly once and finite MW."""
    slots = [_stored_time(row["value_time"]) for row in values]
    expected = list(delivery_slots(day, "30min"))
    if sorted(slots) != expected:
        raise ValueError("Values must cover exactly the delivery day's UTC slots")
    for row, slot in zip(values, slots, strict=True):
        local = slot.astimezone(PARIS)
        if row["local_slot"] != local.hour * 2 + local.minute // 30:
            raise ValueError("Incorrect local_slot")
        if not math.isfinite(row["point"]):
            raise ValueError("Nonfinite point forecast")
        low, high = row.get("lower"), row.get("upper")
        if (low is None) != (high is None):
            raise ValueError("Both interval endpoints are required")
        if low is not None and (not math.isfinite(low) or not math.isfinite(high) or low > high):
            raise ValueError("Invalid interval")
    _json(values)


def _validate_outputs(archive: Document, freeze: datetime) -> None:
    result = archive["result"]
    slots = {row["value_time"] for row in archive["values"]}
    experts: dict[str, set[str]] = {slot: set() for slot in slots}
    for row in archive["experts"]:
        _stored_time(row["value_time"])
        if row["value_time"] not in experts or not row["expert"] or not math.isfinite(row["value"]):
            raise ValueError("Invalid expert output or slot")
        experts[row["value_time"]].add(row["expert"])
    if not experts or any(not group for group in experts.values()) or len({
        frozenset(group) for group in experts.values()
    }) != 1:
        raise ValueError("Every slot requires the same non-empty expert set")
    if not archive["states"] or any(
        not row["component"] or not row["state_key"] or not row["payload"]
        for row in archive["states"]
    ):
        raise ValueError("Complete runs require non-empty states")
    status = result["weather_status"]
    if status not in ("primary", "fallback", "weather missing"):
        raise ValueError("Unknown weather status")
    if bool(archive["weather"]) != (status != "weather missing"):
        raise ValueError("Weather rows disagree with weather status")
    identities = set()
    for row in archive["weather"]:
        identities.add((row["selection_hash"], row["run_init_time"]))
        if not row["selection_hash"] or not row["message_hash"]:
            raise ValueError("Weather identities must be non-empty")
        for field in ("run_init_time", "file_last_modified", "index_last_modified"):
            if _stored_time(row[field]) >= freeze:
                raise ValueError(f"Weather {field} must precede freeze")
    if len(identities) > 1:
        raise ValueError("Weather rows must name one pinned selection and run")
    for field in ("last_consumption_slot", "max_ingested_at"):
        value = result.get(field)
        if value is None:
            if result.get("flags", {}).get("consumption_missing") is not True:
                raise ValueError(f"Missing {field} requires consumption_missing flag")
        elif field == "max_ingested_at":
            if _stored_time(value) >= freeze:
                raise ValueError("max_ingested_at must precede freeze")
        elif _stored_time(value) > freeze - timedelta(hours=2, minutes=30):
            raise ValueError("last_consumption_slot exceeds D2 boundary")


def _validate_archive(archive: Document) -> None:
    try:
        _check_archive(archive)
    except (ValueError, KeyError, TypeError) as error:
        LOGGER.error("Forecast archive rejected: %s", error)
        raise


def _check_archive(archive: Document) -> None:
    day = date.fromisoformat(archive["delivery_day"])
    if archive["kind"] in ("primary", "replay"):
        start, result = archive["start"], archive["result"]
        if archive["variant"] not in ("A", "B") or start["variant"] != archive["variant"]:
            raise ValueError("Archive and start require the same A/B variant")
        if (start["delivery_day"] != day.isoformat() or start["kind"] != archive["kind"]
                or start["run_id"] != archive["id"] or result["run_id"] != archive["id"]
                or result["status"] != "complete"
                or _stored_time(start["freeze_time"]) != forecast_cutoff(day)):
            raise ValueError("Archive must name a completed primary at the delivery freeze")
        _stored_time(start["started_at"])
        completed = _stored_time(result["completed_at"])
        if completed < _stored_time(start["started_at"]):
            raise ValueError("Result predates run start")
        _validate_outputs(archive, forecast_cutoff(day))
        for key in ("values", "experts", "weather", "states"):
            if any("run_id" in row for row in archive[key]):
                raise ValueError("Nested output rows must not override run_id")
        if any("variant" in row for row in archive["values"]):
            raise ValueError("Forecast rows inherit variant from their archive")
    elif archive["kind"] == "fallback":
        _stored_time(archive["prepared_at"])
        if archive["completeness"].get("complete") is not True:
            raise ValueError("Fallback completeness check failed")
        if any(row.get("lower") is not None or row.get("upper") is not None
               for row in archive["values"]):
            raise ValueError("Prepared fallbacks have no calibrated intervals")
    else:
        raise ValueError("Invalid archive kind")
    validate_values(day, archive["values"])
    _json(archive)


def _sync_directory(folder: Path) -> None:
    # Windows does not expose a directory fsync through this API.
    if os.name != "nt":
        descriptor = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _no_replace(temporary: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(temporary, target)
    else:
        os.link(temporary, target)
        temporary.unlink()
    _sync_directory(target.parent)


def _publish(target: Path, document: Document, *, before_publish: Callable[[], None]) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.stem}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_json(document))
        stream.flush()
        os.fsync(stream.fileno())
    try:
        before_publish()
        _no_replace(temporary, target)
        return True
    except FileExistsError:
        return False
    finally:
        temporary.unlink(missing_ok=True)


class ServingAuthority:
    """A directory outside the checkout is the authority for served forecasts."""

    def __init__(self, root: Path, clock: Callable[[], datetime] = _now):
        self.root = Path(root).resolve()
        self.clock = clock

    def _time(self) -> datetime:
        return _utc(self.clock())

    def _choice_path(self, day: date) -> Path:
        return self.root / "choice" / f"{day.isoformat()}.json"

    def prepare_values(self, archive: Document) -> Document:
        """Persist a self-contained archive, flushed before any choice can name it."""
        _validate_archive(archive)
        digest = _hash(archive)
        relative = f"values/{digest}.json"
        target = self.root / relative
        _publish(target, archive, before_publish=lambda: None)
        if _hash(json.loads(target.read_text(encoding="utf-8"))) != digest:
            raise ValueError("Values content hash mismatch")
        return {"kind": archive["kind"], "id": archive["id"],
                "delivery_day": archive["delivery_day"], "content_hash": digest,
                "values_path": relative}

    def _archive(self, choice: Document) -> Document | None:
        if choice["kind"] == "none":
            if any(choice.get(key) is not None for key in ("id", "content_hash", "values_path")):
                raise ValueError("A none choice must not reference values")
            return None
        digest = choice["content_hash"]
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
                or choice["values_path"] != f"values/{digest}.json"):
            raise ValueError("Invalid values path or content hash")
        path = self.root / choice["values_path"]
        if path.resolve().parent != (self.root / "values").resolve():
            raise ValueError("Values path escapes serving directory")
        archive = json.loads(path.read_text(encoding="utf-8"))
        if _hash(archive) != digest:
            raise ValueError("Values content hash mismatch")
        _validate_archive(archive)
        if any(choice[key] != archive[key] for key in ("kind", "id", "delivery_day")):
            raise ValueError("Choice and values identities disagree")
        if choice["kind"] == "primary" and archive["variant"] != "B":
            raise ValueError("A serving choice must reference variant B")
        return archive

    def read_choice(self, day: date) -> Document:
        """Read only the final day file; corrupt choices raise instead of falling back."""
        choice = json.loads(self._choice_path(day).read_text(encoding="utf-8"))
        expected = {"kind", "id", "delivery_day", "content_hash", "values_path",
                    "chosen_at", "choice_hash"}
        if isinstance(choice, dict) and choice.get("kind") == "none" and "reason" in choice:
            expected.add("reason")
        if not isinstance(choice, dict) or set(choice) != expected:
            raise ValueError("Invalid choice fields")
        digest = choice.pop("choice_hash")
        if digest != _hash(choice) or choice["delivery_day"] != day.isoformat():
            raise ValueError("Choice hash or delivery day mismatch")
        chosen = _stored_time(choice["chosen_at"])
        if choice["kind"] not in ("primary", "fallback", "none"):
            raise ValueError("Invalid serving choice kind")
        if choice["kind"] == "primary" and chosen >= delivery_deadline(day):
            raise ValueError("Primary choice after deadline")
        if choice["kind"] != "primary" and chosen < delivery_deadline(day):
            raise ValueError("Deadline choice before deadline")
        if choice["kind"] == "fallback" and chosen >= delivery_deadline(day) + DEADLINE_WINDOW:
            raise ValueError("Fallback choice after the 12:30 window")
        self._archive(choice)
        return {**choice, "choice_hash": digest}

    def read_served(self, day: date) -> Document | None:
        """Resolve the same archive with the database up or down."""
        return self._archive(self.read_choice(day))

    def _claim(self, day: date, reference: Document | None, *, primary: bool,
               reason: str | None = None) -> Document:
        if self._choice_path(day).exists():
            return self.read_choice(day)
        choice = reference or {"kind": "none", "id": None, "content_hash": None,
                               "values_path": None, "delivery_day": day.isoformat()}
        if reference is None and reason is not None:
            choice = {**choice, "reason": reason}
        self._archive(choice)
        choice = {**choice, "chosen_at": self._time().isoformat()}
        choice["choice_hash"] = _hash(choice)

        def guard() -> None:
            if primary and self._time() >= delivery_deadline(day):
                raise TimeoutError("Primary publication missed the deadline")
            if choice["kind"] == "fallback" and self._time() >= delivery_deadline(day) + DEADLINE_WINDOW:
                raise TimeoutError("Fallback publication missed the 12:30 window")

        _publish(self._choice_path(day), choice, before_publish=guard)
        return self.read_choice(day)

    def publish_primary(self, archive: Document) -> Document:
        """Claim a completed, committed primary before the deadline, without SQL."""
        if archive.get("variant") != "B":
            raise ValueError("Only variant B may publish a serving choice")
        if archive["kind"] != "primary":
            raise ValueError("Expected primary archive")
        day = date.fromisoformat(archive["delivery_day"])
        if self._choice_path(day).exists():
            return self.read_choice(day)
        if self._time() >= delivery_deadline(day):
            raise TimeoutError("Primary publication missed the deadline")
        if self._time() < forecast_cutoff(day):
            raise ValueError("Primary publication refused before freeze")
        if _stored_time(archive["result"]["completed_at"]) > self._time():
            raise ValueError("Primary result completion is in the future")
        return self._claim(day, self.prepare_values(archive), primary=True)

    def prepare_fallback(self, archive: Document) -> Document:
        """Publish the first complete prepared fallback; future retries obey it."""
        if archive["kind"] != "fallback":
            raise ValueError("Expected fallback archive")
        day = date.fromisoformat(archive["delivery_day"])
        reference = self.prepare_values(archive)
        path = self.root / "fallback" / f"{day.isoformat()}.json"
        _publish(path, reference, before_publish=lambda: None)
        stored = json.loads(path.read_text(encoding="utf-8"))
        if stored["delivery_day"] != day.isoformat() or stored["kind"] != "fallback":
            raise ValueError("Invalid prepared fallback reference")
        self._archive(stored)
        return stored

    def deadline_check(self, day: date) -> Document:
        """At 12:30 obey the choice, else claim the prepared fallback or none."""
        if self._time() < delivery_deadline(day):
            raise ValueError("Deadline check refused before 12:30 Paris D-1")
        if self._choice_path(day).exists():
            return self.read_choice(day)
        if self._time() >= delivery_deadline(day) + DEADLINE_WINDOW:
            reason = "Deadline check missed the 12:30 Paris window; delivery guarantee unmet"
            LOGGER.error("%s for %s", reason, day)
            return self._claim(day, None, primary=False, reason=reason)
        path = self.root / "fallback" / f"{day.isoformat()}.json"
        budget_end = monotonic() + 0.25
        for attempt in range(3):
            try:
                reference = json.loads(path.read_text(encoding="utf-8"))
                if reference["kind"] != "fallback" or reference["delivery_day"] != day.isoformat():
                    raise ValueError("Invalid prepared fallback reference")
                self._archive(reference)
            except (FileNotFoundError, ValueError, KeyError, TypeError) as error:
                LOGGER.error("Prepared fallback unavailable for %s: %s", day, error)
                reference = None
                break
            except OSError:
                if attempt == 2 or monotonic() >= budget_end:
                    LOGGER.exception("Prepared fallback read failed for %s; scheduler must retry", day)
                    raise
                LOGGER.warning("Retrying prepared fallback read for %s", day)
                continue
            break
        try:
            return self._claim(day, reference, primary=False)
        except TimeoutError as error:
            LOGGER.error("%s for %s", error, day)
            return self._claim(day, None, primary=False, reason=str(error))

    def cleanup_temporary(self) -> int:
        """Remove only abandoned temporary files older than one hour."""
        threshold = self._time().timestamp() - 3600
        count = 0
        for folder in ("choice", "values", "fallback"):
            for path in (self.root / folder).glob("*.tmp"):
                try:
                    if path.stat().st_mtime < threshold:
                        path.unlink()
                        count += 1
                except FileNotFoundError:
                    pass
        return count


class ForecastStore:
    """Persist events transactionally; this class never applies the migration."""

    def __init__(self, engine: Engine):
        self.engine = engine

    @staticmethod
    def _insert(connection, table: str, row: Mapping[str, Any], *, conflict: bool = False) -> None:
        allowed = {
            "run_starts": "run_id delivery_day variant freeze_time kind started_at code_commit",
            "run_results": "run_id completed_at status refit_day mlflow_run_id kalman_quarter "
                           "weather_status last_consumption_slot max_ingested_at feature_hash flags",
            "values": "run_id variant value_time local_slot point lower upper n_pool",
            "experts": "run_id value_time expert value backup",
            "run_weather": "run_id selection_hash run_init_time end_step variable point message_hash "
                           "file_last_modified index_last_modified",
            "states": "run_id component state_key payload",
            "state_runs": "delivery_day variant run_id",
            "fallbacks": "fallback_id delivery_day prepared_at models completeness content_hash payload",
            "choices": "choice_hash delivery_day kind run_id fallback_id content_hash values_path chosen_at",
            "deliveries": "delivery_day choice_hash",
            "delivery_incidents": "delivery_day mirror_hash file_hash detected_at",
            "scores": "delivery_day choice_hash actual_vintage actual_max_ingested_at scored_at "
                      "metrics",
        }
        if table not in allowed or not set(row) <= set(allowed[table].split()):
            raise ValueError("Unknown forecast table or column")
        columns = ", ".join(f'"{column}"' for column in row)
        parameters = ", ".join(f":{column}" for column in row)
        suffix = " ON CONFLICT DO NOTHING" if conflict else ""
        inserted = connection.execute(text(
            f'INSERT INTO forecast."{table}" ({columns}) VALUES ({parameters}){suffix}'
        ), dict(row))
        if conflict and inserted.rowcount == 0 and table not in ("state_runs", "deliveries"):
            keys = {
                "run_starts": ("run_id",), "run_results": ("run_id",),
                "values": ("run_id", "value_time"),
                "experts": ("run_id", "value_time", "expert"),
                "run_weather": ("run_id", "end_step", "variable", "point"),
                "states": ("run_id", "component", "state_key"),
                "fallbacks": ("fallback_id",), "choices": ("choice_hash",),
                "delivery_incidents": ("delivery_day", "mirror_hash", "file_hash"),
            }[table]
            if table == "delivery_incidents":
                return  # The first detection time stays immutable on subsequent passes.
            predicate = " AND ".join(f'"{key}" = :{key}' for key in keys)
            stored = connection.execute(text(
                f'SELECT * FROM forecast."{table}" WHERE {predicate}'
            ), {key: row[key] for key in keys}).mappings().one()
            if ForecastStore._normalize(stored) != dict(row):
                raise ValueError(f"Conflicting immutable {table} row")

    def start_run(self, start: Document) -> None:
        """Record the start before any inputs are read; a crash leaves no result."""
        day = date.fromisoformat(start["delivery_day"])
        if _stored_time(start["freeze_time"]) != forecast_cutoff(day):
            raise ValueError("Run freeze differs from delivery cutoff")
        _stored_time(start["started_at"])
        if start["variant"] not in ("A", "B"):
            raise ValueError("Expected variant A or B")
        row = {key: start[key] for key in (
            "run_id", "delivery_day", "variant", "freeze_time", "kind", "started_at", "code_commit"
        )}
        with self.engine.begin() as connection:
            self._insert(connection, "run_starts", row)

    def _complete(self, connection, archive: Document, *, conflict: bool = False) -> None:
        if connection.dialect.name == 'postgresql':
            connection.execute(text("SELECT pg_advisory_xact_lock_shared(hashtext('load_forecast:retention'))"))
        result = archive["result"]
        row = {key: result.get(key) for key in (
            "run_id", "completed_at", "status", "refit_day", "mlflow_run_id", "kalman_quarter",
            "weather_status", "last_consumption_slot", "max_ingested_at", "feature_hash"
        )}
        row["flags"] = _json(result.get("flags", {}))
        self._insert(connection, "run_results", row, conflict=conflict)
        for table, rows in (("values", archive["values"]), ("experts", archive["experts"]),
                            ("run_weather", archive["weather"]), ("states", archive["states"])):
            for source in rows:
                record = {**source, "run_id": archive["id"]}
                if table == "values":
                    record["variant"] = archive["variant"]
                if table == "states":
                    record["payload"] = _json(record["payload"])
                self._insert(connection, table, record, conflict=conflict)
        if archive["start"]["kind"] == "primary" and result["status"] == "complete":
            self._insert(connection, "state_runs", {
                "delivery_day": archive["delivery_day"], "variant": archive["variant"],
                "run_id": archive["id"]
            }, conflict=True)

    def complete_run(self, archive: Document) -> None:
        """Commit result, outputs and the first state run in one transaction."""
        _validate_archive(archive)
        _utc(archive["result"]["completed_at"])
        with self.engine.begin() as connection:
            start = connection.execute(text(
                "SELECT * FROM forecast.run_starts WHERE run_id = :id"
            ), {"id": archive["id"]}).mappings().one()
            if self._normalize(start) != archive["start"]:
                raise ValueError("Stored start differs from archive")
            self._complete(connection, archive)

    def fail_run(self, run_id: str, completed_at: datetime, flags: Document) -> None:
        """Record a failure event without outputs or a state run."""
        with self.engine.begin() as connection:
            self._insert(connection, "run_results", {
                "run_id": run_id, "completed_at": _utc(completed_at).isoformat(),
                "status": "failed", "flags": _json(flags),
            })

    @staticmethod
    def _normalize(row: Mapping[str, Any]) -> Document:
        return {key: (_utc(value).isoformat() if isinstance(value, datetime)
                      else value.isoformat() if isinstance(value, date) else value)
                for key, value in row.items()}

    def replay_run(self, run_id: str) -> Document:
        """Reconstruct the inputs, predictions and online states of a completed run."""
        with self.engine.connect() as connection:
            def rows(table: str, order: str = "") -> list[Document]:
                return [self._normalize(row) for row in connection.execute(text(
                    f'SELECT * FROM forecast."{table}" WHERE run_id = :id {order}'
                ), {"id": run_id}).mappings()]

            start = rows("run_starts")[0]
            results = rows("run_results")
            if not results or results[0]["status"] != "complete":
                raise ValueError("Run has no complete result")
            result = results[0]
            result["flags"] = json.loads(result["flags"])
            archive = {"kind": start["kind"], "id": run_id,
                       "variant": start["variant"],
                       "delivery_day": start["delivery_day"], "start": start, "result": result}
            for key, table, order in (
                ("values", "values", "ORDER BY value_time"),
                ("experts", "experts", "ORDER BY value_time, expert"),
                ("weather", "run_weather", "ORDER BY end_step, variable, point"),
                ("states", "states", "ORDER BY component, state_key"),
            ):
                archive[key] = rows(table, order)
                for row in archive[key]:
                    row.pop("run_id")
                    if key == "values":
                        if row.pop("variant") != start["variant"]:
                            raise ValueError("Stored forecast variant differs from run")
                    if key == "states":
                        row["payload"] = json.loads(row["payload"])
        return archive

    def _fallback(self, connection, archive: Document) -> None:
        self._insert(connection, "fallbacks", {
            "fallback_id": archive["id"], "delivery_day": archive["delivery_day"],
            "prepared_at": archive["prepared_at"], "models": _json(archive["models"]),
            "completeness": _json(archive["completeness"]), "content_hash": _hash(archive),
            "payload": _json(archive),
        }, conflict=True)
        stored = connection.execute(text(
            "SELECT content_hash FROM forecast.fallbacks WHERE fallback_id = :id"
        ), {"id": archive["id"]}).scalar_one()
        if stored != _hash(archive):
            raise ValueError("Stored fallback differs from file archive")

    def store_fallback(self, archive: Document) -> None:
        """Store a prepared fallback before its file is made available."""
        _validate_archive(archive)
        if archive["kind"] != "fallback":
            raise ValueError("Expected fallback archive")
        with self.engine.begin() as connection:
            self._fallback(connection, archive)

    def reconcile(self, authority: ServingAuthority, day: date) -> Document:
        """Import the authority; preserve conflicting mirrors and record an incident."""
        choice = authority.read_choice(day)
        archive = authority.read_served(day)
        with self.engine.begin() as connection:
            if archive is not None:
                if choice["kind"] == "primary":
                    self._insert(connection, "run_starts", archive["start"], conflict=True)
                    self._complete(connection, archive, conflict=True)
                else:
                    self._fallback(connection, archive)
            self._insert(connection, "choices", {
                "choice_hash": choice["choice_hash"], "delivery_day": day.isoformat(),
                "kind": choice["kind"],
                "run_id": choice["id"] if choice["kind"] == "primary" else None,
                "fallback_id": choice["id"] if choice["kind"] == "fallback" else None,
                "content_hash": choice["content_hash"], "values_path": choice["values_path"],
                "chosen_at": choice["chosen_at"],
            }, conflict=True)
            self._insert(connection, "deliveries", {
                "delivery_day": day.isoformat(), "choice_hash": choice["choice_hash"]
            }, conflict=True)
            mirror = connection.execute(text(
                "SELECT choice_hash FROM forecast.deliveries WHERE delivery_day = :day"
            ), {"day": day.isoformat()}).scalar_one()
            if mirror != choice["choice_hash"]:
                self._insert(connection, "delivery_incidents", {
                    "delivery_day": day.isoformat(), "mirror_hash": mirror,
                    "file_hash": choice["choice_hash"],
                    "detected_at": authority._time().isoformat(),
                }, conflict=True)
        authority.cleanup_temporary()
        return choice

    def served_for_scoring(self, authority: ServingAuthority, day: date) -> Document | None:
        """Refuse unreconciled choices; never select forecasts through the mirror."""
        choice = authority.read_choice(day)
        with self.engine.connect() as connection:
            row = connection.execute(text(
                "SELECT * FROM forecast.choices WHERE choice_hash = :hash"
            ), {"hash": choice["choice_hash"]}).mappings().one_or_none()
        if row is None:
            raise ValueError("Choice has not been reconciled")
        if choice["kind"] == "none":
            return None
        if choice["kind"] == "primary":
            archive = self.replay_run(row["run_id"])
        else:
            with self.engine.connect() as connection:
                archive = json.loads(connection.execute(text(
                    "SELECT payload FROM forecast.fallbacks WHERE fallback_id = :id"
                ), {"id": row["fallback_id"]}).scalar_one())
        if _hash(archive) != choice["content_hash"]:
            raise ValueError("Stored forecast differs from serving authority")
        return archive

    def record_score(self, authority: ServingAuthority, row: Document) -> None:
        """Insert one score row; it must name the reconciled choice of its day's file."""
        choice = authority.read_choice(date.fromisoformat(row["delivery_day"]))
        if choice["choice_hash"] != row["choice_hash"]:
            raise ValueError("Score does not belong to the choice file of its day")
        _utc(row["actual_max_ingested_at"])
        _utc(row["scored_at"])
        with self.engine.begin() as connection:
            self._insert(connection, "scores", {**row, "metrics": _json(row["metrics"])})
