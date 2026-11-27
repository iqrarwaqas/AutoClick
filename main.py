"""AutoClick — Desktop Automation Studio (Record & Replay, Auto Clicker, Multi-Step Sequences).

A small Windows desktop app with three tools:
- Record & Replay: cool-off countdown to set up your scene, live recording of
  clicks and key presses (including combos and hold depth), and replay for N iterations.
- Auto Clicker: fast single-point, cursor, or multi-point clicking with hold depth,
  intervals, and cool-off countdown.
- Sequence Builder: multi-step keyboard & mouse sequences with nested repeat
  groups (multi-step depth), key hold duration, and saved presets (including the
  v0.5 Copy Tab URLs preset).

The UI is a local Flask app shown in a native window (pywebview / Edge WebView2).

Usage:
    python main.py            # open in a native desktop window
    python main.py --browser  # open in the web browser instead of a window
    python main.py --port 5050
"""

from __future__ import annotations

import argparse
import copy
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import webbrowser
from pathlib import Path

if sys.platform == "win32":
    try:
        if not ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        pass

from common import APP_DATA, FROZEN, RESOURCE_DIR, load_config, save_config, start_again_later

try:
    from flask import Flask, abort, jsonify, render_template, request
    from werkzeug.serving import make_server

    import engine
    import recorder as rec_mod
except ImportError:
    start_again_later()  # just after an update the new files can still be locked
    raise

__version__ = "1.0.0"
GITHUB_REPO = "iqrarwaqas/AutoClick"

# Where the app window keeps its local storage (theme and UI preferences).
WEBVIEW_STORAGE = APP_DATA / "WebView"
DEFAULT_PORT = 5050

app = Flask(__name__, template_folder=str(RESOURCE_DIR / "templates"), static_folder=str(RESOURCE_DIR / "static"))
store_lock = threading.Lock()
window = None  # the pywebview window, when running as a desktop app


def _minimize_window():
    if window is not None:
        window.minimize()


def _restore_window():
    if window is not None:
        window.restore()
        window.show()


runner = engine.SequenceRunner(on_minimize=_minimize_window, on_restore=_restore_window)
recorder = rec_mod.InputRecorder(on_minimize=_minimize_window, on_restore=_restore_window)


# ---------------------------------------------------------------- config & presets


def default_settings() -> dict:
    return {
        "stop_key": "f8",
        "record_stop_key": "f9",
        "failsafe": True,
        "minimize_on_start": False,
        "speed": 1.0,
    }


def load_store() -> dict:
    """Return normalized presets, active_preset_id, and global run settings."""
    cfg = load_config()
    raw_presets = cfg.get("presets")
    if not isinstance(raw_presets, list) or not raw_presets:
        presets = engine.default_presets()
    else:
        presets = [engine.normalize_preset(p) for p in raw_presets if isinstance(p, dict)]
        if not presets:
            presets = engine.default_presets()

    active_id = cfg.get("active_preset_id")
    if not any(p["id"] == active_id for p in presets):
        active_id = presets[0]["id"]

    raw_settings = cfg.get("settings") if isinstance(cfg.get("settings"), dict) else {}
    settings = {**default_settings(), **raw_settings}
    for key_field, fallback in (("stop_key", "f8"), ("record_stop_key", "f9")):
        val = str(settings.get(key_field, fallback)).lower()
        settings[key_field] = val if val in engine.STOP_KEYS else fallback
    settings["failsafe"] = bool(settings.get("failsafe", True))
    settings["minimize_on_start"] = bool(settings.get("minimize_on_start", False))
    try:
        settings["speed"] = max(0.1, min(5.0, float(settings.get("speed", 1.0))))
    except (TypeError, ValueError):
        settings["speed"] = 1.0

    return {"presets": presets, "active_preset_id": active_id, "settings": settings}


def persist_store(store: dict):
    cfg = load_config()
    cfg["presets"] = store["presets"]
    cfg["active_preset_id"] = store["active_preset_id"]
    cfg["settings"] = store["settings"]
    save_config(cfg)


# ---------------------------------------------------------------- updates

_update_info: dict | None = None
_update_checked_at: float = 0.0
UPDATE_CACHE_TTL = 1800.0  # re-check every 30 minutes for long-running sessions


def parse_version(tag: str) -> tuple[int, ...]:
    nums = [int(n) for n in re.findall(r"\d+", tag)[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def github_get(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": f"AutoClick/{__version__}"})
    return urllib.request.urlopen(req, timeout=timeout)


def check_update(force: bool = False) -> dict:
    """Compare this version with the latest GitHub release (cached unless forced or expired)."""
    global _update_info, _update_checked_at
    if _update_info is None or force or (time.monotonic() - _update_checked_at) >= UPDATE_CACHE_TTL:
        info = {"current": __version__, "available": False}
        try:
            with github_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest", timeout=5) as r:
                release = json.load(r)
            installer = next(
                (a["browser_download_url"] for a in release.get("assets", []) if a["name"].lower().endswith(".exe")),
                None,
            )
            info.update(
                latest=release["tag_name"].lstrip("v"),
                url=release["html_url"],
                installer=installer,
                available=parse_version(release["tag_name"]) > parse_version(__version__),
            )
        except Exception:  # noqa: BLE001 - offline or no releases yet: just don't offer an update
            info["error"] = "Couldn't check for updates. Check your internet connection and try again."
        _update_info = info
        _update_checked_at = time.monotonic()
    return {**_update_info, "can_install": FROZEN and bool(_update_info.get("installer"))}


def quit_app():
    recorder.stop()
    runner.stop("App closed.")
    os._exit(0)


# ---------------------------------------------------------------- routes


def is_local() -> bool:
    return request.remote_addr in ("127.0.0.1", "::1")


@app.get("/")
def index():
    return render_template("index.html", version=__version__)


@app.get("/api/version")
def api_version():
    return jsonify({"app": "AutoClick", "version": __version__})


@app.post("/api/focus")
def api_focus():
    """Called by a second copy of the app: bring this window to the front."""
    if window is None:
        return jsonify({"ok": True, "window": False})
    window.restore()
    window.show()
    window.on_top = True  # Windows blocks focus stealing, so raise the window this way
    window.on_top = False
    return jsonify({"ok": True, "window": True})


@app.get("/api/update")
def api_update():
    return jsonify(check_update(force=request.args.get("force") == "1"))


@app.post("/api/update/install")
def api_update_install():
    info = check_update()
    if not (info["available"] and info["can_install"]):
        return jsonify({"ok": False, "error": "There is no update to install."}), 400
    target = Path(tempfile.gettempdir()) / Path(info["installer"]).name
    try:
        with github_get(info["installer"], timeout=60) as r, open(target, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Download failed: {e}"}), 502
    # The installer replaces the app's files, so this process has to exit.
    # /RELAUNCH=1 makes the installer start the new version when it's done.
    subprocess.Popen([str(target), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1"])
    threading.Timer(1.0, quit_app).start()
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state():
    with store_lock:
        store = load_store()
    return jsonify({
        "ok": True,
        "version": __version__,
        "presets": store["presets"],
        "active_preset_id": store["active_preset_id"],
        "settings": store["settings"],
        "runner": runner.state(),
        "recorder": recorder.state(),
    })


@app.get("/api/run/status")
def api_run_status():
    return jsonify({"ok": True, "runner": runner.state(), "recorder": recorder.state()})


# ---------------------------------------------------------------- presets


@app.post("/api/presets/select")
def api_presets_select():
    data = request.get_json(force=True)
    preset_id = str(data.get("id") or "")
    with store_lock:
        store = load_store()
        if not any(p["id"] == preset_id for p in store["presets"]):
            return jsonify({"ok": False, "error": "Sequence not found."}), 404
        store["active_preset_id"] = preset_id
        persist_store(store)
    return jsonify({"ok": True, "active_preset_id": preset_id})


@app.post("/api/presets/save")
def api_presets_save():
    data = request.get_json(force=True)
    raw = data.get("preset")
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "error": "Invalid sequence payload."}), 400
    try:
        preset = engine.normalize_preset(raw)
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Invalid step or value: {e}"}), 400

    with store_lock:
        store = load_store()
        replaced = False
        for i, existing in enumerate(store["presets"]):
            if existing["id"] == preset["id"]:
                store["presets"][i] = preset
                replaced = True
                break
        if not replaced:
            store["presets"].append(preset)
        store["active_preset_id"] = preset["id"]
        persist_store(store)
    runner.update_live_preset(preset)
    return jsonify({"ok": True, "preset": preset, "presets": store["presets"], "active_preset_id": store["active_preset_id"]})


@app.post("/api/presets/create")
def api_presets_create():
    data = request.get_json(force=True) or {}
    name = str(data.get("name") or "New Sequence").strip() or "New Sequence"
    clone_from = data.get("clone_from")
    raw_steps = data.get("steps")
    with store_lock:
        store = load_store()
        source = next((p for p in store["presets"] if p["id"] == clone_from), None) if clone_from else None
        if source:
            new_preset = copy.deepcopy(source)
            new_preset["id"] = uuid.uuid4().hex[:8]
            new_preset["name"] = name
            for step in new_preset["steps"]:
                step["id"] = uuid.uuid4().hex[:8]
        else:
            new_preset = engine.normalize_preset({
                "id": uuid.uuid4().hex[:8],
                "name": name,
                "description": str(data.get("description") or ""),
                "start_delay": float(data.get("start_delay", 5.0)),
                "iterations": int(data.get("iterations", 10)),
                "infinite": bool(data.get("infinite", False)),
                "steps": raw_steps if isinstance(raw_steps, list) and raw_steps else [
                    {"action": "wait", "seconds": 0.5, "enabled": True, "note": ""},
                ],
            })
        store["presets"].append(new_preset)
        store["active_preset_id"] = new_preset["id"]
        persist_store(store)
    return jsonify({"ok": True, "preset": new_preset, "presets": store["presets"], "active_preset_id": store["active_preset_id"]})


@app.delete("/api/presets/<preset_id>")
def api_presets_delete(preset_id: str):
    with store_lock:
        store = load_store()
        if len(store["presets"]) <= 1:
            return jsonify({"ok": False, "error": "At least one sequence must remain."}), 400
        remaining = [p for p in store["presets"] if p["id"] != preset_id]
        if len(remaining) == len(store["presets"]):
            return jsonify({"ok": False, "error": "Sequence not found."}), 404
        store["presets"] = remaining
        if store["active_preset_id"] == preset_id:
            store["active_preset_id"] = remaining[0]["id"]
        persist_store(store)
    return jsonify({"ok": True, "presets": store["presets"], "active_preset_id": store["active_preset_id"]})


@app.post("/api/presets/reset")
def api_presets_reset():
    """Restore the built-in Copy Tab URLs (v0.5) preset."""
    default = engine.default_presets()[0]
    with store_lock:
        store = load_store()
        found = False
        for i, p in enumerate(store["presets"]):
            if p["id"] == default["id"]:
                store["presets"][i] = default
                found = True
                break
        if not found:
            store["presets"].insert(0, default)
        store["active_preset_id"] = default["id"]
        persist_store(store)
    return jsonify({"ok": True, "presets": store["presets"], "active_preset_id": store["active_preset_id"]})


@app.post("/api/presets/import")
def api_presets_import():
    data = request.get_json(force=True)
    incoming = data.get("presets")
    if isinstance(incoming, dict):
        incoming = [incoming]
    if not isinstance(incoming, list) or not incoming:
        return jsonify({"ok": False, "error": "Expected a preset object or list of presets."}), 400
    try:
        normalized = [engine.normalize_preset(p) for p in incoming if isinstance(p, dict)]
    except (TypeError, ValueError) as e:
        return jsonify({"ok": False, "error": f"Invalid preset data: {e}"}), 400
    if not normalized:
        return jsonify({"ok": False, "error": "No valid presets found in file."}), 400

    with store_lock:
        store = load_store()
        existing_ids = {p["id"] for p in store["presets"]}
        for p in normalized:
            if p["id"] in existing_ids:
                p["id"] = uuid.uuid4().hex[:8]
            existing_ids.add(p["id"])
            store["presets"].append(p)
        store["active_preset_id"] = normalized[-1]["id"]
        persist_store(store)
    return jsonify({"ok": True, "imported": len(normalized), "presets": store["presets"], "active_preset_id": store["active_preset_id"]})


@app.post("/api/settings")
def api_settings():
    data = request.get_json(force=True)
    with store_lock:
        store = load_store()
        settings = store["settings"]
        for key_field in ("stop_key", "record_stop_key"):
            if key_field in data:
                key = str(data[key_field]).strip().lower()
                if key in engine.STOP_KEYS:
                    settings[key_field] = key
        if "failsafe" in data:
            settings["failsafe"] = bool(data["failsafe"])
        if "minimize_on_start" in data:
            settings["minimize_on_start"] = bool(data["minimize_on_start"])
        if "speed" in data:
            try:
                settings["speed"] = max(0.1, min(5.0, float(data["speed"])))
            except (TypeError, ValueError):
                pass
        persist_store(store)
    return jsonify({"ok": True, "settings": store["settings"]})


# ---------------------------------------------------------------- recorder


@app.post("/api/record/start")
def api_record_start():
    if not is_local():
        abort(403)
    if runner.is_active():
        return jsonify({"ok": False, "error": "Stop the running task before recording."}), 409
    data = request.get_json(silent=True) or {}
    with store_lock:
        settings = load_store()["settings"]
    mode = str(data.get("mode") or "both").lower()
    cooloff = float(data.get("cooloff", 5.0))
    stop_key = str(data.get("stop_key") or settings["record_stop_key"]).lower()
    stop_vk = engine.STOP_KEYS.get(stop_key, engine.STOP_KEYS["f9"])
    capture_delays = bool(data.get("capture_delays", True))
    minimize_on_start = bool(data.get("minimize_on_start", settings["minimize_on_start"]))

    try:
        recorder.start(
            mode=mode,
            cooloff=cooloff,
            stop_key=stop_key,
            stop_vk=stop_vk,
            capture_delays=capture_delays,
            minimize_on_start=minimize_on_start,
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "recorder": recorder.state()})


@app.post("/api/record/stop")
def api_record_stop():
    if not is_local():
        abort(403)
    recorder.stop(wait=True)
    return jsonify({"ok": True, "recorder": recorder.state()})


@app.post("/api/record/clear")
def api_record_clear():
    if not is_local():
        abort(403)
    data = request.get_json(silent=True) or {}
    if data.get("logs_only"):
        with recorder._lock:
            recorder._logs.clear()
    else:
        recorder.clear()
    return jsonify({"ok": True, "recorder": recorder.state()})


# ---------------------------------------------------------------- runner


@app.post("/api/run/start")
def api_run_start():
    if not is_local():
        abort(403)
    if recorder.is_active():
        return jsonify({"ok": False, "error": "Stop recording before running a sequence."}), 409
    data = request.get_json(silent=True) or {}
    save_changes = bool(data.get("save", True))
    with store_lock:
        store = load_store()
        raw_preset = data.get("preset")
        if isinstance(raw_preset, dict):
            preset = engine.normalize_preset(raw_preset)
            if save_changes:
                for i, existing in enumerate(store["presets"]):
                    if existing["id"] == preset["id"]:
                        store["presets"][i] = preset
                        store["active_preset_id"] = preset["id"]
                        persist_store(store)
                        break
        else:
            preset = next((p for p in store["presets"] if p["id"] == store["active_preset_id"]), store["presets"][0])
        settings = dict(store["settings"])

    try:
        runner.start(
            preset,
            stop_key=str(data.get("stop_key") or settings["stop_key"]),
            failsafe=bool(data.get("failsafe", settings["failsafe"])),
            minimize_on_start=bool(data.get("minimize_on_start", settings["minimize_on_start"])),
            speed=float(data.get("speed", settings["speed"])),
        )
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "runner": runner.state()})


@app.post("/api/run/pause")
def api_run_pause():
    if not is_local():
        abort(403)
    runner.pause()
    return jsonify({"ok": True, "runner": runner.state()})


@app.post("/api/run/resume")
def api_run_resume():
    if not is_local():
        abort(403)
    runner.resume()
    return jsonify({"ok": True, "runner": runner.state()})


@app.post("/api/run/stop")
def api_run_stop():
    if not is_local():
        abort(403)
    runner.stop("Stopped by user.")
    return jsonify({"ok": True, "runner": runner.state()})


@app.post("/api/run/clear-log")
def api_run_clear_log():
    runner.clear_logs()
    return jsonify({"ok": True, "runner": runner.state()})


@app.post("/api/cursor/capture")
def api_cursor_capture():
    """Wait N seconds (default 3) then return the current mouse coordinates."""
    if not is_local():
        abort(403)
    data = request.get_json(silent=True) or {}
    delay = max(0.0, min(10.0, float(data.get("delay", 3.0))))
    if delay > 0:
        time.sleep(delay)
    x, y = engine.get_cursor_position()
    return jsonify({"ok": True, "x": x, "y": y})


# ---------------------------------------------------------------- entry


class DesktopApi:
    """Functions the page can call as window.pywebview.api.<name>()."""

    def set_dark_title_bar(self, dark: bool):
        """Match the Windows title bar to the theme picked in the app."""
        try:
            import ctypes

            hwnd = window.native.Handle.ToInt32()
            value = ctypes.c_int(1 if dark else 0)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), 4)  # DWMWA_USE_IMMERSIVE_DARK_MODE
            ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0037)
        except Exception:  # noqa: BLE001 - cosmetic only
            pass


def system_uses_dark_theme() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except Exception:  # noqa: BLE001
        return False


def already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1) as r:
            return json.load(r).get("app") == "AutoClick"
    except Exception:  # noqa: BLE001
        return False


def focus_running(port: int) -> bool:
    """Ask the running copy to show its window. False if it has none (browser mode)."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/focus", method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r).get("window", False)
    except Exception:  # noqa: BLE001
        return False


def port_is_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1" if host == "0.0.0.0" else host, port)) != 0


def run_desktop(url: str) -> bool:
    """Show the app in a native window until it is closed. False if no window could be created."""
    global window
    try:
        import webview
    except ImportError:
        return False
    window = webview.create_window(
        "AutoClick",
        url,
        js_api=DesktopApi(),
        width=1240,
        height=820,
        min_size=(920, 620),
        background_color="#0e0e0e" if system_uses_dark_theme() else "#d4d4d4",
    )
    try:
        # private_mode=False keeps the theme and UI preferences between runs.
        webview.start(private_mode=False, storage_path=str(WEBVIEW_STORAGE))
    except Exception:  # noqa: BLE001 - e.g. the WebView2 runtime is missing
        window = None
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="AutoClick — Record & Replay, Auto Clicker, and Multi-Step Sequences.")
    parser.add_argument("--version", action="version", version=f"AutoClick {__version__}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--browser", action="store_true", help="Open in the web browser instead of an app window")
    parser.add_argument("--no-browser", action="store_true", help="Only run the server (implies --browser)")
    args = parser.parse_args()
    browser_mode = args.browser or args.no_browser

    if already_running(args.port):
        url = f"http://127.0.0.1:{args.port}"
        print(f"AutoClick is already running: {url}")
        if not focus_running(args.port) and not args.no_browser:
            webbrowser.open(url)
        return

    print(f"AutoClick {__version__}")
    port = args.port if port_is_free(args.host, args.port) else 0
    server = make_server(args.host, port, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Open   : {url}")

    if not browser_mode:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        if run_desktop(url):
            quit_app()
        server.shutdown()
    print("Keep this window open while using AutoClick. Close it to quit.")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
