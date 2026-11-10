"""回測：把「現在正在跑的同一套規則」丟回過去的 K 線，看成績長怎樣。

重點：
- 用的是 scripts/engine.py，跟每 4 小時排程完全同一份程式，沒有另外調參數
- 每一根 4H 只用「當時已經收盤」的日線和 4H 資料判斷，不偷看未來
- 同一個幣同時只會有一筆單；結束後冷卻 cooldown_bars 根才能再出手（跟實盤一樣）
- 已扣手續費加滑價（config 的 fee_pct_round_trip，預設 0.2%）
- 已逐筆扣掉幣安真實的歷史資金費率（2019-09 永續上市後才有資料；之前的交易和最近一個月內的交易沒有資金費資料）
- 沒算爆倉；價格是現貨，跟合約價格有小差距
- 分三段看：2017-08～2019-12 是挑策略時「完全沒看過」的資料（含 2018 大熊市），最能說明規則是不是真的有效

結果寫到 data/backtest.json，網站成績單會把「回測」和「實盤」並排比較。
實盤明顯比回測差 → 規則可能只是剛好適合過去，或是執行上有落差。

執行：python research/backtest.py            （預設每個幣回測 21000 根 4H ≈ 9.5 年，幣安資料最早到 2017-08，不夠的幣就從上市開始）
      python research/backtest.py --bars 3000   （只看最近約 500 天）
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import engine  # noqa: E402
from data_source import SourceError, fetch_klines  # noqa: E402
from funding import fetch_funding, funding_r  # noqa: E402

PERIODS = [
    ("2017-08～2019-12（挑策略時沒看過）", 0, 1577836800000),
    ("2020-01～2023-06", 1577836800000, 1688169600000),
    ("2023-07～現在", 1688169600000, 1 << 62),
]


def run_symbol(sym, h4, d1, cfg):
    r = cfg["rules"]
    biases = engine.daily_bias(d1, cfg)
    ind = engine.prepare_4h(h4, cfg)
    trades, next_ok = [], 0
    for i in range(len(h4)):
        if h4[i]["ct"] <= next_ok:  # 出場那根不立刻再進（跟實盤一樣）
            continue
        bias = engine.bias_at(h4[i]["ct"], biases)
        sig = engine.check_setup(sym, i, h4, ind, bias, cfg)
        if not sig:
            continue
        engine.settle(sig, h4, cfg, ind["atr"])
        trades.append(sig)
        if sig["status"] != "closed":
            break  # 最後一筆還沒結束，後面也不會再有新單
        next_ok = sig["closed_time"] + r["cooldown_bars"] * engine.H4
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=21000, help="每個幣回測幾根 4H（21000 ≈ 9.5 年）")
    args = ap.parse_args()
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    now = int(os.environ.get("NOW_MS") or time.time() * 1000)
    days = args.bars // 6 + 80  # 日線多抓一點給 EMA 暖身
    all_trades, per_symbol, errors, first, last = [], {}, [], None, None
    funding_info = {}
    for sym in cfg["symbols"]:
        try:
            h4, _ = fetch_klines(sym, "4h", args.bars, cfg["data_sources"], now)
            d1, _ = fetch_klines(sym, "1d", days, cfg["data_sources"], now)
        except SourceError as e:
            errors.append(f"{sym}：{e}")
            continue
        trades = run_symbol(sym, h4, d1, cfg)
        # 逐筆加上資金費率
        fts, frs = fetch_funding(sym, now) if cfg.get("include_funding", True) else ([], [])
        n_fund = 0
        for t in trades:
            if t["status"] != "closed":
                continue
            t["r_ex_funding"] = t["r"]
            fr = funding_r(t, fts, frs)
            t["r_funding"] = None if fr is None else round(fr, 3)
            if fr is not None:
                t["r"] = round(t["r"] + fr, 3)
                n_fund += 1
        funding_info[sym] = {"records": len(fts), "trades_with_funding": n_fund,
                             "from": time.strftime("%Y-%m-%d", time.gmtime(fts[0] / 1000)) if fts else None}
        print(f"  {sym} 資金費率 {len(fts)} 筆紀錄，套用到 {n_fund} 筆交易")
        all_trades += trades
        per_symbol[sym] = engine.stats(trades)
        per_symbol[sym].pop("curve", None)
        first = min(first or h4[0]["t"], h4[0]["t"])
        last = max(last or h4[-1]["ct"], h4[-1]["ct"])
        print(f"{sym}: {per_symbol[sym].get('n', 0)} 筆，勝率 {per_symbol[sym].get('win_rate', '-')}%，"
              f"平均 {per_symbol[sym].get('avg_r', '-')}R")
    st = engine.stats(all_trades)
    keep = ["symbol", "side", "setup", "time", "entry", "sl", "exit_price", "result", "r", "r_funding", "closed_time", "bars_held"]
    closed = [t for t in all_trades if t["status"] == "closed"]
    closed.sort(key=lambda t: t["closed_time"])
    years = {}
    for t in closed:
        y = time.gmtime(t["closed_time"] / 1000).tm_year
        years.setdefault(str(y), []).append(t)

    def brief(ts):
        s = engine.stats(ts)
        b = {k: s.get(k) for k in ("n", "win_rate", "avg_r", "total_r", "profit_factor", "max_dd_r")}
        fs = [t["r_funding"] for t in ts if t.get("r_funding") is not None]
        b["funding_r_total"] = round(sum(fs), 2)
        b["funding_r_avg"] = round(sum(fs) / len(fs), 3) if fs else None
        return b

    ex = [dict(t, r=t.get("r_ex_funding", t["r"])) for t in closed]
    st_ex = engine.stats(ex)
    fund_all = [t["r_funding"] for t in closed if t.get("r_funding") is not None]
    side_funding = {}
    for sd in ("long", "short"):
        fs = [t["r_funding"] for t in closed if t["side"] == sd and t.get("r_funding") is not None]
        side_funding[sd] = round(sum(fs) / len(fs), 3) if fs else None
    out = {
        "years": {y: {"n": len(v), "total_r": round(sum(t["r"] for t in v), 2),
                      "win_rate": round(sum(1 for t in v if t["r"] > 0) / len(v) * 100, 1),
                      "funding_r": round(sum(t.get("r_funding") or 0 for t in v), 2)} for y, v in sorted(years.items())},
        "periods": {name: brief([t for t in closed if a <= t["closed_time"] < b]) for name, a, b in PERIODS},
        "funding": {"per_symbol": funding_info, "trades_with_funding": len(fund_all), "trades_total": len(closed),
                    "avg_r": round(sum(fund_all) / len(fund_all), 3) if fund_all else None,
                    "total_r": round(sum(fund_all), 2), "by_side_avg_r": side_funding,
                    "stats_ex_funding": {k: st_ex.get(k) for k in ("n", "win_rate", "avg_r", "total_r", "profit_factor", "max_dd_r")}},
        "generated": now, "period": [first, last], "bars": args.bars,
        "stats": st, "per_symbol": per_symbol, "errors": errors,
        "rules": cfg["rules"], "fee_pct_round_trip": cfg.get("fee_pct_round_trip", 0.2),
        "recent_trades": [{k: t.get(k) for k in keep} for t in closed[-150:]],
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)  # 第一次跑時 data 資料夾還不存在
    with open(os.path.join(ROOT, "data", "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"合計 {st['n']} 筆，勝率 {st.get('win_rate', '-')}%，平均 {st.get('avg_r', '-')}R，"
          f"總計 {st.get('total_r', '-')}R，最大回撤 {st.get('max_dd_r', '-')}R")
    print(f"資金費率：{len(fund_all)}/{len(closed)} 筆有資料，平均每筆 {out['funding']['avg_r']}R，"
          f"多單 {side_funding['long']}R／空單 {side_funding['short']}R；不含資金費時平均 {st_ex.get('avg_r')}R")
    for name, b in out["periods"].items():
        print(f"  {name}: {b['n']} 筆，勝率 {b['win_rate']}%，平均 {b['avg_r']}R，總計 {b['total_r']}R，回撤 {b['max_dd_r']}R")
    for e in errors:
        print("  抓不到：", e)


if __name__ == "__main__":
    main()
