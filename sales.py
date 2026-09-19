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


def run() -> list[dict]:
    tr = trades()
    entry, first_ask, sold_seen = {}, {}, set()
    for r in tr:
        if not r.get("nft"):
            continue
        if r["kind"] == "buy" and r.get("ok"):
            entry[r["nft"]] = r["price"]
        elif r["kind"] == "sold":
            sold_seen.add(r["nft"])
    new = []
    for nft, paid in entry.items():
        if nft in sold_seen:
            continue
        try:
            d = gg(f"/v1/nft/{nft}")
        except Exception as e:
            print(f"  {nft[:16]}: {e}"); continue
        owner = d.get("actualOwnerAddress") or d.get("ownerAddress")
        if owner and addr_hash(owner) == addr_hash(WALLET):
            continue                      # still ours (listed or idle)
        hist = gg(f"/v1/nft/history/{nft}", limit=20).get("items", [])
        sales = [h for h in hist if (h.get("typeData") or {}).get("type") == "sold"
                 and addr_hash((h["typeData"].get("newOwner") or "")) != addr_hash(WALLET)
                 and h["typeData"].get("price")]
        if not sales:
            print(f"  {d.get('name')}: left the wallet without a sale record"); continue
        s = sorted(sales, key=lambda h: h["timestamp"])[-1]
        price = float(s["typeData"]["price"]); pnl = round(price * (1 - GG_FEE) - paid - GAS, 2)
        new.append(log(dict(kind="sold", nft=nft, price=price, ok=True, entry=paid, pnl=pnl,
                            name=d.get("name"), sold_at=s["timestamp"] / 1000,
                            hold_h=round((s["timestamp"] / 1000 - 0) and (time.time() - s["timestamp"] / 1000) / 3600, 1))))
        print(f"  SOLD {d.get('name')}: {paid} -> {price} TON, realised {pnl:+.2f}")
    return new


def summary():
    tr = trades()
    sold = [r for r in tr if r["kind"] == "sold"]
    bought = [r for r in tr if r["kind"] == "buy" and r.get("ok")]
    realised = sum(r["pnl"] for r in sold)
    print(f"buys {len(bought)} | sales {len(sold)} | realised PnL {realised:+.2f} TON")
    for r in sold:
        print(f"  {r['name'][:26]:26} {r['entry']:>7.2f} -> {r['price']:>7.2f}  {r['pnl']:+7.2f}")


if __name__ == "__main__":
    run(); summary()
