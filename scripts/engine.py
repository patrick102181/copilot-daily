"""訊號引擎 v2：4H 趨勢跟隨。

規則（2026-11 用策略實驗室、2020 年起 7 年資料驗證後選定，參數是教科書標準值，沒有微調）：
  方向：日線大方向（連續 2 天確認才換），盤整不出手
  進場：4H 收盤突破前 20 根最高點（空單：跌破前 20 根最低點），順著日線方向才做
  停損：進場 − 2 倍 ATR（空單是＋）
  出場：之後每根 4H 收盤，把停損移到「持倉期間最高價 − 3 倍 ATR」（只往有利方向移），
        被打到就出場。不設止盈、不設到期，讓大行情自己跑
  成交：停損遇到跳空，用開盤價和停損價較差的那個

為什麼這樣設計：趨勢策略本來就是勝率低（約 36%），靠少數大波段賺錢。
舊版 2.5R 就全平、5 天到期，等於把最賺錢的那段砍掉；回踩進場 7 年平均 −0.01R，沒有優勢。

即時排程（update.py）和回測（research/backtest.py）共用這一份。
"""
import bisect

H4 = 4 * 3600_000


# ---------- 指標 ----------
def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    out[n - 1] = e
    for i in range(n, len(vals)):
        e = vals[i] * k + e * (1 - k)
        out[i] = e
    return out


def atr(bars, n):
    out = [None] * len(bars)
    trs = []
    for i, b in enumerate(bars):
        pc = bars[i - 1]["c"] if i else b["c"]
        trs.append(max(b["h"] - b["l"], abs(b["h"] - pc), abs(b["l"] - pc)))
    if len(bars) <= n:
        return out
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(bars)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ---------- 日線大方向 ----------
BIAS_TEXT = {"多": "多頭", "空": "空頭", "盤整": "盤整"}


def daily_bias(dbars, cfg):
    """回傳每根日線的 {raw, locked, since}。

    raw：當天收盤的原始判斷。
      多＝收在 EMA50 上、EMA20 > EMA50、EMA50 比 5 天前高
      空＝反過來
      其他＝盤整
    locked：連續 bias_confirm_days 天 raw 一樣才換方向，否則維持原本的。
      一天最多變一次，而且要確認，所以方向不會盤中亂跳。
    """
    r = cfg["rules"]
    c = [b["c"] for b in dbars]
    e_fast, e_mid = ema(c, r["ema_fast"]), ema(c, r["ema_mid"])
    k = max(1, r["bias_confirm_days"])
    out, locked, since, raws = [], "盤整", None, []
    for i, b in enumerate(dbars):
        raw = "盤整"
        if i >= 5 and e_mid[i] is not None and e_mid[i - 5] is not None and e_fast[i] is not None:
            if c[i] > e_mid[i] and e_fast[i] > e_mid[i] and e_mid[i] > e_mid[i - 5]:
                raw = "多"
            elif c[i] < e_mid[i] and e_fast[i] < e_mid[i] and e_mid[i] < e_mid[i - 5]:
                raw = "空"
        raws.append(raw)
        if len(raws) >= k and all(x == raw for x in raws[-k:]) and raw != locked:
            locked, since = raw, b["ct"]
        out.append({"raw": raw, "locked": locked, "since": since, "ct": b["ct"]})
    return out


_cts_cache = {}


def bias_at(t_ms, bias_list):
    """在時間 t_ms 時「已經知道」的大方向（只用已收盤的日線）。"""
    key = (id(bias_list), len(bias_list))
    cts = _cts_cache.get(key)
    if cts is None or (bias_list and cts[-1] != bias_list[-1]["ct"]):
        cts = [x["ct"] for x in bias_list]
        _cts_cache.clear()
        _cts_cache[key] = cts
    j = bisect.bisect_right(cts, t_ms) - 1
    return bias_list[j] if j >= 0 else None


# ---------- 4H ----------
def prepare_4h(bars, cfg):
    r = cfg["rules"]
    c = [b["c"] for b in bars]
    return {"atr": atr(bars, r["atr_len"]), "ema200": ema(c, r["ema_slow"])}


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6g}"


def check_setup(symbol, i, bars, ind, bias, cfg):
    """第 i 根（已收盤）4H：順著日線方向，收盤突破前 N 根高點（空單跌破低點）就進場。"""
    r = cfg["rules"]
    n = r["entry_lookback"]
    if bias is None or bias["locked"] == "盤整" or i < n + 1:
        return None
    a = ind["atr"][i]
    if not a:
        return None
    b = bars[i]
    look = bars[i - n:i]
    if bias["locked"] == "多":
        if not b["c"] > max(x["h"] for x in look):
            return None
        side, sgn = "long", 1
    else:
        if not b["c"] < min(x["l"] for x in look):
            return None
        side, sgn = "short", -1
    entry = b["c"]
    risk = r["init_stop_atr"] * a
    risk_pct = risk / entry * 100
    notional = cfg.get("notional_usdt", 1000)
    return {
        "id": f"{symbol}-{b['t']}",
        "symbol": symbol, "side": side, "setup": "趨勢突破",
        "bar_t": b["t"], "time": b["ct"],
        "entry": entry, "sl": entry - sgn * risk, "stop": entry - sgn * risk,
        "risk": risk, "hi": b["h"], "lo": b["l"], "last_t": b["t"],
        "risk_pct": round(risk_pct, 2),
        "risk_usdt": round(notional * risk_pct / 100, 2),
        "bias": bias["locked"],
        "reasons": [
            f"日線{BIAS_TEXT[bias['locked']]}（已連續確認，不會盤中翻轉）",
            f"4H 收盤{'突破' if sgn > 0 else '跌破'}前 {n} 根{'最高' if sgn > 0 else '最低'}點",
            f"初始停損＝進場 {'−' if sgn > 0 else '＋'} {r['init_stop_atr']:g} 倍 ATR",
            f"出場：{r['trail_atr']:g} 倍 ATR 移動停損，不設止盈，讓行情自己跑",
        ],
        "status": "open", "bars_held": 0,
    }


# ---------- 結算（增量式）----------
def settle(sig, bars, cfg, atr_list=None):
    """只處理上次結算之後的新 K 線，所以持倉再久都能正確結算；重跑同一批 K 線不會重複計算。

    每根新 K 線：
      1. 盤中碰到目前停損 → 出場（跳空用開盤價）
      2. 沒碰到 → 收盤後把停損移到「持倉期間最高價 − trail_atr × ATR」（空單反過來），只往有利方向移
    """
    if sig["status"] == "closed":
        return sig
    r = cfg["rules"]
    if atr_list is None:
        atr_list = atr(bars, r["atr_len"])
    sgn = 1 if sig["side"] == "long" else -1
    entry, risk = sig["entry"], sig["risk"]
    fee_r = cfg.get("fee_pct_round_trip", 0.2) / 100 * entry / risk
    ts = [b["t"] for b in bars]
    j0 = bisect.bisect_right(ts, sig["last_t"])
    for j in range(j0, len(bars)):
        b = bars[j]
        sig["bars_held"] += 1
        sig["last_t"] = b["t"]
        stop = sig["stop"]
        hit = b["l"] <= stop if sgn > 0 else b["h"] >= stop
        if hit:
            px = min(stop, b["o"]) if sgn > 0 else max(stop, b["o"])
            gross = (px - entry) * sgn / risk
            sig.update(status="closed", result="停損" if stop * sgn <= entry * sgn else "移動停損出場",
                       r_gross=round(gross, 3), r=round(gross - fee_r, 3),
                       closed_time=b["ct"], exit_price=px)
            return sig
        sig["hi"] = max(sig["hi"], b["h"])
        sig["lo"] = min(sig["lo"], b["l"])
        a = atr_list[j]
        if a:
            new = sig["hi"] - r["trail_atr"] * a if sgn > 0 else sig["lo"] + r["trail_atr"] * a
            if new * sgn > stop * sgn:
                sig["stop"] = new
        sig["last_price"] = b["c"]
    if "last_price" in sig:
        sig["unrealized_r"] = round((sig["last_price"] - entry) * sgn / risk, 3)
    sig["locked_r"] = round((sig["stop"] - entry) * sgn / risk, 3)
    return sig


# ---------- 成績單 ----------
def stats(signals):
    closed = sorted([s for s in signals if s.get("status") == "closed"], key=lambda s: s["closed_time"])
    out = {"n": len(closed), "open": sum(1 for s in signals if s.get("status") == "open")}
    if not closed:
        return out
    rs = [s["r"] for s in closed]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    cum, peak, dd, curve = 0.0, 0.0, 0.0, []
    streak = worst_streak = 0
    for s in closed:
        cum += s["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append([s["closed_time"], round(cum, 3)])
        streak = streak + 1 if s["r"] <= 0 else 0
        worst_streak = max(worst_streak, streak)

    def group(key):
        g = {}
        for s in closed:
            g.setdefault(s[key], []).append(s["r"])
        return {k: {"n": len(v), "win_rate": round(sum(1 for x in v if x > 0) / len(v) * 100, 1),
                    "avg_r": round(sum(v) / len(v), 3)} for k, v in g.items()}

    out.update({
        "win_rate": round(len(wins) / len(rs) * 100, 1),
        "avg_r": round(sum(rs) / len(rs), 3),
        "total_r": round(sum(rs), 2),
        "avg_win_r": round(sum(wins) / len(wins), 3) if wins else 0,
        "avg_loss_r": round(sum(losses) / len(losses), 3) if losses else 0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "max_dd_r": round(dd, 2),
        "max_r": round(max(rs), 2),
        "worst_losing_streak": worst_streak,
        "avg_hold_bars": round(sum(s["bars_held"] for s in closed) / len(closed), 1),
        "by_setup": group("setup"),
        "by_side": group("side"),
        "by_result": {k: sum(1 for s in closed if s["result"] == k) for k in sorted({s["result"] for s in closed})},
        "curve": curve,
        "reliable": len(closed) >= 30,
    })
    return out
