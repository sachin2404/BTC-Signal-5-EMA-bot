#!/usr/bin/env python3
"""
BTC Signal Bot -> Telegram

Strategy (checked on the last CLOSED candle of 15m and 1h):
  1. Whole candle above EMA(5): open, high, low, close ALL > EMA5
  2. Wick >= 2x body   (WICK_MODE: "upper" | "lower" | "either", default "upper")
  3. Red candle: close < open

Schedule (IST): 15m candle checked at :00 :15 :30 :45
                1h  candle checked at :30 only (3:30, 4:30, 5:30 ...)

Since all 3 conditions describe a red rejection candle above the EMA,
the signal is posted as a SHORT idea.

Modes:
  python btc_signal_bot_15m_1h.py          -> runs forever, fires at :00 :15 :30 :45 (IST)
  python btc_signal_bot_15m_1h.py --once   -> one check then exit (GitHub Actions / cron)
  python btc_signal_bot_15m_1h.py --test   -> sends a test Telegram message and exits

Env vars required: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Only dependency: pip install requests
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone

import requests

# ----------------------------- CONFIG ---------------------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

EMA_PERIOD = 5
WICK_MULTIPLE = 2.0
WICK_MODE = os.getenv("WICK_MODE", "upper").lower()   # upper | lower | either
RUN_DELAY_SEC = 5          # wait a few seconds after the boundary so the candle is final
RR_TARGETS = (1.0, 2.0)    # info only: targets at 1R and 2R from entry
IST = timezone(timedelta(hours=5, minutes=30))
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("btc-bot")
sent_keys = set()  # avoid duplicate alerts for the same candle (loop mode)


# ----------------------------- DATA -----------------------------------------
def fetch_kraken(interval_min):
    r = requests.get(
        "https://api.kraken.com/0/OHLC",
        params={"pair": "XBTUSD", "interval": interval_min},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Kraken error: {data['error']}")
    key = next(k for k in data["result"] if k != "last")
    return [
        {"t": int(c[0]), "o": float(c[1]), "h": float(c[2]),
         "l": float(c[3]), "c": float(c[4])}
        for c in data["result"][key]
    ]


def fetch_binance(interval_min):
    interval = "15m" if interval_min == 15 else "1h"
    r = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": interval, "limit": 200},
        timeout=20,
    )
    r.raise_for_status()
    return [
        {"t": int(k[0]) // 1000, "o": float(k[1]), "h": float(k[2]),
         "l": float(k[3]), "c": float(k[4])}
        for k in r.json()
    ]


def get_closed_candles(interval_min):
    """Returns only fully closed candles, oldest -> newest."""
    last_err = None
    for name, fn in (("Kraken", fetch_kraken), ("Binance", fetch_binance)):
        try:
            candles = fn(interval_min)
            now_ts = time.time()
            closed = [c for c in candles if c["t"] + interval_min * 60 <= now_ts]
            log.info(f"[{interval_min}m] Data source: {name}, closed candles: {len(closed)}")
            return closed
        except Exception as e:
            last_err = e
            log.warning(f"[{interval_min}m] {name} failed: {e}")
    raise RuntimeError(f"All data sources failed: {last_err}")


# ----------------------------- STRATEGY -------------------------------------
def ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def fmt_ist(ts):
    return datetime.fromtimestamp(ts, IST).strftime("%d-%b-%Y %I:%M %p IST")


def check_signal(candles, interval_min):
    tf = "15m" if interval_min == 15 else "1h"
    if len(candles) < 30:
        log.warning(f"[{tf}] Not enough candles ({len(candles)})")
        return None

    closes = [c["c"] for c in candles]
    e5 = ema(closes, EMA_PERIOD)[-1]
    c = candles[-1]
    o, h, l, cl = c["o"], c["h"], c["l"], c["c"]

    body = abs(cl - o)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    if WICK_MODE == "lower":
        wick_ok = lower_wick >= WICK_MULTIPLE * body
    elif WICK_MODE == "either":
        wick_ok = upper_wick >= WICK_MULTIPLE * body or lower_wick >= WICK_MULTIPLE * body
    else:
        wick_ok = upper_wick >= WICK_MULTIPLE * body

    above_ema = min(o, h, l, cl) > e5      # whole candle above EMA5
    red = cl < o
    body_ok = body > 0

    log.info(
        f"[{tf}] Candle {fmt_ist(c['t'])} | O={o:.2f} H={h:.2f} L={l:.2f} C={cl:.2f} | "
        f"EMA5={e5:.2f} | body={body:.2f} upWick={upper_wick:.2f} loWick={lower_wick:.2f}"
    )
    log.info(
        f"[{tf}] Conditions -> aboveEMA5={above_ema} | wick>={WICK_MULTIPLE}x body"
        f"({WICK_MODE})={wick_ok} | red={red} | body>0={body_ok}"
    )

    if not (above_ema and wick_ok and red and body_ok):
        log.info(f"[{tf}] NO SIGNAL")
        return None

    sl = h
    risk = sl - cl
    return {
        "tf": tf, "time": fmt_ist(c["t"]), "key": f"{tf}-{c['t']}",
        "o": o, "h": h, "l": l, "c": cl, "ema": e5,
        "body": body, "upper": upper_wick, "lower": lower_wick,
        "ratio": (upper_wick / body) if body else 0,
        "entry": cl, "sl": sl,
        "targets": [cl - risk * r for r in RR_TARGETS],
    }


# ----------------------------- TELEGRAM -------------------------------------
def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set. Message NOT sent.")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=20,
        )
        if r.ok:
            log.info("Telegram message sent")
            return True
        log.error(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        log.error(f"Telegram exception: {e}")
    return False


def build_message(s):
    return (
        f"🚨 <b>BTC SHORT SIGNAL ({s['tf']})</b>\n"
        f"🕒 Candle: {s['time']}\n\n"
        f"<b>Entry:</b> {s['entry']:.2f}\n"
        f"<b>Stop Loss:</b> {s['sl']:.2f}\n"
        f"<b>Target 1 (1R):</b> {s['targets'][0]:.2f}\n"
        f"<b>Target 2 (2R):</b> {s['targets'][1]:.2f}\n\n"
        f"O: {s['o']:.2f} | H: {s['h']:.2f}\n"
        f"L: {s['l']:.2f} | C: {s['c']:.2f}\n"
        f"EMA5: {s['ema']:.2f}\n"
        f"Body: {s['body']:.2f} | Upper wick: {s['upper']:.2f} "
        f"({s['ratio']:.1f}x body)\n\n"
        f"✅ Whole candle above EMA5\n✅ Wick ≥ 2x body\n✅ Red candle\n"
        f"<i>Not financial advice.</i>"
    )


# ----------------------------- RUNNER ---------------------------------------
def floor_slot(dt):
    return dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)


def run_checks(slot, dedupe=True):
    log.info(f"===== RUN for slot {slot.strftime('%d-%b-%Y %I:%M %p')} IST =====")
    intervals = [15]
    # 1h candles follow UTC hours, i.e. they open/close at :30 IST
    # (2:30-3:30, 3:30-4:30 ...), so check the 1h candle at the :30 slot.
    if slot.minute == 30:
        intervals.append(60)
    for iv in intervals:
        try:
            sig = check_signal(get_closed_candles(iv), iv)
            if sig:
                if dedupe and sig["key"] in sent_keys:
                    log.info(f"Signal {sig['key']} already sent, skipping")
                    continue
                if send_telegram(build_message(sig)):
                    sent_keys.add(sig["key"])
        except Exception as e:
            log.exception(f"[{iv}m] check failed: {e}")


def wait_for_next_slot():
    now = datetime.now(IST)
    nxt = floor_slot(now) + timedelta(minutes=15)
    target = nxt + timedelta(seconds=RUN_DELAY_SEC)
    log.info(f"Now {now.strftime('%I:%M:%S %p')} -> next run at {nxt.strftime('%I:%M %p')} IST")
    time.sleep(max(0, (target - datetime.now(IST)).total_seconds()))
    return nxt


def main():
    if "--test" in sys.argv:
        send_telegram("✅ BTC signal bot test message")
        return
    if "--once" in sys.argv:
        run_checks(floor_slot(datetime.now(IST)), dedupe=False)
        return

    log.info("Bot started (loop mode)")
    send_telegram("🤖 BTC signal bot started. Checking 15m & 1h at every 15-min boundary (IST).")
    while True:
        slot = wait_for_next_slot()
        try:
            run_checks(slot)
        except Exception as e:
            log.exception(f"Unexpected error: {e}")


if __name__ == "__main__":
    main()
