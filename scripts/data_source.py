"""抓 K 線。只用 Python 內建模組，不用 pip install。

回傳格式（每根 K 線一個 dict）：
  {"t": 開盤時間毫秒, "ct": 收盤時間毫秒, "o", "h", "l", "c", "v"}
只回傳「已經收盤」的 K 線，正在跑的那根會丟掉，避免訊號跟著盤中價格跳來跳去。

環境變數 MOCK_DATA=1 時改用假資料（離線測試用，數字沒有意義）。
"""
import json
import os
import random
import time
import urllib.error
import urllib.request

SOURCES = {
    # 幣安 USDT 永續合約。GitHub 美國伺服器會回 451（地區封鎖）。
    "binance_futures": ("合約", "https://fapi.binance.com/fapi/v1/klines"),
    # 幣安現貨公開行情（只讀），美國伺服器一般連得到。價格和合約差很小。
    "binance_spot_vision": ("現貨", "https://data-api.binance.vision/api/v3/klines"),
}

INTERVAL_MS = {"4h": 4 * 3600_000, "1d": 86400_000}


class SourceError(Exception):
    pass


class Blocked(SourceError):
    """整個資料源連不上（地區封鎖、代理擋掉）。這次執行剩下的幣都不再試它。"""


_dead = set()  # 本次執行已確定連不上的資料源


def _get(url, tries=3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "copilot-daily/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # 451／403＝地區封鎖；400＝沒有這個交易對。這些重試也沒用。
            if e.code in (403, 451):
                raise Blocked(f"HTTP {e.code}（地區封鎖）")
            if e.code == 400:
                raise SourceError("HTTP 400（可能沒有這個交易對）")
            last = e
        except urllib.error.URLError as e:
            if "Tunnel connection failed" in str(e.reason):  # 代理直接拒絕，重試也沒用
                raise Blocked(str(e.reason))
            last = e
        except Exception as e:  # 網路抖動，等一下再試
            last = e
        if i < tries - 1:
            time.sleep(1.5 * (i + 1))
    raise SourceError(str(last))


def _parse(rows, now_ms):
    out = []
    for k in rows:
        bar = {"t": int(k[0]), "o": float(k[1]), "h": float(k[2]), "l": float(k[3]),
               "c": float(k[4]), "v": float(k[5]), "ct": int(k[6])}
        if bar["ct"] < now_ms:  # 只留已收盤的
            out.append(bar)
    return out


def fetch_klines(symbol, interval, limit, sources, now_ms=None):
    """依序嘗試資料源，回傳 (K 線, 用到的資料源名稱)。全部失敗就丟 SourceError。"""
    now_ms = now_ms or int(time.time() * 1000)
    if os.environ.get("MOCK_DATA") == "1":
        return mock_klines(symbol, interval, limit, now_ms), "mock"
    errors = []
    for name in sources:
        if name in _dead:
            errors.append(f"{name}: 本次已確定連不上，略過")
            continue
        _, base = SOURCES[name]
        try:
            # 幣安單次上限：合約 1500、現貨 1000
            cap = 1500 if name == "binance_futures" else 1000
            rows, end = [], None
            need = limit + 1
            while need > 0:
                n = min(cap, need)
                url = f"{base}?symbol={symbol}&interval={interval}&limit={n}"
                if end:
                    url += f"&endTime={end}"
                part = _get(url)
                if not part:
                    break
                rows = part + rows
                need -= len(part)
                end = int(part[0][0]) - 1
                if len(part) < n:
                    break
            bars = _parse(rows, now_ms)
            if bars:
                return bars[-limit:], name
            errors.append(f"{name}: 沒有資料")
        except Blocked as e:
            _dead.add(name)
            errors.append(f"{name}: {e}")
        except SourceError as e:
            errors.append(f"{name}: {e}")
    raise SourceError("；".join(errors))


# ---------- 假資料（只給離線測試）----------
MOCK_START = 1735689600000  # 假資料從 2025-01-01（UTC）開始，時間軸固定，重跑結果一致


def _mock_4h(symbol, now_ms):
    """同一個幣種固定一條 4H 走勢（以 UTC 日界線對齊），日線由它合併出來，兩者一致。"""
    step = INTERVAL_MS["4h"]
    day = INTERVAL_MS["1d"]
    rng = random.Random(symbol)
    start_price = {"BTCUSDT": 60000, "ETHUSDT": 3000, "SOLUSDT": 150}.get(symbol, rng.uniform(0.2, 50))
    t0 = MOCK_START
    n = (now_ms - t0) // step + 1
    bars, p, drift = [], start_price, 0.0
    for i in range(n):
        if i % 60 == 0:
            drift = rng.choice([0.004, -0.004, 0.0015, -0.0015, 0.0])
        o = p
        c = o * (1 + drift + rng.gauss(0, 0.012))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.006)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.006)))
        v = rng.uniform(800, 1200) * (3 if rng.random() < 0.04 else 1)
        t = t0 + i * step
        bars.append({"t": t, "ct": t + step - 1, "o": o, "h": h, "l": l, "c": c, "v": v})
        p = c
    return bars


def mock_klines(symbol, interval, limit, now_ms):
    b4 = _mock_4h(symbol, now_ms)
    if interval == "4h":
        bars = b4
    else:
        day = INTERVAL_MS["1d"]
        groups = {}
        for b in b4:
            groups.setdefault(b["t"] // day, []).append(b)
        bars = []
        for d in sorted(groups):
            g = groups[d]
            t = d * day
            bars.append({"t": t, "ct": t + day - 1, "o": g[0]["o"], "h": max(x["h"] for x in g),
                         "l": min(x["l"] for x in g), "c": g[-1]["c"], "v": sum(x["v"] for x in g)})
    return [b for b in bars if b["ct"] < now_ms][-limit:]
