"""V3 cost-aware paper-only engine; never sends an order."""
import os
import datetime as dt
import json
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path

FILE = Path("data.json")
d = json.loads(FILE.read_text())
now = dt.datetime.now(dt.timezone.utc)
stamp = now.isoformat()
initial = 3000.0
fee_theta = max(0, float(os.getenv("PAPER_FEE_THETA", "0.0695")))
slip_rate = max(0, float(os.getenv("PAPER_SLIPPAGE_RATE", "0.005")))

# Strategy parameters (momentum measured against a quote roughly an hour old).
BUY_USD = 300.0
MAX_POSITIONS = 10
MAX_EXPOSURE = 3000.0
LOOKBACK_MIN, LOOKBACK_TARGET, LOOKBACK_MAX = 20, 60, 180   # minutes
SERIES_POINTS = 48
TAKE_PROFIT, STOP_LOSS, MAX_HOLD_HOURS = 0.06, 0.08, 48
DAILY_LOSS_LIMIT, TOTAL_LOSS_LIMIT = 30.0, 100.0
# Round-trip costs are ~2*(theta*(1-p)+slippage), e.g. ~9% at p=0.40, so the
# momentum ceiling must sit above that or no market can ever qualify.
MIN_MOMENTUM, MAX_MOMENTUM, MIN_EDGE = 0.02, 0.20, 0.01


# Polymarket US taker fee (docs.polymarket.us/fees, effective 2026-10-19):
# theta * contracts * p * (1 - p), rounded to the cent with banker's rounding.
# Table Tennis (theta 0.10) is filtered out in scan.py; combos are not traded.
def taker_fee(shares, price):
    exact = Decimal(repr(round(shares * fee_theta * price * (1 - price), 10)))
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN))


def money(v):
    return round(v + 1e-9, 4)


def parse_time(s):
    try:
        return dt.datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def exit_value(shares, bid):
    """Cash received selling at bid after slippage and taker fee."""
    return shares * bid * (1 - slip_rate) - taker_fee(shares, bid)


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
peak = float(d.get("peak_equity", initial))
realized = float(d.get("realized_pnl", 0))
fresh = d.get("scan_status") == "ok" and d.get("quotes_as_of") == d.get("last_scan")
quotes = {q["slug"]: q for q in d.get("markets", [])} if fresh else {}

# Quote history: slug -> list of {"mid", "time"}; migrate the old single-point format.
history = {}
for slug, h in (d.get("quote_history") or {}).items():
    if isinstance(h, dict):
        h = [h]
    if isinstance(h, list):
        history[slug] = [p for p in h if isinstance(p, dict) and "mid" in p and "time" in p]


def lookback_point(slug):
    """The stored quote closest to LOOKBACK_TARGET minutes ago, within the allowed window."""
    best = None
    for point in history.get(slug, []):
        t = parse_time(point.get("time"))
        if t is None:
            continue
        age_min = (now - t).total_seconds() / 60
        if LOOKBACK_MIN <= age_min <= LOOKBACK_MAX:
            score = abs(age_min - LOOKBACK_TARGET)
            if best is None or score < best[0]:
                best = (score, point, age_min)
    return best


# Mark positions conservatively at bid, not mid or ask. Old marks remain explicitly stale.
for p in positions:
    q = quotes.get(p["slug"])
    if q and 0 < q.get("bid", 0) < 1:
        p["last_bid"] = q["bid"]
        p["marked_at"] = stamp
    p["mark_stale"] = p.get("marked_at") != stamp

# Exit only against a fresh, real two-sided quote; never claim a settlement.
remaining = []
for p in positions:
    q = quotes.get(p["slug"])
    opened = parse_time(p.get("opened_at")) or now
    age_hours = (now - opened).total_seconds() / 3600
    if not q:
        p["exit_pending"] = age_hours >= MAX_HOLD_HOURS
        remaining.append(p)
        continue
    bid = float(q["bid"])
    entry = float(p["entry"])
    target = entry * (1 + TAKE_PROFIT + 2 * (fee_theta * (1 - entry) + slip_rate))
    reason = "target" if bid >= target else "stop" if bid <= entry * (1 - STOP_LOSS) else "time" if age_hours >= MAX_HOLD_HOURS else None
    if reason:
        proceeds = exit_value(p["shares"], bid)
        cost = float(p.get("total_entry_cost", p["shares"] * entry))
        pnl = proceeds - cost
        cash += proceeds
        realized += pnl
        trades.append({"time": stamp, "slug": p["slug"], "side": "SELL", "price": bid, "shares": p["shares"],
                       "cash_change": money(proceeds), "realized_pnl": money(pnl),
                       "fee": money(taker_fee(p["shares"], bid)), "reason": reason})
    else:
        p.pop("exit_pending", None)
        remaining.append(p)
positions = remaining

# A deliberately simple momentum baseline. No forecasting claim.
# Fixed $300 virtual notional per buy, at most 10 positions / $3000 opening cost,
# halted after $30 daily or $100 cumulative realized loss.
today = now.date().isoformat()
daily_pnl = sum(float(t.get("realized_pnl", 0)) for t in trades
                if t.get("side") == "SELL" and str(t.get("time", "")).startswith(today))
exposure = sum(float(p.get("total_entry_cost", p["shares"] * p["entry"])) for p in positions)
halted = realized <= -TOTAL_LOSS_LIMIT or daily_pnl <= -DAILY_LOSS_LIMIT
sold_now = {t["slug"] for t in trades if t.get("side") == "SELL" and t.get("time") == stamp}
if fresh and not halted:
    for slug, q in quotes.items():
        if len(positions) >= MAX_POSITIONS or exposure + BUY_USD > MAX_EXPOSURE + 1e-6 or cash < BUY_USD:
            break
        if slug in sold_now or any(p["slug"] == slug for p in positions):
            continue
        found = lookback_point(slug)
        if not found:
            continue
        _, prev, age_min = found
        bid, ask = float(q["bid"]), float(q["ask"])
        prev_mid = float(prev["mid"])
        mid = (bid + ask) / 2
        spread = ask - bid
        momentum = mid / prev_mid - 1 if prev_mid > 0 else 0
        estimated_edge = momentum - spread / mid - 2 * (fee_theta * (1 - mid) + slip_rate) if mid else -1
        allowed = 0.10 <= ask <= 0.85 and spread <= 0.01 and MIN_MOMENTUM <= momentum <= MAX_MOMENTUM and estimated_edge >= MIN_EDGE
        decisions.append({"time": stamp, "slug": slug, "decision": "BUY" if allowed else "WAIT",
                          "lookback_min": round(age_min, 1), "momentum_pct": round(momentum * 100, 3),
                          "estimated_edge_pct": round(estimated_edge * 100, 3),
                          "explanation": "Two public quotes only; cause of price movement unknown"})
        if not allowed:
            continue
        effective_ask = ask * (1 + slip_rate)
        shares = BUY_USD / (effective_ask + fee_theta * effective_ask * (1 - effective_ask))
        entry_fee = taker_fee(shares, effective_ask)
        cash -= BUY_USD
        exposure += BUY_USD
        positions.append({"slug": slug, "title": q.get("title", slug), "shares": shares, "entry": effective_ask,
                          "quoted_ask": ask, "entry_fee": money(entry_fee), "total_entry_cost": BUY_USD,
                          "opened_at": stamp, "last_bid": bid, "marked_at": stamp, "mark_stale": False})
        trades.append({"time": stamp, "slug": slug, "side": "BUY", "price": effective_ask, "shares": shares,
                       "fee": money(entry_fee), "cash_change": money(-BUY_USD), "reason": "cost_adjusted_momentum"})

if fresh:
    for slug, q in quotes.items():
        series = history.setdefault(slug, [])
        series.append({"mid": (float(q["bid"]) + float(q["ask"])) / 2, "time": stamp})
        del series[:-SERIES_POINTS]
# Keep the 300 most recently updated markets.
history = dict(sorted(history.items(), key=lambda kv: kv[1][-1]["time"] if kv[1] else "")[-300:])

market_value = 0.0
unrealized = 0.0
for p in positions:
    value = exit_value(p["shares"], float(p.get("last_bid", 0)))
    p["unrealized_pnl"] = money(value - float(p.get("total_entry_cost", p["shares"] * p["entry"])))
    market_value += value
    unrealized += p["unrealized_pnl"]
equity = cash + market_value
peak = max(peak, equity)
d.update({
    "strategy": "COST_ADJUSTED_MOMENTUM",
    "paper_engine": "v3.1",
    "fee_model": "POLYMARKET_US_TAKER_P_TIMES_ONE_MINUS_P",
    "fee_theta": fee_theta,
    "fee_verified": True,
    "fee_verified_at": "2026-10-28",
    "fee_source": "https://docs.polymarket.us/fees",
    "assumed_slippage_rate": slip_rate,
    "decision_log": (d.get("decision_log", []) + decisions)[-250:],
    "model_note": "No AI probability model; RSS news associations are not causal explanations. Taker fee per the official Polymarket US schedule (theta 0.0695, cent rounding); no maker or volume rebates assumed.",
    "fixed_paper_buy_usd": BUY_USD,
    "momentum_lookback_minutes": [LOOKBACK_MIN, LOOKBACK_TARGET, LOOKBACK_MAX],
    "initial_capital": initial,
    "live_trading_enabled": False,
    "cash": money(cash),
    "equity": money(equity),
    "market_value": money(market_value),
    "unrealized_pnl": money(unrealized),
    "realized_pnl": money(realized),
    "positions": positions,
    "trades": trades[-500:],
    "quote_history": history,
    "peak_equity": money(peak),
    "max_drawdown_pct": round(max(float(d.get("max_drawdown_pct", 0)), (peak - equity) / peak * 100 if peak else 0), 3),
    "paper_status": "HALTED_LOSS_LIMIT" if halted else "ACTIVE" if fresh else "WAITING_FOR_FRESH_QUOTES",
    "paper_updated_at": stamp,
    "pricing_note": "Taker fee theta=0.0695 per official schedule (verified 2026-10-28); 0.5% adverse slippage per side is an assumption. No depth, latency, news attribution or settlement. Stale marks are not executable.",
})
FILE.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print(f"Paper v3.1: fresh={fresh}, positions={len(positions)}, trades={len(trades)}, decisions={len(decisions)}, cash={cash:.2f}, equity={equity:.2f}", flush=True)
