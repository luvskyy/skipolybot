"""
Background update checker — polls GitHub Releases for newer versions.
"""

import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from version import VERSION, GITHUB_RELEASES_URL, DOWNLOAD_URL


@dataclass
class UpdateStatus:
    checked: bool = False
    available: bool = False
    latest_version: str = ""
    download_url: str = ""
    release_notes: str = ""
    error: str = ""


_status = UpdateStatus()
_lock = threading.Lock()


def _parse_version(v: str) -> tuple:
    """Turn '1.2.3' into (1, 2, 3) for comparison."""
    try:
        return tuple(int(x) for x in v.lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


def check_for_update() -> UpdateStatus:
    """Hit GitHub Releases API and compare versions."""
    global _status
    try:
        resp = requests.get(
            GITHUB_RELEASES_URL,
            headers={"Accept": "application/vnd.github.v3+json"},
            timeout=10,
        )
        if resp.status_code == 404:
            # No releases yet
            with _lock:
                _status.checked = True
                _status.available = False
            return _status

        resp.raise_for_status()
        data = resp.json()

        latest_tag = data.get("tag_name", "")
        latest_ver = _parse_version(latest_tag)
        current_ver = _parse_version(VERSION)

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

    except Exception as e:
        with _lock:
            _status.checked = True
            _status.error = str(e)

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
        }


def start_update_check():
    """Run the update check in a background thread on startup."""
    def _check():
        time.sleep(2)  # let the app settle first
        check_for_update()

    t = threading.Thread(target=_check, daemon=True, name="update-checker")
    t.start()
