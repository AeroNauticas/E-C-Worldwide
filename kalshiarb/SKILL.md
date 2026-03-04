---
name: cross-arb-15m-kalshi
description: "Cross-asset correlation arbitrage on Kalshi 15-minute BTC/ETH UP/DOWN markets. Buys correlated pairs (BTC UP + ETH DOWN, BTC DOWN + ETH UP) when combined price < $0.95 for positive expected value. Runs autonomously with risk management, dry-run mode, and full trade logging."
homepage: https://kalshi.com/category/crypto/15-min
metadata:
  openclaw:
    emoji: "🦞"
    requires:
      bins:
        - python3
        - pip
      env:
        - KALSHI_API_KEY_ID
        - KALSHI_PRIVATE_KEY_PATH
      optionalEnv:
        - KALSHI_PRIVATE_KEY_PEM
        - KALSHI_BASE_URL
        - MAX_COMBINED_PRICE
        - POSITION_SIZE_USD
        - MAX_OPEN_POSITIONS
        - MAX_DAILY_LOSS_USD
        - DRY_RUN
        - POLL_INTERVAL_SECONDS
---

# Cross-Asset Correlation Arbitrage — Kalshi 15-Minute Markets

Autonomous bot that exploits pricing inefficiencies between BTC and ETH 15-minute UP/DOWN prediction markets on Kalshi.

## What This Skill Does

Scans Kalshi's 15-minute crypto candle markets every 2 seconds, looking for cross-asset pairs where the combined buy price is below a threshold (default $0.95). When found, it buys both legs simultaneously.

**How it maps to Kalshi:**
- **BTC UP**   = BUY YES on `KXBTC15M` (current market)
- **BTC DOWN** = BUY NO  on `KXBTC15M` (same market, opposite side)
- **ETH UP**   = BUY YES on `KXETH15M`
- **ETH DOWN** = BUY NO  on `KXETH15M`

**Pairs traded:**
- **Pair A:** BTC YES + ETH NO  (betting: BTC up, ETH down — wins when correlated)
- **Pair B:** BTC NO  + ETH YES (betting: BTC down, ETH up — wins when correlated)

Because BTC and ETH are ~85-90% correlated, the most likely outcome is that both move in the same direction — meaning one leg wins and pays $1.00. If you entered at $0.93 combined, that's a $0.07 profit per trade, ~85% of the time.

Running both pairs simultaneously hedges further — the losing scenario for one pair is the winning scenario for the other.

## Setup

### Step 1: Install dependencies

```bash
pip install -r requirements.txt
```

Only 3 dependencies: `requests`, `cryptography`, `python-dotenv`. No Kalshi SDK needed — the bot uses the REST API directly.

### Step 2: Configure environment variables

Required:
```bash
export KALSHI_API_KEY_ID="your-api-key-id"
export KALSHI_PRIVATE_KEY_PATH="/path/to/your/kalshi-key.pem"
```

Alternative (inline PEM):
```bash
export KALSHI_PRIVATE_KEY_PEM="-----BEGIN RSA PRIVATE KEY-----\n..."
```

Optional (with defaults):
```bash
export KALSHI_BASE_URL="https://api.elections.kalshi.com/trade-api/v2"  # or demo-api.kalshi.co for paper
export MAX_COMBINED_PRICE="0.95"           # Max combined entry price
export POSITION_SIZE_USD="5.0"             # USD per pair trade
export MAX_OPEN_POSITIONS="3"              # Max concurrent pairs
export MAX_DAILY_LOSS_USD="25.0"           # Daily loss circuit breaker
export DRY_RUN="true"                      # Set "false" for live trading
export POLL_INTERVAL_SECONDS="2.0"         # Scan frequency
```

To get your API key: Kalshi → Settings → API Keys → Generate API Key. This gives you a Key ID and a downloadable `.pem` private key file.

### Step 3: Run the scanner (no auth needed)

```bash
python3 scanner.py
```

This reads the public order book and shows current cross-arb opportunities without placing any orders.

### Step 4: Run in dry-run mode

```bash
python3 bot.py
```

Logs all detected opportunities and simulated trades without placing real orders. Review `trades.jsonl` to validate the strategy.

### Step 5: Paper trade on Kalshi demo

```bash
export KALSHI_BASE_URL="https://demo-api.kalshi.co/trade-api/v2"
export DRY_RUN="false"
python3 bot.py
```

### Step 6: Go live

```bash
export KALSHI_BASE_URL="https://api.elections.kalshi.com/trade-api/v2"
export DRY_RUN="false"
python3 bot.py
```

## Key Differences from Polymarket Version

| Aspect | Polymarket (original) | Kalshi (this version) |
|--------|----------------------|----------------------|
| Interval | 5 minutes | 15 minutes |
| Market structure | Separate UP/DOWN tokens | Single binary market (YES=UP, NO=DOWN) |
| Order book | Unified book per market | Bids only; YES ask = 1.00 - NO bid |
| Prices | Dollars (0.00–1.00) | Cents (1–99) internally, dollars in API |
| Auth | Ethereum wallet signing | RSA-PSS key pair |
| SDK | py-clob-client | Direct REST (no SDK needed) |
| Chain | Polygon (on-chain) | Centralized (CFTC-regulated) |
| Fees | ~0% (recently added 3% taker on 15m) | ~2% |
| Discovery | Gamma API by slug | REST API by series_ticker |
| Settlement | UMA oracle | Automatic (CF Benchmarks) |

## Strategy Details

### Outcome Matrix (example: BTC YES + ETH NO at $0.92 combined)

| BTC   | ETH   | Result       | Payout | P&L     |
|-------|-------|-------------|--------|---------|
| ↑     | ↓     | Win both    | $2.00  | +$1.08  |
| ↑     | ↑     | Win Leg A   | $1.00  | +$0.08  |
| ↓     | ↓     | Win Leg B   | $1.00  | +$0.08  |
| ↓     | ↑     | Lose both   | $0.00  | -$0.92  |

Rows 2 and 3 (correlated movement) happen ~85-90% of the time. Row 4 (anti-correlated) happens ~5-10% of the time.

### Risk Management Built In
- **Daily loss cap** — stops trading after configurable daily loss (default $25)
- **Position limits** — max 3 concurrent open pairs
- **Loss streak cooldown** — pauses 5 minutes after 5 consecutive losses
- **Time guards** — won't enter with < 60 seconds left in a candle
- **Liquidity checks** — skips if order book can't fill the position size
- **Partial fill handling** — cancels orphaned leg if only one side fills

## Commands

- "Start the cross-arb bot" → runs `python3 bot.py`
- "Start in dry run mode" → runs with `DRY_RUN=true`
- "Start live trading" → runs with `DRY_RUN=false`
- "Paper trade on demo" → runs with `KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2`
- "Show recent trades" → reads `trades.jsonl`
- "What's the current edge on BTC/ETH 15min markets?" → runs `python3 scanner.py`
- "Stop the bot" → sends SIGTERM to the running process

## Output Files

- `cross_arb_bot.log` — Full activity log
- `trades.jsonl` — Machine-readable trade log (one JSON object per line)

## Warnings

⚠️ **This trades real money when DRY_RUN=false.** Understand the risks:
- BTC/ETH correlation can break during major events
- Partial fills can leave you with unhedged exposure
- Kalshi fees (~2%) reduce your edge
- 15-minute windows are more volatile than 5-minute ones
- Kalshi has trading hours — markets may not be open 24/7

**Never trade more than you can afford to lose.**
