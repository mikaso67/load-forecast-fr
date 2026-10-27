"""Build the clean model from raw rows known at a delivery day's freeze."""

import json
import os
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from load_forecast.cutoff import forecast_cutoff


def build_clean_snapshot(
    delivery_day: date,
    *,
    schema: str,
    project_dir: Path = Path("dbt"),
    now: datetime | None = None,
) -> str:
    """Build a dated table with dbt dev, then select it for this Python process.

    The caller supplies a writable development schema. The intermediate model is
    ephemeral for this build, so ordinary clean relations are never replaced.
    D2 slot availability remains the responsibility of the feature builders.
    """
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", schema) or schema in {
        "raw", "clean", "analytics", "labels", "public", "information_schema",
    } or schema.startswith("pg_"):
        raise ValueError("schema must be a development schema identifier")
    freeze = forecast_cutoff(delivery_day)
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if current < freeze:
        raise ValueError("Cannot build a snapshot before its freeze")
    relation = f"load_30min_snapshot_{delivery_day:%Y%m%d}"
    variables = {
        "ingested_before": freeze.isoformat(),
        "snapshot_relation": relation,
    }
    environment = os.environ.copy()
    environment["LFF_DEV_SCHEMA"] = schema
    subprocess.run(
        [
            sys.executable, "-m", "dbt.cli.main", "run",
            "--project-dir", str(project_dir), "--profiles-dir", str(project_dir),
            "--target", "dev", "--select", "+load_30min",
            "--vars", json.dumps(variables),
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    table = f"{schema}.{relation}"
    os.environ["LFF_CLEAN_TABLE"] = table
    return table
