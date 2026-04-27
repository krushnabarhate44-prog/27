import os
import requests
from datetime import datetime, timedelta
from typing import Optional

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA OPTION SNIPER ENGINE", version="3.0")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

client_obj = None
scrip_master_cache = None

INDEX_CONFIG = {
    "NIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "symboltoken": "26000"},
        "option_exchange": "NFO",
        "option_name": "NIFTY",
        "step": 50
    },
    "BANKNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009"},
        "option_exchange": "NFO",
        "option_name": "BANKNIFTY",
        "step": 100
    },
    "FINNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037"},
        "option_exchange": "NFO",
        "option_name": "FINNIFTY",
        "step": 50
    },
    "SENSEX": {
        "spot": {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1"},
        "option_exchange": "BFO",
        "option_name": "SENSEX",
        "step": 100
    }
}


def check_token(authorization: Optional[str], token: Optional[str]):
    if not RIGA_ACTION_TOKEN:
        return

    header_ok = authorization == f"Bearer {RIGA_ACTION_TOKEN}"
    query_ok = token == RIGA_ACTION_TOKEN

    if not header_ok and not query_ok:
        raise HTTPException(status_code=401, detail="Unauthorized")


def get_client():
    global client_obj

    if client_obj:
        return client_obj

    client = SmartConnect(api_key=ANGEL_API_KEY)

    totp = pyotp.TOTP(
        ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()
    ).now()

    session = client.generateSession(
        ANGEL_CLIENT_CODE,
        ANGEL_PASSWORD,
        totp
    )

    if not session or not session.get("status"):
        raise HTTPException(status_code=500, detail="Angel login failed")

    client_obj = client
    return client


def load_scrip_master():
    global scrip_master_cache

    if scrip_master_cache:
        return scrip_master_cache

    res = requests.get(SCRIP_MASTER_URL, timeout=25)
    res.raise_for_status()
    scrip_master_cache = res.json()
    return scrip_master_cache


def get_ltp(client, item):
    res = client.ltpData(
        item["exchange"],
        item["tradingsymbol"],
        str(item["symboltoken"])
    )

    if not res or not res.get("status"):
        return None

    d = res["data"]

    return {
        "symbol": item["tradingsymbol"],
        "exchange": item["exchange"],
        "token": item["symboltoken"],
        "ltp": d.get("ltp"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "close": d.get("close")
    }


def riga_logic(data):
    if not data:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "No data"}

    ltp = data.get("ltp")
    high = data.get("high")
    low = data.get("low")
    open_p = data.get("open")

    if not all(isinstance(x, (int, float)) for x in [ltp, high, low, open_p]):
        return {"bias": "NO TRADE", "confidence": 0, "reason": "Invalid data"}

    rng = high - low
    if rng <= 0:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "Invalid range"}

    pos = (ltp - low) / rng
    mom = ((ltp - open_p) / open_p) * 100 if open_p else 0

    if pos > 0.85 and mom > 0.70:
        sl = round(ltp - rng * 0.20, 2)
        target = round(ltp + (ltp - sl) * 2, 2)
        return {
            "bias": "BUY",
            "entry": round(ltp, 2),
            "sl": sl,
            "target": target,
            "confidence": 70,
            "reason": "Strong momentum near high"
        }

    if pos < 0.15 and mom < -0.70:
        sl = round(ltp + rng * 0.20, 2)
        target = round(ltp - (sl - ltp) * 2, 2)
        return {
            "bias": "SELL",
            "entry": round(ltp, 2),
            "sl": sl,
            "target": target,
            "confidence": 70,
            "reason": "Strong momentum near low"
        }

    return {"bias": "NO TRADE", "confidence": 50, "reason": "No 70% setup"}


def round_to_step(price, step):
    return int(round(price / step) * step)


def parse_expiry(expiry):
    for fmt in ("%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(str(expiry).upper(), fmt)
        except:
            pass
    return None


def get_auto_option_chain(index_name, spot_price, strikes_around=3):
    index_name = index_name.upper()

    if index_name not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Use NIFTY, BANKNIFTY, FINNIFTY, SENSEX")

    cfg = INDEX_CONFIG[index_name]
    master = load_scrip_master()

    atm = round_to_step(spot_price, cfg["step"])
    allowed = {
        atm + i * cfg["step"]
        for i in range(-strikes_around, strikes_around + 1)
    }

    today = datetime.now()
    found = []

    for s in master:
        try:
            if s.get("name") != cfg["option_name"]:
                continue
            if s.get("exch_seg") != cfg["option_exchange"]:
                continue
            if s.get("instrumenttype") != "OPTIDX":
                continue

            symbol = s.get("symbol", "")
            if not (symbol.endswith("CE") or symbol.endswith("PE")):
                continue

            strike = int(float(s.get("strike", 0)) / 100)
            if strike not in allowed:
                continue

            expiry_dt = parse_expiry(s.get("expiry"))
            if not expiry_dt or expiry_dt.date() < today.date():
                continue

            found.append({
                "exchange": cfg["option_exchange"],
                "tradingsymbol": symbol,
                "symboltoken": str(s.get("token")),
                "strike": strike,
                "type": "CE" if symbol.endswith("CE") else "PE",
                "expiry": s.get("expiry"),
                "expiry_dt": expiry_dt
            })

        except:
            continue

    if not found:
        return atm, None, []

    nearest = min(x["expiry_dt"] for x in found)
    options = [x for x in found if x["expiry_dt"] == nearest]

    for x in options:
        x.pop("expiry_dt", None)

    options.sort(key=lambda x: (abs(x["strike"] - atm), x["strike"], x["type"]))

    return atm, nearest.strftime("%d%b%Y").upper(), options


@app.get("/")
def root():
    return {
        "status": "RIGA OPTION SNIPER LIVE",
        "features": [
            "auto ATM strike",
            "NIFTY options",
            "BANKNIFTY options",
            "FINNIFTY options",
            "SENSEX options"
        ]
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/spot")
def spot(
    index: str = Query("NIFTY"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    client = get_client()

    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    return get_ltp(client, INDEX_CONFIG[index]["spot"])


@app.get("/option-chain")
def option_chain(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    client = get_client()

    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(
        index,
        float(spot_data["ltp"]),
        strikes_around
    )

    return {
        "index": index,
        "spot": spot_data,
        "atm": atm,
        "nearest_expiry": expiry,
        "options_count": len(options),
        "options": options
    }


@app.get("/scan-options")
def scan_options(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    client = get_client()

    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(
        index,
        float(spot_data["ltp"]),
        strikes_around
    )

    results = []

    for opt in options:
        data = get_ltp(client, opt)
        signal = riga_logic(data)

        results.append({
            "option": opt,
            "data": data,
            "signal": signal
        })

    trades = [
        x for x in results
        if x["signal"].get("bias") in ["BUY", "SELL"]
        and x["signal"].get("confidence", 0) >= 70
    ]

    return {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "atm": atm,
        "nearest_expiry": expiry,
        "total_options_scanned": len(results),
        "trade_count": len(trades),
        "trades": trades,
        "all_results": results
    }


@app.get("/scan-all-options")
def scan_all_options(
    strikes_around: int = Query(2),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    output = {}

    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        try:
            client = get_client()
            spot_data = get_ltp(client, INDEX_CONFIG[idx]["spot"])

            if not spot_data:
                output[idx] = {"error": "spot failed"}
                continue

            atm, expiry, options = get_auto_option_chain(
                idx,
                float(spot_data["ltp"]),
                strikes_around
            )

            results = []

            for opt in options:
                data = get_ltp(client, opt)
                signal = riga_logic(data)
                results.append({
                    "option": opt,
                    "data": data,
                    "signal": signal
                })

            trades = [
                x for x in results
                if x["signal"].get("bias") in ["BUY", "SELL"]
                and x["signal"].get("confidence", 0) >= 70
            ]

            output[idx] = {
                "spot_ltp": spot_data["ltp"],
                "atm": atm,
                "expiry": expiry,
                "trade_count": len(trades),
                "trades": trades,
                "total_options_scanned": len(results)
            }

        except Exception as e:
            output[idx] = {"error": str(e)}

    return output
