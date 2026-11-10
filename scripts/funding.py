"""資金費率歷史（幣安 U 本位永續），回測時逐筆扣掉（多單通常付錢、空單通常收錢）。

來源：data.binance.vision 的公開月檔，不需金鑰，GitHub 的美國伺服器也連得到。
幣安永續合約從 2019-09 開始，之前的交易沒有資金費資料（那段期間只能當作沒有資金費）。
當月的檔案要到下個月才會出現，所以最近幾週的交易可能缺資料，回測結果會標出缺多少筆。
"""
import bisect
import csv
import io
import os
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor

BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate/{s}/{s}-fundingRate-{y}-{m:02d}.zip"
FIRST = (2019, 9)


def _months(now_ms):
    t = time.gmtime(now_ms / 1000)
    y, m = FIRST
    while (y, m) < (t.tm_year, t.tm_mon):
        yield y, m
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "copilot-daily/1.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:  # 那個月還沒上市，或檔案還沒出
                return None
        except Exception:
            pass
        time.sleep(1.5 * (i + 1))
    return None


def parse_zip(data):
    """月檔是 zip 包一個 CSV：calc_time, funding_interval_hours, last_funding_rate（有沒有表頭都能讀）。"""
    out = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            with z.open(name) as f:
                for row in csv.reader(io.TextIOWrapper(f, encoding="utf-8")):
                    if row and row[0].strip().isdigit():
                        out.append((int(row[0]), float(row[-1])))
    return out


def fetch_funding(symbol, now_ms=None):
    """回傳 (時間列表, 費率列表)，時間由小到大。抓不到就回傳空列表。"""
    if os.environ.get("MOCK_DATA") == "1":
        return [], []
    now_ms = now_ms or int(time.time() * 1000)
    urls = [BASE.format(s=symbol, y=y, m=m) for y, m in _months(now_ms)]
    with ThreadPoolExecutor(8) as ex:
        blobs = list(ex.map(_get, urls))
    rows = []
    for b in blobs:
        if b:
            try:
                rows += parse_zip(b)
            except Exception:
                pass
    rows.sort()
    return [t for t, _ in rows], [r for _, r in rows]


def funding_r(trade, ts, rates):
    """這筆單持有期間（進場收盤 → 出場那根收盤）付出/收到的資金費，換算成 R。
    多單：費率為正時付錢（扣 R）；空單相反。資料沒涵蓋到這段期間就回傳 None。"""
    gap = 9 * 3600_000  # 結算最長每 8 小時一次，留 1 小時餘裕
    if not ts or trade["time"] < ts[0] - gap or trade["closed_time"] > ts[-1] + gap:
        return None
    sgn = 1 if trade["side"] == "long" else -1
    i = bisect.bisect_right(ts, trade["time"])
    j = bisect.bisect_right(ts, trade["closed_time"])
    total = sum(rates[i:j])
    return -sgn * total * trade["entry"] / trade["risk"]
