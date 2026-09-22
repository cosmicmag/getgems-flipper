"""Getgems rare-trait scanner v1: Getgems listings (on-chain + offchain) vs FILL-based per-model reference.

candidate: gg_price + GAS < ref_p25 * (1 - GG_FEE)  — buy on Getgems, resell on Getgems/Portals at the model's
fill price. Portals ask floor per model is printed only as context (asks are not fills).
"""
from __future__ import annotations
import json, os, sys, time
from gg_api import gg, gg_paged, portals, nano
from refs import Fills, norm_coll

GG_FEE = 0.02
GAS = 0.3
TOP_N = int(sys.argv[1]) if len(sys.argv) > 1 else 40
ONSALE_PAGES, OFFCHAIN_PAGES, HISTORY_PAGES = 4, 2, 3
MIN_NET_PCT, MAX_NET_PCT = 15.0, 300.0
MIN_ABS_NET = 2.0


def attrs(item):
    return {a["traitType"].lower(): a["value"] for a in item.get("attributes", []) if a.get("value")}


def sale_price(item):
    s = item.get("sale") or {}
    if s.get("type") != "FixPriceSale" or s.get("currency", "TON") != "TON":
        return None
    return nano(s["fullPrice"])


def main():
    t0 = time.time()
    fills = Fills(); fills.load_onchain(); fills.load_portals()
    print(f"fills loaded: chain+portals={len(fills.rows)}")
    top = gg("/v1/gifts/collections/top", kind="week", limit=TOP_N)["items"]
    try:
        pc = portals("/collections", limit=300, offset=0, favorites_only="false") if os.environ.get("PORTALS_AUTH") else []
        pcols = pc.get("collections", pc) if isinstance(pc, dict) else pc
        p_by = {norm_coll(c["name"]): c for c in pcols}
    except Exception as e:
        print("portals collections err", e); p_by = {}

    listings_all = []
    for t in top:
        coll = t["collection"]; addr = coll["address"]; name = coll["name"] or addr
        hist = gg_paged(f"/v1/collection/history/{addr}", max_pages=HISTORY_PAGES, types=["sold"])
        fills.load_gg_history(name, hist)
        onchain = gg_paged(f"/v1/nfts/on-sale/{addr}", max_pages=ONSALE_PAGES)
        offchain = gg_paged(f"/v1/nfts/offchain/on-sale/{addr}", max_pages=OFFCHAIN_PAGES)
        seen = set(); n = 0
        for item in onchain + offchain:
            if item["address"] in seen:
                continue
            seen.add(item["address"]); n += 1
            listings_all.append((name, item))
        pcol = p_by.get(norm_coll(name))
        pf = {}
        if pcol and os.environ.get("PORTALS_AUTH"):
            try:
                f = portals("/collections/filters", short_names=pcol["short_name"])
                for m in f["collections"][pcol["short_name"]].get("models", []):
                    if m.get("floor_price") not in (None, "", "0"):
                        pf[m["name"].lower()] = (float(m["floor_price"]), m.get("supply"))
            except Exception as e:
                print(f"  portals filters err {name}: {e}")
        for nm, item in listings_all:
            if nm == name:
                item["_pf"] = pf.get(attrs(item).get("model", "").lower())
        print(f"  {name[:24]:24} listings={n} sold_hist={len(hist)} fills_total={len(fills.rows)}", flush=True)

    print(f"gg fills persisted: {fills.dump_gg()}")
    refs = fills.references()
    print(f"references: {len(refs)} (model/tier keys)")
    cands = []
    for name, item in listings_all:
        p = sale_price(item)
        if not p:
            continue
        a = attrs(item); c = norm_coll(name); model = a.get("model", "").lower(); bd = a.get("backdrop", "").lower()
        ref = refs.get((c, model, bd)) or refs.get((c, model))
        if not ref:
            continue
        target = ref["p25"]                      # conservative: sell at lower-quartile fill
        pf = item.get("_pf")
        if pf and pf[0] < target * 0.9:          # Portals already asks below our target -> model repriced, fills stale
            continue
        net = target * (1 - GG_FEE) - p - GAS
        pct = net / p * 100
        if net >= MIN_ABS_NET and MIN_NET_PCT <= pct <= MAX_NET_PCT:
            cands.append(dict(coll=name, nft=item["address"], offchain=item.get("kind") == "OffchainNft", name=item.get("name"),
                              model=a.get("model"), backdrop=a.get("backdrop"), symbol=a.get("symbol"), gg_price=p,
                              ref_med=round(ref["med"], 2), ref_p25=round(target, 2), ref_n=ref["n"], ref_age_d=ref["last_age_d"],
                              ref_srcs="+".join(ref["srcs"]), tier=(c, model, bd) in refs,
                              portals_model_floor=(item.get("_pf") or (None, None))[0], portals_supply=(item.get("_pf") or (None, None))[1],
                              net=round(net, 2), net_pct=round(pct, 1), version=(item.get("sale") or {}).get("version")))
    cands.sort(key=lambda c: -c["net"])
    json.dump(dict(candidates=cands, ts=time.time(), refs={"|".join(k): v for k, v in refs.items()}),
              open("scan_dump.json", "w"), indent=1, ensure_ascii=False)
    print(f"\n=== CANDIDATES vs fills (p25 of last {14}d), {len(cands)} ===")
    print(f"{'coll':15} {'model':16} {'backdrop':13} {'GG':>8} {'refP25':>7} {'refMed':>7} {'n':>3} {'age':>4} {'src':>10} {'P.ask':>7} {'net':>7} {'net%':>6}  nft")
    for c in cands[:40]:
        print(f"{c['coll'][:15]:15} {(c['model'] or '')[:16]:16} {(c['backdrop'] or '')[:13]:13} {c['gg_price']:>8.2f} {c['ref_p25']:>7.1f} "
              f"{c['ref_med']:>7.1f} {c['ref_n']:>3} {c['ref_age_d']:>4} {c['ref_srcs'][:10]:>10} {str(c['portals_model_floor'] or '-'):>7} "
              f"{c['net']:>7.2f} {c['net_pct']:>6.1f}  {'TIER ' if c['tier'] else ''}{'OFF ' if c['offchain'] else ''}https://getgems.io/nft/{c['nft']}")
    print(f"\ndone in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
