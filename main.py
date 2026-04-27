import os
from typing import Optional, List, Dict, Any

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA REAL AI Backend", version="5.0.0")

# ENV
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

_smart_api: Optional[SmartConnect] = None

# WATCHLIST
WATCHLIST = [
    {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"},
    {"exchange": "NSE", "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885"},
    {"exchange": "NSE", "tradingsymbol": "TCS-EQ", "symboltoken": "11536"},
    {"exchange": "NSE", "tradingsymbol": "INFY-EQ", "symboltoken": "1594"},
    {"exchange": "NSE", "tradingsymbol": "HDFCBANK-EQ", "symboltoken": "1333"},
    {"exchange": "NSE", "tradingsymbol": "ICICIBANK-EQ", "symboltoken": "4963"},
    {"exchange": "NSE", "tradingsymbol": "AXISBANK-EQ", "symboltoken": "5900"},
]

# AUTH
def check_token(authorization: Optional[str]):
    if RIGA_ACTION_TOKEN and authorization != f"Bearer {RIGA_ACTION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

# CONNECT
def get_client():
    global _smart_api

    if _smart_api:
        return _smart_api

    client = SmartConnect(api_key=ANGEL_API_KEY)

    totp = pyotp.TOTP(
        ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()
    ).now()

    session = client.generateSession(
        ANGEL_CLIENT_CODE,
        ANGEL_PASSWORD,
        totp
    )

    if not session.get("status"):
        raise HTTPException(status_code=500, detail="Login failed")

    _smart_api = client
    return client

# FETCH LTP
def fetch_ltp(client, item):
    res = client.ltpData(item["exchange"], item["tradingsymbol"], item["symboltoken"])

    if not res.get("status"):
        return None

    data = res["data"]

    return {
        "symbol": item["tradingsymbol"],
        "ltp": data["ltp"],
        "open": data["open"],
        "high": data["high"],
        "low": data["low"]
    }

# REAL RIGA LOGIC
def riga_logic(data):
    if not data:
        return {"bias": "NO TRADE"}

    ltp = data["ltp"]
    high = data["high"]
    low = data["low"]
    open_price = data["open"]

    range_ = high - low

    if range_ == 0:
        return {"bias": "NO TRADE"}

    position = (ltp - low) / range_
    momentum = (ltp - open_price) / open_price * 100

    # BUY LOGIC
    if position > 0.85 and momentum > 0.7:
        sl = round(ltp - range_ * 0.2, 2)
        target = round(ltp + (ltp - sl) * 2, 2)

        return {
            "bias": "BUY",
            "entry": ltp,
            "sl": sl,
            "target": target,
            "confidence": 70,
            "reason": "Strong momentum + near breakout high"
        }

    # SELL LOGIC
    if position < 0.15 and momentum < -0.7:
        sl = round(ltp + range_ * 0.2, 2)
        target = round(ltp - (sl - ltp) * 2, 2)

        return {
            "bias": "SELL",
            "entry": ltp,
            "sl": sl,
            "target": target,
            "confidence": 70,
            "reason": "Strong breakdown + near day low"
        }

    return {"bias": "NO TRADE"}

# ROOT
@app.get("/")
def root():
    return {"status": "RIGA LIVE"}

# SIGNAL
@app.get("/real-riga-signal")
def real_signal(
    exchange: str,
    tradingsymbol: str,
    symboltoken: str,
    authorization: Optional[str] = Header(None)
):
    check_token(authorization)

    client = get_client()

    data = fetch_ltp(client, {
        "exchange": exchange,
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken
    })

    result = riga_logic(data)

    return result

# SCAN
@app.get("/real-riga-scan")
def real_scan(authorization: Optional[str] = Header(None)):
    check_token(authorization)

    client = get_client()

    results = []

    for item in WATCHLIST:
        data = fetch_ltp(client, item)
        signal = riga_logic(data)

        if signal["bias"] != "NO TRADE":
            results.append({
                "symbol": item["tradingsymbol"],
                **signal
            })

    return {
        "total": len(WATCHLIST),
        "trades": results
    }
