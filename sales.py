"""Detect sales of our lots and record realised PnL.

Nothing in the pipeline noticed a sale before: buys and relists were logged, sales were not, so the weekly
loss stop (which reads kind="sold") had no data at all. This walks every lot we ever bought, asks Getgems
for its history, and appends a "sold" record with the realised PnL the first time it left our wallet.
"""
from __future__ import annotations
import json, sys, time

from gg_api import gg
from trade import GG_FEE, WALLET, addr_hash, log, trades

GAS = 0.3


def dedupe_log(path="data/trades.jsonl"):
    """The hourly job and the watcher both detect sales in separate runners, so the same sale can be
    appended twice before git merges them. Keep the first record per (kind, nft)."""
    import os
    if not os.path.exists(path):
        return
    rows = [json.loads(l) for l in open(path)]
    seen, out = set(), []
    for r in rows:
        key = (r.get("kind"), r.get("nft"), r.get("offer"))
        if r.get("kind") in ("sold", "offer_cancel") and key in seen:
            continue
        seen.add(key); out.append(r)
    if len(out) != len(rows):
        with open(path, "w") as f:
            for r in out:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"  deduped journal: {len(rows) - len(out)} duplicate records dropped")


def run() -> list[dict]:
    dedupe_log()
    tr = trades()
    entry, transferred, sold_seen = {}, {}, set()
    for r in tr:
        if not r.get("nft"):
            continue
        if r["kind"] == "buy" and r.get("ok"):
            entry[r["nft"]] = r["price"]
        elif r["kind"] == "list" and r.get("ok") and r.get("source") == "owner_transfer":
            transferred.setdefault(r["nft"], r["price"])     # no cost basis: report gross proceeds
        elif r["kind"] == "sold":
            sold_seen.add(r["nft"])
    for nft, ask in transferred.items():
        entry.setdefault(nft, None)
    new = []
    for nft, paid in entry.items():
        if nft in sold_seen:
            continue
        owner_lot = paid is None
        try:
            d = gg(f"/v1/nft/{nft}")
        except Exception as e:
            print(f"  {nft[:16]}: {e}"); continue
        owner = d.get("actualOwnerAddress") or d.get("ownerAddress")
        if owner and addr_hash(owner) == addr_hash(WALLET):
            continue                      # still ours (listed or idle)
        hist = gg(f"/v1/nft/history/{nft}", limit=20).get("items", [])
        sales = [h for h in hist if (h.get("typeData") or {}).get("type") in ("sold", "luckyBuy")
                 and addr_hash((h["typeData"].get("newOwner") or "")) != addr_hash(WALLET)
                 and h["typeData"].get("price")]
        if not sales:
            print(f"  {d.get('name')}: left the wallet without a sale record"); continue
        s = sorted(sales, key=lambda h: h["timestamp"])[-1]
        price = float(s["typeData"]["price"]); kind = s["typeData"]["type"]
        pnl = round(price * (1 - GG_FEE) - (0 if owner_lot else paid) - GAS, 2)
        new.append(log(dict(kind="sold", nft=nft, price=price, ok=True, entry=paid, pnl=pnl, owner_lot=owner_lot,
                            via=kind,
                            name=d.get("name"), sold_at=s["timestamp"] / 1000,
                            hold_h=round((s["timestamp"] / 1000 - 0) and (time.time() - s["timestamp"] / 1000) / 3600, 1))))
        print(f"  SOLD {d.get('name')}: {'owner gift' if owner_lot else paid} -> {price} TON, "
              f"{'proceeds' if owner_lot else 'realised'} {pnl:+.2f}")
    return new


def summary():
    tr = trades()
    sold = [r for r in tr if r["kind"] == "sold"]
    bought = [r for r in tr if r["kind"] == "buy" and r.get("ok")]
    flips = [r for r in sold if not r.get("owner_lot")]
    gifts = [r for r in sold if r.get("owner_lot")]
    print(f"buys {len(bought)} | sales {len(sold)} | flip PnL {sum(r['pnl'] for r in flips):+.2f} TON "
          f"| owner gifts sold {len(gifts)} for {sum(r['pnl'] for r in gifts):.2f} TON net")
    for r in sold:
        base = f"{r['entry']:>7.2f}" if r.get("entry") is not None else "   gift"
        print(f"  {r['name'][:26]:26} {base} -> {r['price']:>7.2f}  {r['pnl']:+7.2f}")


if __name__ == "__main__":
    run(); summary()
