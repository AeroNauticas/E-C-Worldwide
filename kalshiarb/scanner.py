"""
Quick scanner — checks the current 15-min BTC/ETH markets on Kalshi for cross-arb opportunities.
No trading, no authentication needed. Just reads the public order book.

Usage:
    python3 scanner.py
"""

import json
import sys
from datetime import datetime, timezone

import requests

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
ASSETS = {
    "btc": "KXBTC15M",
    "eth": "KXETH15M",
}
INTERVAL = 15


def get_interval_info():
    now = datetime.now(timezone.utc)
    minutes = (now.minute // INTERVAL) * INTERVAL
    interval_start = now.replace(minute=minutes, second=0, microsecond=0)
    from datetime import timedelta
    interval_end = interval_start + timedelta(minutes=INTERVAL)
    remaining = (interval_end - now).total_seconds()
    return interval_start, interval_end, now, remaining


def get_open_market(series_ticker):
    """Get the currently open market for a series."""
    url = f"{BASE_URL}/markets"
    params = {
        "series_ticker": series_ticker,
        "status": "open",
        "limit": 5,
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            markets = data.get("markets", [])
            if markets:
                return markets[0]
    except Exception as e:
        print(f"  ⚠ Error fetching {series_ticker}: {e}")
    return None


def get_orderbook(ticker):
    """Get the order book for a market (public, no auth)."""
    url = f"{BASE_URL}/markets/{ticker}/orderbook"
    try:
        resp = requests.get(url, params={"depth": 5}, timeout=5)
        if resp.status_code == 200:
            return resp.json().get("orderbook", {})
    except Exception as e:
        print(f"  ⚠ Error fetching orderbook for {ticker}: {e}")
    return None


def parse_best_bid(bids):
    """
    Parse the best (highest) bid from the orderbook.
    Kalshi returns bids as [[price_cents, quantity], ...] or
    [["0.5500", quantity], ...] for dollar-precision fields.
    """
    if not bids or len(bids) == 0:
        return None, None

    entry = bids[0]
    if isinstance(entry[0], str):
        price = float(entry[0])
    else:
        price = entry[0] / 100.0  # cents → dollars

    size = float(entry[1]) if len(entry) > 1 else None
    return price, size


def main():
    interval_start, interval_end, now, remaining = get_interval_info()

    print(f"\n{'='*60}")
    print(f"  KALSHI CROSS-ARB SCANNER — {now.strftime('%H:%M:%S')} UTC")
    print(f"  Interval: {interval_start.strftime('%H:%M')}–{interval_end.strftime('%H:%M')} | Remaining: {remaining:.0f}s")
    print(f"{'='*60}\n")

    # Fetch markets and prices
    tickers = {}
    prices = {}

    for asset, series in ASSETS.items():
        mkt = get_open_market(series)
        if not mkt:
            print(f"  ❌ No open market for {asset.upper()} ({series})")
            continue

        ticker = mkt["ticker"]
        status = mkt.get("status", "?")
        tickers[asset] = ticker
        print(f"  {asset.upper()} market: {ticker} (status: {status})")

        book = get_orderbook(ticker)
        if not book:
            print(f"  ❌ No order book for {ticker}")
            continue

        # Parse bids — prefer dollar-precision fields
        yes_bids = book.get("yes_dollars") or book.get("yes", [])
        no_bids = book.get("no_dollars") or book.get("no", [])

        best_yes_bid, yes_bid_size = parse_best_bid(yes_bids)
        best_no_bid, no_bid_size = parse_best_bid(no_bids)

        # Derive asks
        # YES ask = 1.00 - best NO bid
        # NO ask  = 1.00 - best YES bid
        up_ask = round(1.0 - best_no_bid, 4) if best_no_bid is not None else None
        down_ask = round(1.0 - best_yes_bid, 4) if best_yes_bid is not None else None

        prices[f"{asset}_up"] = {"ask": up_ask, "size": no_bid_size}
        prices[f"{asset}_down"] = {"ask": down_ask, "size": yes_bid_size}

        up_str = f"${up_ask:.4f}" if up_ask else "NO ASK"
        down_str = f"${down_ask:.4f}" if down_ask else "NO ASK"
        print(f"    UP (YES):   {up_str}  (derived: 1.00 - NO bid ${best_no_bid})")
        print(f"    DOWN (NO):  {down_str}  (derived: 1.00 - YES bid ${best_yes_bid})")

    if len(tickers) < 2:
        print("\n  ⚠ Not all markets available. Cannot evaluate pairs.\n")
        return

    print()

    # Evaluate pairs
    pairs = [
        ("BTC UP + ETH DOWN", "btc_up", "eth_down"),
        ("BTC DOWN + ETH UP", "btc_down", "eth_up"),
    ]

    print(f"  {'PAIR':<22s} {'LEG A':>8s} {'LEG B':>8s} {'COMBINED':>10s} {'EDGE':>8s} {'SIGNAL':>10s}")
    print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*10} {'-'*8} {'-'*10}")

    for name, leg_a, leg_b in pairs:
        a = prices.get(leg_a, {}).get("ask")
        b = prices.get(leg_b, {}).get("ask")

        if a is None or b is None:
            print(f"  {name:<22s} {'N/A':>8s} {'N/A':>8s} {'N/A':>10s} {'N/A':>8s} {'❌ NO DATA':>10s}")
            continue

        combined = a + b
        edge = 1.0 - combined

        if combined <= 0.90:
            signal_str = "🔥 STRONG"
        elif combined <= 0.93:
            signal_str = "✅ GOOD"
        elif combined <= 0.95:
            signal_str = "⚡ THIN"
        elif combined <= 1.00:
            signal_str = "⏸ PASS"
        else:
            signal_str = "❌ OVER"

        print(
            f"  {name:<22s} ${a:>7.4f} ${b:>7.4f} ${combined:>9.4f} ${edge:>7.4f} {signal_str:>10s}"
        )

    print(f"\n  Threshold: $0.95 | Only trade STRONG, GOOD, or THIN signals")
    print(f"  Time remaining in candle: {remaining:.0f}s")
    if remaining < 60:
        print(f"  ⚠ Too close to expiry — bot would not trade this candle")
    print()


if __name__ == "__main__":
    main()
