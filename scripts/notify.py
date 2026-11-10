"""Telegram 推播（選填）。沒設 TELEGRAM_BOT_TOKEN／TELEGRAM_CHAT_ID 就安靜略過。"""
import html
import json
import os
import urllib.request

from engine import fmt_price


def _send(text):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("（沒設定 Telegram，略過推播）")
        return
    body = json.dumps({"chat_id": chat, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # 推播失敗不影響主流程
        print("Telegram 推播失敗：", e)


def send_signals(sigs, cfg):
    site = os.environ.get("SITE_URL", "")
    for s in sigs:
        side = "做多 🟢" if s["side"] == "long" else "做空 🔴"
        lines = [
            f"<b>{html.escape(s['symbol'])}｜{side}｜{s['setup']}</b>（慢速層・4H）",
            f"進場 {fmt_price(s['entry'])}",
            f"初始停損 {fmt_price(s['sl'])}（−{s['risk_pct']}%，名目 {cfg.get('notional_usdt', 1000)} U 約虧 {s['risk_usdt']} U）",
            f"出場：{cfg['rules']['trail_atr']:g} 倍 ATR 移動停損，不設止盈（網站會顯示每 4 小時更新的停損價）",
            "",
            *[f"・{html.escape(x)}" for x in s["reasons"]],
        ]
        if site:
            lines += ["", f"成績單：{html.escape(site)}"]
        _send("\n".join(lines))
