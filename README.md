# RIGA Direct Trade Backend v4

Read-only direct trade signal backend:
- /ltp
- /direct-trade-signal
- /direct-trade-scan
- /direct-trade-custom-scan

No order placement. It returns BUY / SELL / NO TRADE only.

Render commands:
Build: pip install -r requirements.txt
Start: uvicorn main:app --host 0.0.0.0 --port $PORT
