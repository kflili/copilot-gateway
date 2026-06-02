# pyinstaller.spec — Windows packaging for Copilot Gateway tray app.
#
# Bundles `tray_app.py` (entry), `gateway.py`, `demo.py`, `demo.html` into a
# single `.exe` via PyInstaller `--onefile` semantics. Build with:
#
#     pyinstaller pyinstaller.spec
#
# or via the wrapper: `.\build-windows.ps1` (installs deps + invokes spec).
#
# Hidden-import enumeration is pre-emptive (Δ5): tray_app.py imports pystray
# (line 952, 998), PIL (903), tkinter (932) lazily inside callback functions,
# which PyInstaller's static module-graph analysis can miss. pystray itself
# loads its platform backend by name (`pystray._win32` on Windows) which
# bypasses the import scanner entirely — explicit collect.
#
# Runtime data: `demo.html` is loaded by `demo.py` at runtime via a path
# relative to the script. PyInstaller extracts datas to `sys._MEIPASS` in
# onefile mode, where the entry script's `Path(__file__).parent` resolves.
# `gateway.py` and `demo.py` are bundled as data (not entry points) so
# `tray_app.py:56` (`GATEWAY_PY = HERE / "gateway.py"`) can locate them at
# runtime; tray_app.py spawns gateway.py as a subprocess via `[sys.executable,
# str(GATEWAY_PY), ...]` (tray_app.py:197). NOTE: in a frozen onefile build
# `sys.executable` is the bootloader `.exe`, not a Python interpreter, so the
# subprocess.Popen call cannot directly re-execute a .py script — a follow-up
# PR to tray_app.py is required to detect `getattr(sys, 'frozen', False)` and
# either re-spawn self with a sub-command sentinel or import gateway as a
# module and call its main() in a thread. Tracked as R3 follow-up in
# `docs/design/windows-app/plan.md` § Out of Scope.

# -*- mode: python ; coding: utf-8 -*-

block_cipher = None


a = Analysis(
    ['tray_app.py'],
    pathex=[],
    binaries=[],
    datas=[
        # Runtime assets the bundled processes load by relative path.
        ('demo.html', '.'),
        ('gateway.py', '.'),
        ('demo.py', '.'),
    ],
    hiddenimports=[
        # Lazy imports inside tray_app.py callback functions — PyInstaller's
        # static analysis follows top-level imports only.
        'pystray',
        'pystray._win32',
        'PIL',
        'PIL.Image',
        'PIL.ImageDraw',
        'tkinter',
        'tkinter.font',
        'tkinter.ttk',
        # Used by gateway.py's origin classifier (`_classify_origin` →
        # `ipaddress.ip_network('172.16.0.0/12')`) and tray_app.py:99
        # (`ipaddress.ip_address(host).is_loopback`). Stdlib, statically
        # imported, but listed defensively in case PyInstaller's stdlib
        # module-graph misses it on Windows.
        'ipaddress',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='copilot-gateway',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # tray app: no console window (Plan §Subprocess hygiene)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
