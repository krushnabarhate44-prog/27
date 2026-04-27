# RIGA Real Scanner Backend v3.1

Connects to Angel One and scans a watchlist using live LTP + day OHLC.

Endpoints:
- /ltp
- /riga-signal-demo
- /scan-default-watchlist
- /scan-custom-watchlist
- /option-chain-status
- /auto-alert-info

Render Build:
pip install -r requirements.txt

Render Start:
uvicorn main:app --host 0.0.0.0 --port $PORT

Keep existing env variables:
ANGEL_API_KEY
ANGEL_CLIENT_CODE
ANGEL_PASSWORD
ANGEL_TOTP_SECRET
RIGA_ACTION_TOKEN
