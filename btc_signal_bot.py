"""
BTC Signal Bot -> Telegram
Timeframes : 15m and 1h (1h candles are Binance/UTC candles = IST xx:30 to xx:30)
Schedule   : wakes at every 15-min boundary (9:00, 9:15, 9:30 ...) even if started at 9:18
Run        : python btc_signal_bot.py          (24/7 loop)
             python btc_signal_bot.py --once   (single check, for cron/GitHub Actions)

Env vars   : TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import pandas as pd

# =====================  VARIABLES (edit here)  =====================
BINANCE_SYMBOL = "BTCUSDT"
KRAKEN_PAIR = "XBTUSD"          # fallback exchange if Binance is blocked on your server
EMA_PERIOD = 5
WICK_BODY_RATIO = 2.0           # wick must be >= 2 x body
WICK_SIDE = "UPPER"             # "UPPER", "LOWER" or "EITHER"
RR_RATIO = 2.0                  # target = entry - RR * (SL distance)
CANDLES_TO_FETCH = 100
TIMEFRAMES = {"15m": 15, "1h": 60}   # name -> minutes
RUN_DELAY_SECONDS = 5           # wait a few sec after boundary so exchange finalises candle
RUN_ON_START = False            # True = also run one check immediately at startup
LOCAL_TZ = ZoneInfo("Asia/Kolkata")
# ===================================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("btc-bot")

sent_signals = set()   # (timeframe, candle_open_time) -> avoid duplicate alerts


def ist(dt):
    return dt.astimezone(LOCAL_TZ).strftime("%d-%b-%Y %I:%M %p IST")


# -------------------------- DATA --------------------------
def fetch_binance(tf_min):
    interval = "15m" if tf_min == 15 else "1h"
    last_err = None
    for host in ("https://data-api.binance.vision", "https://api.binance.com"):
        try:
            r = requests.get(
                f"{host}/api/v3/klines",
                params={"symbol": BINANCE_SYMBOL, "interval": interval, "limit": CANDLES_TO_FETCH},
                timeout=15,
            )
            r.raise_for_status()
            df = pd.DataFrame(r.json()).iloc[:, :5]
            df.columns = ["open_time", "open", "high", "low", "close"]
            df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
            return df
        except Exception as e:
            last_err = e
            log.warning(f"Binance host {host} failed: {e}")
    raise RuntimeError(f"Binance failed: {last_err}")


def fetch_kraken(tf_min):
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": KRAKEN_PAIR, "interval": tf_min},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    key = [k for k in data["result"] if k != "last"][0]
    rows = data["result"][key]
    df = pd.DataFrame(rows).iloc[:, :5]
    df.columns = ["open_time", "open", "high", "low", "close"]
    df["open_time"] = pd.to_datetime(df["open_time"].astype(int), unit="s", utc=True)
    df[["open", "high", "low", "close"]] = df[["open", "high", "low", "close"]].astype(float)
    return df.tail(CANDLES_TO_FETCH).reset_index(drop=True)


def get_candles(tf_min):
    try:
        df = fetch_binance(tf_min)
        src = "Binance"
    except Exception as e:
        log.warning(f"Falling back to Kraken ({e})")
        df = fetch_kraken(tf_min)
        src = "Kraken"
    df["close_time"] = df["open_time"] + pd.Timedelta(minutes=tf_min)
    now = datetime.now(timezone.utc)
    df = df[df["close_time"] <= now].reset_index(drop=True)   # keep ONLY closed candles
    return df, src


# -------------------------- STRATEGY --------------------------
def evaluate(df, tf_name):
    df = df.copy()
    df["ema"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    c = df.iloc[-1]

    o, h, l, cl, ema = c["open"], c["high"], c["low"], c["close"], c["ema"]
    body = abs(o - cl)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l

    above_ema = min(o, h, l, cl) > ema          # whole candle above EMA
    is_red = cl < o
    if WICK_SIDE == "UPPER":
        wick = upper_wick
    elif WICK_SIDE == "LOWER":
        wick = lower_wick
    else:
        wick = max(upper_wick, lower_wick)
    wick_ok = body > 0 and wick >= WICK_BODY_RATIO * body

    log.info(
        f"[{tf_name}] candle {ist(c['open_time'])} -> {ist(c['close_time'])} | "
        f"O={o:.2f} H={h:.2f} L={l:.2f} C={cl:.2f} | EMA{EMA_PERIOD}={ema:.2f}"
    )
    log.info(
        f"[{tf_name}] body={body:.2f} upperWick={upper_wick:.2f} lowerWick={lower_wick:.2f} | "
        f"allAboveEMA={above_ema} red={is_red} wick>={WICK_BODY_RATIO}xbody={wick_ok}"
    )

    if above_ema and is_red and wick_ok:
        entry = cl
        sl = h
        risk = sl - entry
        target = entry - RR_RATIO * risk
        return {
            "tf": tf_name, "open_time": c["open_time"], "close_time": c["close_time"],
            "entry": entry, "sl": sl, "target": target, "ema": ema,
            "body": body, "wick": wick, "o": o, "h": h, "l": l, "c": cl,
        }
    return None


# -------------------------- TELEGRAM --------------------------
def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - message NOT sent")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=15,
        )
        log.info(f"Telegram response: {r.status_code} {r.text[:120]}")
        return r.ok
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def format_signal(s, src):
    return (
        f"🔴 BTC SELL SIGNAL ({s['tf']})\n"
        f"Source: {src} | Pair: {BINANCE_SYMBOL}\n"
        f"Candle: {ist(s['open_time'])} -> {ist(s['close_time'])}\n\n"
        f"Entry : {s['entry']:.2f}\n"
        f"SL    : {s['sl']:.2f}\n"
        f"Target: {s['target']:.2f} (RR 1:{RR_RATIO:g})\n\n"
        f"O {s['o']:.2f} | H {s['h']:.2f} | L {s['l']:.2f} | C {s['c']:.2f}\n"
        f"EMA{EMA_PERIOD}: {s['ema']:.2f}\n"
        f"Body: {s['body']:.2f} | Wick: {s['wick']:.2f} ({s['wick']/s['body']:.1f}x body)\n"
        f"Rule: whole candle above EMA{EMA_PERIOD} + red + wick >= {WICK_BODY_RATIO:g}x body"
    )


# -------------------------- RUN ONE CHECK --------------------------
def run_check():
    now = datetime.now(timezone.utc)
    log.info("=" * 70)
    log.info(f"CHECK STARTED at {ist(now)}")
    for tf_name, tf_min in TIMEFRAMES.items():
        try:
            df, src = get_candles(tf_min)
            if df.empty:
                log.warning(f"[{tf_name}] no closed candles returned")
                continue
            last = df.iloc[-1]
            # only evaluate a candle that closed within the last 15 minutes
            # (so 1h candle is checked only at IST xx:30, 15m every 15 min)
            if now - last["close_time"] > timedelta(minutes=15):
                log.info(f"[{tf_name}] latest closed candle ended {ist(last['close_time'])} "
                         f"-> not due yet, skipped")
                continue
            sig = evaluate(df, tf_name)
            if sig is None:
                log.info(f"[{tf_name}] NO SIGNAL")
                continue
            key = (tf_name, sig["open_time"])
            if key in sent_signals:
                log.info(f"[{tf_name}] signal already sent for this candle")
                continue
            log.info(f"[{tf_name}] *** SIGNAL FOUND ***")
            if send_telegram(format_signal(sig, src)):
                sent_signals.add(key)
        except Exception as e:
            log.exception(f"[{tf_name}] check failed: {e}")
    log.info("CHECK FINISHED")


# -------------------------- SCHEDULER --------------------------
def next_boundary(now):
    base = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    return base + timedelta(minutes=15)


def main():
    if "--once" in sys.argv:
        run_check()
        return

    log.info("Bot started. Waiting for next 15-minute boundary...")
    send_telegram("✅ BTC signal bot started")
    if RUN_ON_START:
        run_check()

    while True:
        now = datetime.now(timezone.utc)
        target = next_boundary(now) + timedelta(seconds=RUN_DELAY_SECONDS)
        wait = (target - now).total_seconds()
        log.info(f"Now {ist(now)} | next run at {ist(target)} | sleeping {wait:.0f}s")
        time.sleep(max(wait, 0))
        run_check()


if __name__ == "__main__":
    main()
