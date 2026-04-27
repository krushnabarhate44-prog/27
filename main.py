import os
import requests
from datetime import datetime
from typing import Optional

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA SNIPER v6", version="6.0")

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
    try:
        res = client.ltpData(
            item["exchange"],
            item["tradingsymbol"],
            str(item["symboltoken"])
        )
    except Exception as e:
        return None

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


def candle_quality(data):
    high = data.get("high")
    low = data.get("low")
    ltp = data.get("ltp")
    open_p = data.get("open")

    if not all(isinstance(x, (int, float)) for x in [high, low, ltp, open_p]):
        return "BAD", 0

    rng = high - low
    if rng <= 0:
        return "BAD", 0

    body = abs(ltp - open_p)
    strength = body / rng

    if strength >= 0.65:
        return "A_GRADE", strength
    if strength >= 0.45:
        return "B_GRADE", strength

    return "LOW_QUALITY", strength


def detect_pattern(data):
    high = data.get("high")
    low = data.get("low")
    ltp = data.get("ltp")
    open_p = data.get("open")

    if not all(isinstance(x, (int, float)) for x in [high, low, ltp, open_p]):
        return "NO_PATTERN"

    rng = high - low
    if rng <= 0:
        return "NO_PATTERN"

    position = (ltp - low) / rng
    momentum = ((ltp - open_p) / open_p) * 100 if open_p else 0

    if position > 0.85 and momentum > 0.70:
        return "BULLISH_BREAKOUT"

    if position < 0.15 and momentum < -0.70:
        return "BEARISH_BREAKDOWN"

    if position > 0.70 and momentum > 0.35:
        return "BULLISH_CONTINUATION"

    if position < 0.30 and momentum < -0.35:
        return "BEARISH_CONTINUATION"

    return "NO_PATTERN"


def fake_breakout_filter(data):
    high = data.get("high")
    low = data.get("low")
    ltp = data.get("ltp")
    open_p = data.get("open")

    if not all(isinstance(x, (int, float)) for x in [high, low, ltp, open_p]):
        return True

    rng = high - low
    if rng <= 0:
        return True

    upper_wick = high - max(open_p, ltp)
    lower_wick = min(open_p, ltp) - low
    body = abs(ltp - open_p)

    if upper_wick > body * 1.5 and ltp < high:
        return True

    if lower_wick > body * 1.5 and ltp > low:
        return True

    return False


def riga_sniper_logic(data, option_type=None):
    if not data:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "No data"
        }

    ltp = data.get("ltp")
    high = data.get("high")
    low = data.get("low")
    open_p = data.get("open")

    if not all(isinstance(x, (int, float)) for x in [ltp, high, low, open_p]):
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "Invalid data"
        }

    rng = high - low
    if rng <= 0:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "Invalid range"
        }

    position = (ltp - low) / rng
    momentum = ((ltp - open_p) / open_p) * 100 if open_p else 0

    pattern = detect_pattern(data)
    candle, strength = candle_quality(data)
    fake = fake_breakout_filter(data)

    buy_score = 0
    sell_score = 0
    reasons = []

    if pattern in ["BULLISH_BREAKOUT", "BULLISH_CONTINUATION"]:
        buy_score += 30
        reasons.append(pattern)

    if pattern in ["BEARISH_BREAKDOWN", "BEARISH_CONTINUATION"]:
        sell_score += 30
        reasons.append(pattern)

    if candle == "A_GRADE":
        buy_score += 20
        sell_score += 20
        reasons.append("A grade candle")
    elif candle == "B_GRADE":
        buy_score += 10
        sell_score += 10
        reasons.append("B grade candle")

    if momentum > 0.70:
        buy_score += 20
        reasons.append("strong bullish momentum")

    if momentum < -0.70:
        sell_score += 20
        reasons.append("strong bearish momentum")

    if position > 0.85:
        buy_score += 15
        reasons.append("near day high")

    if position < 0.15:
        sell_score += 15
        reasons.append("near day low")

    if fake:
        buy_score -= 25
        sell_score -= 25
        reasons.append("fake breakout risk")

    # Option logic alignment:
    # CE should prefer BUY, PE should prefer SELL.
    if option_type == "CE":
        sell_score -= 20
    if option_type == "PE":
        buy_score -= 20

    if buy_score >= 70 and buy_score >= sell_score:
        sl = round(ltp - rng * 0.20, 2)
        target = round(ltp + (ltp - sl) * 2, 2)

        return {
            "bias": "BUY",
            "entry": round(ltp, 2),
            "sl": sl,
            "target": target,
            "confidence": min(buy_score, 90),
            "pattern": pattern,
            "candle": candle,
            "candle_strength": round(strength, 2),
            "reason": ", ".join(reasons)
        }

    if sell_score >= 70 and sell_score > buy_score:
        sl = round(ltp + rng * 0.20, 2)
        target = round(ltp - (sl - ltp) * 2, 2)

        return {
            "bias": "SELL",
            "entry": round(ltp, 2),
            "sl": sl,
            "target": target,
            "confidence": min(sell_score, 90),
            "pattern": pattern,
            "candle": candle,
            "candle_strength": round(strength, 2),
            "reason": ", ".join(reasons)
        }

    return {
        "bias": "NO TRADE",
        "confidence": max(buy_score, sell_score),
        "pattern": pattern,
        "candle": candle,
        "candle_strength": round(strength, 2),
        "reason": "RIGA sniper confirmations below 70"
    }


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
        "status": "RIGA SNIPER v6 LIVE",
        "features": [
            "auto ATM strike",
            "option chain scan",
            "pattern filter",
            "candlestick filter",
            "fake breakout filter",
            "70 confidence rule"
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
        signal = riga_sniper_logic(data, opt.get("type"))

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
                signal = riga_sniper_logic(data, opt.get("type"))

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
