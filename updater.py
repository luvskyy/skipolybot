"""
Background update checker — polls GitHub Releases for newer versions.

Supports two update channels:
  - "stable" — only checks non-prerelease releases (default)
  - "beta"   — checks all releases including prereleases
"""

import threading
import time
from dataclasses import dataclass
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


_status = UpdateStatus()
_lock = threading.Lock()
_channel = "stable"  # current update channel


def _parse_version(v: str) -> tuple:
    """Turn '1.2.3' or '1.2.3-beta.1' into a comparable tuple.

    Stable versions compare higher than pre-release versions of the same
    number. E.g., (1,0,1) > (1,0,1,-1) where -1 represents 'beta'.
    """
    try:
        clean = v.lstrip("v")
        # Split off pre-release suffix
        if "-" in clean:
            base, pre = clean.split("-", 1)
            base_tuple = tuple(int(x) for x in base.split("."))
            # Pre-release sorts lower: append -1 so 1.0.1-beta < 1.0.1
            return base_tuple + (-1,)
        return tuple(int(x) for x in clean.split("."))
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
