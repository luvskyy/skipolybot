"""
Background update checker — polls GitHub Releases for newer versions.

Supports two update channels:
  - "stable" — only checks non-prerelease releases (default)
  - "beta"   — checks all releases including prereleases

Also handles in-app download and install-and-restart.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import requests

from version import VERSION, GITHUB_RELEASES_URL, GITHUB_ALL_RELEASES_URL, DOWNLOAD_URL


@dataclass
class UpdateStatus:
    checked: bool = False
    available: bool = False
    latest_version: str = ""
    download_url: str = ""
    release_notes: str = ""
    error: str = ""
    channel: str = "stable"  # "stable" or "beta"
    checking: bool = False   # True while a check is in progress


@dataclass
class DownloadStatus:
    downloading: bool = False
    progress: float = 0.0        # 0.0 – 1.0
    total_bytes: int = 0
    downloaded_bytes: int = 0
    done: bool = False
    error: str = ""
    dmg_path: str = ""           # path to downloaded DMG when done


_status = UpdateStatus()
_download = DownloadStatus()
_lock = threading.Lock()
_download_lock = threading.Lock()
_channel = "stable"  # current update channel


def _parse_version(v: str) -> tuple:
    """Turn '1.2.3' or '1.2.3-beta.1' into a comparable tuple.

    Stable versions compare higher than pre-release versions of the same
    number. Append 0 for stable and -1 for pre-release so that
    (1,2,0,0) > (1,2,0,-1), i.e. 1.2.0 > 1.2.0-beta.1.
    """
    try:
        clean = v.lstrip("v")
        # Split off pre-release suffix
        if "-" in clean:
            base, pre = clean.split("-", 1)
            base_tuple = tuple(int(x) for x in base.split("."))
            # Extract pre-release number (e.g. "beta.2" → 2) for ordering
            # Pre-release sorts lower than stable: -1 prefix, then the number
            pre_num = 0
            parts = pre.split(".")
            if len(parts) >= 2 and parts[-1].isdigit():
                pre_num = int(parts[-1])
            return base_tuple + (-1, pre_num)
        return tuple(int(x) for x in clean.split(".")) + (0,)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def set_channel(channel: str):
    """Switch the update channel. Valid values: 'stable', 'beta'."""
    global _channel
    if channel not in ("stable", "beta"):
        return
    with _lock:
        _channel = channel
        _status.channel = channel


def get_channel() -> str:
    with _lock:
        return _channel


def check_for_update() -> UpdateStatus:
    """Hit GitHub Releases API and compare versions."""
    global _status

    with _lock:
        _status.checking = True
        channel = _channel

    try:
        if channel == "stable":
            # Only check the latest non-prerelease
            resp = requests.get(
                GITHUB_RELEASES_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=10,
            )
            if resp.status_code == 404:
                with _lock:
                    _status.checked = True
                    _status.available = False
                    _status.checking = False
                return _status

            resp.raise_for_status()
            data = resp.json()
        else:
            # Beta channel: fetch all releases, pick the newest (including prereleases)
            resp = requests.get(
                GITHUB_ALL_RELEASES_URL,
                headers={"Accept": "application/vnd.github.v3+json"},
                timeout=10,
                params={"per_page": 10},
            )
            if resp.status_code == 404:
                with _lock:
                    _status.checked = True
                    _status.available = False
                    _status.checking = False
                return _status

            resp.raise_for_status()
            releases = resp.json()
            if not releases:
                with _lock:
                    _status.checked = True
                    _status.available = False
                    _status.checking = False
                return _status

            # The first release in the list is the most recent
            data = releases[0]

        latest_tag = data.get("tag_name", "")
        latest_ver = _parse_version(latest_tag)
        current_ver = _parse_version(VERSION)
        is_prerelease = data.get("prerelease", False)

        # Find the macOS DMG asset if available
        dmg_url = DOWNLOAD_URL
        for asset in data.get("assets", []):
            if asset["name"].endswith(".dmg"):
                dmg_url = asset["browser_download_url"]
                break

        with _lock:
            _status.checked = True
            _status.latest_version = latest_tag.lstrip("v")
            _status.download_url = dmg_url
            _status.release_notes = data.get("body", "")[:500]
            _status.available = latest_ver > current_ver
            _status.checking = False

    except Exception as e:
        with _lock:
            _status.checked = True
            _status.error = str(e)
            _status.checking = False

    return _status


def get_status() -> dict:
    with _lock:
        return {
            "checked": _status.checked,
            "available": _status.available,
            "current_version": VERSION,
            "latest_version": _status.latest_version,
            "download_url": _status.download_url,
            "release_notes": _status.release_notes,
            "error": _status.error,
            "channel": _status.channel,
            "checking": _status.checking,
        }


def start_update_check():
    """Run the update check in a background thread on startup."""
    def _check():
        time.sleep(2)  # let the app settle first
        check_for_update()

    t = threading.Thread(target=_check, daemon=True, name="update-checker")
    t.start()


# ── In-app download ─────────────────────────────────────────────────────


def get_download_status() -> dict:
    with _download_lock:
        return {
            "downloading": _download.downloading,
            "progress": round(_download.progress, 4),
            "total_bytes": _download.total_bytes,
            "downloaded_bytes": _download.downloaded_bytes,
            "done": _download.done,
            "error": _download.error,
            "dmg_path": _download.dmg_path,
        }


def start_download() -> dict:
    """Start downloading the DMG in a background thread. Returns immediately."""
    with _lock:
        url = _status.download_url
        version = _status.latest_version
    if not url or not url.endswith(".dmg"):
        return {"ok": False, "error": "No DMG download URL available"}

    with _download_lock:
        if _download.downloading:
            return {"ok": False, "error": "Download already in progress"}
        _download.downloading = True
        _download.progress = 0.0
        _download.total_bytes = 0
        _download.downloaded_bytes = 0
        _download.done = False
        _download.error = ""
        _download.dmg_path = ""

    def _do_download():
        global _download
        try:
            resp = requests.get(url, stream=True, timeout=60)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            with _download_lock:
                _download.total_bytes = total

            # Download to a temp file
            tmp_dir = tempfile.mkdtemp(prefix="polybot_update_")
            filename = f"PolymarketBot-{version}.dmg"
            dmg_path = os.path.join(tmp_dir, filename)

            downloaded = 0
            with open(dmg_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        with _download_lock:
                            _download.downloaded_bytes = downloaded
                            _download.progress = downloaded / total if total else 0.0

            with _download_lock:
                _download.progress = 1.0
                _download.done = True
                _download.downloading = False
                _download.dmg_path = dmg_path

        except Exception as e:
            with _download_lock:
                _download.error = str(e)
                _download.downloading = False

    t = threading.Thread(target=_do_download, daemon=True, name="update-download")
    t.start()
    return {"ok": True}


def install_and_restart() -> dict:
    """Mount the downloaded DMG, copy the .app over the current one, and relaunch."""
    with _download_lock:
        dmg_path = _download.dmg_path
        if not _download.done or not dmg_path:
            return {"ok": False, "error": "No downloaded update ready"}

    if not getattr(sys, "frozen", False):
        return {"ok": False, "error": "Install only works in bundled .app mode"}

    # Locate current .app bundle
    exe_path = os.path.realpath(sys.executable)
    parts = exe_path.split("/")
    app_path = None
    for i, part in enumerate(parts):
        if part.endswith(".app"):
            app_path = "/".join(parts[: i + 1])
            break

    if not app_path or not os.path.exists(app_path):
        return {"ok": False, "error": "Cannot locate current .app bundle"}

    try:
        # Mount the DMG
        mount_result = subprocess.run(
            ["hdiutil", "attach", dmg_path, "-nobrowse", "-noverify", "-noautoopen"],
            capture_output=True, text=True, timeout=30,
        )
        if mount_result.returncode != 0:
            return {"ok": False, "error": f"Failed to mount DMG: {mount_result.stderr.strip()}"}

        # Find the mount point (last line, third column)
        mount_point = None
        for line in mount_result.stdout.strip().split("\n"):
            cols = line.split("\t")
            if len(cols) >= 3:
                mount_point = cols[-1].strip()
        if not mount_point:
            return {"ok": False, "error": "Could not determine DMG mount point"}

        # Find the .app inside the mounted volume
        new_app = None
        for item in os.listdir(mount_point):
            if item.endswith(".app"):
                new_app = os.path.join(mount_point, item)
                break
        if not new_app:
            subprocess.run(["hdiutil", "detach", mount_point, "-quiet"], timeout=10)
            return {"ok": False, "error": "No .app found in DMG"}

        # Write a script that waits for us to exit, then copies and relaunches
        script = tempfile.NamedTemporaryFile(
            mode="w", suffix=".sh", prefix="polybot_install_", delete=False
        )
        script.write(f"""#!/bin/bash
# Wait for old process to exit
sleep 1
# Remove old app
rm -rf "{app_path}"
# Copy new app
cp -R "{new_app}" "{app_path}"
# Detach DMG
hdiutil detach "{mount_point}" -quiet 2>/dev/null
# Clean up temp
rm -rf "{os.path.dirname(dmg_path)}"
# Relaunch
open "{app_path}"
# Self-delete
rm -f "$0"
""")
        script.close()
        os.chmod(script.name, 0o755)

        # Launch the install script
        subprocess.Popen(
            ["/bin/bash", script.name],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Schedule exit
        def _exit_soon():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()

        return {"ok": True}

    except Exception as e:
        return {"ok": False, "error": str(e)}
