"""Bounded, read-only Polymarket US scanner. No trading credentials or orders."""
import datetime as dt
import json
import re
import time
from pathlib import Path
from polymarket_us import PolymarketUS, RateLimitError

FILE = Path("data.json")
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

def current(item):
    if not isinstance(item, dict) or not item.get("slug"):
        return False
    slug = str(item["slug"])
    # Reject obvious historical date stamps, without assuming every slug has a date.
    for match in re.finditer(r"20\d{2}-\d{2}-\d{2}", slug):
        try:
            if dt.date.fromisoformat(match.group()) < now.date():
                return False
        except ValueError:
            pass
    for key in ("closed", "settled", "resolved"):
        if item.get(key) is True:
            return False
    for key in ("endDate", "endTime", "closeTime", "expirationTime"):
        value = item.get(key)
        if isinstance(value, str):
            try:
                end = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                if end.tzinfo is not None and end < now:
                    return False
            except ValueError:
                pass
    return True

quotes, errors = [], []
listed = eligible = attempted = 0
status = "unknown"
try:
    # Fetch one small page. Rotate offset each run to avoid repeatedly scanning
    # the same stale first page; no aggressive pagination or burst of requests.
    page = int(now.timestamp() // 900) % 10
    offset = page * 30
    try:
        response = client.markets.list({"limit": 30, "offset": offset, "active": True})
        items = response.get("markets", []) if isinstance(response, dict) else []
        listed = len(items)
        candidates = [m for m in items if current(m)]
        eligible = len(candidates)
        print(f"Market page offset={offset}: listed={listed}, eligible={eligible}", flush=True)
        for market in candidates[:4]:
            slug = market["slug"]
            attempted += 1
            try:
                bbo_response = client.markets.bbo(slug)
                bbo = bbo_response.get("marketData") if isinstance(bbo_response, dict) else None
                bbo = bbo if isinstance(bbo, dict) else {}
                bid, ask = number(bbo.get("bestBid")), number(bbo.get("bestAsk"))
                if bid is not None and ask is not None and bid <= ask:
                    quotes.append({"slug": slug, "title": str(market.get("question") or market.get("title") or slug)[:180], "bid": bid, "ask": ask})
                else:
                    errors.append(f"{slug}: no valid two-sided BBO; keys={list(bbo.keys())[:8]}")
            except RateLimitError as exc:
                errors.append(f"{slug}: RateLimitError ({str(exc)[:100]})")
                print("Rate limit encountered; stopping requests for this run.", flush=True)
                break
            except Exception as exc:
                errors.append(f"{slug}: {type(exc).__name__}: {str(exc)[:120]}")
            time.sleep(1.5)
        status = "ok" if quotes else ("no_eligible_markets" if not eligible else "no_valid_quotes")
    except RateLimitError as exc:
        status = "rate_limited"
        errors.append(f"Market listing rate-limited: {str(exc)[:120]}")
    except Exception as exc:
        status = "api_error"
        errors.append(f"Market listing: {type(exc).__name__}: {str(exc)[:150]}")
finally:
    client.close()

# Keep previous quotes only as explicitly stale, never mislabel them as fresh.
old_quotes = old.get("markets", [])
if quotes:
    displayed = quotes
    quote_time = now.isoformat()
else:
    displayed = old_quotes if isinstance(old_quotes, list) else []
    quote_time = old.get("quotes_as_of")
    if displayed:
        status = status + "_stale"

old.update({
    "mode": "PAPER_ONLY",
    "live_trading_enabled": False,
    "strategy": "DATA_COLLECTION_ONLY",
    "cash": 1000,
    "equity": 1000,
    "last_scan": now.isoformat(),
    "quotes_as_of": quote_time,
    "scan_status": status,
    "markets": displayed,
    "diagnostics": {
        "returned_markets": listed,
        "eligible_markets": eligible,
        "slugs_checked": attempted,
        "valid_quotes": len(quotes),
        "sample_errors": errors[:4],
        "rate_limited": any("RateLimit" in e or "rate-limited" in e for e in errors),
    },
})
FILE.write_text(json.dumps(old, ensure_ascii=False, indent=2) + "\n")
print(f"Scan complete: status={status}, new_quotes={len(quotes)}, requests={attempted}, errors={errors[:2]}", flush=True)
