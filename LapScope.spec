# PyInstaller spec for the plug-and-play Windows build (onedir).
# Build:  pip install -r requirements.txt -r requirements-build.txt
#         pyinstaller LapScope.spec
# Output: dist/LapScope/LapScope.exe (+ bundled runtime, static assets, car
#         list, track catalogue).

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
    # car_ordinals.json and track_catalog.json sit next to the app package
    # (cars.py / tracks.py read Path(__file__).parent / …); the static tree is
    # added to COLLECT. lapscope.ico is bundled as well as being the exe's
    # icon: the launcher window sets its own title-bar icon at runtime through
    # desktop/paths.py, which resolves it under sys._MEIPASS.
    datas=[("app/car_ordinals.json", "app"), ("app/track_catalog.json", "app"),
           ("assets/lapscope.ico", "assets")],
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
    # Windowed: the launcher (desktop/ui.py) is the status/error surface now,
    # and it writes the same lines to DATA_DIR/logs/lapscope.log so support can
    # ask for a file instead of a screenshot of a console.
    #
    # Anything that prints, or writes to a stream, breaks under this — stdout
    # and stderr are None. run_desktop.py guards them, and desktop/server.py
    # passes log_config=None because uvicorn's default logging config calls
    # sys.stdout.isatty() while building its formatter and would otherwise take
    # the whole exe down before the window ever appeared.
    console=False,
    icon="assets/lapscope.ico",
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
