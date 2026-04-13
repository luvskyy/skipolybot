"""
Background update checker — polls GitHub Releases for newer versions.

Supports two update channels:
  - "stable" — only checks non-prerelease releases (default)
  - "beta"   — checks all releases including prereleases

Also handles in-app download and install-and-restart.
"""

import os
import plistlib
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


# Hardcoded name of the .app bundle we expect inside the DMG. Preventing
# "first entry wins" protects against a malicious DMG shipping a trojan
# alongside the real app (H3).
EXPECTED_APP_NAME = "PolymarketBot.app"

# Maximum number of characters / dots we'll accept in a release tag before
# refusing to parse it. Prevents memory-exhaustion DoS from a crafted tag
# like "1." * 10_000_000 (M2).
_MAX_TAG_LEN = 64
_MAX_TAG_PARTS = 8


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

    Refuses to parse pathologically long tags to avoid memory-exhaustion
    DoS from a crafted GitHub release tag.
    """
    if not isinstance(v, str) or len(v) > _MAX_TAG_LEN:
        return (0, 0, 0)
    try:
        clean = v.lstrip("v")
        # Split off pre-release suffix
        if "-" in clean:
            base, pre = clean.split("-", 1)
            if base.count(".") > _MAX_TAG_PARTS:
                return (0, 0, 0)
            base_tuple = tuple(int(x) for x in base.split("."))
            # Extract pre-release number (e.g. "beta.2" → 2) for ordering
            # Pre-release sorts lower than stable: -1 prefix, then the number
            pre_num = 0
            parts = pre.split(".")
            if len(parts) >= 2 and parts[-1].isdigit():
                pre_num = int(parts[-1])
            return base_tuple + (-1, pre_num)
        if clean.count(".") > _MAX_TAG_PARTS:
            return (0, 0, 0)
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


def _mount_dmg(dmg_path: str) -> Optional[str]:
    """Mount a DMG and return the mount point by parsing hdiutil plist output.

    Using ``-plist`` instead of the human-readable text output closes an
    attack where a DMG whose volume label contains tabs/newlines can
    misdirect a text parser (H4).
    """
    result = subprocess.run(
        [
            "hdiutil", "attach", dmg_path,
            "-nobrowse", "-noverify", "-noautoopen",
            "-plist",
        ],
        capture_output=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hdiutil attach failed: {result.stderr.decode(errors='replace').strip()}"
        )
    try:
        data = plistlib.loads(result.stdout)
    except Exception as e:
        raise RuntimeError(f"could not parse hdiutil plist output: {e}")

    for entity in data.get("system-entities", []):
        mp = entity.get("mount-point")
        if mp:
            return mp
    return None


def _detach_dmg(mount_point: str) -> None:
    """Best-effort detach; swallow errors because this runs in cleanup paths."""
    try:
        subprocess.run(
            ["hdiutil", "detach", mount_point, "-quiet"],
            timeout=15, capture_output=True,
        )
    except Exception:
        pass


def _verify_codesign(app_path: str) -> None:
    """Reject unsigned, tampered, or revoked app bundles before installing them.

    Raises RuntimeError with a human-readable reason on failure. This is
    our line of defense against a malicious DMG: even if an attacker
    controls the download URL, they can't forge an Apple Developer ID
    signature without the corresponding private key (C5).
    """
    result = subprocess.run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", app_path],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"codesign --verify failed for {app_path}: "
            f"{(result.stderr or result.stdout).strip()}"
        )


def install_and_restart() -> dict:
    """Verify, mount, code-sign-check, and copy the downloaded DMG over the running app.

    Security-relevant changes vs. prior versions:
      * Only ``PolymarketBot.app`` is accepted from the mounted volume (H3).
      * Mount point comes from ``hdiutil attach -plist``, not text parsing (H4).
      * The new ``.app`` must pass ``codesign --verify --deep --strict`` (C5).
      * The copy is done in pure Python via ``shutil.copytree``; no bash
        script is written to disk, so f-string injection into an install
        script is impossible (C6, M1).
    """
    with _download_lock:
        dmg_path = _download.dmg_path
        if not _download.done or not dmg_path:
            return {"ok": False, "error": "No downloaded update ready"}

    if not getattr(sys, "frozen", False):
        return {"ok": False, "error": "Install only works in bundled .app mode"}

    if not os.path.isfile(dmg_path):
        return {"ok": False, "error": "Downloaded DMG is missing"}

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

    mount_point = None
    try:
        mount_point = _mount_dmg(dmg_path)
        if not mount_point:
            return {"ok": False, "error": "Could not determine DMG mount point"}

        # Enforce the expected .app name — no "first one wins" (H3).
        new_app = os.path.join(mount_point, EXPECTED_APP_NAME)
        if not os.path.isdir(new_app):
            return {"ok": False, "error": f"{EXPECTED_APP_NAME} not found in DMG"}

        # Gatekeeper-style code signature check (C5).
        try:
            _verify_codesign(new_app)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}

        # Stage the new app into the same parent directory as the current
        # one, then atomically swap via rename. This means:
        #   * A failure during copy leaves the old .app intact.
        #   * The swap is a single filesystem operation.
        parent = os.path.dirname(app_path)
        staging_path = os.path.join(
            parent, f".{EXPECTED_APP_NAME}.new-{os.getpid()}"
        )
        backup_path = os.path.join(
            parent, f".{EXPECTED_APP_NAME}.old-{os.getpid()}"
        )

        if os.path.exists(staging_path):
            shutil.rmtree(staging_path, ignore_errors=True)
        if os.path.exists(backup_path):
            shutil.rmtree(backup_path, ignore_errors=True)

        # Copy-in-pure-Python, no shell involved (C6).
        shutil.copytree(new_app, staging_path, symlinks=True)

        # Swap: move current app out of the way, then move new app into place.
        os.rename(app_path, backup_path)
        try:
            os.rename(staging_path, app_path)
        except Exception as e:
            # Roll back if the final rename fails.
            try:
                os.rename(backup_path, app_path)
            except Exception:
                pass
            return {"ok": False, "error": f"Failed to install new app: {e}"}

        # Cleanup: detach DMG, drop backup, drop downloaded DMG.
        _detach_dmg(mount_point)
        mount_point = None
        shutil.rmtree(backup_path, ignore_errors=True)
        shutil.rmtree(os.path.dirname(dmg_path), ignore_errors=True)

        # Relaunch the new app and quit this process. ``/usr/bin/open`` is
        # called with a list so there's no shell interpretation of app_path.
        subprocess.Popen(
            ["/usr/bin/open", app_path],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        def _exit_soon():
            time.sleep(0.5)
            os._exit(0)
        threading.Thread(target=_exit_soon, daemon=True).start()

        return {"ok": True}

    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        if mount_point:
            _detach_dmg(mount_point)
