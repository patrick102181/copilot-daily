"""訊號引擎：指標、日線大方向（鎖定）、4H 進場、停損止盈、結算、成績單。

即時排程（update.py）和回測（research/backtest.py）共用這一份，
所以回測測到的規則，就是實際在跑的規則。
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


def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
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


def macd_hist(closes, fast=12, slow=26, sig=9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), len(line))
    sig_part = ema([v for v in line[start:]], sig)
    signal = [None] * start + sig_part
    return [l - s if l is not None and s is not None else None for l, s in zip(line, signal)]


# ---------- 日線大方向 ----------
BIAS_TEXT = {"多": "多頭", "空": "空頭", "盤整": "盤整"}


def daily_bias(dbars, cfg):
    """回傳每根日線的 {raw, locked, since}。

    raw：當天收盤的原始判斷。
      多＝收在 EMA50 上、EMA20 > EMA50、EMA50 比 5 天前高
      空＝反過來
      其他＝盤整
    locked：連續 bias_confirm_days 天 raw 一樣才換方向，否則維持原本的。
      這就是「方向不會 5 分鐘一變」的關鍵：一天最多變一次，而且要確認。
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
        out.append({"raw": raw, "locked": locked, "since": since, "ct": b["ct"],
                    "ema20": e_fast[i], "ema50": e_mid[i]})
    return out


def bias_at(t_ms, bias_list):
    """在時間 t_ms 時「已經知道」的大方向（只用已收盤的日線）。"""
    cts = [x["ct"] for x in bias_list]
    j = bisect.bisect_right(cts, t_ms) - 1
    return bias_list[j] if j >= 0 else None


# ---------- 4H 進場 ----------
def prepare_4h(bars, cfg):
    r = cfg["rules"]
    c = [b["c"] for b in bars]
    return {
        "ema20": ema(c, r["ema_fast"]),
        "ema50": ema(c, r["ema_mid"]),
        "ema200": ema(c, r["ema_slow"]),
        "atr": atr(bars, r["atr_len"]),
        "hist": macd_hist(c),
        "volsma": sma([b["v"] for b in bars], 20),
    }


def fmt_price(p):
    if p >= 1000:
        return f"{p:,.1f}"
    if p >= 1:
        return f"{p:,.4f}".rstrip("0").rstrip(".")
    return f"{p:.6g}"


def check_setup(symbol, i, bars, ind, bias, cfg):
    """在第 i 根（已收盤）4H K 線上找進場。只做順著日線大方向的單，盤整不出手。"""
    r = cfg["rules"]
    if bias is None or bias["locked"] == "盤整" or i < max(r["ema_mid"], r["breakout_lookback"] + 1, r["swing_lookback"], 35):
        return None
    b = bars[i]
    e20, e50, a, h, h_prev, vs = (ind["ema20"][i], ind["ema50"][i], ind["atr"][i],
                                  ind["hist"][i], ind["hist"][i - 1], ind["volsma"][i - 1])
    if None in (e20, e50, a, h, h_prev, vs):
        return None
    side = "long" if bias["locked"] == "多" else "short"
    sgn = 1 if side == "long" else -1
    look = bars[i - r["breakout_lookback"]:i]
    swing = bars[i - r["swing_lookback"] + 1:i + 1]
    touch = r["pullback_touch_pct"] / 100
    setup, reasons, sl = None, [], None

    # 突破：收盤突破前 N 根高點（空單是跌破低點），而且量放大
    if side == "long":
        brk = b["c"] > max(x["h"] for x in look) and b["c"] > e20
    else:
        brk = b["c"] < min(x["l"] for x in look) and b["c"] < e20
    if brk and b["v"] > r["breakout_volume_mult"] * vs:
        setup = "突破"
        sl = b["c"] - sgn * r["breakout_stop_atr"] * a
        reasons.append(f"4H 收盤{'突破' if sgn > 0 else '跌破'}前 {r['breakout_lookback']} 根{'高' if sgn > 0 else '低'}點，"
                       f"量是均量的 {b['v'] / vs:.1f} 倍")
    # 回踩：碰到 EMA20 後收回來、MACD 柱轉強
    else:
        if side == "long":
            ok = (b["l"] <= e20 * (1 + touch) and b["c"] > e20 and b["c"] > e50 and b["c"] > b["o"] and h > h_prev)
        else:
            ok = (b["h"] >= e20 * (1 - touch) and b["c"] < e20 and b["c"] < e50 and b["c"] < b["o"] and h < h_prev)
        if ok:
            setup = "回踩"
            if side == "long":
                sl = min(x["l"] for x in swing) - r["stop_buffer_atr"] * a
            else:
                sl = max(x["h"] for x in swing) + r["stop_buffer_atr"] * a
            reasons.append(f"4H 回踩 EMA20 後收{'回上方' if sgn > 0 else '回下方'}，MACD 柱{'轉強' if sgn > 0 else '轉弱'}")
    if not setup:
        return None

    entry = b["c"]
    risk = (entry - sl) * sgn
    risk_pct = risk / entry * 100
    if risk <= 0 or risk_pct < r["min_risk_pct"] or risk_pct > r["max_risk_pct"]:
        return None
    reasons.insert(0, f"日線{BIAS_TEXT[bias['locked']]}（已連續確認，不會盤中翻轉）")
    if setup == "回踩":
        reasons.append(f"停損放在近 {r['swing_lookback']} 根{'低' if sgn > 0 else '高'}點再外推 {r['stop_buffer_atr']} 倍 ATR，留插針空間")
    else:
        reasons.append(f"停損 = 進場 {'−' if sgn > 0 else '＋'} {r['breakout_stop_atr']} 倍 ATR")
    notional = cfg.get("notional_usdt", 1000)
    return {
        "id": f"{symbol}-{b['t']}",
        "symbol": symbol, "side": side, "setup": setup,
        "bar_t": b["t"], "time": b["ct"],
        "entry": entry, "sl": sl,
        "tp1": entry + sgn * r["tp1_r"] * risk,
        "tp2": entry + sgn * r["tp2_r"] * risk,
        "risk_pct": round(risk_pct, 2),
        "risk_usdt": round(notional * risk_pct / 100, 2),
        "bias": bias["locked"],
        "reasons": reasons,
        "status": "open", "stage": 0, "bars_held": 0,
    }


# ---------- 結算 ----------
def settle(sig, bars, cfg):
    """用訊號之後的 K 線結算。每次都從頭重算，所以重跑幾次結果都一樣。

    出場規則（固定、事先寫死）：
      碰到止盈 1 → 平一半，剩下的停損移到成本
      碰到止盈 2 → 全平
      同一根同時碰到停損和止盈 → 當作先碰停損（保守）
      持有超過 max_hold_bars 根還沒結束 → 用收盤價平掉
    """
    if sig["status"] == "closed":
        return sig
    r = cfg["rules"]
    sgn = 1 if sig["side"] == "long" else -1
    entry, sl, tp1, tp2 = sig["entry"], sig["sl"], sig["tp1"], sig["tp2"]
    risk = (entry - sl) * sgn
    fee_r = cfg.get("fee_pct_round_trip", 0.1) / 100 * entry / risk
    ts = [b["t"] for b in bars]
    j = bisect.bisect_right(ts, sig["bar_t"])
    stage, realized, held, last = 0, 0.0, 0, None

    def done(result, gross, bar, price):
        sig.update(status="closed", result=result, r_gross=round(gross, 3),
                   r=round(gross - fee_r, 3), closed_time=bar["ct"], exit_price=price,
                   bars_held=held, stage=stage)
        return sig

    for b in bars[j:]:
        held += 1
        last = b
        hi, lo = (b["h"], b["l"]) if sgn > 0 else (-b["l"], -b["h"])
        e, s, t1, t2 = entry * sgn, sl * sgn, tp1 * sgn, tp2 * sgn
        if stage == 0:
            if lo <= s:
                return done("停損", -1.0, b, sl)
            if hi >= t1:
                stage, realized = 1, 0.5 * r["tp1_r"]
                if hi >= t2:
                    return done("止盈 2", realized + 0.5 * r["tp2_r"], b, tp2)
        else:
            if lo <= e:
                return done("止盈 1 後保本出場", realized, b, entry)
            if hi >= t2:
                return done("止盈 2", realized + 0.5 * r["tp2_r"], b, tp2)
        if held >= r["max_hold_bars"]:
            move = (b["c"] - entry) * sgn / risk
            gross = move if stage == 0 else realized + 0.5 * move
            return done("到期平倉", gross, b, b["c"])
    sig.update(stage=stage, bars_held=held)
    if last:
        sig["last_price"] = last["c"]
        sig["unrealized_r"] = round(((last["c"] - entry) * sgn / risk) * (1 if stage == 0 else 0.5) + realized, 3)
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
    for s in closed:
        cum += s["r"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
        curve.append([s["closed_time"], round(cum, 3)])

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
        "avg_hold_bars": round(sum(s["bars_held"] for s in closed) / len(closed), 1),
        "by_setup": group("setup"),
        "by_side": group("side"),
        "by_result": {k: sum(1 for s in closed if s["result"] == k) for k in sorted({s["result"] for s in closed})},
        "curve": curve,
        "reliable": len(closed) >= 30,
    })
    return out
