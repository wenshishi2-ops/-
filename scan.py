"""Bounded, read-only Polymarket US scanner. No trading credentials or orders.

Each run:
1. Lists one small page of active markets (page rotates every 15 minutes).
2. Re-quotes markets we already hold or are watching, so momentum can be
   measured on the same market over time and open positions can always exit.
3. Fills the remaining request budget with new candidates from the page.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path
from polymarket_us import PolymarketUS, RateLimitError

FILE = Path("data.json")
PAGE_SIZE = 30
PAGES = 10
MAX_NEW_CANDIDATES = 4      # new markets quoted per run
MAX_WATCH = 8               # previously seen markets re-quoted per run (plus all open positions)
WATCH_TTL_HOURS = 24        # forget a watched market after this long without a valid quote
REQUEST_PAUSE = 2.5         # seconds between BBO requests (API rate-limits faster bursts)
DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")

old = json.loads(FILE.read_text()) if FILE.exists() else {}
now = dt.datetime.now(dt.timezone.utc)
client = PolymarketUS(timeout=12.0, max_retries=0)


def number(value):
    if isinstance(value, dict):
        value = value.get("value")
    try:
        n = float(value)
        return n if 0 < n < 1 else None
    except (ValueError, TypeError):
        return None


def parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return t if t.tzinfo is not None else None
    except ValueError:
        return None


def reject_reason(item):
    """Return why a listed market is not tradable now, or None if it is current."""
    if not isinstance(item, dict) or not item.get("slug"):
        return "missing_slug"
    for match in DATE_RE.finditer(str(item["slug"])):
        try:
            if dt.date.fromisoformat(match.group()) < now.date():
                return "historical_slug"
        except ValueError:
            pass
    if any(item.get(k) is True for k in ("closed", "settled", "resolved", "archived")):
        return "closed_flag"
    for key in ("endDate", "endTime", "closeTime", "expirationTime"):
        end = parse_time(item.get(key))
        if end is not None and end < now:
            return "past_end"
    return None


def title_of(market, slug):
    return str(market.get("question") or market.get("title") or slug)[:180]


# Markets to re-quote: open positions first (always), then the watchlist.
positions = old.get("positions", []) if isinstance(old.get("positions"), list) else []
watch = old.get("watchlist", {}) if isinstance(old.get("watchlist"), dict) else {}
for p in positions:
    watch.setdefault(p["slug"], {"title": p.get("title", p["slug"]), "last_valid": p.get("marked_at") or now.isoformat()})
fresh_cutoff = now - dt.timedelta(hours=WATCH_TTL_HOURS)
held = {p["slug"] for p in positions}
watch = {
    slug: w for slug, w in watch.items()
    if slug in held or (parse_time(w.get("last_valid")) or now) >= fresh_cutoff
}

quotes, errors = [], []
listed = eligible = attempted = 0
sample = []
rejected = {"missing_slug": 0, "historical_slug": 0, "closed_flag": 0, "past_end": 0}
status = "unknown"
rate_limited = False


def quote(slug, title):
    """Fetch one BBO. Returns False if rate-limited (caller should stop)."""
    global attempted
    attempted += 1
    try:
        bbo_response = client.markets.bbo(slug)
        bbo = bbo_response.get("marketData") if isinstance(bbo_response, dict) else None
        bbo = bbo if isinstance(bbo, dict) else {}
        bid, ask = number(bbo.get("bestBid")), number(bbo.get("bestAsk"))
        if bid is not None and ask is not None and bid <= ask:
            quotes.append({"slug": slug, "title": title, "bid": bid, "ask": ask})
            watch[slug] = {"title": title, "last_valid": now.isoformat()}
        else:
            errors.append(f"{slug}: no valid two-sided BBO")
    except RateLimitError as exc:
        errors.append(f"{slug}: RateLimitError ({str(exc)[:100]})")
        print("Rate limit encountered; stopping requests for this run.", flush=True)
        return False
    except Exception as exc:
        errors.append(f"{slug}: {type(exc).__name__}: {str(exc)[:120]}")
    time.sleep(REQUEST_PAUSE)
    return True


try:
    # 1) Re-quote held positions, then most recently valid watched markets.
    ordered = sorted(
        watch.items(),
        key=lambda kv: (kv[0] not in held, -(parse_time(kv[1].get("last_valid")) or now).timestamp()),
    )
    for slug, w in ordered[: max(MAX_WATCH, len(held))]:
        if not quote(slug, w.get("title", slug)):
            rate_limited = True
            break

    # 2) List one page and quote a few new candidates.
    if not rate_limited:
        offset = (int(now.timestamp() // 900) % PAGES) * PAGE_SIZE
        try:
            response = client.markets.list({"limit": PAGE_SIZE, "offset": offset, "active": True, "closed": False})
            items = response.get("markets", []) if isinstance(response, dict) else []
            listed = len(items)
            candidates = []
            for item in items:
                reason = reject_reason(item)
                if reason:
                    rejected[reason] += 1
                else:
                    candidates.append(item)
                if len(sample) < 3 and isinstance(item, dict):
                    sample.append({
                        "slug": str(item.get("slug", ""))[:100],
                        "closed": item.get("closed"),
                        "endDate": str(item.get("endDate", ""))[:35],
                        "rejected": reason,
                    })
            eligible = len(candidates)
            print(f"Market page offset={offset}: listed={listed}, eligible={eligible}, rejected={rejected}", flush=True)
            new = [m for m in candidates if m["slug"] not in watch][:MAX_NEW_CANDIDATES]
            for market in new:
                if not quote(market["slug"], title_of(market, market["slug"])):
                    rate_limited = True
                    break
        except RateLimitError as exc:
            rate_limited = True
            errors.append(f"Market listing rate-limited: {str(exc)[:120]}")
        except Exception as exc:
            errors.append(f"Market listing: {type(exc).__name__}: {str(exc)[:150]}")

    if quotes:
        status = "ok"
    elif rate_limited:
        status = "rate_limited"
    elif errors and not attempted:
        status = "api_error"
    else:
        status = "no_valid_quotes"
finally:
    client.close()

# Keep previous quotes only as explicitly stale, never mislabel them as fresh.
if quotes:
    displayed, quote_time = quotes, now.isoformat()
else:
    displayed = old.get("markets", []) if isinstance(old.get("markets"), list) else []
    quote_time = old.get("quotes_as_of")
    if displayed:
        status += "_stale"

old.update({
    "mode": "PAPER_ONLY",
    "live_trading_enabled": False,
    "last_scan": now.isoformat(),
    "quotes_as_of": quote_time,
    "scan_status": status,
    "markets": displayed,
    "watchlist": watch,
    "diagnostics": {
        "returned_markets": listed,
        "eligible_markets": eligible,
        "market_samples": sample,
        "rejected_reasons": rejected,
        "watched_markets": len(watch),
        "slugs_checked": attempted,
        "valid_quotes": len(quotes),
        "sample_errors": errors[:4],
        "rate_limited": rate_limited,
    },
})
FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n")
print(f"Scan complete: status={status}, quotes={len(quotes)}, requests={attempted}, watched={len(watch)}, errors={errors[:2]}", flush=True)
