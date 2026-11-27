# AutoClick

A lightweight Windows desktop app for recording and automating mouse clicks, keyboard shortcuts, and multi-step sequences.

The app opens in its own native window with three tools on the start screen:

1. **Record & Replay**: start with a cool-off countdown to set up your scene, record live mouse clicks and keyboard presses (including combos, hold duration, and real timing), then replay the captured sequence for as many iterations as you need.
2. **Auto Clicker**: configure left/right/middle clicks, single or double clicks, press hold depth, target point (cursor, fixed coordinate, or multi-point list), cool-off countdown, and repeat count.
3. **Sequence Builder**: create, save, import/export, and run multi-step keyboard & mouse sequences with **key press hold depth (ms)** and **nested Repeat Groups (multi-step depth)**. Includes the original v0.5 **Copy Tab URLs** preset out of the box.

## Install (Windows app)

1. Download `AutoClick-Setup-<version>.exe` from the [latest release](https://github.com/iqrarwaqas/AutoClick/releases/latest) and run it. No admin rights are needed.
2. Start **AutoClick** from the Start menu. The app opens in its own window; close the window to quit.

**Updates:** the app checks for a new version when it starts and shows a banner when one is available. You can also check at any time with **⚙ Settings → Check for updates** (or **Check for updates** on the start screen). Click **Update now** to download, install, and relaunch automatically while keeping your saved sequences.

**Emergency Stop:** press **`F8`** (configurable) at any time while a sequence is running, or move your mouse to any screen corner (PyAutoGUI failsafe), to stop immediately. Press **`F9`** (configurable) to stop recording.

**Theme:** pick **System**, **Light**, or **Dark** in **⚙ Settings**.

Windows may show "Windows protected your PC" because the installer isn't code-signed. Click **More info → Run anyway**.

## Setup from source (once)

```bash
pip install -r requirements.txt
```

## Run

```bash
python main.py
```

Options:

| Flag | Meaning |
|---|---|
| `--browser` | Open the app in your web browser instead of a native window |
| `--no-browser` | Run the local server without opening a browser |
| `--port N` | Port of the local server (default `5050`) |
| `--version` | Show the app version and exit |

## Releasing a new version (maintainers)

1. Update `__version__` in `main.py`, e.g. `"1.1.0"`, and commit.
2. Tag and push the tag:
   ```bash
   git tag v1.1.0
   git push origin v1.1.0
   ```
3. The **Release** GitHub Action builds `AutoClick-Setup-1.1.0.exe` and publishes it as a GitHub Release. Installed apps see the update the next time they start.

The tag must match `__version__`, or the build fails.

To build the installer locally, install [Inno Setup 6](https://jrsoftware.org/isdl.php) and run:

```bash
pip install pyinstaller
pyinstaller packaging/AutoClick.spec --noconfirm
iscc /DAppVersion=1.0.0 installer\AutoClick.iss
```

The installer is written to `dist/`.

To regenerate the `.ico` after editing `static/icon.svg`:

```bash
python packaging/make_icon.py
```
