"""Desktop launcher for the packaged LapScope.exe.

Deliberately empty: no re-exports. Every module here except ``ui`` is free of
tkinter so the launcher's logic can be unit-tested on the Linux CI box, which
has no display. A single ``from .ui import ...`` added here would drag tkinter
into every one of those imports and break that — tests/test_desktop_no_tkinter.py
fails loudly if anyone does.
"""
