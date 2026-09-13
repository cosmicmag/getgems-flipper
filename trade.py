"""Live trading primitives for the Getgems flipper: buy a fixed-price listing, verify ownership, relist.

Risk limits (env, TON): MAX_LOT_TON=50, DAILY_CAP_TON=150, WEEKLY_LOSS_STOP_TON=30, RESERVE_TON=3.
A file named STOP in the project root disables all buying. Every action is appended to data/trades.jsonl.
Transactions are signed by signer/send.ts with the bot wallet (mnemonic in macOS keychain).
"""
from __future__ import annotations
import json, os, subprocess, sys, time, urllib.parse, urllib.request
from gg_api import gg, gg_key, GG_BASE, UA

ROOT = os.path.dirname(os.path.abspath(__file__))
TRADES = os.path.join(ROOT, "data", "trades.jsonl")
STOP_FILE = os.path.join(ROOT, "STOP")
MAX_LOT_TON = float(os.environ.get("MAX_LOT_TON", "50"))
DAILY_CAP_TON = float(os.environ.get("DAILY_CAP_TON", "150"))
WEEKLY_LOSS_STOP_TON = float(os.environ.get("WEEKLY_LOSS_STOP_TON", "30"))
RESERVE_TON = float(os.environ.get("RESERVE_TON", "3"))
GG_FEE = 0.02
WALLET = os.environ.get("BOT_WALLET", "UQD-35-osBmsmtrajn-ZX6jc_Mkws-pHQ_oBTmVppgtvJHVz")


def addr_hash(a: str | None) -> str:
    """Account hash from a user-friendly TON address, so EQ.. and UQ.. forms of one wallet compare equal."""
    import base64
    if not a:
        return ""
    if ":" in a:
        return a.split(":")[1].lower()
    raw = base64.urlsafe_b64decode(a + "=" * (-len(a) % 4))
    return raw[2:34].hex()


def log(rec: dict):
    rec = dict(rec, t=time.time())
    with open(TRADES, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def trades() -> list[dict]:
    if not os.path.exists(TRADES):
        return []
    return [json.loads(l) for l in open(TRADES)]


def gg_post(path: str, body: dict) -> dict:
    req = urllib.request.Request(f"{GG_BASE}{path}", data=json.dumps(body).encode(),
                                 headers={"accept": "application/json", "content-type": "application/json",
                                          "Authorization": gg_key(), "user-agent": UA}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GG POST {path} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    if not d.get("success", True):
        raise RuntimeError(f"GG POST {path} failed: {str(d)[:300]}")
    return d.get("response", d)


def balance() -> float:
    out = subprocess.check_output(["npx", "tsx", "send.ts", "--balance"], cwd=os.path.join(ROOT, "signer"), text=True, timeout=90)
    return float(json.loads(out.strip().splitlines()[-1])["balanceTon"])


def sign_and_send(tx: dict, dry_run: bool) -> dict:
    path = os.path.join(ROOT, "data", f"tx_{int(time.time()*1000)}.json")
    json.dump(tx, open(path, "w"))
    args = ["npx", "tsx", "send.ts", "--file", path] + ([] if dry_run else ["--send"])
    p = subprocess.run(args, cwd=os.path.join(ROOT, "signer"), text=True, capture_output=True, timeout=180)
    if p.returncode != 0:
        raise RuntimeError(f"signer failed: {p.stderr.strip()[-300:]}")
    return json.loads(p.stdout.strip().splitlines()[-1])


def wait_tx(tx: dict, timeout=150) -> str:
    """Poll Getgems check-tx-status for the first message of the tx list. Returns Ready|Failed|NotReady."""
    m = tx["list"][0]
    body = dict(to=m["to"], amount=m["amount"], check=m["check"], uuid=tx["uuid"], context=m.get("context") or [])
    if tx.get("from"):
        body["from"] = tx["from"]
    t0 = time.time(); state = "NotReady"
    while time.time() - t0 < timeout:
        try:
            state = gg_post("/v1/check-tx-status", body).get("state", "NotReady")
        except Exception as e:
            print("check-tx err", e, file=sys.stderr)
        if state in ("Ready", "Failed"):
            return state
        time.sleep(5)
    return state


def limits_ok(price: float) -> tuple[bool, str]:
    if os.path.exists(STOP_FILE):
        return False, "STOP file present"
    if price > MAX_LOT_TON:
        return False, f"lot {price} > MAX_LOT_TON {MAX_LOT_TON}"
    now = time.time(); tr = trades()
    spent_24h = sum(x["price"] for x in tr if x.get("kind") == "buy" and x.get("ok") and now - x["t"] < 86400)
    if spent_24h + price > DAILY_CAP_TON:
        return False, f"daily cap: spent {spent_24h:.1f} + {price} > {DAILY_CAP_TON}"
    realized_7d = sum(x.get("pnl", 0) for x in tr if x.get("kind") == "sold" and now - x["t"] < 7 * 86400)
    if realized_7d < -WEEKLY_LOSS_STOP_TON:
        return False, f"weekly realized loss {realized_7d:.1f} beyond stop"
    return True, "ok"


def buy(nft: str, version: str, price: float, meta: dict, dry_run=False) -> dict:
    """Buy a fixed-price listing. Returns the trade record (ok=True when ownership is confirmed)."""
    ok, why = limits_ok(price)
    if not ok:
        return log(dict(kind="buy", nft=nft, price=price, ok=False, reason=why, **meta))
    bal = balance()
    if bal - price - 1.0 < RESERVE_TON:
        return log(dict(kind="buy", nft=nft, price=price, ok=False, reason=f"balance {bal:.1f} too low", **meta))
    tx = gg_post(f"/v1/nfts/buy-fix-price/{nft}", {"version": version})
    total = sum(int(m["amount"]) for m in tx["list"]) / 1e9
    if total > price * 1.15 + 1.5:
        return log(dict(kind="buy", nft=nft, price=price, ok=False, reason=f"tx total {total} inconsistent with price", **meta))
    sent = sign_and_send(tx, dry_run)
    if dry_run:
        return log(dict(kind="buy", nft=nft, price=price, ok=False, dry_run=True, tx_total=total, **meta))
    state = wait_tx(tx)
    owner = None
    for _ in range(36):          # up to 3 min: Getgems indexes ownership with a lag
        try:
            d = gg(f"/v1/nft/{nft}"); owner = d.get("actualOwnerAddress") or d.get("ownerAddress")
            if owner and addr_hash(owner) == addr_hash(WALLET):
                break
        except Exception:
            pass
        time.sleep(5)
    mine = bool(owner) and addr_hash(owner) == addr_hash(WALLET)
    if not mine and state == "Ready":
        mine = True            # tx confirmed by Getgems; ownership index may still lag -> listing will retry
    return log(dict(kind="buy", nft=nft, price=price, ok=mine, tx_state=state, tx_total=total, owner=owner, seqno=sent.get("seqno"), **meta))


def list_for_sale(nft: str, full_price: float, meta: dict, dry_run=False) -> dict:
    """Relist an on-chain NFT we own at a fixed price (TON). Retries while Getgems still shows the old owner."""
    tx = None
    for attempt in range(8):
        try:
            tx = gg_post(f"/v1/nfts/put-on-sale-fix-price/{nft}", {"ownerAddress": WALLET, "fullPrice": str(int(round(full_price * 1e9)))})
            break
        except RuntimeError as e:
            if "FPS_ALREADY_ON_SALE" in str(e) or "belong" in str(e):
                time.sleep(20); continue
            raise
    if tx is None:
        return log(dict(kind="list", nft=nft, price=full_price, ok=False, reason="ownership not indexed after retries", **meta))
    sent = sign_and_send(tx, dry_run)
    if dry_run:
        return log(dict(kind="list", nft=nft, price=full_price, ok=False, dry_run=True, **meta))
    state = wait_tx(tx)
    listed = False
    for _ in range(12):
        try:
            d = gg(f"/v1/nft/{nft}")
            if (d.get("sale") or {}).get("fullPrice"):
                listed = True; break
        except Exception:
            pass
        time.sleep(5)
    return log(dict(kind="list", nft=nft, price=full_price, ok=listed, tx_state=state, seqno=sent.get("seqno"), **meta))


def flip(nft: str, version: str, price: float, target: float, meta: dict, dry_run=False) -> dict:
    """Buy then immediately relist at target. target = fill-based p25 of the model."""
    b = buy(nft, version, price, meta, dry_run)
    if not b.get("ok"):
        return b
    lst = list_for_sale(nft, round(target, 2), dict(meta, entry=price), dry_run)
    return dict(buy=b, list=lst)


if __name__ == "__main__":
    # manual: python3 trade.py buy <nft> [--send]   /  python3 trade.py list <nft> <price> [--send]
    cmd = sys.argv[1]; dry = "--send" not in sys.argv
    if cmd == "buy":
        d = gg(f"/v1/nft/{sys.argv[2]}"); s = d["sale"]
        print(json.dumps(buy(sys.argv[2], s["version"], int(s["fullPrice"]) / 1e9, dict(name=d.get("name"), manual=True), dry), ensure_ascii=False))
    elif cmd == "list":
        print(json.dumps(list_for_sale(sys.argv[2], float(sys.argv[3]), dict(manual=True), dry), ensure_ascii=False))
    elif cmd == "balance":
        print(balance())
