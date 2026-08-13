"""SimBridge core — shared modules: config, events, audit, ACL, rate limiting."""

import pathlib as _pl

_VERSION_FILE = _pl.Path(__file__).resolve().parent.parent / "VERSION"
if _VERSION_FILE.exists():
    __version__ = _VERSION_FILE.read_text().strip()
else:
    __version__ = "0.0.0+unknown"
