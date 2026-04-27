import os
import requests
from datetime import datetime
from typing import Optional, List, Dict, Any

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v6 Index + Options Scanner", version="6.0.0")

# ENV
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

_smart_api = None
_scrip_master = None

# INDEX WATCHLIST
WATCHLIST = [
    {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "symboltoken": "26000"},
    {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009"},
    {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037"},
    {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1"},
]

# AUTH
def check_token(auth):
    if RIGA_ACTION_TOKEN and auth != f"Bearer {RIGA_ACTION_TOKEN}":
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

# LOAD OPTION TOKENS
def load_scrip_master():
    global _scrip_master

    if _scrip_master:
        return _scrip_master

    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    res = requests.get(url)
    _scrip_master = res.json()
    return _scrip_master

# FETCH LTP
def get_ltp(client, item):
    res = client.ltpData(item["exchange"], item["tradingsymbol"], item["symboltoken"])

    if not res.get("status"):
        return None

    d = res["data"]

    return {
        "symbol": item["tradingsymbol"],
        "ltp": d["ltp"],
        "high": d["high"],
        "low": d["low"],
        "open": d["open"]
    }

# RIGA LOGIC
def riga_logic(data):
    if not data:
        return {"bias": "NO TRADE"}

    ltp = data["ltp"]
    high = data["high"]
    low = data["low"]
    open_p = data["open"]

    rng = high - low
    if rng == 0:
        return {"bias": "NO TRADE"}

    pos = (ltp - low) / rng
    mom = (ltp - open_p) / open_p * 100

    if pos > 0.85 and mom > 0.7:
        sl = round(ltp - rng * 0.2, 2)
        target = round(ltp + (ltp - sl) * 2, 2)
        return {"bias": "BUY", "entry": ltp, "sl": sl, "target": target}

    if pos < 0.15 and mom < -0.7:
        sl = round(ltp + rng * 0.2, 2)
        target = round(ltp - (sl - ltp) * 2, 2)
        return {"bias": "SELL", "entry": ltp, "sl": sl, "target": target}

    return {"bias": "NO TRADE"}

# ROOT
@app.get("/")
def root():
    return {"status": "RIGA v6 LIVE"}

# SCAN INDEX
@app.get("/real-riga-scan")
def scan(auth: Optional[str] = Header(None)):
    check_token(auth)
    client = get_client()

    trades = []

    for item in WATCHLIST:
        data = get_ltp(client, item)
        signal = riga_logic(data)

        if signal["bias"] != "NO TRADE":
            trades.append({
                "symbol": item["tradingsymbol"],
                **signal
            })

    return {"trades": trades}

# OPTION CHAIN (ATM)
@app.get("/scan-options")
def scan_options(index: str = "NIFTY", auth: Optional[str] = Header(None)):
    check_token(auth)
    client = get_client()

    master = load_scrip_master()

    spot = get_ltp(client, WATCHLIST[0])  # NIFTY default
    atm = round(spot["ltp"] / 50) * 50

    options = []

    for s in master:
        if index in s.get("name", "") and ("CE" in s["symbol"] or "PE" in s["symbol"]):
            strike = int(float(s["strike"]) / 100)

            if abs(strike - atm) <= 200:
                options.append({
                    "exchange": s["exch_seg"],
                    "tradingsymbol": s["symbol"],
                    "symboltoken": s["token"]
                })

    results = []

    for opt in options[:20]:
        data = get_ltp(client, opt)
        signal = riga_logic(data)

        if signal["bias"] != "NO TRADE":
            results.append({
                "symbol": opt["tradingsymbol"],
                **signal
            })

    return {
        "index": index,
        "atm": atm,
        "trades": results
    }
