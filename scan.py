"""Read-only Polymarket US market watcher; no authentication or order endpoints."""
import json, pathlib, datetime
from polymarket_us import PolymarketUS
out=pathlib.Path("data.json")
now=datetime.datetime.now(datetime.timezone.utc).isoformat()
previous=json.loads(out.read_text()) if out.exists() else {"cash":1000,"equity":1000,"trades":[]}
client=PolymarketUS()
markets=[]
try:
    result=client.markets.list()
    items=result if isinstance(result,list) else next((result.get(k,[]) for k in ("markets","data","items","results") if isinstance(result.get(k),list)),[])
    for m in items[:30]:
        slug=m.get("slug") or m.get("marketSlug")
        if not slug: continue
        try:
            quote=client.markets.bbo(slug)
            quote=quote.get("marketData",quote) if isinstance(quote,dict) else {}
            bid=float(quote.get("bestBid",0))
            ask=float(quote.get("bestAsk",0))
            if 0<bid<ask<1:
                markets.append({"slug":slug,"title":str(m.get("question") or m.get("title") or slug)[:180],"bid":bid,"ask":ask})
        except (ValueError,TypeError,KeyError,AttributeError):
            continue
finally:
    client.close()
if not markets: raise RuntimeError("No valid market quotes returned; refusing to publish a fake success.")
previous.update({"last_scan":now,"markets":markets,"mode":"PAPER_ONLY","live_trading_enabled":False,"strategy":"DATA_COLLECTION_ONLY","cash":1000,"equity":1000})
out.write_text(json.dumps(previous,ensure_ascii=False,indent=2)+"\n")
print("Collected",len(markets),"markets")
