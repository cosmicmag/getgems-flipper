"""List anything sitting unlisted in the bot wallet (gifts transferred in by the owner, not bought by the bot).

Price = head of the book: just under the cheapest live ask for that exact model, capped by the model's own
fill history, and never below MIN_OF_MED of the model median (no panic dumping). Models with no fills fall
back to the collection floor. Lots the bot bought itself are left to trade.py / reprice.py.
"""
from __future__ import annotations
import json, os, sys, time
from collections import defaultdict

from gg_api import gg
from refs import Fills, norm_coll
from trade import GG_FEE, WALLET, log, trades, STOP_FILE
from reprice import relist

UNDERCUT = float(os.environ.get("LIST_UNDERCUT_TON", "0.1"))
MIN_OF_MED = float(os.environ.get("LIST_MIN_OF_MED", "0.75"))
MAX_PER_RUN = int(os.environ.get("LIST_MAX_PER_RUN", "8"))


def asks_by_model(collection: str) -> dict[str, float]:
    """Cheapest live TON ask per model in a collection."""
    best: dict[str, float] = {}
    cursor = None
    for _ in range(6):
        r = gg(f"/v1/nfts/on-sale/{collection}", limit=100, after=cursor)
        for x in r.get("items", []):
            s = x.get("sale") or {}
            if s.get("type") != "FixPriceSale" or s.get("currency", "TON") != "TON":
                continue
            model = next((a["value"] for a in x.get("attributes", []) if a["traitType"].lower() == "model"), None)
            if not model:
                continue
            p = int(s["fullPrice"]) / 1e9
            if p < best.get(model.lower(), 1e9):
                best[model.lower()] = p
        cursor = r.get("cursor")
        if not cursor:
            break
    return best


def run(dry_run: bool = True) -> list[dict]:
    if os.path.exists(STOP_FILE):
        print("STOP file present, skipping"); return []
    ours = {r["nft"] for r in trades() if r["kind"] == "buy" and r.get("ok")}
    items = gg(f"/v1/nfts/owner/{WALLET}", limit=100).get("items", [])
    idle = [x for x in items if not (x.get("sale") or {}).get("fullPrice") and x["address"] not in ours]
    if not idle:
        print("nothing idle to list"); return []
    print(f"idle lots to list: {len(idle)}")
    fills = Fills(); fills.load_onchain(); fills.load_gg(); fills.load_portals()
    refs = fills.references()
    books: dict[str, dict[str, float]] = {}
    done = []
    for x in idle[:MAX_PER_RUN]:
        coll = x.get("collectionAddress")
        attrs = {a["traitType"].lower(): a["value"] for a in x.get("attributes", [])}
        model = (attrs.get("model") or "").lower()
        cname = norm_coll((gg(f"/v1/collection/{coll}").get("name") if coll else "") or "")
        ref = refs.get((cname, model))
        if coll not in books:
            books[coll] = asks_by_model(coll)
        rival = books[coll].get(model)
        if ref:
            price = min(ref["med"] - 1, rival - UNDERCUT) if rival else ref["med"] - 1
            price = max(price, ref["med"] * MIN_OF_MED)
        elif rival:
            price = rival - UNDERCUT          # no fills for this model: undercut the cheapest live ask
        else:
            print(f"  {x['name']}: no fills and no rival ask, skipped"); continue
        price = round(max(price, 1.0), 2)
        print(f"  {x['name']} ({attrs.get('model')}): list at {price}"
              f" (fills med {ref['med'] if ref else '-'}, cheapest rival {rival})")
        if dry_run:
            continue
        try:
            state = relist(x["address"], price)
        except Exception as e:
            print(f"    listing failed: {e}"); continue
        done.append(log(dict(kind="list", nft=x["address"], price=price, ok=state == "Ready", tx_state=state,
                             name=x.get("name"), model=attrs.get("model"), source="owner_transfer",
                             ref_med=ref["med"] if ref else None, rival_ask=rival)))
    return done


if __name__ == "__main__":
    res = run(dry_run="--send" not in sys.argv)
    if res:
        print(json.dumps(res, ensure_ascii=False)[:600])
