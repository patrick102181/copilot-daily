"""策略實驗室：同一批資料、同一套成本，比較幾個「事先寫死、不調參數」的策略。

為什麼要事先寫死：一直改參數直到回測好看，只是在過去的雜訊裡挑結果，實盤會打回原形。
所以這裡每個策略都用業界常見的標準設定，跑一次就看結果，不回頭微調。

驗證方式：
- 資料從 2020-01-01 起（約 7 年，涵蓋 2021 牛市、2022 熊市、之後的盤整與行情）
- 切成前段（2023-07-01 以前結算）和後段（之後結算），兩段都要站得住才算數
- 另外看每一年的成績、多空分開的成績
- 成本：手續費＋滑價，一進一出合計 0.2%（比實盤保守）
- 停損遇到跳空：用開盤價和停損價較差的那個成交

策略（2026-11-10 第一次跑的結果，含已淘汰的 S0 舊版、S1 只做突破，見 git 紀錄裡當天的 data/lab.json）：
  S0 舊版（已淘汰）  日線方向＋4H 回踩/突破，止盈 1.5R/2.5R，5 天到期 → 7 年 2213 筆平均 −0.005R
  S1 只做突破（淘汰） 同上只留突破 → +0.139R，但後段變弱、2026 年虧
  S2 4H 趨勢跟隨    ← 現在實盤用的規則（scripts/engine.py）。日線方向＋4H 收盤突破 20 根高/低點；停損 2 ATR，之後用 3 ATR 移動停損，不設止盈、不設到期
  S3 S2＋BTC 濾網   其他幣只做和 BTC 日線方向相同的單
  S4 日線海龜       日線收盤突破 50 日高/低點進場；停損 2 ATR；收盤跌破 20 日低點（空單是漲破高點）出場
  S5 日線海龜只做多  同 S4，只做多

執行：python research/strategy_lab.py   →  data/lab.json
"""
import json
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import engine  # noqa: E402
from data_source import SourceError, fetch_klines  # noqa: E402

START = 1577836800000          # 2020-01-01 UTC
SPLIT = 1688169600000          # 2023-07-01 UTC，前後段分界
COST_PCT = 0.2                 # 一進一出合計 %
DAY = 86400_000


# ---------- 共用：通用持倉模擬（移動停損／通道出場）----------
def simulate(sym, bars, entry_fn, trail_fn, exit_fn, init_stop_atr, atr_list, label):
    """逐根跑。entry_fn(i) → 'long'/'short'/None（第 i 根收盤進場）
    trail_fn(pos, i) → 新停損（只會往有利方向移動），用到第 i 根收盤為止的資訊
    exit_fn(pos, i) → True 表示第 i 根收盤出場
    停損在下一根盤中檢查；跳空就用開盤價成交。"""
    trades, pos = [], None
    for i in range(1, len(bars)):
        b = bars[i]
        if pos:
            sgn = pos["sgn"]
            hit = (b["l"] <= pos["stop"]) if sgn > 0 else (b["h"] >= pos["stop"])
            exit_px, why = None, None
            if hit:
                exit_px = min(pos["stop"], b["o"]) if sgn > 0 else max(pos["stop"], b["o"])
                why = "停損" if pos["stop"] * sgn <= pos["entry"] * sgn else "移動停損"
            elif exit_fn and exit_fn(pos, i):
                exit_px, why = b["c"], "通道出場"
            if exit_px is not None:
                gross = (exit_px - pos["entry"]) * sgn / pos["risk"]
                cost = COST_PCT / 100 * pos["entry"] / pos["risk"]
                trades.append({"symbol": sym, "side": "long" if sgn > 0 else "short", "setup": label,
                               "time": pos["time"], "closed_time": b["ct"], "result": why,
                               "r": round(gross - cost, 3), "bars_held": i - pos["i"], "status": "closed"})
                pos = None
                continue
            if trail_fn:
                new = trail_fn(pos, i)
                if new is not None and new * sgn > pos["stop"] * sgn:
                    pos["stop"] = new
            continue
        if b["ct"] < START:
            continue
        side = entry_fn(i)
        a = atr_list[i]
        if side and a:
            sgn = 1 if side == "long" else -1
            stop = b["c"] - sgn * init_stop_atr * a
            pos = {"sgn": sgn, "entry": b["c"], "stop": stop, "risk": init_stop_atr * a,
                   "i": i, "time": b["ct"], "hi": b["h"], "lo": b["l"]}
    return trades


# ---------- 各策略 ----------
def s2(sym, h4, d1, cfg, btc_bias=None):
    biases = engine.daily_bias(d1, cfg)
    a = engine.atr(h4, 14)
    n = 20

    def entry(i):
        if i < n + 1:
            return None
        bias = engine.bias_at(h4[i]["ct"], biases)
        if not bias or bias["locked"] == "盤整":
            return None
        want = "long" if bias["locked"] == "多" else "short"
        if btc_bias is not None and sym != "BTCUSDT":
            bb = engine.bias_at(h4[i]["ct"], btc_bias)
            if not bb or bb["locked"] != bias["locked"]:
                return None
        look = h4[i - n:i]
        if want == "long" and h4[i]["c"] > max(x["h"] for x in look):
            return "long"
        if want == "short" and h4[i]["c"] < min(x["l"] for x in look):
            return "short"
        return None

    def trail(pos, i):
        pos["hi"] = max(pos["hi"], h4[i]["h"])
        pos["lo"] = min(pos["lo"], h4[i]["l"])
        if not a[i]:
            return None
        return pos["hi"] - 3 * a[i] if pos["sgn"] > 0 else pos["lo"] + 3 * a[i]

    return simulate(sym, h4, entry, trail, None, 2.0, a, "4H趨勢")


def s4(sym, d1, long_only=False):
    a = engine.atr(d1, 20)
    n_in, n_out = 50, 20

    def entry(i):
        if i < n_in + 1:
            return None
        look = d1[i - n_in:i]
        if d1[i]["c"] > max(x["h"] for x in look):
            return "long"
        if not long_only and d1[i]["c"] < min(x["l"] for x in look):
            return "short"
        return None

    def exit_(pos, i):
        look = d1[i - n_out:i]
        if pos["sgn"] > 0:
            return d1[i]["c"] < min(x["l"] for x in look)
        return d1[i]["c"] > max(x["h"] for x in look)

    return simulate(sym, d1, entry, None, exit_, 2.0, a, "日線海龜")


# ---------- 統計 ----------
def summarize(trades):
    st = engine.stats(trades)
    st.pop("curve", None)
    st.pop("by_result", None)
    pre = [t for t in trades if t["closed_time"] < SPLIT]
    post = [t for t in trades if t["closed_time"] >= SPLIT]

    def brief(ts):
        s = engine.stats(ts)
        return {k: s.get(k) for k in ("n", "win_rate", "avg_r", "total_r", "profit_factor", "max_dd_r")}

    years = {}
    for t in trades:
        y = time.gmtime(t["closed_time"] / 1000).tm_year
        years.setdefault(y, []).append(t["r"])
    st["前段"] = brief(pre)
    st["後段"] = brief(post)
    st["各年"] = {y: {"n": len(v), "total_r": round(sum(v), 2)} for y, v in sorted(years.items())}
    st["年度賺錢比例"] = f"{sum(1 for v in years.values() if sum(v) > 0)}/{len(years)}"
    st["報酬回撤比"] = round(st["total_r"] / st["max_dd_r"], 2) if st.get("max_dd_r") else None
    st["最大單筆"] = max((t["r"] for t in trades), default=None)
    st["前10%獲利占比"] = None
    if trades:
        rs = sorted((t["r"] for t in trades), reverse=True)
        top = rs[:max(1, len(rs) // 10)]
        tot = sum(x for x in rs if x > 0)
        st["前10%獲利占比"] = round(sum(x for x in top if x > 0) / tot * 100, 1) if tot > 0 else None
    return st


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    now = int(os.environ.get("NOW_MS") or time.time() * 1000)
    days = (now - START) // DAY + 120
    bars4 = days * 6
    data, errors = {}, []
    for sym in cfg["symbols"]:
        try:
            h4, _ = fetch_klines(sym, "4h", bars4, cfg["data_sources"], now)
            d1, _ = fetch_klines(sym, "1d", days, cfg["data_sources"], now)
            data[sym] = (h4, d1)
            print(f"{sym}: 4H {len(h4)} 根（{time.strftime('%Y-%m-%d', time.gmtime(h4[0]['t'] / 1000))} 起），日線 {len(d1)} 根")
        except SourceError as e:
            errors.append(f"{sym}：{e}")
    btc_bias = engine.daily_bias(data["BTCUSDT"][1], cfg) if "BTCUSDT" in data else None

    runs = {
        "S2 4H趨勢跟隨": lambda s, h, d: s2(s, h, d, cfg),
        "S3 4H趨勢＋BTC濾網": lambda s, h, d: s2(s, h, d, cfg, btc_bias),
        "S4 日線海龜": lambda s, h, d: s4(s, d),
        "S5 日線海龜只做多": lambda s, h, d: s4(s, d, long_only=True),
    }
    out = {"generated": now, "start": START, "split": SPLIT, "cost_pct": COST_PCT,
           "errors": errors, "results": {}}
    for name, fn in runs.items():
        trades, per = [], {}
        for sym, (h4, d1) in data.items():
            ts = fn(sym, h4, d1)
            trades += ts
            s = engine.stats(ts)
            per[sym] = {k: s.get(k) for k in ("n", "win_rate", "avg_r", "total_r", "max_dd_r")}
        st = summarize(trades)
        st["各幣"] = per
        out["results"][name] = st
        print(f"{name}: {st.get('n')} 筆 勝率 {st.get('win_rate')}% 平均 {st.get('avg_r')}R 總計 {st.get('total_r')}R "
              f"回撤 {st.get('max_dd_r')}R｜前段 {st['前段'].get('avg_r')}R／後段 {st['後段'].get('avg_r')}R｜"
              f"年度 {st['年度賺錢比例']}")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "lab.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
