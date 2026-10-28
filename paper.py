"""V3 cost-aware paper-only engine; never sends an order."""
import os
import datetime as dt
import json
from pathlib import Path

FILE = Path("data.json")
d = json.loads(FILE.read_text())
now = dt.datetime.now(dt.timezone.utc)
stamp = now.isoformat()
initial = 3000.0
fee_rate = max(0, float(os.getenv("PAPER_FEE_RATE", "0.01")))
slip_rate = max(0, float(os.getenv("PAPER_SLIPPAGE_RATE", "0.005")))
decisions = []
# One-time migration of the original $1000 paper account to $3000.
# Treat the difference as a virtual deposit, never as trading P&L.
if float(d.get("initial_capital", 1000.0)) < initial:
    previous_capital = float(d.get("initial_capital", 1000.0))
    deposit = initial - previous_capital
    d["cash"] = float(d.get("cash", previous_capital)) + deposit
    d["equity"] = float(d.get("equity", previous_capital)) + deposit
    d["peak_equity"] = float(d.get("peak_equity", previous_capital)) + deposit
    d["initial_capital"] = initial
    d["virtual_deposits"] = float(d.get("virtual_deposits", 0)) + deposit
    d.setdefault("capital_events", []).append({"time": stamp, "type": "VIRTUAL_DEPOSIT", "amount": deposit})
    print(f"Applied one-time virtual deposit: ${deposit:.2f}", flush=True)
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
    if bid >= entry * (1.06 + 2*(fee_rate+slip_rate)) or bid <= entry * 0.92 or age_hours >= 48:
        proceeds_gross = p["shares"] * bid * (1-slip_rate)
        sell_fee = proceeds_gross * fee_rate
        proceeds = proceeds_gross - sell_fee
        cost = float(p.get("total_entry_cost", p["shares"] * entry))
        cash += proceeds
        pnl = proceeds - cost
        realized += pnl
        trades.append({"time": stamp, "slug": p["slug"], "side": "SELL", "price": bid, "shares": p["shares"], "cash_change": money(proceeds), "realized_pnl": money(pnl), "fee": money(sell_fee), "reason": "target" if bid >= entry*(1.06+2*(fee_rate+slip_rate)) else "stop" if bid <= entry*0.92 else "time"})
    else:
        remaining.append(p)
positions = remaining

# A deliberately simple momentum baseline: needs two distinct, recent observations.
# No forecasting claim. New buys use exactly $300 virtual notional each.
# At most 10 simultaneous positions, $3000 maximum opening cost,
# $30 daily realized loss and $100 cumulative realized loss.
daily_pnl = sum(float(t.get("realized_pnl", 0)) for t in trades if t.get("side") == "SELL" and str(t.get("time", "")).startswith(now.date().isoformat()))
exposure = sum(p["shares"] * p["entry"] for p in positions)
halted = realized <= -100 or daily_pnl <= -30
if fresh and not halted:
    for slug, q in quotes.items():
        if len(positions) >= 10 or exposure > 2700.000001 or cash < 300:
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
        momentum = mid / prev_mid - 1 if prev_mid > 0 else 0
        estimated_edge = momentum - spread/mid - 2*(fee_rate+slip_rate) if mid else -1
        allowed = 300 <= age <= 24*3600 and 0.10 <= ask <= 0.85 and spread <= 0.01 and 0.02 <= momentum <= 0.08 and estimated_edge >= 0.01
        decisions.append({"time": stamp, "slug": slug, "decision": "BUY" if allowed else "WAIT", "momentum_pct": round(momentum*100, 3), "estimated_edge_pct": round(estimated_edge*100, 3), "explanation": "Two public quotes only; cause of price movement unknown"})
        if not allowed:
            continue
        spend = 300.0
        effective_ask = ask*(1+slip_rate)
        shares = spend/(effective_ask*(1+fee_rate))
        entry_fee = shares*effective_ask*fee_rate
        cost = spend
        cash -= cost
        exposure += cost
        positions.append({"slug": slug, "title": q.get("title", slug), "shares": shares, "entry": effective_ask, "quoted_ask": ask, "entry_fee": money(entry_fee), "total_entry_cost": cost, "opened_at": stamp, "last_bid": bid, "marked_at": stamp, "mark_stale": False})
        trades.append({"time": stamp, "slug": slug, "side": "BUY", "price": effective_ask, "shares": shares, "fee": money(entry_fee), "cash_change": money(-cost), "reason": "cost_adjusted_momentum"})

if fresh:
    for slug, q in quotes.items():
        history[slug] = {"mid": (float(q["bid"]) + float(q["ask"])) / 2, "time": stamp}
history = dict(list(history.items())[-300:])
market_value = sum(p["shares"] * float(p.get("last_bid", 0))*(1-slip_rate)*(1-fee_rate) for p in positions)
equity = cash + market_value
peak = max(peak, equity)
d.update({
    "strategy": "COST_ADJUSTED_MOMENTUM",
    "paper_engine": "v3",
    "assumed_fee_rate": fee_rate,
    "assumed_slippage_rate": slip_rate,
    "decision_log": (d.get("decision_log", []) + decisions)[-250:],
    "model_note": "No AI probability model or news attribution. Costs are assumptions, not verified fees.",
    "fixed_paper_buy_usd": 300,
    "initial_capital": initial,
    "live_trading_enabled": False,
    "cash": money(cash),
    "equity": money(equity),
    "market_value": money(market_value),
    "unrealized_pnl": money(sum(p["shares"]*float(p.get("last_bid", 0))*(1-slip_rate)*(1-fee_rate)-float(p.get("total_entry_cost", p["shares"]*p["entry"])) for p in positions)),
    "realized_pnl": money(realized),
    "positions": positions,
    "trades": trades[-500:],
    "quote_history": history,
    "peak_equity": money(peak),
    "max_drawdown_pct": round(max(float(d.get("max_drawdown_pct", 0)), (peak-equity)/peak*100 if peak else 0), 3),
    "paper_status": "HALTED_LOSS_LIMIT" if halted else "ACTIVE" if fresh else "WAITING_FOR_FRESH_QUOTES",
    "paper_updated_at": stamp,
    "pricing_note": "V3 assumes 1% fee and 0.5% adverse slippage per side by default; NOT verified actual rates. No depth, latency, news attribution or settlement. Stale marks are not executable.",
})
FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print(f"Paper V3: fresh={fresh}, positions={len(positions)}, trades={len(trades)}, cash={cash:.2f}, equity={equity:.2f}", flush=True)
