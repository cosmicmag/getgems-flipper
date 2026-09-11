"""Portals fills collector (--once: fetch new pages and exit): polls /api/market/actions (purchases) and appends new ones to data/portals_fills.jsonl.
Portals caps the page at 20 actions, so this must run continuously to build history. Dedupe by offer_id."""
import json, os, sys, time
from gg_api import portals

OUT = "data/portals_fills.jsonl"
INTERVAL = int(os.environ.get("PORTALS_POLL_SEC", "45"))
seen = set()
if os.path.exists(OUT):
    for line in open(OUT):
        try: seen.add(json.loads(line)["offer_id"])
        except Exception: pass
print(f"loaded {len(seen)} fills", file=sys.stderr, flush=True)
ONCE = "--once" in sys.argv
while True:
    try:
        new = 0
        for offset in range(0, 200, 20):
            d = portals("/market/actions", offset=offset, limit=20, action_types="buy")
            acts = d.get("actions", [])
            fresh = [a for a in acts if a.get("offer_id") and a["offer_id"] not in seen]
            with open(OUT, "a") as f:
                for a in fresh:
                    n = a.get("nft") or {}
                    attrs = {x["type"]: x["value"] for x in n.get("attributes", [])}
                    rec = dict(offer_id=a["offer_id"], t=a["created_at"], amount=float(a["amount"]),
                               coll=n.get("name"), num=n.get("external_collection_number"),
                               model=attrs.get("model"), backdrop=attrs.get("backdrop"), symbol=attrs.get("symbol"),
                               bundle=bool(a.get("bundle")))
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n"); seen.add(a["offer_id"]); new += 1
            if len(fresh) < len(acts) or not acts:
                break  # reached already-seen actions
        print(time.strftime("%H:%M:%S"), "new", new, "total", len(seen), file=sys.stderr, flush=True)
    except Exception as e:
        print("err", e, file=sys.stderr, flush=True)
        time.sleep(15)
    if ONCE:
        break
    time.sleep(INTERVAL)
