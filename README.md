# Live Quotes Dashboard

A simple Streamlit app showing live NSE quotes — LTP, previous close, % change,
volume — for the full equity + F&O universe, with an F&O-only view that adds:

- **VWAP** (session, from 5-min candles)
- **200 EMA** (5-min timeframe, ~45 days of history for proper convergence)
- **Just Crossed VWAP / Just Crossed EMA** flags, fired only on a fully closed
  5-min candle (never a still-forming one)

NIFTY 50 and NIFTY BANK are always pinned to the top of the table. Rows with
an active crossover sort to the top; stocks crossing *both* VWAP and EMA rank
above single-crossover stocks.

## Setup

```
pip install streamlit requests pandas --break-system-packages   # drop the flag on Windows
```

Set your Upstox access token before launching (or paste it into the sidebar):

```powershell
$env:UPSTOX_ACCESS_TOKEN = "your_token_here"
```

## Run

```
python -m streamlit run live_quotes_dashboard.py
```

## Data source

All quotes and candles come from the [Upstox API](https://upstox.com/developer/api-documentation) —
v2 `/market-quote/quotes` for LTP, and the v3 historical-candle endpoints
(chunked into ≤25-day requests) for 5-min candle history.
