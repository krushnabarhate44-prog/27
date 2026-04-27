import os
from typing import Optional

import pyotp
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from SmartApi import SmartConnect

load_dotenv()

app = FastAPI(
    title="RIGA Angel One Backend",
    version="2.0.0",
    description="Clean read-only backend for RIGA Custom GPT + Angel One SmartAPI."
)

ANGEL_API_KEY = os.getenv("ANGEL_API_KEY")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET")
RIGA_ACTION_TOKEN = os.getenv("RIGA_ACTION_TOKEN", "")

_smart_api: Optional[SmartConnect] = None


def check_action_token(authorization: Optional[str]) -> None:
    if not RIGA_ACTION_TOKEN:
        return
    expected = f"Bearer {RIGA_ACTION_TOKEN}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


def angel_client() -> SmartConnect:
    global _smart_api

    if _smart_api is not None:
        return _smart_api

    missing = []
    for key, value in {
        "ANGEL_API_KEY": ANGEL_API_KEY,
        "ANGEL_CLIENT_CODE": ANGEL_CLIENT_CODE,
        "ANGEL_PASSWORD": ANGEL_PASSWORD,
        "ANGEL_TOTP_SECRET": ANGEL_TOTP_SECRET,
    }.items():
        if not value:
            missing.append(key)

    if missing:
        raise HTTPException(
            status_code=500,
            detail=f"Missing environment variables: {', '.join(missing)}"
        )

    try:
        client = SmartConnect(api_key=ANGEL_API_KEY)
        totp = pyotp.TOTP(ANGEL_TOTP_SECRET).now()

        session = client.generateSession(
            ANGEL_CLIENT_CODE,
            ANGEL_PASSWORD,
            totp
        )

        if not session or not session.get("status"):
            raise HTTPException(
                status_code=500,
                detail={"message": "Angel One login failed", "response": session}
            )

        _smart_api = client
        return client

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Angel One login error: {str(exc)}")


@app.get("/")
def root():
    return {
        "name": "RIGA Angel One Backend",
        "version": "2.0.0",
        "mode": "read-only",
        "status": "running"
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ltp")
def get_ltp(
    exchange: str = Query(..., description="Exchange: NSE, NFO, BSE, MCX etc."),
    tradingsymbol: str = Query(..., description="Example: SBIN-EQ"),
    symboltoken: str = Query(..., description="Angel One symbol token"),
    authorization: Optional[str] = Header(default=None)
):
    check_action_token(authorization)
    client = angel_client()

    try:
        response = client.ltpData(exchange, tradingsymbol, symboltoken)

        if not response or not response.get("status"):
            raise HTTPException(
                status_code=502,
                detail={"message": "Angel One LTP failed", "response": response}
            )

        data = response.get("data", {})

        return {
            "status": "success",
            "exchange": exchange,
            "tradingsymbol": tradingsymbol,
            "symboltoken": symboltoken,
            "ltp": data.get("ltp"),
            "open": data.get("open"),
            "high": data.get("high"),
            "low": data.get("low"),
            "close": data.get("close"),
            "raw": response
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LTP fetch error: {str(exc)}")


@app.get("/riga-signal-demo")
def riga_signal_demo(
    exchange: str = Query(...),
    tradingsymbol: str = Query(...),
    symboltoken: str = Query(...),
    authorization: Optional[str] = Header(default=None)
):
    check_action_token(authorization)
    client = angel_client()

    try:
        response = client.ltpData(exchange, tradingsymbol, symboltoken)
        ltp = None

        if response and response.get("status"):
            ltp = response.get("data", {}).get("ltp")

        return {
            "status": "success",
            "symbol": tradingsymbol,
            "ltp": ltp,
            "market_bias": "NO TRADE",
            "entry": None,
            "stop_loss": None,
            "target": None,
            "reason": "Demo mode only. Full RIGA logic not connected yet."
        }

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
