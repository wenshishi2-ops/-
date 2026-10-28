"""Conservative research-only paper engine; never sends an order."""
import datetime as dt
import json
from pathlib import Path

FILE = Path("data.json")
d = json.loads(FILE.read_text())
now = dt.datetime.now(dt.timezone.utc)
stamp = now.isoformat()
initial = 1000.0
cash = float(d.get("cash", initial))
positions = d.get("positions", [])
trades = d.get("trades", [])
history = d.get("quote_history", {})
peak = float(d.get("peak_equity", initial))
realized = float(d.get("realized_pnl", 0))
fresh = d.get("scan_status") == "ok" and d.get("quotes_as_of") == d.get("last_scan")
quotes = {q["slug"]: q for q in d.get("markets", [])} if fresh else {}
def money(v): return round(v + 1e-9, 4)
def is_price(v):
    return isinstance(v, (int, float)) and 0 < v < 1

# Mark positions conservatively at bid, not mid or ask. Old marks remain explicitly stale.
for p in positions:
    q = quotes.get(p["slug"])
    if q and is_price(q.get("bid")):
        p["last_bid"] = q["bid"]
        p["marked_at"] = stamp
    p["mark_stale"] = p.get("marked_at") != stamp

# Exit only against a fresh, real two-sided quote; never claim a settlement.
remaining = []
for p in positions:
    q = quotes.get(p["slug"])
    if not q:
        remaining.append(p)
        continue
    bid = float(q["bid"])
    entry = float(p["entry"])
    age_hours = (now - dt.datetime.fromisoformat(p["opened_at"])).total_seconds() / 3600
    if bid >= entry * 1.06 or bid <= entry * 0.92 or age_hours >= 48:
        proceeds = p["shares"] * bid
        cost = p["shares"] * entry
        cash += proceeds
        pnl = proceeds - cost
        realized += pnl
        trades.append({"time": stamp, "slug": p["slug"], "side": "SELL", "price": bid, "shares": p["shares"], "cash_change": money(proceeds), "realized_pnl": money(pnl), "reason": "target" if bid >= entry*1.06 else "stop" if bid <= entry*0.92 else "time"})
    else:
        remaining.append(p)
positions = remaining

# A deliberately simple momentum baseline: needs two distinct, recent observations.
# No forecasting claim. New buys use exactly $300 virtual notional each.
# At most 3 simultaneous positions, $900 maximum opening cost,
# $30 daily realized loss and $100 cumulative realized loss.
daily_pnl = sum(float(t.get("realized_pnl", 0)) for t in trades if t.get("side") == "SELL" and str(t.get("time", "")).startswith(now.date().isoformat()))
exposure = sum(p["shares"] * p["entry"] for p in positions)
halted = realized <= -100 or daily_pnl <= -30
if fresh and not halted:
    for slug, q in quotes.items():
        if len(positions) >= 3 or exposure > 600.000001 or cash < 300:
            break
        bid, ask = float(q["bid"]), float(q["ask"])
        prev = history.get(slug)
        if not prev or any(p["slug"] == slug for p in positions):
            continue
        try:
            prev_time = dt.datetime.fromisoformat(prev["time"])
            prev_mid = float(prev["mid"])
        except (KeyError, ValueError, TypeError):
            continue
        age = (now - prev_time).total_seconds()
        mid = (bid + ask) / 2
        spread = ask - bid
        if not (300 <= age <= 24*3600 and 0.10 <= ask <= 0.85 and spread <= 0.01 and prev_mid > 0 and 0.02 <= (mid/prev_mid - 1) <= 0.08):
            continue
        spend = 300.0
        shares = spend / ask  # fractional paper units: exactly $300 notional
        cost = spend
        cash -= cost
        exposure += cost
        positions.append({"slug": slug, "title": q.get("title", slug), "shares": shares, "entry": ask, "opened_at": stamp, "last_bid": bid, "marked_at": stamp, "mark_stale": False})
        trades.append({"time": stamp, "slug": slug, "side": "BUY", "price": ask, "shares": shares, "cash_change": money(-cost), "reason": "two_snapshot_momentum_baseline"})

if fresh:
    for slug, q in quotes.items():
        history[slug] = {"mid": (float(q["bid"]) + float(q["ask"])) / 2, "time": stamp}
history = dict(list(history.items())[-300:])
market_value = sum(p["shares"] * float(p.get("last_bid", 0)) for p in positions)
equity = cash + market_value
peak = max(peak, equity)
d.update({
    "strategy": "TWO_SNAPSHOT_MOMENTUM_BASELINE",
    "paper_engine": "v2",
    "fixed_paper_buy_usd": 300,
    "live_trading_enabled": False,
    "cash": money(cash),
    "equity": money(equity),
    "market_value": money(market_value),
    "unrealized_pnl": money(sum(p["shares"] * (float(p.get("last_bid", 0)) - p["entry"]) for p in positions)),
    "realized_pnl": money(realized),
    "positions": positions,
    "trades": trades[-500:],
    "quote_history": history,
    "peak_equity": money(peak),
    "max_drawdown_pct": round(max(float(d.get("max_drawdown_pct", 0)), (peak-equity)/peak*100 if peak else 0), 3),
    "paper_status": "HALTED_LOSS_LIMIT" if halted else "ACTIVE" if fresh else "WAITING_FOR_FRESH_QUOTES",
    "paper_updated_at": stamp,
    "pricing_note": "Paper fills at quoted ask/bid without depth, fees, latency or settlement. Stale marks are not executable.",
})
FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print(f"Paper V2: fresh={fresh}, positions={len(positions)}, trades={len(trades)}, cash={cash:.2f}, equity={equity:.2f}", flush=True)
