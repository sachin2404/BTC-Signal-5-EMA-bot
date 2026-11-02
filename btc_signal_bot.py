"""
BTC signal bot (15m + 1h) -> Telegram

Strategy (checked on every CLOSED candle):
  1. Whole candle above 5 EMA  (open, high, low, close all > EMA5)
  2. Wick >= 2x body           (upper wick, since candle is red above EMA)
  3. Red candle                (close < open)
-> SHORT signal is sent to Telegram with entry / stop-loss / targets.

Data source : Kraken public API (free, no API key, not geo-blocked on cloud servers)
Env vars    : TG_TOKEN, TG_CHAT_ID
Modes       : MODE=once  (run one check and exit - for GitHub Actions)
              MODE=loop  (run forever - for a VPS / always-on server)
"""

import os
import time
from datetime import datetime, timezone, timedelta

import pandas as pd
import requests

# ---------------- CONFIG ----------------
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
MODE = os.getenv("MODE", "once")

PAIR = "XBTUSD"                       # Kraken symbol for BTC/USD
TIMEFRAMES = {"15m": 15, "1h": 60}    # name -> Kraken interval in minutes
EMA_PERIOD = 5
WICK_BODY_RATIO = 2.0                 # wick must be >= 2 x body
SL_BUFFER = 0.0005                    # 0.05% above candle high for stop-loss
RR_TARGETS = (1.0, 2.0)               # targets in risk:reward multiples
FRESH_WINDOW_MIN = 15                 # in "once" mode, only alert if candle closed within this many minutes
IST = timezone(timedelta(hours=5, minutes=30))
# ----------------------------------------


def get_candles(interval_min: int) -> pd.DataFrame:
    url = "https://api.kraken.com/0/public/OHLC"
    r = requests.get(url, params={"pair": PAIR, "interval": interval_min}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    rows = next(v for k, v in data["result"].items() if k != "last")
    df = pd.DataFrame(
        rows,
        columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"],
    )
    for c in ["open", "high", "low", "close"]:
        df[c] = df[c].astype(float)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    # last row is the still-forming candle -> drop it, keep closed candles only
    return df.iloc[:-1].reset_index(drop=True)


def check_signal(df: pd.DataFrame, interval_min: int):
    df = df.copy()
    df["ema5"] = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
    c = df.iloc[-1]  # last closed candle

    whole_above_ema = min(c.open, c.high, c.low, c.close) > c.ema5
    is_red = c.close < c.open
    body = c.open - c.close
    upper_wick = c.high - c.open
    wick_ok = body > 0 and upper_wick >= WICK_BODY_RATIO * body

    if not (whole_above_ema and is_red and wick_ok):
        return None

    entry = c.close
    sl = c.high * (1 + SL_BUFFER)
    risk = sl - entry
    targets = [entry - risk * rr for rr in RR_TARGETS]
    close_time = c.time + timedelta(minutes=interval_min)
    return {
        "side": "SHORT",
        "entry": entry,
        "sl": sl,
        "targets": targets,
        "ema5": c.ema5,
        "body": body,
        "wick": upper_wick,
        "open_time": c.time,
        "close_time": close_time,
    }


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    r = requests.post(url, data={"chat_id": TG_CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()


def format_msg(tf: str, s: dict) -> str:
    t1, t2 = s["targets"]
    return (
        f"🔴 BTC {s['side']} SIGNAL ({tf})\n"
        f"━━━━━━━━━━━━━━━\n"
        f"Entry : {s['entry']:.2f}\n"
        f"SL    : {s['sl']:.2f}\n"
        f"TP1   : {t1:.2f} (1:{RR_TARGETS[0]:g})\n"
        f"TP2   : {t2:.2f} (1:{RR_TARGETS[1]:g})\n"
        f"━━━━━━━━━━━━━━━\n"
        f"5 EMA : {s['ema5']:.2f}\n"
        f"Body  : {s['body']:.2f} | Upper wick: {s['wick']:.2f} "
        f"({s['wick'] / s['body']:.1f}x body)\n"
        f"Candle closed: {s['close_time'].astimezone(IST):%d-%b %H:%M} IST\n"
        f"Rule: full candle above 5 EMA + red + wick >= {WICK_BODY_RATIO:g}x body\n"
        f"⚠️ Not financial advice. Use your own risk management."
    )


def run_once(last_sent: dict | None = None):
    now = datetime.now(timezone.utc)
    for tf, minutes in TIMEFRAMES.items():
        try:
            df = get_candles(minutes)
            sig = check_signal(df, minutes)
        except Exception as e:
            print(f"[{tf}] error: {e}")
            continue
        if not sig:
            print(f"[{tf}] no signal")
            continue

        if last_sent is not None:  # loop mode: de-duplicate in memory
            if last_sent.get(tf) == sig["open_time"]:
                continue
            last_sent[tf] = sig["open_time"]
        else:  # once mode: only alert for a freshly closed candle
            age_min = (now - sig["close_time"]).total_seconds() / 60
            if age_min > FRESH_WINDOW_MIN:
                print(f"[{tf}] signal is old ({age_min:.0f} min), skipping")
                continue

        send_telegram(format_msg(tf, sig))
        print(f"[{tf}] SIGNAL SENT")


def main():
    if not TG_TOKEN or not TG_CHAT_ID:
        raise SystemExit("Set TG_TOKEN and TG_CHAT_ID environment variables.")

    if MODE == "loop":
        last_sent: dict = {}
        while True:
            run_once(last_sent)
            time.sleep(60)  # check every minute; de-dupe prevents repeats
    else:
        run_once()


if __name__ == "__main__":
    main()
