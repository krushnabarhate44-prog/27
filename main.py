import os
import requests
from datetime import datetime
from typing import Optional

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(title="RIGA v8 OPTION BUYING SNIPER", version="8.1")

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
    except Exception:
        return None

    if not res or not res.get("status"):
        return None

    d = res.get("data", {})

    return {
        "symbol": item["tradingsymbol"],
        "exchange": item["exchange"],
        "token": item["symboltoken"],
        "ltp": d.get("ltp"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "close": d.get("close"),
    }


def safe_num(value):
    return isinstance(value, (int, float)) and value is not None


def candle_stats(data):
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


def candle_quality(data):
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


def detect_premium_pattern(data):
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

    return "NO_PATTERN"


def liquidity_trap_filter(data):
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


def premium_acceptance_filter(data, pattern):
    st = candle_stats(data)
    if not st:
        return False, "no premium acceptance data"

    # Option buying only: CE/PE premium must close/ltp in upper range.
    if pattern in ["PREMIUM_BULLISH_BREAKOUT", "PREMIUM_BULLISH_CONTINUATION"]:
        if st["position"] >= 0.72:
            return True, "premium bullish acceptance"

    return False, "premium acceptance not confirmed"


def get_index_bias(spot_data, min_change_pct=0.10):
    if not spot_data:
        return {
            "bias": "NEUTRAL",
            "option_side": None,
            "reason": "spot data missing",
            "change_pct": 0,
        }

    ltp = spot_data.get("ltp")
    close = spot_data.get("close")

    if not safe_num(ltp) or not safe_num(close) or close == 0:
        return {
            "bias": "NEUTRAL",
            "option_side": None,
            "reason": "invalid spot close/ltp",
            "change_pct": 0,
        }

    change_pct = round(((ltp - close) / close) * 100, 2)

    if change_pct >= min_change_pct:
        return {
            "bias": "BULLISH",
            "option_side": "CE",
            "reason": f"index bullish vs previous close ({change_pct}%)",
            "change_pct": change_pct,
        }

    if change_pct <= -min_change_pct:
        return {
            "bias": "BEARISH",
            "option_side": "PE",
            "reason": f"index bearish vs previous close ({change_pct}%)",
            "change_pct": change_pct,
        }

    return {
        "bias": "NEUTRAL",
        "option_side": None,
        "reason": f"index neutral vs previous close ({change_pct}%)",
        "change_pct": change_pct,
    }


def make_buy_levels(data):
    """
    Option buying levels.
    SL is below option premium recent low with buffer.
    Buffer is dynamic but capped so SL does not become unnecessarily wide.
    """
    st = candle_stats(data)
    if not st:
        return None

    ltp = data.get("ltp")
    low = data.get("low")
    rng = st["range"]

    if not safe_num(ltp) or not safe_num(low):
        return None

    # Structure-based approximation with only OHLC:
    # SL below day's/recent option low + small buffer.
    buffer = max(round(ltp * 0.01, 2), round(rng * 0.03, 2), 1.0)
    sl = round(low - buffer, 2)

    risk = round(ltp - sl, 2)

    # Risk guardrails: reject very tight or very wide SL.
    min_risk = max(round(ltp * 0.01, 2), 1.0)
    max_risk = round(ltp * 0.35, 2)

    if risk <= min_risk:
        return None

    if risk > max_risk:
        # Fallback to tighter structural SL around lower wick/open zone.
        alt_sl = round(ltp - max_risk, 2)
        if alt_sl < ltp:
            sl = alt_sl
            risk = round(ltp - sl, 2)
        else:
            return None

    t1 = round(ltp + risk * 1.5, 2)
    t2 = round(ltp + risk * 2.0, 2)
    t3 = round(ltp + risk * 3.0, 2)

    if not (sl < ltp < t1 < t2 < t3):
        return None

    return {
        "entry": round(ltp, 2),
        "sl": sl,
        "target": t2,
        "targets": {
            "t1": t1,
            "t2": t2,
            "t3": t3,
        },
        "risk": risk,
    }


def riga_option_buy_logic(data, option_type, index_bias):
    """
    Final RIGA execution rule:
    - Bullish index -> BUY CE only
    - Bearish index -> BUY PE only
    - No SELL/SHORT/WRITING output
    """
    if not data:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "No option premium data",
        }

    if not index_bias or index_bias.get("option_side") not in ["CE", "PE"]:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "Index bias neutral/invalid",
        }

    required_side = index_bias["option_side"]
    if option_type != required_side:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": f"Option side skipped; index requires {required_side}",
        }

    st = candle_stats(data)
    if not st:
        return {
            "bias": "NO TRADE",
            "confidence": 0,
            "reason": "Invalid option OHLC data",
        }

    pattern = detect_premium_pattern(data)
    candle, strength = candle_quality(data)
    trap, trap_reason = liquidity_trap_filter(data)
    accepted, accept_reason = premium_acceptance_filter(data, pattern)

    momentum = st["momentum"]
    position = st["position"]

    score = 0
    reasons = [index_bias.get("reason", "")]

    if pattern == "PREMIUM_BULLISH_BREAKOUT":
        score += 30
        reasons.append("option premium bullish breakout")
    elif pattern == "PREMIUM_BULLISH_CONTINUATION":
        score += 22
        reasons.append("option premium bullish continuation")
    else:
        reasons.append("no bullish premium pattern")

    if candle == "A_PLUS":
        score += 20
        reasons.append("A+ premium candle")
    elif candle == "A_GRADE":
        score += 15
        reasons.append("A grade premium candle")
    elif candle == "B_GRADE":
        score += 8
        reasons.append("B grade premium candle")

    if momentum > 0.75:
        score += 20
        reasons.append("strong option premium momentum")
    elif momentum > 0.40:
        score += 10
        reasons.append("moderate option premium momentum")

    if position > 0.85:
        score += 15
        reasons.append("premium near upper range")
    elif position > 0.72:
        score += 8
        reasons.append("premium holding upper range")

    if accepted:
        score += 10
        reasons.append(accept_reason)

    if trap:
        score -= 25
        reasons.append(trap_reason)
    else:
        reasons.append(trap_reason)

    levels = make_buy_levels(data)
    if not levels:
        return {
            "bias": "NO TRADE",
            "confidence": min(max(score, 0), 95),
            "pattern": pattern,
            "candle": candle,
            "candle_strength": round(strength, 2),
            "premium_acceptance": accepted,
            "trap_filter": trap_reason,
            "reason": "Invalid BUY option SL/RR structure, " + ", ".join([r for r in reasons if r]),
        }

    if score >= 70:
        trade_bias = "BUY_CE" if option_type == "CE" else "BUY_PE"
        return {
            "bias": trade_bias,
            **levels,
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
        "confidence": min(max(score, 0), 95),
        "pattern": pattern,
        "candle": candle,
        "candle_strength": round(strength, 2),
        "premium_acceptance": accepted,
        "trap_filter": trap_reason,
        "reason": "RIGA option-buy confirmations below 70, " + ", ".join([r for r in reasons if r]),
    }


def round_to_step(price, step):
    return int(round(price / step) * step)


def parse_expiry(expiry):
    for fmt in ("%d%b%Y", "%d%b%y"):
        try:
            return datetime.strptime(str(expiry).upper(), fmt)
        except Exception:
            pass
    return None


def get_auto_option_chain(index_name, spot_price, strikes_around=3):
    index_name = index_name.upper()

    if index_name not in INDEX_CONFIG:
        raise HTTPException(
            status_code=400,
            detail="Use NIFTY, BANKNIFTY, FINNIFTY, SENSEX"
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


def select_best_trade(trades):
    if not trades:
        return None

    # Prefer high confidence, then smaller risk percentage, then closer ATM.
    def rank(t):
        sig = t.get("signal", {})
        data = t.get("data", {})
        entry = sig.get("entry") or data.get("ltp") or 0
        risk = sig.get("risk") or 999999
        risk_pct = (risk / entry) if entry else 999999
        atm_distance = t.get("atm_distance", 999999)
        return (
            sig.get("confidence", 0),
            -risk_pct,
            -atm_distance,
        )

    return sorted(trades, key=rank, reverse=True)[0]


@app.get("/")
def root():
    return {
        "status": "RIGA v8.1 OPTION BUYING LIVE",
        "features": [
            "BUY_CE / BUY_PE / NO TRADE only",
            "index bias to option side mapping",
            "bullish index scans CE only",
            "bearish index scans PE only",
            "premium bullish breakout/continuation",
            "premium LTP/OHLC in option-chain when include_premium=true",
            "SL below option premium entry",
            "targets above option premium entry",
            "wide SL risk guardrail",
            "liquidity trap filter",
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
    include_premium: bool = Query(False),
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
        strikes_around
    )

    if include_premium:
        enriched = []
        for opt in options:
            premium = get_ltp(client, opt)
            enriched.append({
                **opt,
                "premium": premium,
            })
        options = enriched

    return {
        "index": index,
        "spot": spot_data,
        "index_bias": get_index_bias(spot_data),
        "atm": atm,
        "nearest_expiry": expiry,
        "options_count": len(options),
        "options": options,
    }


@app.get("/scan-options")
def scan_options(
    index: str = Query("NIFTY"),
    strikes_around: int = Query(3),
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

    index_bias = get_index_bias(spot_data)

    atm, expiry, options = get_auto_option_chain(
        index,
        float(spot_data["ltp"]),
        strikes_around
    )

    required_side = index_bias.get("option_side")
    if required_side:
        options = [o for o in options if o.get("type") == required_side]
    else:
        options = []

    trades = []
    scanned = 0

    for opt in options:
        data = get_ltp(client, opt)
        signal = riga_option_buy_logic(data, opt.get("type"), index_bias)
        scanned += 1

        if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= 70:
            trade_obj = {
                "index": index,
                "option": opt,
                "data": data,
                "signal": signal,
                "atm_distance": abs(opt["strike"] - atm),
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

            index_bias = get_index_bias(spot_data)

            atm, expiry, options = get_auto_option_chain(
                idx,
                float(spot_data["ltp"]),
                strikes_around
            )

            required_side = index_bias.get("option_side")
            if required_side:
                options = [o for o in options if o.get("type") == required_side]
            else:
                options = []

            trades = []
            scanned = 0

            for opt in options:
                data = get_ltp(client, opt)
                signal = riga_option_buy_logic(data, opt.get("type"), index_bias)
                scanned += 1

                if signal.get("bias") in ["BUY_CE", "BUY_PE"] and signal.get("confidence", 0) >= 70:
                    trade_obj = {
                        "index": idx,
                        "option": opt,
                        "data": data,
                        "signal": signal,
                        "atm_distance": abs(opt["strike"] - atm),
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
