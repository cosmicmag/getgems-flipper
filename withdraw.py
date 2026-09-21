"""Send spare cash from the bot wallet to the owner's wallet.

The destination is hardcoded: automation must never be able to move funds anywhere else, whatever the
config or a compromised env says. Keeps WORKING_BALANCE_TON on the bot for gas, bids and flips.
"""
from __future__ import annotations
import json, os, sys, time

from trade import WALLET, balance, log, sign_and_send, STOP_FILE

OWNER = "UQArCMo24Ax10gXDjxTgJW3-GegB343yevqxGpkd964Ji6dH"   # Kirill's own wallet, the only allowed sink
WORKING_BALANCE_TON = float(os.environ.get("WORKING_BALANCE_TON", "120"))
MIN_WITHDRAW_TON = float(os.environ.get("MIN_WITHDRAW_TON", "50"))


def withdraw(amount: float, dry_run: bool = True) -> dict:
    bal = balance()
    if amount > bal - 2:
        return dict(ok=False, reason=f"amount {amount} above balance {bal:.2f}")
    tx = {"uuid": f"withdraw-{int(time.time())}",
          "list": [{"to": OWNER, "amount": str(int(round(amount * 1e9))), "payload": None, "stateInit": None,
                    "check": "", "context": []}]}
    print(f"withdraw {amount:.2f} TON -> {OWNER} (balance {bal:.2f})")
    if dry_run:
        return dict(ok=False, dry_run=True, amount=amount)
    sent = sign_and_send(tx, dry_run=False)
    try:
        from sales import tg
        tg(f"🏦 Вывод {amount:.2f} TON на твой кошелёк (на боте остаётся {bal - amount:.1f})")
    except Exception:
        pass
    return log(dict(kind="withdraw", nft=None, price=amount, ok=True, to=OWNER, seqno=sent.get("seqno")))


def sweep(dry_run: bool = True) -> dict | None:
    """Withdraw whatever sits above the working balance."""
    if os.path.exists(STOP_FILE):
        return None
    spare = balance() - WORKING_BALANCE_TON
    if spare < MIN_WITHDRAW_TON:
        print(f"nothing to sweep: spare {spare:.2f} TON below {MIN_WITHDRAW_TON}")
        return None
    return withdraw(round(spare, 2), dry_run)


if __name__ == "__main__":
    dry = "--send" not in sys.argv
    if len(sys.argv) > 1 and sys.argv[1] not in ("--send", "sweep"):
        print(json.dumps(withdraw(float(sys.argv[1]), dry), ensure_ascii=False))
    else:
        r = sweep(dry)
        print(json.dumps(r, ensure_ascii=False) if r else "no sweep")
