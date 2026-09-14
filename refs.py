"""Fill-based reference prices per (collection, model[, backdrop]).

Sources (all real fills, never asks):
  1. on-chain purchases decoded by toncenter (data/actions_168h.jsonl + metadata) — all venues
  2. Getgems sold history (on-chain items only; model resolved through toncenter metadata batches)
  3. Portals purchase feed collected by portals_fills.py (data/portals_fills.jsonl)
"""
from __future__ import annotations
import json, os, re, statistics, time, urllib.request
from collections import defaultdict

DESC = re.compile(r"appearance (.+?) on a (.+?) background with (.+?) icons")
UA = {"user-agent": "Mozilla/5.0 (Macintosh) Chrome/148", "accept": "application/json"}
MAX_AGE_DAYS = 14
MIN_N_MODEL = 3
MIN_N_TIER = 2


def norm_coll(name: str) -> str:
    """Singular, alnum-only collection key so Getgems 'Swiss Watches' == Portals 'Swiss Watch'."""
    n = "".join(ch for ch in (name or "").lower() if ch.isalnum())
    if n.endswith("ies"):
        return n[:-3] + "y"
    if n.endswith(("ches", "shes", "xes", "sses")):
        return n[:-2]
    if n.endswith("s") and not n.endswith("ss"):
        return n[:-1]
    return n


class Fills:
    def __init__(self):
        self.rows = []  # dict(coll, model, backdrop, price, t, src)

    def add(self, coll, model, backdrop, price, t, src):
        if not coll or not model or not price:
            return
        self.rows.append(dict(coll=norm_coll(coll), model=model.lower().strip(), backdrop=(backdrop or "").lower().strip(),
                              price=float(price), t=float(t), src=src))

    def load_onchain(self, path="data/onchain_fills.jsonl"):
        """Compact on-chain purchase fills (built from toncenter nft_transfer actions with is_purchase=true)."""
        if not os.path.exists(path):
            return
        for line in open(path):
            r = json.loads(line)
            self.add(r["coll"], r["model"], r["backdrop"], r["price"], r["t"], "chain")

    def load_portals(self, path="data/portals_fills.jsonl"):
        if not os.path.exists(path):
            return
        for line in open(path):
            r = json.loads(line)
            if r.get("bundle"):
                continue
            t = time.mktime(time.strptime(r["t"][:19], "%Y-%m-%dT%H:%M:%S"))
            self.add(r["coll"], r.get("model"), r.get("backdrop"), r["amount"], t, "portals")

    def load_gg(self, path="data/gg_fills.jsonl"):
        if not os.path.exists(path):
            return
        for line in open(path):
            r = json.loads(line)
            self.add(r["coll"], r["model"], r["backdrop"], r["price"], r["t"], "gg")

    def dump_gg(self, path="data/gg_fills.jsonl", max_age_days=30):
        """Merge this run's Getgems fills into the persisted file (dedupe by coll/model/price/t)."""
        old = []
        if os.path.exists(path):
            old = [json.loads(l) for l in open(path)]
        cutoff = time.time() - max_age_days * 86400
        keyed = {(r["coll"], r["model"], r["price"], int(r["t"])): r for r in old if r["t"] > cutoff}
        for r in self.rows:
            if r["src"] == "gg" and r["t"] > cutoff:
                keyed[(r["coll"], r["model"], r["price"], int(r["t"]))] = dict(coll=r["coll"], model=r["model"], backdrop=r["backdrop"], price=r["price"], t=r["t"])
        with open(path, "w") as f:
            for r in keyed.values():
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return len(keyed)

    def load_gg_history(self, coll_name: str, hist: list[dict]):
        """hist = Getgems /v1/collection/history items (types=sold). Resolves models via toncenter in batches of 50."""
        onchain = [h for h in hist if not h["address"].startswith("EQf_") and (h.get("typeData") or {}).get("price")]
        for i in range(0, len(onchain), 50):
            batch = onchain[i:i + 50]
            q = "&".join("address=" + h["address"] for h in batch)
            try:
                r = json.loads(urllib.request.urlopen(urllib.request.Request(
                    f"https://toncenter.com/api/v3/nft/items?{q}&limit=50", headers=UA), timeout=60).read())
            except Exception as e:
                print(f"    toncenter batch err: {e}")
                time.sleep(2); continue
            ab = r.get("address_book", {}); md = r.get("metadata", {})
            raw_by_friendly = {v.get("user_friendly"): k for k, v in ab.items()}
            for h in batch:
                raw = raw_by_friendly.get(h["address"])
                ti = (md.get(raw, {}).get("token_info") or [{}])[0] if raw else {}
                m = DESC.search(ti.get("description") or "")
                if not m:
                    continue
                td = h["typeData"]
                if td.get("currency", "TON") != "TON":
                    continue
                self.add(coll_name, m.group(1), m.group(2), float(td["price"]), h["timestamp"] / 1000, "gg")
            time.sleep(1.1)

    def references(self) -> dict:
        """{(coll, model): ref, (coll, model, backdrop): ref} where ref = dict(med, n, last_age_d, srcs)."""
        now = time.time(); cutoff = now - MAX_AGE_DAYS * 86400
        by_model, by_tier = defaultdict(list), defaultdict(list)
        seen = set()   # the same sale is reported by several sources (chain + Getgems history): count it once
        for r in self.rows:
            if r["t"] < cutoff:
                continue
            key = (r["coll"], r["model"], round(r["price"], 2), int(r["t"] // 600))
            if key in seen:
                continue
            seen.add(key)
            by_model[(r["coll"], r["model"])].append(r)
            if r["backdrop"]:
                by_tier[(r["coll"], r["model"], r["backdrop"])].append(r)
        refs = {}
        for k, rows in by_model.items():
            if len(rows) >= MIN_N_MODEL:
                refs[k] = self._ref(rows, now)
        for k, rows in by_tier.items():
            base = refs.get(k[:2])
            if len(rows) >= MIN_N_TIER and base and statistics.median(x["price"] for x in rows) >= base["med"] * 1.3:
                refs[k] = self._ref(rows, now)
        return refs

    @staticmethod
    def _ref(rows, now):
        ps = [x["price"] for x in rows]
        return dict(med=statistics.median(ps), n=len(ps), p25=sorted(ps)[len(ps) // 4],
                    last_age_d=round((now - max(x["t"] for x in rows)) / 86400, 1),
                    srcs=sorted({x["src"] for x in rows}), uniq_nums=len(rows))
