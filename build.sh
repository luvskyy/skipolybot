#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# PolymarketBot — Build script for macOS .app + DMG
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP_NAME="PolymarketBot"
VERSION=$(python3 -c "exec(open('version.py').read()); print(VERSION)")
DMG_NAME="${APP_NAME}-${VERSION}-macOS"

echo "═══════════════════════════════════════════════════════"
echo "  Building ${APP_NAME} v${VERSION}"
echo "═══════════════════════════════════════════════════════"

# ── Step 1: Clean previous builds ────────────────────────────────────────────
echo ""
echo "→ Cleaning previous builds..."
rm -rf build/ dist/ "${DMG_NAME}.dmg"

# ── Step 2: Activate venv ────────────────────────────────────────────────────
echo "→ Activating virtual environment..."
if [ -d ".venv" ]; then
    source .venv/bin/activate
elif [ -d "venv" ]; then
    source venv/bin/activate
else
    echo "ERROR: No virtual environment found. Run: python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi

# ── Step 3: Install build dependencies ───────────────────────────────────────
echo "→ Installing build dependencies..."
pip install pyinstaller pywebview --quiet 2>/dev/null || true

# ── Step 4: Run PyInstaller ──────────────────────────────────────────────────
echo "→ Building macOS .app bundle..."
pyinstaller polybot.spec --noconfirm

echo ""
echo "→ .app bundle created at: dist/${APP_NAME}.app"

# ── Step 4b: Ad-hoc codesign ────────────────────────────────────────────────
# The in-app updater (updater.py:_verify_codesign) rejects unsigned bundles
# via `codesign --verify --deep --strict`. Ad-hoc signing (identity "-") is
# enough to pass that check without an Apple Developer cert. Gatekeeper will
# still warn on first launch for end users — for real distribution, swap "-"
# for a "Developer ID Application: ..." identity and add notarization.
echo "→ Ad-hoc codesigning .app..."
codesign --deep --force --sign - "dist/${APP_NAME}.app"
codesign --verify --deep --strict --verbose=2 "dist/${APP_NAME}.app"

# ── Step 5: Create DMG ──────────────────────────────────────────────────────
echo "→ Creating DMG installer..."

# Check if create-dmg is available
if command -v create-dmg &>/dev/null; then
    create-dmg \
        --volname "${APP_NAME}" \
        --volicon "icon.icns" 2>/dev/null \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 100 \
        --icon "${APP_NAME}.app" 175 190 \
        --hide-extension "${APP_NAME}.app" \
        --app-drop-link 425 190 \
        --no-internet-enable \
        "dist/${DMG_NAME}.dmg" \
        "dist/${APP_NAME}.app" \
    || {
        # Fallback: create-dmg without icon (in case icon.icns doesn't exist)
        create-dmg \
            --volname "${APP_NAME}" \
            --window-pos 200 120 \
            --window-size 600 400 \
            --icon-size 100 \
            --icon "${APP_NAME}.app" 175 190 \
            --hide-extension "${APP_NAME}.app" \
            --app-drop-link 425 190 \
            --no-internet-enable \
            "dist/${DMG_NAME}.dmg" \
            "dist/${APP_NAME}.app"
    }
else
    # Fallback: use hdiutil directly
    echo "  (create-dmg not found, using hdiutil fallback)"
    mkdir -p dist/dmg-staging
    cp -R "dist/${APP_NAME}.app" dist/dmg-staging/
    ln -sf /Applications dist/dmg-staging/Applications
    hdiutil create -volname "${APP_NAME}" \
        -srcfolder dist/dmg-staging \
        -ov -format UDZO \
        "dist/${DMG_NAME}.dmg"
    rm -rf dist/dmg-staging
fi

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  BUILD COMPLETE"
echo ""
echo "  .app:  dist/${APP_NAME}.app"
echo "  .dmg:  dist/${DMG_NAME}.dmg"
echo "═══════════════════════════════════════════════════════"
