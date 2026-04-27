import os
from datetime import datetime, timedelta
from typing import Optional

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA FULL ENGINE v2", version="2.0")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

client_obj = None

WATCHLIST = [
    {"name": "NIFTY", "exchange": "NSE", "symboltoken": "26000"},
    {"name": "BANKNIFTY", "exchange": "NSE", "symboltoken": "26009"},
    {"name": "FINNIFTY", "exchange": "NSE", "symboltoken": "26037"},
]


def check_token(auth):
    if RIGA_ACTION_TOKEN and auth != f"Bearer {RIGA_ACTION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_client():
    global client_obj
    if client_obj:
        return client_obj

    client = SmartConnect(api_key=ANGEL_API_KEY)
    totp = pyotp.TOTP(ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()).now()

    session = client.generateSession(
        ANGEL_CLIENT_CODE,
        ANGEL_PASSWORD,
        totp
    )

    if not session.get("status"):
        raise HTTPException(status_code=500, detail="Angel login failed")

    client_obj = client
    return client


def get_candles(client, exchange, token, interval="FIVE_MINUTE", days=3):
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days)

    params = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M")
    }

    res = client.getCandleData(params)

    if not res or not res.get("status"):
        return []

    candles = []
    for c in res.get("data", []):
        candles.append({
            "time": c[0],
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
        })

    return candles


def avg(values):
    return sum(values) / len(values) if values else 0


def candle_strength(c):
    rng = c["high"] - c["low"]
    if rng <= 0:
        return 0
    body = abs(c["close"] - c["open"])
    return body / rng


def detect_trend(candles):
    if len(candles) < 30:
        return "SIDEWAYS"

    closes = [c["close"] for c in candles]
    ma_fast = avg(closes[-9:])
    ma_slow = avg(closes[-21:])

    highs = [c["high"] for c in candles[-6:]]
    lows = [c["low"] for c in candles[-6:]]

    hh_hl = highs[-1] > highs[-3] and lows[-1] > lows[-3]
    lh_ll = highs[-1] < highs[-3] and lows[-1] < lows[-3]

    if ma_fast > ma_slow and hh_hl:
        return "UPTREND"

    if ma_fast < ma_slow and lh_ll:
        return "DOWNTREND"

    return "SIDEWAYS"


def key_levels(candles):
    recent = candles[-30:]
    resistance = max(c["high"] for c in recent[:-1])
    support = min(c["low"] for c in recent[:-1])
    return support, resistance


def detect_breakout(candles):
    last = candles[-1]
    prev = candles[-2]
    support, resistance = key_levels(candles)

    breakout_up = last["close"] > resistance and prev["close"] <= resistance
    breakout_down = last["close"] < support and prev["close"] >= support

    return breakout_up, breakout_down, support, resistance


def detect_retest(candles, support, resistance):
    if len(candles) < 3:
        return False, False

    last = candles[-1]
    prev = candles[-2]

    buy_retest = (
        prev["low"] <= resistance
        and last["close"] > resistance
        and last["close"] > last["open"]
    )

    sell_retest = (
        prev["high"] >= support
        and last["close"] < support
        and last["close"] < last["open"]
    )

    return buy_retest, sell_retest


def fake_breakout_filter(candles, support, resistance):
    last = candles[-1]

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]
    body = abs(last["close"] - last["open"])

    fake_up = last["high"] > resistance and last["close"] < resistance and upper_wick > body
    fake_down = last["low"] < support and last["close"] > support and lower_wick > body

    return fake_up, fake_down


def momentum_score(candles):
    last = candles[-1]
    prev = candles[-2]

    strength = candle_strength(last)
    avg_vol = avg([c["volume"] for c in candles[-20:-1]])
    vol_ok = last["volume"] > avg_vol * 1.1 if avg_vol else False

    score = 0
    if strength >= 0.60:
        score += 15
    if last["close"] > prev["close"]:
        score += 5
    if vol_ok:
        score += 10

    return score, strength, vol_ok


def riga_engine(candles):
    if len(candles) < 35:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "Not enough candles"}

    trend = detect_trend(candles)
    breakout_up, breakout_down, support, resistance = detect_breakout(candles)
    buy_retest, sell_retest = detect_retest(candles, support, resistance)
    fake_up, fake_down = fake_breakout_filter(candles, support, resistance)

    last = candles[-1]
    mom_score, strength, vol_ok = momentum_score(candles)

    buy_score = 0
    sell_score = 0
    buy_reasons = []
    sell_reasons = []

    if trend == "UPTREND":
        buy_score += 20
        buy_reasons.append("trend up")
    if trend == "DOWNTREND":
        sell_score += 20
        sell_reasons.append("trend down")

    if breakout_up and not fake_up:
        buy_score += 20
        buy_reasons.append("valid breakout")
    if breakout_down and not fake_down:
        sell_score += 20
        sell_reasons.append("valid breakdown")

    if buy_retest:
        buy_score += 15
        buy_reasons.append("retest confirmed")
    if sell_retest:
        sell_score += 15
        sell_reasons.append("retest confirmed")

    if last["close"] > last["open"] and strength >= 0.60:
        buy_score += 15
        buy_reasons.append("strong bullish candle")

    if last["close"] < last["open"] and strength >= 0.60:
        sell_score += 15
        sell_reasons.append("strong bearish candle")

    buy_score += mom_score
    sell_score += mom_score

    if fake_up:
        buy_score -= 25
        buy_reasons.append("fake breakout risk")

    if fake_down:
        sell_score -= 25
        sell_reasons.append("fake breakdown risk")

    # BUY
    if buy_score >= 70 and buy_score >= sell_score:
        entry = last["close"]
        sl = min(last["low"], support)
        risk = entry - sl
        if risk <= 0:
            return {"bias": "NO TRADE", "confidence": buy_score, "reason": "Invalid risk"}
        target = entry + risk * 2

        return {
            "bias": "BUY",
            "confidence": min(buy_score, 90),
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "target": round(target, 2),
            "rr": "1:2",
            "trend": trend,
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "candle_strength": round(strength, 2),
            "volume_confirmed": vol_ok,
            "reason": ", ".join(buy_reasons)
        }

    # SELL
    if sell_score >= 70 and sell_score > buy_score:
        entry = last["close"]
        sl = max(last["high"], resistance)
        risk = sl - entry
        if risk <= 0:
            return {"bias": "NO TRADE", "confidence": sell_score, "reason": "Invalid risk"}
        target = entry - risk * 2

        return {
            "bias": "SELL",
            "confidence": min(sell_score, 90),
            "entry": round(entry, 2),
            "sl": round(sl, 2),
            "target": round(target, 2),
            "rr": "1:2",
            "trend": trend,
            "support": round(support, 2),
            "resistance": round(resistance, 2),
            "candle_strength": round(strength, 2),
            "volume_confirmed": vol_ok,
            "reason": ", ".join(sell_reasons)
        }

    return {
        "bias": "NO TRADE",
        "confidence": max(buy_score, sell_score),
        "trend": trend,
        "support": round(support, 2),
        "resistance": round(resistance, 2),
        "candle_strength": round(strength, 2),
        "buy_score": buy_score,
        "sell_score": sell_score,
        "reason": "RIGA confirmations below 70"
    }


@app.get("/")
def root():
    return {"status": "RIGA FULL ENGINE v2 LIVE"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/candles")
def candles(
    exchange: str,
    symboltoken: str,
    interval: str = "FIVE_MINUTE",
    authorization: Optional[str] = Header(None)
):
    check_token(authorization)
    client = get_client()
    data = get_candles(client, exchange, symboltoken, interval)

    return {
        "count": len(data),
        "candles": data[-50:]
    }


@app.get("/riga-signal")
def riga_signal(
    exchange: str,
    symboltoken: str,
    interval: str = "FIVE_MINUTE",
    authorization: Optional[str] = Header(None)
):
    check_token(authorization)
    client = get_client()
    candles = get_candles(client, exchange, symboltoken, interval)
    signal = riga_engine(candles)

    return signal


@app.get("/riga-scan")
def riga_scan(
    interval: str = "FIVE_MINUTE",
    authorization: Optional[str] = Header(None)
):
    check_token(authorization)
    client = get_client()

    results = []

    for item in WATCHLIST:
        candles = get_candles(client, item["exchange"], item["symboltoken"], interval)
        signal = riga_engine(candles)

        results.append({
            "symbol": item["name"],
            "exchange": item["exchange"],
            "symboltoken": item["symboltoken"],
            "signal": signal
        })

    trades = [
        r for r in results
        if r["signal"].get("bias") in ["BUY", "SELL"]
        and r["signal"].get("confidence", 0) >= 70
    ]

    return {
        "total_scanned": len(results),
        "trade_count": len(trades),
        "trades": trades,
        "all_results": results
    }
