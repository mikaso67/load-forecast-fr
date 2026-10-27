"""Generate a verification screenshot of the Streamlit dashboard on a fixture database."""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import requests
import websockets
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from load_forecast.cutoff import PARIS, delivery_slots, forecast_cutoff  # noqa: E402


def setup_fixture_db(db_path: Path) -> str:
    if db_path.exists():
        db_path.unlink()
    url = f"sqlite:///{db_path}"
    engine = create_engine(url)

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
        conn.execute(text("DROP TABLE IF EXISTS state_runs"))
        conn.execute(text("""
            CREATE TABLE state_runs (
                delivery_day DATE NOT NULL,
                variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
                run_id TEXT NOT NULL UNIQUE,
                PRIMARY KEY (delivery_day, variant)
            )
        """))

    days = [date(2024, 4, 1) + timedelta(days=i) for i in range(14)]
    clean_rows = []

    with engine.begin() as conn:
        for idx, day in enumerate(days):
            freeze = forecast_cutoff(day)
            run_id_b = f"run_b_{day.isoformat()}"
            run_id_a = f"run_a_{day.isoformat()}"
            is_fallback = (idx == 6)

            # Variant B run
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

            # Variant A run (shadow)
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
            for s_idx, slot in enumerate(slots):
                slot_iso = slot.isoformat()
                local_slot = slot.astimezone(PARIS).hour * 2 + slot.astimezone(PARIS).minute // 30
                base_load = 42000.0 + 4000.0 * (s_idx / 48.0) + (idx % 7) * 500.0
                pt_b = base_load
                pt_a = base_load - 250.0 + (50.0 if s_idx % 2 == 0 else -50.0)

                # Variant B values
                conn.execute(text("""
                    INSERT INTO "values" (run_id, variant, value_time, local_slot, point, lower, upper, n_pool)
                    VALUES (:run_id, :variant, :value_time, :local_slot, :point, :lower, :upper, :n_pool)
                """), {
                    "run_id": run_id_b, "variant": "B", "value_time": slot_iso, "local_slot": local_slot,
                    "point": pt_b, "lower": pt_b - 400.0, "upper": pt_b + 400.0, "n_pool": 60,
                })

                # Variant A values
                conn.execute(text("""
                    INSERT INTO "values" (run_id, variant, value_time, local_slot, point, lower, upper, n_pool)
                    VALUES (:run_id, :variant, :value_time, :local_slot, :point, :lower, :upper, :n_pool)
                """), {
                    "run_id": run_id_a, "variant": "A", "value_time": slot_iso, "local_slot": local_slot,
                    "point": pt_a, "lower": pt_a - 380.0, "upper": pt_a + 380.0, "n_pool": 60,
                })

                clean_rows.append({
                    "value_time": slot_iso,
                    "consommation": pt_b + (30.0 if s_idx % 2 == 0 else -40.0),
                    "prevision_j1": pt_b - 150.0,
                })

            # State runs for both variants
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
                    "models": ["EXP-002 calendar OLS"], "completeness": {"complete": True},
                    "values": [{"value_time": s.isoformat(),
                                "local_slot": s.astimezone(PARIS).hour * 2 + s.astimezone(PARIS).minute // 30,
                                "point": 41000.0} for s in slots],
                }
                conn.execute(text("""
                    INSERT INTO fallbacks (fallback_id, delivery_day, prepared_at, models, completeness, content_hash, payload)
                    VALUES (:fallback_id, :delivery_day, :prepared_at, :models, :completeness, :content_hash, :payload)
                """), {
                    "fallback_id": f"fb_{run_id_b}", "delivery_day": day.isoformat(),
                    "prepared_at": (freeze - timedelta(hours=23)).isoformat(),
                    "models": '["EXP-002 calendar OLS"]', "completeness": '{"complete": true}',
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

    engine.dispose()
    return url


def main():
    tmp_dir = ROOT / "data/scratch"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    db_path = tmp_dir / "fixture_dashboard.sqlite"
    db_url = setup_fixture_db(db_path)
    output_png = ROOT / "docs/figures/dashboard_fixture.png"

    env = os.environ.copy()
    env["LFF_DATABASE_URL"] = db_url
    env["LFF_CLEAN_TABLE"] = "clean_load_30min"

    port = 8503
    server_url = f"http://127.0.0.1:{port}"

    python_exe = sys.executable
    cmd = [
        python_exe, "-m", "streamlit", "run",
        str(ROOT / "dashboard/app.py"),
        "--server.port", str(port),
        "--server.headless", "true",
    ]

    env["PYTHONPATH"] = str(ROOT / "src")

    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    try:
        started = False
        for _ in range(30):
            time.sleep(1)
            try:
                resp = requests.get(server_url, timeout=2)
                if resp.status_code == 200:
                    started = True
                    break
            except Exception:
                pass

        if not started:
            raise RuntimeError("Streamlit server failed to start within timeout.")

        chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        user_data_dir = tempfile.mkdtemp(prefix="chrome_cdp_")
        cdp_port = 9225

        chrome_proc = subprocess.Popen([
            chrome_path,
            "--headless=new",
            "--disable-gpu",
            f"--user-data-dir={user_data_dir}",
            f"--remote-debugging-port={cdp_port}",
            "--window-size=1400,2200",
            "about:blank",
        ])

        try:
            ws_url = None
            for _ in range(20):
                time.sleep(0.5)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json") as resp:
                        targets = json.loads(resp.read().decode())
                        page_target = next((t for t in targets if t.get("type") == "page"), None)
                        if page_target and "webSocketDebuggerUrl" in page_target:
                            ws_url = page_target["webSocketDebuggerUrl"]
                            break
                except Exception:
                    pass

            if not ws_url:
                raise RuntimeError("Failed to connect to Chrome DevTools Protocol page endpoint.")

            async def do_screenshot():
                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": server_url}}))

                    for i in range(25):
                        await asyncio.sleep(1)
                        eval_req = {
                            "id": 100 + i,
                            "method": "Runtime.evaluate",
                            "params": {
                                "expression": (
                                    "document.querySelectorAll('[data-testid=\"stMetricValue\"]').length >= 3 && "
                                    "document.querySelectorAll('img').length >= 2"
                                ),
                                "returnByValue": True,
                            },
                        }
                        await ws.send(json.dumps(eval_req))
                        while True:
                            reply = json.loads(await ws.recv())
                            if reply.get("id") == 100 + i:
                                val = reply.get("result", {}).get("result", {}).get("value", False)
                                if val:
                                    await asyncio.sleep(2)
                                    await ws.send(json.dumps({
                                        "id": 200,
                                        "method": "Page.captureScreenshot",
                                        "params": {"format": "png", "captureBeyondViewport": True},
                                    }))
                                    while True:
                                        cap_reply = json.loads(await ws.recv())
                                        if cap_reply.get("id") == 200:
                                            img_bytes = base64.b64decode(cap_reply["result"]["data"])
                                            output_png.write_bytes(img_bytes)
                                            return
                                break

                    # Fallback capture if specific selectors timeout
                    await ws.send(json.dumps({"id": 300, "method": "Page.captureScreenshot", "params": {"format": "png"}}))
                    while True:
                        cap_reply = json.loads(await ws.recv())
                        if cap_reply.get("id") == 300:
                            img_bytes = base64.b64decode(cap_reply["result"]["data"])
                            output_png.write_bytes(img_bytes)
                            return

            asyncio.run(do_screenshot())

        finally:
            chrome_proc.terminate()
            try:
                chrome_proc.wait(timeout=5)
            except Exception:
                chrome_proc.kill()
            shutil.rmtree(user_data_dir, ignore_errors=True)

        if not (output_png.exists() and output_png.stat().st_size > 0):
            raise RuntimeError(f"Screenshot file {output_png} was not created.")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
