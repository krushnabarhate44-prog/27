"""
RIGA AI - Final main.py
Book-based option buying scanner logic

IMPORTANT:
- Final trades are OPTION BUYING only.
- Bullish bias  -> BUY CE
- Bearish bias  -> BUY PE
- No option selling / writing / shorting.
- Entry, SL, and targets are calculated on OPTION PREMIUM.
- No clean retest hold / invalid RR / wide SL / confidence < 70 => NO TRADE.

This file is designed as a drop-in FastAPI backend core.
Connect your broker/data provider inside `fetch_market_data()` if you want live scans.
You can also POST raw candles to `/scan` for direct RIGA analysis.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Tuple
import os
import math
import json
import time
import requests
import base64
import hmac
import hashlib
import struct
from datetime import datetime, timedelta
from pathlib import Path

try:
    from fastapi import FastAPI, HTTPException
    from pydantic import BaseModel, Field
except Exception:  # keeps analysis functions importable even if FastAPI is not installed
    FastAPI = None
    HTTPException = Exception
    BaseModel = object
    Field = lambda default=None, **kwargs: default  # type: ignore


Side = Literal["CE", "PE"]
Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]
Decision = Literal["BUY_CE", "BUY_PE", "NO_TRADE"]


# -----------------------------
# Config
# -----------------------------

CONFIDENCE_MIN = 70
MAX_RISK_PCT = 15.0
DEFAULT_BUFFER_PCT = 0.015  # 1.5% premium buffer below swing low
MIN_CANDLES = 8


# -----------------------------
# API Models
# -----------------------------

class Candle(BaseModel):
    time: Optional[str] = None
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class OptionCandidate(BaseModel):
    symbol: str
    strike: float
    type: Side
    expiry: Optional[str] = None
    candles: List[Candle]
    ltp: Optional[float] = None


class ScanPayload(BaseModel):
    index: str = Field(..., description="NIFTY / BANKNIFTY / FINNIFTY / SENSEX")
    spot_ltp: Optional[float] = None
    atm: Optional[float] = None
    index_candles: List[Candle]
    options: List[OptionCandidate]
    strikes_around: int = 2


# -----------------------------
# Utility
# -----------------------------

def as_dict(c: Any) -> Dict[str, float]:
    """Accepts pydantic Candle, dataclass, or dict."""
    if isinstance(c, dict):
        return c
    if hasattr(c, "model_dump"):
        return c.model_dump()
    if hasattr(c, "dict"):
        return c.dict()
    return {
        "open": c.open,
        "high": c.high,
        "low": c.low,
        "close": c.close,
        "volume": getattr(c, "volume", 0.0),
    }


def safe_round(value: Optional[float], ndigits: int = 2) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), ndigits)


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def vwap(candles: List[Any]) -> Optional[float]:
    total_pv = 0.0
    total_vol = 0.0
    for c in candles:
        d = as_dict(c)
        vol = float(d.get("volume") or 0)
        if vol <= 0:
            continue
        typical = (d["high"] + d["low"] + d["close"]) / 3
        total_pv += typical * vol
        total_vol += vol
    if total_vol <= 0:
        return None
    return total_pv / total_vol


def recent_swing_high(candles: List[Any], lookback: int = 8) -> Optional[float]:
    if not candles:
        return None
    data = [as_dict(c) for c in candles[-lookback:]]
    return max(d["high"] for d in data)


def recent_swing_low(candles: List[Any], lookback: int = 8) -> Optional[float]:
    if not candles:
        return None
    data = [as_dict(c) for c in candles[-lookback:]]
    return min(d["low"] for d in data)


def day_high(candles: List[Any]) -> Optional[float]:
    if not candles:
        return None
    return max(as_dict(c)["high"] for c in candles)


def day_low(candles: List[Any]) -> Optional[float]:
    if not candles:
        return None
    return min(as_dict(c)["low"] for c in candles)


def body_strength_pct(candle: Any) -> float:
    d = as_dict(candle)
    rng = d["high"] - d["low"]
    if rng <= 0:
        return 0.0
    return abs(d["close"] - d["open"]) / rng


def candle_quality(candle: Any) -> str:
    pct = body_strength_pct(candle)
    if pct >= 0.70:
        return "A_PLUS"
    if pct >= 0.50:
        return "A"
    if pct >= 0.35:
        return "B"
    return "WEAK"


def candle_direction(candle: Any) -> Bias:
    d = as_dict(candle)
    if d["close"] > d["open"]:
        return "BULLISH"
    if d["close"] < d["open"]:
        return "BEARISH"
    return "NEUTRAL"


def detect_candlestick_pattern(candles: List[Any]) -> str:
    """Simple candle recognition for decision filtering; expand as needed."""
    if not candles:
        return "none"

    last = as_dict(candles[-1])
    q = candle_quality(last)
    direction = candle_direction(last)
    rng = last["high"] - last["low"]
    body = abs(last["close"] - last["open"])

    if rng <= 0:
        return "none"

    upper_wick = last["high"] - max(last["open"], last["close"])
    lower_wick = min(last["open"], last["close"]) - last["low"]

    # Doji / indecision
    if body / rng <= 0.15:
        return "doji"

    # Single candle patterns
    if lower_wick >= body * 2 and upper_wick <= body * 0.6:
        return "hammer" if direction == "BULLISH" else "hanging_man"
    if upper_wick >= body * 2 and lower_wick <= body * 0.6:
        return "shooting_star" if direction == "BEARISH" else "inverted_hammer"
    if q in ["A_PLUS", "A"] and upper_wick <= rng * 0.1 and lower_wick <= rng * 0.1:
        return "bullish_marubozu" if direction == "BULLISH" else "bearish_marubozu"

    # Two candle patterns
    if len(candles) >= 2:
        prev = as_dict(candles[-2])
        prev_dir = candle_direction(prev)
        last_body_low = min(last["open"], last["close"])
        last_body_high = max(last["open"], last["close"])
        prev_body_low = min(prev["open"], prev["close"])
        prev_body_high = max(prev["open"], prev["close"])

        if (
            prev_dir == "BEARISH"
            and direction == "BULLISH"
            and last_body_low <= prev_body_low
            and last_body_high >= prev_body_high
        ):
            return "bullish_engulfing"

        if (
            prev_dir == "BULLISH"
            and direction == "BEARISH"
            and last_body_low <= prev_body_low
            and last_body_high >= prev_body_high
        ):
            return "bearish_engulfing"

        if direction == "BULLISH" and prev_dir == "BEARISH":
            prev_mid = (prev["open"] + prev["close"]) / 2
            if last["close"] > prev_mid:
                return "piercing_line"

        if direction == "BEARISH" and prev_dir == "BULLISH":
            prev_mid = (prev["open"] + prev["close"]) / 2
            if last["close"] < prev_mid:
                return "dark_cloud_cover"

    return "none"


# -----------------------------
# Book Knowledge Engine
# -----------------------------

def detect_index_bias(index_candles: List[Any]) -> Tuple[Bias, int, List[str]]:
    reasons: List[str] = []
    if len(index_candles) < MIN_CANDLES:
        return "NEUTRAL", 0, ["not enough index candles"]

    data = [as_dict(c) for c in index_candles]
    closes = [d["close"] for d in data]
    last = data[-1]
    prev = data[-2]
    q = candle_quality(last)
    dirn = candle_direction(last)

    score = 0

    ema9 = ema(closes, min(9, len(closes)))
    ema21 = ema(closes, min(21, len(closes)))

    swing_hi = recent_swing_high(data[:-1], lookback=8)
    swing_lo = recent_swing_low(data[:-1], lookback=8)
    vw = vwap(data)

    # Bullish conditions
    bullish_points = 0
    bearish_points = 0

    if ema9 is not None and ema21 is not None:
        if ema9 > ema21:
            bullish_points += 20
            reasons.append("EMA momentum bullish")
        elif ema9 < ema21:
            bearish_points += 20
            reasons.append("EMA momentum bearish")

    if swing_hi is not None and last["close"] > swing_hi:
        bullish_points += 25
        reasons.append("index breakout above swing high")

    if swing_lo is not None and last["close"] < swing_lo:
        bearish_points += 25
        reasons.append("index breakdown below swing low")

    if dirn == "BULLISH" and q in ["A_PLUS", "A"]:
        bullish_points += 20
        reasons.append(f"last index candle {q} bullish")
    elif dirn == "BEARISH" and q in ["A_PLUS", "A"]:
        bearish_points += 20
        reasons.append(f"last index candle {q} bearish")
    elif q == "WEAK":
        reasons.append("weak body / indecision")

    if vw is not None:
        if last["close"] > vw:
            bullish_points += 10
            reasons.append("index above VWAP")
        elif last["close"] < vw:
            bearish_points += 10
            reasons.append("index below VWAP")

    # Follow-through check
    if last["close"] > prev["close"]:
        bullish_points += 10
    elif last["close"] < prev["close"]:
        bearish_points += 10

    if bullish_points >= 55 and bullish_points > bearish_points:
        return "BULLISH", min(bullish_points, 100), reasons

    if bearish_points >= 55 and bearish_points > bullish_points:
        return "BEARISH", min(bearish_points, 100), reasons

    return "NEUTRAL", max(bullish_points, bearish_points), reasons or ["index structure not clean enough"]


def breakout_level(option_candles: List[Any]) -> Optional[float]:
    if len(option_candles) < 4:
        return None
    # breakout level is prior swing high of option premium
    return recent_swing_high(option_candles[:-1], lookback=8)


def is_breakout_confirmed(option_candles: List[Any]) -> bool:
    if len(option_candles) < 4:
        return False
    last = as_dict(option_candles[-1])
    level = breakout_level(option_candles)
    if level is None:
        return False
    return last["close"] > level


def is_retest_hold(option_candles: List[Any]) -> bool:
    """
    Book rule:
    Breakout alone is not enough.
    Premium should retest/throwback and hold breakout/support area.
    """
    if len(option_candles) < 5:
        return False

    data = [as_dict(c) for c in option_candles]
    level = breakout_level(data[:-1]) or breakout_level(data)
    if level is None:
        return False

    last = data[-1]
    prev = data[-2]

    # Retest hold: wick touches/comes near breakout level, close accepts above it
    tolerance = max(level * 0.004, 0.5)
    touched = last["low"] <= level + tolerance or prev["low"] <= level + tolerance
    accepted = last["close"] > level and candle_direction(last) == "BULLISH"
    return bool(touched and accepted)


def detect_liquidity_trap(option_candles: List[Any]) -> bool:
    if len(option_candles) < 4:
        return False

    data = [as_dict(c) for c in option_candles]
    level = breakout_level(data)
    if level is None:
        return False

    last = data[-1]
    # False breakout: high crosses breakout, close falls back below breakout
    if last["high"] > level and last["close"] < level:
        return True

    # Day high rejection trap
    dh = day_high(data[:-1])
    if dh is not None:
        upper_rejection = last["high"] >= dh and last["close"] < (last["open"] + last["high"]) / 2
        weak = candle_quality(last) == "WEAK" or detect_candlestick_pattern(data) in ["shooting_star", "doji"]
        if upper_rejection and weak:
            return True

    return False


def option_premium_alignment(
    option_candles: List[Any],
    required_side: Side,
) -> Tuple[bool, List[str], int]:
    """
    Option buying alignment:
    - Bullish setup requires ATM/near-ATM CE premium strength.
    - Bearish setup requires ATM/near-ATM PE premium strength.
    - Reject day-low/decay mode.
    """
    reasons: List[str] = []
    score = 0

    if len(option_candles) < MIN_CANDLES:
        return False, ["not enough option candles"], score

    data = [as_dict(c) for c in option_candles]
    last = data[-1]
    closes = [d["close"] for d in data]
    q = candle_quality(last)
    pattern = detect_candlestick_pattern(data)
    vw = vwap(data)
    dh = day_high(data)
    dl = day_low(data)

    if candle_direction(last) == "BULLISH" and q in ["A_PLUS", "A", "B"]:
        score += 20
        reasons.append(f"{required_side} premium bullish candle quality {q}")
    else:
        reasons.append(f"{required_side} premium candle not strong")

    if pattern in [
        "bullish_engulfing",
        "hammer",
        "inverted_hammer",
        "piercing_line",
        "bullish_marubozu",
    ]:
        score += 15
        reasons.append(f"{required_side} premium bullish candle pattern: {pattern}")

    if vw is not None and last["close"] > vw:
        score += 15
        reasons.append(f"{required_side} premium above VWAP")

    if is_breakout_confirmed(data):
        score += 20
        reasons.append(f"{required_side} premium breakout confirmed")

    if is_retest_hold(data):
        score += 25
        reasons.append(f"{required_side} premium retest/throwback hold confirmed")

    # Avoid day-high chase if no retest
    if dh is not None and last["close"] >= dh * 0.995 and not is_retest_hold(data):
        reasons.append(f"{required_side} premium near day high without retest hold")
        score -= 25

    # Reject decay / day-low area
    if dl is not None and last["close"] <= dl * 1.02:
        reasons.append(f"{required_side} premium near day low / decay mode")
        score -= 30

    # EMA premium momentum
    e9 = ema(closes, min(9, len(closes)))
    e21 = ema(closes, min(21, len(closes)))
    if e9 is not None and e21 is not None and e9 > e21:
        score += 10
        reasons.append(f"{required_side} premium EMA momentum positive")

    aligned = score >= 55
    return aligned, reasons, max(0, min(score, 100))


def calculate_structure_risk(option_candles: List[Any], entry: float) -> Dict[str, Any]:
    swing_low = recent_swing_low(option_candles[:-1], lookback=8)
    if swing_low is None:
        return {"rr_valid": False, "reason": "missing swing low for structure SL"}

    buffer_value = max(entry * DEFAULT_BUFFER_PCT, 0.5)
    sl = round(swing_low - buffer_value, 2)
    risk = round(entry - sl, 2)

    if risk <= 0:
        return {"rr_valid": False, "reason": "invalid structure SL"}

    risk_pct = round((risk / entry) * 100, 2)

    return {
        "entry": round(entry, 2),
        "structure_sl": sl,
        "risk_points": risk,
        "risk_pct": risk_pct,
        "targets": {
            "t1": round(entry + risk * 1.5, 2),
            "t2": round(entry + risk * 2.0, 2),
            "t3": round(entry + risk * 3.0, 2),
        },
        "rr_valid": True,
    }


def riga_book_filter(signal: Dict[str, Any]) -> Dict[str, Any]:
    reject_reasons: List[str] = []

    if signal.get("index_bias") not in ["BULLISH", "BEARISH"]:
        reject_reasons.append("index bias not clean")

    if not signal.get("breakout_confirmed"):
        reject_reasons.append("pattern not activated / breakout not confirmed")

    if not signal.get("retest_hold"):
        reject_reasons.append("no retest hold")

    if signal.get("trap_detected"):
        reject_reasons.append("liquidity trap / failed breakout detected")

    if signal.get("candle_quality") in ["WEAK", None]:
        reject_reasons.append("weak option candle quality")

    if signal.get("candle_pattern") in ["doji", "shooting_star", "hanging_man"]:
        reject_reasons.append(f"option candle pattern against buying strength: {signal.get('candle_pattern')}")

    if not signal.get("option_alignment"):
        reject_reasons.append("option premium not aligned")

    if not signal.get("rr_valid"):
        reject_reasons.append("RR invalid")

    if signal.get("risk_pct", 999) > MAX_RISK_PCT:
        reject_reasons.append("SL too wide")

    if signal.get("confidence", 0) < CONFIDENCE_MIN:
        reject_reasons.append("confidence below 70")

    if reject_reasons:
        return {
            "decision": "NO_TRADE",
            "passed": False,
            "reasons": reject_reasons,
        }

    return {
        "decision": signal.get("option_trade"),
        "passed": True,
        "reasons": ["book knowledge filter passed"],
    }


def analyze_option_candidate(
    index_name: str,
    index_bias: Bias,
    index_score: int,
    index_reasons: List[str],
    option: Any,
) -> Dict[str, Any]:
    option_data = option.model_dump() if hasattr(option, "model_dump") else option.dict() if hasattr(option, "dict") else option
    opt_type: Side = option_data["type"]
    candles = option_data["candles"]
    data = [as_dict(c) for c in candles]

    required_side: Optional[Side] = None
    option_trade: Decision = "NO_TRADE"

    if index_bias == "BULLISH":
        required_side = "CE"
        option_trade = "BUY_CE"
    elif index_bias == "BEARISH":
        required_side = "PE"
        option_trade = "BUY_PE"

    if required_side is None or opt_type != required_side:
        return {
            "index": index_name,
            "symbol": option_data.get("symbol"),
            "strike": option_data.get("strike"),
            "type": opt_type,
            "decision": "NO_TRADE",
            "confidence": 0,
            "reject_reasons": [f"wrong option side for {index_bias} bias"],
        }

    last = data[-1]
    entry = float(option_data.get("ltp") or last["close"])
    risk = calculate_structure_risk(data, entry)
    aligned, alignment_reasons, alignment_score = option_premium_alignment(data, required_side)

    breakout = is_breakout_confirmed(data)
    retest = is_retest_hold(data)
    trap = detect_liquidity_trap(data)
    c_quality = candle_quality(last)
    c_pattern = detect_candlestick_pattern(data)

    confidence = int(
        min(
            100,
            max(
                0,
                index_score * 0.35
                + alignment_score * 0.45
                + (15 if breakout else 0)
                + (15 if retest else 0)
                - (25 if trap else 0)
                - (15 if c_quality == "WEAK" else 0)
            ),
        )
    )

    signal = {
        "index": index_name,
        "index_bias": index_bias,
        "option_trade": option_trade,
        "symbol": option_data.get("symbol"),
        "strike": option_data.get("strike"),
        "type": opt_type,
        "expiry": option_data.get("expiry"),
        "entry": safe_round(entry),
        "stop_loss": risk.get("structure_sl"),
        "target": risk.get("targets", {}).get("t2"),
        "targets": risk.get("targets"),
        "risk_points": risk.get("risk_points"),
        "risk_pct": risk.get("risk_pct", 999),
        "rr_valid": risk.get("rr_valid", False),
        "breakout_confirmed": breakout,
        "retest_hold": retest,
        "trap_detected": trap,
        "candle_pattern": c_pattern,
        "candle_quality": c_quality,
        "option_alignment": aligned,
        "confidence": confidence,
        "reason": ", ".join(index_reasons + alignment_reasons),
    }

    filter_result = riga_book_filter(signal)

    if not filter_result["passed"]:
        signal["decision"] = "NO_TRADE"
        signal["reject_reasons"] = filter_result["reasons"]
    else:
        signal["decision"] = option_trade
        signal["reject_reasons"] = []

    signal["book_filter"] = {
        "pattern_status": "retest" if retest else "breakout" if breakout else "forming",
        "breakout_confirmed": breakout,
        "retest_hold": retest,
        "trap_detected": trap,
        "candle_pattern": c_pattern,
        "candle_quality": c_quality,
        "option_alignment": aligned,
        "sl_valid": bool(risk.get("rr_valid")) and signal["risk_pct"] <= MAX_RISK_PCT,
        "rr_valid": risk.get("rr_valid", False),
        "final_decision": signal["decision"],
        "filter_reasons": filter_result["reasons"],
    }

    return signal


def choose_best_trade(signals: List[Dict[str, Any]]) -> Dict[str, Any]:
    valid = [s for s in signals if s.get("decision") in ["BUY_CE", "BUY_PE"]]
    if not valid:
        return {
            "best_trade": "NO TRADE",
            "trade_count": 0,
            "signals": signals,
        }
    best = sorted(valid, key=lambda x: (x.get("confidence", 0), -x.get("risk_pct", 999)), reverse=True)[0]
    return {
        "best_trade": best,
        "trade_count": len(valid),
        "signals": signals,
    }


def scan_payload(payload: Any) -> Dict[str, Any]:
    data = payload.model_dump() if hasattr(payload, "model_dump") else payload.dict() if hasattr(payload, "dict") else payload
    index_candles = data["index_candles"]

    index_bias, index_score, index_reasons = detect_index_bias(index_candles)

    signals = []
    for option in data["options"]:
        signals.append(
            analyze_option_candidate(
                index_name=data["index"],
                index_bias=index_bias,
                index_score=index_score,
                index_reasons=index_reasons,
                option=option,
            )
        )

    result = choose_best_trade(signals)
    result.update(
        {
            "index": data["index"],
            "spot_ltp": data.get("spot_ltp"),
            "atm": data.get("atm"),
            "index_bias": {
                "bias": index_bias,
                "score": index_score,
                "reason": ", ".join(index_reasons),
            },
        }
    )
    return result


# -----------------------------
# Live data hook
# -----------------------------

# -----------------------------
# Live Angel One SmartAPI Adapter
# -----------------------------

INDEX_CONFIG = {
    "NIFTY": {
        "spot_symbol": "NIFTY 50",
        "spot_exchange": "NSE",
        "spot_token": "26000",
        "option_exchange": "NFO",
        "name_keywords": ["NIFTY"],
        "step": 50,
    },
    "BANKNIFTY": {
        "spot_symbol": "NIFTY BANK",
        "spot_exchange": "NSE",
        "spot_token": "26009",
        "option_exchange": "NFO",
        "name_keywords": ["BANKNIFTY", "BANK NIFTY"],
        "step": 100,
    },
    "FINNIFTY": {
        "spot_symbol": "NIFTY FIN SERVICE",
        "spot_exchange": "NSE",
        "spot_token": "26037",
        "option_exchange": "NFO",
        "name_keywords": ["FINNIFTY", "NIFTY FIN SERVICE"],
        "step": 50,
    },
    "SENSEX": {
        "spot_symbol": "SENSEX",
        "spot_exchange": "BSE",
        "spot_token": "1",
        "option_exchange": "BFO",
        "name_keywords": ["SENSEX"],
        "step": 100,
    },
}

ANGEL_HISTORICAL_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData"
ANGEL_QUOTE_URL = "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/"
ANGEL_LOGIN_URL = "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword"
ANGEL_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SCRIP_CACHE_FILE = "/tmp/riga_angel_scrip_master.json"
SCRIP_CACHE_TTL_SECONDS = 6 * 60 * 60


def _generate_totp(secret: str, interval: int = 30, digits: int = 6) -> str:
    """
    Generates TOTP without external dependency.
    ANGEL_TOTP_SECRET should be the base32 secret from authenticator setup.
    """
    if not secret:
        raise RuntimeError("ANGEL_TOTP_SECRET missing. Set it in environment variables.")

    normalized_secret = secret.replace(" ", "").upper()
    # Base32 strings sometimes come from env without "=" padding.
    # Python's b32decode requires correct padding length.
    missing_padding = len(normalized_secret) % 8
    if missing_padding:
        normalized_secret += "=" * (8 - missing_padding)

    key = base64.b32decode(normalized_secret, casefold=True)
    counter = int(time.time() // interval)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def _base_headers() -> Dict[str, str]:
    api_key = os.getenv("ANGEL_API_KEY", "").strip()
    client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()

    if not api_key:
        raise RuntimeError("ANGEL_API_KEY missing. Set it in Render environment variables.")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-PrivateKey": api_key,
        "X-SourceID": "WEB",
        "X-ClientLocalIP": os.getenv("ANGEL_CLIENT_LOCAL_IP", "127.0.0.1"),
        "X-ClientPublicIP": os.getenv("ANGEL_CLIENT_PUBLIC_IP", "127.0.0.1"),
        "X-MACAddress": os.getenv("ANGEL_MAC_ADDRESS", "00:00:00:00:00:00"),
        "X-UserType": "USER",
    }

    if client_code:
        headers["X-ClientCode"] = client_code

    return headers


def _login_angel_and_get_jwt() -> str:
    """
    Logs into Angel One SmartAPI using env credentials and returns JWT.
    Required Render env:
    - ANGEL_API_KEY
    - ANGEL_CLIENT_CODE
    - ANGEL_PASSWORD
    - ANGEL_TOTP_SECRET
    """
    client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()
    password = os.getenv("ANGEL_PASSWORD", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    if not client_code:
        raise RuntimeError("ANGEL_CLIENT_CODE missing.")
    if not password:
        raise RuntimeError("ANGEL_PASSWORD missing.")
    if not totp_secret:
        raise RuntimeError("ANGEL_TOTP_SECRET missing.")

    payload = {
        "clientcode": client_code,
        "password": password,
        "totp": _generate_totp(totp_secret),
    }

    response = requests.post(
        ANGEL_LOGIN_URL,
        headers=_base_headers(),
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    body = response.json()

    if not body.get("status"):
        raise RuntimeError(f"Angel login failed: {body}")

    data = body.get("data") or {}
    jwt = data.get("jwtToken") or data.get("jwt_token") or data.get("token")

    if not jwt:
        raise RuntimeError(f"Angel login response missing jwtToken: {body}")

    return str(jwt)


def _resolve_jwt_token(token: str) -> str:
    """
    Supports two modes:
    1. token == RIGA_ACTION_TOKEN, e.g. Krushna123:
       backend logs in to Angel using Render env vars and generates JWT.
    2. token is already Angel JWT:
       use it directly.
    """
    incoming = (token or "").strip()
    action_token = os.getenv("RIGA_ACTION_TOKEN", "").strip()

    if action_token and incoming == action_token:
        return _login_angel_and_get_jwt()

    env_jwt = os.getenv("ANGEL_JWT_TOKEN", "").strip()
    if not incoming and env_jwt:
        return env_jwt

    if incoming:
        return incoming

    raise RuntimeError("Token missing. Pass RIGA_ACTION_TOKEN or ANGEL JWT token.")


def _angel_headers(jwt_token: str) -> Dict[str, str]:
    headers = _base_headers()
    headers["Authorization"] = f"Bearer {jwt_token}"
    return headers

def _load_scrip_master() -> List[Dict[str, Any]]:
    """
    Downloads and caches Angel One OpenAPI scrip master.
    This is required to map ATM/near-ATM option symbols to symbol tokens.
    """
    cache = Path(SCRIP_CACHE_FILE)

    if cache.exists() and time.time() - cache.stat().st_mtime < SCRIP_CACHE_TTL_SECONDS:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            pass

    response = requests.get(ANGEL_SCRIP_MASTER_URL, timeout=20)
    response.raise_for_status()
    data = response.json()
    cache.write_text(json.dumps(data), encoding="utf-8")
    return data


def _parse_expiry(value: Any) -> Optional[datetime]:
    if value is None:
        return None

    s = str(value).strip().upper()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d%b%y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _normalize_strike(raw: Any) -> Optional[float]:
    """
    Angel master sometimes stores strikes scaled by 100.
    This normalizer handles both formats.
    """
    try:
        value = float(raw)
    except Exception:
        return None

    if value > 100000:
        value = value / 100.0

    return value


def _round_to_step(value: float, step: int) -> float:
    return round(value / step) * step


def _fetch_candles(
    exchange: str,
    token: str,
    jwt_token: str,
    interval: str = "ONE_MINUTE",
    lookback_minutes: int = 90,
) -> List[Dict[str, Any]]:
    """
    Fetches live historical candles from Angel One SmartAPI.
    Output candle format:
    [time, open, high, low, close, volume]
    """
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(minutes=lookback_minutes)

    payload = {
        "exchange": exchange,
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
    }

    response = requests.post(
        ANGEL_HISTORICAL_URL,
        headers=_angel_headers(jwt_token),
        json=payload,
        timeout=20,
    )

    response.raise_for_status()
    body = response.json()

    if not body.get("status"):
        raise RuntimeError(f"Angel candle API error: {body}")

    rows = body.get("data") or []
    candles: List[Dict[str, Any]] = []

    for row in rows:
        if len(row) < 6:
            continue
        candles.append(
            {
                "time": str(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5] or 0),
            }
        )

    return candles


def _find_option_candidates(
    index: str,
    atm: float,
    strikes_around: int,
) -> List[Dict[str, Any]]:
    """
    Finds ATM/near-ATM CE/PE option contracts from Angel scrip master.
    Far OTM strikes are intentionally ignored as per RIGA rule.
    """
    cfg = INDEX_CONFIG[index]
    master = _load_scrip_master()

    today = datetime.now().date()
    wanted_strikes = {
        float(atm + offset * cfg["step"])
        for offset in range(-strikes_around, strikes_around + 1)
    }

    rows: List[Dict[str, Any]] = []
    for item in master:
        exch_seg = str(item.get("exch_seg", item.get("exchange", ""))).upper()
        symbol = str(item.get("symbol", item.get("tradingsymbol", ""))).upper()
        name = str(item.get("name", "")).upper()
        instrumenttype = str(item.get("instrumenttype", "")).upper()

        if exch_seg != cfg["option_exchange"]:
            continue

        if not any(k.replace(" ", "") in symbol.replace(" ", "") or k in name for k in cfg["name_keywords"]):
            continue

        if "OPT" not in instrumenttype and not (symbol.endswith("CE") or symbol.endswith("PE")):
            continue

        strike = _normalize_strike(item.get("strike"))
        if strike is None:
            continue

        if strike not in wanted_strikes:
            continue

        opt_type = "CE" if symbol.endswith("CE") else "PE" if symbol.endswith("PE") else None
        if opt_type not in ["CE", "PE"]:
            continue

        expiry_dt = _parse_expiry(item.get("expiry"))
        if expiry_dt is None or expiry_dt.date() < today:
            continue

        rows.append(
            {
                "symbol": symbol,
                "token": str(item.get("token", item.get("symboltoken", ""))),
                "strike": strike,
                "type": opt_type,
                "expiry_dt": expiry_dt,
                "expiry": expiry_dt.strftime("%d%b%Y").upper(),
                "exchange": exch_seg,
            }
        )

    if not rows:
        raise RuntimeError(f"No ATM/near-ATM option contracts found for {index} ATM {atm}")

    nearest_expiry = min(r["expiry_dt"] for r in rows)
    rows = [r for r in rows if r["expiry_dt"] == nearest_expiry]

    rows.sort(key=lambda r: (abs(r["strike"] - atm), 0 if r["type"] == "CE" else 1))
    return rows


def _index_spot_from_quote(index: str, jwt_token: str) -> Optional[float]:
    """
    Tries Angel quote API for index spot.
    If quote API fails, caller can fallback to last candle close.
    """
    cfg = INDEX_CONFIG[index]
    payload = {
        "mode": "LTP",
        "exchangeTokens": {
            cfg["spot_exchange"]: [str(cfg["spot_token"])]
        },
    }

    try:
        response = requests.post(
            ANGEL_QUOTE_URL,
            headers=_angel_headers(jwt_token),
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        body = response.json()
        data = body.get("data", {})
        fetched = data.get("fetched") or []
        if fetched:
            ltp = fetched[0].get("ltp")
            if ltp is not None:
                return float(ltp)
    except Exception:
        return None

    return None


def fetch_market_data(index: str, token: str, strikes_around: int = 2) -> ScanPayload:
    """
    LIVE DATA CONNECTED:
    Uses Angel One SmartAPI to fetch:
    - live index candles
    - live ATM/near-ATM CE/PE option premium candles

    Requirements:
    Environment variables:
    - ANGEL_API_KEY
    Optional:
    - ANGEL_CLIENT_CODE
    - ANGEL_JWT_TOKEN

    Usage:
    /scan-live/NIFTY?token=<RIGA_ACTION_TOKEN>&strikes_around=2

    Notes:
    - This does NOT use far OTM options.
    - This returns raw candles, so RIGA book filter can check breakout, retest, traps, candle quality, SL, and RR.
    """
    index = index.upper().strip()
    if index not in INDEX_CONFIG:
        raise RuntimeError(f"Unsupported index: {index}. Use NIFTY, BANKNIFTY, FINNIFTY, or SENSEX.")

    jwt_token = _resolve_jwt_token(token)
    cfg = INDEX_CONFIG[index]

    interval = os.getenv("RIGA_CANDLE_INTERVAL", "ONE_MINUTE")
    lookback_minutes = int(os.getenv("RIGA_LOOKBACK_MINUTES", "90"))

    index_candles = _fetch_candles(
        exchange=cfg["spot_exchange"],
        token=cfg["spot_token"],
        jwt_token=jwt_token,
        interval=interval,
        lookback_minutes=lookback_minutes,
    )

    if len(index_candles) < MIN_CANDLES:
        raise RuntimeError(f"Not enough index candles fetched for {index}")

    spot_ltp = _index_spot_from_quote(index, jwt_token)
    if spot_ltp is None:
        spot_ltp = index_candles[-1]["close"]

    atm = _round_to_step(float(spot_ltp), int(cfg["step"]))
    candidates = _find_option_candidates(index=index, atm=atm, strikes_around=strikes_around)

    options: List[OptionCandidate] = []
    for c in candidates:
        option_candles = _fetch_candles(
            exchange=c["exchange"],
            token=c["token"],
            jwt_token=jwt_token,
            interval=interval,
            lookback_minutes=lookback_minutes,
        )

        if len(option_candles) < MIN_CANDLES:
            continue

        options.append(
            OptionCandidate(
                symbol=c["symbol"],
                strike=c["strike"],
                type=c["type"],
                expiry=c["expiry"],
                candles=[Candle(**x) for x in option_candles],
                ltp=option_candles[-1]["close"],
            )
        )

    if not options:
        raise RuntimeError(f"No option premium candles available for {index}")

    return ScanPayload(
        index=index,
        spot_ltp=float(spot_ltp),
        atm=float(atm),
        index_candles=[Candle(**x) for x in index_candles],
        options=options,
        strikes_around=strikes_around,
    )


# -----------------------------
# FastAPI app
# -----------------------------

if FastAPI is not None:
    app = FastAPI(title="RIGA AI Option Buying Scanner", version="2.0")

    @app.get("/")
    def root() -> Dict[str, str]:
        return {
            "name": "RIGA AI Option Buying Scanner",
            "status": "ok",
            "rule": "Bullish -> BUY CE, Bearish -> BUY PE, otherwise NO TRADE",
        }

    @app.post("/scan")
    def scan(payload: ScanPayload) -> Dict[str, Any]:
        try:
            return scan_payload(payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/scan-live/{index}")
    def scan_live(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        try:
            payload = fetch_market_data(index=index, token=token, strikes_around=strikes_around)
            return scan_payload(payload)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "server_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "routes": [
                "/scan-live/{index}",
                "/scanOptions",
                "/getOptionChain",
                "/scanAllMarkets",
                "/getSpotPrice",
            ],
        }

    @app.get("/scan-live/{index}")
    def scan_live_get(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        try:
            payload = fetch_market_data(index=index, token=token, strikes_around=strikes_around)
            return scan_payload(payload)
        except NotImplementedError as exc:
            raise HTTPException(status_code=501, detail=str(exc))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/getSpotPrice")
    def get_spot_price(index: str, token: str) -> Dict[str, Any]:
        try:
            index = index.upper().strip()
            if index not in INDEX_CONFIG:
                raise RuntimeError(f"Unsupported index: {index}")
            jwt_token = _resolve_jwt_token(token)
            spot = _index_spot_from_quote(index, jwt_token)
            if spot is None:
                cfg = INDEX_CONFIG[index]
                candles = _fetch_candles(
                    exchange=cfg["spot_exchange"],
                    token=cfg["spot_token"],
                    jwt_token=jwt_token,
                    interval=os.getenv("RIGA_CANDLE_INTERVAL", "ONE_MINUTE"),
                    lookback_minutes=20,
                )
                if not candles:
                    raise RuntimeError("No candles available for fallback spot")
                spot = candles[-1]["close"]
            return {"index": index, "spot_ltp": float(spot)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/getSpotPrice")
    def get_spot_price_get(index: str, token: str) -> Dict[str, Any]:
        return get_spot_price(index=index, token=token)

    @app.post("/getOptionChain")
    def get_option_chain(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        try:
            index = index.upper().strip()
            if index not in INDEX_CONFIG:
                raise RuntimeError(f"Unsupported index: {index}")
            jwt_token = _resolve_jwt_token(token)
            spot = _index_spot_from_quote(index, jwt_token)
            if spot is None:
                cfg = INDEX_CONFIG[index]
                candles = _fetch_candles(
                    exchange=cfg["spot_exchange"],
                    token=cfg["spot_token"],
                    jwt_token=jwt_token,
                    interval=os.getenv("RIGA_CANDLE_INTERVAL", "ONE_MINUTE"),
                    lookback_minutes=20,
                )
                if not candles:
                    raise RuntimeError("No candles available for fallback spot")
                spot = candles[-1]["close"]

            atm = _round_to_step(float(spot), int(INDEX_CONFIG[index]["step"]))
            candidates = _find_option_candidates(index=index, atm=atm, strikes_around=strikes_around)
            options = [
                {
                    "exchange": c["exchange"],
                    "tradingsymbol": c["symbol"],
                    "symboltoken": c["token"],
                    "strike": c["strike"],
                    "type": c["type"],
                    "expiry": c["expiry"],
                }
                for c in candidates
            ]
            return {
                "index": index,
                "spot": {"ltp": float(spot)},
                "atm": float(atm),
                "nearest_expiry": options[0]["expiry"] if options else None,
                "options_count": len(options),
                "options": options,
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/getOptionChain")
    def get_option_chain_get(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        return get_option_chain(index=index, token=token, strikes_around=strikes_around)

    @app.post("/scanOptions")
    def scan_options(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        try:
            payload = fetch_market_data(index=index, token=token, strikes_around=strikes_around)
            result = scan_payload(payload)
            best = result.get("best_trade")
            return {
                "index": result.get("index"),
                "spot_ltp": result.get("spot_ltp"),
                "atm": result.get("atm"),
                "index_bias": result.get("index_bias"),
                "trade_count": result.get("trade_count", 0),
                "best_trade": best,
                "trades": [
                    s for s in result.get("signals", [])
                    if s.get("decision") in ["BUY_CE", "BUY_PE"]
                ],
                "all_signals": result.get("signals", []),
                "formatted": format_riga_output(result),
            }
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/scanOptions")
    def scan_options_get(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.post("/scanAllMarkets")
    def scan_all_markets(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        results: Dict[str, Any] = {}
        valid_trades: List[Dict[str, Any]] = []

        for idx in ["NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"]:
            try:
                res = scan_options(index=idx, token=token, strikes_around=strikes_around)
                results[idx] = res
                best = res.get("best_trade")
                if isinstance(best, dict) and best.get("decision") in ["BUY_CE", "BUY_PE"]:
                    valid_trades.append(best)
            except Exception as exc:
                results[idx] = {"index": idx, "best_trade": "NO TRADE", "error": str(exc)}

        if not valid_trades:
            return {
                "best_trade": "NO TRADE",
                "trade_count": 0,
                "results": results,
            }

        best = sorted(
            valid_trades,
            key=lambda x: (x.get("confidence", 0), -x.get("risk_pct", 999)),
            reverse=True,
        )[0]

        return {
            "best_trade": best,
            "trade_count": len(valid_trades),
            "results": results,
        }

    @app.get("/scanAllMarkets")
    def scan_all_markets_get(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    # Extra compatibility aliases for action/plugin path naming
    @app.post("/scan-options")
    def scan_options_dash(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.get("/scan-options")
    def scan_options_dash_get(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.post("/scan_options")
    def scan_options_snake(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.get("/scan_options")
    def scan_options_snake_get(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.post("/scan/options")
    def scan_options_slash(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.get("/scan/options")
    def scan_options_slash_get(index: str, token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_options(index=index, token=token, strikes_around=strikes_around)

    @app.post("/scan-all-markets")
    def scan_all_markets_dash(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.get("/scan-all-markets")
    def scan_all_markets_dash_get(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.post("/scan_all_markets")
    def scan_all_markets_snake(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.get("/scan_all_markets")
    def scan_all_markets_snake_get(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.post("/scanallmarkets")
    def scan_all_markets_lower(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.get("/scanallmarkets")
    def scan_all_markets_lower_get(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.post("/scan-all")
    def scan_all_short(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.get("/scan-all")
    def scan_all_short_get(token: str, strikes_around: int = 2) -> Dict[str, Any]:
        return scan_all_markets(token=token, strikes_around=strikes_around)

    @app.post("/option-chain")
    def get_option_chain_dash(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        return get_option_chain(index=index, token=token, strikes_around=strikes_around)

    @app.get("/option-chain")
    def get_option_chain_dash_get(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        return get_option_chain(index=index, token=token, strikes_around=strikes_around)

    @app.post("/option_chain")
    def get_option_chain_snake(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        return get_option_chain(index=index, token=token, strikes_around=strikes_around)

    @app.get("/option_chain")
    def get_option_chain_snake_get(index: str, token: str, strikes_around: int = 3) -> Dict[str, Any]:
        return get_option_chain(index=index, token=token, strikes_around=strikes_around)

    @app.post("/spot-price")
    def get_spot_price_dash(index: str, token: str) -> Dict[str, Any]:
        return get_spot_price(index=index, token=token)

    @app.get("/spot-price")
    def get_spot_price_dash_get(index: str, token: str) -> Dict[str, Any]:
        return get_spot_price(index=index, token=token)

    @app.post("/spot_price")
    def get_spot_price_snake(index: str, token: str) -> Dict[str, Any]:
        return get_spot_price(index=index, token=token)

    @app.get("/spot_price")
    def get_spot_price_snake_get(index: str, token: str) -> Dict[str, Any]:
        return get_spot_price(index=index, token=token)


# -----------------------------
# Final output formatter
# -----------------------------

def format_riga_output(scan_result: Dict[str, Any]) -> str:
    best = scan_result.get("best_trade")
    if best == "NO TRADE" or not isinstance(best, dict):
        return "NO TRADE"

    bias = "Bullish" if best["index_bias"] == "BULLISH" else "Bearish"
    option_trade = "BUY CE" if best["decision"] == "BUY_CE" else "BUY PE"

    return (
        f"Market:\n"
        f"Bias: {bias}\n"
        f"Option Trade: {option_trade}\n"
        f"Strike: {best.get('strike')}\n"
        f"Entry: {best.get('entry')}\n"
        f"Stop Loss: {best.get('stop_loss')}\n"
        f"Target: {best.get('targets')}\n"
        f"Confidence: {best.get('confidence')}%\n"
        f"Reason: {best.get('reason')}"
    )


if __name__ == "__main__":
    print("RIGA AI main.py loaded. Run with: uvicorn main:app --reload")
