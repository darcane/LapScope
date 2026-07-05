# PyInstaller spec for the plug-and-play Windows build (onedir).
# Build:  pip install -r requirements.txt -r requirements-build.txt
#         pyinstaller LapScope.spec
# Output: dist/LapScope/LapScope.exe (+ bundled runtime, static assets, car list).

from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_submodules

# uvicorn[standard] and websockets import their protocol/loop backends lazily,
# so PyInstaller's static analysis misses them without help.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("websockets")
    + ["wsproto", "httptools", "h11", "anyio", "fastapi", "starlette"]
)

a = Analysis(
    ["run_desktop.py"],
    pathex=["."],
    binaries=[],
    # car_ordinals.json sits next to the app package (routes.py reads
    # parent.parent / "car_ordinals.json"); the static tree is added to COLLECT.
    datas=[("app/car_ordinals.json", "app")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LapScope",
    debug=False,
    strip=False,
    upx=False,
    # Keep the console so users/support can read the "Listening…/Session ended"
    # logs; the dashboard is the real UI, this window is a live status/error log.
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    # Whole frontend tree: HTML/CSS/JS plus the binary assets (fonts/*.woff2,
    # css/uplot.min.css, js/vendor/uplot.iife.min.js). main.py serves this from
    # Path(__file__).parent / "static", i.e. <bundle>/app/static.
    Tree("app/static", prefix="app/static"),
    strip=False,
    upx=False,
    name="LapScope",
)
