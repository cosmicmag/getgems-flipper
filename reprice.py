"""Price ladder for unsold inventory: every day on the shelf cuts the asking price, never below break-even.

target(age) = max(floor_price, first_listing_price * (1 - STEP_PCT) ** days_on_shelf)
floor_price = (entry + GAS) / (1 - GG_FEE)   -- selling there returns the entry price, not a loss.

Getgems has no "change price" call: a reprice is cancel-fix-price + put-on-sale-fix-price at the new price.
Runs hourly from the paper workflow; each lot is repriced at most once per REPRICE_COOLDOWN_H.
"""
from __future__ import annotations
import json, os, sys, time

from gg_api import gg
from trade import GG_FEE, WALLET, gg_post, log, sign_and_send, trades, wait_tx, STOP_FILE

STEP_PCT = float(os.environ.get("REPRICE_STEP_PCT", "7")) / 100
GAS = float(os.environ.get("REPRICE_GAS_TON", "0.3"))
COOLDOWN_H = float(os.environ.get("REPRICE_COOLDOWN_H", "20"))
MIN_AGE_H = float(os.environ.get("REPRICE_MIN_AGE_H", "24"))


def history() -> dict[str, dict]:
    """Per NFT: entry price, first asking price, when it was listed, when it was last repriced."""
    out: dict[str, dict] = {}
    for r in trades():
        if not r.get("ok") or not r.get("nft"):
            continue
        h = out.setdefault(r["nft"], {})
        if r["kind"] == "buy":
            h["entry"] = r["price"]
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


def run(dry_run: bool = True) -> list[dict]:
    if os.path.exists(STOP_FILE):
        print("STOP file present, skipping"); return []
    hist = history(); now = time.time(); done = []
    for item in gg(f"/v1/nfts/owner/{WALLET}", limit=100).get("items", []):
        sale = item.get("sale") or {}
        if sale.get("type") != "FixPriceSale" or sale.get("currency", "TON") != "TON":
            continue
        nft = item["address"]; cur = int(sale["fullPrice"]) / 1e9
        h = hist.get(nft)
        if not h or not h.get("entry") or not h.get("listed_at"):
            print(f"  {item['name']}: no trade history, skipped"); continue
        age_h = (now - h["listed_at"]) / 3600
        since_reprice_h = (now - h.get("last_reprice", 0)) / 3600
        if age_h < MIN_AGE_H or since_reprice_h < COOLDOWN_H:
            print(f"  {item['name']}: {cur} TON, on shelf {age_h:.1f}h — too early"); continue
        floor = (h["entry"] + GAS) / (1 - GG_FEE)
        days = int((now - h["listed_at"]) // 86400) + 1
        target = round(max(floor, h.get("first_ask", cur) * (1 - STEP_PCT) ** days), 2)
        if target >= cur - 0.01:
            print(f"  {item['name']}: {cur} TON already at/below target {target} (floor {floor:.2f})"); continue
        print(f"  {item['name']}: {cur} -> {target} TON (entry {h['entry']}, floor {floor:.2f}, {age_h:.0f}h)")
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
                             name=item.get("name"), old_price=cur, entry=h["entry"])))
    return done


if __name__ == "__main__":
    res = run(dry_run="--send" not in sys.argv)
    if res:
        print(json.dumps(res, ensure_ascii=False))
