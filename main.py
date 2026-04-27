import os
from typing import Optional, List, Dict, Any

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query, Body
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA Angel One Real Scanner Backend", version="3.1.0")

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

def riga_watch_logic(data: Dict[str, Any]) -> Dict[str, Any]:
    if data.get("status") != "success":
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Data fetch error"}

    ltp, opn, high, low = data.get("ltp"), data.get("open"), data.get("high"), data.get("low")
    if not all(isinstance(x, (int, float)) for x in [ltp, opn, high, low]):
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Missing OHLC/LTP"}

    day_range = high - low
    if day_range <= 0:
        return {**data, "market_bias": "NO TRADE", "confidence": 0, "reason": "Invalid day range"}

    position = (ltp - low) / day_range
    move_from_open = ((ltp - opn) / opn) * 100 if opn else 0

    if position >= 0.80 and move_from_open >= 0.40:
        return {
            **data,
            "market_bias": "BUY WATCH",
            "confidence": 62,
            "entry": "Wait for breakout candle close + retest",
            "stop_loss": "Below retest low / recent swing low",
            "target": "Next resistance zone",
            "reason": "Strong LTP near day high with positive movement. Needs candle close + retest for 70% trade."
        }

    if position <= 0.20 and move_from_open <= -0.40:
        return {
            **data,
            "market_bias": "SELL WATCH",
            "confidence": 62,
            "entry": "Wait for breakdown candle close + retest",
            "stop_loss": "Above retest high / recent swing high",
            "target": "Next support zone",
            "reason": "Weak LTP near day low with negative movement. Needs candle close + retest for 70% trade."
        }

    return {**data, "market_bias": "NO TRADE", "confidence": 45, "reason": "No valid 70% setup from LTP + day OHLC scan"}

@app.get("/")
def root():
    return {"status": "running", "name": "RIGA Real Scanner Backend", "version": "3.1.0"}

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

@app.get("/riga-signal-demo")
def riga_signal_demo(exchange: str = Query(...), tradingsymbol: str = Query(...), symboltoken: str = Query(...), authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    data = fetch_ltp(client, {"exchange": exchange, "tradingsymbol": tradingsymbol, "symboltoken": symboltoken})
    return riga_watch_logic(data)

@app.get("/scan-default-watchlist")
def scan_default_watchlist(authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    results = [riga_watch_logic(fetch_ltp(client, item)) for item in DEFAULT_WATCHLIST]
    watch_items = [r for r in results if r.get("market_bias") in ["BUY WATCH", "SELL WATCH"]]
    return {
        "scan_type": "default_watchlist",
        "important": "Request-based scan only. Custom GPT cannot auto-run in background by itself.",
        "total_scanned": len(results),
        "watch_count": len(watch_items),
        "watch_items": watch_items,
        "all_results": results
    }

@app.post("/scan-custom-watchlist")
def scan_custom_watchlist(items: List[Dict[str, str]] = Body(...), authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    client = get_client()
    results = []
    for item in items:
        if not all(k in item for k in ["exchange", "tradingsymbol", "symboltoken"]):
            results.append({"status": "error", "input": item, "error": "Missing exchange/tradingsymbol/symboltoken"})
            continue
        results.append(riga_watch_logic(fetch_ltp(client, item)))
    watch_items = [r for r in results if r.get("market_bias") in ["BUY WATCH", "SELL WATCH"]]
    return {"scan_type": "custom_watchlist", "total_scanned": len(results), "watch_count": len(watch_items), "watch_items": watch_items, "all_results": results}

@app.get("/option-chain-status")
def option_chain_status(authorization: Optional[str] = Header(default=None)):
    check_token(authorization)
    return {
        "status": "next_upgrade_required",
        "message": "Option chain scanner needs symbol master + nearest expiry CE/PE token mapping.",
        "next_build": ["Download Angel symbol master", "Find nearest expiry", "Generate CE/PE strikes", "Fetch option LTP", "Apply RIGA strike rules"]
    }

@app.get("/auto-alert-info")
def auto_alert_info():
    return {
        "custom_gpt_limit": "Custom GPT cannot continuously scan in background automatically.",
        "solution": "Use Render Cron Job / GitHub Actions / UptimeRobot to call /scan-default-watchlist every 5 minutes.",
        "current_backend": "Request-based scanner is ready."
    }
