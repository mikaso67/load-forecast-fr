"""Tests for Streamlit production monitoring dashboard."""

from datetime import date, timedelta
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import pandas as pd
import pytest
from dashboard.app import (
    compute_daily_scores,
    compute_dashboard_cusum,
    load_actual_and_rte,
    load_choices_data,
    load_model_values_by_variant,
    load_runs_data,
    load_served_values,
)
from sqlalchemy import create_engine, text
from streamlit.testing.v1 import AppTest

from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.monitoring import H, K

ROOT = Path(__file__).resolve().parents[1]


def create_sqlite_forecast_schema(engine, with_variants: bool = True):
    sql_text = (ROOT / "infra/postgres/forecast.sql").read_text(encoding="utf-8")
    tables_sql = sql_text.split("-- BEGIN TABLES", 1)[1].split("\n", 1)[1].split("-- END TABLES")[0]
    tables_sql = tables_sql.replace("CREATE TABLE forecast.", "CREATE TABLE ")
    with engine.begin() as conn:
        conn.connection.driver_connection.executescript(tables_sql)
        if with_variants:
            try:
                conn.execute(text("ALTER TABLE run_starts ADD COLUMN variant TEXT DEFAULT 'B'"))
            except Exception:
                pass
            try:
                conn.execute(text('ALTER TABLE "values" ADD COLUMN variant TEXT DEFAULT \'B\''))
            except Exception:
                pass
            conn.execute(text("DROP TABLE IF EXISTS state_runs"))
            conn.execute(text("""
                CREATE TABLE state_runs (
                    delivery_day DATE NOT NULL,
                    variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
                    run_id TEXT NOT NULL UNIQUE,
                    PRIMARY KEY (delivery_day, variant)
                )
            """))


@pytest.fixture
def empty_db(tmp_path):
    db_file = tmp_path / "empty_forecast.sqlite"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    create_sqlite_forecast_schema(engine, with_variants=False)
    yield engine, url
    engine.dispose()


@pytest.fixture
def fixture_db_both_variants(tmp_path):
    db_file = tmp_path / "fixture_forecast.sqlite"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    create_sqlite_forecast_schema(engine, with_variants=True)

    days = [date(2024, 4, 1), date(2024, 4, 2), date(2024, 4, 3)]
    clean_rows = []

    with engine.begin() as conn:
        for idx, day in enumerate(days):
            freeze = forecast_cutoff(day)
            run_id_b = f"run_b_{day.isoformat()}"
            run_id_a = f"run_a_{day.isoformat()}"
            is_fallback = (idx == 1)

            # Insert Variant B run
            conn.execute(text("""
                INSERT INTO run_starts (run_id, delivery_day, variant, freeze_time, kind, started_at, code_commit)
                VALUES (:run_id, :delivery_day, :variant, :freeze_time, :kind, :started_at, :code_commit)
            """), {
                "run_id": run_id_b, "delivery_day": day.isoformat(), "variant": "B",
                "freeze_time": freeze.isoformat(), "kind": "fallback" if is_fallback else "primary",
                "started_at": freeze.isoformat(), "code_commit": "abc1234",
            })

            conn.execute(text("""
                INSERT INTO run_results (run_id, completed_at, status, refit_day, mlflow_run_id,
                                        kalman_quarter, weather_status, last_consumption_slot,
                                        max_ingested_at, feature_hash, flags)
                VALUES (:run_id, :completed_at, :status, :refit_day, :mlflow_run_id,
                        :kalman_quarter, :weather_status, :last_consumption_slot,
                        :max_ingested_at, :feature_hash, :flags)
            """), {
                "run_id": run_id_b,
                "completed_at": (freeze + timedelta(minutes=5 if not is_fallback else 35)).isoformat(),
                "status": "complete", "refit_day": "2024-04-01", "mlflow_run_id": f"mlflow_{run_id_b}",
                "kalman_quarter": "2024Q2", "weather_status": "primary",
                "last_consumption_slot": (freeze - timedelta(hours=2, minutes=30)).isoformat(),
                "max_ingested_at": (freeze - timedelta(seconds=1)).isoformat(),
                "feature_hash": f"hash_{run_id_b}", "flags": '{"stale_models": false}',
            })

            # Insert Variant A run (shadow)
            conn.execute(text("""
                INSERT INTO run_starts (run_id, delivery_day, variant, freeze_time, kind, started_at, code_commit)
                VALUES (:run_id, :delivery_day, :variant, :freeze_time, :kind, :started_at, :code_commit)
            """), {
                "run_id": run_id_a, "delivery_day": day.isoformat(), "variant": "A",
                "freeze_time": freeze.isoformat(), "kind": "primary",
                "started_at": freeze.isoformat(), "code_commit": "abc1234",
            })

            conn.execute(text("""
                INSERT INTO run_results (run_id, completed_at, status, refit_day, mlflow_run_id,
                                        kalman_quarter, weather_status, last_consumption_slot,
                                        max_ingested_at, feature_hash, flags)
                VALUES (:run_id, :completed_at, :status, :refit_day, :mlflow_run_id,
                        :kalman_quarter, :weather_status, :last_consumption_slot,
                        :max_ingested_at, :feature_hash, :flags)
            """), {
                "run_id": run_id_a,
                "completed_at": (freeze + timedelta(minutes=6)).isoformat(),
                "status": "complete", "refit_day": "2024-04-01", "mlflow_run_id": f"mlflow_{run_id_a}",
                "kalman_quarter": "2024Q2", "weather_status": "primary",
                "last_consumption_slot": (freeze - timedelta(hours=2, minutes=30)).isoformat(),
                "max_ingested_at": (freeze - timedelta(seconds=1)).isoformat(),
                "feature_hash": f"hash_{run_id_a}", "flags": '{"stale_models": false}',
            })

            slots = list(delivery_slots(day, "30min"))
            for slot in slots:
                slot_iso = slot.isoformat()
                local_slot = slot.astimezone(PARIS).hour * 2 + slot.astimezone(PARIS).minute // 30
                pt_b = 45000.0 + idx * 500.0
                pt_a = 44700.0 + idx * 480.0

                # Variant B values
                conn.execute(text("""
                    INSERT INTO "values" (run_id, variant, value_time, local_slot, point, lower, upper, n_pool)
                    VALUES (:run_id, :variant, :value_time, :local_slot, :point, :lower, :upper, :n_pool)
                """), {
                    "run_id": run_id_b, "variant": "B", "value_time": slot_iso, "local_slot": local_slot,
                    "point": pt_b, "lower": pt_b - 300.0, "upper": pt_b + 300.0, "n_pool": 60,
                })

                # Variant A values
                conn.execute(text("""
                    INSERT INTO "values" (run_id, variant, value_time, local_slot, point, lower, upper, n_pool)
                    VALUES (:run_id, :variant, :value_time, :local_slot, :point, :lower, :upper, :n_pool)
                """), {
                    "run_id": run_id_a, "variant": "A", "value_time": slot_iso, "local_slot": local_slot,
                    "point": pt_a, "lower": pt_a - 280.0, "upper": pt_a + 280.0, "n_pool": 60,
                })

                clean_rows.append({
                    "value_time": slot_iso,
                    "consommation": pt_b + 50.0,
                    "prevision_j1": pt_b - 200.0,
                })

            # Insert state runs for both variants
            conn.execute(text("""
                INSERT INTO state_runs (delivery_day, variant, run_id)
                VALUES (:delivery_day, :variant, :run_id)
            """), {
                "delivery_day": day.isoformat(), "variant": "B", "run_id": run_id_b,
            })
            conn.execute(text("""
                INSERT INTO state_runs (delivery_day, variant, run_id)
                VALUES (:delivery_day, :variant, :run_id)
            """), {
                "delivery_day": day.isoformat(), "variant": "A", "run_id": run_id_a,
            })

            # Serving choice only references Variant B (or fallback)
            choice_hash = f"chash_{day.isoformat()}"
            conn.execute(text("""
                INSERT INTO choices (choice_hash, delivery_day, kind, run_id, fallback_id, content_hash, values_path, chosen_at)
                VALUES (:choice_hash, :delivery_day, :kind, :run_id, :fallback_id, :content_hash, :values_path, :chosen_at)
            """), {
                "choice_hash": choice_hash, "delivery_day": day.isoformat(),
                "kind": "fallback" if is_fallback else "primary",
                "run_id": run_id_b if not is_fallback else None,
                "fallback_id": f"fb_{run_id_b}" if is_fallback else None,
                "content_hash": f"chash_{run_id_b}", "values_path": f"values/{choice_hash}.json",
                "chosen_at": freeze.isoformat(),
            })

            conn.execute(text("""
                INSERT INTO deliveries (delivery_day, choice_hash)
                VALUES (:delivery_day, :choice_hash)
            """), {
                "delivery_day": day.isoformat(), "choice_hash": choice_hash,
            })

            if is_fallback:
                fb_payload = {
                    "kind": "fallback", "id": f"fb_{run_id_b}", "delivery_day": day.isoformat(),
                    "models": ["calendar"], "completeness": {"complete": True},
                    "values": [{"value_time": s.isoformat(),
                                "local_slot": s.astimezone(PARIS).hour * 2 + s.astimezone(PARIS).minute // 30,
                                "point": 44000.0} for s in slots],
                }
                import json
                conn.execute(text("""
                    INSERT INTO fallbacks (fallback_id, delivery_day, prepared_at, models, completeness, content_hash, payload)
                    VALUES (:fallback_id, :delivery_day, :prepared_at, :models, :completeness, :content_hash, :payload)
                """), {
                    "fallback_id": f"fb_{run_id_b}", "delivery_day": day.isoformat(),
                    "prepared_at": (freeze - timedelta(hours=23)).isoformat(),
                    "models": '["calendar"]', "completeness": '{"complete": true}',
                    "content_hash": f"hash_fb_{run_id_b}", "payload": json.dumps(fb_payload),
                })

        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS clean_load_30min (
                value_time TEXT PRIMARY KEY,
                consommation REAL,
                prevision_j1 REAL
            )
        """))
        for cr in clean_rows:
            conn.execute(text("""
                INSERT INTO clean_load_30min (value_time, consommation, prevision_j1)
                VALUES (:value_time, :consommation, :prevision_j1)
            """), cr)

    yield engine, url
    engine.dispose()


def test_empty_database_handling(empty_db, monkeypatch):
    engine, url = empty_db
    monkeypatch.setenv("LFF_DATABASE_URL", url)
    runs_df = load_runs_data(engine)
    choices_df = load_choices_data(engine)
    assert runs_df.empty
    assert choices_df.empty

    at = AppTest.from_file(str(ROOT / "dashboard/app.py"))
    at.run()
    assert not at.exception
    assert any("empty" in msg.value.lower() for msg in at.info)


def test_fixture_database_with_both_variants(fixture_db_both_variants, monkeypatch):
    engine, url = fixture_db_both_variants
    monkeypatch.setenv("LFF_DATABASE_URL", url)
    monkeypatch.setenv("LFF_CLEAN_TABLE", "clean_load_30min")

    runs_df = load_runs_data(engine)
    assert len(runs_df) == 6
    assert set(runs_df["variant"].unique()) == {"A", "B"}

    choices_df = load_choices_data(engine)
    assert len(choices_df) == 3

    served_values = load_served_values(engine, choices_df)
    assert not served_values.empty
    assert "forecast_mw" in served_values.columns

    days = choices_df["delivery_day"].tolist()
    models_dict = load_model_values_by_variant(engine, days)
    assert not models_dict["A"].empty
    assert not models_dict["B"].empty
    assert set(models_dict["A"]["variant"].unique()) == {"A"}
    assert set(models_dict["B"]["variant"].unique()) == {"B"}

    min_time = served_values["value_time"].min()
    max_time = served_values["value_time"].max()
    actual_df = load_actual_and_rte(engine, min_time, max_time)
    assert not actual_df.empty
    assert "consommation" in actual_df.columns

    scores_df = compute_daily_scores(served_values, actual_df, models_dict)
    assert not scores_df.empty
    assert len(scores_df) == 3
    assert "served_mae" in scores_df.columns
    assert "model_b_mae" in scores_df.columns
    assert "model_a_mae" in scores_df.columns
    assert "err_b" in scores_df.columns
    assert "err_a" in scores_df.columns
    assert scores_df["model_a_mae"].notna().all()
    assert scores_df["err_a"].notna().all()

    daily_err_b = pd.Series(
        scores_df["err_b"].to_numpy(),
        index=pd.to_datetime(scores_df["delivery_day"]),
    )
    cusum_b = compute_dashboard_cusum(daily_err_b, h=H, k=K)
    assert "s_plus" in cusum_b.columns
    assert "s_minus" in cusum_b.columns
    assert len(cusum_b) == 3

    daily_err_a = pd.Series(
        scores_df["err_a"].to_numpy(),
        index=pd.to_datetime(scores_df["delivery_day"]),
    )
    cusum_a = compute_dashboard_cusum(daily_err_a, h=H, k=K)
    assert "s_plus" in cusum_a.columns
    assert "s_minus" in cusum_a.columns
    assert len(cusum_a) == 3

    at = AppTest.from_file(str(ROOT / "dashboard/app.py"))
    at.run()
    assert not at.exception
    assert any("Headline served MAE" in m.label for m in at.metric)
    assert any("Variant A shadow MAE" in m.label for m in at.metric)
