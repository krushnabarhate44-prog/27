import os
from typing import Optional, List, Dict, Any

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Body
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA Direct Trade Signal Backend", version="4.0.0")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

_smart_api: Optional[SmartConnect] = None

DEFAULT_WATCHLIST = [
    {"exchange": "NSE", "tradingsymbol": "SBIN-EQ", "symboltoken": "3045"},
    {"exchange": "NSE", "tradingsymbol": "RELIANCE-EQ", "symboltoken": "2885"},
    {"exchange": "NSE", "tradingsymbol": "TCS-EQ", "symboltoken": "11536"},
    {"exchange": "NSE", "tradingsymbol": "INFY-EQ", "symboltoken": "1594"},
    {"exchange": "NSE", "tradingsymbol": "HDFCBANK-EQ", "symboltoken": "1333"},
    {"exchange": "NSE", "tradingsymbol": "ICICIBANK-EQ", "symboltoken": "4963"},
    {"exchange": "NSE", "tradingsymbol": "AXISBANK-EQ", "symboltoken": "5900"},
]

def check_token(authorization: Optional[str]) -> None:
    if RIGA_ACTION_TOKEN and authorization != f"Bearer {RIGA_ACTION_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

def get_client() -> SmartConnect:
    global _smart_api
    if _smart_api is not None:
        return _smart_api

    missing = [k for k, v in {
        "ANGEL_API_KEY": ANGEL_API_KEY,
        "ANGEL_CLIENT_CODE": ANGEL_CLIENT_CODE,
        "ANGEL_PASSWORD": ANGEL_PASSWORD,
        "ANGEL_TOTP_SECRET": ANGEL_TOTP_SECRET,
    }.items() if not v]

    if missing:
        raise HTTPException(status_code=500, detail=f"Missing env variables: {', '.join(missing)}")

    try:
        client = SmartConnect(api_key=ANGEL_API_KEY)
        clean_secret = ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()
        totp = pyotp.TOTP(clean_secret).now()
        session = client.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)
        if not session or not session.get("status"):
            raise HTTPException(status_code=500, detail={"message": "Angel login failed", "response": session})
        _smart_api = client
        return client
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Angel login error: {str(exc)}")

def fetch_ltp(client: SmartConnect, item: Dict[str, str]) -> Dict[str, Any]:
    try:
        response = client.ltpData(item["exchange"], item["tradingsymbol"], item["symboltoken"])
    except Exception as exc:
        return {**item, "status": "error", "error": str(exc)}

    if not response or not response.get("status"):
        return {**item, "status": "error", "error": response}

    data = response.get("data", {})
    return {
        **item,
        "status": "success",
        "ltp": data.get("ltp"),
        "open": data.get("open"),
        "high": data.get("high"),
        "low": data.get("low"),
        "close": data.get("close"),
    }

def direct_trade_logic(data: Dict[str, Any]) -> Dict[str, Any]:
    # This uses LTP + day OHLC only. It is direct signal style, not order execution.
    if data.get("status") != "success":
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Data fetch error"}

    ltp, opn, high, low = data.get("ltp"), data.get("open"), data.get("high"), data.get("low")
    if not all(isinstance(x, (int, float)) for x in [ltp, opn, high, low]):
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Missing OHLC/LTP"}

    rng = high - low
    if rng <= 0:
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Invalid day range"}

    pos = (ltp - low) / rng
    move = ((ltp - opn) / opn) * 100 if opn else 0

    # Strict direct trade rules:
    # BUY only if price is near day high and strong from open.
    if pos >= 0.88 and move >= 0.75:
        sl = round(ltp - (rng * 0.18), 2)
        target = round(ltp + ((ltp - sl) * 2), 2)
        return {
            **data,
            "market_bias": "BUY",
            "confidence": 70,
            "entry": round(ltp, 2),
            "stop_loss": sl,
            "target": target,
            "risk_reward": "1:2 approx",
            "reason": "Direct BUY signal: price near day high with strong positive move from open. Use strict SL."
        }

    # SELL only if price is near day low and weak from open.
    if pos <= 0.12 and move <= -0.75:
        sl = round(ltp + (rng * 0.18), 2)
        target = round(ltp - ((sl - ltp) * 2), 2)
        return {
            **data,
            "market_bias": "SELL",
            "confidence": 70,
            "entry": round(ltp, 2),
            "stop_loss": sl,
            "target": target,
            "risk_reward": "1:2 approx",
            "reason": "Direct SELL signal: price near day low with strong negative move from open. Use strict SL."
        }

    return {
        **data,
        "market_bias": "NO TRADE",
        "confidence": 50,
        "entry": None,
        "stop_loss": None,
        "target": None,
        "reason": "No direct 70% signal from live LTP + day OHLC."
    }

@app.get("/")
def root():
    return {"status": "running", "name": "RIGA Direct Trade Signal Backend", "version": "4.0.0"}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/ltp")
def ltp(exchange: str = Query(...), tradingsymbol: str = Query(...), symboltoken: str = Query(...), authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    result = fetch_ltp(client, {"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken})
    if result.get("status") != "success":
        raise HTTPException(status_code=502, detail=result)
    return result

@app.get("/direct-trade-signal")
def direct_trade_signal(exchange: str = Query(...), tradingsymbol: str = Query(...), symboltoken: str = Query(...), authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    data = fetch_ltp(client, {"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken})
    return direct_trade_logic(data)

@app.get("/direct-trade-scan")
def direct_trade_scan(authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    results = [direct_trade_logic(fetch_ltp(client, item)) for item in DEFAULT_WATCHLIST]
    trades = [r for r in results if r.get("market_bias") in ["BUY", "SELL"] and r.get("confidence", 0) >= 70]
    return {
        "scan_type": "direct_trade_scan",
        "total_scanned": len(results),
        "trade_count": len(trades),
        "trades": trades,
        "all_results": results
    }

@app.post("/direct-trade-custom-scan")
def direct_trade_custom_scan(items: List[Dict[str, str]] = Body(...), authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    results = []
    for item in items:
        if not all(k in item for k in ["exchange", "tradingsymbol", "symboltoken"]):
            results.append({"status": "error", "input": item, "error": "Missing exchange/tradingsymbol/symboltoken"})
            continue
        results.append(direct_trade_logic(fetch_ltp(client, item)))
    trades = [r for r in results if r.get("market_bias") in ["BUY", "SELL"] and r.get("confidence", 0) >= 70]
    return {"scan_type": "direct_trade_custom_scan", "total_scanned": len(results), "trade_count": len(trades), "trades": trades, "all_results": results}

@app.get("/option-chain-status")
def option_chain_status(authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    return {"status": "next_upgrade_required", "message": "Option chain direct trades need CE/PE token mapping and candle/option data."}
