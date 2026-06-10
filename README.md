# ICT/SMC + S&R Trading Bot

**Stack:** AWS EC2 Windows (MT5) → MetaApi → Render (Python Bot) ← GitHub

---

## Architecture

```
┌─────────────────────┐     MetaApi      ┌──────────────────────┐
│  AWS EC2 Windows    │ ←── bridge ────→ │  Render (Linux)      │
│  MT5 Terminal       │                  │  Python Bot          │
│  (watch trades      │                  │  Flask Dashboard     │
│   live here)        │                  │  /api/* endpoints    │
└─────────────────────┘                  └──────────────────────┘
                                                   ↑
                                          GitHub auto-deploy
```

MetaApi bridges your MT5 account to the Python bot on Render's Linux servers.

---

## Step 1 — MetaApi Setup

1. Sign up at **https://metaapi.cloud** (free tier available)
2. Go to **MT Accounts → Add Account**
3. Enter your broker credentials:
   - Login (account number), Password, Server (e.g. `ICMarkets-Demo`), Platform: `MT5`
4. Wait for status → **DEPLOYED**
5. Go to **API Access → Auth Tokens → Generate Token**
6. Copy your `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID`

> Your AWS EC2 MT5 terminal is for visual monitoring. MetaApi connects directly
> to your broker's server — MT5 on EC2 does not need to be running for trades.

---

## Step 2 — GitHub Setup

Push these files to a **private** repo:

```
TradingAI/
├── mt5_bot_cloud.py      ← main bot (Render runs this)
├── dashboard.html        ← dashboard (served by Flask at /)
├── requirements.txt      ← Python dependencies
├── render.yaml           ← Render auto-config
├── .env.example          ← env var template (safe to commit)
└── README.md
```

```bash
git init
git add .
git commit -m "Initial bot setup"
git remote add origin https://github.com/YOUR_USERNAME/TradingAI.git
git push -u origin main
```

**Add `.env` to your `.gitignore`** — never commit real credentials.

---

## Step 3 — Render Deployment

1. **https://render.com** → New → Web Service → connect GitHub repo
2. Render auto-reads `render.yaml`
3. Go to **Environment** and add these two secrets:

   | Key | Value |
   |---|---|
   | `METAAPI_TOKEN` | from MetaApi dashboard |
   | `METAAPI_ACCOUNT_ID` | from MetaApi dashboard |

4. Click **Deploy** — build takes ~2 minutes
5. Visit your Render URL → dashboard loads ✓

> **Use Starter plan ($7/mo).** Free tier spins down after 15 min of inactivity
> which would kill the bot mid-session.

---

## Step 4 — Start the Bot

1. Open your Render URL (e.g. `https://ict-smc-bot.onrender.com`)
2. Check connection badge shows **LIVE**
3. Click **▶ START**
4. Bot scans every 60s, executes during London (07–10 UTC) and NY (12–15 UTC) kill zones only

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `METAAPI_TOKEN` | **required** | MetaApi auth token |
| `METAAPI_ACCOUNT_ID` | **required** | MT5 account ID on MetaApi |
| `TRADING_SYMBOL` | `EURUSD` | Symbol to trade |
| `HTF_TIMEFRAME` | `4h` | Structure timeframe |
| `LTF_TIMEFRAME` | `15m` | Entry timeframe |
| `RISK_PCT` | `1.0` | % of balance per trade |
| `MIN_RR` | `2.0` | Min risk:reward |
| `MAX_TRADES` | `3` | Max concurrent positions |
| `MIN_CONFLUENCE` | `3` | Min ICT/SMC score to execute |
| `MAX_SPREAD` | `20` | Max spread (points) |
| `SCAN_INTERVAL` | `60` | Seconds between scans |

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/` | Trading dashboard UI |
| GET | `/health` | Health check (used by Render) |
| GET | `/api/status` | Bot state, signals, open trades |
| GET | `/api/history` | Last 100 closed trades |
| GET | `/api/equity` | Equity curve data |
| POST | `/api/start` | Start bot loop |
| POST | `/api/stop` | Stop bot loop |
| GET/POST | `/api/config` | Read or update settings |
| POST | `/api/close/<id>` | Close a position |
| POST | `/api/closeall` | Close all bot positions |

---

## Strategy Components

| Component | How It Works |
|---|---|
| **Market Structure** | H4 swing points → HH+HL = Bullish, LH+LL = Bearish, BOS confirmation |
| **Order Blocks** | Last opposing candle before impulsive move (unmitigated only) |
| **Fair Value Gaps** | 3-candle imbalance, unfilled only |
| **Support & Resistance** | Clustered swing highs/lows with touch-count strength |
| **Kill Zone Filter** | London 07–10 UTC · NY Open 12–15 UTC |
| **Confluence Score** | 3+/5 required: HTF Bias + OB + FVG + S&R + Kill Zone |
| **Lot Sizing** | Auto: balance × risk% ÷ (SL pips × pip value) |

---

## EC2 Windows VPS Tips

Your EC2 instance is valuable for:
- **Watching live trades** in the MT5 terminal as the bot executes
- **Manual intervention** — adjust SL/TP or close trades directly in MT5
- **Running the Windows bot** (`mt5_trading_bot.py`) locally for minimum latency:
  ```bash
  pip install MetaTrader5 pandas numpy flask flask-cors
  python mt5_trading_bot.py
  ```

---

## ⚠️ Risk Warning

Always test on a **demo account** for at least 2 weeks before going live.
Start with `RISK_PCT=0.5` or lower. Never risk money you cannot afford to lose.
