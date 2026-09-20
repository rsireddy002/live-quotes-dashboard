"""
Live Quotes Dashboard — NSE stocks (F&O + Equity)
------------------------------------------------------
Streamlit app showing a live, auto-refreshing table of:
    Sl No | Symbol | Prev Close | LTP | % Change | Volume
and, for the F&O universe (~190 stocks):
    VWAP | 200 EMA (5-min) | Just Crossed VWAP | Just Crossed EMA
using the Upstox API. LTP/quote data refreshes on the fast interval you set
in the sidebar; VWAP/EMA/crossovers recompute every 5 minutes (matching
5-min candle close), independent of that faster refresh loop.

Run with:
    python -m streamlit run live_quotes_dashboard.py

Setup:
    1. pip install streamlit requests pandas --break-system-packages   (Windows: drop --break-system-packages)
    2. Set your Upstox access token as an environment variable before launching:
         Windows PowerShell:  $env:UPSTOX_ACCESS_TOKEN = "your_token_here"
       Or just paste it into the sidebar box when the app opens.
"""

import os
import io
import gzip
import json
import time
import urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import streamlit as st
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
BATCH_SIZE = 500  # Upstox quote endpoint limit per request

# V3 historical-candle API (supports arbitrary 5-min interval; v2 only offers 1min/30min)
V3_HIST_URL = "https://api.upstox.com/v3/historical-candle/{key}/minutes/5/{to_date}/{from_date}"
V3_INTRADAY_URL = "https://api.upstox.com/v3/historical-candle/intraday/{key}/minutes/5"
EMA_PERIOD = 200
HIST_LOOKBACK_DAYS = 45  # calendar days of 5-min history — enough bars for EMA-200 to properly converge
HIST_CHUNK_DAYS = 25  # matches the 25-day chunking already used for historical candles elsewhere
INDICATOR_TTL = 300  # seconds — recompute VWAP/EMA/crossovers every 5 min, not every LTP refresh

st.set_page_config(page_title="Live NSE Quotes", layout="wide")
st.title("📈 Live NSE Quotes — F&O + Equity")

# ---------------- Sidebar controls ----------------
def _default_token():
    env_val = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    if env_val:
        return env_val
    try:
        return st.secrets.get("UPSTOX_ACCESS_TOKEN", "")
    except Exception:
        return ""  # no secrets.toml locally — that's fine


with st.sidebar:
    st.header("Settings")
    token = st.text_input(
        "Upstox Access Token",
        value=_default_token(),
        type="password",
        help="Reads UPSTOX_ACCESS_TOKEN from env var or Streamlit secrets by default; can paste one here instead.",
    )
    refresh_secs = st.number_input("Refresh interval (seconds)", min_value=3, max_value=60, value=5)
    universe = st.radio(
        "Universe", ["F&O stocks only", "All (Equity + F&O)", "Equity (EQ) only"], index=0,
        help="VWAP / 200 EMA (5-min) / crossover columns are only computed for the F&O universe — "
             "pulling 200+ candles per stock for the full ~2000-stock equity universe every cycle "
             "would be far too slow / rate-limited.",
    )
    search = st.text_input("Filter by symbol contains", "")
    auto_refresh = st.checkbox("Auto-refresh", value=True)

if not token:
    st.warning("Enter your Upstox access token in the sidebar to begin.")
    st.stop()

HEADERS = {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ---------------- Instrument list (cached) ----------------
@st.cache_data(ttl=24 * 3600, show_spinner="Loading NSE instrument list...")
def load_instruments():
    resp = requests.get(INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()
    data = json.loads(gzip.decompress(resp.content))
    df = pd.DataFrame(data)
    # Keep only equity (EQ) and F&O underlying stock derivatives' cash-market entries.
    # Segment values: NSE_EQ (cash equity), NSE_FO (futures & options)
    df = df[df["segment"].isin(["NSE_EQ", "NSE_FO"])].copy()
    return df


try:
    instruments = load_instruments()
except Exception as e:
    st.error(f"Failed to load instrument list: {e}")
    st.stop()

eq_df = instruments[(instruments["segment"] == "NSE_EQ") & (instruments["instrument_type"] == "EQ")]
fo_underlyings = instruments[instruments["segment"] == "NSE_FO"]["asset_symbol"].dropna().unique().tolist()

if universe == "Equity (EQ) only":
    symbol_df = eq_df
elif universe == "F&O stocks only":
    symbol_df = eq_df[eq_df["trading_symbol"].isin(fo_underlyings) | eq_df["asset_symbol"].isin(fo_underlyings)]
else:
    symbol_df = eq_df  # equity list already a superset that includes all F&O underlyings

if search:
    symbol_df = symbol_df[symbol_df["trading_symbol"].str.contains(search.upper(), na=False)]

instrument_keys = symbol_df["instrument_key"].dropna().unique().tolist()
key_to_symbol = dict(zip(symbol_df["instrument_key"], symbol_df["trading_symbol"]))

# Nifty 50 / Bank Nifty — pinned to the top of the table regardless of Universe selection.
INDEX_INSTRUMENTS = [("NSE_INDEX|Nifty 50", "NIFTY 50"), ("NSE_INDEX|Nifty Bank", "NIFTY BANK")]
for _key, _label in INDEX_INSTRUMENTS:
    key_to_symbol.setdefault(_key, _label)

st.caption(f"Tracking {len(instrument_keys)} instruments · last load of instrument master cached 24h")

if not instrument_keys:
    st.info("No instruments match the current filter.")
    st.stop()


# ---------------- Quote fetching ----------------
def fetch_quotes(keys):
    rows = []
    for i in range(0, len(keys), BATCH_SIZE):
        batch = keys[i : i + BATCH_SIZE]
        params = {"instrument_key": ",".join(batch)}
        try:
            r = requests.get(QUOTE_URL, headers=HEADERS, params=params, timeout=15)
            r.raise_for_status()
        except requests.exceptions.RequestException as e:
            st.error(f"Quote request failed: {e}")
            continue
        payload = r.json().get("data", {})
        for _, q in payload.items():
            ik = q.get("instrument_token") or q.get("instrument_key")
            symbol = key_to_symbol.get(ik, q.get("symbol", ik))
            ltp = q.get("last_price")
            # net_change = change from YESTERDAY's close to LTP (per Upstox docs).
            # ohlc.close is unreliable intraday — it reflects *today's* close and
            # is only populated post-market, so we don't use it for prev-close math.
            net_change = q.get("net_change")
            prev_close = (ltp - net_change) if (ltp is not None and net_change is not None) else None
            pct_change = (net_change / prev_close * 100) if (net_change is not None and prev_close) else None
            rows.append(
                {
                    "instrument_key": ik,
                    "Symbol": symbol,
                    "Prev Close": round(prev_close, 2) if prev_close is not None else None,
                    "LTP": ltp,
                    "% Change": round(pct_change, 2) if pct_change is not None else None,
                    "Volume": q.get("volume"),
                }
            )
    return pd.DataFrame(rows)


# ---------------- VWAP / 200 EMA (5-min) / crossover indicators ----------------
CANDLE_COLS = ["ts", "open", "high", "low", "close", "volume", "oi"]


def _date_chunks(start_date, end_date, chunk_days=HIST_CHUNK_DAYS):
    """Split [start_date, end_date] into <=chunk_days windows, newest-first."""
    chunks = []
    cur_end = end_date
    while cur_end >= start_date:
        cur_start = max(start_date, cur_end - timedelta(days=chunk_days - 1))
        chunks.append((cur_start, cur_end))
        cur_end = cur_start - timedelta(days=1)
    return chunks


def _fetch_candles_for_key(key, headers):
    """Fetch 5-min candles: chunked recent history (V3, up to yesterday) + today (V3 intraday)."""
    encoded_key = urllib.parse.quote(key, safe="")
    today = datetime.now(IST).date()
    hist_end = today - timedelta(days=1)
    hist_start = today - timedelta(days=HIST_LOOKBACK_DAYS)
    frames = []
    for chunk_start, chunk_end in _date_chunks(hist_start, hist_end):
        url = V3_HIST_URL.format(key=encoded_key, to_date=chunk_end.isoformat(), from_date=chunk_start.isoformat())
        try:
            r = requests.get(url, headers=headers, timeout=15)
            if r.ok:
                candles = (r.json().get("data") or {}).get("candles") or []
                if candles:
                    frames.append(pd.DataFrame(candles, columns=CANDLE_COLS[: len(candles[0])]))
        except requests.exceptions.RequestException:
            pass
    try:
        r = requests.get(V3_INTRADAY_URL.format(key=encoded_key), headers=headers, timeout=15)
        if r.ok:
            candles = (r.json().get("data") or {}).get("candles") or []
            if candles:
                frames.append(pd.DataFrame(candles, columns=CANDLE_COLS[: len(candles[0])]))
    except requests.exceptions.RequestException:
        pass
    if not frames:
        return key, None
    df = pd.concat(frames, ignore_index=True)
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(IST)
    df = df.sort_values("ts").drop_duplicates(subset="ts").reset_index(drop=True)
    return key, df


def _cross_flag(prev_close, now_close, prev_ind, now_ind):
    if prev_close is None or prev_ind is None or now_ind is None or pd.isna(prev_ind) or pd.isna(now_ind):
        return ""
    if prev_close <= prev_ind and now_close > now_ind:
        return "↑ Just crossed above from below"
    if prev_close >= prev_ind and now_close < now_ind:
        return "↓ Just crossed below from above"
    return ""


def _compute_indicator_row(df):
    if df is None or df.empty:
        return {"VWAP": None, "200 EMA (5m)": None, "Just Crossed VWAP": "", "Just Crossed EMA": ""}

    # Drop a still-forming candle (its 5-min bar hasn't closed yet) so VWAP/EMA
    # and crossover checks only ever use fully closed 5-min bars.
    now = pd.Timestamp.now(tz=IST)
    df = df[df["ts"] + pd.Timedelta(minutes=5) <= now]
    if df.empty:
        return {"VWAP": None, "200 EMA (5m)": None, "Just Crossed VWAP": "", "Just Crossed EMA": ""}

    # Use the most recent trading session present in the data (not literal
    # calendar "today") so VWAP still shows correctly on weekends/holidays/
    # pre-market, when today itself has no candles.
    session_date = df["ts"].dt.date.max()
    session_df = df[df["ts"].dt.date == session_date]
    if not session_df.empty:
        typ = (session_df["high"] + session_df["low"] + session_df["close"]) / 3
        cum_pv = (typ * session_df["volume"]).cumsum()
        cum_vol = session_df["volume"].cumsum().replace(0, pd.NA)
        vwap_series = cum_pv / cum_vol
    else:
        vwap_series = pd.Series(dtype=float)

    ema_series = df["close"].ewm(span=EMA_PERIOD, adjust=False).mean()

    close_now = df["close"].iloc[-1]
    close_prev = df["close"].iloc[-2] if len(df) > 1 else None
    vwap_now = vwap_series.iloc[-1] if len(vwap_series) else None
    vwap_prev = vwap_series.iloc[-2] if len(vwap_series) > 1 else None
    ema_now = ema_series.iloc[-1]
    ema_prev = ema_series.iloc[-2] if len(ema_series) > 1 else None

    return {
        "VWAP": round(vwap_now, 2) if vwap_now is not None and not pd.isna(vwap_now) else None,
        "200 EMA (5m)": round(ema_now, 2) if ema_now is not None else None,
        "Just Crossed VWAP": _cross_flag(close_prev, close_now, vwap_prev, vwap_now),
        "Just Crossed EMA": _cross_flag(close_prev, close_now, ema_prev, ema_now),
    }


def _current_five_min_bucket():
    """Wall-clock 5-min bucket key (e.g. '2026-09-20T09:15'), IST. Passed into
    compute_indicators as a cache key so recompute happens exactly at 5-min
    candle-close boundaries (9:15, 9:20, 9:25, ...), not just N seconds after
    the app happened to start."""
    now = datetime.now(IST)
    floored_minute = (now.minute // 5) * 5
    bucket = now.replace(minute=floored_minute, second=0, microsecond=0)
    return bucket.isoformat()


@st.cache_data(ttl=INDICATOR_TTL, show_spinner="Computing VWAP / 200 EMA (5-min) for F&O universe...")
def compute_indicators(keys_tuple, _headers, five_min_bucket):
    results = {}
    with ThreadPoolExecutor(max_workers=20) as ex:
        futures = {ex.submit(_fetch_candles_for_key, k, _headers): k for k in keys_tuple}
        for fut in as_completed(futures):
            k, df = fut.result()
            results[k] = _compute_indicator_row(df)
    return results


placeholder = st.empty()
status = st.empty()

df = fetch_quotes(instrument_keys)

show_indicators = universe == "F&O stocks only"
if show_indicators and not df.empty:
    indicator_map = compute_indicators(tuple(instrument_keys), HEADERS, _current_five_min_bucket())
    ind_df = pd.DataFrame.from_dict(indicator_map, orient="index").reset_index()
    ind_df = ind_df.rename(columns={"index": "instrument_key"})
    df = df.merge(ind_df, on="instrument_key", how="left")

# Nifty 50 / Bank Nifty — always fetched, pinned to the top regardless of Universe/sort order.
index_keys = [k for k, _ in INDEX_INSTRUMENTS]
index_df = fetch_quotes(index_keys)
if show_indicators and not index_df.empty:
    idx_indicator_map = compute_indicators(tuple(index_keys), HEADERS, _current_five_min_bucket())
    idx_ind_df = pd.DataFrame.from_dict(idx_indicator_map, orient="index").reset_index()
    idx_ind_df = idx_ind_df.rename(columns={"index": "instrument_key"})
    index_df = index_df.merge(idx_ind_df, on="instrument_key", how="left")
if not index_df.empty:
    order_map = {k: i for i, (k, _) in enumerate(INDEX_INSTRUMENTS)}
    index_df = index_df.assign(_order=index_df["instrument_key"].map(order_map))
    index_df = index_df.sort_values("_order").drop(columns=["_order"]).reset_index(drop=True)

with placeholder.container():
    if df.empty and index_df.empty:
        st.info("No quote data returned.")
    else:
        if not df.empty:
            cross_count = pd.Series([0] * len(df), index=df.index)
            if show_indicators:
                cross_count = df["Just Crossed VWAP"].astype(bool).astype(int) + df["Just Crossed EMA"].astype(bool).astype(int)
            df = df.assign(_cross_count=cross_count)
            df = df.sort_values(["_cross_count", "Symbol"], ascending=[False, True]).drop(columns=["_cross_count"])
            df = df.reset_index(drop=True)

        df = pd.concat([index_df, df], ignore_index=True) if not index_df.empty else df
        df = df.drop(columns=["instrument_key"])
        df.insert(0, "Sl No", range(1, len(df) + 1))

        def color_pct(val):
            if val is None or pd.isna(val):
                return ""
            color = "green" if val > 0 else ("red" if val < 0 else "black")
            return f"color: {color}"

        def color_cross(val):
            if not val:
                return ""
            return "color: green; font-weight: 600" if val.startswith("↑") else "color: red; font-weight: 600"

        style_fn = df.style.map if hasattr(df.style, "map") else df.style.applymap
        fmt = {"LTP": "{:.2f}", "% Change": "{:.2f}%", "Prev Close": "{:.2f}"}
        styled = style_fn(color_pct, subset=["% Change"])
        if show_indicators:
            fmt.update({"VWAP": "{:.2f}", "200 EMA (5m)": "{:.2f}"})
            styled = styled.map(color_cross, subset=["Just Crossed VWAP", "Just Crossed EMA"]) if hasattr(
                styled, "map"
            ) else styled.applymap(color_cross, subset=["Just Crossed VWAP", "Just Crossed EMA"])
        styled = styled.format(fmt, na_rep="—")
        st.dataframe(styled, use_container_width=True, height=700, hide_index=True)

        if not show_indicators:
            st.caption("Switch Universe to \"F&O stocks only\" in the sidebar to see VWAP / 200 EMA (5-min) / crossover columns.")

now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
status.caption(f"Last updated: {now}")

if auto_refresh:
    time.sleep(refresh_secs)
    st.rerun()
