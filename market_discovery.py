"""
Market discovery — find currently active 15-min BTC Up/Down markets.

Uses the Gamma API (https://gamma-api.polymarket.com) for market metadata,
then the CLOB API for fee rates.
"""

import json
import requests
from datetime import datetime, timezone
from typing import Optional

import config
from models import Market
from utils import log, floor_to_15min, epoch_for_15min_window, current_utc


GAMMA = config.GAMMA_HOST
CLOB = config.CLOB_HOST

# Keywords that identify a 15-min BTC Up/Down market
BTC_KEYWORDS = ["bitcoin", "btc"]
INTERVAL_KEYWORDS = ["15", "fifteen", "15m", "15 min"]
DIRECTION_KEYWORDS = ["up", "down", "higher", "lower", "up or down", "updown"]

# The exact slug prefix for the 15-min BTC Up/Down markets
SLUG_PREFIX_15M = "btc-updown-15m-"


def search_btc_15min_markets(active_only: bool = True) -> list[Market]:
    """
    Search Gamma API for 15-min BTC markets.

    Tries multiple strategies:
    1. Search by keyword in the Gamma markets endpoint
    2. Tag-based filtering
    """
    markets = []

    # ── Strategy 1: Keyword search ──────────────────────────────────────────
    try:
        markets = _search_by_keyword(active_only)
        if markets:
            log.info(f"Found {len(markets)} BTC 15-min market(s) via keyword search")
            return markets
    except Exception as e:
        log.warning(f"Keyword search failed: {e}")

    # ── Strategy 2: Tag-based search ────────────────────────────────────────
    try:
        markets = _search_by_tag(active_only)
        if markets:
            log.info(f"Found {len(markets)} BTC 15-min market(s) via tag search")
            return markets
    except Exception as e:
        log.warning(f"Tag search failed: {e}")

    # ── Strategy 3: Browse all active crypto markets ────────────────────────
    try:
        markets = _search_all_active(active_only)
        if markets:
            log.info(f"Found {len(markets)} BTC 15-min market(s) via active scan")
            return markets
    except Exception as e:
        log.warning(f"Active scan failed: {e}")

    log.warning("No 15-min BTC markets found with any strategy")
    return []


def get_current_market() -> Optional[Market]:
    """
    Get the 15-min BTC market whose window is happening RIGHT NOW.

    The slug pattern is btc-updown-15m-{unix_start}, where unix_start is the
    beginning of the 15-min window (aligned to :00/:15/:30/:45).  We compute
    the current window's expected slug and look it up directly, then fall back
    to scanning if the direct lookup misses.
    """
    now = current_utc()
    now_ts = int(now.timestamp())

    # Compute the start of the current 15-min window (floor to 900s boundary)
    window_start_ts = (now_ts // 900) * 900
    expected_slug = f"btc-updown-15m-{window_start_ts}"

    # ── Strategy A: Direct slug lookup ─────────────────────────────────────
    market = _lookup_by_slug(expected_slug)
    if market:
        log.info(f"Found current 15-min market via slug: {market.question}")
        return market

    # ── Strategy B: Try the previous window (in case of clock skew / late creation)
    prev_slug = f"btc-updown-15m-{window_start_ts - 900}"
    market = _lookup_by_slug(prev_slug)
    if market and not market.is_expired:
        log.info(f"Found previous-window market (still active): {market.question}")
        return market

    # ── Strategy C: Fall back to search + filter by end_date ───────────────
    markets = search_btc_15min_markets(active_only=True)
    if not markets:
        return None

    active = [m for m in markets if m.active and not m.is_expired]
    if not active:
        active = markets

    active.sort(key=lambda m: m.end_date or datetime.max.replace(tzinfo=timezone.utc))

    # Pick the market whose window is currently in progress (ends within 16 min)
    for m in active:
        if m.end_date and m.end_date > now:
            remaining = (m.end_date - now).total_seconds()
            if remaining <= 16 * 60:
                return m

    # Nothing in progress — return soonest future market
    log.debug("No market in current window; picking soonest future market")
    return active[0] if active else None


def _lookup_by_slug(slug: str) -> Optional[Market]:
    """Look up a market directly by slug via the Gamma API."""
    try:
        resp = requests.get(
            f"{GAMMA}/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return _parse_market(data[0])
    except Exception as e:
        log.debug(f"Slug lookup failed for '{slug}': {e}")
    return None


# ── Internal Search Strategies ───────────────────────────────────────────────

def _search_by_keyword(active_only: bool) -> list[Market]:
    """Search Gamma API markets endpoint, sorted newest-first to catch short-lived markets."""
    params = {
        "active": "true" if active_only else "false",
        "closed": "false",
        "limit": 100,
        "order": "createdAt",
        "ascending": "false",
    }

    resp = requests.get(f"{GAMMA}/markets", params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if not isinstance(data, list):
        data = data.get("data", data.get("results", []))

    return _filter_btc_15min(data)


def _search_by_tag(active_only: bool) -> list[Market]:
    """Find markets by tag (Bitcoin / Crypto)."""
    # First, find relevant tag IDs
    try:
        resp = requests.get(f"{GAMMA}/tags", timeout=10)
        resp.raise_for_status()
        tags = resp.json()
    except Exception:
        tags = []

    crypto_tag_ids = []
    if isinstance(tags, list):
        for tag in tags:
            label = (tag.get("label", "") or tag.get("name", "")).lower()
            tag_id = tag.get("id", "")
            if any(kw in label for kw in ["crypto", "bitcoin", "btc"]):
                crypto_tag_ids.append(tag_id)

    if not crypto_tag_ids:
        return []

    all_markets = []
    for tag_id in crypto_tag_ids[:3]:  # limit to first 3 tags
        params = {
            "tag_id": tag_id,
            "active": "true" if active_only else "false",
            "closed": "false",
            "limit": 100,
        }
        try:
            resp = requests.get(f"{GAMMA}/markets", params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, list):
                data = data.get("data", data.get("results", []))
            all_markets.extend(data)
        except Exception as e:
            log.debug(f"Tag {tag_id} search failed: {e}")

    return _filter_btc_15min(all_markets)


def _search_all_active(active_only: bool) -> list[Market]:
    """Broad search across all active markets, filtering client-side."""
    all_markets = []
    cursor = None

    for _ in range(5):  # max 5 pages
        params = {
            "active": "true" if active_only else "false",
            "closed": "false",
            "limit": 100,
            "order": "createdAt",
            "ascending": "false",
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(f"{GAMMA}/markets", params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, list):
            page = data
            cursor = None
        else:
            page = data.get("data", data.get("results", []))
            cursor = data.get("next_cursor")

        all_markets.extend(page)

        if not cursor or not page:
            break

    return _filter_btc_15min(all_markets)


def _filter_btc_15min(raw_markets: list[dict]) -> list[Market]:
    """Filter raw API results for 15-min BTC Up/Down markets."""
    results = []
    seen_ids = set()

    for m in raw_markets:
        question = (m.get("question", "") or "").lower()
        description = (m.get("description", "") or "").lower()
        slug = (m.get("slug", "") or "").lower()
        combined_text = f"{question} {description} {slug}"

        # Slug-based match — only 15-min BTC markets (e.g. "btc-updown-15m-1775187900")
        slug_match = slug.startswith(SLUG_PREFIX_15M)

        # Fallback keyword match: must mention BTC + 15-min interval
        has_btc = any(kw in combined_text for kw in BTC_KEYWORDS)
        has_interval = any(kw in combined_text for kw in INTERVAL_KEYWORDS)
        has_direction = any(kw in combined_text for kw in DIRECTION_KEYWORDS)

        if not (slug_match or (has_btc and has_interval and has_direction)):
            continue

        # Parse into Market object
        market = _parse_market(m)
        if market and market.condition_id not in seen_ids:
            seen_ids.add(market.condition_id)
            results.append(market)

    return results


def _parse_market(raw: dict) -> Optional[Market]:
    """Parse a raw Gamma API market response into a Market model."""
    condition_id = raw.get("condition_id", "") or raw.get("conditionId", "")
    if not condition_id:
        return None

    # Extract token IDs
    tokens = raw.get("tokens", [])
    clob_token_ids = raw.get("clobTokenIds", raw.get("clob_token_ids", []))
    outcomes_raw = raw.get("outcomes", [])

    # Parse JSON strings if needed (Gamma API returns these as strings)
    if isinstance(clob_token_ids, str):
        try:
            clob_token_ids = json.loads(clob_token_ids)
        except (json.JSONDecodeError, TypeError):
            clob_token_ids = []
    if isinstance(outcomes_raw, str):
        try:
            outcomes_raw = json.loads(outcomes_raw)
        except (json.JSONDecodeError, TypeError):
            outcomes_raw = []

    yes_token = ""
    no_token = ""

    if tokens and isinstance(tokens, list):
        for t in tokens:
            outcome = (t.get("outcome", "") or "").lower()
            token_id = t.get("token_id", "") or t.get("tokenId", "")
            if outcome in ("yes", "up"):
                yes_token = token_id
            elif outcome in ("no", "down"):
                no_token = token_id

    # Fallback: map clobTokenIds using outcomes list (e.g. ["Up","Down"])
    if not yes_token and clob_token_ids and outcomes_raw:
        outcomes_lower = [o.lower() for o in outcomes_raw]
        for i, outcome in enumerate(outcomes_lower):
            if i < len(clob_token_ids):
                if outcome in ("yes", "up") and not yes_token:
                    yes_token = str(clob_token_ids[i])
                elif outcome in ("no", "down") and not no_token:
                    no_token = str(clob_token_ids[i])

    # Last resort: assume [YES/Up, NO/Down] ordering
    if not yes_token and clob_token_ids and len(clob_token_ids) >= 1:
        yes_token = str(clob_token_ids[0])
    if not no_token and clob_token_ids and len(clob_token_ids) >= 2:
        no_token = str(clob_token_ids[1])

    if not yes_token or not no_token:
        log.debug(f"Skipping market {condition_id}: missing token IDs")
        return None

    # Parse end date
    end_date = None
    end_str = raw.get("end_date_iso", "") or raw.get("endDate", "")
    if end_str:
        try:
            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # Parse event start time (window open) — used for Pyth historical fallback
    event_start = None
    start_str = raw.get("eventStartTime", "") or raw.get("event_start_time", "")
    if start_str:
        try:
            event_start = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    # Derive from slug if API didn't provide (btc-updown-15m-{unix_ts})
    if event_start is None:
        slug_val = raw.get("slug", "") or ""
        if slug_val.startswith(SLUG_PREFIX_15M):
            try:
                ts = int(slug_val[len(SLUG_PREFIX_15M):])
                event_start = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

    # Get fee rate from CLOB API
    fee_rate_bps = _fetch_fee_rate(yes_token)

    question_text = raw.get("question", "")

    return Market(
        condition_id=condition_id,
        question=question_text,
        slug=raw.get("slug", ""),
        yes_token_id=yes_token,
        no_token_id=no_token,
        end_date=end_date,
        active=bool(raw.get("active", True)),
        neg_risk=bool(raw.get("neg_risk", False) or raw.get("negRisk", False)),
        tick_size=raw.get("minimum_tick_size", "0.01") or "0.01",
        fee_rate_bps=fee_rate_bps,
        market_id=str(raw.get("id", "")),
        group_id=str(raw.get("group_id", "") or raw.get("groupId", "")),
        description=raw.get("description", ""),
        event_start_time=event_start,
    )


def _fetch_fee_rate(token_id: str) -> int:
    """Fetch the fee rate (in bps) for a given token from the CLOB API."""
    try:
        resp = requests.get(
            f"{CLOB}/fee-rate",
            params={"token_id": token_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response might be {"fee_rate_bps": 300} or similar
        return int(data.get("fee_rate_bps", 0) or data.get("feeRateBps", 0))
    except Exception as e:
        log.debug(f"Could not fetch fee rate for {token_id[:20]}...: {e}")
        return 0


def lookup_market_by_slug(slug: str) -> Optional[Market]:
    """Direct lookup of a market by its slug."""
    try:
        resp = requests.get(f"{GAMMA}/markets/slug/{slug}", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        return _parse_market(data)
    except Exception as e:
        log.debug(f"Slug lookup failed for '{slug}': {e}")
        return None


def get_market_by_condition_id(condition_id: str) -> Optional[Market]:
    """Fetch a specific market by condition_id."""
    try:
        resp = requests.get(
            f"{GAMMA}/markets",
            params={"condition_id": condition_id},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return _parse_market(data[0])
        elif isinstance(data, dict):
            results = data.get("data", data.get("results", []))
            if results:
                return _parse_market(results[0])
    except Exception as e:
        log.debug(f"Condition ID lookup failed: {e}")
    return None
