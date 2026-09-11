"""Thin clients: Getgems public-api (key from macOS keychain) and Portals (auth from env)."""
from __future__ import annotations
import json, os, subprocess, time, urllib.parse, urllib.request

GG_BASE = "https://api.getgems.io/public-api"
PORTALS_BASE = "https://portal-market.com/api"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")


def gg_key() -> str:
    k = os.environ.get("GG_KEY")
    if k:
        return k
    return subprocess.check_output(
        ["security", "find-generic-password", "-a", "kirillll", "-s", "getgems-public-api", "-w"],
        text=True).strip()


class RateLimiter:
    """Getgems allows 400 req / 5 min per IP. Keep a safe 1 req / 0.85 s."""
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self):
        now = time.time()
        d = self._last + self.min_interval - now
        if d > 0:
            time.sleep(d)
        self._last = time.time()


_gg_rl = RateLimiter(0.85)
_p_rl = RateLimiter(0.4)


def _get(url: str, headers: dict, timeout=30, retries=4) -> dict:
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (429, 500, 502, 503) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise RuntimeError(f"HTTP {e.code} {url[:90]} {body}") from None
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return {}


def gg(path: str, **params) -> dict:
    _gg_rl.wait()
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
    url = f"{GG_BASE}{path}" + (f"?{q}" if q else "")
    d = _get(url, {"accept": "application/json", "Authorization": gg_key(), "user-agent": UA})
    if not d.get("success", True):
        raise RuntimeError(f"GG error {path}: {str(d)[:200]}")
    return d.get("response", d)


def gg_paged(path: str, max_pages=5, limit=100, **params) -> list[dict]:
    out, cursor = [], None
    for _ in range(max_pages):
        r = gg(path, limit=limit, after=cursor, **params)
        items = r.get("items", [])
        out.extend(items)
        cursor = r.get("cursor")
        if not cursor or not items:
            break
    return out


def portals_headers() -> dict:
    auth = os.environ.get("PORTALS_AUTH")
    if not auth:
        raise RuntimeError("PORTALS_AUTH env is empty")
    return {"accept": "application/json", "authorization": auth,
            "cookie": os.environ.get("PORTALS_COOKIE", ""),
            "referer": "https://portal-market.com/", "user-agent": UA}


def portals(path: str, **params) -> dict:
    _p_rl.wait()
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return _get(f"{PORTALS_BASE}{path}" + (f"?{q}" if q else ""), portals_headers())


def nano(x) -> float:
    return int(x) / 1e9
