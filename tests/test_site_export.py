"""Tests for static public site export and publishing script."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import create_engine, text

from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff
from load_forecast.site_export import build_site_payload, export_site_data

ROOT = Path(__file__).resolve().parents[1]


def render_site(payload, mode=None):
    """Execute the shipped script with a minimal DOM and capture its chart options."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed to verify the site script")
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const data = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = {};
const charts = {};
const listeners = {};
function element(id) {
  return elements[id] ||= {textContent: '', style: {}, clientWidth: 375,
    classList: {toggle() {}}, setAttribute() {},
    addEventListener(event, fn) {listeners[id] = fn;},
    parentElement: {querySelector() {return {};}}};
}
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: element, querySelectorAll() {return [];},
    readyState: 'complete'},
  window: {innerWidth: 375, addEventListener() {}},
  echarts: {init(el) {return {setOption(option) {
    charts[Object.keys(elements).find(id => elements[id] === el)] = option;
  }, dispose() {}};}},
  fetch: () => Promise.resolve({ok: true, json: () => Promise.resolve(data)}),
  console, setTimeout,
});
setTimeout(() => {
  const initialMae = elements['tile-mae-val'].textContent;
  const initialRolling = charts['chart-rolling-mae'];
  if (process.argv[2] && listeners['comparison-mode']) {
    elements['comparison-mode'].value = process.argv[2];
    listeners['comparison-mode']();
  }
  const daily = charts['chart-daily-mae-28d'];
  process.stdout.write(JSON.stringify({
    initialMae, initialRolling, dailyLegend: daily.legend.data, dailySeries: daily.series, rolling: charts['chart-rolling-mae'],
    text: Object.fromEntries(Object.entries(elements).map(([k,v]) => [k,v.textContent])),
    degradedHidden: elements['degraded-inputs'].hidden,
    bars: daily.series[0].data,
    tooltip: daily.tooltip.formatter([{dataIndex: Math.max(0, data.daily_mae_28d.findIndex(d =>
      ['realtime', 'real-time'].includes(d.actual_vintage)))}]),
    areas: charts['chart-recent7d'].series[0].markArea.data,
    recentSeries: charts['chart-recent7d'].series,
    recentLegend: charts['chart-recent7d'].legend.data,
  }));
}, 0);
"""
    result = subprocess.run([node, "-e", script, str(ROOT / "site/app.js"), *([mode] if mode else [])],
                            input=json.dumps(payload), text=True, capture_output=True, check=True)
    return json.loads(result.stdout)


def create_sqlite_forecast_schema(engine):
    sql_text = (ROOT / "infra/postgres/forecast.sql").read_text(encoding="utf-8")
    tables_sql = sql_text.split("-- BEGIN TABLES", 1)[1].split("\n", 1)[1].split("-- END TABLES")[0]
    tables_sql = tables_sql.replace("CREATE TABLE forecast.", "CREATE TABLE ")
    with engine.begin() as conn:
        conn.connection.driver_connection.executescript(tables_sql)
        try:
            conn.execute(text("ALTER TABLE run_starts ADD COLUMN variant TEXT DEFAULT 'B'"))
        except Exception:
            pass
        try:
            conn.execute(text('ALTER TABLE "values" ADD COLUMN variant TEXT DEFAULT \'B\''))
        except Exception:
            pass


@pytest.mark.parametrize('stored_flags', [False, True])
def test_degraded_day_visible_with_legacy_or_new_provenance(fixture_db, stored_flags):
    day = '2024-04-14'
    flags = {'degraded_inputs': ['expert_backup_chain', 'stale_consumption']} if stored_flags else {}
    with fixture_db.begin() as conn:
        conn.execute(text('''UPDATE run_results SET last_consumption_slot=:slot, flags=:flags
            WHERE run_id=:run'''), {'slot': '2024-04-11T11:30:00Z', 'flags': json.dumps(flags),
                                    'run': f'run_b_{day}'})
        conn.execute(text('''INSERT INTO experts (run_id, value_time, expert, value, backup)
            VALUES (:run, :slot, 'linear', 45000, 'backup_chain')'''),
                     {'run': f'run_b_{day}', 'slot': '2024-04-13T22:00:00Z'})
    data = build_site_payload(engine=fixture_db)
    assert data['tomorrow']['degraded_inputs'] == ['expert_backup_chain', 'stale_consumption']
    assert data['degraded_deliveries'] == [{'date': day, 'reasons': [
        'expert_backup_chain', 'stale_consumption']}]
    rendered = render_site(data)
    assert 'Degraded inputs: 2024-04-14' in rendered['text']['degraded-inputs']
    assert rendered['degradedHidden'] is False


@pytest.fixture
def empty_db(tmp_path):
    db_file = tmp_path / "empty_forecast.sqlite"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    create_sqlite_forecast_schema(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def fixture_db(tmp_path, monkeypatch):
    db_file = tmp_path / "fixture_forecast.sqlite"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    create_sqlite_forecast_schema(engine)

    days = [date(2024, 4, 1) + timedelta(days=i) for i in range(14)]
    clean_rows = []

    with engine.begin() as conn:
        for idx, day in enumerate(days):
            freeze = forecast_cutoff(day)
            run_id = f"run_b_{day.isoformat()}"
            is_fallback = (idx == 6)

            conn.execute(text("""
                INSERT INTO run_starts (run_id, delivery_day, variant, freeze_time, kind, started_at, code_commit)
                VALUES (:run_id, :delivery_day, :variant, :freeze_time, :kind, :started_at, :code_commit)
            """), {
                "run_id": run_id, "delivery_day": day.isoformat(), "variant": "B",
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
                "run_id": run_id,
                "completed_at": (freeze + timedelta(minutes=5 if not is_fallback else 35)).isoformat(),
                "status": "complete", "refit_day": "2024-04-01", "mlflow_run_id": f"mlflow_{run_id}",
                "kalman_quarter": "2024Q2", "weather_status": "primary",
                "last_consumption_slot": (freeze - timedelta(hours=2, minutes=30)).isoformat(),
                "max_ingested_at": (freeze - timedelta(seconds=1)).isoformat(),
                "feature_hash": f"hash_{run_id}", "flags": '{"stale_models": false}',
            })

            slots = list(delivery_slots(day, "30min"))
            for slot in slots:
                slot_iso = slot.isoformat()
                local_slot = slot.astimezone(PARIS).hour * 2 + slot.astimezone(PARIS).minute // 30
                pt = 45000.0 + idx * 500.0

                if not is_fallback:
                    conn.execute(text("""
                        INSERT INTO "values" (run_id, variant, value_time, local_slot, point, lower, upper, n_pool)
                        VALUES (:run_id, :variant, :value_time, :local_slot, :point, :lower, :upper, :n_pool)
                    """), {
                        "run_id": run_id, "variant": "B", "value_time": slot_iso, "local_slot": local_slot,
                        "point": pt, "lower": pt - 300.0, "upper": pt + 300.0, "n_pool": 60,
                    })

                # Only include actuals up to day 12 (leaving day 13 as tomorrow with no actuals)
                if idx < 13:
                    clean_rows.append({
                        "value_time": slot_iso,
                        "consommation": pt + 40.0,
                        "prevision_j1": pt - 150.0,
                    })
                else:
                    clean_rows.append({
                        "value_time": slot_iso,
                        "consommation": None,
                        "prevision_j1": pt - 100.0,
                    })

            choice_hash = f"chash_{day.isoformat()}"
            conn.execute(text("""
                INSERT INTO choices (choice_hash, delivery_day, kind, run_id, fallback_id, content_hash, values_path, chosen_at)
                VALUES (:choice_hash, :delivery_day, :kind, :run_id, :fallback_id, :content_hash, :values_path, :chosen_at)
            """), {
                "choice_hash": choice_hash, "delivery_day": day.isoformat(),
                "kind": "fallback" if is_fallback else "primary",
                "run_id": run_id if not is_fallback else None,
                "fallback_id": f"fb_{run_id}" if is_fallback else None,
                "content_hash": f"chash_{run_id}", "values_path": f"values/{choice_hash}.json",
                "chosen_at": freeze.isoformat(),
            })

            if is_fallback:
                fb_payload = {
                    "kind": "fallback", "id": f"fb_{run_id}", "delivery_day": day.isoformat(),
                    "models": ["calendar"], "completeness": {"complete": True},
                    "values": [{
                        "value_time": s.isoformat(),
                        "local_slot": s.astimezone(PARIS).hour * 2 + s.astimezone(PARIS).minute // 30,
                        "point": 44000.0,
                    } for s in slots],
                }
                conn.execute(text("""
                    INSERT INTO fallbacks (fallback_id, delivery_day, prepared_at, models, completeness, content_hash, payload)
                    VALUES (:fallback_id, :delivery_day, :prepared_at, :models, :completeness, :content_hash, :payload)
                """), {
                    "fallback_id": f"fb_{run_id}", "delivery_day": day.isoformat(),
                    "prepared_at": (freeze - timedelta(minutes=5)).isoformat(),
                    "models": "calendar", "completeness": "complete", "content_hash": "hash_fb",
                    "payload": json.dumps(fb_payload),
                })

        # Create clean table in SQLite
        conn.execute(text("""
            CREATE TABLE load_30min (
                value_time TEXT PRIMARY KEY,
                consommation REAL,
                prevision_j1 REAL,
                source_dataset TEXT DEFAULT 'eco2mix-national-cons-def'
            )
        """))
        for cr in clean_rows:
            conn.execute(text("""
                INSERT INTO load_30min (value_time, consommation, prevision_j1)
                VALUES (:value_time, :consommation, :prevision_j1)
            """), cr)

    install_raw_fixture(engine, monkeypatch, clean_rows)
    with engine.begin() as conn:
        conn.execute(text("""INSERT INTO run_starts
            SELECT replace(run_id, 'run_b_', 'run_a_'), delivery_day, 'A', freeze_time,
                   kind, started_at, code_commit FROM run_starts WHERE variant = 'B'"""))
        conn.execute(text("""INSERT INTO run_results
            SELECT replace(run_id, 'run_b_', 'run_a_'), completed_at, status, refit_day,
                   mlflow_run_id, kalman_quarter, weather_status, last_consumption_slot,
                   max_ingested_at, feature_hash, flags FROM run_results"""))
        conn.execute(text("""INSERT INTO state_runs
            SELECT delivery_day, 'A', run_id FROM run_starts WHERE variant = 'A'"""))
        for idx, day in enumerate(days):
            for slot in delivery_slots(day, "30min"):
                point = 44000 if idx == 6 else 45000 + idx * 500
                conn.execute(text('''INSERT INTO "values"
                    (run_id, variant, value_time, local_slot, point, n_pool)
                    VALUES (:id, 'A', :time, 0, :point, 60)'''),
                    {"id": f"run_a_{day}", "time": slot.isoformat(), "point": point})
    yield engine
    engine.dispose()


def install_raw_fixture(engine, monkeypatch, rows):
    """Run the scoring reader on SQLite with only SQL date syntax translated."""
    from load_forecast.production_scoring import ACTUALS_QUERY

    query = ACTUALS_QUERY.replace(
        "extract(minute FROM value_time AT TIME ZONE 'UTC')::int",
        "CAST(strftime('%M', value_time) AS INTEGER)").replace(
        "original_values->>'heure'", "json_extract(original_values, '$.heure')").replace(
        "to_char(value_time AT TIME ZONE 'Europe/Paris', 'HH24:MI')", "paris_time(value_time)")
    query = query.replace("value_time >= :start", "julianday(value_time) >= julianday(:start)")
    query = query.replace("value_time < :end", "julianday(value_time) < julianday(:end)")
    query = query.replace("ingested_at < :as_of", "julianday(ingested_at) < julianday(:as_of)")
    monkeypatch.setattr("load_forecast.production_scoring.ACTUALS_QUERY", query)
    with engine.begin() as conn:
        conn.exec_driver_sql("ATTACH DATABASE ':memory:' AS raw")
        conn.connection.driver_connection.create_function(
            "paris_time", 1, lambda value: pd.Timestamp(value).tz_convert(PARIS).strftime("%H:%M"))
        conn.execute(text("""CREATE TABLE raw.eco2mix (
            value_time TEXT, consommation REAL, prevision_j1 REAL, source_dataset TEXT,
            ingested_at TEXT, payload_hash TEXT, original_values TEXT)"""))
        for row in rows:
            stamp = pd.Timestamp(row["value_time"])
            conn.execute(text("""INSERT INTO raw.eco2mix VALUES
                (:value_time, :consommation, :prevision_j1, :source_dataset,
                 :ingested_at, :payload_hash, :original_values)"""), {
                **row, "source_dataset": row.get("source_dataset", "eco2mix-national-cons-def"),
                "ingested_at": (stamp + timedelta(hours=1)).isoformat(), "payload_hash": "first",
                "original_values": json.dumps({"heure": stamp.tz_convert(PARIS).strftime("%H:%M")})})


def test_export_empty_database(empty_db, tmp_path):
    out_file = tmp_path / "latest.json"
    result_path = export_site_data(engine=empty_db, output_path=out_file)
    assert result_path.exists()

    with out_file.open(encoding="utf-8") as f:
        data = json.load(f)

    assert data["status"] == "empty"
    assert data["tomorrow"] is None
    assert data["header_tiles"]["tomorrow_peak"] is None
    assert data["header_tiles"]["rolling_28d_mae"] is None
    assert data["header_tiles"]["delivery_counts"]["served"] == 0
    assert data["recent_14d"] == []
    assert data["rolling_mae_history"] == []
    assert data["cusum_history"]["series"] == []


def test_export_fixture_database(fixture_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LFF_CLEAN_TABLE", "load_30min")
    out_file = tmp_path / "latest.json"
    result_path = export_site_data(engine=fixture_db, output_path=out_file)
    assert result_path.exists()

    with out_file.open(encoding="utf-8") as f:
        data = json.load(f)

    assert data["status"] == "ok"
    assert data["tomorrow"] is not None
    assert len(data["tomorrow"]["slots"]) == 48

    # Check rounding to MW on slots
    for slot in data["tomorrow"]["slots"]:
        assert isinstance(slot["forecast_mw"], int)
        assert isinstance(slot["rte_mw"], int)
        if slot["lower_mw"] is not None:
            assert isinstance(slot["lower_mw"], int)
            assert isinstance(slot["upper_mw"], int)

    # Check header tiles
    tiles = data["header_tiles"]
    assert tiles["tomorrow_peak"] is not None
    assert isinstance(tiles["tomorrow_peak"]["peak_mw"], int)
    assert tiles["tomorrow_peak"]["peak_time"] is not None

    assert tiles["rolling_28d_mae"] is not None
    assert isinstance(tiles["rolling_28d_mae"]["model_mw"], int)
    assert isinstance(tiles["rolling_28d_mae"]["rte_mw"], int)
    assert "relative_gain_pct" in tiles["rolling_28d_mae"]

    assert tiles["beating_rte"] is not None
    assert isinstance(tiles["beating_rte"]["pct"], (int, float))

    counts = tiles["delivery_counts"]
    assert counts["served"] == 13
    assert counts["fallback"] == 1
    assert counts["none"] == 0
    assert counts["total"] == 14

    # Check recent slot series
    assert len(data["recent_7d"]) > 0
    assert len(data["recent_14d"]) > 0
    first_slot = data["recent_7d"][0]
    assert "time" in first_slot
    assert "forecast_mw" in first_slot
    assert "actual_mw" in first_slot

    # Check daily MAE 28d
    assert len(data["daily_mae_28d"]) > 0
    assert "model_mae" in data["daily_mae_28d"][0]
    assert "actual_vintage" in data["daily_mae_28d"][0]
    assert "actual_vintage" in data["rolling_mae_history"][0]
    assert "is_provisional" in tiles["rolling_28d_mae"]
    assert "is_provisional" in tiles["beating_rte"]
    assert "vintage_switch_date" in data

    # Check CUSUM series
    assert len(data["cusum_history"]["series"]) > 0
    assert data["cusum_history"]["h"] == 11.46
    assert data["cusum_history"]["vintage"] == "consolidated"


def test_export_with_replay_and_served_days(fixture_db, tmp_path, monkeypatch):
    monkeypatch.setenv("LFF_CLEAN_TABLE", "load_30min")

    # Generate 3 replay days before the served days (2024-03-29, 2024-03-30, 2024-03-31)
    replay_rows = []
    for d_offset in range(3):
        day = date(2024, 3, 29) + timedelta(days=d_offset)
        slots = list(delivery_slots(day, "30min"))
        for s in slots:
            pt = 42000.0 + d_offset * 400.0
            replay_rows.append({
                "value_time": s,
                "delivery_day": day,
                "variant": "B",
                "point": pt,
                "lower": pt - 250.0,
                "upper": pt + 250.0,
                "actual": pt + 30.0,
                "rte_d1": pt - 100.0,
            })
    replay_df = pd.DataFrame(replay_rows)

    out_file = tmp_path / "latest.json"
    export_site_data(engine=fixture_db, replay_source=replay_df, output_path=out_file)

    with out_file.open(encoding="utf-8") as f:
        data = json.load(f)

    assert data["status"] == "ok"
    counts = data["header_tiles"]["delivery_counts"]
    assert counts["replay"] == 3
    assert counts["served"] == 13
    assert counts["fallback"] == 1
    assert counts["total"] == 17

    # Verify that replayed days appear with is_replay=True
    replayed_mae_rows = [r for r in data["rolling_mae_history"] if r["is_replay"]]
    assert len(replayed_mae_rows) == 3

    served_mae_rows = [r for r in data["rolling_mae_history"] if not r["is_replay"]]
    assert len(served_mae_rows) == 13


def test_publish_site_script(tmp_path):
    repo_dir = tmp_path / "public_clone"
    repo_dir.mkdir()

    # Initialize a dummy git repository with gh-pages branch
    subprocess.check_call(["git", "init", "-b", "gh-pages"], cwd=repo_dir)
    subprocess.check_call(["git", "config", "user.name", "Tester"], cwd=repo_dir)
    subprocess.check_call(["git", "config", "user.email", "tester@example.com"], cwd=repo_dir)

    # Initial commit in dummy repo
    dummy_file = repo_dir / "README.md"
    dummy_file.write_text("# Public site\n", encoding="utf-8")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo_dir)
    subprocess.check_call(["git", "commit", "-m", "Initial commit"], cwd=repo_dir)

    script_path = ROOT / "scripts/publish_site.ps1"
    pwsh_cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", str(script_path),
        "-PublicRepoPath", str(repo_dir),
        "-Branch", "gh-pages",
        "-DryRun",
    ]

    # First run: should copy files and commit
    proc = subprocess.run(pwsh_cmd, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "Data changes detected" in proc.stdout

    # Verify site files were copied
    assert (repo_dir / "index.html").exists()
    assert (repo_dir / "styles.css").exists()
    assert (repo_dir / "app.js").exists()
    assert (repo_dir / "vendor/echarts.min.js").exists()

    # Second run without data changes: should report up to date
    proc2 = subprocess.run(pwsh_cmd, capture_output=True, text=True)
    assert proc2.returncode == 0
    assert "Public site is up to date" in proc2.stdout


def test_export_replay_forecasts_extending_past_last_actual(tmp_path):
    """Test that when replay forecasts extend beyond the last actual, recent_7d history is preserved."""
    # 14 days of forecasts: 2026-09-29 to 2026-10-12
    # Actuals exist only through 2026-10-10 (days 2026-10-11 and 2026-10-12 have NaN actuals)
    start_d = date(2026, 9, 29)
    end_d = date(2026, 10, 12)
    last_actual_d = date(2026, 10, 10)

    rows = []
    days = [start_d + timedelta(days=i) for i in range((end_d - start_d).days + 1)]
    for d in days:
        slots = list(delivery_slots(d, "30min"))
        has_actual = (d <= last_actual_d)
        for s in slots:
            pt = 45000.0 + (d - start_d).days * 100.0
            rows.append({
                "value_time": s,
                "delivery_day": d,
                "variant": "B",
                "point": pt,
                "lower": pt - 300.0,
                "upper": pt + 300.0,
                "actual": pt + 50.0 if has_actual else float("nan"),
                "rte_d1": pt - 120.0 if has_actual else float("nan"),
            })
    replay_df = pd.DataFrame(rows)

    out_file = tmp_path / "latest.json"
    result_path = export_site_data(engine=None, replay_source=replay_df, output_path=out_file, days_back=90)
    assert result_path.exists()

    with out_file.open(encoding="utf-8") as f:
        data = json.load(f)

    assert data["status"] == "ok"
    assert data["tomorrow"] is not None
    assert data["tomorrow"]["delivery_day"] == "2026-10-12"

    # Recent 7-day history must NOT be empty: it should contain the last 7 days of actuals
    assert len(data["recent_7d"]) == 7 * 48
    recent_dates = sorted({s["date"] for s in data["recent_7d"]})
    assert len(recent_dates) == 7
    assert recent_dates[-1] == "2026-10-10"
    assert recent_dates[0] == "2026-10-04"

    # Every slot in recent_7d must have a valid actual_mw
    for slot in data["recent_7d"]:
        assert slot["actual_mw"] is not None
        assert slot["forecast_mw"] is not None

    # Daily MAE 28d must include only scored days (ending at 2026-10-10)
    daily_mae_dates = [d["date"] for d in data["daily_mae_28d"]]
    assert "2026-10-10" in daily_mae_dates
    assert "2026-10-11" not in daily_mae_dates
    assert "2026-10-12" not in daily_mae_dates


def test_missing_relations_are_empty(tmp_path):
    from sqlalchemy import create_engine

    from load_forecast.site_export import _load_actual_and_rte, _load_stored_scores

    engine = create_engine('sqlite://')
    assert build_site_payload(engine=engine)['status'] == 'empty'
    assert _load_stored_scores(engine).empty
    now = datetime.now(UTC)
    assert _load_actual_and_rte(engine, now, now).empty


def test_connection_failure_propagates(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError

    engine = create_engine('sqlite://')
    def fail(*args, **kwargs):
        raise OperationalError('connect', None, RuntimeError('offline'))
    monkeypatch.setattr(engine, 'connect', fail)
    with pytest.raises(OperationalError):
        build_site_payload(engine=engine)


def test_actuals_ignore_clean_relation(fixture_db, monkeypatch):
    from load_forecast.site_export import _load_actual_and_rte

    monkeypatch.setenv('LFF_CLEAN_TABLE', 'absent_clean_table')
    assert not _load_actual_and_rte(fixture_db, datetime(2024, 1, 1, tzinfo=UTC),
                                    datetime(2025, 1, 1, tzinfo=UTC)).empty


@pytest.mark.parametrize('setting', ['POSTGRES_USER', 'POSTGRES_PASSWORD'])
def test_reader_requires_credentials(monkeypatch, setting):
    from load_forecast.site_export import get_reader_engine

    monkeypatch.delenv('LFF_DATABASE_URL', raising=False)
    monkeypatch.setenv('POSTGRES_USER', 'reader')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'fixture')
    monkeypatch.delenv(setting)
    with pytest.raises(ValueError, match=setting):
        get_reader_engine()


@pytest.mark.parametrize("start", [date(2024, 3, 30), date(2024, 10, 26)])
@pytest.mark.parametrize("vintages", [
    ["realtime", "real-time", "realtime"],
    ["consolidated", "realtime", "definitive"],
    ["unknown", "realtime", "unknown"],
    ["realtime", "consolidated", "realtime"],
])
def test_comparisons_select_matching_scale(monkeypatch, start, vintages):
    rows = []
    scores = []
    for i, vintage in enumerate(vintages):
        day = start + timedelta(days=i)
        # Using served B against real-time actuals would reverse the verdict.
        model_error = 1100 if vintage in ("realtime", "real-time") else -100
        for slot in delivery_slots(day, "30min"):
            rows.append({"value_time": slot, "delivery_day": day,
                         "variant": "B", "point": 45000 + model_error, "actual": 45000,
                         "rte_d1": 45200, "actual_vintage": vintage})
        scores.append({"delivery_day": day, "actual_vintage": vintage,
                       "model_mae": abs(model_error), "model_mean_error": model_error,
                       "rte_d1_mae": 200, "rte_d1_mean_error": 200,
                       "n_common_slots": len(delivery_slots(day, "30min"))})
    shadow_rows = [dict(row, variant="A", point=45050) for row in rows]
    rows.extend(shadow_rows)
    # The latest delivery is still unscored.
    day = start + timedelta(days=3)
    rows.extend({"value_time": slot, "delivery_day": day, "variant": "B", "point": 45000}
                for slot in delivery_slots(day, "30min"))
    monkeypatch.setattr("load_forecast.site_export._load_replay_scores",
                        lambda _: pd.DataFrame(scores + [dict(score, variant="A", model_mae=50,
                            model_mean_error=50) for score in scores
                            if score["actual_vintage"] in ("realtime", "real-time")]))
    data = build_site_payload(replay_source=pd.DataFrame(rows))
    n_final = sum(v in ("consolidated", "definitive") for v in vintages)
    n_provisional = sum(v in ("realtime", "real-time") for v in vintages)
    mae = data["header_tiles"]["rolling_28d_mae"]
    beating = data["header_tiles"]["beating_rte"]
    assert mae["provisional_days"] == beating["provisional_days"] == n_provisional
    assert mae["total_days"] == beating["total_days"] == n_final + n_provisional
    assert mae["status"] == "ok"
    assert mae["model_mw"] == round((100 * n_final + 50 * n_provisional) / (n_final + n_provisional))
    assert mae["rte_mw"] == 200
    assert beating["better_days"] == n_final + n_provisional
    assert beating["pct"] == 100
    assert mae["is_provisional"]
    last = data["rolling_mae_history"][-1]
    assert last["model_rolling_mae"] == mae["model_mw"]
    assert last["rte_rolling_mae"] == 200
    assert len(data["daily_mae_28d"]) == 3
    for i, row in enumerate(data["daily_mae_28d"]):
        assert row["actual_vintage"] == vintages[i]
        assert row["model_mean_error"] == (50 if vintages[i] in ("realtime", "real-time") else scores[i]["model_mean_error"])
        assert row["rte_mean_error"] == 200
        assert row["model_mae"] == (50 if vintages[i] in ("realtime", "real-time") else scores[i]["model_mae"])
    assert {r["actual_vintage"] for r in data["recent_7d"]} == set(vintages)
    rendered = render_site(data)
    text = rendered["text"]
    assert text["tile-mae-val"] == f"{mae['model_mw']} vs 200 MW"
    assert "provisional" in text["title-recent7d"]
    assert "provisional" in text["title-daily-mae"]
    assert "provisional" in text["title-rolling-mae"]
    assert f"{n_provisional} provisional" in text["tile-mae-sub"]
    assert "shadow variant A" in text["finding-recent7d"]
    provisional_index = next(i for i, v in enumerate(vintages) if v in ("realtime", "real-time"))
    assert rendered["bars"][provisional_index]["value"] == 50
    assert rendered["bars"][provisional_index]["itemStyle"]["decal"] is not None
    assert "provisional" in rendered["tooltip"]
    assert "bias (forecast minus actual): 50 MW" in rendered["tooltip"]
    assert "Difference:" not in rendered["tooltip"]
    assert any(area[0]["label"]["formatter"] == "provisional" for area in rendered["areas"])
    if vintages == ["realtime", "consolidated", "realtime"]:
        areas = [area for area in rendered["areas"] if area[0]["label"]["formatter"] == "provisional"]
        assert len(areas) == 2
        final_date = (start + timedelta(days=1)).isoformat()
        assert all(not (area[0]["xAxis"][:10] <= final_date <= area[1]["xAxis"][:10])
                   for area in areas)


def test_slot_scores_keep_raw_bias_and_vintage(fixture_db, monkeypatch):
    monkeypatch.setenv("LFF_CLEAN_TABLE", "load_30min")
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset = 'eco2mix-national-tr'"))
    data = build_site_payload(engine=fixture_db)
    assert data["header_tiles"]["rolling_28d_mae"]["status"] == "ok"
    assert data["header_tiles"]["rolling_28d_mae"]["provisional_days"] == 13
    first = data["daily_mae_28d"][0]
    assert first["model_mae"] == 40
    assert first["model_mean_error"] == -40
    assert first["rte_mean_error"] == -190
    assert first["actual_vintage"] == "realtime"


def test_final_scores_replace_provisional_even_after_a_realtime_rerun():
    from load_forecast.site_export import _load_stored_scores

    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("""CREATE TABLE scores (
            delivery_day TEXT, choice_hash TEXT, actual_vintage TEXT,
            scored_at TEXT, metrics TEXT)"""))
        for vintage, scored_at in [("consolidated", "2026-10-14"),
                                   ("definitive", "2026-10-15"),
                                   ("realtime", "2026-10-16")]:
            conn.execute(text("INSERT INTO scores VALUES ('2026-08-01', 'choice', :v, :t, '{}')"),
                         {"v": vintage, "t": scored_at})
    assert _load_stored_scores(engine).iloc[0]["actual_vintage"] == "definitive"
    engine.dispose()


def test_comparison_window_does_not_reuse_old_consolidated_days():
    rows = []
    for i in range(31):
        day = date(2026, 8, 31) + timedelta(days=i)
        for slot in delivery_slots(day, "30min"):
            rows.append({"value_time": slot, "delivery_day": day,
                         "variant": "B", "point": 46100, "actual": 45000 if i < 30 else None,
                         "rte_d1": 45200,
                         "actual_vintage": "consolidated" if i == 0 else "realtime"})
    rows += [dict(row, variant="A", point=45050) for row in rows]
    data = build_site_payload(replay_source=pd.DataFrame(rows))
    summary = data["header_tiles"]["rolling_28d_mae"]
    assert summary["status"] == "ok"
    assert summary["provisional_days"] == 28
    assert summary["total_days"] == 28
    assert data["rolling_mae_history"][-1]["model_rolling_mae"] == 50
    assert len(data["daily_mae_28d"]) == 28
    assert all(r["actual_vintage"] == "realtime" for r in data["daily_mae_28d"])


@pytest.mark.parametrize("stored", [False, True])
def test_unscored_forecast_awaits_consolidated_actuals(monkeypatch, stored):
    days = [date(2026, 10, 16), date(2026, 10, 17)]
    rows = [{"value_time": slot, "point": 45000}
            for day in days for slot in delivery_slots(day, "30min")]
    if stored:
        monkeypatch.setattr("load_forecast.site_export._load_replay_scores", lambda _: pd.DataFrame([
            {"delivery_day": day, "model_mae": float("nan"), "actual_vintage": "realtime"}
            for day in days
        ]))
    data = build_site_payload(replay_source=pd.DataFrame(rows))
    for tile in ("rolling_28d_mae", "beating_rte"):
        assert data["header_tiles"][tile]["status"] == "awaiting_consolidated"
        assert data["header_tiles"][tile]["total_days"] == 0
        assert data["header_tiles"][tile]["provisional_days"] == 0
    assert data["daily_mae_28d"] == []


@pytest.mark.parametrize("day", [date(2024, 3, 31), date(2024, 10, 27)])
def test_raw_actuals_are_fresh_ranked_and_dst_filtered(tmp_path, monkeypatch, day):
    from load_forecast.site_export import _load_actual_and_rte

    engine = create_engine("sqlite://")
    slots = delivery_slots(day, "30min")
    rows = [{"value_time": slot.isoformat(), "consommation": 45000,
             "prevision_j1": 45200, "source_dataset": "eco2mix-national-tr"}
            for slot in slots]
    install_raw_fixture(engine, monkeypatch, rows)
    with engine.begin() as conn:
        # No clean table exists. New collector rows are enough for this page.
        conn.execute(text("""INSERT INTO raw.eco2mix
            SELECT value_time, 45123, prevision_j1, source_dataset,
                   '2025-01-01T00:00:00+00:00', 'revised', original_values
            FROM raw.eco2mix"""))
        conn.execute(text("""INSERT INTO raw.eco2mix
            SELECT value_time, 99999, prevision_j1, source_dataset,
                   '2099-01-01T00:00:00+00:00', 'future', original_values
            FROM raw.eco2mix WHERE payload_hash = 'first'"""))
        conn.execute(text("""INSERT INTO raw.eco2mix
            SELECT value_time, 88888, prevision_j1, source_dataset,
                   '2025-02-01T00:00:00+00:00', 'bad-clock', '{"heure":"99:99"}'
            FROM raw.eco2mix WHERE payload_hash = 'first'"""))
    monkeypatch.setenv("LFF_CLEAN_TABLE", "missing_stale_snapshot")
    data = _load_actual_and_rte(engine, slots[0], slots[-1])
    assert len(data) == (46 if day.month == 3 else 50)
    assert data.consommation.eq(45123).all()
    assert not data.value_time.duplicated().any()
    assert str(data.value_time.dt.tz) == "UTC"
    engine.dispose()


def test_live_provisional_uses_selected_shadow_and_stored_model_a(fixture_db):
    day = "2024-04-01"
    metrics = {"served": {"mae": 1100, "mean_error": 1100},
               "model_a": {"mae": 123, "mean_error": -123},
               "rte_d1": {"mae": 200, "mean_error": 200}, "n_common_slots": 48}
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset='eco2mix-national-tr'"))
        conn.execute(text("UPDATE \"values\" SET point=point-1000 WHERE variant='A'"))
        conn.execute(text("""INSERT INTO scores VALUES
            (:day, :choice, 'realtime', '2024-04-02', '2024-04-02', :metrics)"""),
            {"day": day, "choice": f"chash_{day}", "metrics": json.dumps(metrics)})
    payload = build_site_payload(engine=fixture_db)
    first = payload["daily_mae_28d"][0]
    assert first["model_mae"] == 123
    assert first["model_mean_error"] == -123
    assert first["comparison_variant"] == "A"
    # No adjusted or reconstructed B value substitutes for the stored A point.
    slot = payload["recent_14d"][0]
    assert slot["forecast_mw"] == 44000
    assert slot["served_forecast_mw"] == 45000
    assert slot["actual_mw"] == 45040
    rendered = render_site(payload)
    assert "My forecast (real-time scale)" in rendered["recentLegend"]
    dotted = next(s for s in rendered["recentSeries"]
                  if s["name"] == "Served forecast (targets consolidated)")
    assert dotted["lineStyle"]["type"] == "dotted"
    assert dotted["lineStyle"]["width"] == 1
    assert payload["header_tiles"]["rolling_28d_mae"]["is_provisional"]


def test_fresh_final_actuals_switch_to_b_before_scores_refresh(fixture_db):
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset='eco2mix-national-tr'"))
        conn.execute(text("""INSERT INTO raw.eco2mix
            SELECT value_time, consommation+1000, prevision_j1,
                   'eco2mix-national-cons-def', ingested_at, 'final', original_values
            FROM raw.eco2mix"""))
        # A stale real-time score must not override newly consolidated slot values.
        conn.execute(text("""INSERT INTO scores VALUES
            ('2024-04-01', 'chash_2024-04-01', 'realtime', '2024-04-02',
             '2024-04-02', :metrics)"""),
            {"metrics": json.dumps({"model_a": {"mae": 12}, "rte_d1": {"mae": 200}})})
    payload = build_site_payload(engine=fixture_db)
    first = payload["daily_mae_28d"][0]
    assert first["model_mae"] == 1040
    assert first["comparison_variant"] == "B"
    assert first["actual_vintage"] == "consolidated"
    slot = payload["recent_14d"][0]
    assert slot["forecast_mw"] == slot["served_forecast_mw"] == 45000
    assert slot["actual_mw"] == 46040
    assert not payload["header_tiles"]["rolling_28d_mae"]["is_provisional"]


def test_missing_shadow_never_compares_b_to_realtime(fixture_db):
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset='eco2mix-national-tr'"))
        conn.execute(text("UPDATE state_runs SET variant='B'"))
    payload = build_site_payload(engine=fixture_db)
    assert payload["header_tiles"]["rolling_28d_mae"]["model_mw"] is None
    assert payload["daily_mae_28d"] == []
    assert all(s["forecast_mw"] is None and s["served_forecast_mw"] is not None
               for s in payload["recent_7d"])


def test_page_shows_collector_actuals_after_stale_clean_snapshot(fixture_db, monkeypatch):
    last_day = date(2024, 4, 14)
    slots = delivery_slots(last_day, "30min")
    with fixture_db.begin() as conn:
        conn.execute(text("""UPDATE raw.eco2mix SET consommation=50000,
            source_dataset='eco2mix-national-tr', ingested_at='2024-04-15T06:00:00+00:00'
            WHERE value_time >= :first"""), {"first": slots[0].isoformat()})
    monkeypatch.setenv("LFF_CLEAN_TABLE", "not_rebuilt_or_used")
    payload = build_site_payload(engine=fixture_db)
    latest = [s for s in payload["recent_7d"] if s["date"] == str(last_day)]
    assert len(latest) == 48
    assert all(s["actual_mw"] == 50000 and s["comparison_variant"] == "A" for s in latest)
    assert payload["daily_mae_28d"][-1]["date"] == str(last_day)
    assert payload["daily_mae_28d"][-1]["model_mae"] == 1500


def test_replay_file_path_loads_both_variants(tmp_path):
    from load_forecast.site_export import _load_replay_data, _load_replay_scores

    for variant, point in [("A", 45000), ("B", 46000)]:
        pd.DataFrame([{"value_time": pd.Timestamp("2026-10-14T00:00:00Z"),
                       "variant": variant, "point": point}]).to_parquet(
                           tmp_path / f"forecasts_{variant}.parquet")
    pd.DataFrame([{"delivery_day": date(2026, 10, 14), "variant": v, "model_mae": 50}
                  for v in ("A", "B")]).to_parquet(tmp_path / "replay_scores.parquet")
    source = tmp_path / "forecasts_B.parquet"
    assert _load_replay_data(source, "A").iloc[0].forecast_mw == 45000
    assert _load_replay_data(source, "B").iloc[0].forecast_mw == 46000
    assert set(_load_replay_scores(source).variant) == {"A", "B"}



def test_shadow_comparison_remains_visible_on_a_none_delivery(fixture_db):
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset='eco2mix-national-tr'"))
        conn.execute(text("""UPDATE choices SET kind='none', run_id=NULL, content_hash=NULL, values_path=NULL
            WHERE delivery_day='2024-04-01'"""))
    payload = build_site_payload(engine=fixture_db)
    first = payload["recent_14d"][0]
    assert first["date"] == "2024-04-01"
    assert first["forecast_mw"] == 45000
    assert first["served_forecast_mw"] is None
    assert first["actual_mw"] == 45040
    assert payload["header_tiles"]["delivery_counts"]["none"] == 1
    assert payload["daily_mae_28d"][0]["model_mae"] == 40


def test_live_selector_updates_tiles_and_chart(fixture_db):
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE run_results SET flags=:flags WHERE run_id=:run"),
                     {"flags": json.dumps({"degraded_inputs": ["stale_consumption"]}),
                      "run": "run_b_2024-04-13"})
    payload = build_site_payload(engine=fixture_db)
    all_live = payload["live_comparisons"]["all_live"]
    clean = payload["live_comparisons"]["excluding_degraded"]
    assert all_live["header_tiles"]["rolling_28d_mae"]["total_days"] == 13
    assert clean["header_tiles"]["rolling_28d_mae"]["total_days"] == 12
    rendered = render_site(payload, "excluding_degraded")
    assert rendered["initialRolling"]["series"][0]["data"] == [
        r["model_rolling_mae"] for r in all_live["history"]]
    assert rendered["rolling"]["series"][0]["data"] == [
        r["model_rolling_mae"] for r in clean["history"]]
    assert "12 days" in rendered["text"]["tile-mae-sub"]
    assert "fresh inputs" in rendered["text"]["comparison-scope"]
    assert rendered["text"]["comparison-scope"] == (
        "Leaves out days served on backup experts or stale inputs, to show the model with fresh inputs.")
    assert "over the 12 clean days of the last 28 (final)" in rendered["text"]["title-rolling-mae"]
    initial = render_site(payload)
    assert "over the 13 live days of the last 28 (final)" in initial["text"]["title-rolling-mae"]


@pytest.mark.parametrize("day", [date(2026, 3, 29), date(2026, 10, 25)])
def test_live_comparison_complete_dst_days_and_calendar_window(day):
    from load_forecast.site_export import _live_comparisons

    def record(day, **changes):
        return {"date": str(day), "delivery_day": day, "kind": "primary",
                "is_replay": False, "n_slots": len(delivery_slots(day, "30min")),
                "actual_vintage": "realtime", "comparison_variant": "A",
                "model_mae": 100.4, "rte_mae": 200.4, "degraded_inputs": [], **changes}

    records = [record(day - timedelta(days=28), model_mae=9999),
               record(day - timedelta(days=2), is_replay=True),
               record(day - timedelta(days=1), n_slots=24),
               record(day, kind="fallback", degraded_inputs=["stale_consumption"]),
               record(day + timedelta(days=1), n_slots=24)]
    result = _live_comparisons(records)
    all_live = result["all_live"]["header_tiles"]["rolling_28d_mae"]
    assert result["all_live"]["history"][-1]["date"] == str(day)
    assert all_live["total_days"] == 1
    assert all_live["model_mw"] == 100
    assert all_live["rte_mw"] == 200
    assert all_live["provisional_days"] == 1
    clean = result["excluding_degraded"]["header_tiles"]["rolling_28d_mae"]
    assert clean["status"] == "awaiting_complete"
    assert clean["model_mw"] is None
    assert clean["total_days"] == 0
    assert _live_comparisons([])["all_live"]["history"] == []


def test_javascript_marks_degraded_daily_bars(fixture_db):
    with fixture_db.begin() as conn:
        conn.execute(text("UPDATE run_results SET flags=:flags WHERE run_id=:run"),
                     {"flags": json.dumps({"degraded_inputs": ["stale_consumption"]}),
                      "run": "run_b_2024-04-13"})
        conn.execute(text("UPDATE raw.eco2mix SET source_dataset='eco2mix-national-tr'"))
    payload = build_site_payload(engine=fixture_db)
    rendered = render_site(payload)
    legend = next(item for item in rendered["dailyLegend"]
                  if isinstance(item, dict) and item["name"] == "degraded inputs")
    assert legend["itemStyle"]["borderWidth"] == 2
    for series in rendered["dailySeries"][:2]:
        for bar in series["data"]:
            style = bar["itemStyle"]
            assert style["borderWidth"] == (2 if bar["date"] == "2024-04-13" else 0)
            if bar["date"] == "2024-04-13":
                assert style["borderColor"] == legend["itemStyle"]["borderColor"]
                assert style["decal"] is not None
    assert "provisional" in rendered["text"]["title-rolling-mae"]
