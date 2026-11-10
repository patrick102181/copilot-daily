"""每 4 小時跑一次：抓資料 → 結算舊訊號 → 找新訊號 → 寫成 JSON → 推播新訊號。

輸出（網站讀這些檔）：
  data/market.json   每個幣的大方向與狀態
  data/signals.json  訊號帳本（全部訊號，含已結算）
  data/stats.json    實盤成績單
執行：python scripts/update.py
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import engine  # noqa: E402
import notify  # noqa: E402
from data_source import SOURCES, SourceError, fetch_klines  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    cfg = load(os.path.join(ROOT, "config.json"), None)
    if not cfg:
        sys.exit("找不到 config.json")
    os.makedirs(DATA, exist_ok=True)
    book_path = os.path.join(DATA, "signals.json")
    book = load(book_path, {"signals": []})
    signals = book["signals"]
    known = {s["id"] for s in signals}
    now = int(os.environ.get("NOW_MS") or time.time() * 1000)  # NOW_MS 只給測試用
    r = cfg["rules"]
    market, new_sigs, errors, used_sources = [], [], [], set()

    for sym in cfg["symbols"]:
        try:
            d1, src1 = fetch_klines(sym, "1d", 260, cfg["data_sources"], now)
            h4, src4 = fetch_klines(sym, "4h", 400, cfg["data_sources"], now)
        except SourceError as e:
            errors.append(f"{sym}：抓不到資料（{e}）")
            print(f"[略過] {sym} {e}")
            continue
        used_sources.update([src1, src4])
        biases = engine.daily_bias(d1, cfg)
        ind = engine.prepare_4h(h4, cfg)

        # 1) 結算這個幣還沒結束的訊號
        for s in signals:
            if s["symbol"] == sym and s["status"] == "open":
                engine.settle(s, h4, cfg)

        # 2) 在最後一根已收盤的 4H 上找新訊號
        i = len(h4) - 1
        bias = engine.bias_at(h4[i]["ct"], biases)
        open_now = any(s["symbol"] == sym and s["status"] == "open" for s in signals)
        last_closed = max((s["closed_time"] for s in signals
                           if s["symbol"] == sym and s["status"] == "closed"), default=0)
        cooling = last_closed + r["cooldown_bars"] * engine.H4 > h4[i]["ct"]
        status = "等待"
        if open_now:
            status = "持有中"
        elif cooling:
            status = "冷卻中"
        elif bias and bias["locked"] != "盤整":
            sig = engine.check_setup(sym, i, h4, ind, bias, cfg)
            if sig and sig["id"] not in known:  # 同一根 K 線不會重複發
                signals.append(sig)
                known.add(sig["id"])
                new_sigs.append(sig)
                status = "新訊號"
        elif bias:
            status = "盤整不出手"

        b = h4[i]
        prev_day = h4[i - 6]["c"] if i >= 6 else b["c"]
        e200 = ind["ema200"][i]
        market.append({
            "symbol": sym,
            "price": b["c"],
            "change_24h": round((b["c"] / prev_day - 1) * 100, 2),
            "bias": bias["locked"] if bias else "盤整",
            "bias_raw": bias["raw"] if bias else "盤整",
            "bias_since": bias["since"] if bias else None,
            "above_ema200_4h": (b["c"] > e200) if e200 else None,
            "atr_pct": round(ind["atr"][i] / b["c"] * 100, 2) if ind["atr"][i] else None,
            "status": status,
            "bar_close": b["ct"],
            "source": src4,
        })

    if not market:
        # 全部抓不到（例如交易所或網路掛了）：什麼都不覆蓋，讓流程失敗，GitHub 會寄信通知
        for e in errors:
            print("  ", e)
        sys.exit("全部幣種都抓不到資料，這次不更新任何檔案")
    # 部分幣抓不到：沿用上一次的資料並標記，網頁不會整列消失
    prev = {m["symbol"]: m for m in load(os.path.join(DATA, "market.json"), {}).get("symbols", [])}
    got = {m["symbol"] for m in market}
    for sym in cfg["symbols"]:
        if sym not in got and sym in prev:
            market.append(dict(prev[sym], status="資料暫缺", stale=True))
    order = {s: i for i, s in enumerate(cfg["symbols"])}
    market.sort(key=lambda m: order.get(m["symbol"], 99))

    signals.sort(key=lambda s: s["time"])
    save(book_path, {"signals": signals})
    st = engine.stats(signals)
    st["updated"] = now
    save(os.path.join(DATA, "stats.json"), st)
    src_names = sorted(SOURCES[x][0] if x in SOURCES else "測試假資料" for x in used_sources)
    save(os.path.join(DATA, "market.json"), {
        "updated": now, "symbols": market, "errors": errors,
        "source_label": "、".join(src_names) or "無",
        "notional_usdt": cfg.get("notional_usdt", 1000),
    })
    print(f"完成：{len(market)} 個幣、新訊號 {len(new_sigs)} 筆、進行中 {st['open']} 筆、已結算 {st['n']} 筆")
    for e in errors:
        print("  ", e)
    if new_sigs:
        notify.send_signals(new_sigs, cfg)


if __name__ == "__main__":
    main()
