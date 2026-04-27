# RIGA Angel One Backend Clean

Clean read-only backend for RIGA Custom GPT.

## Render Settings

Build Command:
```bash
pip install -r requirements.txt
```

Start Command:
```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Environment Variables on Render

Add:
```text
ANGEL_API_KEY
ANGEL_CLIENT_CODE
ANGEL_PASSWORD
ANGEL_TOTP_SECRET
RIGA_ACTION_TOKEN
```

## Test after deploy

Open:
```text
https://YOUR-RENDER-URL.onrender.com/health
```

Expected:
```json
{"status":"ok"}
```

## LTP test

```text
https://YOUR-RENDER-URL.onrender.com/ltp?exchange=NSE&tradingsymbol=SBIN-EQ&symboltoken=3045
```

## Custom GPT Action

1. GPT Builder → Configure → Actions → Create new action
2. Paste `openapi_schema_for_custom_gpt.json`
3. Replace server URL with your actual Render URL
4. Authentication: Bearer token = your `RIGA_ACTION_TOKEN`
