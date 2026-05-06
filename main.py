import os
import requests
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v8 OPTION BUYING ONLY", version="8.0")

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

    client = SmartConnect(api_key=ANGEL_API_KEY)

    totp = pyotp.TOTP(
        ANGEL_TOTP_SECRET.strip().replace(" ", "").upper()
    ).now()

    session = client.generateSession(
        ANGEL_CLIENT_CODE,
        ANGEL_PASSWORD,
        totp,
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


# -----------------------------
# Data helpers
# -----------------------------
def get_ltp(client, item: Dict[str, Any]):
    try:
        res = client.ltpData(
            item["exchange"],
            item["tradingsymbol"],
            str(item["symboltoken"]),
        )
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


def calc_change_pct(data: Dict[str, Any]) -> Optional[float]:
    ltp = data.get("ltp") if data else None
    close = data.get("close") if data else None

    if not safe_num(ltp) or not safe_num(close) or close == 0:
        return None

    return ((ltp - close) / close) * 100


def detect_index_bias(spot_data: Dict[str, Any], threshold_pct: float = 0.08) -> Dict[str, Any]:
    """
    Index bias is decided from spot LTP vs previous close.
    Bullish index -> BUY CE side
    Bearish index -> BUY PE side
    Neutral/choppy -> NO TRADE
    """
    change_pct = calc_change_pct(spot_data)

    if change_pct is None:
        return {
            "bias": "NEUTRAL",
            "option_side": None,
            "reason": "index change data unavailable",
            "change_pct": None,
        }

    if change_pct >= threshold_pct:
        return {
            "bias": "BULLISH",
            "option_side": "CE",
            "reason": f"index bullish vs previous close ({change_pct:.2f}%)",
            "change_pct": round(change_pct, 2),
        }

    if change_pct <= -threshold_pct:
        return {
            "bias": "BEARISH",
            "option_side": "PE",
            "reason": f"index bearish vs previous close ({change_pct:.2f}%)",
            "change_pct": round(change_pct, 2),
        }

    return {
        "bias": "NEUTRAL",
        "option_side": None,
        "reason": f"index neutral/choppy ({change_pct:.2f}%)",
        "change_pct": round(change_pct, 2),
    }


# -----------------------------
# RIGA option premium analysis
# -----------------------------
def candle_stats(data: Dict[str, Any]):
    if not data:
        return None

    high = data.get("high")
    low = data.get("low")
    ltp = data.get("ltp")
    open_p = data.get("open")

    if not all(safe_num(x) for x in [high, low, ltp, open_p]):
        return None

    rng = high - low
    if rng <= 0:
        return None

    body = abs(ltp - open_p)
    upper_wick = high - max(open_p, ltp)
    lower_wick = min(open_p, ltp) - low
    position = (ltp - low) / rng
    momentum = ((ltp - open_p) / open_p) * 100 if open_p else 0
    strength = body / rng

    return {
        "range": rng,
        "body": body,
        "upper_wick": upper_wick,
        "lower_wick": lower_wick,
        "position": position,
        "momentum": momentum,
        "strength": strength,
    }


def candle_quality(data: Dict[str, Any]):
    st = candle_stats(data)
    if not st:
        return "BAD", 0

    strength = st["strength"]

    if strength >= 0.70:
        return "A_PLUS", strength
    if strength >= 0.60:
        return "A_GRADE", strength
    if strength >= 0.45:
        return "B_GRADE", strength

    return "LOW_QUALITY", strength


def detect_premium_bullish_pattern(data: Dict[str, Any]):
    """
    OPTION BUYING ONLY:
    For both CE buying and PE buying, option premium must show bullish behavior.
    No premium SELL/SHORT logic is used.
    """
    st = candle_stats(data)
    if not st:
        return "NO_PATTERN"

    pos = st["position"]
    mom = st["momentum"]
    strength = st["strength"]

    if pos > 0.88 and mom > 0.75 and strength >= 0.60:
        return "PREMIUM_BULLISH_BREAKOUT"

    if pos > 0.72 and mom > 0.40 and strength >= 0.45:
        return "PREMIUM_BULLISH_CONTINUATION"

    if mom > 0.25 and strength >= 0.35 and pos > 0.55:
        return "PREMIUM_BUYING_PRESSURE"

    return "NO_PATTERN"


def liquidity_trap_filter(data: Dict[str, Any]):
    st = candle_stats(data)
    if not st:
        return True, "bad candle data"

    body = st["body"]
    upper_wick = st["upper_wick"]
    lower_wick = st["lower_wick"]
    pos = st["position"]

    if body <= 0:
        return True, "doji/no body trap"

    if upper_wick > body * 1.7 and pos < 0.75:
        return True, "upper wick rejection trap"

    if lower_wick > body * 1.7 and pos > 0.25:
        return True, "lower wick rejection trap"

    return False, "no liquidity trap"


def premium_retest_or_acceptance_filter(data: Dict[str, Any], pattern: str):
    """
    Retest is still approximate because Angel LTP data gives only OHLC/LTP.
    This does not force retest. It gives extra score only.
    """
    st = candle_stats(data)
    if not st:
        return False, "no acceptance data"

    if pattern in [
        "PREMIUM_BULLISH_BREAKOUT",
        "PREMIUM_BULLISH_CONTINUATION",
        "PREMIUM_BUYING_PRESSURE",
    ] and st["position"] >= 0.65:
        return True, "premium bullish acceptance"

    return False, "premium acceptance not confirmed"


def riga_option_buy_logic(
    data: Dict[str, Any],
    option_type: str,
    index_bias: Dict[str, Any],
    min_confidence: int = 70,
):
    """
    Final output can only be:
    - BUY_CE
    - BUY_PE
    - NO TRADE

    Conversion:
    Bullish index -> scan CE -> BUY_CE
    Bearish index -> scan PE -> BUY_PE

    No SELL / SHORT / option writing.
    """
    if not data:
        return {"bias": "NO TRADE", "confidence": 0, "reason": "No option premium data"}

    if index_bias.get("bias") == "NEUTRAL" or not index_bias.get("option_side"):
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": index_bias.get("reason", "index neutral"),
        }

    required_side = index_bias["option_side"]
    if option_type != required_side:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": f"index {index_bias['bias']} requires {required_side}, skipped {option_type}",
        }

    st = candle_stats(data)
    if not st:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "Invalid option OHLC data",
        }

    pattern = detect_premium_bullish_pattern(data)
    candle, strength = candle_quality(data)
    trap, trap_reason = liquidity_trap_filter(data)
    accepted, accepted_reason = premium_retest_or_acceptance_filter(data, pattern)

    momentum = st["momentum"]
    position = st["position"]
    rng = st["range"]
    ltp = data["ltp"]

    score = 0
    reasons = [index_bias.get("reason", "")]

    # Pattern + momentum
    if pattern == "PREMIUM_BULLISH_BREAKOUT":
        score += 30
        reasons.append("option premium bullish breakout")
    elif pattern == "PREMIUM_BULLISH_CONTINUATION":
        score += 24
        reasons.append("option premium bullish continuation")
    elif pattern == "PREMIUM_BUYING_PRESSURE":
        score += 18
        reasons.append("option premium buying pressure")

    # Candle quality
    if candle == "A_PLUS":
        score += 20
        reasons.append("A+ premium candle")
    elif candle == "A_GRADE":
        score += 15
        reasons.append("A grade premium candle")
    elif candle == "B_GRADE":
        score += 8
        reasons.append("B grade premium candle")

    # Premium momentum
    if momentum > 0.75:
        score += 20
        reasons.append("strong option premium momentum")
    elif momentum > 0.40:
        score += 12
        reasons.append("positive option premium momentum")

    # Price position in option premium range.
    # This is not a day-high/day-low rejection rule. It only rewards premium strength.
    if position > 0.85:
        score += 15
        reasons.append("premium near upper range")
    elif position > 0.65:
        score += 8
        reasons.append("premium holding upper half")

    if accepted:
        score += 10
        reasons.append(accepted_reason)

    if trap:
        score -= 25
        reasons.append(trap_reason)
    else:
        reasons.append(trap_reason)

    trade_bias = "BUY_CE" if option_type == "CE" else "BUY_PE"

    # Structure based premium SL and RR targets
    # SL below option premium swing/range low with small buffer.
    buffer_points = max(round(rng * 0.05, 2), 0.05)
    sl = round(data["low"] - buffer_points, 2)

    if sl >= ltp:
        return {
            "bias": "NO TRADE",
            "confidence": min(score, 95),
            "pattern": pattern,
            "candle": candle,
            "candle_strength": round(strength, 2),
            "trap_filter": trap_reason,
            "reason": "Invalid BUY structure: SL is not below entry",
        }

    risk = round(ltp - sl, 2)
    t1 = round(ltp + risk * 1.5, 2)
    t2 = round(ltp + risk * 2.0, 2)
    t3 = round(ltp + risk * 3.0, 2)

    if t1 <= ltp:
        return {
            "bias": "NO TRADE",
            "confidence": min(score, 95),
            "reason": "Invalid BUY structure: target is not above entry",
        }

    if score >= min_confidence:
        return {
            "bias": trade_bias,
            "entry": round(ltp, 2),
            "sl": sl,
            "target": t2,
            "targets": {
                "t1": t1,
                "t2": t2,
                "t3": t3,
            },
            "risk": risk,
            "confidence": min(score, 95),
            "pattern": pattern,
            "candle": candle,
            "candle_strength": round(strength, 2),
            "premium_acceptance": accepted,
            "trap_filter": trap_reason,
            "reason": ", ".join([r for r in reasons if r]),
        }

    return {
        "bias": "NO TRADE",
        "confidence": min(score, 95),
        "pattern": pattern,
        "candle": candle,
        "candle_strength": round(strength, 2),
        "premium_acceptance": accepted,
        "trap_filter": trap_reason,
        "reason": "RIGA option buying confirmations below threshold: " + ", ".join([r for r in reasons if r]),
    }


# -----------------------------
# Option chain
# -----------------------------
def get_auto_option_chain(index_name, spot_price, strikes_around=3):
    index_name = index_name.upper()

    if index_name not in INDEX_CONFIG:
        raise HTTPException(
            status_code=400,
            detail="Use NIFTY, BANKNIFTY, FINNIFTY, SENSEX",
        )

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


def enrich_options_with_ltp(client, options: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    enriched = []

    for opt in options:
        data = get_ltp(client, opt)
        enriched.append({
            **opt,
            "premium": data,
        })

    return enriched


def select_best_trade(trades: List[Dict[str, Any]]):
    if not trades:
        return None

    return sorted(
        trades,
        key=lambda x: x["signal"].get("confidence", 0),
        reverse=True,
    )[0]


# -----------------------------
# Routes
# -----------------------------
@app.get("/")
def root():
    return {
        "status": "RIGA v8 OPTION BUYING ONLY LIVE",
        "allowed_outputs": ["BUY_CE", "BUY_PE", "NO TRADE"],
        "features": [
            "index bullish converts to CE buy scan",
            "index bearish converts to PE buy scan",
            "SELL/SHORT/WRITING disabled",
            "option premium bullish confirmation required",
            "premium SL below entry",
            "premium targets above entry",
            "auto ATM option chain",
            "option-chain endpoint returns premium LTP/OHLC",
            "liquidity trap filter",
            "candlestick grading",
            "70 confidence rule",
        ],
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/spot")
def spot(
    index: str = Query("NIFTY"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    return get_ltp(client, INDEX_CONFIG[index]["spot"])


@app.get("/option-chain")
def option_chain(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    include_premium: bool = Query(True),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    atm, expiry, options = get_auto_option_chain(
        index,
        float(spot_data["ltp"]),
        strikes_around,
    )

    options_out = enrich_options_with_ltp(client, options) if include_premium else options

    return {
        "index": index,
        "spot": spot_data,
        "index_bias": detect_index_bias(spot_data),
        "atm": atm,
        "nearest_expiry": expiry,
        "options_count": len(options_out),
        "options": options_out,
    }


@app.get("/scan-options")
def scan_options(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
    min_confidence: int = Query(70),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)

    index = index.upper()
    if index not in INDEX_CONFIG:
        raise HTTPException(status_code=400, detail="Invalid index")

    client = get_client()
    spot_data = get_ltp(client, INDEX_CONFIG[index]["spot"])

    if not spot_data:
        raise HTTPException(status_code=500, detail="Spot data failed")

    index_bias = detect_index_bias(spot_data)

    atm, expiry, options = get_auto_option_chain(
        index,
        float(spot_data["ltp"]),
        strikes_around,
    )

    trades = []
    scanned = 0

    for opt in options:
        # Main conversion:
        # Bullish index = CE only, Bearish index = PE only.
        if index_bias.get("option_side") and opt.get("type") != index_bias["option_side"]:
            continue

        data = get_ltp(client, opt)
        signal = riga_option_buy_logic(
            data=data,
            option_type=opt.get("type"),
            index_bias=index_bias,
            min_confidence=min_confidence,
        )
        scanned += 1

        if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= min_confidence:
            trade_obj = {
                "index": index,
                "option": opt,
                "data": data,
                "signal": signal,
            }
            trades.append(trade_obj)

    best_trade = select_best_trade(trades)

    return {
        "index": index,
        "spot_ltp": spot_data["ltp"],
        "spot_close": spot_data.get("close"),
        "index_bias": index_bias,
        "atm": atm,
        "nearest_expiry": expiry,
        "total_options_scanned": scanned,
        "trade_count": len(trades),
        "best_trade": best_trade if best_trade else "NO TRADE",
        "trades": trades[:5],
    }


@app.get("/scan-all-options")
def scan_all_options(
    strikes_around: int = Query(2),
    min_confidence: int = Query(70),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    check_token(authorization, token)

    output = {}
    overall_trades = []
    client = get_client()

    for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
        try:
            spot_data = get_ltp(client, INDEX_CONFIG[idx]["spot"])

            if not spot_data:
                output[idx] = {"error": "spot failed"}
                continue

            index_bias = detect_index_bias(spot_data)

            atm, expiry, options = get_auto_option_chain(
                idx,
                float(spot_data["ltp"]),
                strikes_around,
            )

            trades = []
            scanned = 0

            for opt in options:
                # Main conversion:
                # Bullish index = CE only, Bearish index = PE only.
                if index_bias.get("option_side") and opt.get("type") != index_bias["option_side"]:
                    continue

                data = get_ltp(client, opt)
                signal = riga_option_buy_logic(
                    data=data,
                    option_type=opt.get("type"),
                    index_bias=index_bias,
                    min_confidence=min_confidence,
                )
                scanned += 1

                if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= min_confidence:
                    trade_obj = {
                        "index": idx,
                        "option": opt,
                        "data": data,
                        "signal": signal,
                    }
                    trades.append(trade_obj)
                    overall_trades.append(trade_obj)

            best_trade = select_best_trade(trades)

            output[idx] = {
                "spot_ltp": spot_data["ltp"],
                "spot_close": spot_data.get("close"),
                "index_bias": index_bias,
                "atm": atm,
                "expiry": expiry,
                "total_options_scanned": scanned,
                "trade_count": len(trades),
                "best_trade": best_trade if best_trade else "NO TRADE",
                "trades": trades[:3],
            }

        except Exception as e:
            output[idx] = {"error": str(e)}

    overall_best = select_best_trade(overall_trades)

    return {
        "overall_best_trade": overall_best if overall_best else "NO TRADE",
        "markets": output,
    }
