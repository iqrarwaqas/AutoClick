# PyInstaller build for AutoClick.
# Run from the repo root:  pyinstaller packaging/AutoClick.spec --noconfirm
# Output: dist/AutoClick/AutoClick.exe (a folder build, packed by installer/AutoClick.iss)

from pathlib import Path

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "templates"), "templates"), (str(ROOT / "static"), "static")],
    hiddenimports=["pyautogui", "pyscreeze", "pytweening", "pygetwindow", "pymsgbox", "mouseinfo"],
    # Only the Windows (WinForms + WebView2) backend of pywebview is used.
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "PyQt5", "PyQt6", "PySide2", "PySide6", "gi", "cefpython3"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoClick",
    console=False,  # a desktop app: the pywebview window is the whole UI
    icon=str(ROOT / "packaging" / "icon.ico"),
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="AutoClick", upx=False)
