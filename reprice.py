"""Price ladder for unsold inventory: every day on the shelf cuts the asking price, never below break-even.

target(age) = max(floor_price, first_listing_price * (1 - STEP_PCT) ** days_on_shelf)
floor_price = (entry + GAS) / (1 - GG_FEE)   -- selling there returns the entry price, not a loss.

Getgems has no "change price" call: a reprice is cancel-fix-price + put-on-sale-fix-price at the new price.
Runs hourly from the paper workflow; each lot is repriced at most once per REPRICE_COOLDOWN_H.
"""
from __future__ import annotations
import json, os, sys, time

from gg_api import gg
from refs import Fills, norm_coll
from trade import GG_FEE, WALLET, gg_post, log, sign_and_send, trades, wait_tx, STOP_FILE

STEP_PCT = float(os.environ.get("REPRICE_STEP_PCT", "7")) / 100
GAS = float(os.environ.get("REPRICE_GAS_TON", "0.3"))
COOLDOWN_H = float(os.environ.get("REPRICE_COOLDOWN_H", "20"))
MIN_AGE_H = float(os.environ.get("REPRICE_MIN_AGE_H", "24"))
OWNER_LOT_FLOOR = float(os.environ.get("REPRICE_OWNER_FLOOR", "0.75"))   # gifts sent in by the owner: stop at 75% of the first ask
# After this long on the shelf, break-even stops being a floor: holding a lot the market has repriced below
# our cost just freezes capital (Low Rider: cost 57.5, break-even 62.2, market below both for nine days).
STOP_LOSS_DAYS = float(os.environ.get("STOP_LOSS_DAYS", "14"))
STOP_LOSS_MIN_OF_MED = float(os.environ.get("STOP_LOSS_MIN_OF_MED", "0.6"))


def history() -> dict[str, dict]:
    """Per NFT: entry price, first asking price, when it was listed, when it was last repriced."""
    out: dict[str, dict] = {}
    for r in trades():
        if not r.get("ok") or not r.get("nft"):
            continue
        h = out.setdefault(r["nft"], {})
        if r["kind"] == "buy":
            h["entry"] = r["price"]
            h.setdefault("bought_at", r["t"])
        elif r["kind"] == "list":
            h.setdefault("first_ask", r["price"])
            h["listed_at"] = r["t"]
        elif r["kind"] == "reprice":
            h["last_reprice"] = r["t"]
            h["listed_at"] = r["t"]
    return out


def cancel_sale(nft: str) -> str:
    tx = gg_post(f"/v1/nfts/cancel-fix-price/{nft}", {})
    sign_and_send(tx, dry_run=False)
    return wait_tx(tx)


def relist(nft: str, price: float) -> str:
    tx = gg_post(f"/v1/nfts/put-on-sale-fix-price/{nft}",
                 {"ownerAddress": WALLET, "fullPrice": str(int(round(price * 1e9)))})
    sign_and_send(tx, dry_run=False)
    return wait_tx(tx)


def head_of_book(item, books, refs) -> float | None:
    """Cheapest comparable live ask (same model, and same backdrop when fills prove a premium)."""
    from list_inventory import asks_by_model
    coll = item.get("collectionAddress")
    if not coll:
        return None
    attrs = {a["traitType"].lower(): a["value"] for a in item.get("attributes", [])}
    model = (attrs.get("model") or "").lower(); backdrop = (attrs.get("backdrop") or "").lower()
    cname = norm_coll((gg(f"/v1/collection/{coll}").get("name") or "") if coll else "")
    if coll not in books:
        books[coll] = asks_by_model(coll)
    same = books[coll].get((model, backdrop))
    if refs.get((cname, model, backdrop)):
        return same                      # premium backdrop: only a same-backdrop ask is comparable
    return same or books[coll].get((model,))


def run(dry_run: bool = True) -> list[dict]:
    if os.path.exists(STOP_FILE):
        print("STOP file present, skipping"); return []
    hist = history(); now = time.time(); done = []
    fills = Fills(); fills.load_onchain(); fills.load_gg(); fills.load_portals()
    refs = fills.references(); books: dict = {}
    for item in gg(f"/v1/nfts/owner/{WALLET}", limit=100).get("items", []):
        sale = item.get("sale") or {}
        if sale.get("type") != "FixPriceSale" or sale.get("currency", "TON") != "TON":
            continue
        nft = item["address"]; cur = int(sale["fullPrice"]) / 1e9
        h = hist.get(nft)
        if not h or not h.get("listed_at"):
            print(f"  {item['name']}: no trade history, skipped"); continue
        if not h.get("entry"):
            h = dict(h, entry=h.get("first_ask", cur) * OWNER_LOT_FLOOR)   # owner transfer: no cost basis
        age_h = (now - h["listed_at"]) / 3600
        since_reprice_h = (now - h.get("last_reprice", 0)) / 3600
        if age_h < MIN_AGE_H or since_reprice_h < COOLDOWN_H:
            print(f"  {item['name']}: {cur} TON, on shelf {age_h:.1f}h — too early"); continue
        floor = (h["entry"] + GAS) / (1 - GG_FEE)
        held_days = (now - h.get("bought_at", h["listed_at"])) / 86400
        stop_loss = held_days >= STOP_LOSS_DAYS
        if stop_loss:
            attrs = {a["traitType"].lower(): a["value"] for a in item.get("attributes", [])}
            model = (attrs.get("model") or "").lower(); backdrop = (attrs.get("backdrop") or "").lower()
            cname = norm_coll((gg(f"/v1/collection/{item['collectionAddress']}").get("name") or "")
                              if item.get("collectionAddress") else "")
            ref = refs.get((cname, model, backdrop)) or refs.get((cname, model))
            floor = ref["med"] * STOP_LOSS_MIN_OF_MED if ref else floor * 0.6
        days = int((now - h["listed_at"]) // 86400) + 1
        ladder = h.get("first_ask", cur) * (1 - STEP_PCT) ** days
        # Sitting above the book means never trading: if a comparable ask is cheaper than our ladder step,
        # go just under it (this is what actually sold the cigars), but never below break-even.
        rival = head_of_book(item, books, refs)
        if rival and rival - 0.1 < ladder:
            ladder = rival - 0.1
        target = round(max(floor, ladder), 2)
        if target >= cur - 0.01:
            print(f"  {item['name']}: {cur} TON already at/below target {target} (floor {floor:.2f})"); continue
        print(f"  {item['name']}: {cur} -> {target} TON (entry {h['entry']}, floor {floor:.2f}, {age_h:.0f}h"
              + (", STOP-LOSS" if stop_loss else "") + ")")
        if dry_run:
            continue
        try:
            st_cancel = cancel_sale(nft)
            st_list = relist(nft, target)
            ok = st_list == "Ready"
        except Exception as e:
            print(f"    reprice failed: {e}"); log(dict(kind="reprice", nft=nft, price=target, ok=False, reason=str(e)[:200],
                                                        name=item.get("name"), old_price=cur)); continue
        done.append(log(dict(kind="reprice", nft=nft, price=target, ok=ok, tx_state=f"{st_cancel}/{st_list}",
                             name=item.get("name"), old_price=cur, entry=h["entry"], stop_loss=stop_loss)))
        if stop_loss and target < (h["entry"] + GAS) / (1 - GG_FEE):
            try:
                from sales import tg
                tg(f"✂️ Стоп-лосс: {item.get('name')} {cur} → {target} TON\n"
                   f"вход {h['entry']}, на полке {held_days:.0f} дн, режем к рынку")
            except Exception:
                pass
    return done


if __name__ == "__main__":
    res = run(dry_run="--send" not in sys.argv)
    if res:
        print(json.dumps(res, ensure_ascii=False))
