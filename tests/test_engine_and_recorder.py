"""Comprehensive unit tests for common.py, engine.py, and recorder.py."""

from __future__ import annotations

import ctypes
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import common
import engine
import recorder


class FakeFailSafeException(Exception):
    """Stand-in for pyautogui.FailSafeException."""


def make_fake_pyautogui() -> mock.MagicMock:
    """Create a fresh mocked pyautogui module so tests never emit real OS input."""
    fake = mock.MagicMock(name="pyautogui")
    fake.FailSafeException = FakeFailSafeException
    fake.FAILSAFE = True
    fake.PAUSE = 0.0
    fake.position.return_value = SimpleNamespace(x=123, y=456)
    return fake


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Poll predicate until truthy or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# ---------------------------------------------------------------------------
# 1. common.py tests
# ---------------------------------------------------------------------------


class TestCommonConfig(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / "nested" / "autoclick_test_config.json"
        self._patcher = mock.patch.object(common, "CONFIG_FILE", self.cfg_path)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()
        self._tmp.cleanup()

    def test_load_config_missing_file_returns_empty_dict(self):
        self.assertFalse(self.cfg_path.exists())
        self.assertEqual(common.load_config(), {})

    def test_load_config_corrupt_json_returns_empty_dict(self):
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.cfg_path.write_text("{not valid json", encoding="utf-8")
        self.assertEqual(common.load_config(), {})

    def test_save_and_load_config_roundtrip_and_atomic_cleanup(self):
        payload = {"stop_key": "f10", "speed": 1.5, "presets": [{"id": "p1", "name": "Demo"}]}
        common.save_config(payload)

        self.assertTrue(self.cfg_path.exists())
        self.assertFalse(self.cfg_path.with_suffix(".tmp").exists())
        self.assertEqual(common.load_config(), payload)
        # Ensure readable UTF-8 indented JSON on disk
        raw = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw, payload)

    def test_update_config_merges_and_preserves_existing_keys(self):
        common.save_config({"a": 1, "b": "keep"})
        common.update_config(b="updated", c=[1, 2, 3])
        self.assertEqual(common.load_config(), {"a": 1, "b": "updated", "c": [1, 2, 3]})

    def test_update_config_creates_file_when_missing(self):
        common.update_config(failsafe=False, speed=2.0)
        self.assertTrue(self.cfg_path.exists())
        self.assertEqual(common.load_config(), {"failsafe": False, "speed": 2.0})


# ---------------------------------------------------------------------------
# 2. engine.py pure function tests
# ---------------------------------------------------------------------------


class TestEngineHelpers(unittest.TestCase):
    def test_default_presets_v05_copy_tab_urls_structure(self):
        presets = engine.default_presets()
        self.assertEqual(len(presets), 1)
        p = presets[0]
        self.assertEqual(p["id"], engine.DEFAULT_PRESET_ID)
        self.assertEqual(p["id"], "copy-tab-urls")
        self.assertEqual(p["name"], "Copy Tab URLs (v0.5)")
        self.assertIn("address bar", p["description"])
        self.assertEqual(p["start_delay"], 5.0)
        self.assertEqual(p["iterations"], 15)
        self.assertFalse(p["infinite"])

        steps = p["steps"]
        self.assertEqual(len(steps), 8)
        self.assertEqual(steps[0]["action"], "wait")
        self.assertEqual(steps[0]["seconds"], 1.0)
        expected_hotkeys = [
            (["ctrl", "l"], 0.1),
            (["ctrl", "c"], 0.1),
            (["alt", "tab"], 0.5),
            (["ctrl", "v"], 0.1),
            (["shift", "enter"], 0.1),
            (["alt", "tab"], 0.5),
            (["ctrl", "w"], 0.0),
        ]
        for step, (keys, delay) in zip(steps[1:], expected_hotkeys):
            self.assertEqual(step["action"], "hotkey")
            self.assertEqual(step["keys"], keys)
            self.assertEqual(step["hold_ms"], 0)
            self.assertEqual(step["delay"], delay)
            self.assertTrue(step["enabled"])
            self.assertTrue(step["note"])

        # Each call returns independent copies
        presets2 = engine.default_presets()
        presets[0]["steps"].pop()
        self.assertEqual(len(presets2[0]["steps"]), 8)

    def test_normalize_step_all_six_actions_and_clamping(self):
        # 1. hotkey (string keys, clamping, empty fallback)
        hk_str = engine.normalize_step({"action": "HOTKEY", "keys": " Ctrl + Shift , T ", "hold_ms": 20000, "delay": -5})
        self.assertEqual(hk_str["action"], "hotkey")
        self.assertEqual(hk_str["keys"], ["ctrl", "shift", "t"])
        self.assertEqual(hk_str["hold_ms"], 10000)
        self.assertEqual(hk_str["delay"], 0.0)
        self.assertTrue(hk_str["enabled"])
        self.assertEqual(len(hk_str["id"]), 8)

        hk_empty = engine.normalize_step({"action": "hotkey", "keys": [], "hold_ms": -10})
        self.assertEqual(hk_empty["keys"], ["ctrl", "c"])
        self.assertEqual(hk_empty["hold_ms"], 0)

        # Unknown action defaults to hotkey
        unknown = engine.normalize_step({"action": "teleport", "note": "  hi  ", "enabled": False})
        self.assertEqual(unknown["action"], "hotkey")
        self.assertEqual(unknown["note"], "hi")
        self.assertFalse(unknown["enabled"])

        # 2. key
        k = engine.normalize_step({"action": "key", "key": "  Space ", "presses": 999, "hold_ms": 500, "delay": 0.25})
        self.assertEqual(k["key"], "space")
        self.assertEqual(k["presses"], 100)
        self.assertEqual(k["hold_ms"], 500)
        self.assertEqual(k["delay"], 0.25)

        k_min = engine.normalize_step({"action": "key", "key": "", "presses": 0, "hold_ms": None})
        self.assertEqual(k_min["key"], "enter")
        self.assertEqual(k_min["presses"], 1)
        self.assertEqual(k_min["hold_ms"], 0)

        # 3. type
        t = engine.normalize_step({"action": "type", "text": "Hello World!", "interval": 9.9, "delay": -1})
        self.assertEqual(t["text"], "Hello World!")
        self.assertEqual(t["interval"], 2.0)
        self.assertEqual(t["delay"], 0.0)

        t_min = engine.normalize_step({"action": "type", "interval": -0.5})
        self.assertEqual(t_min["text"], "")
        self.assertEqual(t_min["interval"], 0.0)

        # 4. click
        c = engine.normalize_step({
            "action": "click",
            "x": "150",
            "y": 300,
            "button": "RIGHT",
            "clicks": 9,
            "hold_ms": 250,
            "delay": 0.2,
        })
        self.assertEqual(c["x"], 150)
        self.assertEqual(c["y"], 300)
        self.assertEqual(c["button"], "right")
        self.assertEqual(c["clicks"], 5)
        self.assertEqual(c["hold_ms"], 250)
        self.assertEqual(c["delay"], 0.2)

        c_default = engine.normalize_step({"action": "click", "x": "", "y": None, "button": "side", "clicks": 0})
        self.assertIsNone(c_default["x"])
        self.assertIsNone(c_default["y"])
        self.assertEqual(c_default["button"], "left")
        self.assertEqual(c_default["clicks"], 1)

        # 5. wait
        w_low = engine.normalize_step({"action": "wait", "seconds": 0.0})
        w_high = engine.normalize_step({"action": "wait", "seconds": 99999})
        self.assertEqual(w_low["seconds"], 0.01)
        self.assertEqual(w_high["seconds"], 3600.0)

        # 6. group
        g = engine.normalize_step({
            "action": "group",
            "name": "   ",
            "repeat": 5000,
            "delay": -2,
            "steps": [{"action": "key", "key": "a"}, "not-a-dict", None],
        })
        self.assertEqual(g["action"], "group")
        self.assertEqual(g["name"], "Sub-steps")
        self.assertEqual(g["repeat"], 1000)
        self.assertEqual(g["delay"], 0.0)
        self.assertEqual(len(g["steps"]), 1)
        self.assertEqual(g["steps"][0]["key"], "a")

        g_min = engine.normalize_step({"action": "group", "name": "Burst", "repeat": 0, "steps": []})
        self.assertEqual(g_min["name"], "Burst")
        self.assertEqual(g_min["repeat"], 1)

    def test_normalize_step_enforces_max_depth(self):
        # Build nested groups of depth 6: depth 1..4 stay "group", depth 5 becomes "wait"
        raw: dict = {"action": "group", "name": "Level 6", "steps": [{"action": "key", "key": "z"}]}
        for lvl in range(5, 0, -1):
            raw = {"action": "group", "name": f"Level {lvl}", "repeat": 2, "steps": [raw]}

        norm = engine.normalize_step(raw, _depth=1)
        cur = norm
        for lvl in range(1, engine.MAX_DEPTH):
            self.assertEqual(cur["action"], "group", f"Expected group at depth {lvl}")
            self.assertEqual(len(cur["steps"]), 1)
            cur = cur["steps"][0]

        # At _depth == MAX_DEPTH (5), group is converted to wait with no child steps
        self.assertEqual(cur["action"], "wait")
        self.assertEqual(cur["seconds"], 1.0)
        self.assertNotIn("steps", cur)
        self.assertEqual(engine.max_step_depth([norm]), engine.MAX_DEPTH)

    def test_normalize_preset_defaults_and_clamping(self):
        p = engine.normalize_preset({
            "name": "   ",
            "description": "  My macro  ",
            "start_delay": 999,
            "iterations": 0,
            "infinite": 1,
            "steps": [{"action": "wait", "seconds": 0.5}, 123],
        })
        self.assertEqual(len(p["id"]), 8)
        self.assertEqual(p["name"], "Untitled sequence")
        self.assertEqual(p["description"], "My macro")
        self.assertEqual(p["start_delay"], 120.0)
        self.assertEqual(p["iterations"], 1)
        self.assertTrue(p["infinite"])
        self.assertEqual(len(p["steps"]), 1)

        p2 = engine.normalize_preset({"id": "custom", "name": "Fast", "start_delay": -3, "iterations": 999999})
        self.assertEqual(p2["id"], "custom")
        self.assertEqual(p2["start_delay"], 0.0)
        self.assertEqual(p2["iterations"], 100000)
        self.assertFalse(p2["infinite"])

    def test_count_enabled_leaves_and_max_step_depth(self):
        self.assertEqual(engine.count_enabled_leaves([]), 0)
        self.assertEqual(engine.max_step_depth([]), 0)

        steps = [
            {"action": "key", "key": "a", "enabled": True},
            {"action": "key", "key": "b", "enabled": False},
            {
                "action": "group",
                "name": "Outer",
                "repeat": 3,
                "enabled": True,
                "steps": [
                    {"action": "wait", "seconds": 0.1, "enabled": True},
                    {
                        "action": "group",
                        "name": "Inner",
                        "repeat": 2,
                        "enabled": True,
                        "steps": [
                            {"action": "click", "enabled": True},
                            {"action": "key", "key": "x", "enabled": False},
                        ],
                    },
                ],
            },
            {
                "action": "group",
                "name": "DisabledGroup",
                "repeat": 10,
                "enabled": False,
                "steps": [{"action": "key", "key": "c", "enabled": True}],
            },
        ]
        # 1 + 3 * (1 + 2 * 1) + 0 = 10
        self.assertEqual(engine.count_enabled_leaves(steps), 10)
        self.assertEqual(engine.max_step_depth(steps), 3)

        # Empty group at depth 1 reports depth 2
        self.assertEqual(engine.max_step_depth([{"action": "group", "steps": []}]), 2)
        self.assertEqual(engine.max_step_depth([{"action": "key", "key": "a"}]), 1)

    def test_describe_step_all_variants(self):
        self.assertEqual(engine.describe_step({"action": "hotkey", "keys": ["ctrl", "c"]}), "Ctrl + C")
        self.assertEqual(
            engine.describe_step({"action": "hotkey", "keys": ["alt", "tab"], "hold_ms": 150}),
            "Alt + Tab · hold 150ms",
        )
        self.assertEqual(engine.describe_step({"action": "key", "key": "a", "presses": 1}), "A")
        self.assertEqual(
            engine.describe_step({"action": "key", "key": "enter", "presses": 3, "hold_ms": 200}),
            "Enter ×3 · hold 200ms",
        )
        self.assertEqual(engine.describe_step({"action": "type", "text": "hello"}), 'Type "hello"')
        long_text = "a" * 30
        self.assertEqual(engine.describe_step({"action": "type", "text": long_text}), f'Type "{"a" * 25}…"')
        self.assertEqual(
            engine.describe_step({"action": "click", "button": "left", "clicks": 1, "x": 10, "y": 20}),
            "Left Click (10, 20)",
        )
        self.assertEqual(
            engine.describe_step({"action": "click", "button": "right", "clicks": 2, "x": None, "y": None}),
            "Right Double-click at cursor",
        )
        self.assertEqual(
            engine.describe_step({"action": "click", "button": "middle", "clicks": 3, "x": 5, "y": 6, "hold_ms": 90}),
            "Middle 3× click (5, 6) · hold 90ms",
        )
        self.assertEqual(engine.describe_step({"action": "wait", "seconds": 1.5}), "Wait 1.5 s")
        self.assertEqual(engine.describe_step({"action": "group", "name": "Combo", "repeat": 4}), "Combo ×4")
        self.assertEqual(engine.describe_step({"action": "custom_unknown"}), "custom_unknown")


# ---------------------------------------------------------------------------
# 3. SequenceRunner tests
# ---------------------------------------------------------------------------


class TestSequenceRunner(unittest.TestCase):
    def setUp(self):
        self.fake_gui = make_fake_pyautogui()
        self._gui_patch = mock.patch.object(engine, "pyautogui", self.fake_gui)
        self._vk_patch = mock.patch.object(engine, "is_vk_pressed", return_value=False)
        self._gui_patch.start()
        self._vk_patch.start()
        self.runner = engine.SequenceRunner()

    def tearDown(self):
        self.runner.stop("test teardown")
        if self.runner._thread and self.runner._thread.is_alive():
            self.runner._thread.join(timeout=2.0)
        self._vk_patch.stop()
        self._gui_patch.stop()

    def _wait_done(self, timeout: float = 2.0) -> dict:
        if self.runner._thread:
            self.runner._thread.join(timeout=timeout)
        self.assertFalse(
            self.runner._thread is not None and self.runner._thread.is_alive(),
            "Runner thread did not finish in time (possible deadlock).",
        )
        return self.runner.state()

    def test_start_validation_errors(self):
        # No enabled steps
        with self.assertRaises(ValueError):
            self.runner.start({"name": "Empty", "steps": []})
        with self.assertRaises(ValueError):
            self.runner.start({"name": "AllOff", "steps": [{"action": "key", "key": "a", "enabled": False}]})

        # Missing pyautogui
        with mock.patch.object(engine, "pyautogui", None):
            with self.assertRaises(RuntimeError):
                self.runner.start({"name": "NoGUI", "steps": [{"action": "key", "key": "a"}]})

        # Already active
        self.runner.start(
            {"name": "Slow", "start_delay": 5.0, "iterations": 1, "steps": [{"action": "key", "key": "a"}]},
            stop_key="none",
        )
        with self.assertRaises(RuntimeError):
            self.runner.start({"name": "Second", "start_delay": 0, "steps": [{"action": "key", "key": "b"}]})

    def test_cooloff_countdown_and_minimize_restore_callbacks(self):
        events: list[str] = []
        runner = engine.SequenceRunner(
            on_minimize=lambda: events.append("minimize"),
            on_restore=lambda: events.append("restore"),
        )
        self.runner = runner
        preset = {
            "id": "cool",
            "name": "Cool-off Test",
            "start_delay": 0.12,
            "iterations": 1,
            "steps": [{"action": "key", "key": "enter", "delay": 0.0}],
        }
        runner.start(preset, stop_key="none", minimize_on_start=True)
        initial = runner.state()
        self.assertIn(initial["status"], ("countdown", "running", "completed"))
        self.assertEqual(initial["preset_name"], "Cool-off Test")

        final = self._wait_done()
        self.assertEqual(final["status"], "completed")
        self.assertFalse(final["active"])
        self.assertEqual(final["countdown_remaining"], 0.0)
        self.assertEqual(final["iteration"], 1)
        self.assertEqual(final["steps_completed"], 1)
        self.assertEqual(events, ["minimize", "restore"])
        self.fake_gui.press.assert_called_once_with("enter", presses=1)

    def test_all_step_types_including_hold_ms_and_disabled_skip(self):
        call_log: list[tuple] = []
        self.fake_gui.hotkey.side_effect = lambda *k: call_log.append(("hotkey", k))
        self.fake_gui.keyDown.side_effect = lambda k: call_log.append(("keyDown", k))
        self.fake_gui.keyUp.side_effect = lambda k: call_log.append(("keyUp", k))
        self.fake_gui.press.side_effect = lambda k, presses=1: call_log.append(("press", k, presses))
        self.fake_gui.write.side_effect = lambda txt, interval=0.0: call_log.append(("write", txt, interval))
        self.fake_gui.click.side_effect = lambda x=None, y=None, clicks=1, button="left": call_log.append(
            ("click", x, y, clicks, button)
        )
        self.fake_gui.mouseDown.side_effect = lambda x=None, y=None, button="left": call_log.append(
            ("mouseDown", x, y, button)
        )
        self.fake_gui.mouseUp.side_effect = lambda x=None, y=None, button="left": call_log.append(
            ("mouseUp", x, y, button)
        )

        preset = {
            "id": "all-steps",
            "name": "All Actions",
            "start_delay": 0.0,
            "iterations": 1,
            "steps": [
                {"action": "hotkey", "keys": ["ctrl", "c"], "hold_ms": 0, "delay": 0.0},
                {"action": "hotkey", "keys": ["ctrl", "shift", "k"], "hold_ms": 15, "delay": 0.0},
                {"action": "key", "key": "tab", "presses": 2, "hold_ms": 0, "delay": 0.0},
                {"action": "key", "key": "space", "presses": 2, "hold_ms": 15, "delay": 0.0},
                {"action": "type", "text": "abc", "interval": 0.01, "delay": 0.0},
                {"action": "click", "x": 100, "y": 200, "button": "right", "clicks": 2, "hold_ms": 0, "delay": 0.0},
                {"action": "click", "x": 50, "y": 60, "button": "left", "clicks": 2, "hold_ms": 15, "delay": 0.0},
                {"action": "wait", "seconds": 0.01},
                {"action": "key", "key": "z", "enabled": False, "delay": 0.0},
            ],
        }

        self.runner.start(preset, stop_key="none", failsafe=False)
        st = self._wait_done()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["steps_completed"], 8)
        self.assertFalse(self.fake_gui.FAILSAFE)
        self.assertEqual(self.fake_gui.PAUSE, 0.0)

        self.assertEqual(
            call_log,
            [
                ("hotkey", ("ctrl", "c")),
                ("keyDown", "ctrl"),
                ("keyDown", "shift"),
                ("keyDown", "k"),
                ("keyUp", "k"),
                ("keyUp", "shift"),
                ("keyUp", "ctrl"),
                ("press", "tab", 2),
                ("keyDown", "space"),
                ("keyUp", "space"),
                ("keyDown", "space"),
                ("keyUp", "space"),
                ("write", "abc", 0.01),
                ("click", 100, 200, 2, "right"),
                ("mouseDown", 50, 60, "left"),
                ("mouseUp", 50, 60, "left"),
                ("mouseDown", 50, 60, "left"),
                ("mouseUp", 50, 60, "left"),
            ],
        )

    def test_nested_group_repeat_loops(self):
        pressed: list[str] = []
        depths_seen: list[int] = []

        def on_press(k, presses=1):
            pressed.append(k)
            depths_seen.append(self.runner.state()["step_depth"])

        self.fake_gui.press.side_effect = on_press
        preset = {
            "id": "nested",
            "name": "Nested Groups",
            "start_delay": 0.0,
            "iterations": 2,
            "steps": [
                {
                    "action": "group",
                    "name": "Outer",
                    "repeat": 2,
                    "delay": 0.0,
                    "steps": [
                        {"action": "key", "key": "a", "delay": 0.0},
                        {
                            "action": "group",
                            "name": "Inner",
                            "repeat": 3,
                            "delay": 0.0,
                            "steps": [{"action": "key", "key": "b", "delay": 0.0}],
                        },
                    ],
                }
            ],
        }
        self.runner.start(preset, stop_key="none")
        st = self._wait_done()
        self.assertEqual(st["status"], "completed")
        self.assertEqual(st["iteration"], 2)
        # Per outer repeat: [a, b, b, b]; 2 outer repeats * 2 iterations = 4 * 4 = 16 leaves
        self.assertEqual(pressed, ["a", "b", "b", "b"] * 4)
        self.assertEqual(depths_seen, [2, 3, 3, 3] * 4)
        self.assertEqual(st["steps_completed"], 16)

    def test_speed_scaling_divides_wait_and_delays(self):
        recorded_sleeps: list[float] = []
        orig_sleep = self.runner._sleep_interruptible

        def spy_sleep(seconds, vk_code, stop_key, *, update_countdown=False):
            recorded_sleeps.append(round(seconds, 5))
            return orig_sleep(0.0, vk_code, stop_key, update_countdown=update_countdown)

        preset = {
            "id": "speed",
            "name": "Speed Scaling",
            "start_delay": 0.0,
            "iterations": 1,
            "steps": [
                {"action": "wait", "seconds": 1.0},
                {"action": "key", "key": "a", "delay": 0.4},
                {
                    "action": "group",
                    "name": "G",
                    "repeat": 1,
                    "delay": 0.8,
                    "steps": [{"action": "key", "key": "b", "delay": 0.2}],
                },
            ],
        }
        with mock.patch.object(self.runner, "_sleep_interruptible", side_effect=spy_sleep):
            self.runner.start(preset, stop_key="none", speed=4.0)
            st = self._wait_done()

        self.assertEqual(st["status"], "completed")
        self.assertEqual(recorded_sleeps, [0.25, 0.1, 0.05, 0.2])

    def test_repeated_pause_and_resume_no_deadlock(self):
        step_entered = threading.Event()
        cycles = 6
        pressed: list[str] = []

        def on_press(k, presses=1):
            pressed.append(k)
            step_entered.set()

        self.fake_gui.press.side_effect = on_press
        preset = {
            "id": "pause-cycle",
            "name": "Pause Stress",
            "start_delay": 0.0,
            "iterations": cycles + 2,
            "steps": [{"action": "key", "key": "a", "delay": 0.02}],
        }
        # Pause/resume while idle are safe no-ops
        self.runner.pause()
        self.runner.resume()
        self.assertEqual(self.runner.state()["status"], "idle")

        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(step_entered.is_set, timeout=1.0))

        for _ in range(cycles):
            if not self.runner.is_active():
                break
            self.runner.pause()
            st_paused = self.runner.state()
            if st_paused["status"] == "paused":
                # Repeated pause while already paused must be a safe no-op
                self.runner.pause()
                self.assertEqual(self.runner.state()["status"], "paused")
                self.runner.resume()
                # Repeated resume while running must be a safe no-op
                self.runner.resume()
                self.assertIn(self.runner.state()["status"], ("running", "completed"))
            time.sleep(0.01)

        final = self._wait_done(timeout=2.0)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(len(pressed), cycles + 2)
        self.assertTrue(any("Paused." in line for line in final["logs"]))
        self.assertTrue(any("Resumed." in line for line in final["logs"]))

    def test_mid_pause_update_live_preset_reorder_and_delete(self):
        first_step_done = threading.Event()
        pressed: list[str] = []

        def on_press(k, presses=1):
            pressed.append(k)
            if len(pressed) == 1:
                # Pause immediately on step 1 of iteration 1
                self.runner.pause()
                first_step_done.set()

        self.fake_gui.press.side_effect = on_press
        preset = {
            "id": "live-edit",
            "name": "Live Edit",
            "start_delay": 0.0,
            "iterations": 3,
            "steps": [
                {"id": "s1", "action": "key", "key": "a", "delay": 0.0},
                {"id": "s2", "action": "key", "key": "b", "delay": 0.0},
                {"id": "s3", "action": "key", "key": "c", "delay": 0.0},
            ],
        }
        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(first_step_done.is_set, timeout=1.0))
        self.assertEqual(self.runner.state()["status"], "paused")

        # Update for a DIFFERENT preset id must be ignored
        self.runner.update_live_preset({
            "id": "other-preset",
            "name": "Other",
            "iterations": 1,
            "steps": [{"id": "x", "action": "key", "key": "z", "delay": 0.0}],
        })
        self.assertEqual(self.runner.state()["total_steps"], 3)

        # Delete 'b' and reorder remaining to ['c', 'a'] mid-pause
        self.runner.update_live_preset({
            "id": "live-edit",
            "name": "Live Edit",
            "iterations": 3,
            "steps": [
                {"id": "s3", "action": "key", "key": "c", "delay": 0.0},
                {"id": "s1", "action": "key", "key": "a", "delay": 0.0},
            ],
        })
        self.assertEqual(self.runner.state()["total_steps"], 2)

        self.runner.resume()
        final = self._wait_done()
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["iteration"], 3)
        # Iteration 1 finishes original [a, b, c]; iterations 2 & 3 use updated [c, a]
        self.assertEqual(pressed, ["a", "b", "c", "c", "a", "c", "a"])

    def test_removing_all_steps_mid_pause_stops_cleanly(self):
        paused_in_iter1 = threading.Event()

        def on_press(k, presses=1):
            if k == "a" and not paused_in_iter1.is_set():
                self.runner.pause()
                paused_in_iter1.set()

        self.fake_gui.press.side_effect = on_press
        preset = {
            "id": "clear-all",
            "name": "Clear All Mid-Pause",
            "start_delay": 0.0,
            "iterations": 5,
            "steps": [
                {"id": "s1", "action": "key", "key": "a", "delay": 0.0},
                {"id": "s2", "action": "key", "key": "b", "delay": 0.0},
            ],
        }
        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(paused_in_iter1.is_set, timeout=1.0))
        self.assertEqual(self.runner.state()["status"], "paused")

        self.runner.update_live_preset({"id": "clear-all", "name": "Clear All Mid-Pause", "iterations": 5, "steps": []})
        self.runner.resume()

        final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["iteration"], 1)
        self.assertTrue(any("All steps were removed" in line for line in final["logs"]))

    def test_stop_during_cooloff(self):
        preset = {
            "id": "stop-cool",
            "name": "Stop Cool-off",
            "start_delay": 10.0,
            "iterations": 1,
            "steps": [{"action": "key", "key": "a"}],
        }
        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(lambda: self.runner.state()["status"] == "countdown", timeout=1.0))
        self.runner.stop("Cancelled in cool-off.")
        final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["stop_reason"], "Cancelled in cool-off.")
        self.assertEqual(final["iteration"], 0)
        self.fake_gui.press.assert_not_called()

    def test_stop_during_running(self):
        started = threading.Event()
        self.fake_gui.press.side_effect = lambda k, presses=1: started.set()
        preset = {
            "id": "stop-run",
            "name": "Stop Running",
            "start_delay": 0.0,
            "infinite": True,
            "steps": [
                {"action": "key", "key": "a", "delay": 0.0},
                {"action": "wait", "seconds": 5.0},
            ],
        }
        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(started.is_set, timeout=1.0))
        self.runner.stop("Stopped mid-run.")
        final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["stop_reason"], "Stopped mid-run.")

    def test_stop_during_paused(self):
        paused_ready = threading.Event()

        def on_press(k, presses=1):
            self.runner.pause()
            paused_ready.set()

        self.fake_gui.press.side_effect = on_press
        preset = {
            "id": "stop-pause",
            "name": "Stop Paused",
            "start_delay": 0.0,
            "iterations": 10,
            "steps": [
                {"action": "key", "key": "a", "delay": 0.0},
                {"action": "key", "key": "b", "delay": 0.0},
            ],
        }
        self.runner.start(preset, stop_key="none")
        self.assertTrue(wait_until(paused_ready.is_set, timeout=1.0))
        self.assertEqual(self.runner.state()["status"], "paused")

        self.runner.stop("Stopped while paused.")
        final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertEqual(final["stop_reason"], "Stopped while paused.")

    def test_emergency_stop_hotkey_stops_runner(self):
        with mock.patch.object(engine, "is_vk_pressed", return_value=True):
            preset = {
                "id": "hotkey-stop",
                "name": "Emergency Stop",
                "start_delay": 0.0,
                "iterations": 5,
                "steps": [{"action": "key", "key": "a", "delay": 0.0}],
            }
            self.runner.start(preset, stop_key="f8")
            final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertIn("F8", final["stop_reason"])

    def test_failsafe_exception_handling(self):
        self.fake_gui.press.side_effect = FakeFailSafeException("corner triggered")
        preset = {
            "id": "fs",
            "name": "Failsafe",
            "start_delay": 0.0,
            "iterations": 1,
            "steps": [{"action": "key", "key": "a", "delay": 0.0}],
        }
        self.runner.start(preset, stop_key="none", failsafe=True)
        final = self._wait_done()
        self.assertEqual(final["status"], "stopped")
        self.assertIn("corner failsafe", final["stop_reason"].lower())
        self.assertTrue(any("corner failsafe" in line.lower() for line in final["logs"]))

    def test_error_status_handling_and_clear_logs(self):
        self.fake_gui.press.side_effect = RuntimeError("synthetic driver failure")
        preset = {
            "id": "err",
            "name": "Error Case",
            "start_delay": 0.0,
            "iterations": 1,
            "steps": [{"action": "key", "key": "a", "delay": 0.0}],
        }
        self.runner.start(preset, stop_key="none")
        final = self._wait_done()
        self.assertEqual(final["status"], "error")
        self.assertEqual(final["error"], "synthetic driver failure")
        self.assertEqual(final["step_label"], "Error: synthetic driver failure")
        self.assertTrue(len(final["logs"]) > 0)

        self.runner.clear_logs()
        self.assertEqual(self.runner.state()["logs"], [])


# ---------------------------------------------------------------------------
# 4. recorder.py tests
# ---------------------------------------------------------------------------


class TestVkToName(unittest.TestCase):
    def test_letters_and_digits(self):
        for code in range(0x41, 0x5B):
            self.assertEqual(recorder.vk_to_name(code), chr(code).lower())
        for code in range(0x30, 0x3A):
            self.assertEqual(recorder.vk_to_name(code), chr(code))

    def test_modifiers(self):
        for vk in (0x10, 0xA0, 0xA1):
            self.assertEqual(recorder.vk_to_name(vk), "shift")
        for vk in (0x11, 0xA2, 0xA3):
            self.assertEqual(recorder.vk_to_name(vk), "ctrl")
        for vk in (0x12, 0xA4, 0xA5):
            self.assertEqual(recorder.vk_to_name(vk), "alt")
        for vk in (0x5B, 0x5C):
            self.assertEqual(recorder.vk_to_name(vk), "win")

    def test_special_keys_fkeys_and_numpad(self):
        expected = {
            0x08: "backspace",
            0x09: "tab",
            0x0D: "enter",
            0x13: "pause",
            0x14: "capslock",
            0x1B: "esc",
            0x20: "space",
            0x21: "pageup",
            0x22: "pagedown",
            0x23: "end",
            0x24: "home",
            0x25: "left",
            0x26: "up",
            0x27: "right",
            0x28: "down",
            0x2E: "delete",
            0x6A: "multiply",
            0x6B: "add",
            0x6D: "subtract",
            0x6E: "decimal",
            0x6F: "divide",
            0xBA: ";",
            0xBB: "=",
            0xBC: ",",
            0xBD: "-",
            0xBE: ".",
            0xBF: "/",
            0xC0: "`",
            0xDB: "[",
            0xDC: "\\",
            0xDD: "]",
            0xDE: "'",
        }
        for vk, name in expected.items():
            self.assertEqual(recorder.vk_to_name(vk), name)

        # F1..F24
        for i in range(1, 25):
            self.assertEqual(recorder.vk_to_name(0x6F + i), f"f{i}")

        # Numpad 0..9
        for i in range(10):
            self.assertEqual(recorder.vk_to_name(0x60 + i), f"num{i}")

    def test_unknown_vks_return_none(self):
        for vk in (-1, 0x00, 0x01, 0x02, 0x07, 0x88, 0xFF, 0x999):
            self.assertIsNone(recorder.vk_to_name(vk))


class TestMultiMonitorSupport(unittest.TestCase):
    def test_compute_failsafe_points_single_multi_and_l_shaped(self):
        # Single monitor: all 4 outer corners
        self.assertEqual(
            engine.compute_failsafe_points([(0, 0, 1920, 1080)]),
            [(0, 0), (1919, 0), (0, 1079), (1919, 1079)],
        )

        # 3-monitor horizontal strip (left negative X, primary, right 125%-scaled monitor)
        three_mon = [(-1920, 0, 0, 1080), (0, 0, 1920, 1080), (1920, 0, 3840, 1080)]
        pts = engine.compute_failsafe_points(three_mon)
        self.assertEqual(
            set(pts),
            {(-1920, 0), (-1920, 1079), (3839, 0), (3839, 1079)},
        )
        # Internal monitor seams must NOT trigger failsafe
        for seam_pt in ((0, 0), (1919, 0), (0, 1079), (1919, 1079), (1920, 0), (-1, 0)):
            self.assertNotIn(seam_pt, pts)

        # Vertically stacked monitors exclude internal horizontal seam corners
        stacked = [(0, 0, 1920, 1080), (0, 1080, 1920, 2160)]
        stacked_pts = engine.compute_failsafe_points(stacked)
        self.assertEqual(
            set(stacked_pts),
            {(0, 0), (1919, 0), (0, 2159), (1919, 2159)},
        )
        for seam_pt in ((0, 1079), (1919, 1079), (0, 1080), (1919, 1080)):
            self.assertNotIn(seam_pt, stacked_pts)

        # Empty monitor list falls back safely
        self.assertEqual(engine.compute_failsafe_points([]), [(0, 0)])

    @unittest.skipUnless(sys.platform == "win32", "Win32 cursor API requires Windows")
    def test_move_cursor_to_retries_cross_dpi_and_virtualdesk_fallback(self):
        fake_user32 = mock.MagicMock()
        fake_user32.GetSystemMetrics.side_effect = lambda idx: {76: -1920, 77: 0, 78: 5760, 79: 1080}.get(idx, 0)
        mock_windll = SimpleNamespace(user32=fake_user32)

        def run_with_positions(positions):
            pos_iter = iter(positions)

            def fake_get_pos(pt_ref):
                pt = pt_ref._obj
                x, y = next(pos_iter)
                pt.x, pt.y = x, y
                return 1

            fake_user32.SetCursorPos.reset_mock()
            fake_user32.GetCursorPos.side_effect = fake_get_pos

        with (
            mock.patch.object(engine.ctypes, "windll", mock_windll),
            mock.patch.object(engine, "_send_virtual_mouse_move") as mock_send,
        ):
            # 1. Exact on first SetCursorPos
            run_with_positions([(100, 200)])
            engine.move_cursor_to(100, 200)
            self.assertEqual(fake_user32.SetCursorPos.call_count, 1)
            mock_send.assert_not_called()

            # 2. Cross-DPI mismatch on first try, exact on second SetCursorPos
            run_with_positions([(2600, 400), (3500, 900)])
            engine.move_cursor_to(3500, 900)
            self.assertEqual(fake_user32.SetCursorPos.call_count, 2)
            mock_send.assert_not_called()

            # 3. Stays off-target for all 3 SetCursorPos calls -> SendInput VIRTUALDESK fallback (negative X)
            run_with_positions([(-500, 500), (-500, 500), (-500, 500)])
            engine.move_cursor_to(-960, 540)
            self.assertEqual(fake_user32.SetCursorPos.call_count, 4)  # 3 retries + 1 final settle
            exp_nx = int(round((-960 - (-1920)) * 65535 / 5759))
            exp_ny = int(round(540 * 65535 / 1079))
            mock_send.assert_called_once_with(exp_nx, exp_ny)

            # 4. VIRTUALDESK fallback on right 125% monitor
            mock_send.reset_mock()
            run_with_positions([(2800, 700), (2800, 700), (2800, 700)])
            engine.move_cursor_to(3500, 900)
            exp_nx2 = int(round((3500 - (-1920)) * 65535 / 5759))
            exp_ny2 = int(round(900 * 65535 / 1079))
            mock_send.assert_called_once_with(exp_nx2, exp_ny2)

    def test_negative_and_secondary_monitor_click_coords_preserved(self):
        fake_pg = make_fake_pyautogui()
        runner = engine.SequenceRunner()
        preset = {
            "id": "multi-mon",
            "name": "Multi Monitor Clicks",
            "start_delay": 0.0,
            "iterations": 1,
            "steps": [
                {"id": "c1", "action": "click", "x": -960, "y": 540, "button": "left", "clicks": 1, "delay": 0.0},
                {"id": "c2", "action": "click", "x": 3500, "y": 900, "button": "right", "clicks": 2, "hold_ms": 20, "delay": 0.0},
            ],
        }
        norm = engine.normalize_preset(preset)
        self.assertEqual(norm["steps"][0]["x"], -960)
        self.assertEqual(norm["steps"][1]["x"], 3500)

        with mock.patch.object(engine, "pyautogui", fake_pg):
            runner.start(preset, stop_key="none", failsafe=True)
            self.assertTrue(wait_until(lambda: not runner.is_active(), timeout=2.0))

        self.assertEqual(runner.state()["status"], "completed")
        fake_pg.click.assert_called_once_with(x=-960, y=540, clicks=1, button="left")
        self.assertEqual(fake_pg.mouseDown.call_count, 2)
        fake_pg.mouseDown.assert_called_with(x=3500, y=900, button="right")


class TestInputRecorder(unittest.TestCase):
    def setUp(self):
        self.rec = recorder.InputRecorder()

    def tearDown(self):
        self.rec.stop()
        if self.rec._thread and self.rec._thread.is_alive():
            self.rec._thread.join(timeout=2.0)

    def test_initial_state_fields(self):
        st = self.rec.state()
        expected_keys = {
            "status",
            "active",
            "mode",
            "stop_key",
            "cooloff_remaining",
            "elapsed",
            "step_count",
            "steps",
            "error",
            "logs",
        }
        self.assertEqual(set(st.keys()), expected_keys)
        self.assertEqual(st["status"], "idle")
        self.assertFalse(st["active"])
        self.assertFalse(self.rec.is_active())
        self.assertEqual(st["step_count"], 0)
        self.assertEqual(st["steps"], [])
        self.assertEqual(st["error"], "")
        self.assertEqual(st["logs"], [])

    def test_record_step_inter_step_delay_calculation(self):
        self.rec._capture_delays = True
        s1 = {"id": "1", "action": "key", "key": "a", "delay": 0.1}
        s2 = {"id": "2", "action": "key", "key": "b", "delay": 0.1}
        s3 = {"id": "3", "action": "wait", "seconds": 1.0}
        s4 = {"id": "4", "action": "click", "x": 1, "y": 2, "delay": 0.1}

        self.rec._record_step(s1, 10.0)
        self.assertEqual(s1["delay"], 0.1)

        # Gap of 0.456s rounds to 0.46 and updates previous step s1
        self.rec._record_step(s2, 10.456)
        self.assertEqual(s1["delay"], 0.46)
        self.assertEqual(s2["delay"], 0.1)

        # Gap of 0.0 does not overwrite previous step delay
        self.rec._record_step(s3, 10.456)
        self.assertEqual(s2["delay"], 0.1)

        # Previous step is 'wait' -> must not gain a 'delay' key
        self.rec._record_step(s4, 12.0)
        self.assertNotIn("delay", s3)
        self.assertEqual(self.rec.state()["step_count"], 4)

        # When capture_delays is False, previous delay is left untouched
        self.rec._capture_delays = False
        s5 = {"id": "5", "action": "key", "key": "c", "delay": 0.1}
        self.rec._record_step(s5, 15.0)
        self.assertEqual(s4["delay"], 0.1)

    def test_clear_behavior_idle_vs_active(self):
        self.rec._steps.append({"id": "1", "action": "key", "key": "a"})
        self.rec._logs.append("[00:00:00] test")
        self.rec._status = "done"
        self.rec._error = "old error"

        self.rec.clear()
        st = self.rec.state()
        self.assertEqual(st["status"], "idle")
        self.assertEqual(st["steps"], [])
        self.assertEqual(st["logs"], [])
        self.assertEqual(st["error"], "")

        # While active, clear() must be a no-op
        self.rec._steps.append({"id": "2", "action": "key", "key": "b"})
        self.rec._status = "recording"
        self.rec.clear()
        self.assertEqual(self.rec.state()["status"], "recording")
        self.assertEqual(self.rec.state()["step_count"], 1)
        self.rec._status = "idle"

    def test_cooloff_cancellation_via_stop(self):
        fake_user32 = mock.MagicMock()
        fake_user32.GetAsyncKeyState.return_value = 0

        with mock.patch.object(recorder, "_user32", fake_user32):
            self.rec.start(mode="both", cooloff=5.0, stop_key="f9", stop_vk=0x78)
            self.assertTrue(wait_until(lambda: self.rec.state()["status"] == "cooloff", timeout=1.0))
            self.assertTrue(self.rec.is_active())
            self.assertGreater(self.rec.state()["cooloff_remaining"], 0.0)

            # Starting again while active must raise
            with self.assertRaises(RuntimeError):
                self.rec.start(cooloff=1.0)

            self.rec.stop()
            self.rec._thread.join(timeout=2.0)

        self.assertFalse(self.rec._thread.is_alive())
        st = self.rec.state()
        self.assertEqual(st["status"], "idle")
        self.assertFalse(st["active"])
        self.assertEqual(st["cooloff_remaining"], 0.0)
        self.assertTrue(any("cancelled during cool-off" in line.lower() for line in st["logs"]))

    def test_start_rejects_non_win32_and_normalizes_invalid_mode(self):
        with mock.patch.object(recorder.sys, "platform", "linux"):
            with self.assertRaises(RuntimeError):
                self.rec.start()

    @unittest.skipUnless(sys.platform == "win32", "Win32 low-level hooks require Windows")
    def test_live_win32_hook_start_and_clean_stop(self):
        # Install real WH_KEYBOARD_LL + WH_MOUSE_LL hooks with zero cooloff, then stop cleanly.
        self.rec.start(mode="invalid-mode", cooloff=0.0, stop_key="f9", stop_vk=0x78)
        ready = wait_until(
            lambda: self.rec.state()["status"] == "recording" and self.rec._hook_thread_id is not None,
            timeout=1.5,
        )
        self.assertTrue(ready, f"Recorder did not enter recording state: {self.rec.state()}")
        # Give GetMessageW a brief moment to initialize the thread message queue
        time.sleep(0.05)

        st_live = self.rec.state()
        self.assertEqual(st_live["status"], "recording")
        self.assertTrue(st_live["active"])
        self.assertEqual(st_live["mode"], "both")  # invalid mode fell back to "both"
        self.assertEqual(st_live["stop_key"], "f9")
        self.assertGreaterEqual(st_live["elapsed"], 0.0)

        self.rec.stop()
        self.rec._thread.join(timeout=2.0)
        self.assertFalse(self.rec._thread.is_alive())

        st_done = self.rec.state()
        self.assertEqual(st_done["status"], "done")
        self.assertFalse(st_done["active"])
        self.assertIsNone(self.rec._hook_thread_id)
        self.assertTrue(any("Recording stopped." in line for line in st_done["logs"]))

    @unittest.skipUnless(sys.platform == "win32", "KBDLLHOOKSTRUCT/MSLLHOOKSTRUCT defined on win32")
    def test_mocked_hooks_capture_key_hotkey_click_and_stop_key(self):
        """Exercise on_keyboard and on_mouse callbacks deterministically without real OS hooks."""
        hooks: dict[int, object] = {}
        msg_loop_entered = threading.Event()
        quit_event = threading.Event()
        events: list[str] = []

        rec = recorder.InputRecorder(
            on_minimize=lambda: events.append("min"),
            on_restore=lambda: events.append("res"),
        )
        self.rec = rec

        fake_user32 = mock.MagicMock()
        fake_kernel32 = mock.MagicMock()
        fake_kernel32.GetCurrentThreadId.return_value = 4242

        def fake_set_hook(hook_id, proc, hmod, tid):
            hooks[hook_id] = proc
            return 100 + hook_id

        def fake_get_message(msg_ptr, hwnd, fmin, fmax):
            msg_loop_entered.set()
            quit_event.wait(timeout=2.0)
            return 0

        def fake_post_thread_msg(tid, msg, wp, lp):
            quit_event.set()
            return 1

        fake_user32.SetWindowsHookExW.side_effect = fake_set_hook
        fake_user32.GetMessageW.side_effect = fake_get_message
        fake_user32.PostThreadMessageW.side_effect = fake_post_thread_msg
        fake_user32.CallNextHookEx.return_value = 0
        fake_user32.UnhookWindowsHookEx.return_value = 1

        with (
            mock.patch.object(recorder, "_user32", fake_user32),
            mock.patch.object(recorder, "_kernel32", fake_kernel32),
        ):
            rec.start(mode="both", cooloff=0.0, stop_key="f9", stop_vk=0x78, minimize_on_start=True)
            self.assertTrue(msg_loop_entered.wait(timeout=1.5))

            kb_cb = hooks[recorder.WH_KEYBOARD_LL]
            ms_cb = hooks[recorder.WH_MOUSE_LL]

            def send_kb(vk: int, wparam: int, flags: int = 0):
                s = recorder.KBDLLHOOKSTRUCT(vkCode=vk, scanCode=0, flags=flags, time=0, dwExtraInfo=0)
                kb_cb(0, wparam, ctypes.addressof(s))

            def send_ms(wparam: int, x: int, y: int, flags: int = 0):
                s = recorder.MSLLHOOKSTRUCT(pt=ctypes.wintypes.POINT(x, y), mouseData=0, flags=flags, time=0, dwExtraInfo=0)
                ms_cb(0, wparam, ctypes.addressof(s))

            # 1. Injected key should be ignored
            send_kb(0x41, recorder.WM_KEYDOWN, flags=recorder.LLKHF_INJECTED)
            send_kb(0x41, recorder.WM_KEYUP, flags=recorder.LLKHF_INJECTED)

            # 2. Single key press (crisp press -> hold_ms == 0)
            send_kb(0x41, recorder.WM_KEYDOWN)  # 'a' down
            send_kb(0x41, recorder.WM_KEYUP)    # 'a' up

            # 3. Hotkey: Ctrl + C (crisp press -> hold_ms == 0)
            send_kb(0xA2, recorder.WM_KEYDOWN)  # ctrl down
            send_kb(0x43, recorder.WM_KEYDOWN)  # 'c' down
            send_kb(0x43, recorder.WM_KEYUP)    # 'c' up
            send_kb(0xA2, recorder.WM_KEYUP)    # ctrl up

            # 4. Mouse right-click with hold >= 80ms
            send_ms(recorder.WM_RBUTTONDOWN, 320, 240)
            time.sleep(0.095)
            send_ms(recorder.WM_RBUTTONUP, 320, 240)

            # 5. Injected mouse click should be ignored
            send_ms(recorder.WM_LBUTTONDOWN, 10, 10, flags=recorder.LLMHF_INJECTED)
            send_ms(recorder.WM_LBUTTONUP, 10, 10, flags=recorder.LLMHF_INJECTED)

            # 6. Stop key (F9) triggers stop()
            send_kb(0x78, recorder.WM_KEYDOWN)
            rec._thread.join(timeout=2.0)

        self.assertFalse(rec._thread.is_alive())
        st = rec.state()
        self.assertEqual(st["status"], "done")
        self.assertEqual(st["step_count"], 3)
        self.assertEqual(events, ["min", "res"])

        s_key, s_hotkey, s_click = st["steps"]
        self.assertEqual(s_key["action"], "key")
        self.assertEqual(s_key["key"], "a")
        self.assertEqual(s_key["hold_ms"], 0)

        self.assertEqual(s_hotkey["action"], "hotkey")
        self.assertEqual(s_hotkey["keys"], ["ctrl", "c"])
        self.assertEqual(s_hotkey["hold_ms"], 0)

        self.assertEqual(s_click["action"], "click")
        self.assertEqual(s_click["button"], "right")
        self.assertEqual(s_click["x"], 320)
        self.assertEqual(s_click["y"], 240)
        self.assertGreaterEqual(s_click["hold_ms"], 80)

        # Both hooks were unhooked cleanly
        self.assertEqual(fake_user32.UnhookWindowsHookEx.call_count, 2)

    @unittest.skipUnless(sys.platform == "win32", "Win32 hook error path")
    def test_hook_install_failure_sets_error_status(self):
        fake_user32 = mock.MagicMock()
        fake_user32.SetWindowsHookExW.return_value = 0
        fake_kernel32 = mock.MagicMock()
        fake_kernel32.GetCurrentThreadId.return_value = 1234

        with (
            mock.patch.object(recorder, "_user32", fake_user32),
            mock.patch.object(recorder, "_kernel32", fake_kernel32),
        ):
            self.rec.start(mode="both", cooloff=0.0)
            self.rec._thread.join(timeout=2.0)

        st = self.rec.state()
        self.assertEqual(st["status"], "error")
        self.assertIn("keyboard hook", st["error"].lower())

    def test_recorder_stop_wait_joins_worker_thread(self):
        fake_thread = mock.MagicMock(spec=threading.Thread)
        fake_thread.is_alive.return_value = True
        self.rec._thread = fake_thread
        self.rec._hook_thread_id = None

        self.rec.stop(wait=True)
        fake_thread.join.assert_called_once_with(timeout=0.5)

        # Calling stop(wait=True) from the worker thread itself must not self-join
        fake_thread2 = threading.current_thread()
        self.rec._thread = fake_thread2
        self.rec.stop(wait=True)  # should not raise RuntimeError("cannot join current thread")
        self.rec._thread = None


if __name__ == "__main__":
    unittest.main()
