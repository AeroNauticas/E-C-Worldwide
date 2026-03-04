# Cross-Asset Correlation Arbitrage — Strategy Reference (Kalshi 15m)

## Core Concept

BTC and ETH have a historical price correlation of approximately 0.85–0.95.
This means when BTC goes up, ETH almost always goes up too (and vice versa).

Kalshi's 15-minute crypto markets price BTC UP and ETH DOWN as independent
events. But they're NOT independent — they're inversely correlated.

When the market misprices this relationship (combined cost < $1.00), we profit.

## Kalshi Market Mechanics

Unlike Polymarket (which has separate UP and DOWN tokens), Kalshi uses a
single binary market per asset per interval:

- `KXBTC15M` → "Will BTC be up in 15 minutes?"
  - BUY YES = bet BTC goes UP
  - BUY NO  = bet BTC goes DOWN

- `KXETH15M` → "Will ETH be up in 15 minutes?"
  - BUY YES = bet ETH goes UP
  - BUY NO  = bet ETH goes DOWN

**Order book structure:**
Kalshi returns only BIDS (not asks). In a binary market:
- YES ask = $1.00 - best NO bid
- NO ask  = $1.00 - best YES bid

So to find the cost to buy "BTC UP": look at the NO bids on KXBTC15M,
and compute 1.00 - best_no_bid.

**Settlement:**
Kalshi uses CF Benchmarks (same methodology as CME Bitcoin futures)
for price determination. Settlement is automatic — no oracle needed.

## Expected Value Calculation

### Variables
- `c` = combined cost of both legs (e.g., $0.92)
- `p_corr` = probability both assets move same direction ≈ 0.85
- `p_anti_good` = probability of anti-correlation in our favor ≈ 0.05
- `p_anti_bad` = probability of anti-correlation against us ≈ 0.10

### EV Formula
```
EV = p_corr × ($1.00 - c) + p_anti_good × ($2.00 - c) + p_anti_bad × ($0.00 - c)
```

### EV at different entry prices (assuming 85/5/10 split):

| Combined Entry | EV per $1 risked | EV per trade ($5) |
|---------------|------------------|-------------------|
| $0.85         | +$0.0925         | +$0.46            |
| $0.88         | +$0.0625         | +$0.31            |
| $0.90         | +$0.0425         | +$0.21            |
| $0.92         | +$0.0225         | +$0.11            |
| $0.93         | +$0.0125         | +$0.06            |
| $0.95         | -$0.0075         | -$0.04            |
| $0.97         | -$0.0275         | -$0.14            |

### Key insight:
At $0.95, the strategy is approximately breakeven. Below $0.93, it becomes
reliably profitable. The default `max_combined_price` of $0.95 is the
primary gate.

## 15-Minute vs 5-Minute Considerations

The 15-minute window on Kalshi (vs 5-minute on Polymarket) has implications:

1. **More time for decorrelation**: In 15 minutes, BTC and ETH have more
   time to diverge. Correlation may be slightly lower over 15m vs 5m,
   perhaps 0.80-0.90 instead of 0.85-0.95. This slightly reduces edge.

2. **Fewer trades per day**: With 15m intervals, there are 96 candles/day
   vs 288 with 5m. Lower frequency but potentially better pricing.

3. **More liquidity**: 15-minute markets tend to attract more volume,
   which means tighter spreads and better fills.

4. **Higher volatility per candle**: 15 minutes of price action means
   larger moves, which may make the UP/DOWN split less predictable
   for any single candle.

## Dual-Pair Hedging

Running both pairs simultaneously:
- Pair A: BTC YES + ETH NO   (betting BTC up, ETH down)
- Pair B: BTC NO  + ETH YES  (betting BTC down, ETH up)

| BTC | ETH | Pair A | Pair B | Net (both at $0.92) |
|-----|-----|--------|--------|---------------------|
| ↑   | ↑   | Win A  | Win B  | +$0.08 + $0.08 = +$0.16 |
| ↓   | ↓   | Win A  | Win B  | +$0.08 + $0.08 = +$0.16 |
| ↑   | ↓   | Win 2  | Lose 2 | +$1.08 - $0.92 = +$0.16 |
| ↓   | ↑   | Lose 2 | Win 2  | -$0.92 + $1.08 = +$0.16 |

**When both pairs are available at the same combined price, the dual-pair
strategy is RISK-FREE.** In practice, both pairs rarely price identically,
but the hedging still significantly reduces variance.

## Fee Considerations

Kalshi charges approximately 2% fees per trade. Unlike Polymarket where
fees scale with price distance from $0.50, Kalshi fees are more uniform.

For our strategy with leg prices often in the $0.40–$0.55 range:
- ~2% per leg = ~4% combined fee drag
- At $0.92 combined entry: edge is $0.08 (8.7%)
- After ~4% fees: net edge ≈ 4.7% — still profitable

**This is why entries below $0.93 are important — they provide enough
edge to absorb fees and still be profitable.**

## Correlation Regime Risk

The strategy FAILS when BTC and ETH decorrelate. Historical triggers:
- ETH-specific catalysts (Merge, Dencun upgrade, ETF decisions)
- BTC-specific catalysts (halving, spot ETF flows, Mt. Gox distributions)
- Macro divergence (DeFi blowups affecting ETH but not BTC)
- Flash crashes on a single asset

Mitigation:
1. Position sizing (small, $5 per trade)
2. Daily loss limits ($25)
3. Loss streak detection (pause after 5 losses)
4. Time-of-day awareness (avoid known event windows)
