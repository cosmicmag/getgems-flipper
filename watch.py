"""Near-real-time watcher: polls the Getgems new-listings feed for gifts every TICK seconds and checks each new
listing against fill-based per-model references (built offline by scan.py: data/onchain_fills.jsonl,
data/gg_fills.jsonl, data/portals_fills.jsonl). Hits go to data/watch_hits.jsonl, Telegram (if TG_BOT_TOKEN
and TG_CHAT_ID are set) and stdout. Never buys anything.

Budget: ~1 feed call per tick + 1 detail call per new listing (~2/min) -> far below Getgems' 400 req / 5 min.
"""
from __future__ import annotations
import json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from gg_api import gg, nano
from refs import Fills, norm_coll
import trade

TICK = int(os.environ.get("TICK_SECONDS", "20"))
GG_FEE, GAS = 0.02, 0.3
MIN_NET_PCT, MAX_NET_PCT, MIN_ABS_NET = 15.0, 300.0, 2.0
MAX_REF_AGE_D, MIN_REF_N = 10.0, 4
AGE_PENALTY_PCT = 5.0   # an older reference is less trustworthy, so demand a wider margin instead of dropping it:
                        # required margin = MIN_NET_PCT + AGE_PENALTY_PCT per day of reference age beyond 2 days
REFRESH_SEC = int(os.environ.get("REFS_REFRESH_SEC", "1800"))
MAX_RUN_SEC = int(os.environ.get("MAX_RUN_SEC", "0"))   # 0 = run forever
AUTO_BUY = os.environ.get("AUTO_BUY") == "1"          # live trading only when explicitly enabled
ALERT_HITS = os.environ.get("ALERT_HITS", "1") == "1"  # the GitHub watcher alerts hits; a local trader may alert trades only
HITS = "data/watch_hits.jsonl"
DESC = re.compile(r"appearance (.+?) on a (.+?) background with (.+?) icons")


def tg(text: str):
    tok, chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if not tok or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "1"}).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{tok}/sendMessage", data=data, timeout=15)
    except Exception as e:
        print("tg err", e, file=sys.stderr)


def build_refs():
    f = Fills(); f.load_onchain(); f.load_gg(); f.load_portals()
    refs = f.references()
    print(time.strftime("%H:%M:%S"), f"refs rebuilt: {len(refs)} keys from {len(f.rows)} fills", flush=True)
    return refs


def git_sync():
    """Pull newer fills committed by the hourly scan; push our hits. Safe no-op outside a git checkout."""
    try:
        subprocess.run(["git", "add", HITS, "data/trades.jsonl"], check=False, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "watch: hits"], check=False, capture_output=True)
        for attempt in range(4):
            subprocess.run(["git", "fetch", "-q", "origin", "main"], check=False, capture_output=True)
            rb = subprocess.run(["git", "rebase", "-X", "theirs", "origin/main"], check=False, capture_output=True)
            if rb.returncode != 0:
                # keep our appended records: abort, reset to remote, re-append and commit again
                subprocess.run(["git", "rebase", "--abort"], check=False, capture_output=True)
                keep = {}
                for fpath in (HITS, "data/trades.jsonl"):
                    if os.path.exists(fpath):
                        keep[fpath] = open(fpath).read()
                subprocess.run(["git", "reset", "--hard", "origin/main"], check=False, capture_output=True)
                for fpath, content in keep.items():
                    have = open(fpath).read() if os.path.exists(fpath) else ""
                    missing = [l for l in content.splitlines() if l and l not in have]
                    if missing:
                        with open(fpath, "a") as f:
                            f.write("\n".join(missing) + "\n")
                subprocess.run(["git", "add", HITS, "data/trades.jsonl"], check=False, capture_output=True)
                subprocess.run(["git", "commit", "-qm", "watch: hits/trades (re-applied)"], check=False, capture_output=True)
            if subprocess.run(["git", "push", "-q"], check=False, capture_output=True).returncode == 0:
                return
            time.sleep(5 + attempt * 5)
    except Exception as e:
        print("git sync err", e, file=sys.stderr)


def coll_names():
    top = gg("/v1/gifts/collections/top", kind="week", limit=50)["items"]
    return {t["collection"]["address"]: t["collection"]["name"] for t in top}


def main():
    refs = build_refs(); names = coll_names(); last_refresh = time.time(); started = time.time(); last_paper = 0.0
    seen = set(); last_ts = int(time.time() * 1000) - 5 * 60 * 1000
    hits_total = 0
    print(f"watching {len(names)} collections, tick {TICK}s", flush=True)
    tg(f"👀 getgems watcher up: {len(refs)} refs, {len(names)} collections")
    while True:
        try:
            feed = gg("/v1/nfts/history/gifts", limit=100, types=["putUpForSale"], minTime=last_ts)["items"]
            new = [x for x in feed if x["hash"] not in seen]
            for x in new:
                seen.add(x["hash"]); last_ts = max(last_ts, x["timestamp"] - 1)
                td = x.get("typeData") or {}
                if td.get("currency", "TON") != "TON" or not td.get("priceNano"):
                    continue
                cname = names.get(x.get("collectionAddress"))
                if not cname:
                    continue                                   # not a tracked top collection
                price = nano(td["priceNano"])
                d = gg(f"/v1/nft/{x['address']}")
                m = DESC.search(d.get("description") or "")
                if not m:
                    continue
                c = norm_coll(cname); model, bd = m.group(1).lower(), m.group(2).lower()
                ref = refs.get((c, model, bd)) or refs.get((c, model))
                if not ref:
                    continue
                target = ref["p25"]; net = target * (1 - GG_FEE) - price - GAS; pct = net / price * 100
                required_pct = MIN_NET_PCT + AGE_PENALTY_PCT * max(0.0, ref["last_age_d"] - 2.0)
                usable = ref["last_age_d"] <= MAX_REF_AGE_D and ref["n"] >= MIN_REF_N
                status = ("HIT" if (usable and net >= MIN_ABS_NET and required_pct <= pct <= MAX_NET_PCT)
                          else "weak" if net >= MIN_ABS_NET and pct >= MIN_NET_PCT else "seen")
                line = (f"{time.strftime('%H:%M:%S')} {status:4} {cname[:16]:16} {m.group(1)[:16]:16} {m.group(2)[:12]:12} "
                        f"price {price:>8.2f} refP25 {target:>7.1f} med {ref['med']:>7.1f} n={ref['n']} age={ref['last_age_d']}d "
                        f"net {net:>7.2f} ({pct:5.1f}% vs {required_pct:4.1f}% req) https://getgems.io/nft/{x['address']}")
                print(line, flush=True)
                if status == "HIT":
                    hits_total += 1
                    rec = dict(t=time.time(), coll=cname, nft=x["address"], offchain=x.get("isOffchain"), model=m.group(1),
                               backdrop=m.group(2), symbol=m.group(3), price=price, ref_p25=target, ref_med=ref["med"],
                               ref_n=ref["n"], ref_age_d=ref["last_age_d"], net=round(net, 2), net_pct=round(pct, 1),
                               version=(d.get("sale") or {}).get("version"))
                    with open(HITS, "a") as f:
                        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    if ALERT_HITS: tg(f"🎯 {cname} · {m.group(1)} / {m.group(2)}\nлистинг {price:.2f} TON, филлы p25 {target:.1f} (med {ref['med']:.1f}, n={ref['n']}, {ref['last_age_d']}d)\n"
                       f"net ≈ +{net:.1f} TON ({pct:.0f}%)\nhttps://getgems.io/nft/{x['address']}")
                    if AUTO_BUY:
                        if d.get("kind") != "CollectionItem":
                            print("  skip auto-buy: offchain gift (relist needs SignData)", flush=True)
                        else:
                            try:
                                res = trade.flip(x["address"], (d.get("sale") or {}).get("version"), price, target, rec)
                                b = res.get("buy", res); l = res.get("list") or {}
                                if b.get("ok"):
                                    tg(f"✅ КУПИЛ {cname} {m.group(1)} за {price:.2f}, выставил за {target:.2f}"
                                       f" ({'листинг ок' if l.get('ok') else 'листинг НЕ подтверждён: ' + str(l.get('tx_state'))})")
                                elif str(b.get("reason", "")).startswith("lost race"):
                                    tg(f"⏱ не успел: {cname} {m.group(1)} за {price:.2f} уже купили до нас")
                                else:
                                    tg(f"⛔ не купил {cname} {m.group(1)}: {b.get('reason') or b.get('tx_state')}")
                            except Exception as e:
                                print("auto-buy err", e, file=sys.stderr, flush=True); tg(f"💥 auto-buy error: {str(e)[:200]}")
            if len(seen) > 5000:
                seen = set(list(seen)[-2000:])
            if time.time() - last_refresh > REFRESH_SEC:
                git_sync(); refs = build_refs(); names = coll_names(); last_refresh = time.time()
            if time.time() - last_paper > 3600 and os.environ.get("GITHUB_ACTIONS"):
                # GitHub skips many scheduled runs; trigger the hourly reference rebuild ourselves
                subprocess.run(["gh", "workflow", "run", "paper.yml"], check=False, capture_output=True,
                               env={**os.environ, "GH_TOKEN": os.environ.get("GH_TOKEN", os.environ.get("GITHUB_TOKEN", ""))})
                last_paper = time.time()
            if MAX_RUN_SEC and time.time() - started > MAX_RUN_SEC:
                git_sync(); print("max run time reached, exiting for re-dispatch", flush=True); return
        except Exception as e:
            print("tick err", e, file=sys.stderr, flush=True); time.sleep(10)
        time.sleep(TICK)


if __name__ == "__main__":
    main()
