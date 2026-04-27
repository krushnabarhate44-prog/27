# RIGA REAL AI Backend v5 🚀

Advanced pattern-based trading system built by Krushna.

-------------------------------------

## 🔥 FEATURES

- Pattern + Trend + Level + Momentum logic
- Only high probability trades (>=70%)
- Strict RIGA discipline rules
- Real-time LTP via Angel One SmartAPI
- Sniper entry system (Breakout + Retest)

-------------------------------------

## 📡 API ENDPOINTS

- `/health` → Check backend status
- `/ltp` → Get live market price
- `/real-riga-signal` → Get BUY / SELL / NO TRADE signal
- `/real-riga-scan` → Scan multiple stocks for sniper trades

-------------------------------------

## ⚠️ RULES

- Trade only if probability >= 70%
- If not → return "NO TRADE"
- Avoid fake breakouts
- Entry only after candle close
- Prefer retest entries

-------------------------------------

## ⚙️ DEPLOYMENT

Build:
pip install -r requirements.txt

Start:
uvicorn main:app --host 0.0.0.0 --port $PORT

-------------------------------------

## 🎯 GOAL

Sniper trading system:
- Wait patiently
- Avoid noise
- Enter only high probability trades
- Maximize accuracy

-------------------------------------

## ⚠️ NOTE

- No auto order placement (signal only)
- Uses Angel One API
- Requires valid API credentials

-------------------------------------
