"""回測：把「現在正在跑的同一套規則」丟回過去的 K 線，看成績長怎樣。

重點：
- 用的是 scripts/engine.py，跟每 4 小時排程完全同一份程式，沒有另外調參數
- 每一根 4H 只用「當時已經收盤」的日線和 4H 資料判斷，不偷看未來
- 同一個幣同時只會有一筆單；結束後冷卻 cooldown_bars 根才能再出手（跟實盤一樣）
- 已扣手續費（config 的 fee_pct_round_trip）
- 沒算滑價、資金費率、爆倉，實際成績通常會比回測差一點

結果寫到 data/backtest.json，網站成績單會把「回測」和「實盤」並排比較。
實盤明顯比回測差 → 規則可能只是剛好適合過去，或是執行上有落差。

執行：python research/backtest.py            （預設每個幣回測約 1500 根 4H ≈ 250 天）
      python research/backtest.py --bars 3000
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


def run_symbol(sym, h4, d1, cfg):
    r = cfg["rules"]
    biases = engine.daily_bias(d1, cfg)
    ind = engine.prepare_4h(h4, cfg)
    trades, next_ok = [], 0
    for i in range(len(h4)):
        if h4[i]["ct"] < next_ok:
            continue
        bias = engine.bias_at(h4[i]["ct"], biases)
        sig = engine.check_setup(sym, i, h4, ind, bias, cfg)
        if not sig:
            continue
        engine.settle(sig, h4, cfg)
        trades.append(sig)
        if sig["status"] != "closed":
            break  # 最後一筆還沒結束，後面也不會再有新單
        next_ok = sig["closed_time"] + r["cooldown_bars"] * engine.H4
    return trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=1500, help="每個幣回測幾根 4H")
    args = ap.parse_args()
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    now = int(os.environ.get("NOW_MS") or time.time() * 1000)
    days = args.bars // 6 + 80  # 日線多抓一點給 EMA 暖身
    all_trades, per_symbol, errors, first, last = [], {}, [], None, None
    for sym in cfg["symbols"]:
        try:
            h4, _ = fetch_klines(sym, "4h", args.bars, cfg["data_sources"], now)
            d1, _ = fetch_klines(sym, "1d", days, cfg["data_sources"], now)
        except SourceError as e:
            errors.append(f"{sym}：{e}")
            continue
        trades = run_symbol(sym, h4, d1, cfg)
        all_trades += trades
        per_symbol[sym] = engine.stats(trades)
        per_symbol[sym].pop("curve", None)
        first = min(first or h4[0]["t"], h4[0]["t"])
        last = max(last or h4[-1]["ct"], h4[-1]["ct"])
        print(f"{sym}: {per_symbol[sym].get('n', 0)} 筆，勝率 {per_symbol[sym].get('win_rate', '-')}%，"
              f"平均 {per_symbol[sym].get('avg_r', '-')}R")
    st = engine.stats(all_trades)
    keep = ["symbol", "side", "setup", "time", "entry", "sl", "tp1", "tp2", "result", "r", "closed_time", "bars_held"]
    closed = [t for t in all_trades if t["status"] == "closed"]
    closed.sort(key=lambda t: t["closed_time"])
    out = {
        "generated": now, "period": [first, last], "bars": args.bars,
        "stats": st, "per_symbol": per_symbol, "errors": errors,
        "rules": cfg["rules"], "fee_pct_round_trip": cfg.get("fee_pct_round_trip", 0.1),
        "recent_trades": [{k: t.get(k) for k in keep} for t in closed[-150:]],
    }
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)  # 第一次跑時 data 資料夾還不存在
    with open(os.path.join(ROOT, "data", "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"合計 {st['n']} 筆，勝率 {st.get('win_rate', '-')}%，平均 {st.get('avg_r', '-')}R，"
          f"總計 {st.get('total_r', '-')}R，最大回撤 {st.get('max_dd_r', '-')}R")
    for e in errors:
        print("  抓不到：", e)


if __name__ == "__main__":
    main()
