# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: один файл dist\AmneziaMonitor.exe (без консоли, значок в трее).
# Сборка: powershell -ExecutionPolicy Bypass -File build_exe.ps1
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT = SPECPATH
icon_path = None
try:  # значок генерируется из того же рисунка, что и значок в трее
    import sys
    sys.path.insert(0, ROOT)
    from app_main import make_icon_image
    os.makedirs(os.path.join(ROOT, "build"), exist_ok=True)
    icon_path = os.path.join(ROOT, "build", "AmneziaMonitor.ico")
    make_icon_image().save(icon_path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
except Exception as e:  # без значка .exe всё равно соберётся
    print("WARNING: icon not generated:", e)
    icon_path = None

hidden = [
    "amnezia_monitor", "app_main", "app_settings", "app_window", "secrets_store", "dashboard", "dashboard_api",
    "demo_data", "yaml", "requests", "pystray._win32", "PIL.Image", "PIL.ImageDraw",
    "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr", "clr_loader", "pythonnet",
] + collect_submodules("paramiko")

wv_datas, wv_bins = [], []
try:
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs
    from pathlib import Path
    wv_datas += collect_data_files("webview", subdir="lib")
    wv_bins += collect_dynamic_libs("webview")
    import webview
    lib = Path(webview.__file__).resolve().parent / "lib"
    native = lib / "runtimes" / "win-x64" / "native" / "WebView2Loader.dll"
    if native.is_file():
        wv_datas.append((str(native), "win-x64"))
    for name in ("Microsoft.Web.WebView2.Core.dll", "Microsoft.Web.WebView2.WinForms.dll",
                 "WebBrowserInterop.x64.dll"):
        found = lib / name
        if found.is_file():
            wv_datas.append((str(found), "webview/lib"))
except Exception as e:
    print("WARNING: webview files not collected:", e)

a = Analysis(
    [os.path.join(ROOT, "app_main.py")],
    pathex=[ROOT],
    binaries=wv_bins,
    datas=[(os.path.join(ROOT, "dashboard.html"), ".")] + wv_datas,
    hiddenimports=hidden,
    excludes=["tkinter", "IPython", "matplotlib", "numpy"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="AmneziaMonitor",
    debug=False,
    strip=False,
    upx=False,          # UPX чаще вызывает ложные срабатывания антивирусов
    console=False,      # окно консоли не нужно: управление через значок в трее и веб-панель
    icon=icon_path,
    runtime_tmpdir=None,
)
