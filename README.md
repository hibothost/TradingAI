# ICT/SMC + S&R Trading Bot for MetaTrader 5

## Strategy
Combines three ICT/Smart Money Concept principles:
- **Order Blocks (OB)** — Last opposing candle before an impulsive move
- **Fair Value Gaps (FVG)** — 3-candle imbalance / liquidity void
- **Support & Resistance** — Clustered swing highs/lows
- **Market Structure** — HTF (H4) Break of Structure for directional bias
- **Kill Zone Filter** — Only executes during London Open (07:00–10:00 UTC) and NY Open (12:00–15:00 UTC)

Minimum 3 confluence factors are required before a trade is placed.

---

## Requirements
- Windows PC (MetaTrader5 Python library is Windows-only)
- MetaTrader 5 terminal installed and open
- Python 3.10+

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure the bot
Open `mt5_trading_bot.py` and edit the CONFIG block at the top:

```python
CONFIG = {
    "login":    None,     # Your MT5 account number  e.g. 12345678
    "password": None,     # Your MT5 password
    "server":   None,     # Your broker's server     e.g. "ICMarkets-Demo"
    "symbol":   "EURUSD", # Symbol to trade
    "risk_pct": 1.0,      # % of balance per trade
    "min_rr":   2.0,      # Minimum Risk:Reward ratio
    ...
}
```

> If you're already logged into MT5, you can leave login/password/server as None.

### 3. Run the bot
```bash
python mt5_trading_bot.py
```

### 4. Open the dashboard
Open `dashboard.html` in your browser. It will automatically connect to the bot at `http://localhost:5000`.

---

## Dashboard Features
| Feature | Description |
|---|---|
| LIVE / DEMO badge | Green = connected to your bot |
| H4 Bias | Current higher timeframe market structure |
| Kill Zones | Countdown to next London / NY open |
| S&R Levels | Nearest support and resistance with strength bars |
| Signals tab | Active ICT/SMC setups with confluence breakdown |
| Equity tab | Live equity curve + drawdown stats |
| History tab | Full trade history with P&L |
| ⚙ Config | Adjust risk %, RR, max trades without restarting |

---

## API Endpoints
The bot exposes a REST API on port 5000:

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/status` | Full bot state, signals, open trades |
| GET | `/api/history` | Last 100 closed trades |
| GET | `/api/equity` | Equity curve data |
| POST | `/api/start` | Start the bot loop |
| POST | `/api/stop` | Stop the bot loop |
| GET/POST | `/api/config` | Get or update config |
| POST | `/api/close/<ticket>` | Close a specific trade |
| POST | `/api/closeall` | Close all bot trades |

---

## Risk Warning
This bot places real trades with real money. Always:
- Test on a **demo account** first
- Start with minimum lot sizes
- Monitor performance regularly
- Never risk more than you can afford to lose

The bot is provided as-is for educational purposes.
