"""Chromium engine seal verification, download, hardware profile, and X11 workarea."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, Optional, Union
import urllib.error
import urllib.request

import platformdirs
from invisible_core.seedmix import sub_seed

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_SEAL_PATH = PACKAGE_DIR / "seal.json"
CHROMIUM_SEAL_ENV = "INVISIBLE_CHROMIUM_SEAL_FILE"
CHROMIUM_CACHE_ENV = "INVISIBLE_CHROMIUM_CACHE_DIR"
CHROMIUM_BINARY_ENV = "INVISIBLE_CHROMIUM_BINARY"

DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_S = (2.0, 8.0)
TRANSIENT_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})

VALID_DEVICE_MEMORY_GIB = (2, 4, 8, 16, 32)

LINUX_INTEL_MESA_RENDERERS: tuple[tuple[str, str], ...] = (
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 620 (KBL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Xe Graphics (TGL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Graphics (ADL GT2), OpenGL 4.6)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Mesa Intel(R) Graphics (RPL-S), OpenGL 4.6)",
    ),
)

LINUX_DESKTOP_SPECS: tuple[tuple[int, int, int], ...] = (
    (8, 8, 30),
    (8, 16, 35),
    (12, 16, 15),
    (16, 16, 8),
    (16, 32, 7),
    (4, 8, 5),
)


def derive_cpu_performance(cores: int) -> int:
    if cores <= 2:
        return 1
    if cores <= 4:
        return 2
    if cores <= 12:
        return 3
    return 4


@dataclass(frozen=True)
class DesktopHardwareProfile:
    """Linux Desktop hardware profile serialized to INVISIBLE_CHROMIUM_PROFILE."""

    hardware_concurrency: int = 8
    device_memory_gb: int = 8
    cpu_performance: Optional[int] = None
    normalize_webgl: bool = True
    webgl_vendor: str = "Google Inc. (Intel)"
    webgl_renderer: str = (
        "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.hardware_concurrency, int)
            or isinstance(self.hardware_concurrency, bool)
            or self.hardware_concurrency < 1
        ):
            raise ValueError("hardware_concurrency must be a positive integer")
        if self.device_memory_gb not in VALID_DEVICE_MEMORY_GIB:
            raise ValueError(
                f"device_memory_gb must be one of {VALID_DEVICE_MEMORY_GIB}"
            )
        if self.cpu_performance is not None:
            if (
                not isinstance(self.cpu_performance, int)
                or isinstance(self.cpu_performance, bool)
                or not (0 <= self.cpu_performance <= 4)
            ):
                raise ValueError("cpu_performance must be in [0, 4]")
        if not isinstance(self.normalize_webgl, bool):
            raise ValueError("normalize_webgl must be a bool")
        if not self.webgl_vendor or not self.webgl_renderer:
            raise ValueError("webgl_vendor and webgl_renderer must be non-empty")

    @property
    def effective_cpu_performance(self) -> int:
        if self.cpu_performance is not None:
            return self.cpu_performance
        return derive_cpu_performance(self.hardware_concurrency)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hardwareConcurrency": self.hardware_concurrency,
            "deviceMemory": self.device_memory_gb,
            "cpuPerformance": self.effective_cpu_performance,
            "normalizeWebgl": self.normalize_webgl,
            "webglVendor": self.webgl_vendor,
            "webglRenderer": self.webgl_renderer,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_seed(
        cls, seed: int, *, pin: Optional[Dict[str, Any]] = None
    ) -> "DesktopHardwareProfile":
        rng = random.Random(sub_seed(seed, "chromium:desktop-hardware"))
        total_weight = sum(w for _, _, w in LINUX_DESKTOP_SPECS)
        pick = rng.uniform(0, total_weight)
        acc = 0.0
        cores, ram_gb = 8, 8
        for c, r, w in LINUX_DESKTOP_SPECS:
            acc += w
            if pick <= acc:
                cores, ram_gb = c, r
                break
        vendor, renderer = rng.choice(LINUX_INTEL_MESA_RENDERERS)
        cpu_perf: Optional[int] = None
        norm_webgl = True

        if pin:
            for k in (
                "hardware.concurrency",
                "navigator.hardwareConcurrency",
                "hardware.cores",
                "hardwareConcurrency",
            ):
                if k in pin and pin[k] is not None:
                    cores = int(pin[k])
                    break
            for k in ("navigator.deviceMemory", "hardware.memory", "deviceMemory"):
                if k in pin and pin[k] is not None:
                    ram_gb = int(pin[k])
                    break
            for k in ("navigator.cpuPerformance", "cpuPerformance"):
                if k in pin and pin[k] is not None:
                    cpu_perf = int(pin[k])
                    break
            for k in ("webgl.vendor", "webglVendor"):
                if k in pin and pin[k]:
                    vendor = str(pin[k])
                    break
            for k in ("webgl.renderer", "webglRenderer"):
                if k in pin and pin[k]:
                    renderer = str(pin[k])
                    break
            if "normalizeWebgl" in pin:
                norm_webgl = bool(pin["normalizeWebgl"])

        return cls(
            hardware_concurrency=cores,
            device_memory_gb=ram_gb,
            cpu_performance=cpu_perf,
            normalize_webgl=norm_webgl,
            webgl_vendor=vendor,
            webgl_renderer=renderer,
        )


def load_chromium_seal(
    seal_path: Optional[Union[str, os.PathLike[str]]] = None,
) -> Dict[str, Any]:
    target = (
        Path(seal_path)
        if seal_path
        else Path(os.environ.get(CHROMIUM_SEAL_ENV) or DEFAULT_SEAL_PATH)
    )
    return json.loads(target.read_text(encoding="utf-8"))


def chromium_cache_root() -> Path:
    override = os.environ.get(CHROMIUM_CACHE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "invisible-chromium"
    return Path(platformdirs.user_cache_dir("invisible-chromium"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_transient_download_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in TRANSIENT_HTTP_CODES
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _download_public_release_archive(url: str, dest_file: Path) -> None:
    asset_name = url.rsplit("/", 1)[-1]
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "invisible-playwright-chromium"}
            )
            with urllib.request.urlopen(req, timeout=600) as resp, dest_file.open(
                "wb"
            ) as out_f:
                shutil.copyfileobj(resp, out_f, length=1024 * 1024)
            return
        except Exception as exc:
            if attempt == DOWNLOAD_ATTEMPTS or not _is_transient_download_error(exc):
                raise
            wait_s = DOWNLOAD_BACKOFF_S[attempt - 1]
            print(
                f"  {asset_name}: attempt {attempt} of {DOWNLOAD_ATTEMPTS} failed "
                f"({exc}); retrying in {wait_s:.0f}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(wait_s)


def _extract_verified_archive(
    archive: Path,
    *,
    expected_sha256: str,
    expected_size: Optional[int],
    dest_dir: Path,
) -> Path:
    actual_size = archive.stat().st_size
    if expected_size is not None and actual_size != expected_size:
        raise RuntimeError(
            f"Chromium archive size mismatch for {archive}: "
            f"expected {expected_size} bytes, got {actual_size} bytes"
        )
    actual_sha = _sha256_file(archive)
    if actual_sha != expected_sha256:
        raise RuntimeError(
            f"Chromium archive SHA256 mismatch for {archive}: "
            f"expected {expected_sha256}, got {actual_sha}"
        )
    dest_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(
        tempfile.mkdtemp(prefix=f".{dest_dir.name}-extract-", dir=str(dest_dir.parent))
    )
    os.chmod(tmp_dir, 0o755)
    try:
        with tarfile.open(archive, mode="r:gz") as tf:
            for member in tf.getmembers():
                member_path = (tmp_dir / member.name).resolve()
                if not str(member_path).startswith(str(tmp_dir.resolve())):
                    raise RuntimeError(f"Unsafe tar member path: {member.name}")
            tf.extractall(tmp_dir, filter="fully_trusted")
        marker = tmp_dir / ".verified-seal-sha256"
        marker.write_text(expected_sha256 + "\n", encoding="utf-8")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        tmp_dir.rename(dest_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    return dest_dir


def resolve_chromium_executable(
    binary_path: Optional[Union[str, Path]] = None,
) -> Path:
    if binary_path:
        resolved = Path(binary_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Chromium binary not found: {resolved}")
        return resolved

    env_bin = os.environ.get(CHROMIUM_BINARY_ENV)
    if env_bin:
        resolved = Path(env_bin).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"{CHROMIUM_BINARY_ENV} points to missing file: {resolved}"
            )
        return resolved

    return ensure_chromium_binary()


def ensure_chromium_binary(
    *, seal_path: Optional[Union[str, os.PathLike[str]]] = None
) -> Path:
    sys_key = f"{platform.system().lower()}-{platform.machine().lower()}"
    if sys_key == "linux-amd64":
        sys_key = "linux-x86_64"

    seal = load_chromium_seal(seal_path)
    asset = seal.get("assets", {}).get(sys_key)
    if not asset:
        raise RuntimeError(
            f"No Chromium binary asset recorded in seal for platform {sys_key}"
        )

    tag = seal["tag"]
    expected_sha = asset["sha256"]
    expected_size = asset.get("size_bytes")
    entry_rel = asset.get("entry_rel", "chrome")

    install_dir = chromium_cache_root() / tag
    marker = install_dir / ".verified-seal-sha256"
    entry = install_dir / entry_rel
    if (
        entry.is_file()
        and marker.is_file()
        and marker.read_text(encoding="utf-8").strip() == expected_sha
    ):
        return entry

    install_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix="chromium-download-",
        suffix=".tar.gz",
        dir=str(install_dir.parent),
        delete=False,
    ) as tmp_f:
        tmp_archive = Path(tmp_f.name)
    try:
        _download_public_release_archive(asset["url"], tmp_archive)
        _extract_verified_archive(
            tmp_archive,
            expected_sha256=expected_sha,
            expected_size=expected_size,
            dest_dir=install_dir,
        )
    finally:
        tmp_archive.unlink(missing_ok=True)

    return install_dir / entry_rel


def detect_needs_no_sandbox() -> bool:
    if (
        os.environ.get("INVISIBLE_CHROMIUM_NO_SANDBOX") == "1"
        or os.environ.get("PROBE_NO_SANDBOX") == "1"
    ):
        return True
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    try:
        val = (
            Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")
            .read_text(encoding="utf-8")
            .strip()
        )
        if val == "1":
            return True
    except OSError:
        pass
    return False


class OpenboxWorkarea:
    """Attaches an Openbox window manager with a desktop panel margin to an Xvfb display."""

    def __init__(
        self,
        display_env: Dict[str, Optional[str]],
        *,
        panel_height: int = 36,
    ) -> None:
        self.display_env = dict(display_env)
        self.display = str(display_env.get("DISPLAY") or "")
        self.panel_height = panel_height
        self._tmp_dir: Optional[Path] = None
        self._proc: Optional[subprocess.Popen[bytes]] = None

    def _x11_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        for k, v in self.display_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
        return env

    def start(self) -> None:
        if not self.display:
            return
        env = self._x11_env()
        xprop_bin = shutil.which("xprop")
        if xprop_bin:
            existing = subprocess.run(
                [xprop_bin, "-root", "-display", self.display, "_NET_WORKAREA"],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            if "_NET_WORKAREA(" in existing.stdout:
                return

        xrdb_bin = shutil.which("xrdb")
        if xrdb_bin:
            subprocess.run(
                [xrdb_bin, "-merge"],
                input=(
                    b"Xft.dpi: 96\nXft.antialias: 1\nXft.hinting: 1\n"
                    b"Xft.hintstyle: hintslight\nXft.rgba: rgb\n"
                ),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

        openbox_bin = shutil.which("openbox")
        if not openbox_bin:
            return

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="invpw-openbox-"))
        rc_path = self._tmp_dir / "openbox-rc.xml"
        rc_path.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<openbox_config xmlns="http://openbox.org/3.4/rc">
  <margins>
    <top>0</top>
    <bottom>{self.panel_height}</bottom>
    <left>0</left>
    <right>0</right>
  </margins>
</openbox_config>
""",
            encoding="utf-8",
        )
        self._proc = subprocess.Popen(
            [openbox_bin, "--sm-disable", "--config-file", str(rc_path)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if xprop_bin:
            for _ in range(30):
                res = subprocess.run(
                    [xprop_bin, "-root", "-display", self.display, "_NET_WORKAREA"],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if "_NET_WORKAREA(" in res.stdout:
                    break
                time.sleep(0.04)
        else:
            time.sleep(0.12)

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=2.0)
        self._proc = None
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None
