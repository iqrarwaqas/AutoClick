"""Comprehensive unit tests for AutoClick Flask API routes and packaging/release integrity."""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import common
import engine
import main
import recorder as rec_mod


class _DummyFailSafeException(Exception):
    pass


def _make_mock_pyautogui() -> mock.MagicMock:
    pg = mock.MagicMock(name="pyautogui")
    pg.FailSafeException = _DummyFailSafeException
    pg.FAILSAFE = True
    pg.PAUSE = 0.0
    pos = mock.MagicMock()
    pos.x = 100
    pos.y = 200
    pg.position.return_value = pos
    return pg


def _make_json_response(payload: dict) -> io.BytesIO:
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class BaseApiTestCase(unittest.TestCase):
    """Base test case providing isolated config storage, mocked GUI/network, and clean runner/recorder state."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory(prefix="autoclick_api_test_")
        self.config_path = Path(self._tmpdir.name) / "config.json"

        self._patches = [
            mock.patch.object(common, "CONFIG_FILE", self.config_path),
            mock.patch.object(engine, "pyautogui", _make_mock_pyautogui()),
            mock.patch.object(engine, "is_vk_pressed", return_value=False),
            mock.patch.object(engine, "get_cursor_position", return_value=(420, 310)),
            mock.patch.object(main, "github_get", side_effect=AssertionError("Unexpected real network call")),
        ]
        if sys.platform == "win32" and hasattr(rec_mod, "_user32"):
            self._patches.append(mock.patch.object(rec_mod._user32, "GetAsyncKeyState", return_value=0))

        for p in self._patches:
            p.start()

        main._update_info = None
        main.window = None
        self._reset_background_workers()

        main.app.config["TESTING"] = True
        self.client = main.app.test_client()

    def tearDown(self):
        self._reset_background_workers()
        main._update_info = None
        main.window = None
        for p in reversed(self._patches):
            p.stop()
        self._tmpdir.cleanup()

    def _reset_background_workers(self):
        main.runner.stop("Test cleanup")
        if main.runner._thread and main.runner._thread.is_alive():
            main.runner._thread.join(timeout=2.0)
        with main.runner._lock:
            main.runner._status = "idle"
            main.runner._stop_reason = ""
            main.runner._error = ""
            main.runner._live_preset_id = None
            main.runner._live_steps = None
        main.runner.clear_logs()

        main.recorder.stop()
        if main.recorder._thread and main.recorder._thread.is_alive():
            main.recorder._thread.join(timeout=2.0)
        with main.recorder._lock:
            main.recorder._status = "idle"
            main.recorder._error = ""
        main.recorder.clear()


class TestCoreAndStateRoutes(BaseApiTestCase):
    def test_index_serves_html_with_title_and_version(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        self.assertIn("AutoClick", body)
        self.assertIn(main.__version__, body)

    def test_api_version(self):
        resp = self.client.get("/api/version")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"app": "AutoClick", "version": main.__version__})

    def test_api_state_defaults(self):
        resp = self.client.get("/api/state")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["version"], main.__version__)
        self.assertEqual(data["active_preset_id"], engine.DEFAULT_PRESET_ID)
        self.assertEqual(len(data["presets"]), 1)
        self.assertEqual(data["presets"][0]["name"], "Copy Tab URLs (v0.5)")
        self.assertEqual(data["settings"], main.default_settings())
        self.assertEqual(data["runner"]["status"], "idle")
        self.assertEqual(data["recorder"]["status"], "idle")

    def test_api_run_status(self):
        resp = self.client.get("/api/run/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertIn("runner", data)
        self.assertIn("recorder", data)
        self.assertFalse(data["runner"]["active"])
        self.assertFalse(data["recorder"]["active"])

    def test_api_focus_without_and_with_window(self):
        resp = self.client.post("/api/focus")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "window": False})

        fake_window = mock.MagicMock()
        main.window = fake_window
        resp = self.client.post("/api/focus")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "window": True})
        fake_window.restore.assert_called_once()
        fake_window.show.assert_called_once()
        self.assertFalse(fake_window.on_top)


class TestPresetRoutes(BaseApiTestCase):
    def test_presets_select_valid_and_404(self):
        created = self.client.post("/api/presets/create", json={"name": "Second"}).get_json()["preset"]
        resp = self.client.post("/api/presets/select", json={"id": engine.DEFAULT_PRESET_ID})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "active_preset_id": engine.DEFAULT_PRESET_ID})

        # Persisted in isolated config
        self.assertEqual(common.load_config()["active_preset_id"], engine.DEFAULT_PRESET_ID)

        # Select second preset
        resp2 = self.client.post("/api/presets/select", json={"id": created["id"]})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.get_json()["active_preset_id"], created["id"])

        # Non-existent ID returns 404
        missing = self.client.post("/api/presets/select", json={"id": "does-not-exist"})
        self.assertEqual(missing.status_code, 404)
        self.assertFalse(missing.get_json()["ok"])

    def test_presets_save_create_update_and_invalid_payload(self):
        # Invalid payload: preset missing or not a dict
        bad1 = self.client.post("/api/presets/save", json={"preset": "not-a-dict"})
        self.assertEqual(bad1.status_code, 400)
        self.assertFalse(bad1.get_json()["ok"])

        bad2 = self.client.post("/api/presets/save", json={"preset": {"id": "bad", "iterations": "not-an-int"}})
        self.assertEqual(bad2.status_code, 400)
        self.assertFalse(bad2.get_json()["ok"])

        # Create a brand new preset via save
        new_payload = {
            "id": "custom-save-1",
            "name": "Custom Sequence",
            "description": "Created via save",
            "start_delay": 1.5,
            "iterations": 4,
            "steps": [
                {"id": "st1", "action": "key", "key": "space", "presses": 2, "hold_ms": 50, "delay": 0.2},
            ],
        }
        resp = self.client.post("/api/presets/save", json={"preset": new_payload})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["active_preset_id"], "custom-save-1")
        self.assertEqual(len(data["presets"]), 2)
        self.assertEqual(data["preset"]["name"], "Custom Sequence")

        # Update the same preset in place (no duplicate added)
        updated_payload = dict(new_payload, name="Custom Sequence v2", iterations=7)
        resp2 = self.client.post("/api/presets/save", json={"preset": updated_payload})
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.get_json()
        self.assertEqual(len(data2["presets"]), 2)
        self.assertEqual(data2["preset"]["name"], "Custom Sequence v2")
        self.assertEqual(data2["preset"]["iterations"], 7)

    def test_presets_save_mid_pause_live_update(self):
        pressed_keys: list[str] = []
        first_key_seen = threading.Event()

        def fake_press(key, presses=1):
            pressed_keys.append(key)
            if key == "a":
                first_key_seen.set()

        engine.pyautogui.press.side_effect = fake_press

        initial_preset = {
            "id": "live-edit-preset",
            "name": "Live Edit Test",
            "start_delay": 0.0,
            "iterations": 2,
            "infinite": False,
            "steps": [
                {"id": "k1", "action": "key", "key": "a", "presses": 1, "hold_ms": 0, "delay": 0.0},
                {"id": "w1", "action": "wait", "seconds": 0.15},
            ],
        }
        self.assertEqual(self.client.post("/api/presets/save", json={"preset": initial_preset}).status_code, 200)

        start_resp = self.client.post("/api/run/start", json={"preset": initial_preset, "save": False})
        self.assertEqual(start_resp.status_code, 200)
        self.assertTrue(first_key_seen.wait(timeout=2.0), "Runner did not execute first step in time")

        pause_resp = self.client.post("/api/run/pause")
        self.assertEqual(pause_resp.status_code, 200)
        self.assertEqual(pause_resp.get_json()["runner"]["status"], "paused")

        # Edit the preset mid-pause: replace steps with a new key 'z'
        edited_preset = {
            "id": "live-edit-preset",
            "name": "Live Edit Test",
            "start_delay": 0.0,
            "iterations": 2,
            "infinite": False,
            "steps": [
                {"id": "k2", "action": "key", "key": "z", "presses": 1, "hold_ms": 0, "delay": 0.0},
            ],
        }
        save_resp = self.client.post("/api/presets/save", json={"preset": edited_preset})
        self.assertEqual(save_resp.status_code, 200)
        self.assertEqual(main.runner.state()["total_steps"], 1)

        resume_resp = self.client.post("/api/run/resume")
        self.assertEqual(resume_resp.status_code, 200)
        self.assertEqual(resume_resp.get_json()["runner"]["status"], "running")

        finished = _wait_until(lambda: not main.runner.is_active(), timeout=3.0)
        self.assertTrue(finished, "Runner did not finish after mid-pause live edit and resume")
        self.assertEqual(main.runner.state()["status"], "completed")
        self.assertEqual(pressed_keys, ["a", "z"])

    def test_presets_create_blank_clone_and_custom_steps(self):
        # Blank create
        r_blank = self.client.post("/api/presets/create", json={"name": "Blank One"})
        self.assertEqual(r_blank.status_code, 200)
        blank = r_blank.get_json()["preset"]
        self.assertEqual(blank["name"], "Blank One")
        self.assertEqual(len(blank["steps"]), 1)
        self.assertEqual(blank["steps"][0]["action"], "wait")

        # Clone from default preset
        default_steps = engine.default_presets()[0]["steps"]
        r_clone = self.client.post(
            "/api/presets/create",
            json={"name": "Cloned Tabs", "clone_from": engine.DEFAULT_PRESET_ID},
        )
        self.assertEqual(r_clone.status_code, 200)
        cloned = r_clone.get_json()["preset"]
        self.assertNotEqual(cloned["id"], engine.DEFAULT_PRESET_ID)
        self.assertEqual(cloned["name"], "Cloned Tabs")
        self.assertEqual(len(cloned["steps"]), len(default_steps))
        self.assertTrue(
            all(c["id"] != orig["id"] for c, orig in zip(cloned["steps"], default_steps)),
            "Cloned steps must get new distinct step IDs",
        )

        # Custom steps create
        custom_steps = [
            {"action": "click", "x": 50, "y": 60, "button": "right", "clicks": 2, "hold_ms": 120, "delay": 0.25},
            {
                "action": "group",
                "name": "Nested",
                "repeat": 3,
                "steps": [{"action": "type", "text": "hello", "delay": 0.1}],
            },
        ]
        r_custom = self.client.post(
            "/api/presets/create",
            json={
                "name": "Custom Steps",
                "description": "Custom desc",
                "start_delay": 2.0,
                "iterations": 3,
                "infinite": True,
                "steps": custom_steps,
            },
        )
        self.assertEqual(r_custom.status_code, 200)
        custom = r_custom.get_json()["preset"]
        self.assertEqual(custom["name"], "Custom Steps")
        self.assertEqual(custom["description"], "Custom desc")
        self.assertEqual(custom["start_delay"], 2.0)
        self.assertEqual(custom["iterations"], 3)
        self.assertTrue(custom["infinite"])
        self.assertEqual(len(custom["steps"]), 2)
        self.assertEqual(custom["steps"][0]["action"], "click")
        self.assertEqual(custom["steps"][1]["action"], "group")
        self.assertEqual(custom["steps"][1]["steps"][0]["text"], "hello")

    def test_presets_delete_guard_404_and_valid(self):
        # Cannot delete the last remaining preset
        guard_resp = self.client.delete(f"/api/presets/{engine.DEFAULT_PRESET_ID}")
        self.assertEqual(guard_resp.status_code, 400)
        self.assertFalse(guard_resp.get_json()["ok"])

        # Add a second preset
        second = self.client.post("/api/presets/create", json={"name": "Disposable"}).get_json()["preset"]

        # Deleting a non-existent ID returns 404
        missing_resp = self.client.delete("/api/presets/nonexistent-id")
        self.assertEqual(missing_resp.status_code, 404)
        self.assertFalse(missing_resp.get_json()["ok"])

        # Deleting the active second preset succeeds and falls back active_preset_id to remaining[0]
        del_resp = self.client.delete(f"/api/presets/{second['id']}")
        self.assertEqual(del_resp.status_code, 200)
        data = del_resp.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(len(data["presets"]), 1)
        self.assertEqual(data["active_preset_id"], engine.DEFAULT_PRESET_ID)

    def test_presets_reset_restores_copy_tab_urls_v05(self):
        # Case 1: Mutate default preset and restore it
        mutated = dict(engine.default_presets()[0], name="Tampered", steps=[{"action": "wait", "seconds": 0.5}])
        self.client.post("/api/presets/save", json={"preset": mutated})
        self.assertEqual(len(self.client.get("/api/state").get_json()["presets"][0]["steps"]), 1)

        reset_resp = self.client.post("/api/presets/reset")
        self.assertEqual(reset_resp.status_code, 200)
        reset_data = reset_resp.get_json()
        self.assertEqual(reset_data["active_preset_id"], engine.DEFAULT_PRESET_ID)
        restored = next(p for p in reset_data["presets"] if p["id"] == engine.DEFAULT_PRESET_ID)
        self.assertEqual(restored["name"], "Copy Tab URLs (v0.5)")
        self.assertEqual(len(restored["steps"]), 8)

        # Case 2: Delete default preset after adding another, then reset re-inserts it at index 0
        self.client.post("/api/presets/create", json={"name": "Survivor"})
        self.assertEqual(self.client.delete(f"/api/presets/{engine.DEFAULT_PRESET_ID}").status_code, 200)
        reset_resp2 = self.client.post("/api/presets/reset")
        self.assertEqual(reset_resp2.status_code, 200)
        data2 = reset_resp2.get_json()
        self.assertEqual(data2["presets"][0]["id"], engine.DEFAULT_PRESET_ID)
        self.assertEqual(data2["presets"][0]["name"], "Copy Tab URLs (v0.5)")
        self.assertEqual(data2["active_preset_id"], engine.DEFAULT_PRESET_ID)

    def test_presets_import_single_list_dedup_and_invalid(self):
        # Single object
        single = {"id": "imp-1", "name": "Imported One", "steps": [{"action": "wait", "seconds": 0.2}]}
        r1 = self.client.post("/api/presets/import", json={"presets": single})
        self.assertEqual(r1.status_code, 200)
        d1 = r1.get_json()
        self.assertTrue(d1["ok"])
        self.assertEqual(d1["imported"], 1)
        self.assertEqual(d1["active_preset_id"], "imp-1")

        # List with duplicate IDs (one colliding with existing 'imp-1', one colliding within batch)
        batch = [
            {"id": "imp-1", "name": "Dup Existing", "steps": [{"action": "wait", "seconds": 0.3}]},
            {"id": "imp-2", "name": "Batch A", "steps": [{"action": "wait", "seconds": 0.3}]},
            {"id": "imp-2", "name": "Batch B", "steps": [{"action": "wait", "seconds": 0.3}]},
        ]
        r2 = self.client.post("/api/presets/import", json={"presets": batch})
        self.assertEqual(r2.status_code, 200)
        d2 = r2.get_json()
        self.assertEqual(d2["imported"], 3)
        ids = [p["id"] for p in d2["presets"]]
        self.assertEqual(len(ids), len(set(ids)), "All preset IDs must be unique after deduplication")
        self.assertEqual(len(ids), 5)  # default + imp-1 + 3 imported
        self.assertEqual(d2["active_preset_id"], d2["presets"][-1]["id"])

        # Invalid payloads return 400
        for bad_body in (
            {"presets": []},
            {"presets": "invalid"},
            {"presets": [123, "bad"]},
            {"presets": [{"name": "Bad", "iterations": "NaN"}]},
        ):
            r_bad = self.client.post("/api/presets/import", json=bad_body)
            self.assertEqual(r_bad.status_code, 400, f"Expected 400 for payload {bad_body}")
            self.assertFalse(r_bad.get_json()["ok"])


class TestSettingsRoute(BaseApiTestCase):
    def test_settings_valid_invalid_clamping_and_toggles(self):
        # Valid keys & toggles
        r1 = self.client.post(
            "/api/settings",
            json={
                "stop_key": "F10",
                "record_stop_key": "esc",
                "failsafe": False,
                "minimize_on_start": True,
                "speed": 2.5,
            },
        )
        self.assertEqual(r1.status_code, 200)
        s1 = r1.get_json()["settings"]
        self.assertEqual(s1["stop_key"], "f10")
        self.assertEqual(s1["record_stop_key"], "esc")
        self.assertFalse(s1["failsafe"])
        self.assertTrue(s1["minimize_on_start"])
        self.assertEqual(s1["speed"], 2.5)

        # Invalid stop keys are ignored; speed clamps to [0.1, 5.0]
        r2 = self.client.post(
            "/api/settings",
            json={"stop_key": "not_a_key", "record_stop_key": "ctrl", "speed": 99.0},
        )
        self.assertEqual(r2.status_code, 200)
        s2 = r2.get_json()["settings"]
        self.assertEqual(s2["stop_key"], "f10")
        self.assertEqual(s2["record_stop_key"], "esc")
        self.assertEqual(s2["speed"], 5.0)

        r3 = self.client.post("/api/settings", json={"speed": 0.001, "failsafe": True, "minimize_on_start": False})
        self.assertEqual(r3.status_code, 200)
        s3 = r3.get_json()["settings"]
        self.assertEqual(s3["speed"], 0.1)
        self.assertTrue(s3["failsafe"])
        self.assertFalse(s3["minimize_on_start"])

        # Non-numeric speed ignored
        r4 = self.client.post("/api/settings", json={"speed": "super-fast"})
        self.assertEqual(r4.status_code, 200)
        self.assertEqual(r4.get_json()["settings"]["speed"], 0.1)


class TestRunnerAndRecorderRoutes(BaseApiTestCase):
    def test_runner_lifecycle_pause_resume_stop_clear_log(self):
        preset = {
            "id": "run-test",
            "name": "Run Lifecycle",
            "start_delay": 0.0,
            "iterations": 20,
            "infinite": False,
            "steps": [{"id": "w1", "action": "wait", "seconds": 0.1}],
        }
        start_resp = self.client.post("/api/run/start", json={"preset": preset, "save": False})
        self.assertEqual(start_resp.status_code, 200)
        self.assertTrue(start_resp.get_json()["runner"]["active"])

        self.assertTrue(_wait_until(lambda: main.runner.state()["status"] == "running"))

        pause_resp = self.client.post("/api/run/pause")
        self.assertEqual(pause_resp.status_code, 200)
        self.assertEqual(pause_resp.get_json()["runner"]["status"], "paused")

        resume_resp = self.client.post("/api/run/resume")
        self.assertEqual(resume_resp.status_code, 200)
        self.assertEqual(resume_resp.get_json()["runner"]["status"], "running")

        stop_resp = self.client.post("/api/run/stop")
        self.assertEqual(stop_resp.status_code, 200)
        self.assertTrue(_wait_until(lambda: not main.runner.is_active()))
        self.assertEqual(main.runner.state()["status"], "stopped")
        self.assertGreater(len(main.runner.state()["logs"]), 0)

        clear_resp = self.client.post("/api/run/clear-log")
        self.assertEqual(clear_resp.status_code, 200)
        self.assertEqual(clear_resp.get_json()["runner"]["logs"], [])

    def test_runner_start_save_flag_controls_persistence(self):
        base = engine.default_presets()[0]
        modified_unsaved = dict(base, name="Temp Unsaved", start_delay=5.0)
        r1 = self.client.post("/api/run/start", json={"preset": modified_unsaved, "save": False})
        self.assertEqual(r1.status_code, 200)
        self.client.post("/api/run/stop")
        self.assertTrue(_wait_until(lambda: not main.runner.is_active()))
        # Stored preset name remains unchanged
        self.assertEqual(self.client.get("/api/state").get_json()["presets"][0]["name"], "Copy Tab URLs (v0.5)")

        modified_saved = dict(base, name="Persisted On Start", start_delay=5.0)
        r2 = self.client.post("/api/run/start", json={"preset": modified_saved, "save": True})
        self.assertEqual(r2.status_code, 200)
        self.client.post("/api/run/stop")
        self.assertTrue(_wait_until(lambda: not main.runner.is_active()))
        self.assertEqual(self.client.get("/api/state").get_json()["presets"][0]["name"], "Persisted On Start")

    def test_runner_start_error_on_empty_steps(self):
        empty_preset = {"id": "empty", "name": "Empty", "start_delay": 0.0, "iterations": 1, "steps": []}
        resp = self.client.post("/api/run/start", json={"preset": empty_preset, "save": False})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

    def test_runner_conflict_when_recorder_active(self):
        rec_start = self.client.post("/api/record/start", json={"mode": "both", "cooloff": 5.0})
        self.assertEqual(rec_start.status_code, 200)
        self.assertTrue(rec_start.get_json()["recorder"]["active"])

        run_start = self.client.post("/api/run/start", json={})
        self.assertEqual(run_start.status_code, 409)
        self.assertFalse(run_start.get_json()["ok"])

    def test_recorder_start_stop_and_clear_modes(self):
        # Start in cool-off (avoids installing real OS hooks while testing live API flow)
        r_start = self.client.post("/api/record/start", json={"mode": "keyboard", "cooloff": 5.0, "capture_delays": True})
        self.assertEqual(r_start.status_code, 200)
        rec_state = r_start.get_json()["recorder"]
        self.assertTrue(rec_state["active"])
        self.assertEqual(rec_state["status"], "cooloff")
        self.assertEqual(rec_state["mode"], "keyboard")

        # Stop recording
        r_stop = self.client.post("/api/record/stop")
        self.assertEqual(r_stop.status_code, 200)
        self.assertTrue(_wait_until(lambda: not main.recorder.is_active()))
        self.assertEqual(main.recorder.state()["status"], "idle")

        # Seed a captured step and log entry to test both clear modes
        sample_step = {"id": "rec1", "action": "click", "x": 10, "y": 20, "button": "left", "clicks": 1, "hold_ms": 0, "delay": 0.1, "enabled": True, "note": ""}
        main.recorder._record_step(sample_step, time.monotonic())
        main.recorder._log("Captured click")
        self.assertEqual(main.recorder.state()["step_count"], 1)
        self.assertGreater(len(main.recorder.state()["logs"]), 0)

        # logs_only=True clears logs while preserving captured steps
        r_clear_logs = self.client.post("/api/record/clear", json={"logs_only": True})
        self.assertEqual(r_clear_logs.status_code, 200)
        state_after_logs = r_clear_logs.get_json()["recorder"]
        self.assertEqual(state_after_logs["logs"], [])
        self.assertEqual(state_after_logs["step_count"], 1)
        self.assertEqual(state_after_logs["steps"][0]["id"], "rec1")

        # Full clear empties steps and logs
        main.recorder._log("Another log")
        r_clear_all = self.client.post("/api/record/clear", json={})
        self.assertEqual(r_clear_all.status_code, 200)
        state_after_all = r_clear_all.get_json()["recorder"]
        self.assertEqual(state_after_all["logs"], [])
        self.assertEqual(state_after_all["step_count"], 0)
        self.assertEqual(state_after_all["steps"], [])
        self.assertEqual(state_after_all["status"], "idle")

    def test_recorder_conflict_when_runner_active(self):
        preset = {
            "id": "hold-runner",
            "name": "Countdown",
            "start_delay": 5.0,
            "iterations": 1,
            "steps": [{"id": "w1", "action": "wait", "seconds": 0.1}],
        }
        self.assertEqual(self.client.post("/api/run/start", json={"preset": preset, "save": False}).status_code, 200)
        self.assertTrue(main.runner.is_active())

        rec_resp = self.client.post("/api/record/start", json={"cooloff": 1.0})
        self.assertEqual(rec_resp.status_code, 409)
        self.assertFalse(rec_resp.get_json()["ok"])

    def test_non_local_remote_addr_forbidden_403(self):
        remote_env = {"REMOTE_ADDR": "192.168.1.99"}
        protected_routes = [
            "/api/run/start",
            "/api/run/pause",
            "/api/run/resume",
            "/api/run/stop",
            "/api/record/start",
            "/api/record/stop",
            "/api/record/clear",
            "/api/cursor/capture",
        ]
        for route in protected_routes:
            resp = self.client.post(route, json={}, environ_base=remote_env)
            self.assertEqual(resp.status_code, 403, f"Expected 403 for non-local POST {route}")

        # Verify IPv6 loopback ::1 is allowed
        ok_resp = self.client.post("/api/cursor/capture", json={"delay": 0}, environ_base={"REMOTE_ADDR": "::1"})
        self.assertEqual(ok_resp.status_code, 200)

    def test_cursor_capture_with_zero_delay(self):
        engine.get_cursor_position.return_value = (777, 555)
        resp = self.client.post("/api/cursor/capture", json={"delay": 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"ok": True, "x": 777, "y": 555})
        engine.get_cursor_position.assert_called_once()


class TestUpdateRoutesAndVersioning(BaseApiTestCase):
    def test_parse_version_and_comparisons(self):
        self.assertEqual(main.parse_version("v1.0.0"), (1, 0, 0))
        self.assertEqual(main.parse_version("v0.5"), (0, 5, 0))
        self.assertEqual(main.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(main.parse_version("2"), (2, 0, 0))
        self.assertGreater(main.parse_version("v1.0.0"), main.parse_version("v0.5"))
        self.assertGreater(main.parse_version("v1.2.3"), main.parse_version("v1.0.0"))
        self.assertEqual(main.parse_version("v1.0.0"), main.parse_version("1.0.0"))

    def test_api_update_newer_available_and_caching(self):
        release_payload = {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/iqrarwaqas/AutoClick/releases/tag/v9.9.9",
            "assets": [
                {"name": "notes.txt", "browser_download_url": "https://example.com/notes.txt"},
                {"name": "AutoClick-Setup-9.9.9.exe", "browser_download_url": "https://example.com/AutoClick-Setup-9.9.9.exe"},
            ],
        }
        main.github_get.side_effect = lambda url, timeout: _make_json_response(release_payload)

        r1 = self.client.get("/api/update")
        self.assertEqual(r1.status_code, 200)
        d1 = r1.get_json()
        self.assertTrue(d1["available"])
        self.assertEqual(d1["current"], main.__version__)
        self.assertEqual(d1["latest"], "9.9.9")
        self.assertEqual(d1["installer"], "https://example.com/AutoClick-Setup-9.9.9.exe")
        self.assertFalse(d1["can_install"])  # running from source (FROZEN=False)
        self.assertEqual(main.github_get.call_count, 1)

        # Cached on second call without force=1
        r2 = self.client.get("/api/update")
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(main.github_get.call_count, 1)

        # force=1 bypasses cache
        r3 = self.client.get("/api/update?force=1")
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(main.github_get.call_count, 2)

    def test_api_update_up_to_date(self):
        release_payload = {
            "tag_name": f"v{main.__version__}",
            "html_url": f"https://github.com/iqrarwaqas/AutoClick/releases/tag/v{main.__version__}",
            "assets": [],
        }
        main.github_get.side_effect = lambda url, timeout: _make_json_response(release_payload)

        resp = self.client.get("/api/update")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["available"])
        self.assertEqual(data["latest"], main.__version__)
        self.assertIsNone(data["installer"])

    def test_api_update_network_error_fallback(self):
        main.github_get.side_effect = OSError("Simulated offline network")

        resp = self.client.get("/api/update")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertFalse(data["available"])
        self.assertFalse(data["can_install"])
        self.assertIn("error", data)
        self.assertTrue(data["error"])

    def test_api_update_install_returns_400_when_no_update_available(self):
        # Case 1: already up to date
        release_payload = {
            "tag_name": f"v{main.__version__}",
            "html_url": "https://github.com/iqrarwaqas/AutoClick/releases",
            "assets": [],
        }
        main.github_get.side_effect = lambda url, timeout: _make_json_response(release_payload)

        resp = self.client.post("/api/update/install")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.get_json()["ok"])

        # Case 2: newer release exists, but running from source (FROZEN=False => can_install=False)
        main._update_info = None
        newer_payload = {
            "tag_name": "v9.9.9",
            "html_url": "https://github.com/iqrarwaqas/AutoClick/releases/tag/v9.9.9",
            "assets": [{"name": "AutoClick-Setup-9.9.9.exe", "browser_download_url": "https://example.com/setup.exe"}],
        }
        main.github_get.side_effect = lambda url, timeout: _make_json_response(newer_payload)
        resp2 = self.client.post("/api/update/install")
        self.assertEqual(resp2.status_code, 400)
        self.assertFalse(resp2.get_json()["ok"])


class TestPackagingAndReleaseIntegrity(unittest.TestCase):
    def test_main_version_matches_release_workflow_format(self):
        main_src = (REPO_ROOT / "main.py").read_text(encoding="utf-8")
        # Matches the exact line format asserted by .github/workflows/release.yml:
        #   grep -q "^__version__ = \"$VERSION\"$" main.py
        matches = re.findall(r'^__version__ = "(\d+\.\d+\.\d+)"$', main_src, flags=re.MULTILINE)
        self.assertEqual(len(matches), 1, "main.py must define a single __version__ = \"<semver>\" line")
        self.assertEqual(matches[0], main.__version__)

        workflow_src = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn(r'grep -q "^__version__ = \"$VERSION\"$" main.py', workflow_src)

    def test_packaging_and_release_files_exist_and_are_consistent(self):
        icon_ico = REPO_ROOT / "packaging" / "icon.ico"
        icon_svg = REPO_ROOT / "static" / "icon.svg"
        spec_file = REPO_ROOT / "packaging" / "AutoClick.spec"
        iss_file = REPO_ROOT / "installer" / "AutoClick.iss"
        workflow_file = REPO_ROOT / ".github" / "workflows" / "release.yml"

        for path in (icon_ico, icon_svg, spec_file, iss_file, workflow_file):
            self.assertTrue(path.is_file(), f"Required packaging/release file missing: {path}")
            self.assertGreater(path.stat().st_size, 0, f"Required file is empty: {path}")

        # ICO magic header: 00 00 01 00
        self.assertEqual(icon_ico.read_bytes()[:4], b"\x00\x00\x01\x00")

        svg_text = icon_svg.read_text(encoding="utf-8")
        self.assertIn("<svg", svg_text)

        spec_text = spec_file.read_text(encoding="utf-8")
        self.assertIn('name="AutoClick"', spec_text)
        self.assertIn("main.py", spec_text)
        self.assertIn("templates", spec_text)
        self.assertIn("static", spec_text)
        self.assertIn("icon.ico", spec_text)

        iss_text = iss_file.read_text(encoding="utf-8")
        self.assertRegex(
            iss_text,
            r"(?m)^AppId=\{\{[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}\}$",
            "Inno Setup AppId must be a valid escaped GUID ({{...})",
        )
        self.assertIn("AppName=AutoClick", iss_text)
        self.assertIn("OutputBaseFilename=AutoClick-Setup-{#AppVersion}", iss_text)
        self.assertIn("AutoClick.exe", iss_text)
        self.assertIn("icon.ico", iss_text)

        workflow_text = workflow_file.read_text(encoding="utf-8")
        self.assertIn("packaging/AutoClick.spec", workflow_text)
        self.assertIn(r"installer\AutoClick.iss", workflow_text)
        self.assertIn("dist/AutoClick-Setup-$VERSION.exe", workflow_text)
        self.assertIn("AutoClick", workflow_text)

        # Ensure no leftover VideoLabeler references exist in any packaging/release/icon/backend files
        for text_path in (icon_svg, spec_file, iss_file, workflow_file, REPO_ROOT / "main.py", REPO_ROOT / "common.py"):
            content = text_path.read_text(encoding="utf-8").lower()
            self.assertNotIn("videolabeler", content, f"Leftover VideoLabeler reference found in {text_path.name}")


if __name__ == "__main__":
    unittest.main()
