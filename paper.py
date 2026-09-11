"""Paper trading: log scanner candidates, later check whether they actually sold and at what price.

  python3 paper.py log     -> append current scan_dump.json candidates to data/paper_log.jsonl (dedupe by nft)
  python3 paper.py check   -> for logged candidates older than 1h, fetch Getgems NFT history and record the outcome
"""
import json, os, sys, time
from gg_api import gg

LOG = "data/paper_log.jsonl"


def load():
    if not os.path.exists(LOG):
        return []
    return [json.loads(l) for l in open(LOG)]


def save(rows):
    with open(LOG, "w") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def cmd_log():
    rows = load(); seen = {r["nft"] for r in rows}
    d = json.load(open("scan_dump.json")); new = 0
    for c in d["candidates"]:
        if c["nft"] in seen:
            continue
        rows.append(dict(c, logged_at=time.time(), outcome=None)); new += 1
    save(rows); print(f"logged {new} new candidates, total {len(rows)}")


def cmd_check():
    rows = load(); upd = 0
    for r in rows:
        if r.get("outcome") or time.time() - r["logged_at"] < 3600:
            continue
        try:
            h = gg(f"/v1/nft/history/{r['nft']}", limit=20)
        except Exception as e:
            print("hist err", r["nft"][:20], e); continue
        items = h.get("items", [])
        sold = [x for x in items if (x.get("typeData") or {}).get("type") == "sold" and x["timestamp"] / 1000 > r["logged_at"] - 600]
        listing_gone = not any((x.get("typeData") or {}).get("type") in ("putUpForSale",) for x in items[:1])
        if sold:
            price = float(sold[0]["typeData"]["price"])
            r["outcome"] = dict(kind="sold", price=price, at=sold[0]["timestamp"] / 1000,
                                # if we had bought at gg_price and the next buyer paid `price`, that is our exit
                                paper_net=round(price * 0.98 - r["gg_price"] - 0.3, 2))
            upd += 1
        elif time.time() - r["logged_at"] > 7 * 86400:
            r["outcome"] = dict(kind="unsold_7d"); upd += 1
    save(rows)
    done = [r for r in rows if r.get("outcome")]
    sold = [r for r in done if r["outcome"]["kind"] == "sold"]
    print(f"checked, updated {upd}; outcomes: {len(done)} of {len(rows)}; sold {len(sold)}")
    if sold:
        net = sum(r["outcome"]["paper_net"] for r in sold)
        wins = sum(1 for r in sold if r["outcome"]["paper_net"] > 0)
        print(f"paper: bought at candidate price -> next real sale: total net {net:.1f} TON, wins {wins}/{len(sold)}")
        for r in sold:
            print(f"  {r['coll'][:14]:14} {r['model'][:14]:14} buy {r['gg_price']:>7.2f} -> sold {r['outcome']['price']:>7.2f} net {r['outcome']['paper_net']:>7.2f}")


if __name__ == "__main__":
    {"log": cmd_log, "check": cmd_check}[sys.argv[1]]()
