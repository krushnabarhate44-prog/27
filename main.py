import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pyotp
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v9 PRO SNIPER", version="9.1")

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
        "step": 50,
    },
    "BANKNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009"},
        "option_exchange": "NFO",
        "option_name": "BANKNIFTY",
        "step": 100,
    },
    "FINNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037"},
        "option_exchange": "NFO",
        "option_name": "FINNIFTY",
        "step": 50,
    },
    "SENSEX": {
        "spot": {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1"},
        "option_exchange": "BFO",
        "option_name": "SENSEX",
        "step": 100,
    },
}


def check_token(authorization: Optional[str], token: Optional[str]):
    if not RIGA_ACTION_TOKEN:
        return
    if authorization == f"Bearer {RIGA_ACTION_TOKEN}" or token == RIGA_ACTION_TOKEN:
        return
    raise HTTPException(status_code=401, detail="Unauthorized")


def get_client():
    global client_obj
    if client_obj:
        return client_obj

    if not all([ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PASSWORD, ANGEL_TOTP_SECRET]):
        raise HTTPException(status_code=500, detail="Missing Angel credentials in .env")

    client = SmartConnect(api_key=ANGEL_API_KEY)
    totp = pyotp.TOTP(ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()).now()
    session = client.generateSession(ANGEL_CLIENT_CODE, ANGEL_PASSWORD, totp)

    if not session or not session.get("status"):
        raise HTTPException(status_code=500, detail=f"Angel login failed: {session}")

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


def get_ltp(client, item: Dict[str, Any]):
    try:
        res = client.ltpData(item["exchange"], item["tradingsymbol"], str(item["symboltoken"]))
    except Exception:
        return None

    if not res or not res.get("status"):
        return None

    d = res.get("data", {}) or {}
    return {
        "symbol": item["tradingsymbol"],
        "exchange": item["exchange"],
        "token": str(item["symboltoken"]),
        "ltp": d.get("ltp"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "close": d.get("close"),
    }


def get_candles(client, exchange: str, symboltoken: str, interval: str = "FIVE_MINUTE", lookback_minutes: int = 390):
    now = datetime.now()
    start = now - timedelta(minutes=lookback_minutes)
    params = {
        "exchange": exchange,
        "symboltoken": str(symboltoken),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": now.strftime("%Y-%m-%d %H:%M"),
    }

    try:
        res = client.getCandleData(params)
    except Exception:
        return []

    if not res or not res.get("status"):
        return []

    candles = []
    for row in res.get("data", []) or []:
        try:
            candles.append({
                "time": row[0],
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
            })
        except Exception:
            continue
    return candles


def safe_num(value):
    return isinstance(value, (int, float)) and value is not None


def round_to_step(price, step):
    return int(round(price / step) * step)


def parse_expiry(expiry):
    for fmt in ("%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(str(expiry).upper(), fmt)
        except Exception:
            pass
    return None


def avg(values: List[float]) -> float:
    vals = [v for v in values if safe_num(v)]
    return sum(vals) / len(vals) if vals else 0.0


def pct_change(a: float, b: float) -> float:
    if not b:
        return 0.0
    return ((a - b) / b) * 100


def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 0.01)


def candle_strength(c):
    return candle_body(c) / candle_range(c)


def candle_position(c):
    return (c["close"] - c["low"]) / candle_range(c)


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def is_bull_candle(c):
    return c["close"] > c["open"]


def is_bear_candle(c):
    return c["close"] < c["open"]


def calc_vwap(candles: List[Dict[str, Any]]):
    total_pv = 0.0
    total_v = 0.0
    for c in candles:
        tp = (c["high"] + c["low"] + c["close"]) / 3
        v = c.get("volume", 0) or 0
        total_pv += tp * v
        total_v += v
    if total_v <= 0:
        return None
    return total_pv / total_v


def find_swing_levels(candles: List[Dict[str, Any]], lookback: int = 20):
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    if not recent:
        return None
    prev = recent[:-1] if len(recent) > 1 else recent
    return {
        "swing_high": max(c["high"] for c in recent),
        "swing_low": min(c["low"] for c in recent),
        "prev_high": max(c["high"] for c in prev),
        "prev_low": min(c["low"] for c in prev),
    }


def volume_spike(candles: List[Dict[str, Any]], lookback: int = 20):
    if len(candles) < 5:
        return False, 0.0
    last_v = candles[-1].get("volume", 0) or 0
    prev_vols = [c.get("volume", 0) or 0 for c in candles[-lookback - 1:-1]]
    base = avg(prev_vols)
    if base <= 0:
        return False, 0.0
    ratio = last_v / base
    return ratio >= 1.25, round(ratio, 2)


def classify_candle(c):
    strength = candle_strength(c)
    pos = candle_position(c)
    if strength >= 0.70 and pos >= 0.75 and is_bull_candle(c):
        return "A_PLUS_BULL"
    if strength >= 0.60 and pos >= 0.65 and is_bull_candle(c):
        return "A_BULL"
    if strength >= 0.45 and pos >= 0.60 and is_bull_candle(c):
        return "B_BULL"
    if strength >= 0.70 and pos <= 0.25 and is_bear_candle(c):
        return "A_PLUS_BEAR"
    if strength >= 0.60 and pos <= 0.35 and is_bear_candle(c):
        return "A_BEAR"
    if strength >= 0.45 and pos <= 0.40 and is_bear_candle(c):
        return "B_BEAR"
    return "LOW_QUALITY"


def trap_filter(c):
    body = max(candle_body(c), 0.01)
    if candle_strength(c) < 0.35:
        return True, "weak body / indecision"
    if upper_wick(c) > body * 1.8 and candle_position(c) < 0.75:
        return True, "upper wick rejection / fake breakout risk"
    if lower_wick(c) > body * 1.8 and candle_position(c) > 0.25:
        return True, "lower wick rejection / fake breakdown risk"
    return False, "no liquidity trap"


def get_auto_option_chain(index_name, spot_price, strikes_around=3):
    cfg = INDEX_CONFIG[index_name]
    master = load_scrip_master()
    atm = round_to_step(spot_price, cfg["step"])
    allowed = {atm + i * cfg["step"] for i in range(-strikes_around, strikes_around + 1)}

    today = datetime.now().date()
    found = []
    for s in master:
        try:
            if s.get("name") != cfg["option_name"] or s.get("exch_seg") != cfg["option_exchange"]:
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
            if not expiry_dt or expiry_dt.date() < today:
                continue
            found.append({
                "exchange": cfg["option_exchange"],
                "tradingsymbol": symbol,
                "symboltoken": str(s.get("token")),
                "strike": strike,
                "type": "CE" if symbol.endswith("CE") else "PE",
                "expiry": s.get("expiry"),
                "expiry_dt": expiry_dt,
            })
        except Exception:
            continue

    if not found:
        return atm, None, []

    nearest = min(x["expiry_dt"] for x in found)
    options = [x for x in found if x["expiry_dt"] == nearest]
    for x in options:
        x.pop("expiry_dt", None)
    options.sort(key=lambda x: (abs(x["strike"] - atm), x["strike"], x["type"]))
    return atm, nearest.strftime("%d%b%Y").upper(), options


def analyze_index_structure(index_name: str, spot_data: Dict[str, Any], candles: List[Dict[str, Any]]):
    if not candles or len(candles) < 10:
        return {"bias": "NEUTRAL", "option_side": None, "score": 0, "reason": "not enough index candle data"}

    last = candles[-1]
    prev_close = candles[-2]["close"]
    first_open = candles[0]["open"]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)

    score_bull, score_bear = 0, 0
    bull, bear = [], []

    intraday_change = pct_change(last["close"], first_open)
    if intraday_change > 0.12:
        score_bull += 20
        bull.append(f"index intraday bullish {round(intraday_change,2)}%")
    elif intraday_change < -0.12:
        score_bear += 20
        bear.append(f"index intraday bearish {round(intraday_change,2)}%")

    if vwap:
        if last["close"] > vwap:
            score_bull += 15
            bull.append("index above VWAP")
        else:
            score_bear += 15
            bear.append("index below VWAP")

    if levels and last["close"] > levels["prev_high"]:
        score_bull += 25
        bull.append("index breakout above swing high")
    if levels and last["close"] < levels["prev_low"]:
        score_bear += 25
        bear.append("index breakdown below swing low")

    cq = classify_candle(last)
    if cq in ["A_PLUS_BULL", "A_BULL"]:
        score_bull += 15
    if cq in ["A_PLUS_BEAR", "A_BEAR"]:
        score_bear += 15

    mom = pct_change(last["close"], prev_close)
    if mom > 0.03:
        score_bull += 10
    elif mom < -0.03:
        score_bear += 10

    if trap_filter(last)[0]:
        score_bull -= 20
        score_bear -= 20

    if score_bull >= 55 and score_bull > score_bear:
        return {"bias": "BULLISH", "option_side": "CE", "score": min(score_bull, 95), "reason": ", ".join(bull)}
    if score_bear >= 55 and score_bear > score_bull:
        return {"bias": "BEARISH", "option_side": "PE", "score": min(score_bear, 95), "reason": ", ".join(bear)}
    return {"bias": "NEUTRAL", "option_side": None, "score": max(score_bull, score_bear), "reason": "index structure not clean enough"}


def analyze_option_buy_setup(index_bias, opt, ltp_data, candles, atm, index_name):
    side = index_bias.get("option_side")
    if opt.get("type") != side or not ltp_data or not candles or len(candles) < 10:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "setup data not valid"}

    last = candles[-1]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    if not levels:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "no swing levels"}

    score = 12 if index_bias.get("score", 0) >= 55 else 0
    entry = float(ltp_data["ltp"])

    if last["close"] > levels["prev_high"]:
        score += 25
        pattern = "PREMIUM_BREAKOUT"
    elif vwap and last["close"] > vwap and last["close"] > candles[-2]["close"]:
        score += 10
        pattern = "PREMIUM_CONTINUATION"
    else:
        return {"bias": "NO TRADE", "confidence": score, "reason": "premium breakout not activated"}

    cq = classify_candle(last)
    if cq == "A_PLUS_BULL":
        score += 20
    elif cq == "A_BULL":
        score += 15
    else:
        return {"bias": "NO TRADE", "confidence": score, "reason": "premium candle quality not strong"}

    risk_sl = min(levels["swing_low"], levels["prev_low"]) - max(entry * 0.015, 2.0)
    risk = round(entry - risk_sl, 2)
    risk_pct = (risk / entry) * 100 if entry else 999
    if risk_sl <= 0 or risk_sl >= entry or risk_pct > 22 or risk_pct < 2:
        return {"bias": "NO TRADE", "confidence": score, "reason": "invalid risk structure"}

    t1, t2, t3 = round(entry + risk * 1.5, 2), round(entry + risk * 2.0, 2), round(entry + risk * 3.0, 2)
    score += 10 if abs(opt["strike"] - atm) == 0 else 7

    confidence = min(score, 95)
    if confidence < 85:
        return {"bias": "NO TRADE", "confidence": confidence, "reason": "score below 85"}

    expected_profit_pct = round(((t2 - entry) / entry) * 100, 2)
    return {
        "bias": "BUY_CE" if side == "CE" else "BUY_PE",
        "entry": round(entry, 2),
        "sl": round(risk_sl, 2),
        "target": t2,
        "targets": {"t1": t1, "t2": t2, "t3": t3},
        "risk": risk,
        "risk_pct": round(risk_pct, 2),
        "expected_profit_pct": expected_profit_pct,
        "confidence": confidence,
        "pattern": pattern,
        "candle": cq,
        "atm_distance": abs(opt["strike"] - atm),
    }


def select_best_trade(trades):
    if not trades:
        return None

    def score_key(t):
        sig = t["signal"]
        return (sig.get("confidence", 0), sig.get("expected_profit_pct", 0), -sig.get("atm_distance", 9999), -sig.get("risk_pct", 99))

    return sorted(trades, key=score_key, reverse=True)[0]


@app.get("/scan-options")
def scan_options(index: str = Query("NIFTY"), strikes_around: int = Query(3), interval: str = Query("FIVE_MINUTE"), authorization: Optional[str] = Header(None), token: Optional[str] = Query(None)):
    check_token(authorization, token)
    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_item = INDEX_CONFIG[index]["spot"]
    spot_data = get_ltp(client, spot_item)
    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    index_candles = get_candles(client, spot_item["exchange"], spot_item["symboltoken"], interval=interval)
    index_bias = analyze_index_structure(index, spot_data, index_candles)

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)
    side = index_bias.get("option_side")
    options = [opt for opt in options if opt["type"] == side] if side in ["CE", "PE"] else []

    trades = []
    for opt in options:
        ltp_data = get_ltp(client, opt)
        opt_candles = get_candles(client, opt["exchange"], opt["symboltoken"], interval=interval)
        signal = analyze_option_buy_setup(index_bias, opt, ltp_data, opt_candles, atm, index)
        if signal.get("bias") in ["BUY_CE", "BUY_PE"]:
            trades.append({"index": index, "option": opt, "data": ltp_data, "signal": signal})

    return {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "index_bias": index_bias,
        "atm": atm,
        "nearest_expiry": expiry,
        "trade_count": len(trades),
        "best_trade": select_best_trade(trades) if trades else "NO TRADE",
        "trades": trades[:5],
    }
