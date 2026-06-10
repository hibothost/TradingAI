#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║     ICT / SMC + S&R Trading Bot — Cloud Edition             ║
║     Connects to MT5 via MetaApi (https://metaapi.cloud)     ║
║     Deploys on Render · Railway · Heroku · any Linux VPS    ║
╚══════════════════════════════════════════════════════════════╝

Setup:
  1. Sign up at https://metaapi.cloud (free tier available)
  2. Add your MT5 account → copy Token + Account ID
  3. Set env vars:  METAAPI_TOKEN  METAAPI_ACCOUNT_ID
  4. pip install -r requirements.txt
  5. python mt5_bot_cloud.py
"""

import os
import sys
import time
import logging
import threading
import asyncio
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

try:
    from metaapi_cloud_sdk import MetaApi
except ImportError:
    print("Run: pip install metaapi-cloud-sdk")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request, send_file
    from flask_cors import CORS
except ImportError:
    print("Run: pip install flask flask-cors")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
#  CONFIGURATION  (override via environment variables)
# ══════════════════════════════════════════════════════════════

CONFIG = {
    # MetaApi credentials (set as Render env vars — never hardcode)
    "metaapi_token":      os.getenv("METAAPI_TOKEN", ""),
    "metaapi_account_id": os.getenv("METAAPI_ACCOUNT_ID", ""),

    # Trading
    "symbol":           os.getenv("TRADING_SYMBOL",  "EURUSD"),
    "htf":              os.getenv("HTF_TIMEFRAME",   "4h"),    # structure TF
    "ltf":              os.getenv("LTF_TIMEFRAME",   "15m"),   # entry TF

    # Risk
    "risk_pct":         float(os.getenv("RISK_PCT",          "1.0")),
    "min_rr":           float(os.getenv("MIN_RR",            "2.0")),
    "max_spread":       int(os.getenv("MAX_SPREAD",          "20")),
    "max_trades":       int(os.getenv("MAX_TRADES",          "3")),
    "min_confluence":   int(os.getenv("MIN_CONFLUENCE",      "3")),

    # Strategy
    "swing_lookback":   10,
    "ob_lookback":      50,
    "fvg_min_pips":     2.0,
    "sr_lookback":      150,
    "sl_buffer_pips":   5.0,

    # Kill zones (UTC)
    "kill_zones": [
        {"name": "London Open",   "start": 7,  "end": 10},
        {"name": "New York Open", "start": 12, "end": 15},
    ],

    "scan_interval":    int(os.getenv("SCAN_INTERVAL", "60")),
    "comment":          "ICT_SMC_v1",
    "port":             int(os.getenv("PORT", "5000")),
}


# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("ICT_Bot")


# ══════════════════════════════════════════════════════════════
#  ASYNC EVENT LOOP  (MetaApi is async; Flask is sync)
#  We run a dedicated asyncio loop in a background thread and
#  dispatch coroutines to it with run_coroutine_threadsafe.
# ══════════════════════════════════════════════════════════════

_loop: asyncio.AbstractEventLoop | None = None

def _start_loop():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

threading.Thread(target=_start_loop, daemon=True, name="AsyncLoop").start()
time.sleep(0.15)   # give the thread a moment to set _loop


def run(coro, timeout: int = 45):
    """Submit async coroutine to background loop; block until done."""
    if _loop is None:
        raise RuntimeError("Async loop not ready")
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# ══════════════════════════════════════════════════════════════
#  SHARED STATE
# ══════════════════════════════════════════════════════════════

state = {
    "running":      False,
    "connected":    False,
    "market_bias":  "NEUTRAL",
    "last_scan":    None,
    "error":        None,
    "account":      {},
    "equity_curve": [],
    "ob_count":     0,
    "fvg_count":    0,
}

active_signals: list[dict] = []
open_trades:    list[dict] = []
trade_history:  list[dict] = []
sr_cache:       list[dict] = []

# MetaApi objects
_meta_api   = None
_mt5_account = None
_connection  = None


# ══════════════════════════════════════════════════════════════
#  METAAPI CONNECTION
# ══════════════════════════════════════════════════════════════

async def _init_async() -> bool:
    global _meta_api, _mt5_account, _connection

    token      = CONFIG["metaapi_token"]
    account_id = CONFIG["metaapi_account_id"]

    if not token or not account_id:
        log.error("METAAPI_TOKEN and METAAPI_ACCOUNT_ID env vars are required")
        return False

    log.info("Initialising MetaApi…")
    _meta_api    = MetaApi(token)
    _mt5_account = await _meta_api.metatrader_account_api.get_account(account_id)

    if _mt5_account.state not in ("DEPLOYING", "DEPLOYED"):
        log.info("Deploying MT5 account via MetaApi…")
        await _mt5_account.deploy()

    log.info("Waiting for API server connection…")
    await _mt5_account.wait_connected()

    _connection = await _mt5_account.get_rpc_connection()
    await _connection.connect()

    log.info("Waiting for terminal sync…")
    await _connection.wait_synchronized()

    log.info("MetaApi connected and synchronised ✓")
    return True


def connect_metaapi() -> bool:
    try:
        ok = run(_init_async(), timeout=120)
        state["connected"] = bool(ok)
        return bool(ok)
    except Exception as exc:
        log.error(f"MetaApi connect failed: {exc}")
        return False


# ══════════════════════════════════════════════════════════════
#  MARKET DATA  (via MetaApi RPC)
# ══════════════════════════════════════════════════════════════

# Map timeframe strings to approximate hours (used for start_time calc)
_TF_HOURS = {
    "1m": 1/60, "5m": 5/60, "15m": 0.25, "30m": 0.5,
    "1h": 1.0,  "4h": 4.0,  "1d": 24.0,  "1w": 168.0,
}


async def _get_candles_async(symbol: str, timeframe: str, limit: int) -> pd.DataFrame | None:
    hours  = _TF_HOURS.get(timeframe, 1.0) * limit * 1.5
    start  = datetime.now(timezone.utc) - timedelta(hours=hours)
    candles = await _connection.get_historical_candles(symbol, timeframe, start, limit)
    if not candles:
        return None
    df = pd.DataFrame([{
        "time":  c["time"],
        "open":  float(c["open"]),
        "high":  float(c["high"]),
        "low":   float(c["low"]),
        "close": float(c["close"]),
        "tick_volume": c.get("tickVolume", 0),
    } for c in candles])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.sort_values("time").drop_duplicates("time").set_index("time")


def get_ohlcv(symbol: str, timeframe: str, limit: int = 250) -> pd.DataFrame | None:
    try:
        return run(_get_candles_async(symbol, timeframe, limit))
    except Exception as exc:
        log.warning(f"get_ohlcv ({symbol} {timeframe}): {exc}")
        return None


async def _get_price_async(symbol: str) -> dict | None:
    return await _connection.get_symbol_price(symbol)

def get_price(symbol: str) -> dict | None:
    try:
        return run(_get_price_async(symbol))
    except Exception as exc:
        log.warning(f"get_price ({symbol}): {exc}")
        return None


async def _get_spec_async(symbol: str) -> dict | None:
    return await _connection.get_symbol_specification(symbol)

def get_spec(symbol: str) -> dict | None:
    try:
        return run(_get_spec_async(symbol))
    except Exception as exc:
        log.warning(f"get_spec ({symbol}): {exc}")
        return None


async def _get_account_async() -> dict | None:
    return await _connection.get_account_information()

def get_account_info() -> dict | None:
    try:
        return run(_get_account_async())
    except Exception as exc:
        log.warning(f"get_account_info: {exc}")
        return None


async def _get_positions_async() -> list:
    return await _connection.get_positions() or []

def get_positions() -> list:
    try:
        return run(_get_positions_async())
    except Exception as exc:
        log.warning(f"get_positions: {exc}")
        return []


def get_pip_size(symbol: str) -> float:
    spec = get_spec(symbol)
    if spec is None:
        return 0.0001
    digits = spec.get("digits", 5)
    point  = 10 ** (-digits)
    return point * 10  # 1 pip = 10 points


# ══════════════════════════════════════════════════════════════
#  SWING POINT DETECTION
# ══════════════════════════════════════════════════════════════

def find_swings(df: pd.DataFrame, n: int = 10) -> tuple[list, list]:
    highs, lows = [], []
    ah, al, idx = df["high"].values, df["low"].values, df.index
    for i in range(n, len(df) - n):
        if ah[i] == ah[i - n:i + n + 1].max():
            highs.append({"i": i, "time": str(idx[i]), "price": float(ah[i])})
        if al[i] == al[i - n:i + n + 1].min():
            lows.append({"i": i, "time": str(idx[i]), "price": float(al[i])})
    return highs, lows


# ══════════════════════════════════════════════════════════════
#  MARKET STRUCTURE
# ══════════════════════════════════════════════════════════════

def get_market_structure(df: pd.DataFrame) -> tuple[str, list, list]:
    n = CONFIG["swing_lookback"]
    sh, sl = find_swings(df, n)
    if len(sh) < 3 or len(sl) < 3:
        return "NEUTRAL", sh, sl

    sh = sorted(sh, key=lambda x: x["i"])
    sl = sorted(sl, key=lambda x: x["i"])
    close   = float(df["close"].iloc[-1])
    bos_bull = close > sh[-1]["price"]
    bos_bear = close < sl[-1]["price"]

    r_sh, r_sl = sh[-3:], sl[-3:]
    hh = all(r_sh[i+1]["price"] > r_sh[i]["price"] for i in range(2))
    hl = all(r_sl[i+1]["price"] > r_sl[i]["price"] for i in range(2))
    lh = all(r_sh[i+1]["price"] < r_sh[i]["price"] for i in range(2))
    ll = all(r_sl[i+1]["price"] < r_sl[i]["price"] for i in range(2))

    if bos_bull or (hh and hl): return "BULLISH", sh, sl
    if bos_bear or (lh and ll): return "BEARISH", sh, sl
    return "NEUTRAL", sh, sl


# ══════════════════════════════════════════════════════════════
#  ORDER BLOCK DETECTION
# ══════════════════════════════════════════════════════════════

def find_order_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    obs     = []
    sub     = df.iloc[-CONFIG["ob_lookback"]:].reset_index()
    current = float(df["close"].iloc[-1])

    for i in range(2, len(sub) - 4):
        c    = sub.iloc[i]
        body = abs(c["close"] - c["open"])
        rng  = c["high"] - c["low"]
        if rng == 0 or body / rng < 0.25:
            continue
        future = sub.iloc[i+1:i+5]

        if bias in ("BULLISH", "NEUTRAL") and c["close"] < c["open"]:
            if (future["close"].max() - c["high"]) / rng > 0.5:
                obs.append({"type": "BULLISH_OB", "time": str(sub.iloc[i]["time"]),
                            "high": round(float(c["high"]), 5), "low": round(float(c["low"]), 5),
                            "mid":  round((float(c["high"]) + float(c["low"])) / 2, 5),
                            "mitigated": current < float(c["low"])})

        if bias in ("BEARISH", "NEUTRAL") and c["close"] > c["open"]:
            if (c["low"] - future["close"].min()) / rng > 0.5:
                obs.append({"type": "BEARISH_OB", "time": str(sub.iloc[i]["time"]),
                            "high": round(float(c["high"]), 5), "low": round(float(c["low"]), 5),
                            "mid":  round((float(c["high"]) + float(c["low"])) / 2, 5),
                            "mitigated": current > float(c["high"])})

    return [ob for ob in obs if not ob["mitigated"]]


# ══════════════════════════════════════════════════════════════
#  FAIR VALUE GAP DETECTION
# ══════════════════════════════════════════════════════════════

def find_fvg(df: pd.DataFrame) -> list[dict]:
    fvgs    = []
    pip     = get_pip_size(CONFIG["symbol"])
    min_sz  = CONFIG["fvg_min_pips"] * pip
    sub     = df.iloc[-100:]
    current = float(df["close"].iloc[-1])

    for i in range(1, len(sub) - 1):
        prev, nxt = sub.iloc[i-1], sub.iloc[i+1]

        top, bot = float(nxt["low"]), float(prev["high"])
        if top > bot and (top - bot) >= min_sz:
            fvgs.append({"type": "BULLISH_FVG", "time": str(sub.index[i]),
                         "top": round(top, 5), "bottom": round(bot, 5),
                         "mid": round((top+bot)/2, 5), "size_pips": round((top-bot)/pip, 1),
                         "filled": current < bot})

        top, bot = float(prev["low"]), float(nxt["high"])
        if top > bot and (top - bot) >= min_sz:
            fvgs.append({"type": "BEARISH_FVG", "time": str(sub.index[i]),
                         "top": round(top, 5), "bottom": round(bot, 5),
                         "mid": round((top+bot)/2, 5), "size_pips": round((top-bot)/pip, 1),
                         "filled": current > top})

    return [f for f in fvgs if not f["filled"]]


# ══════════════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE
# ══════════════════════════════════════════════════════════════

def find_sr_levels(df: pd.DataFrame) -> list[dict]:
    pip = get_pip_size(CONFIG["symbol"])
    tol = 10 * pip
    sub = df.iloc[-CONFIG["sr_lookback"]:]
    sh, sl = find_swings(sub, n=7)
    raw = sorted(p["price"] for p in sh + sl)
    if not raw:
        return []

    clusters, group = [], [raw[0]]
    for p in raw[1:]:
        if p - group[0] <= tol:
            group.append(p)
        else:
            clusters.append(group); group = [p]
    clusters.append(group)

    current = float(df["close"].iloc[-1])
    levels  = [{"price": round(float(np.mean(g)), 5),
                "type":  "RESISTANCE" if float(np.mean(g)) > current else "SUPPORT",
                "strength": len(g),
                "dist_pips": round(abs(float(np.mean(g)) - current) / pip, 1)} for g in clusters]
    return sorted(levels, key=lambda x: x["dist_pips"])[:10]


# ══════════════════════════════════════════════════════════════
#  KILL ZONE
# ══════════════════════════════════════════════════════════════

def in_kill_zone() -> tuple[bool, str | None]:
    h = datetime.now(timezone.utc).hour
    for kz in CONFIG["kill_zones"]:
        if kz["start"] <= h < kz["end"]:
            return True, kz["name"]
    return False, None


def kill_zone_status() -> list[dict]:
    h, m = datetime.now(timezone.utc).hour, datetime.now(timezone.utc).minute
    out  = []
    for kz in CONFIG["kill_zones"]:
        active = kz["start"] <= h < kz["end"]
        if active:
            out.append({**kz, "active": True,  "mins_left":  int((kz["end"]   - h) * 60 - m)})
        else:
            until = (kz["start"] - h) * 60 - m if h < kz["start"] else (24 - h + kz["start"]) * 60 - m
            out.append({**kz, "active": False, "mins_until": int(until)})
    return out


# ══════════════════════════════════════════════════════════════
#  SIGNAL GENERATION  (ICT/SMC confluence)
# ══════════════════════════════════════════════════════════════

def generate_signals(symbol, bias, obs, fvgs, sr) -> list[dict]:
    price_data = get_price(symbol)
    if price_data is None:
        return []

    bid, ask    = float(price_data["bid"]), float(price_data["ask"])
    pip         = get_pip_size(symbol)
    buf         = CONFIG["sl_buffer_pips"] * pip
    is_kz, kz   = in_kill_zone()
    signals     = []

    for ob in obs[-20:]:
        oh, ol = ob["high"], ob["low"]
        conf: list[str] = []

        if ob["type"] == "BULLISH_OB" and bias == "BULLISH":
            if ol <= ask <= oh:
                conf += ["HTF Bullish Bias", "Price in Bullish OB"]
                for fvg in fvgs:
                    if fvg["type"] == "BULLISH_FVG" and fvg["bottom"] <= oh and fvg["top"] >= ol:
                        conf.append(f"Bullish FVG ({fvg['size_pips']} pips)"); break
                for lvl in sr[:6]:
                    if lvl["type"] == "SUPPORT" and lvl["dist_pips"] < 20:
                        conf.append(f"Support @ {lvl['price']:.5f}"); break
                if is_kz: conf.append(f"Kill Zone: {kz}")
                if len(conf) >= CONFIG["min_confluence"]:
                    sl = ol - buf; tp = ask + (ask - sl) * CONFIG["min_rr"]
                    signals.append(_sig("BUY", symbol, ask, sl, tp, conf, ob, is_kz, pip))

        elif ob["type"] == "BEARISH_OB" and bias == "BEARISH":
            if ol <= bid <= oh:
                conf += ["HTF Bearish Bias", "Price in Bearish OB"]
                for fvg in fvgs:
                    if fvg["type"] == "BEARISH_FVG" and fvg["bottom"] <= oh and fvg["top"] >= ol:
                        conf.append(f"Bearish FVG ({fvg['size_pips']} pips)"); break
                for lvl in sr[:6]:
                    if lvl["type"] == "RESISTANCE" and lvl["dist_pips"] < 20:
                        conf.append(f"Resistance @ {lvl['price']:.5f}"); break
                if is_kz: conf.append(f"Kill Zone: {kz}")
                if len(conf) >= CONFIG["min_confluence"]:
                    sl = oh + buf; tp = bid - (sl - bid) * CONFIG["min_rr"]
                    signals.append(_sig("SELL", symbol, bid, sl, tp, conf, ob, is_kz, pip))

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def _sig(direction, symbol, entry, sl, tp, conf, ob, is_kz, pip) -> dict:
    return {"id": f"{direction}_{int(time.time())}", "symbol": symbol,
            "direction": direction, "entry": round(entry, 5), "sl": round(sl, 5),
            "tp": round(tp, 5), "risk_pips": round(abs(entry - sl) / pip, 1),
            "rr": CONFIG["min_rr"], "score": len(conf), "confluence": conf,
            "ob": ob, "in_kz": is_kz, "time": datetime.now(timezone.utc).isoformat()}


# ══════════════════════════════════════════════════════════════
#  LOT SIZE
# ══════════════════════════════════════════════════════════════

def calc_lot_size(symbol: str, entry: float, sl: float) -> float:
    acct = get_account_info()
    spec = get_spec(symbol)
    if not acct or not spec:
        return 0.01

    balance    = float(acct.get("balance", 1000))
    risk_amt   = balance * (CONFIG["risk_pct"] / 100)
    digits     = spec.get("digits", 5)
    pip_size   = (10 ** -digits) * 10
    tick_sz    = float(spec.get("tickSize", 10 ** -digits))
    tick_val   = float(spec.get("tickValue", 1.0))
    sl_pips    = abs(entry - sl) / pip_size
    pip_value  = tick_val * (pip_size / tick_sz)

    if sl_pips == 0 or pip_value == 0:
        return float(spec.get("minVolume", 0.01))

    step = float(spec.get("volumeStep", 0.01))
    lots = round(risk_amt / (sl_pips * pip_value) / step) * step
    lots = max(float(spec.get("minVolume", 0.01)), min(float(spec.get("maxVolume", 100.0)), lots))
    return round(lots, 2)


# ══════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ══════════════════════════════════════════════════════════════

async def _place_async(direction, symbol, volume, sl, tp):
    opts = {"comment": CONFIG["comment"]}
    if direction == "BUY":
        return await _connection.create_market_buy_order(symbol, volume, sl, tp, opts)
    return await _connection.create_market_sell_order(symbol, volume, sl, tp, opts)


def place_order(sig: dict) -> dict | None:
    symbol    = sig["symbol"]
    direction = sig["direction"]

    # Check spread
    price_data = get_price(symbol)
    spec       = get_spec(symbol)
    if price_data and spec:
        spread_pts = (float(price_data["ask"]) - float(price_data["bid"])) / (10 ** -spec.get("digits", 5))
        if spread_pts > CONFIG["max_spread"]:
            log.warning(f"Spread {spread_pts:.0f} pts > {CONFIG['max_spread']}. Skipping.")
            return None

    # Check max trades
    positions = get_positions()
    bot_pos   = [p for p in positions if CONFIG["comment"] in p.get("comment", "")]
    if len(bot_pos) >= CONFIG["max_trades"]:
        log.info(f"Max trades ({CONFIG['max_trades']}) reached. Skipping.")
        return None

    # Live price
    pd_ = get_price(symbol)
    if not pd_:
        return None
    entry = float(pd_["ask"]) if direction == "BUY" else float(pd_["bid"])
    lots  = calc_lot_size(symbol, entry, sig["sl"])

    try:
        result = run(_place_async(direction, symbol, lots, sig["sl"], sig["tp"]))
    except Exception as exc:
        log.error(f"place_order error: {exc}")
        return None

    trade = {"ticket": result.get("orderId") or result.get("positionId", "?"),
             "symbol": symbol, "direction": direction, "entry": round(entry, 5),
             "sl": sig["sl"], "tp": sig["tp"], "lots": lots, "pnl": 0.0,
             "status": "OPEN", "time": datetime.now(timezone.utc).isoformat(),
             "confluence": sig["confluence"]}
    trade_history.append(trade)
    log.info(f"✅ {direction} {symbol} @ {entry:.5f} | SL {sig['sl']:.5f} | TP {sig['tp']:.5f} | {lots} lots")
    return trade


async def _close_async(position_id: str):
    await _connection.close_position(position_id, {"comment": "Bot close"})

def close_position(position_id: str) -> bool:
    try:
        run(_close_async(str(position_id)))
        return True
    except Exception as exc:
        log.error(f"close_position error: {exc}")
        return False


# ══════════════════════════════════════════════════════════════
#  REFRESH OPEN TRADES
# ══════════════════════════════════════════════════════════════

def refresh_open_trades():
    global open_trades
    positions = get_positions()
    bot_pos   = [p for p in positions if CONFIG["comment"] in p.get("comment", "")]
    open_trades = [{
        "ticket":    p.get("id"),
        "symbol":    p.get("symbol"),
        "direction": "BUY" if p.get("type") == "POSITION_TYPE_BUY" else "SELL",
        "entry":     round(float(p.get("openPrice",    0)), 5),
        "current":   round(float(p.get("currentPrice", 0)), 5),
        "sl":        round(float(p.get("stopLoss",     0)), 5),
        "tp":        round(float(p.get("takeProfit",   0)), 5),
        "lots":      p.get("volume", 0),
        "pnl":       round(float(p.get("profit", 0)), 2),
        "time":      str(p.get("time", "")),
        "comment":   p.get("comment", ""),
    } for p in bot_pos]


# ══════════════════════════════════════════════════════════════
#  MAIN BOT LOOP
# ══════════════════════════════════════════════════════════════

def bot_loop():
    global active_signals, sr_cache
    log.info("═" * 55)
    log.info("  Bot loop started  │  ICT/SMC + S&R strategy")
    log.info("═" * 55)

    while state["running"]:
        try:
            symbol = CONFIG["symbol"]

            # Account snapshot
            acct = get_account_info()
            if acct:
                state["account"] = {
                    "login":       acct.get("login"),
                    "balance":     round(float(acct.get("balance",    0)), 2),
                    "equity":      round(float(acct.get("equity",     0)), 2),
                    "margin":      round(float(acct.get("margin",     0)), 2),
                    "free_margin": round(float(acct.get("freeMargin", 0)), 2),
                    "profit":      round(float(acct.get("profit",     0)), 2),
                    "currency":    acct.get("currency", "USD"),
                    "leverage":    acct.get("leverage", 0),
                    "broker":      acct.get("broker",   "—"),
                }
                state["equity_curve"].append({
                    "time":   datetime.now(timezone.utc).isoformat(),
                    "equity": acct.get("equity", 0),
                })
                state["equity_curve"] = state["equity_curve"][-500:]

            # Candle data
            df_htf = get_ohlcv(symbol, CONFIG["htf"], 250)
            df_ltf = get_ohlcv(symbol, CONFIG["ltf"], 250)
            if df_htf is None or df_ltf is None:
                log.warning("Candle data unavailable — retrying in 30s")
                time.sleep(30)
                continue

            # Analysis
            bias, _, _ = get_market_structure(df_htf)
            state["market_bias"] = bias

            obs  = find_order_blocks(df_ltf, bias)
            fvgs = find_fvg(df_ltf)
            sr   = find_sr_levels(df_htf)

            state["ob_count"]  = len(obs)
            state["fvg_count"] = len(fvgs)
            sr_cache           = sr

            # Signals
            sigs       = generate_signals(symbol, bias, obs, fvgs, sr)
            active_signals = sigs

            # Execute in kill zone only
            is_kz, kz_name = in_kill_zone()
            if sigs:
                best = sigs[0]
                if is_kz and best["score"] >= CONFIG["min_confluence"]:
                    place_order(best)
                else:
                    reason = "outside kill zone" if not is_kz else f"score {best['score']} < {CONFIG['min_confluence']}"
                    log.info(f"Signal queued ({reason}): {best['direction']} {symbol}")

            refresh_open_trades()

            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            state["error"]     = None

            log.info(
                f"Bias:{bias:8s} │ OBs:{len(obs):2d} │ FVGs:{len(fvgs):2d} │ "
                f"S&R:{len(sr):2d} │ Sigs:{len(sigs):2d} │ KZ:{'✓ '+kz_name if is_kz else '—'}"
            )

            time.sleep(CONFIG["scan_interval"])

        except Exception as exc:
            log.exception(f"Bot loop error: {exc}")
            state["error"] = str(exc)
            time.sleep(30)

    log.info("Bot loop stopped.")


# ══════════════════════════════════════════════════════════════
#  FLASK API
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app)


@app.route("/health")
def health():
    return jsonify({"status": "ok", "connected": state["connected"]})


@app.route("/")
def index():
    """Serve the trading dashboard."""
    dashboard = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if os.path.exists(dashboard):
        return send_file(dashboard)
    return jsonify({"bot": "ICT/SMC v1", "connected": state["connected"],
                    "running": state["running"], "bias": state["market_bias"]})


@app.route("/api/status")
def api_status():
    return jsonify({**state, "open_trades": open_trades,
                    "active_signals": active_signals,
                    "kill_zones": kill_zone_status(),
                    "sr_levels": sr_cache[:6]})


@app.route("/api/history")
def api_history():
    return jsonify({"trades": trade_history[-100:]})


@app.route("/api/equity")
def api_equity():
    return jsonify({"curve": state["equity_curve"]})


@app.route("/api/start", methods=["POST"])
def api_start():
    if not state["connected"]:
        return jsonify({"error": "MetaApi not connected"}), 400
    if state["running"]:
        return jsonify({"message": "Already running"})
    state["running"] = True
    threading.Thread(target=bot_loop, daemon=True).start()
    return jsonify({"message": "Bot started"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    state["running"] = False
    return jsonify({"message": "Bot stopping…"})


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    editable = ["risk_pct", "min_rr", "max_trades", "symbol",
                "scan_interval", "min_confluence", "fvg_min_pips"]
    if request.method == "POST":
        for k in editable:
            if k in (data := request.get_json(force=True)):
                CONFIG[k] = data[k]
        return jsonify({"message": "Updated"})
    return jsonify({k: CONFIG[k] for k in editable})


@app.route("/api/close/<position_id>", methods=["POST"])
def api_close(position_id):
    if close_position(position_id):
        refresh_open_trades()
        return jsonify({"message": f"Position {position_id} closed"})
    return jsonify({"error": "Close failed"}), 400


@app.route("/api/closeall", methods=["POST"])
def api_close_all():
    closed = sum(1 for t in list(open_trades) if close_position(str(t["ticket"])))
    refresh_open_trades()
    return jsonify({"message": f"Closed {closed} trade(s)"})


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║       ICT / SMC + S&R Bot  —  Cloud Edition         ║
║   Powered by MetaApi  ·  Deploys on Render/Linux     ║
╚══════════════════════════════════════════════════════╝
    """)

    if not CONFIG["metaapi_token"]:
        print("  ❌  METAAPI_TOKEN env var not set.")
        print("  ➜  Sign up at https://metaapi.cloud and add it to Render > Environment")
        sys.exit(1)

    if connect_metaapi():
        a = state["account"]
        print(f"  ✅  Connected  │  {a.get('broker','—')}")
        print(f"  💰  Balance    │  {a.get('balance',0):.2f} {a.get('currency','')}")
        print(f"  📊  Symbol     │  {CONFIG['symbol']}")
        print(f"  🌐  Port       │  {CONFIG['port']}")
        print(f"\n  POST /api/start to begin trading\n")
        app.run(host="0.0.0.0", port=CONFIG["port"], debug=False, use_reloader=False)
    else:
        print("  ❌  MetaApi connection failed — check your METAAPI_TOKEN and METAAPI_ACCOUNT_ID")
        sys.exit(1)
