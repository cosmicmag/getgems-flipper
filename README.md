# getgems-flipper

Rare-trait scanner and trading tooling for Telegram gifts on Getgems (buy on Getgems, resell at the model's fill price).

## Layout
- `gg_api.py` — Getgems public-api client (key from keychain `getgems-public-api`, rate-limited) and Portals client (env `PORTALS_AUTH`, `PORTALS_COOKIE`).
- `refs.py` — fill-based reference prices per (collection, model[, backdrop]) from on-chain purchases, Getgems sold history and the Portals purchase feed. Asks are never used as references.
- `scan.py [TOP_N]` — scans Getgems listings (on-chain + offchain) of the top gift collections and prints candidates; dump in `scan_dump.json`.
- `portals_fills.py` — long-running collector of Portals purchases into `data/portals_fills.jsonl` (the feed only exposes the last 20 actions per page).
- `signer/send.ts` — signs and broadcasts Getgems transaction lists with the bot wallet (v5r1, mnemonic in keychain `getgems-flipper-wallet`). `--dry-run` by default, `--send` to broadcast, `--balance` to check the wallet.

## Run
```bash
export PORTALS_AUTH='tma user=...' PORTALS_COOKIE='...'
python3 scan.py 20
nohup python3 portals_fills.py > portals_fills.log 2>&1 &
cd signer && npx tsx send.ts --balance
```

## Fees
Getgems marketplace fee on gifts: 2% (from sale objects). Portals: 5%. Gas budget: 0.3 TON per operation.
