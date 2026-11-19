"""In-process Python CDP backend for `invisible_playwright`."""
from __future__ import annotations

from .bridge import CDPBridgeConnection
from .dispatcher import ChromiumBrowserTypeDispatcher
from .engine import (
    CHROMIUM_BINARY_ENV,
    CHROMIUM_CACHE_ENV,
    CHROMIUM_SEAL_ENV,
    DesktopHardwareProfile,
    OpenboxWorkarea,
    chromium_cache_root,
    ensure_chromium_binary,
    load_chromium_seal,
    resolve_chromium_executable,
)

__all__ = [
    "CDPBridgeConnection",
    "CHROMIUM_BINARY_ENV",
    "CHROMIUM_CACHE_ENV",
    "CHROMIUM_SEAL_ENV",
    "ChromiumBrowserTypeDispatcher",
    "DesktopHardwareProfile",
    "OpenboxWorkarea",
    "chromium_cache_root",
    "ensure_chromium_binary",
    "load_chromium_seal",
    "resolve_chromium_executable",
]
