"""Single source of truth for app version and update config."""

VERSION = "1.2.0-beta.1"
GITHUB_REPO = "luvskyy/skipolybot"  # owner/repo for update checks
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_ALL_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases"
DOWNLOAD_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
