"""Paths, settings and helpers shared across AutoClick."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)  # running as the installed .exe
# PyInstaller unpacks the bundled templates/static to sys._MEIPASS.
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DATA = Path(os.environ.get("APPDATA", Path.home())) / "AutoClick"
if FROZEN:
    # The install folder isn't writable, so keep settings in the user's profile.
    CONFIG_FILE = APP_DATA / "config.json"
else:
    CONFIG_FILE = RESOURCE_DIR / ".autoclick_config.json"
START_TRIES = 5


def start_again_later():
    """Start the installed app again in a moment, and exit this copy.

    Right after an update the installer starts the new version while Windows
    Defender may still be scanning the new files. Loading a library then fails
    ("being used by another process"), so try again a few times before giving up.
    Returns only when it's time to give up (or when running from source).
    """
    tries = int(os.environ.get("AUTOCLICK_START_TRY", "1"))
    if not FROZEN or tries >= START_TRIES:
        return
    time.sleep(2)
    # PYINSTALLER_RESET_ENVIRONMENT: start as a new app, not as a child of this one.
    env = {**os.environ, "AUTOCLICK_START_TRY": str(tries + 1), "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen([sys.executable, *sys.argv[1:]], env=env)
    os._exit(0)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def update_config(**values):
    """Save the given settings, keeping the others."""
    cfg = {**load_config(), **values}
    save_config(cfg)
