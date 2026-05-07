import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v9.2 PRO SNIPER", version="9.2")

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

client_obj = None
scrip_master_cache = None

IST = timezone(timedelta(hours=5, minutes=30))

INDEX_CONFIG = {
    "NIFTY": {
        # LTP token and historical candle token are different in Angel SmartAPI.
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY 50", "symboltoken": "26000", "hist_symboltoken": "99926000"},
        "option_exchange": "NFO",
        "option_name": "NIFTY",
        "step": 50,
    },
    "BANKNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY BANK", "symboltoken": "26009", "hist_symboltoken": "99926009"},
        "option_exchange": "NFO",
        "option_name": "BANKNIFTY",
        "step": 100,
    },
    "FINNIFTY": {
        "spot": {"exchange": "NSE", "tradingsymbol": "NIFTY FIN SERVICE", "symboltoken": "26037", "hist_symboltoken": "99926037"},
        "option_exchange": "NFO",
        "option_name": "FINNIFTY",
        "step": 50,
    },
    "SENSEX": {
        # BSE index LTP token is 1, but historical candles generally need 99919000.
        "spot": {"exchange": "BSE", "tradingsymbol": "SENSEX", "symboltoken": "1", "hist_symboltoken": "99919000"},
        "option_exchange": "BFO",
        "option_name": "SENSEX",
        "step": 100,
    },
}


# -----------------------------
# Auth / Client
# -----------------------------

def check_token(authorization: Optional[str], token: Optional[str]):
    if not RIGA_ACTION_TOKEN:
        return
    if authorization == f"Bearer {RIGA_ACTION_TOKEN}":
        return
    if token == RIGA_ACTION_TOKEN:
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


# -----------------------------
# Time / Candle Helpers
# -----------------------------

def now_ist() -> datetime:
    return datetime.now(IST)


def last_trading_date(dt: datetime) -> datetime:
    """
    Weekend fallback only. Exchange holidays are not handled here.
    """
    d = dt
    while d.weekday() >= 5:  # Sat/Sun
        d -= timedelta(days=1)
    return d


def candle_window_ist(lookback_minutes: int = 390):
    """
    FIX:
    Render runs in UTC. Angel candle API expects exchange time.
    We force IST and use today's market session 09:15 to now.
    If before market open, use previous trading day session.
    """
    n = now_ist()
    d = last_trading_date(n)

    market_start = d.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = d.replace(hour=15, minute=30, second=0, microsecond=0)

    if n < market_start:
        d = last_trading_date(d - timedelta(days=1))
        market_start = d.replace(hour=9, minute=15, second=0, microsecond=0)
        market_end = d.replace(hour=15, minute=30, second=0, microsecond=0)
        return market_start, market_end

    if n > market_end:
        return market_start, market_end

    # During live market: keep from 09:15 to now, not UTC lookback.
    return market_start, n


def normalize_interval(interval: str) -> str:
    allowed = {
        "ONE_MINUTE",
        "THREE_MINUTE",
        "FIVE_MINUTE",
        "TEN_MINUTE",
        "FIFTEEN_MINUTE",
        "THIRTY_MINUTE",
        "ONE_HOUR",
        "ONE_DAY",
    }
    interval = (interval or "FIVE_MINUTE").upper()
    return interval if interval in allowed else "FIVE_MINUTE"


# -----------------------------
# Market Data
# -----------------------------

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


def get_candles(
    client,
    exchange: str,
    symboltoken: str,
    interval: str = "FIVE_MINUTE",
    lookback_minutes: int = 390,
):
    """
    Angel SmartAPI response format:
    [timestamp, open, high, low, close, volume]

    Main bug fixed:
    - Previous version used datetime.now() on Render UTC.
    - That created wrong fromdate/todate for NSE/BSE.
    - Now using IST market session window.
    """
    interval = normalize_interval(interval)
    start, end = candle_window_ist(lookback_minutes)

    params = {
        "exchange": exchange,
        "symboltoken": str(symboltoken),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
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


def get_candles_debug(
    client,
    exchange: str,
    symboltoken: str,
    interval: str = "FIVE_MINUTE",
):
    interval = normalize_interval(interval)
    start, end = candle_window_ist()

    params = {
        "exchange": exchange,
        "symboltoken": str(symboltoken),
        "interval": interval,
        "fromdate": start.strftime("%Y-%m-%d %H:%M"),
        "todate": end.strftime("%Y-%m-%d %H:%M"),
    }

    try:
        res = client.getCandleData(params)
    except Exception as e:
        return {"params": params, "error": str(e), "count": 0, "sample": []}

    data = res.get("data", []) if res else []
    return {
        "params": params,
        "status": res.get("status") if res else None,
        "message": res.get("message") if res else None,
        "count": len(data or []),
        "sample": data[-3:] if data else [],
        "raw_keys": list(res.keys()) if isinstance(res, dict) else [],
    }


# -----------------------------
# Basic Math
# -----------------------------

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
    uw = upper_wick(c)
    lw = lower_wick(c)
    pos = candle_position(c)

    if candle_strength(c) < 0.35:
        return True, "weak body / indecision"

    if uw > body * 1.8 and pos < 0.75:
        return True, "upper wick rejection / fake breakout risk"

    if lw > body * 1.8 and pos > 0.25:
        return True, "lower wick rejection / fake breakdown risk"

    return False, "no liquidity trap"


# -----------------------------
# Option Chain
# -----------------------------

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

    today = now_ist()
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


# -----------------------------
# RIGA v9.1 Logic
# -----------------------------

def analyze_index_structure(index_name: str, spot_data: Dict[str, Any], candles: List[Dict[str, Any]]):
    if not candles or len(candles) < 10:
        ltp = spot_data.get("ltp")
        close = spot_data.get("close")

        if safe_num(ltp) and safe_num(close):
            chg = pct_change(ltp, close)

            if chg >= 0.12:
                return {
                    "bias": "BULLISH",
                    "option_side": "CE",
                    "score": 55,
                    "candle_count": len(candles or []),
                    "reason": f"fallback bullish vs close {round(chg, 2)}%"
                }

            if chg <= -0.12:
                return {
                    "bias": "BEARISH",
                    "option_side": "PE",
                    "score": 55,
                    "candle_count": len(candles or []),
                    "reason": f"fallback bearish vs close {round(chg, 2)}%"
                }

        return {
            "bias": "NEUTRAL",
            "option_side": None,
            "score": 0,
            "candle_count": len(candles or []),
            "reason": "not enough index candle data"
        }

    last = candles[-1]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio = volume_spike(candles)

    last_close = last["close"]
    prev_close = candles[-2]["close"]
    first_open = candles[0]["open"]

    score_bull = 0
    score_bear = 0
    bull = []
    bear = []

    intraday_change = pct_change(last_close, first_open)
    last_momentum = pct_change(last_close, prev_close)

    if intraday_change > 0.12:
        score_bull += 20
        bull.append(f"index intraday bullish {round(intraday_change, 2)}%")

    if intraday_change < -0.12:
        score_bear += 20
        bear.append(f"index intraday bearish {round(intraday_change, 2)}%")

    if vwap:
        if last_close > vwap:
            score_bull += 15
            bull.append("index above VWAP")
        elif last_close < vwap:
            score_bear += 15
            bear.append("index below VWAP")

    if levels and last_close > levels["prev_high"]:
        score_bull += 25
        bull.append("index breakout above swing high")

    if levels and last_close < levels["prev_low"]:
        score_bear += 25
        bear.append("index breakdown below swing low")

    cq = classify_candle(last)

    if cq in ["A_PLUS_BULL", "A_BULL"]:
        score_bull += 15
        bull.append(f"index {cq}")

    if cq in ["A_PLUS_BEAR", "A_BEAR"]:
        score_bear += 15
        bear.append(f"index {cq}")

    if last_momentum > 0.03:
        score_bull += 10
        bull.append("last candle bullish momentum")

    if last_momentum < -0.03:
        score_bear += 10
        bear.append("last candle bearish momentum")

    if vol_ok:
        score_bull += 5
        score_bear += 5
        bull.append(f"volume spike {vol_ratio}x")
        bear.append(f"volume spike {vol_ratio}x")

    trap, trap_reason = trap_filter(last)
    if trap:
        score_bull -= 20
        score_bear -= 20

    base = {
        "vwap": round(vwap, 2) if vwap else None,
        "candle_count": len(candles),
        "last_candle": last,
        "trap": trap_reason if trap else "no trap",
    }

    if score_bull >= 55 and score_bull > score_bear:
        return {
            **base,
            "bias": "BULLISH",
            "option_side": "CE",
            "score": min(score_bull, 95),
            "reason": ", ".join(bull)
        }

    if score_bear >= 55 and score_bear > score_bull:
        return {
            **base,
            "bias": "BEARISH",
            "option_side": "PE",
            "score": min(score_bear, 95),
            "reason": ", ".join(bear)
        }

    return {
        **base,
        "bias": "NEUTRAL",
        "option_side": None,
        "score": max(score_bull, score_bear),
        "reason": "index structure not clean enough"
    }


def analyze_option_buy_setup(index_bias, opt, ltp_data, candles, atm, index_name):
    side = index_bias.get("option_side")

    if side not in ["CE", "PE"]:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "index neutral"}

    if opt.get("type") != side:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "option side not aligned"}

    if not ltp_data or not safe_num(ltp_data.get("ltp")):
        return {"bias": "NO TRADE", "confidence": 0, "reason": "no option LTP"}

    if not candles or len(candles) < 10:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "not enough option candle data",
            "candle_count": len(candles or [])
        }

    last = candles[-1]
    levels = find_swing_levels(candles, 20)
    vwap = calc_vwap(candles)
    vol_ok, vol_ratio = volume_spike(candles)

    entry = float(ltp_data["ltp"])
    score = 0
    reasons = []

    idx_score = index_bias.get("score", 0)

    if idx_score >= 70:
        score += 20
    elif idx_score >= 55:
        score += 12

    reasons.append(index_bias.get("reason", ""))

    if not levels:
        return {"bias": "NO TRADE", "confidence": score, "reason": "no swing levels"}

    if last["close"] > levels["prev_high"]:
        score += 25
        reasons.append("premium breakout above swing high")
        pattern = "PREMIUM_BREAKOUT"
    elif vwap and last["close"] > vwap and last["close"] > candles[-2]["close"]:
        score += 10
        reasons.append("premium continuation above VWAP")
        pattern = "PREMIUM_CONTINUATION"
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "reason": "premium breakout not activated",
            "candle_count": len(candles)
        }

    cq = classify_candle(last)
    strength = candle_strength(last)

    if cq == "A_PLUS_BULL":
        score += 20
        reasons.append("A+ bullish premium candle")
    elif cq == "A_BULL":
        score += 15
        reasons.append("A grade bullish premium candle")
    else:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "pattern": pattern,
            "candle": cq,
            "reason": "premium candle quality not strong"
        }

    mom = pct_change(last["close"], candles[-2]["close"])

    if mom > 0.25:
        score += 15
        reasons.append(f"strong premium momentum {round(mom, 2)}%")
    elif mom > 0.08:
        score += 8
        reasons.append(f"premium momentum {round(mom, 2)}%")

    if vwap and last["close"] > vwap:
        score += 10
        reasons.append("premium above VWAP")

    if vol_ok:
        score += 10
        reasons.append(f"volume expansion {vol_ratio}x")

    trap, trap_reason = trap_filter(last)
    if trap:
        return {
            "bias": "NO TRADE",
            "confidence": max(score - 25, 0),
            "pattern": pattern,
            "candle": cq,
            "trap_filter": trap_reason,
            "reason": f"trap rejected: {trap_reason}"
        }

    step = INDEX_CONFIG[index_name]["step"]
    atm_distance = abs(opt["strike"] - atm)

    if atm_distance == 0:
        score += 10
        reasons.append("ATM strike")
    elif atm_distance <= step:
        score += 7
        reasons.append("near ATM strike")
    elif atm_distance <= step * 2:
        score += 2
        reasons.append("acceptable strike distance")
    else:
        score -= 15
        reasons.append("far from ATM penalty")

    buffer = max(entry * 0.015, 2.0)
    structure_sl = min(levels["swing_low"], levels["prev_low"]) - buffer
    risk = round(entry - structure_sl, 2)
    risk_pct = (risk / entry) * 100 if entry else 999

    if structure_sl <= 0 or structure_sl >= entry:
        return {"bias": "NO TRADE", "confidence": score, "reason": "invalid structure SL"}

    if risk_pct > 22:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "risk_pct": round(risk_pct, 2),
            "reason": "risk too wide >22%"
        }

    if risk_pct < 2:
        return {
            "bias": "NO TRADE",
            "confidence": score,
            "entry": round(entry, 2),
            "sl": round(structure_sl, 2),
            "risk_pct": round(risk_pct, 2),
            "reason": "risk too tight / noise SL"
        }

    t1 = round(entry + risk * 1.5, 2)
    t2 = round(entry + risk * 2.0, 2)
    t3 = round(entry + risk * 3.0, 2)

    if not (structure_sl < entry < t1 < t2 < t3):
        return {"bias": "NO TRADE", "confidence": score, "reason": "RR structure invalid"}

    confidence = min(score, 95)

    if confidence < 85:
        return {
            "bias": "NO TRADE",
            "confidence": confidence,
            "pattern": pattern,
            "candle": cq,
            "reason": "RIGA v9.2 sniper score below 85"
        }

    return {
        "bias": "BUY_CE" if side == "CE" else "BUY_PE",
        "entry": round(entry, 2),
        "sl": round(structure_sl, 2),
        "target": t2,
        "targets": {"t1": t1, "t2": t2, "t3": t3},
        "risk": risk,
        "risk_pct": round(risk_pct, 2),
        "confidence": confidence,
        "pattern": pattern,
        "candle": cq,
        "candle_strength": round(strength, 2),
        "premium_vwap": round(vwap, 2) if vwap else None,
        "volume_spike": vol_ratio if vol_ok else None,
        "atm_distance": atm_distance,
        "trap_filter": trap_reason,
        "candle_count": len(candles),
        "reason": ", ".join([r for r in reasons if r]),
    }


def select_best_trade(trades):
    if not trades:
        return None

    def score_key(t):
        sig = t["signal"]
        return (
            sig.get("confidence", 0),
            -sig.get("atm_distance", 9999),
            -sig.get("risk_pct", 99),
        )

    return sorted(trades, key=score_key, reverse=True)[0]


# -----------------------------
# Routes
# -----------------------------

@app.get("/")
def root():
    return {
        "status": "RIGA v9.2 PRO SNIPER LIVE",
        "fixes": [
            "IST candle window fixed for Render UTC",
            "historical index tokens fixed: 99926000 / 99926009 / 99926037 / 99919000",
            "candle debug endpoint added",
            "compact scan-all output added",
            "BUY_CE / BUY_PE / NO TRADE only",
            "structure based SL",
            "risk cap 22%",
            "score 85+ only"
        ]
    }


@app.get("/health")
def health():
    return {"status": "ok", "server_time_ist": now_ist().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/spot")
def spot(
    index: str = Query("NIFTY"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    return get_ltp(client, INDEX_CONFIG[index]["spot"])


@app.get("/candles-test")
def candles_test(
    index: str = Query("NIFTY"),
    interval: str = Query("FIVE_MINUTE"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    item = INDEX_CONFIG[index]["spot"]
    return get_candles_debug(client, item["exchange"], item.get("hist_symboltoken", item["symboltoken"]), interval)


@app.get("/option-chain")
def option_chain(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    include_premium: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)

    if include_premium:
        for opt in options:
            opt["premium"] = get_ltp(client, opt)

    return {
        "index": index,
        "spot": spot_data,
        "atm": atm,
        "nearest_expiry": expiry,
        "options_count": len(options),
        "options": options,
    }


@app.get("/scan-options")
def scan_options(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    debug: bool = Query(False),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_item = INDEX_CONFIG[index]["spot"]
    spot_data = get_ltp(client, spot_item)

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    index_candles = get_candles(client, spot_item["exchange"], spot_item.get("hist_symboltoken", spot_item["symboltoken"]), interval=interval)
    index_bias = analyze_index_structure(index, spot_data, index_candles)
    index_bias["index"] = index

    atm, expiry, options = get_auto_option_chain(index, float(spot_data["ltp"]), strikes_around)
    side = index_bias.get("option_side")

    if side not in ["CE", "PE"]:
        return {
            "index": index,
            "spot_ltp": spot_data["ltp"],
            "spot_close": spot_data.get("close"),
            "index_bias": index_bias,
            "atm": atm,
            "nearest_expiry": expiry,
            "total_options_scanned": 0,
            "trade_count": 0,
            "best_trade": "NO TRADE",
            "trades": [],
        }

    options = [opt for opt in options if opt["type"] == side]

    trades = []
    rejected = []
    scanned = 0

    for opt in options:
        ltp_data = get_ltp(client, opt)
        opt_candles = get_candles(client, opt["exchange"], opt["symboltoken"], interval=interval)
        signal = analyze_option_buy_setup(index_bias, opt, ltp_data, opt_candles, atm, index)
        scanned += 1

        if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= 85:
            trades.append({
                "index": index,
                "option": opt,
                "data": ltp_data,
                "signal": signal,
                "atm_distance": signal.get("atm_distance", abs(opt["strike"] - atm)),
            })
        elif debug:
            rejected.append({
                "symbol": opt.get("tradingsymbol"),
                "strike": opt.get("strike"),
                "type": opt.get("type"),
                "reason": signal.get("reason"),
                "confidence": signal.get("confidence", 0),
                "candle_count": signal.get("candle_count"),
            })

    best_trade = select_best_trade(trades)

    out = {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "spot_close": spot_data.get("close"),
        "index_bias": index_bias,
        "atm": atm,
        "nearest_expiry": expiry,
        "total_options_scanned": scanned,
        "trade_count": len(trades),
        "best_trade": best_trade if best_trade else "NO TRADE",
        "trades": trades[:3],
    }

    if debug:
        out["rejected"] = rejected[:10]

    return out


@app.get("/scan-all-options")
def scan_all_options(
    strikes_around: int = Query(3),
    interval: str = Query("FIVE_MINUTE"),
    compact: bool = Query(True),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None)
):
    check_token(authorization, token)

    output = {}
    overall_trades = []

    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        try:
            result = scan_options(
                index=idx,
                strikes_around=strikes_around,
                interval=interval,
                debug=False,
                authorization=authorization,
                token=token
            )

            best_trade = result.get("best_trade")

            if best_trade != "NO TRADE":
                overall_trades.append(best_trade)

            if compact:
                output[idx] = {
                    "spot_ltp": result.get("spot_ltp"),
                    "index_bias": result.get("index_bias"),
                    "atm": result.get("atm"),
                    "trade_count": result.get("trade_count"),
                    "best_trade": best_trade,
                }
            else:
                output[idx] = {
                    "spot_ltp": result.get("spot_ltp"),
                    "spot_close": result.get("spot_close"),
                    "index_bias": result.get("index_bias"),
                    "atm": result.get("atm"),
                    "expiry": result.get("nearest_expiry"),
                    "total_options_scanned": result.get("total_options_scanned"),
                    "trade_count": result.get("trade_count"),
                    "best_trade": best_trade,
                    "trades": result.get("trades", [])[:2],
                }

        except Exception as e:
            output[idx] = {"error": str(e)}

    overall_best = select_best_trade(overall_trades)

    return {
        "overall_best_trade": overall_best if overall_best else "NO TRADE",
        "markets": output,
    }
