"""Authenticated, read-only account check. Never places or cancels orders.

Runs only when POLYMARKET_KEY_ID and POLYMARKET_SECRET_KEY are set as GitHub
Actions secrets. This repository and its Actions logs are public, so nothing
account-specific (balances, positions, IDs) is printed or saved: only whether
authentication worked.
"""
import datetime as dt
import json
import os
from pathlib import Path
from polymarket_us import PolymarketUS, AuthenticationError

FILE = Path("data.json")
data = json.loads(FILE.read_text()) if FILE.exists() else {}
now = dt.datetime.now(dt.timezone.utc).isoformat()
key_id = os.environ.get("POLYMARKET_KEY_ID", "").strip()
secret = os.environ.get("POLYMARKET_SECRET_KEY", "").strip()

if not key_id or not secret:
    status = "not_configured"
else:
    client = PolymarketUS(key_id=key_id, secret_key=secret, timeout=12.0, max_retries=0)
    try:
        client.account.balances()
        status = "connected_read_only"
    except AuthenticationError:
        status = "auth_failed"
    except Exception as exc:
        status = f"error_{type(exc).__name__}"
    finally:
        client.close()

data["account_link"] = {"status": status, "checked_at": now, "live_trading_enabled": False}
FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print(f"Account check: {status}", flush=True)
