"""Generate verification screenshots of the public static site.

Renders responsive desktop (1280 px) and mobile (375 px) screenshots from real pre-production
replay files in C:/load-forecast-replay/replay/ (read-only), plus an empty-state screenshot.
All intermediate site assets and data payloads are served from a temporary directory.
"""

import asyncio
import base64
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from load_forecast.site_export import (  # noqa: E402
    build_site_payload,
    get_reader_engine,
)


def capture_screenshot_cdp(
    server_url: str,
    output_path: Path,
    width: int,
    height: int,
    wait_expr: str = "document.querySelectorAll('canvas').length >= 4",
):
    chrome_path = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    user_data_dir = tempfile.mkdtemp(prefix="chrome_site_cdp_")
    cdp_port = 9226 if width == 1280 else 9227

    chrome_proc = subprocess.Popen([
        chrome_path,
        "--headless=new",
        "--disable-gpu",
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={cdp_port}",
        f"--window-size={width},{height}",
        "about:blank",
    ])

    try:
        ws_url = None
        for _ in range(25):
            time.sleep(0.4)
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
            raise RuntimeError("Failed to connect to Chrome DevTools Protocol endpoint.")

        async def do_capture():
            async with websockets.connect(ws_url) as ws:
                # Headless Chrome's minimum window size can exceed a phone viewport.
                await ws.send(json.dumps({
                    "id": 0, "method": "Emulation.setDeviceMetricsOverride",
                    "params": {"width": width, "height": height,
                               "deviceScaleFactor": 1, "mobile": False},
                }))
                await ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": server_url}}))

                # Wait for expression to be truthy
                for i in range(25):
                    await asyncio.sleep(1)
                    eval_req = {
                        "id": 100 + i,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": wait_expr,
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
                                await ws.send(json.dumps({"id": 199, "method": "Page.getLayoutMetrics"}))
                                while True:
                                    metrics_reply = json.loads(await ws.recv())
                                    if metrics_reply.get("id") == 199:
                                        metrics = metrics_reply["result"]
                                        content_height = metrics["cssContentSize"]["height"]
                                        break
                                await ws.send(json.dumps({
                                    "id": 200,
                                    "method": "Page.captureScreenshot",
                                    "params": {"format": "png", "captureBeyondViewport": True,
                                               "clip": {"x": 0, "y": 0, "width": width,
                                                        "height": content_height, "scale": 1}},
                                }))
                                while True:
                                    cap_reply = json.loads(await ws.recv())
                                    if cap_reply.get("id") == 200:
                                        img_bytes = base64.b64decode(cap_reply["result"]["data"])
                                        output_path.write_bytes(img_bytes)
                                        return
                            break

                # Fallback capture if wait expression timed out
                await ws.send(json.dumps({
                    "id": 300,
                    "method": "Page.captureScreenshot",
                    "params": {"format": "png"},
                }))
                while True:
                    cap_reply = json.loads(await ws.recv())
                    if cap_reply.get("id") == 300:
                        img_bytes = base64.b64decode(cap_reply["result"]["data"])
                        output_path.write_bytes(img_bytes)
                        return

        asyncio.run(do_capture())

    finally:
        chrome_proc.terminate()
        try:
            chrome_proc.wait(timeout=5)
        except Exception:
            chrome_proc.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)


def setup_temp_site_dir(temp_dir: Path, payload: dict) -> Path:
    """Copy site assets and write payload to temporary directory."""
    site_src = ROOT / "site"
    for item in ["index.html", "styles.css", "app.js"]:
        shutil.copy2(site_src / item, temp_dir / item)

    vendor_src = site_src / "vendor"
    vendor_dst = temp_dir / "vendor"
    if vendor_src.exists():
        shutil.copytree(vendor_src, vendor_dst, dirs_exist_ok=True)

    data_dir = temp_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / "latest.json").open("w", encoding="utf-8") as f:
        json.dump(payload, f, separators=(",", ":"), ensure_ascii=False)

    return temp_dir


def main():
    replay_dir = Path("C:/load-forecast-replay/replay")
    if not replay_dir.exists():
        raise FileNotFoundError(f"Replay directory not found: {replay_dir}")

    figures_dir = ROOT / "docs/figures"
    figures_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clean up obsolete root docs/site_*.png if present
    for obsolete in (ROOT / "docs").glob("site_*.png"):
        try:
            obsolete.unlink()
            print(f"Removed obsolete root screenshot: {obsolete}")
        except Exception as exc:
            print(f"Could not remove {obsolete}: {exc}")

    # 2. Build payload from real replay files
    engine = None
    try:
        candidate_engine = get_reader_engine()
        with candidate_engine.connect():
            pass
        engine = candidate_engine
    except Exception as exc:
        print(f"Note: database engine unavailable ({exc}), reading replay files directly.")

    print(f"Building payload from real replay files in {replay_dir} (read only)...")
    replay_payload = build_site_payload(
        engine=engine,
        replay_source=replay_dir,
        days_back=90,
    )
    print(f"Replay payload built: status={replay_payload['status']}, "
          f"tomorrow={replay_payload.get('tomorrow', {}).get('delivery_day') if replay_payload.get('tomorrow') else None}")

    # 3. Serve real replay payload from a temporary folder and capture screenshots
    with tempfile.TemporaryDirectory(prefix="site_replay_") as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        setup_temp_site_dir(tmp_dir, replay_payload)

        port = 8089
        server_cmd = [sys.executable, "-m", "http.server", str(port), "--directory", str(tmp_dir)]
        server_proc = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server_url = f"http://127.0.0.1:{port}/index.html"
        time.sleep(1.5)

        try:
            # Desktop screenshot at 1280 px
            out_desktop = figures_dir / "site_1280px.png"
            print(f"Capturing desktop screenshot (1280 px) to {out_desktop}...")
            capture_screenshot_cdp(
                server_url,
                out_desktop,
                width=1280,
                height=2200,
                wait_expr="document.querySelectorAll('canvas').length >= 4",
            )
            print(f"Captured {out_desktop} ({out_desktop.stat().st_size} bytes)")

            # Mobile screenshot at 375 px
            out_mobile = figures_dir / "site_375px.png"
            print(f"Capturing mobile screenshot (375 px) to {out_mobile}...")
            capture_screenshot_cdp(
                server_url,
                out_mobile,
                width=375,
                height=2800,
                wait_expr="document.querySelectorAll('canvas').length >= 4",
            )
            print(f"Captured {out_mobile} ({out_mobile.stat().st_size} bytes)")
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except Exception:
                server_proc.kill()

    # 4. Build empty-state payload and capture empty-state screenshot
    print("Building empty-state payload...")
    empty_payload = build_site_payload(engine=None, replay_source=None)

    with tempfile.TemporaryDirectory(prefix="site_empty_") as tmp_empty_str:
        tmp_empty = Path(tmp_empty_str)
        setup_temp_site_dir(tmp_empty, empty_payload)

        port = 8090
        server_cmd = [sys.executable, "-m", "http.server", str(port), "--directory", str(tmp_empty)]
        server_proc = subprocess.Popen(server_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        server_url = f"http://127.0.0.1:{port}/index.html"
        time.sleep(1.5)

        try:
            out_empty = figures_dir / "site_empty_1280px.png"
            print(f"Capturing empty-state screenshot to {out_empty}...")
            capture_screenshot_cdp(
                server_url,
                out_empty,
                width=1280,
                height=900,
                wait_expr="document.getElementById('empty-state').style.display !== 'none'",
            )
            print(f"Captured {out_empty} ({out_empty.stat().st_size} bytes)")
        finally:
            server_proc.terminate()
            try:
                server_proc.wait(timeout=5)
            except Exception:
                server_proc.kill()

    print("Screenshots generation complete.")


if __name__ == "__main__":
    main()
