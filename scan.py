"""Read-only Polymarket US market snapshot. Never submits orders."""
import datetime
import json
from pathlib import Path
from polymarket_us import PolymarketUS

DATA = Path("data.json")
previous = json.loads(DATA.read_text()) if DATA.exists() else {}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
client = PolymarketUS()

def price(value):
    """Polymarket US BBO prices are often objects: {'value': '0.52', ...}."""
    if isinstance(value, dict):
        value = value.get("value")
    try:
        number = float(value)
        return number if 0 < number < 1 else None
    except (ValueError, TypeError):
        return None

markets = []
errors = []
raw_count = 0
slugs = 0
valid_bbo = 0
try:
    response = client.markets.list({"limit": 100, "active": True})
    if not isinstance(response, dict):
        raise RuntimeError(f"Unexpected markets.list response type: {type(response).__name__}")
    items = response.get("markets", [])
    if not isinstance(items, list):
        raise RuntimeError(f"Unexpected markets field type: {type(items).__name__}")
    raw_count = len(items)
    print(f"Market API returned {raw_count} markets, response keys: {list(response.keys())}")
    for item in items[:60]:
        if not isinstance(item, dict):
            continue
        slug = item.get("slug")
        if not slug:
            continue
        slugs += 1
        try:
            response_bbo = client.markets.bbo(slug)
            bbo = response_bbo.get("marketData", {}) if isinstance(response_bbo, dict) else {}
            bid, ask = price(bbo.get("bestBid")), price(bbo.get("bestAsk"))
            if bid is None or ask is None or bid > ask:
                continue
            valid_bbo += 1
            markets.append({
                "slug": slug,
                "title": str(item.get("question") or item.get("title") or slug)[:180],
                "bid": bid,
                "ask": ask,
            })
        except Exception as exc:
            if len(errors) < 3:
                errors.append(f"{slug}: {type(exc).__name__}: {str(exc)[:160]}")
finally:
    client.close()

print(f"Scanned {slugs} slugs; valid BBO pairs: {valid_bbo}; sample errors: {errors}")
if raw_count == 0:
    status = "no_markets_returned"
elif not markets:
    status = "no_valid_quotes"
else:
    status = "ok"

previous.update({
    "last_scan": now,
    "scan_status": status,
    "diagnostics": {"returned_markets": raw_count, "slugs_checked": slugs, "valid_quotes": valid_bbo, "sample_errors": errors},
    "markets": markets,
    "mode": "PAPER_ONLY",
    "live_trading_enabled": False,
    "strategy": "DATA_COLLECTION_ONLY",
    "cash": 1000,
    "equity": 1000,
})
DATA.write_text(json.dumps(previous, indent=2, ensure_ascii=False) + "\n")
print(f"Saved snapshot with status={status}")
