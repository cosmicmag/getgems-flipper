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
    """Outcome of a candidate: 'sniped' = someone bought it near our entry price (market agrees it was cheap),
    'resold' = the sniper sold it again -> that resale is our modelled exit; 'still' / 'unsold_7d' otherwise."""
    rows = load(); upd = 0
    for r in rows:
        if (r.get("outcome") or {}).get("kind") in ("resold", "unsold_7d") or time.time() - r["logged_at"] < 600:
            continue
        try:
            h = gg(f"/v1/nft/history/{r['nft']}", limit=20)
        except Exception as e:
            print("hist err", r["nft"][:20], e); continue
        ev = sorted(((x["timestamp"] / 1000, (x.get("typeData") or {}).get("type"), (x.get("typeData") or {}).get("price"))
                     for x in h.get("items", [])), key=lambda e: e[0])
        sold = [e for e in ev if e[1] == "sold" and e[0] >= r["logged_at"] - 3600 and e[2]]
        if len(sold) >= 2:
            resale = float(sold[1][2]); net = resale * 0.98 - r["gg_price"] - 0.3
            r["outcome"] = dict(kind="resold", sniped_at=sold[0][0], sniped_price=float(sold[0][2]), resale=resale, paper_net=round(net, 2)); upd += 1
        elif sold:
            r["outcome"] = dict(kind="sniped", sniped_at=sold[0][0], sniped_price=float(sold[0][2]),
                                minutes=round((sold[0][0] - r["logged_at"]) / 60)); upd += 1
        elif time.time() - r["logged_at"] > 7 * 86400:
            r["outcome"] = dict(kind="unsold_7d"); upd += 1
    save(rows)
    kinds = {}
    for r in rows:
        kinds[(r.get("outcome") or {}).get("kind") or "open"] = kinds.get((r.get("outcome") or {}).get("kind") or "open", 0) + 1
    print(f"checked, updated {upd}; total {len(rows)}; outcomes {kinds}")
    resold = [r for r in rows if (r.get("outcome") or {}).get("kind") == "resold"]
    if resold:
        net = sum(r["outcome"]["paper_net"] for r in resold); wins = sum(1 for r in resold if r["outcome"]["paper_net"] > 0)
        print(f"paper (entry -> sniper's resale): net {net:+.1f} TON, wins {wins}/{len(resold)}")
        for r in resold:
            print(f"  {r['coll'][:14]:14} {r['model'][:14]:14} buy {r['gg_price']:>7.2f} -> resold {r['outcome']['resale']:>7.2f} net {r['outcome']['paper_net']:>+7.2f}")
    sn = [r for r in rows if (r.get("outcome") or {}).get("kind") == "sniped"]
    if sn:
        import statistics
        print(f"sniped (market agreed, awaiting resale): {len(sn)}, median minutes to snipe {statistics.median(r['outcome']['minutes'] for r in sn)}")


if __name__ == "__main__":
    {"log": cmd_log, "check": cmd_check}[sys.argv[1]]()
