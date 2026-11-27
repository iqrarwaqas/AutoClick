"""Keyboard and mouse sequence runner with press depth, nested multi-step groups, and emergency stop."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import sys
import threading
import time
import types
import uuid
from datetime import datetime
from typing import Any, Callable


def enable_per_monitor_dpi_awareness() -> bool:
    """Enable Per-Monitor V2 DPI awareness before PyAutoGUI locks the process to System DPI."""
    if sys.platform != "win32":
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (DPI_AWARENESS_CONTEXT)-4
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        # PROCESS_PER_MONITOR_DPI_AWARE == 2; E_ACCESSDENIED (-2147024891) means already set
        hr = ctypes.windll.shcore.SetProcessDpiAwareness(2)
        if hr in (0, -2147024891):
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass
    return False


enable_per_monitor_dpi_awareness()

try:
    import pyautogui
except ImportError:
    pyautogui = None  # surfaced gracefully at runtime if missing

# Windows virtual-key codes for global emergency stop / record stop hotkeys (no admin rights needed).
STOP_KEYS: dict[str, int] = {
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f12": 0x7B,
    "pause": 0x13,
    "esc": 0x1B,
    "none": 0,
}

VALID_ACTIONS = {"hotkey", "key", "type", "click", "wait", "group"}
VALID_BUTTONS = {"left", "right", "middle"}
MAX_DEPTH = 5

# The original v0.5 script sequence, included as a ready-to-run preset in v1.0.0.
DEFAULT_PRESET_ID = "copy-tab-urls"


def default_presets() -> list[dict[str, Any]]:
    return [
        {
            "id": DEFAULT_PRESET_ID,
            "name": "Copy Tab URLs (v0.5)",
            "description": "Focuses the address bar, copies the URL, Alt+Tabs to the target app, pastes with Shift+Enter, switches back, and closes the tab.",
            "start_delay": 5.0,
            "iterations": 15,
            "infinite": False,
            "steps": [
                {"id": "s1", "action": "wait", "seconds": 1.0, "enabled": True, "note": "Start-of-loop delay"},
                {"id": "s2", "action": "hotkey", "keys": ["ctrl", "l"], "hold_ms": 0, "delay": 0.1, "enabled": True, "note": "Focus address bar"},
                {"id": "s3", "action": "hotkey", "keys": ["ctrl", "c"], "hold_ms": 0, "delay": 0.1, "enabled": True, "note": "Copy URL"},
                {"id": "s4", "action": "hotkey", "keys": ["alt", "tab"], "hold_ms": 0, "delay": 0.5, "enabled": True, "note": "Switch to target window"},
                {"id": "s5", "action": "hotkey", "keys": ["ctrl", "v"], "hold_ms": 0, "delay": 0.1, "enabled": True, "note": "Paste URL"},
                {"id": "s6", "action": "hotkey", "keys": ["shift", "enter"], "hold_ms": 0, "delay": 0.1, "enabled": True, "note": "New line"},
                {"id": "s7", "action": "hotkey", "keys": ["alt", "tab"], "hold_ms": 0, "delay": 0.5, "enabled": True, "note": "Switch back to browser"},
                {"id": "s8", "action": "hotkey", "keys": ["ctrl", "w"], "hold_ms": 0, "delay": 0.0, "enabled": True, "note": "Close tab"},
            ],
        }
    ]


def normalize_step(raw: dict[str, Any], _depth: int = 1) -> dict[str, Any]:
    action = str(raw.get("action", "hotkey")).strip().lower()
    if action not in VALID_ACTIONS:
        action = "hotkey"
    if action == "group" and _depth >= MAX_DEPTH:
        action = "wait"

    step: dict[str, Any] = {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "action": action,
        "enabled": bool(raw.get("enabled", True)),
        "note": str(raw.get("note") or "").strip(),
    }
    if action == "hotkey":
        keys = raw.get("keys", [])
        if isinstance(keys, str):
            keys = [k.strip().lower() for k in keys.replace("+", ",").split(",") if k.strip()]
        else:
            keys = [str(k).strip().lower() for k in keys if str(k).strip()]
        step["keys"] = keys or ["ctrl", "c"]
        step["hold_ms"] = max(0, min(10000, int(raw.get("hold_ms", 0) or 0)))
        step["delay"] = max(0.0, float(raw.get("delay", 0.1)))
    elif action == "key":
        step["key"] = str(raw.get("key") or "enter").strip().lower()
        step["presses"] = max(1, min(100, int(raw.get("presses", 1))))
        step["hold_ms"] = max(0, min(10000, int(raw.get("hold_ms", 0) or 0)))
        step["delay"] = max(0.0, float(raw.get("delay", 0.1)))
    elif action == "type":
        step["text"] = str(raw.get("text") or "")
        step["interval"] = max(0.0, min(2.0, float(raw.get("interval", 0.0))))
        step["delay"] = max(0.0, float(raw.get("delay", 0.1)))
    elif action == "click":
        x = raw.get("x")
        y = raw.get("y")
        step["x"] = int(x) if x not in (None, "") else None
        step["y"] = int(y) if y not in (None, "") else None
        button = str(raw.get("button") or "left").strip().lower()
        step["button"] = button if button in VALID_BUTTONS else "left"
        step["clicks"] = max(1, min(5, int(raw.get("clicks", 1))))
        step["hold_ms"] = max(0, min(10000, int(raw.get("hold_ms", 0) or 0)))
        step["delay"] = max(0.0, float(raw.get("delay", 0.1)))
    elif action == "wait":
        step["seconds"] = max(0.01, min(3600.0, float(raw.get("seconds", 1.0))))
    elif action == "group":
        step["name"] = str(raw.get("name") or "Sub-steps").strip() or "Sub-steps"
        step["repeat"] = max(1, min(1000, int(raw.get("repeat", 2))))
        step["delay"] = max(0.0, float(raw.get("delay", 0.1)))
        step["steps"] = [normalize_step(s, _depth + 1) for s in (raw.get("steps") or []) if isinstance(s, dict)]
    return step


def normalize_preset(raw: dict[str, Any]) -> dict[str, Any]:
    steps = [normalize_step(s, 1) for s in (raw.get("steps") or []) if isinstance(s, dict)]
    return {
        "id": str(raw.get("id") or uuid.uuid4().hex[:8]),
        "name": str(raw.get("name") or "Untitled sequence").strip() or "Untitled sequence",
        "description": str(raw.get("description") or "").strip(),
        "start_delay": max(0.0, min(120.0, float(raw.get("start_delay", 5.0)))),
        "iterations": max(1, min(100000, int(raw.get("iterations", 15)))),
        "infinite": bool(raw.get("infinite", False)),
        "steps": steps,
    }


def count_enabled_leaves(steps: list[dict[str, Any]]) -> int:
    total = 0
    for s in steps:
        if not s.get("enabled", True):
            continue
        if s["action"] == "group":
            total += count_enabled_leaves(s.get("steps", [])) * s.get("repeat", 1)
        else:
            total += 1
    return total


def max_step_depth(steps: list[dict[str, Any]], depth: int = 1) -> int:
    best = depth if steps else 0
    for s in steps:
        if s["action"] == "group":
            child_d = max_step_depth(s.get("steps", []), depth + 1)
            best = max(best, child_d or (depth + 1))
    return best


def describe_step(step: dict[str, Any]) -> str:
    action = step["action"]
    hold = int(step.get("hold_ms", 0) or 0)
    hold_txt = f" · hold {hold}ms" if hold > 0 else ""
    if action == "hotkey":
        combo = " + ".join(k.upper() if len(k) == 1 else k.title() for k in step.get("keys", []))
        return f"{combo}{hold_txt}"
    if action == "key":
        k = step.get("key", "")
        label = k.upper() if len(k) == 1 else k.title()
        presses = step.get("presses", 1)
        base = f"{label} ×{presses}" if presses > 1 else label
        return f"{base}{hold_txt}"
    if action == "type":
        text = step.get("text", "")
        preview = text if len(text) <= 28 else text[:25] + "…"
        return f'Type "{preview}"'
    if action == "click":
        btn = step.get("button", "left").title()
        clicks = step.get("clicks", 1)
        kind = "Double-click" if clicks == 2 else (f"{clicks}× click" if clicks > 2 else "Click")
        x, y = step.get("x"), step.get("y")
        pos = f"({x}, {y})" if x is not None and y is not None else "at cursor"
        return f"{btn} {kind} {pos}{hold_txt}"
    if action == "wait":
        return f"Wait {step.get('seconds', 0):g} s"
    if action == "group":
        return f"{step.get('name', 'Sub-steps')} ×{step.get('repeat', 1)}"
    return action


def is_vk_pressed(vk_code: int) -> bool:
    """Check globally on Windows whether a virtual key is currently down."""
    if not vk_code or sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)
    except Exception:  # noqa: BLE001
        return False


def get_cursor_position() -> tuple[int, int]:
    if pyautogui is not None:
        pos = pyautogui.position()
        return int(pos.x), int(pos.y)
    if sys.platform == "win32":
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return int(pt.x), int(pt.y)
    return 0, 0


def _is_real_pyautogui() -> bool:
    return (
        pyautogui is not None
        and isinstance(pyautogui, types.ModuleType)
        and getattr(pyautogui, "__name__", "") == "pyautogui"
    )


def get_monitor_rects() -> list[tuple[int, int, int, int]]:
    """Return [(left, top, right, bottom), ...] for all active monitors on Windows."""
    if sys.platform != "win32":
        return []
    rects: list[tuple[int, int, int, int]] = []
    try:
        user32 = ctypes.windll.user32

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD),
            ]

        enum_proc = ctypes.WINFUNCTYPE(
            ctypes.wintypes.BOOL,
            ctypes.wintypes.HMONITOR,
            ctypes.wintypes.HDC,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.wintypes.LPARAM,
        )

        def _callback(hmon, _hdc, _lprc, _lparam):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                r = info.rcMonitor
                rects.append((int(r.left), int(r.top), int(r.right), int(r.bottom)))
            return True

        user32.EnumDisplayMonitors(None, None, enum_proc(_callback), 0)
    except Exception:  # noqa: BLE001
        return []
    return rects


def compute_failsafe_points(rects: list[tuple[int, int, int, int]] | None = None) -> list[tuple[int, int]]:
    """Compute outer-desktop corner points only, ignoring internal multi-monitor seams."""
    if rects is None:
        rects = get_monitor_rects()
    valid_rects = [(int(l), int(t), int(r), int(b)) for l, t, r, b in rects if r > l and b > t]
    if not valid_rects:
        return [(0, 0)]

    def _inside_any(px: int, py: int) -> bool:
        return any(l <= px < r and t <= py < b for l, t, r, b in valid_rects)

    corners: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for l, t, r, b in valid_rects:
        candidates = (
            ((l, t), (l - 1, t), (l, t - 1)),
            ((r - 1, t), (r, t), (r - 1, t - 1)),
            ((l, b - 1), (l - 1, b - 1), (l, b)),
            ((r - 1, b - 1), (r, b - 1), (r - 1, b)),
        )
        for pt, out1, out2 in candidates:
            if not _inside_any(*out1) and not _inside_any(*out2) and pt not in seen:
                seen.add(pt)
                corners.append(pt)
    return corners or [(0, 0)]


def _send_virtual_mouse_move(nx: int, ny: int) -> None:
    """Move cursor via SendInput with MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK."""
    if sys.platform != "win32":
        return

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.wintypes.LONG),
            ("dy", ctypes.wintypes.LONG),
            ("mouseData", ctypes.wintypes.DWORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.wintypes.WORD),
            ("wScan", ctypes.wintypes.WORD),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("time", ctypes.wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.wintypes.DWORD),
            ("wParamL", ctypes.wintypes.WORD),
            ("wParamH", ctypes.wintypes.WORD),
        ]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", _MOUSEINPUT),
            ("ki", _KEYBDINPUT),
            ("hi", _HARDWAREINPUT),
        ]

    class _INPUT(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.wintypes.DWORD),
            ("u", _INPUT_UNION),
        ]

    inp = _INPUT()
    inp.type = 0  # INPUT_MOUSE
    inp.u.mi = _MOUSEINPUT(
        dx=int(nx),
        dy=int(ny),
        mouseData=0,
        dwFlags=0x0001 | 0x8000 | 0x4000,  # MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
        time=0,
        dwExtraInfo=0,
    )
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


def move_cursor_to(x: int, y: int) -> None:
    """Move cursor accurately across monitors with mixed DPI scaling."""
    if sys.platform != "win32":
        return
    ix, iy = int(x), int(y)
    user32 = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()

    # Windows can mis-scale the first SetCursorPos across monitors with different DPI scales; retry until exact.
    for _ in range(3):
        user32.SetCursorPos(ix, iy)
        if user32.GetCursorPos(ctypes.byref(pt)) and pt.x == ix and pt.y == iy:
            return

    vx = int(user32.GetSystemMetrics(76))
    vy = int(user32.GetSystemMetrics(77))
    vw = int(user32.GetSystemMetrics(78))
    vh = int(user32.GetSystemMetrics(79))
    if vw > 1 and vh > 1:
        nx = max(0, min(65535, int(round((ix - vx) * 65535 / (vw - 1)))))
        ny = max(0, min(65535, int(round((iy - vy) * 65535 / (vh - 1)))))
        _send_virtual_mouse_move(nx, ny)
        user32.SetCursorPos(ix, iy)


class SequenceRunner:
    """Runs a normalized automation sequence (including nested repeat groups & hold depth) on a background thread."""

    def __init__(self, on_minimize: Callable[[], None] | None = None, on_restore: Callable[[], None] | None = None):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set = not paused
        self._thread: threading.Thread | None = None
        self._on_minimize = on_minimize
        self._on_restore = on_restore

        self._status = "idle"  # idle | countdown | running | paused | stopping | completed | stopped | error
        self._preset_name = ""
        self._iteration = 0
        self._total_iterations = 0
        self._infinite = False
        self._step_id: str | None = None
        self._step_label = ""
        self._step_depth = 1
        self._total_steps_per_iter = 0
        self._steps_completed = 0
        self._countdown_remaining = 0.0
        self._started_at: float | None = None
        self._finished_at: float | None = None
        self._stop_reason = ""
        self._error = ""
        self._logs: list[str] = []
        self._live_steps: list[dict[str, Any]] | None = None
        self._live_preset_id: str | None = None

    def _log(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        line = f"[{stamp}] {message}"
        with self._lock:
            self._logs.append(line)
            if len(self._logs) > 500:
                self._logs = self._logs[-500:]

    def clear_logs(self):
        with self._lock:
            self._logs.clear()

    def is_active(self) -> bool:
        with self._lock:
            return self._status in ("countdown", "running", "paused", "stopping")

    def state(self) -> dict[str, Any]:
        with self._lock:
            elapsed = 0.0
            if self._started_at is not None:
                end = self._finished_at if self._finished_at is not None else time.monotonic()
                elapsed = max(0.0, end - self._started_at)
            return {
                "status": self._status,
                "active": self._status in ("countdown", "running", "paused", "stopping"),
                "preset_name": self._preset_name,
                "iteration": self._iteration,
                "total_iterations": self._total_iterations,
                "infinite": self._infinite,
                "step_id": self._step_id,
                "step_label": self._step_label,
                "step_depth": self._step_depth,
                "total_steps": self._total_steps_per_iter,
                "steps_completed": self._steps_completed,
                "countdown_remaining": round(self._countdown_remaining, 2),
                "elapsed": round(elapsed, 2),
                "stop_reason": self._stop_reason,
                "error": self._error,
                "logs": list(self._logs),
            }

    def start(
        self,
        preset: dict[str, Any],
        *,
        stop_key: str = "f8",
        failsafe: bool = True,
        minimize_on_start: bool = False,
        speed: float = 1.0,
    ):
        if pyautogui is None:
            raise RuntimeError("PyAutoGUI is not installed. Run: pip install -r requirements.txt")

        normalized = normalize_preset(preset)
        leaf_count = count_enabled_leaves(normalized["steps"])
        if leaf_count <= 0:
            raise ValueError("Add or enable at least one step before starting.")

        speed = max(0.1, min(5.0, float(speed or 1.0)))
        stop_key = stop_key.lower() if stop_key else "f8"
        vk_code = STOP_KEYS.get(stop_key, STOP_KEYS["f8"])

        with self._lock:
            if self._status in ("countdown", "running", "paused", "stopping"):
                raise RuntimeError("A task is already running.")
            self._stop_event.clear()
            self._pause_event.set()
            self._status = "countdown" if normalized["start_delay"] > 0 else "running"
            self._preset_name = normalized["name"]
            self._iteration = 0
            self._total_iterations = normalized["iterations"]
            self._infinite = normalized["infinite"]
            self._step_id = None
            self._step_label = "Cool-off…" if normalized["start_delay"] > 0 else "Starting…"
            self._step_depth = 1
            self._total_steps_per_iter = leaf_count
            self._steps_completed = 0
            self._countdown_remaining = normalized["start_delay"]
            self._started_at = time.monotonic()
            self._finished_at = None
            self._stop_reason = ""
            self._error = ""
            self._live_preset_id = normalized["id"]
            self._live_steps = list(normalized["steps"])

        self._thread = threading.Thread(
            target=self._run_loop,
            args=(normalized, leaf_count, vk_code, stop_key, bool(failsafe), bool(minimize_on_start), speed),
            daemon=True,
        )
        self._thread.start()

    def pause(self):
        paused = False
        with self._lock:
            if self._status == "running":
                self._pause_event.clear()
                self._status = "paused"
                paused = True
        if paused:
            self._log("Paused.")

    def resume(self):
        resumed = False
        with self._lock:
            if self._status == "paused":
                self._pause_event.set()
                self._status = "running"
                resumed = True
        if resumed:
            self._log("Resumed.")

    def update_live_preset(self, preset: dict[str, Any]):
        """Apply step edits/reorders/deletes made mid-pause to subsequent loops of the same preset."""
        normalized = normalize_preset(preset)
        with self._lock:
            if self._status not in ("countdown", "running", "paused"):
                return
            if self._live_preset_id and normalized["id"] != self._live_preset_id:
                return
            self._live_steps = list(normalized["steps"])
            self._total_steps_per_iter = max(1, count_enabled_leaves(self._live_steps))
            self._total_iterations = normalized["iterations"]
            self._infinite = normalized["infinite"]

    def stop(self, reason: str = "Stopped by user."):
        with self._lock:
            if self._status not in ("countdown", "running", "paused", "stopping"):
                return
            self._stop_reason = reason
            self._status = "stopping"
            self._stop_event.set()
            self._pause_event.set()

    def _check_stop(self, vk_code: int, stop_key: str) -> bool:
        if self._stop_event.is_set():
            return True
        if vk_code and is_vk_pressed(vk_code):
            with self._lock:
                self._stop_reason = f"Stopped via emergency hotkey ({stop_key.upper()})."
                self._stop_event.set()
            return True
        return False

    def _sleep_interruptible(self, seconds: float, vk_code: int, stop_key: str, *, update_countdown: bool = False) -> bool:
        if seconds <= 0:
            return not self._check_stop(vk_code, stop_key)
        deadline = time.monotonic() + seconds
        while True:
            if self._check_stop(vk_code, stop_key):
                return False
            if not self._pause_event.is_set():
                remaining = max(0.0, deadline - time.monotonic())
                while not self._pause_event.is_set():
                    if self._check_stop(vk_code, stop_key):
                        return False
                    time.sleep(0.04)
                deadline = time.monotonic() + remaining
            remaining = deadline - time.monotonic()
            if update_countdown:
                with self._lock:
                    self._countdown_remaining = max(0.0, remaining)
            if remaining <= 0:
                return True
            time.sleep(min(0.03, remaining))

    def _execute_step(self, step: dict[str, Any], vk_code: int, stop_key: str) -> bool:
        """Execute a single leaf step, honoring hold_ms (press depth). Returns False if stopped."""
        action = step["action"]
        hold_s = max(0.0, int(step.get("hold_ms", 0) or 0) / 1000.0)

        if action == "hotkey":
            keys = step.get("keys") or []
            if not keys:
                return True
            if hold_s > 0:
                pressed = []
                try:
                    for k in keys:
                        pyautogui.keyDown(k)
                        pressed.append(k)
                    if not self._sleep_interruptible(hold_s, vk_code, stop_key):
                        return False
                finally:
                    for k in reversed(pressed):
                        try:
                            pyautogui.keyUp(k)
                        except Exception:  # noqa: BLE001
                            pass
            else:
                pyautogui.hotkey(*keys)
        elif action == "key":
            key = step["key"]
            presses = step.get("presses", 1)
            if hold_s > 0:
                for p in range(presses):
                    if self._check_stop(vk_code, stop_key):
                        return False
                    try:
                        pyautogui.keyDown(key)
                        if not self._sleep_interruptible(hold_s, vk_code, stop_key):
                            return False
                    finally:
                        try:
                            pyautogui.keyUp(key)
                        except Exception:  # noqa: BLE001
                            pass
                    if p + 1 < presses and not self._sleep_interruptible(0.03, vk_code, stop_key):
                        return False
            else:
                pyautogui.press(key, presses=presses)
        elif action == "type":
            pyautogui.write(step.get("text", ""), interval=step.get("interval", 0.0))
        elif action == "click":
            x, y = step.get("x"), step.get("y")
            btn = step.get("button", "left")
            clicks = step.get("clicks", 1)
            use_win_move = x is not None and y is not None and _is_real_pyautogui() and sys.platform == "win32"
            if hold_s > 0:
                for c in range(clicks):
                    if self._check_stop(vk_code, stop_key):
                        return False
                    try:
                        if use_win_move:
                            move_cursor_to(x, y)
                        pyautogui.mouseDown(x=x, y=y, button=btn)
                        if not self._sleep_interruptible(hold_s, vk_code, stop_key):
                            return False
                    finally:
                        try:
                            pyautogui.mouseUp(x=x, y=y, button=btn)
                        except Exception:  # noqa: BLE001
                            pass
                    if c + 1 < clicks and not self._sleep_interruptible(0.05, vk_code, stop_key):
                        return False
            else:
                if use_win_move:
                    move_cursor_to(x, y)
                pyautogui.click(x=x, y=y, clicks=clicks, button=btn)
        return True

    def _run_steps(
        self,
        steps: list[dict[str, Any]],
        vk_code: int,
        stop_key: str,
        speed: float,
        depth: int = 1,
    ) -> bool:
        for step in steps:
            if not step.get("enabled", True):
                continue
            if self._check_stop(vk_code, stop_key):
                return False
            while not self._pause_event.is_set():
                if self._check_stop(vk_code, stop_key):
                    return False
                time.sleep(0.04)

            if step["action"] == "group":
                repeats = step.get("repeat", 1)
                sub_steps = step.get("steps", [])
                for r in range(1, repeats + 1):
                    if self._check_stop(vk_code, stop_key):
                        return False
                    self._log(f"{'  ' * (depth - 1)}↳ Group '{step.get('name', 'Sub-steps')}' ({r}/{repeats})")
                    if not self._run_steps(sub_steps, vk_code, stop_key, speed, depth + 1):
                        return False
                delay_s = float(step.get("delay", 0.0)) / speed
                if delay_s > 0 and not self._sleep_interruptible(delay_s, vk_code, stop_key):
                    return False
                continue

            label = describe_step(step)
            with self._lock:
                self._step_id = step["id"]
                self._step_label = label
                self._step_depth = depth

            if step["action"] == "wait":
                wait_s = step["seconds"] / speed
                if not self._sleep_interruptible(wait_s, vk_code, stop_key):
                    return False
            else:
                if not self._execute_step(step, vk_code, stop_key):
                    return False
                delay_s = float(step.get("delay", 0.0)) / speed
                if delay_s > 0 and not self._sleep_interruptible(delay_s, vk_code, stop_key):
                    return False

            with self._lock:
                self._steps_completed += 1
        return True

    def _run_loop(
        self,
        preset: dict[str, Any],
        leaf_count: int,
        vk_code: int,
        stop_key: str,
        failsafe: bool,
        minimize_on_start: bool,
        speed: float,
    ):
        pyautogui.FAILSAFE = failsafe
        if _is_real_pyautogui() and sys.platform == "win32":
            pyautogui.FAILSAFE_POINTS = compute_failsafe_points()
        pyautogui.PAUSE = 0.0
        minimized = False
        try:
            mode = "until stopped" if preset["infinite"] else f"{preset['iterations']} iteration(s)"
            stop_hint = f" · Press {stop_key.upper()} to stop" if vk_code else ""
            self._log(f"Starting '{preset['name']}' ({mode}, {leaf_count} step(s)/loop{stop_hint}).")

            if minimize_on_start and self._on_minimize:
                try:
                    self._on_minimize()
                    minimized = True
                except Exception:  # noqa: BLE001
                    pass

            if preset["start_delay"] > 0:
                self._log(f"Cool-off: {preset['start_delay']:g}s — set up your scene and focus the target window.")
                if not self._sleep_interruptible(preset["start_delay"], vk_code, stop_key, update_countdown=True):
                    self._finish("stopped")
                    return

            with self._lock:
                self._countdown_remaining = 0.0
                self._status = "running"

            iteration = 0
            while True:
                with self._lock:
                    infinite = self._infinite
                    max_iters = self._total_iterations
                    current_steps = list(self._live_steps) if self._live_steps is not None else list(preset["steps"])
                if not infinite and iteration >= max_iters:
                    break
                if not current_steps:
                    self._log("All steps were removed; stopping sequence.")
                    self._finish("stopped")
                    return
                iteration += 1
                total_label = "∞" if infinite else str(max_iters)
                with self._lock:
                    self._iteration = iteration
                self._log(f"Iteration {iteration} / {total_label}")

                if not self._run_steps(current_steps, vk_code, stop_key, speed, depth=1):
                    self._finish("stopped")
                    return

            self._log(f"Completed {iteration} iteration(s).")
            self._finish("completed")
        except pyautogui.FailSafeException:
            with self._lock:
                self._stop_reason = "Stopped by corner failsafe (mouse moved to screen corner)."
            self._log(self._stop_reason)
            self._finish("stopped")
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._error = str(e)
            self._log(f"Error: {e}")
            self._finish("error")
        finally:
            if minimized and self._on_restore:
                try:
                    self._on_restore()
                except Exception:  # noqa: BLE001
                    pass

    def _finish(self, final_status: str):
        with self._lock:
            self._status = final_status
            self._finished_at = time.monotonic()
            self._step_id = None
            self._step_depth = 1
            if final_status == "stopped" and self._stop_reason:
                self._step_label = self._stop_reason
            elif final_status == "completed":
                self._step_label = "Sequence complete"
            elif final_status == "error":
                self._step_label = f"Error: {self._error}"
        if final_status == "stopped" and self._stop_reason:
            with self._lock:
                already_logged = bool(self._logs and self._stop_reason in self._logs[-1])
            if not already_logged:
                self._log(self._stop_reason)
