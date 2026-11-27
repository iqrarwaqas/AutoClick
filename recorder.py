"""Global Windows keyboard & mouse recorder with cool-off countdown and hold-depth capture.

Uses native Win32 low-level hooks (WH_KEYBOARD_LL / WH_MOUSE_LL) via ctypes so no
extra dependencies or admin privileges are required.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Callable

# Win32 constants
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_QUIT = 0x0012

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MBUTTONDOWN = 0x0207
WM_MBUTTONUP = 0x0208

LLKHF_INJECTED = 0x10
LLMHF_INJECTED = 0x01

MODIFIER_VKS: dict[int, str] = {
    0x10: "shift", 0xA0: "shift", 0xA1: "shift",
    0x11: "ctrl", 0xA2: "ctrl", 0xA3: "ctrl",
    0x12: "alt", 0xA4: "alt", 0xA5: "alt",
    0x5B: "win", 0x5C: "win",
}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "win")

SPECIAL_VKS: dict[int, str] = {
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
    0x2C: "printscreen",
    0x2D: "insert",
    0x2E: "delete",
    0x6A: "multiply",
    0x6B: "add",
    0x6D: "subtract",
    0x6E: "decimal",
    0x6F: "divide",
    0x90: "numlock",
    0x91: "scrolllock",
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
for _i in range(1, 25):
    SPECIAL_VKS[0x6F + _i] = f"f{_i}"
for _i in range(10):
    SPECIAL_VKS[0x60 + _i] = f"num{_i}"


def vk_to_name(vk: int) -> str | None:
    if vk in MODIFIER_VKS:
        return MODIFIER_VKS[vk]
    if vk in SPECIAL_VKS:
        return SPECIAL_VKS[vk]
    if 0x30 <= vk <= 0x39:  # 0-9
        return chr(vk)
    if 0x41 <= vk <= 0x5A:  # A-Z
        return chr(vk).lower()
    return None


if sys.platform == "win32":
    ULONG_PTR = ctypes.c_size_t
    LRESULT = ctypes.c_ssize_t
    WPARAM = ctypes.c_size_t
    LPARAM = ctypes.c_ssize_t

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", wintypes.POINT),
            ("mouseData", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32

    _user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
    _user32.SetWindowsHookExW.restype = wintypes.HHOOK
    _user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, WPARAM, LPARAM]
    _user32.CallNextHookEx.restype = LRESULT
    _user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    _user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    _user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    _user32.GetMessageW.restype = wintypes.BOOL
    _user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
    _user32.PostThreadMessageW.restype = wintypes.BOOL
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD


class InputRecorder:
    """Records live Windows keyboard and mouse actions after a cool-off countdown."""

    def __init__(self, on_minimize: Callable[[], None] | None = None, on_restore: Callable[[], None] | None = None):
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._hook_thread_id: int | None = None
        self._on_minimize = on_minimize
        self._on_restore = on_restore

        self._status = "idle"  # idle | cooloff | recording | done | error
        self._mode = "both"  # both | keyboard | mouse
        self._stop_key = "f9"
        self._stop_vk = 0x78
        self._capture_delays = True
        self._cooloff_remaining = 0.0
        self._started_at: float | None = None
        self._steps: list[dict[str, Any]] = []
        self._error = ""
        self._logs: list[str] = []

        # Live event tracking state (accessed only on hook thread + locked on read)
        self._pressed_mods: set[str] = set()
        self._down_keys: dict[str, dict[str, Any]] = {}
        self._down_mouse: dict[str, dict[str, Any]] = {}
        self._last_event_at: float | None = None
        self._kb_proc = None
        self._ms_proc = None

    def _log(self, msg: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        with self._lock:
            self._logs.append(f"[{stamp}] {msg}")
            if len(self._logs) > 200:
                self._logs = self._logs[-200:]

    def is_active(self) -> bool:
        with self._lock:
            return self._status in ("cooloff", "recording")

    def state(self) -> dict[str, Any]:
        with self._lock:
            elapsed = 0.0
            if self._started_at is not None and self._status == "recording":
                elapsed = max(0.0, time.monotonic() - self._started_at)
            return {
                "status": self._status,
                "active": self._status in ("cooloff", "recording"),
                "mode": self._mode,
                "stop_key": self._stop_key,
                "cooloff_remaining": round(self._cooloff_remaining, 2),
                "elapsed": round(elapsed, 2),
                "step_count": len(self._steps),
                "steps": list(self._steps),
                "error": self._error,
                "logs": list(self._logs),
            }

    def clear(self):
        with self._lock:
            if self._status in ("cooloff", "recording"):
                return
            self._steps.clear()
            self._logs.clear()
            self._status = "idle"
            self._error = ""

    def start(
        self,
        *,
        mode: str = "both",
        cooloff: float = 5.0,
        stop_key: str = "f9",
        stop_vk: int = 0x78,
        capture_delays: bool = True,
        minimize_on_start: bool = False,
    ):
        if sys.platform != "win32":
            raise RuntimeError("Live input recording is supported on Windows.")
        mode = mode if mode in ("both", "keyboard", "mouse") else "both"
        cooloff = max(0.0, min(60.0, float(cooloff)))

        with self._lock:
            if self._status in ("cooloff", "recording"):
                raise RuntimeError("Recording is already in progress.")
            self._stop_event.clear()
            self._status = "cooloff" if cooloff > 0 else "recording"
            self._mode = mode
            self._stop_key = stop_key
            self._stop_vk = stop_vk
            self._capture_delays = bool(capture_delays)
            self._cooloff_remaining = cooloff
            self._started_at = None
            self._steps = []
            self._error = ""
            self._logs = []
            self._pressed_mods = set()
            self._down_keys = {}
            self._down_mouse = {}
            self._last_event_at = None

        self._thread = threading.Thread(
            target=self._worker,
            args=(mode, cooloff, stop_key, stop_vk, bool(capture_delays), bool(minimize_on_start)),
            daemon=True,
        )
        self._thread.start()

    def stop(self, wait: bool = False):
        self._stop_event.set()
        tid = self._hook_thread_id
        if tid and sys.platform == "win32":
            try:
                _user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if wait:
            thread = self._thread
            if thread is not None and thread is not threading.current_thread() and thread.is_alive():
                thread.join(timeout=0.5)

    def _record_step(self, step: dict[str, Any], event_time: float):
        with self._lock:
            if self._steps and self._last_event_at is not None and self._capture_delays:
                gap = round(max(0.0, event_time - self._last_event_at), 2)
                if gap > 0:
                    prev = self._steps[-1]
                    if prev["action"] != "wait":
                        prev["delay"] = gap
            self._steps.append(step)
            self._last_event_at = event_time

    def _worker(
        self,
        mode: str,
        cooloff: float,
        stop_key: str,
        stop_vk: int,
        capture_delays: bool,
        minimize_on_start: bool,
    ):
        minimized = False
        kb_hook = None
        ms_hook = None
        try:
            mode_label = {"both": "Clicks & Keys", "keyboard": "Keyboard only", "mouse": "Mouse clicks only"}[mode]
            stop_hint = f"Press {stop_key.upper()} to stop." if stop_vk else "Click Stop to finish."
            self._log(f"Preparing to record ({mode_label}). {stop_hint}")

            if minimize_on_start and self._on_minimize:
                try:
                    self._on_minimize()
                    minimized = True
                except Exception:  # noqa: BLE001
                    pass

            if cooloff > 0:
                self._log(f"Cool-off: {cooloff:g}s — set up your window and scene now.")
                deadline = time.monotonic() + cooloff
                while True:
                    if self._stop_event.is_set():
                        self._finish("idle", "Recording cancelled during cool-off.")
                        return
                    if stop_vk and (_user32.GetAsyncKeyState(stop_vk) & 0x8000):
                        self._finish("idle", f"Cancelled with {stop_key.upper()}.")
                        return
                    remaining = deadline - time.monotonic()
                    with self._lock:
                        self._cooloff_remaining = max(0.0, remaining)
                    if remaining <= 0:
                        break
                    time.sleep(min(0.03, remaining))

            with self._lock:
                self._cooloff_remaining = 0.0
                self._status = "recording"
                self._started_at = time.monotonic()
            self._log(f"Recording started! Perform your actions now. {stop_hint}")

            self._hook_thread_id = _kernel32.GetCurrentThreadId()
            rec_kb = mode in ("both", "keyboard")
            rec_ms = mode in ("both", "mouse")

            def on_keyboard(nCode: int, wParam: int, lParam: int) -> int:
                if nCode >= 0:
                    kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if not (kb.flags & LLKHF_INJECTED):
                        vk = int(kb.vkCode)
                        now = time.monotonic()
                        is_down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        is_up = wParam in (WM_KEYUP, WM_SYSKEYUP)

                        # Stop key always works even in mouse-only recording mode.
                        if stop_vk and vk == stop_vk and is_down:
                            self.stop()
                            return _user32.CallNextHookEx(kb_hook, nCode, wParam, lParam)

                        if rec_kb:
                            name = vk_to_name(vk)
                            if name:
                                if vk in MODIFIER_VKS:
                                    if is_down:
                                        self._pressed_mods.add(name)
                                    elif is_up:
                                        self._pressed_mods.discard(name)
                                else:
                                    if is_down and name not in self._down_keys:
                                        mods = [m for m in MODIFIER_ORDER if m in self._pressed_mods]
                                        self._down_keys[name] = {"mods": mods, "t": now}
                                    elif is_up and name in self._down_keys:
                                        entry = self._down_keys.pop(name)
                                        hold_ms = max(0, int(round((now - entry["t"]) * 1000)))
                                        # Only store explicit hold_ms when held noticeably (>= 80ms), else default crisp press
                                        stored_hold = hold_ms if hold_ms >= 80 else 0
                                        mods = entry["mods"]
                                        if mods:
                                            step = {
                                                "id": uuid.uuid4().hex[:8],
                                                "action": "hotkey",
                                                "keys": [*mods, name],
                                                "hold_ms": stored_hold,
                                                "delay": 0.1,
                                                "enabled": True,
                                                "note": "",
                                            }
                                            self._log(f"Recorded Hotkey: {' + '.join(k.upper() for k in step['keys'])}" + (f" (hold {stored_hold}ms)" if stored_hold else ""))
                                        else:
                                            step = {
                                                "id": uuid.uuid4().hex[:8],
                                                "action": "key",
                                                "key": name,
                                                "presses": 1,
                                                "hold_ms": stored_hold,
                                                "delay": 0.1,
                                                "enabled": True,
                                                "note": "",
                                            }
                                            self._log(f"Recorded Key: {name.upper()}" + (f" (hold {stored_hold}ms)" if stored_hold else ""))
                                        self._record_step(step, entry["t"])
                return _user32.CallNextHookEx(kb_hook, nCode, wParam, lParam)

            def on_mouse(nCode: int, wParam: int, lParam: int) -> int:
                if nCode >= 0 and rec_ms:
                    ms = ctypes.cast(lParam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    if not (ms.flags & LLMHF_INJECTED):
                        now = time.monotonic()
                        x, y = int(ms.pt.x), int(ms.pt.y)
                        btn_map_down = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right", WM_MBUTTONDOWN: "middle"}
                        btn_map_up = {WM_LBUTTONUP: "left", WM_RBUTTONUP: "right", WM_MBUTTONUP: "middle"}
                        if wParam in btn_map_down:
                            btn = btn_map_down[wParam]
                            self._down_mouse[btn] = {"x": x, "y": y, "t": now}
                        elif wParam in btn_map_up:
                            btn = btn_map_up[wParam]
                            entry = self._down_mouse.pop(btn, None)
                            if entry:
                                hold_ms = max(0, int(round((now - entry["t"]) * 1000)))
                                stored_hold = hold_ms if hold_ms >= 80 else 0
                                step = {
                                    "id": uuid.uuid4().hex[:8],
                                    "action": "click",
                                    "x": entry["x"],
                                    "y": entry["y"],
                                    "button": btn,
                                    "clicks": 1,
                                    "hold_ms": stored_hold,
                                    "delay": 0.1,
                                    "enabled": True,
                                    "note": "",
                                }
                                self._log(f"Recorded {btn.title()} Click at ({entry['x']}, {entry['y']})" + (f" (hold {stored_hold}ms)" if stored_hold else ""))
                                self._record_step(step, entry["t"])
                return _user32.CallNextHookEx(ms_hook, nCode, wParam, lParam)

            self._kb_proc = HOOKPROC(on_keyboard)
            self._ms_proc = HOOKPROC(on_mouse)

            # Always install keyboard hook so stop_key works even in mouse-only mode.
            kb_hook = _user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kb_proc, None, 0)
            if not kb_hook:
                raise RuntimeError("Could not install keyboard hook.")
            if rec_ms:
                ms_hook = _user32.SetWindowsHookExW(WH_MOUSE_LL, self._ms_proc, None, 0)
                if not ms_hook:
                    raise RuntimeError("Could not install mouse hook.")

            msg = wintypes.MSG()
            while not self._stop_event.is_set():
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:
                    break

            with self._lock:
                count = len(self._steps)
            self._finish("done", f"Recording stopped. Captured {count} step(s).")
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._error = str(e)
            self._finish("error", f"Recorder error: {e}")
        finally:
            if kb_hook:
                _user32.UnhookWindowsHookEx(kb_hook)
            if ms_hook:
                _user32.UnhookWindowsHookEx(ms_hook)
            self._hook_thread_id = None
            self._kb_proc = None
            self._ms_proc = None
            if minimized and self._on_restore:
                try:
                    self._on_restore()
                except Exception:  # noqa: BLE001
                    pass

    def _finish(self, status: str, message: str):
        with self._lock:
            self._status = status
            self._cooloff_remaining = 0.0
        self._log(message)
