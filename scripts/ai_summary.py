"""AI 總結：把程式算好的數字交給 Claude「寫成人話」，存到 data/summary.json。

分工原則（跟市場雷達一樣）：
  數字全部由程式算 → Claude 只負責解讀與表達，不准自己生數字
  沒設 ANTHROPIC_API_KEY 或呼叫失敗 → 改用規則組句，網站照樣有總結
執行：python scripts/ai_summary.py
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(__file__))
from engine import fmt_price  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
DATA = os.path.join(ROOT, "data")


def load(name, default=None):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def facts():
    """整理成給 AI 的精簡事實包（只放已經算好的數字）。"""
    market = load("market.json", {}) or {}
    stats = load("stats.json", {}) or {}
    bt = (load("backtest.json", {}) or {}).get("stats", {})
    sigs = (load("signals.json", {}) or {}).get("signals", [])
    open_sigs = [{k: s[k] for k in ("symbol", "side", "setup", "entry", "sl", "tp1", "tp2", "risk_pct", "stage")}
                 | {"unrealized_r": s.get("unrealized_r")} for s in sigs if s["status"] == "open"]
    recent = [{k: s.get(k) for k in ("symbol", "side", "setup", "result", "r")}
              for s in sorted([s for s in sigs if s["status"] == "closed"], key=lambda s: s["closed_time"])[-8:]]
    pick = ("n", "win_rate", "avg_r", "total_r", "max_dd_r", "profit_factor", "by_setup", "reliable")
    return {
        "市場": [{k: m[k] for k in ("symbol", "price", "change_24h", "bias", "bias_raw", "status", "atr_pct")}
               for m in market.get("symbols", [])],
        "進行中訊號": open_sigs,
        "最近結算": recent,
        "實盤成績": {k: stats.get(k) for k in pick},
        "回測成績": {k: bt.get(k) for k in pick},
    }


PROMPT = """你是交易副駕的「慢速層」解說員，讀者是會看技術分析、做加密貨幣永續合約的台灣交易者。
下面的 JSON 是程式已經算好的事實。請用繁體中文（台灣用語）寫一段總結，規則：

1. 只能使用 JSON 裡出現的數字，不可以自己推算或編造任何價格、機率、目標價
2. 結構：先一句話講整體方向（幾個多頭、幾個空頭、幾個盤整）→ 進行中訊號的狀況 → 成績單（實盤 vs 回測）
3. 實盤樣本少於 30 筆（reliable=false）時，要明說「樣本還太少，勝率參考價值有限」
4. 實盤平均 R 明顯低於回測時，要點出來
5. 「bias_raw」和「bias」不同，代表方向正在醞釀改變但還沒確認，可以提一句
6. 語氣冷靜、具體，不喊單、不保證獲利；全文 250 字以內，不用標題和條列符號

JSON：
"""


def ask_claude(f, cfg):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    body = json.dumps({
        "model": cfg["ai"]["model"],
        "max_tokens": cfg["ai"]["max_tokens"],
        "messages": [{"role": "user", "content": PROMPT + json.dumps(f, ensure_ascii=False)}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            res = json.load(r)
        text = "".join(b.get("text", "") for b in res.get("content", []) if b.get("type") == "text").strip()
        return text or None
    except Exception as e:
        print("Claude 呼叫失敗，改用規則組句：", e)
        return None


def rule_text(f):
    m = f["市場"]
    cnt = {k: sum(1 for x in m if x["bias"] == k) for k in ("多", "空", "盤整")}
    parts = [f"觀察 {len(m)} 個幣：日線多頭 {cnt['多']} 個、空頭 {cnt['空']} 個、盤整 {cnt['盤整']} 個。"]
    brewing = [x["symbol"] for x in m if x["bias_raw"] != x["bias"]]
    if brewing:
        parts.append(f"{'、'.join(brewing)} 的方向正在醞釀轉變，還沒確認。")
    o = f["進行中訊號"]
    if o:
        parts.append("進行中：" + "；".join(
            f"{s['symbol']} {'多' if s['side'] == 'long' else '空'}單（{s['setup']}），進場 {fmt_price(s['entry'])}、"
            f"停損 {fmt_price(s['sl'])}{'，已過止盈 1、停損移到成本' if s['stage'] else ''}" for s in o) + "。")
    else:
        parts.append("目前沒有進行中的訊號。")
    lv, bt = f["實盤成績"], f["回測成績"]
    if lv.get("n"):
        parts.append(f"實盤已結算 {lv['n']} 筆，勝率 {lv['win_rate']}%、平均 {lv['avg_r']}R。")
        if not lv.get("reliable"):
            parts.append("樣本還太少，勝率參考價值有限。")
    if bt.get("n"):
        parts.append(f"回測 {bt['n']} 筆，勝率 {bt['win_rate']}%、平均 {bt['avg_r']}R。")
    return "".join(parts)


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as fh:
        cfg = json.load(fh)
    f = facts()
    text = ask_claude(f, cfg)
    out = {"generated": int(time.time() * 1000), "text": text or rule_text(f),
           "by": cfg["ai"]["model"] if text else "規則組句"}
    with open(os.path.join(DATA, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"總結完成（{out['by']}）")


if __name__ == "__main__":
    main()
