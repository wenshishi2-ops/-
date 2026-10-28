"""Read-only public RSS context. Matches are associations, NOT causal attribution."""
import datetime as dt
import email.utils
import json
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

file = Path("data.json")
data = json.loads(file.read_text())
now = dt.datetime.now(dt.timezone.utc)
stop = {"the","and","for","will","who","win","2026","2027","market","league","national","championship","winner","year","of","in","on","to","a","is"}
def tokens(s):
    return {w for w in re.findall(r"[a-z]{3,}", s.lower()) if w not in stop}
results = []
if data.get("scan_status") == "ok" and data.get("quotes_as_of") == data.get("last_scan"):
    seen = set()
    for m in data.get("markets", [])[:4]:
        title = m.get("title", "")
        query = " ".join(list(tokens(title))[:5])
        if len(tokens(title)) < 2 or not query or query in seen:
            continue
        seen.add(query)
        try:
            url = "https://news.google.com/rss/search?" + urllib.parse.urlencode({"q":query+" when:1d","hl":"en-US","gl":"US","ceid":"US:en"})
            req = urllib.request.Request(url, headers={"User-Agent":"NexaQuantPaperResearch/1.0"})
            with urllib.request.urlopen(req, timeout=8) as response:
                raw = response.read(160000)
            root = ET.fromstring(raw)
            matches = []
            for item in root.findall(".//item")[:12]:
                headline = item.findtext("title", "")
                overlap = tokens(title) & tokens(headline)
                if len(overlap) < 2:
                    continue
                pub = item.findtext("pubDate", "")
                try:
                    published = email.utils.parsedate_to_datetime(pub).astimezone(dt.timezone.utc)
                except (ValueError, TypeError):
                    continue
                if not (0 <= (now-published).total_seconds() <= 86400):
                    continue
                matches.append({"headline":headline[:220],"source":item.findtext("source","")[:80],"url":item.findtext("link","")[:500],"published_at":published.isoformat(),"matching_terms":sorted(overlap)[:6],"causation_verified":False})
            results.append({"slug":m["slug"],"query":query,"articles":matches[:3],"status":"matched" if matches else "no_matching_recent_articles"})
        except Exception as exc:
            results.append({"slug":m["slug"],"status":"unavailable","error":type(exc).__name__})
data["news_context"] = results
data["news_updated_at"] = now.isoformat()
data["news_note"] = "Public RSS headlines, time-window and keyword matches only; NOT proof of causation and NOT a trading signal."
file.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n")
print("News context: "+str(len(results))+" market queries",flush=True)
