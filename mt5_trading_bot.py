#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║         ICT / SMC + S&R Trading Bot for MetaTrader 5        ║
║  Strategy: Order Blocks · FVG · Market Structure · KZ Filter ║
║  Requires Python 3.10+ on Windows with MT5 installed        ║
╚══════════════════════════════════════════════════════════════╝

Install dependencies:
    pip install MetaTrader5 pandas numpy flask flask-cors

Run:
    python mt5_trading_bot.py
    Then open dashboard.html in your browser
"""

import sys
import time
import json
import logging
import threading
from datetime import datetime, timezone

import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
except ImportError:
    print("MetaTrader5 not installed. Run: pip install MetaTrader5")
    sys.exit(1)

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
except ImportError:
    print("Flask not installed. Run: pip install flask flask-cors")
    sys.exit(1)


# ══════════════════════════════════════════════════════════════
#  CONFIGURATION  ─  Edit before running
# ══════════════════════════════════════════════════════════════

CONFIG = {
    # ── MT5 credentials (leave None if already logged in to MT5)
    "login":    None,           # e.g. 12345678
    "password": None,           # e.g. "yourpassword"
    "server":   None,           # e.g. "ICMarkets-Demo"

    # ── Symbol to trade
    "symbol": "EURUSD",

    # ── Timeframes
    "htf": mt5.TIMEFRAME_H4,   # Higher TF — structure analysis
    "ltf": mt5.TIMEFRAME_M15,  # Lower TF — entry signals

    # ── Risk management
    "risk_pct":     1.0,        # % of balance risked per trade
    "min_rr":       2.0,        # Minimum Risk : Reward ratio
    "max_spread":   20,         # Max allowed spread in points
    "max_trades":   3,          # Max concurrent bot trades

    # ── Strategy parameters
    "swing_lookback":   10,     # Bars each side for swing detection
    "ob_lookback":      50,     # Bars to scan back for Order Blocks
    "fvg_min_pips":     2.0,    # Minimum FVG size in pips
    "sr_lookback":      150,    # Bars to scan for S&R levels
    "sl_buffer_pips":   5.0,    # Buffer pips beyond OB for SL
    "min_confluence":   3,      # Min confluence score to execute

    # ── Kill zones (UTC hours) — bot only trades inside these windows
    "kill_zones": [
        {"name": "London Open",   "start": 7,  "end": 10},
        {"name": "New York Open", "start": 12, "end": 15},
    ],

    # ── Bot internals
    "scan_interval": 60,        # Seconds between analysis scans
    "magic_number":  20250101,  # Unique ID for this bot's trades
    "comment":       "ICT_SMC_v1",

    # ── Dashboard
    "port": 5000,
}


# ══════════════════════════════════════════════════════════════
#  LOGGING
# ══════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ICT_Bot")


# ══════════════════════════════════════════════════════════════
#  SHARED STATE  (read by Flask API)
# ══════════════════════════════════════════════════════════════

state = {
    "running":      False,
    "connected":    False,
    "market_bias":  "NEUTRAL",
    "last_scan":    None,
    "error":        None,
    "account":      {},
    "equity_curve": [],   # [{time, equity}, …]  capped at 500 points
    "ob_count":     0,
    "fvg_count":    0,
}

active_signals:  list[dict] = []
open_trades:     list[dict] = []
trade_history:   list[dict] = []
sr_cache:        list[dict] = []


# ══════════════════════════════════════════════════════════════
#  MT5 CONNECTION
# ══════════════════════════════════════════════════════════════

def connect_mt5(login=None, password=None, server=None) -> bool:
    if not mt5.initialize():
        log.error(f"MT5 initialize failed: {mt5.last_error()}")
        return False

    if login and password and server:
        ok = mt5.login(int(login), password=str(password), server=str(server))
        if not ok:
            log.error(f"MT5 login failed: {mt5.last_error()}")
            mt5.shutdown()
            return False

    acct = mt5.account_info()
    if acct is None:
        log.error("MT5: Cannot get account info — are you logged in?")
        mt5.shutdown()
        return False

    state["connected"] = True
    state["account"] = _fmt_account(acct)
    log.info(f"MT5 connected │ {acct.company} │ #{acct.login} │ {acct.balance:.2f} {acct.currency}")
    return True


def _fmt_account(acct) -> dict:
    return {
        "login":       acct.login,
        "balance":     round(acct.balance, 2),
        "equity":      round(acct.equity, 2),
        "margin":      round(acct.margin, 2),
        "free_margin": round(acct.margin_free, 2),
        "profit":      round(acct.profit, 2),
        "currency":    acct.currency,
        "leverage":    acct.leverage,
        "broker":      acct.company,
    }


# ══════════════════════════════════════════════════════════════
#  MARKET DATA
# ══════════════════════════════════════════════════════════════

def get_ohlcv(symbol: str, timeframe, n: int = 250) -> pd.DataFrame | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0:
        log.warning(f"No data for {symbol} tf={timeframe}: {mt5.last_error()}")
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    return df.set_index("time")


def get_tick(symbol: str):
    return mt5.symbol_info_tick(symbol)


def get_pip_size(symbol: str) -> float:
    info = mt5.symbol_info(symbol)
    return (info.point * 10) if info else 0.0001


# ══════════════════════════════════════════════════════════════
#  SWING POINT DETECTION
# ══════════════════════════════════════════════════════════════

def find_swings(df: pd.DataFrame, n: int = 10) -> tuple[list, list]:
    """
    Swing High: bar with the highest High in a ±n bar window.
    Swing Low : bar with the lowest  Low  in a ±n bar window.
    """
    highs, lows = [], []
    ah = df["high"].values
    al = df["low"].values
    idx = df.index

    for i in range(n, len(df) - n):
        if ah[i] == ah[i - n : i + n + 1].max():
            highs.append({"i": i, "time": str(idx[i]), "price": float(ah[i])})
        if al[i] == al[i - n : i + n + 1].min():
            lows.append({"i": i, "time": str(idx[i]), "price": float(al[i])})

    return highs, lows


# ══════════════════════════════════════════════════════════════
#  MARKET STRUCTURE (HTF)
# ══════════════════════════════════════════════════════════════

def get_market_structure(df: pd.DataFrame) -> tuple[str, list, list]:
    """
    Determine HTF bias: BULLISH | BEARISH | NEUTRAL.

    Logic:
      BULLISH  → HH + HL sequence  OR  BOS above last swing high
      BEARISH  → LH + LL sequence  OR  BOS below last swing low
    """
    n = CONFIG["swing_lookback"]
    sh, sl = find_swings(df, n)

    if len(sh) < 3 or len(sl) < 3:
        return "NEUTRAL", sh, sl

    sh = sorted(sh, key=lambda x: x["i"])
    sl = sorted(sl, key=lambda x: x["i"])

    close = float(df["close"].iloc[-1])
    last_sh = sh[-1]["price"]
    last_sl = sl[-1]["price"]

    # Break of Structure
    bos_bull = close > last_sh
    bos_bear = close < last_sl

    # Pattern: last 3 swing points
    r_sh, r_sl = sh[-3:], sl[-3:]
    hh = all(r_sh[i+1]["price"] > r_sh[i]["price"] for i in range(2))
    hl = all(r_sl[i+1]["price"] > r_sl[i]["price"] for i in range(2))
    lh = all(r_sh[i+1]["price"] < r_sh[i]["price"] for i in range(2))
    ll = all(r_sl[i+1]["price"] < r_sl[i]["price"] for i in range(2))

    if bos_bull or (hh and hl):
        return "BULLISH", sh, sl
    if bos_bear or (lh and ll):
        return "BEARISH", sh, sl
    return "NEUTRAL", sh, sl


# ══════════════════════════════════════════════════════════════
#  ORDER BLOCK DETECTION
# ══════════════════════════════════════════════════════════════

def find_order_blocks(df: pd.DataFrame, bias: str) -> list[dict]:
    """
    Bullish OB : Last bearish candle before a strong bullish impulse.
    Bearish OB : Last bullish candle before a strong bearish impulse.
    Unmitigated: price has not yet closed back through the OB.
    """
    obs = []
    lookback = CONFIG["ob_lookback"]
    sub = df.iloc[-lookback:].reset_index()
    current = float(df["close"].iloc[-1])

    for i in range(2, len(sub) - 4):
        c = sub.iloc[i]
        body = abs(c["close"] - c["open"])
        rng  = c["high"] - c["low"]
        if rng == 0 or body / rng < 0.25:
            continue  # Skip dojis

        future = sub.iloc[i+1 : i+5]

        # Bullish OB
        if bias in ("BULLISH", "NEUTRAL") and c["close"] < c["open"]:
            momentum = (future["close"].max() - c["high"]) / rng
            if momentum > 0.5:
                obs.append({
                    "type":      "BULLISH_OB",
                    "time":      str(sub.iloc[i]["time"]),
                    "high":      round(float(c["high"]), 5),
                    "low":       round(float(c["low"]),  5),
                    "mid":       round((float(c["high"]) + float(c["low"])) / 2, 5),
                    "mitigated": current < float(c["low"]),
                })

        # Bearish OB
        if bias in ("BEARISH", "NEUTRAL") and c["close"] > c["open"]:
            momentum = (c["low"] - future["close"].min()) / rng
            if momentum > 0.5:
                obs.append({
                    "type":      "BEARISH_OB",
                    "time":      str(sub.iloc[i]["time"]),
                    "high":      round(float(c["high"]), 5),
                    "low":       round(float(c["low"]),  5),
                    "mid":       round((float(c["high"]) + float(c["low"])) / 2, 5),
                    "mitigated": current > float(c["high"]),
                })

    return [ob for ob in obs if not ob["mitigated"]]


# ══════════════════════════════════════════════════════════════
#  FAIR VALUE GAP (FVG / IMBALANCE)
# ══════════════════════════════════════════════════════════════

def find_fvg(df: pd.DataFrame) -> list[dict]:
    """
    3-candle imbalance pattern.
    Bullish FVG : candle[i-1].high < candle[i+1].low
    Bearish FVG : candle[i-1].low  > candle[i+1].high
    Unfilled only (price hasn't retraced back through).
    """
    fvgs = []
    pip     = get_pip_size(CONFIG["symbol"])
    min_sz  = CONFIG["fvg_min_pips"] * pip
    lookback = 100
    sub     = df.iloc[-lookback:]
    current = float(df["close"].iloc[-1])

    for i in range(1, len(sub) - 1):
        prev = sub.iloc[i - 1]
        nxt  = sub.iloc[i + 1]

        # Bullish FVG
        top, bottom = float(nxt["low"]), float(prev["high"])
        if top > bottom and (top - bottom) >= min_sz:
            fvgs.append({
                "type":      "BULLISH_FVG",
                "time":      str(sub.index[i]),
                "top":       round(top, 5),
                "bottom":    round(bottom, 5),
                "mid":       round((top + bottom) / 2, 5),
                "size_pips": round((top - bottom) / pip, 1),
                "filled":    current < bottom,
            })

        # Bearish FVG
        top, bottom = float(prev["low"]), float(nxt["high"])
        if top > bottom and (top - bottom) >= min_sz:
            fvgs.append({
                "type":      "BEARISH_FVG",
                "time":      str(sub.index[i]),
                "top":       round(top, 5),
                "bottom":    round(bottom, 5),
                "mid":       round((top + bottom) / 2, 5),
                "size_pips": round((top - bottom) / pip, 1),
                "filled":    current > top,
            })

    return [f for f in fvgs if not f["filled"]]


# ══════════════════════════════════════════════════════════════
#  SUPPORT & RESISTANCE LEVELS
# ══════════════════════════════════════════════════════════════

def find_sr_levels(df: pd.DataFrame) -> list[dict]:
    """
    Cluster swing highs/lows into key S&R zones.
    Strength = number of touches within the cluster tolerance.
    """
    pip = get_pip_size(CONFIG["symbol"])
    tol = 10 * pip   # 10-pip cluster tolerance

    sub = df.iloc[-CONFIG["sr_lookback"]:]
    sh, sl = find_swings(sub, n=7)

    raw = sorted(p["price"] for p in sh + sl)
    if not raw:
        return []

    # Cluster
    clusters: list[list[float]] = []
    group = [raw[0]]
    for p in raw[1:]:
        if p - group[0] <= tol:
            group.append(p)
        else:
            clusters.append(group)
            group = [p]
    clusters.append(group)

    current = float(df["close"].iloc[-1])
    levels  = []
    for grp in clusters:
        lvl = float(np.mean(grp))
        levels.append({
            "price":     round(lvl, 5),
            "type":      "RESISTANCE" if lvl > current else "SUPPORT",
            "strength":  len(grp),
            "dist_pips": round(abs(lvl - current) / pip, 1),
        })

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
    now  = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    result = []
    for kz in CONFIG["kill_zones"]:
        active = kz["start"] <= h < kz["end"]
        if active:
            mins_left = (kz["end"] - h) * 60 - m
            result.append({**kz, "active": True, "mins_left": int(mins_left)})
        else:
            if h < kz["start"]:
                mins_until = (kz["start"] - h) * 60 - m
            else:
                mins_until = (24 - h + kz["start"]) * 60 - m
            result.append({**kz, "active": False, "mins_until": int(mins_until)})
    return result


# ══════════════════════════════════════════════════════════════
#  SIGNAL GENERATION  (ICT/SMC confluence scoring)
# ══════════════════════════════════════════════════════════════

def generate_signals(
    symbol: str,
    bias: str,
    obs: list[dict],
    fvgs: list[dict],
    sr:   list[dict],
) -> list[dict]:
    """
    Score each unmitigated Order Block against:
      +1  HTF bias alignment
      +1  Price entering OB zone
      +1  Overlapping FVG (liquidity void)
      +1  Nearby S&R level (within 20 pips)
      +1  Inside Kill Zone

    Minimum score == CONFIG["min_confluence"] to produce a signal.
    """
    tick = get_tick(symbol)
    if tick is None:
        return []

    bid, ask = float(tick.bid), float(tick.ask)
    pip = get_pip_size(symbol)
    buf = CONFIG["sl_buffer_pips"] * pip
    is_kz, kz_name = in_kill_zone()
    signals = []

    for ob in obs[-20:]:
        oh, ol = ob["high"], ob["low"]
        conf: list[str] = []

        # ── BULLISH setup ─────────────────────────────────────
        if ob["type"] == "BULLISH_OB" and bias == "BULLISH":
            if ol <= ask <= oh:
                conf.append("HTF Bullish Bias")
                conf.append("Price in Bullish OB")

                for fvg in fvgs:
                    if fvg["type"] == "BULLISH_FVG" and fvg["bottom"] <= oh and fvg["top"] >= ol:
                        conf.append(f"Bullish FVG ({fvg['size_pips']} pips)")
                        break

                for lvl in sr[:6]:
                    if lvl["type"] == "SUPPORT" and lvl["dist_pips"] < 20:
                        conf.append(f"Support @ {lvl['price']:.5f}")
                        break

                if is_kz:
                    conf.append(f"Kill Zone: {kz_name}")

                if len(conf) >= CONFIG["min_confluence"]:
                    sl = ol - buf
                    tp = ask + (ask - sl) * CONFIG["min_rr"]
                    signals.append(_build_signal("BUY", symbol, ask, sl, tp, conf, ob, is_kz, pip))

        # ── BEARISH setup ─────────────────────────────────────
        elif ob["type"] == "BEARISH_OB" and bias == "BEARISH":
            if ol <= bid <= oh:
                conf.append("HTF Bearish Bias")
                conf.append("Price in Bearish OB")

                for fvg in fvgs:
                    if fvg["type"] == "BEARISH_FVG" and fvg["bottom"] <= oh and fvg["top"] >= ol:
                        conf.append(f"Bearish FVG ({fvg['size_pips']} pips)")
                        break

                for lvl in sr[:6]:
                    if lvl["type"] == "RESISTANCE" and lvl["dist_pips"] < 20:
                        conf.append(f"Resistance @ {lvl['price']:.5f}")
                        break

                if is_kz:
                    conf.append(f"Kill Zone: {kz_name}")

                if len(conf) >= CONFIG["min_confluence"]:
                    sl = oh + buf
                    tp = bid - (sl - bid) * CONFIG["min_rr"]
                    signals.append(_build_signal("SELL", symbol, bid, sl, tp, conf, ob, is_kz, pip))

    return sorted(signals, key=lambda x: x["score"], reverse=True)


def _build_signal(
    direction: str, symbol: str,
    entry: float, sl: float, tp: float,
    conf: list[str], ob: dict,
    is_kz: bool, pip: float,
) -> dict:
    return {
        "id":         f"{direction}_{int(time.time())}",
        "symbol":     symbol,
        "direction":  direction,
        "entry":      round(entry, 5),
        "sl":         round(sl, 5),
        "tp":         round(tp, 5),
        "risk_pips":  round(abs(entry - sl) / pip, 1),
        "rr":         CONFIG["min_rr"],
        "score":      len(conf),
        "confluence": conf,
        "ob":         ob,
        "in_kz":      is_kz,
        "time":       datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════
#  RISK-BASED LOT SIZING
# ══════════════════════════════════════════════════════════════

def calc_lot_size(symbol: str, entry: float, sl: float) -> float:
    acct = mt5.account_info()
    info = mt5.symbol_info(symbol)
    if not acct or not info:
        return 0.01

    risk_amt  = acct.balance * (CONFIG["risk_pct"] / 100)
    sl_pips   = abs(entry - sl) / (10 * info.point)
    pip_value = info.trade_tick_value * (info.trade_tick_size / info.point) * 10

    if sl_pips == 0 or pip_value == 0:
        return info.volume_min

    raw  = risk_amt / (sl_pips * pip_value)
    lots = round(raw / info.volume_step) * info.volume_step
    lots = max(info.volume_min, min(info.volume_max, lots))
    return round(lots, 2)


# ══════════════════════════════════════════════════════════════
#  TRADE EXECUTION
# ══════════════════════════════════════════════════════════════

def place_order(sig: dict) -> dict | None:
    symbol = sig["symbol"]
    info   = mt5.symbol_info(symbol)

    if not info or not info.visible:
        mt5.symbol_select(symbol, True)
        time.sleep(0.5)
        info = mt5.symbol_info(symbol)
    if not info:
        log.error(f"Symbol {symbol} not found")
        return None

    if info.spread > CONFIG["max_spread"]:
        log.warning(f"Spread {info.spread} > {CONFIG['max_spread']} pts. Skipping.")
        return None

    positions = mt5.positions_get(symbol=symbol) or []
    bot_pos   = [p for p in positions if p.magic == CONFIG["magic_number"]]
    if len(bot_pos) >= CONFIG["max_trades"]:
        log.info(f"Max trades ({CONFIG['max_trades']}) reached. Skipping.")
        return None

    tick  = get_tick(symbol)
    if not tick:
        return None
    price = tick.ask if sig["direction"] == "BUY" else tick.bid
    lots  = calc_lot_size(symbol, price, sig["sl"])

    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       symbol,
        "volume":       lots,
        "type":         mt5.ORDER_TYPE_BUY if sig["direction"] == "BUY" else mt5.ORDER_TYPE_SELL,
        "price":        price,
        "sl":           sig["sl"],
        "tp":           sig["tp"],
        "deviation":    20,
        "magic":        CONFIG["magic_number"],
        "comment":      CONFIG["comment"],
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    res = mt5.order_send(req)
    if res.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Order failed [{res.retcode}]: {res.comment}")
        return None

    trade = {
        "ticket":    res.order,
        "symbol":    symbol,
        "direction": sig["direction"],
        "entry":     round(price, 5),
        "sl":        sig["sl"],
        "tp":        sig["tp"],
        "lots":      lots,
        "pnl":       0.0,
        "status":    "OPEN",
        "time":      datetime.now(timezone.utc).isoformat(),
        "confluence": sig["confluence"],
    }
    trade_history.append(trade)
    log.info(f"✅ {sig['direction']} {symbol} @ {price:.5f} | SL {sig['sl']:.5f} | TP {sig['tp']:.5f} | {lots} lots")
    return trade


def close_position(ticket: int) -> bool:
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    p = pos[0]
    t = get_tick(p.symbol)
    if not t:
        return False
    price = t.bid if p.type == 0 else t.ask
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       p.symbol,
        "volume":       p.volume,
        "type":         mt5.ORDER_TYPE_SELL if p.type == 0 else mt5.ORDER_TYPE_BUY,
        "position":     ticket,
        "price":        price,
        "deviation":    20,
        "magic":        CONFIG["magic_number"],
        "comment":      "Bot close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(req)
    return res.retcode == mt5.TRADE_RETCODE_DONE


# ══════════════════════════════════════════════════════════════
#  REFRESH OPEN TRADES FROM MT5
# ══════════════════════════════════════════════════════════════

def refresh_open_trades():
    global open_trades
    positions = mt5.positions_get() or []
    open_trades = [
        {
            "ticket":    p.ticket,
            "symbol":    p.symbol,
            "direction": "BUY" if p.type == 0 else "SELL",
            "entry":     round(p.price_open, 5),
            "current":   round(p.price_current, 5),
            "sl":        round(p.sl, 5),
            "tp":        round(p.tp, 5),
            "lots":      p.volume,
            "pnl":       round(p.profit, 2),
            "swap":      round(p.swap, 2),
            "time":      datetime.fromtimestamp(p.time, tz=timezone.utc).isoformat(),
            "comment":   p.comment,
        }
        for p in positions
        if p.magic == CONFIG["magic_number"]
    ]


# ══════════════════════════════════════════════════════════════
#  MAIN BOT LOOP
# ══════════════════════════════════════════════════════════════

def bot_loop():
    global active_signals, sr_cache
    log.info("═" * 55)
    log.info("  Bot loop started  │  Strategy: ICT/SMC + S&R")
    log.info("═" * 55)

    while state["running"]:
        try:
            symbol = CONFIG["symbol"]

            # Refresh account
            acct = mt5.account_info()
            if acct:
                state["account"] = _fmt_account(acct)
                state["equity_curve"].append({
                    "time":   datetime.now(timezone.utc).isoformat(),
                    "equity": acct.equity,
                })
                state["equity_curve"] = state["equity_curve"][-500:]

            # Fetch candles
            df_htf = get_ohlcv(symbol, CONFIG["htf"], 250)
            df_ltf = get_ohlcv(symbol, CONFIG["ltf"], 250)
            if df_htf is None or df_ltf is None:
                log.warning("Market data unavailable — retrying in 30s")
                time.sleep(30)
                continue

            # Analyse
            bias, _, _ = get_market_structure(df_htf)
            state["market_bias"] = bias

            obs  = find_order_blocks(df_ltf, bias)
            fvgs = find_fvg(df_ltf)
            sr   = find_sr_levels(df_htf)

            state["ob_count"]  = len(obs)
            state["fvg_count"] = len(fvgs)
            sr_cache           = sr

            # Generate signals
            sigs = generate_signals(symbol, bias, obs, fvgs, sr)
            active_signals = sigs

            # Execute — only inside kill zones, take the highest-scored signal
            is_kz, kz_name = in_kill_zone()
            if sigs:
                best = sigs[0]
                if is_kz and best["score"] >= CONFIG["min_confluence"]:
                    place_order(best)
                else:
                    reason = "outside kill zone" if not is_kz else f"score {best['score']} < {CONFIG['min_confluence']}"
                    log.info(f"Signal queued ({reason}): {best['direction']} {symbol}")

            # Refresh open positions
            refresh_open_trades()

            state["last_scan"] = datetime.now(timezone.utc).isoformat()
            state["error"]     = None

            log.info(
                f"Bias:{bias:8s} │ OBs:{len(obs):2d} │ FVGs:{len(fvgs):2d} │ "
                f"S&R:{len(sr):2d} │ Sigs:{len(sigs):2d} │ KZ:{'✓ '+kz_name if is_kz else '—'}"
            )

            time.sleep(CONFIG["scan_interval"])

        except Exception as exc:
            log.exception(f"Bot error: {exc}")
            state["error"] = str(exc)
            time.sleep(30)

    log.info("Bot loop stopped.")


# ══════════════════════════════════════════════════════════════
#  FLASK API
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app)


@app.route("/")
def index():
    return (
        "<h2 style='font-family:monospace'>ICT/SMC Bot API</h2>"
        f"<p>Open <b>dashboard.html</b> in your browser.</p>"
        f"<p>Status: <b>{'Running' if state['running'] else 'Idle'}</b></p>"
    )


@app.route("/api/status")
def api_status():
    return jsonify({
        **state,
        "open_trades":    open_trades,
        "active_signals": active_signals,
        "kill_zones":     kill_zone_status(),
        "sr_levels":      sr_cache[:6],
    })


@app.route("/api/history")
def api_history():
    return jsonify({"trades": trade_history[-100:]})


@app.route("/api/equity")
def api_equity():
    return jsonify({"curve": state["equity_curve"]})


@app.route("/api/start", methods=["POST"])
def api_start():
    if not state["connected"]:
        return jsonify({"error": "MT5 not connected"}), 400
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
    if request.method == "POST":
        data = request.get_json(force=True)
        editable = ["risk_pct", "min_rr", "max_trades", "symbol",
                    "scan_interval", "min_confluence", "fvg_min_pips"]
        for k in editable:
            if k in data:
                CONFIG[k] = data[k]
        return jsonify({"message": "Config updated", "config": {k: CONFIG[k] for k in editable}})
    safe = {k: v for k, v in CONFIG.items() if k not in ("password",)}
    return jsonify(safe)


@app.route("/api/close/<int:ticket>", methods=["POST"])
def api_close(ticket):
    if close_position(ticket):
        refresh_open_trades()
        return jsonify({"message": f"#{ticket} closed"})
    return jsonify({"error": "Close failed — check MT5"}), 400


@app.route("/api/closeall", methods=["POST"])
def api_close_all():
    closed = sum(1 for t in list(open_trades) if close_position(t["ticket"]))
    refresh_open_trades()
    return jsonify({"message": f"Closed {closed} trade(s)"})


# ══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════════════╗
║        ICT / SMC + S&R Trading Bot for MT5          ║
║   Order Blocks · FVG · Structure · Kill Zones       ║
╚══════════════════════════════════════════════════════╝
    """)

    if connect_mt5(
        CONFIG.get("login"),
        CONFIG.get("password"),
        CONFIG.get("server"),
    ):
        a = state["account"]
        print(f"  ✅  Connected  │  {a.get('broker','—')}")
        print(f"  💰  Balance    │  {a.get('balance',0):.2f} {a.get('currency','')}")
        print(f"  📊  Symbol     │  {CONFIG['symbol']}")
        print(f"  🌐  Dashboard  │  http://localhost:{CONFIG['port']}")
        print(f"\n  Use the dashboard or POST /api/start to begin trading.\n")

        app.run(
            host="0.0.0.0",
            port=CONFIG["port"],
            debug=False,
            use_reloader=False,
        )
    else:
        print("  ❌  MT5 connection failed.")
        print("  ➜  Make sure MetaTrader 5 is open and you are logged in.")
        sys.exit(1)
