"""Collection offers (bids) on specific models: let sellers come to us instead of racing snipers to listings.

A bid is escrowed on chain, so this only bids on models whose fills prove both liquidity and a spread:
  bid = model median * (1 - BID_DISCOUNT), capped by MAX_BID_TON and the total OFFER_BUDGET_TON.
Attribute offers target one model, so nobody can fill us with a 4 TON common when we bid 40 for a premium.
"""
from __future__ import annotations
import json, os, sys, time

from gg_api import gg
from refs import Fills, norm_coll
from trade import WALLET, gg_post, log, sign_and_send, trades, wait_tx, STOP_FILE, balance

BID_DISCOUNT = float(os.environ.get("BID_DISCOUNT", "0.25"))
MAX_BID_TON = float(os.environ.get("MAX_BID_TON", "60"))
MIN_BID_TON = float(os.environ.get("MIN_BID_TON", "8"))
# A bid far under the market never fills, it just freezes cash (Scared Cat Obelisk: cap 60 against a 2815
# market). Only bid when the cap still leaves us within reach of the book.
MIN_BID_OF_P25 = float(os.environ.get("MIN_BID_OF_P25", "0.5"))
OFFER_BUDGET_TON = float(os.environ.get("OFFER_BUDGET_TON", "250"))
OFFER_DAYS = int(os.environ.get("OFFER_DAYS", "3"))
MIN_FILLS = int(os.environ.get("OFFER_MIN_FILLS", "6"))
MAX_REF_AGE_D = float(os.environ.get("OFFER_MAX_REF_AGE_D", "3"))
# A model whose backdrops trade far apart (Vintage Cigar Vaporwave: 35 to 333) will be filled with its
# cheapest variant while the median reflects the expensive one. Only bid on models that trade uniformly.
MAX_BACKDROP_SPREAD = float(os.environ.get("MAX_BACKDROP_SPREAD", "2.0"))
RESERVE_TON = float(os.environ.get("RESERVE_TON", "5"))
# Bids must not swallow the cash the flipper needs: escrow grew to 245 TON while only 31 was left to buy
# with, so the budget is capped by what remains after reserving for flips.
FLIP_RESERVE_TON = float(os.environ.get("FLIP_RESERVE_TON", "100"))

COLLECTIONS = {}     # filled from the top gift collections


def live_offers() -> list[dict]:
    try:
        return gg(f"/v1/offers/by-user/{WALLET}", limit=100).get("items", [])
    except Exception as e:
        print("offers fetch err", e); return []


def active_keys() -> set:
    """(collection, model) pairs we already have an escrowed bid on."""
    keys = set()
    for o in live_offers():
        for a in (o.get("attributes") or []):
            for v in a.get("values", []):
                keys.add((o.get("collectionAddress"), v.lower()))
    return keys


def recent_keys() -> set:
    """Offers we placed recently. The offers API lags behind a fresh transaction, so the journal is the
    authority for a few minutes after placing one."""
    keys, cutoff = set(), time.time() - OFFER_DAYS * 86400
    cancelled = {r.get("offer") for r in trades() if r["kind"] == "offer_cancel"}
    for r in trades():
        if r["kind"] == "offer" and r.get("ok") and r["t"] > cutoff and r.get("offer") not in cancelled:
            keys.add((r.get("coll_address") or r.get("coll"), (r.get("model") or "").lower()))
    return keys


def backdrop_spread(fills) -> dict:
    """max/min of per-backdrop medians for each (collection, model)."""
    import statistics
    from collections import defaultdict
    by = defaultdict(lambda: defaultdict(list))
    for r in fills.rows:
        if r["backdrop"]:
            by[(r["coll"], r["model"])][r["backdrop"]].append(r["price"])
    out = {}
    for key, bds in by.items():
        meds = [statistics.median(v) for v in bds.values()]
        out[key] = (max(meds) / min(meds)) if meds and min(meds) > 0 else 99.0
    return out


def plan() -> list[dict]:
    fills = Fills(); fills.load_onchain(); fills.load_gg(); fills.load_portals()
    refs = fills.references()
    spread = backdrop_spread(fills)
    top = gg("/v1/gifts/collections/top", kind="week", limit=25)["items"]
    for t in top:
        COLLECTIONS[norm_coll(t["collection"]["name"] or "")] = t["collection"]["address"]
    cands = []
    for key, ref in refs.items():
        if len(key) != 2:
            continue                       # model-level only: an attribute offer targets Model
        coll, model = key
        if coll not in COLLECTIONS or ref["n"] < MIN_FILLS or ref["last_age_d"] > MAX_REF_AGE_D:
            continue
        if spread.get((coll, model), 1.0) > MAX_BACKDROP_SPREAD:
            continue                       # uneven backdrops: we would be filled with the cheap variant
        bid = round(min(ref["med"] * (1 - BID_DISCOUNT), MAX_BID_TON), 2)
        if bid < MIN_BID_TON or bid >= ref["p25"]:
            continue                       # no room under the market
        if bid < ref["p25"] * MIN_BID_OF_P25:
            continue                       # capped far below the book: would never fill
        cands.append(dict(coll=coll, address=COLLECTIONS[coll], model=model, bid=bid,
                          med=ref["med"], p25=ref["p25"], n=ref["n"], age=ref["last_age_d"],
                          upside=round(ref["p25"] * 0.98 - bid - 0.3, 2)))
    cands.sort(key=lambda c: -c["upside"])
    taken = active_keys() | recent_keys()
    cands = [c for c in cands if (c["address"], c["model"]) not in taken and (c["coll"], c["model"]) not in taken]
    # The budget caps TOTAL escrow. Counting only this run's bids let each hourly run add another 100 TON
    # until the wallet was empty (197 TON locked, 17 TON cash).
    locked = sum(int(o["fullPrice"]) / 1e9 for o in live_offers())
    budget = min(OFFER_BUDGET_TON, max(0.0, balance() + locked - FLIP_RESERVE_TON))
    room = budget - locked
    picked, spent = [], 0.0
    for c in cands:
        if spent + c["bid"] > room:
            continue
        picked.append(c); spent += c["bid"]
    if room <= 0:
        print(f"escrow budget full: {locked:.1f} locked, budget {budget:.1f} "
              f"(cap {OFFER_BUDGET_TON}, flip reserve {FLIP_RESERVE_TON})")
    return picked


def place(c: dict, dry_run=True) -> dict:
    body = {"userAddress": WALLET, "collectionAddress": c["address"], "price": str(int(round(c["bid"] * 1e9))),
            "amount": 1, "finishAt": int(time.time()) + OFFER_DAYS * 86400,
            "attributes": [{"trait": "Model", "values": [c["model"].title()]}]}
    tx = gg_post("/v1/offer/collection/create", body)
    total = sum(int(m["amount"]) for m in tx["list"]) / 1e9
    if dry_run:
        return dict(c, escrow=total, dry_run=True)
    sign_and_send(tx, dry_run=False)
    state = wait_tx(tx)
    return log(dict(kind="offer", nft=None, price=c["bid"], ok=state == "Ready", tx_state=state,
                    coll=c["coll"], coll_address=c["address"], model=c["model"], escrow=total,
                    med=c["med"], upside=c["upside"]))


def cancel_expired(dry_run=True) -> int:
    """Reclaim escrow: Getgems holds the cash until an offer is cancelled, and an hourly job that does not
    dedupe will stack a fresh bid on the same model every run."""
    n = 0
    seen = set()
    for o in live_offers():
        finish = (o.get("finishAt") or 0) / 1000
        key = (o.get("collectionAddress"), tuple(sorted(v for a in (o.get("attributes") or []) for v in a.get("values", []))))
        duplicate = key in seen
        seen.add(key)
        if not duplicate and finish and finish > time.time():
            continue
        if dry_run:
            print(f"  would cancel offer {o.get('offerAddress','')[:14]}"); n += 1; continue
        try:
            tx = gg_post("/v1/offer/collection/cancel", {"userAddress": WALLET, "offerAddress": o["offerAddress"]})
            sign_and_send(tx, dry_run=False); wait_tx(tx)
            log(dict(kind="offer_cancel", nft=None, price=int(o.get("fullPrice", 0)) / 1e9, ok=True,
                     offer=o["offerAddress"], reason="duplicate" if duplicate else "expired"))
            n += 1
        except Exception as e:
            print(f"  cancel failed {o.get('offerAddress','')[:14]}: {e}")
    return n


def trim_to_budget(dry_run=True) -> int:
    """Cancel the weakest bids until locked escrow fits the budget, keeping the ones with the best upside."""
    fills = Fills(); fills.load_onchain(); fills.load_gg(); fills.load_portals()
    refs = fills.references()
    live = live_offers()
    locked = sum(int(o["fullPrice"]) / 1e9 for o in live)
    budget = min(OFFER_BUDGET_TON, max(0.0, balance() + locked - FLIP_RESERVE_TON))
    if locked <= budget:
        return 0
    coll_name = {}
    scored = []
    for o in live:
        bid = int(o["fullPrice"]) / 1e9
        models = [v.lower() for a in (o.get("attributes") or []) for v in a.get("values", [])]
        addr = o.get("collectionAddress")
        if addr not in coll_name:
            try:
                coll_name[addr] = norm_coll(gg(f"/v1/collection/{addr}").get("name") or "")
            except Exception:
                coll_name[addr] = ""
        ref = refs.get((coll_name[addr], models[0])) if models else None
        upside = (ref["p25"] * 0.98 - bid - 0.3) if ref else -bid
        scored.append((upside, bid, o))
    scored.sort()                       # weakest first
    freed = 0
    for upside, bid, o in scored:
        if locked <= budget:
            break
        print(f"  trim offer {bid:.2f} TON (upside {upside:+.2f})")
        locked -= bid; freed += 1
        if dry_run:
            continue
        try:
            tx = gg_post("/v1/offer/collection/cancel", {"userAddress": WALLET, "offerAddress": o["offerAddress"]})
            sign_and_send(tx, dry_run=False); wait_tx(tx)
            log(dict(kind="offer_cancel", nft=None, price=bid, ok=True, offer=o["offerAddress"], reason="over budget"))
        except Exception as e:
            print(f"    cancel failed: {e}")
    return freed


def main():
    if os.path.exists(STOP_FILE):
        print("STOP file present"); return
    dry = "--send" not in sys.argv
    picked = plan()
    bal = balance()
    print(f"balance {bal:.1f} TON | budget {OFFER_BUDGET_TON} | candidates {len(picked)}")
    print(f"{'collection':16} {'model':18} {'bid':>7} {'p25':>7} {'med':>7} {'n':>3} {'age':>5} {'upside':>7}")
    spent = 0.0
    for c in picked:
        print(f"{c['coll'][:16]:16} {c['model'][:18]:18} {c['bid']:>7.2f} {c['p25']:>7.1f} {c['med']:>7.1f} "
              f"{c['n']:>3} {c['age']:>5} {c['upside']:>7.2f}")
        if dry:
            continue
        if bal - spent - c["bid"] - 1 < RESERVE_TON:
            print("  budget/balance exhausted"); break
        r = place(c, dry_run=False); spent += c["bid"]
        print(f"  -> {'placed' if r.get('ok') else 'failed'} {r.get('tx_state','')}")
    freed = cancel_expired(dry_run=dry) + trim_to_budget(dry_run=dry)
    if freed:
        print(f"expired offers cancelled: {freed}")
    live = live_offers()
    print(f"live offers now: {len(live)}")


if __name__ == "__main__":
    main()
