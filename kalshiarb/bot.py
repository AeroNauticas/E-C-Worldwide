"""
Kalshi Cross-Asset Correlation Arbitrage Bot
==============================================
Strategy: Buy correlated pairs across BTC and ETH 15-min UP/DOWN markets
when the combined buy price is < threshold (default $0.95).

Pairs traded:
  - BTC 15min UP (YES) + ETH 15min DOWN (YES)
  - BTC 15min DOWN (YES) + ETH 15min UP (YES)

Because BTC and ETH are ~85-90% correlated, the most likely outcome is that
both move in the same direction — meaning one leg wins and pays $1.00.
If you entered at $0.93 combined, that's a $0.07 profit per trade, ~85% of the time.

Ported from Polymarket 5-min bot to Kalshi 15-min markets.
"""

import os
import sys
import json
import time
import math
import uuid
import logging
import signal
import base64
import datetime as dt
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding


# ============================================================================
# CONFIGURATION
# ============================================================================

@dataclass
class BotConfig:
    """All tunable parameters in one place."""

    # --- Credentials (from environment) ---
    api_key_id: str = ""
    private_key_path: str = ""
    private_key_pem: str = ""  # Alternative: raw PEM string

    # --- API endpoints ---
    base_url: str = "https://api.elections.kalshi.com/trade-api/v2"
    # For demo/paper trading, use:
    # base_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    # --- Strategy parameters ---
    max_combined_price: float = 0.95       # Only enter when both legs cost <= this
    position_size_usd: float = 5.00        # USD per trade (per pair, so $5 on each leg)
    max_open_positions: int = 3            # Max concurrent open pair trades
    min_edge_to_trade: float = 0.03        # Minimum discount below $1.00 to consider

    # --- Market parameters ---
    assets: list = field(default_factory=lambda: ["btc", "eth"])
    interval_minutes: int = 15             # 15-minute candles on Kalshi
    # Series tickers on Kalshi for 15-min up/down markets
    series_tickers: dict = field(default_factory=lambda: {
        "btc": "KXBTC15M",
        "eth": "KXETH15M",
    })

    # --- Timing ---
    poll_interval_seconds: float = 2.0     # How often to check for opportunities
    entry_window_seconds: int = 600        # Only enter within first 10 min of a 15-min candle
    min_remaining_seconds: int = 60        # Don't enter if < 60s left in candle

    # --- Risk management ---
    max_daily_loss_usd: float = 25.00      # Stop trading after this daily loss
    max_consecutive_losses: int = 5        # Pause after N consecutive losing pairs
    cooldown_after_loss_streak: int = 300  # Seconds to pause after loss streak

    # --- Execution ---
    use_limit_orders: bool = True          # True=limit at best ask, False=FOK
    max_slippage: float = 0.02             # Max slippage for market orders
    order_timeout_seconds: int = 10        # Cancel unfilled limit orders after this

    # --- Mode ---
    dry_run: bool = True                   # If True, log trades but don't execute

    # --- Logging ---
    log_level: str = "INFO"
    log_file: str = "cross_arb_bot.log"
    trade_log_file: str = "trades.jsonl"

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Load config from environment variables with sensible defaults."""
        config = cls()

        # Credentials
        config.api_key_id = os.getenv("KALSHI_API_KEY_ID", "")
        config.private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
        config.private_key_pem = os.getenv("KALSHI_PRIVATE_KEY_PEM", "")

        # API endpoint
        config.base_url = os.getenv(
            "KALSHI_BASE_URL",
            "https://api.elections.kalshi.com/trade-api/v2",
        )

        # Strategy parameters
        config.max_combined_price = float(os.getenv("MAX_COMBINED_PRICE", "0.95"))
        config.position_size_usd = float(os.getenv("POSITION_SIZE_USD", "5.0"))
        config.max_open_positions = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
        config.dry_run = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")
        config.log_level = os.getenv("LOG_LEVEL", "INFO")
        config.max_daily_loss_usd = float(os.getenv("MAX_DAILY_LOSS_USD", "25.0"))
        config.poll_interval_seconds = float(os.getenv("POLL_INTERVAL_SECONDS", "2.0"))
        return config


# ============================================================================
# DATA STRUCTURES
# ============================================================================

class PairDirection(Enum):
    BTC_UP_ETH_DOWN = "btc_up_eth_down"
    BTC_DOWN_ETH_UP = "btc_down_eth_up"


@dataclass
class MarketInfo:
    """Represents a single 15-min UP/DOWN market on Kalshi."""
    asset: str              # "btc" or "eth"
    direction: str          # "up" or "down"
    ticker: str             # e.g. "KXBTC15M-26MAR031500"
    event_ticker: str       # e.g. "KXBTC15M-26MAR031500"
    title: str
    end_time: datetime
    status: str             # "open", "active", etc.


@dataclass
class PriceQuote:
    """Best available prices for a market."""
    ticker: str
    best_yes_bid: Optional[float] = None   # Best bid to buy YES (in dollars)
    best_yes_ask: Optional[float] = None   # Best ask to buy YES (in dollars)
    yes_bid_size: Optional[float] = None
    yes_ask_size: Optional[float] = None


@dataclass
class TradePair:
    """A pair trade opportunity."""
    direction: PairDirection
    leg_a: MarketInfo          # e.g., BTC UP
    leg_b: MarketInfo          # e.g., ETH DOWN
    price_a: float             # Best ask for leg A YES
    price_b: float             # Best ask for leg B YES
    combined_price: float      # price_a + price_b
    edge: float                # 1.0 - combined_price
    size_a: float              # Available size at best ask for leg A
    size_b: float              # Available size at best ask for leg B
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class OpenPosition:
    """Tracks an open pair trade."""
    pair_id: str
    direction: PairDirection
    leg_a_ticker: str
    leg_b_ticker: str
    leg_a_order_id: Optional[str] = None
    leg_b_order_id: Optional[str] = None
    entry_price_a: float = 0.0
    entry_price_b: float = 0.0
    combined_entry: float = 0.0
    size: float = 0.0          # Number of contracts
    entry_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expiry_time: Optional[datetime] = None
    status: str = "pending"    # pending, filled, partial, expired, resolved
    pnl: float = 0.0


# ============================================================================
# LOGGING
# ============================================================================

def setup_logging(config: BotConfig) -> logging.Logger:
    logger = logging.getLogger("CrossArbBot")
    logger.setLevel(getattr(logging, config.log_level.upper()))

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(formatter)
    logger.addHandler(ch)

    fh = logging.FileHandler(config.log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    return logger


# ============================================================================
# KALSHI API CLIENT
# ============================================================================

class KalshiClient:
    """
    Thin REST client for the Kalshi Trade API v2.
    Handles RSA-PSS request signing, market discovery, order book reads,
    and order placement.

    Auth docs: https://docs.kalshi.com/getting_started/api_keys
    """

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.base_url = config.base_url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._private_key = None

    # ---- Authentication ----

    def _load_private_key(self):
        """Load the RSA private key for request signing."""
        if self._private_key is not None:
            return

        pem_data = None
        if self.config.private_key_pem:
            pem_data = self.config.private_key_pem.encode()
        elif self.config.private_key_path:
            with open(self.config.private_key_path, "rb") as f:
                pem_data = f.read()

        if pem_data is None:
            raise ValueError(
                "No Kalshi private key configured. "
                "Set KALSHI_PRIVATE_KEY_PATH or KALSHI_PRIVATE_KEY_PEM."
            )

        self._private_key = serialization.load_pem_private_key(
            pem_data, password=None, backend=default_backend()
        )

    def _sign_request(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Create the RSA-PSS SHA-256 signature required by Kalshi.
        Message = timestamp_ms + METHOD + path (without query params).
        """
        self._load_private_key()
        path_without_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_without_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        """Build the three auth headers Kalshi requires."""
        ts = str(int(datetime.now().timestamp() * 1000))
        sig = self._sign_request(ts, method, path)
        return {
            "KALSHI-ACCESS-KEY": self.config.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
        }

    # ---- HTTP helpers ----

    def _get(self, path: str, params: dict = None, auth: bool = False) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        headers = {}
        if auth:
            headers = self._auth_headers("GET", path)
        try:
            resp = self._session.get(url, params=params, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            self.logger.warning(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.logger.error(f"GET {path} error: {e}")
        return None

    def _post(self, path: str, data: dict, auth: bool = True) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers("POST", path) if auth else {}
        try:
            resp = self._session.post(url, json=data, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                return resp.json()
            self.logger.warning(f"POST {path} → {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.logger.error(f"POST {path} error: {e}")
        return None

    def _delete(self, path: str, auth: bool = True) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        headers = self._auth_headers("DELETE", path) if auth else {}
        try:
            resp = self._session.delete(url, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return resp.json() if resp.text else {}
            self.logger.warning(f"DELETE {path} → {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            self.logger.error(f"DELETE {path} error: {e}")
        return None

    # ---- Market Discovery ----

    def get_open_markets(self, series_ticker: str) -> list[dict]:
        """
        Get all open markets for a given series ticker.
        E.g. series_ticker="KXBTC15M" returns the current 15-min BTC UP/DOWN market.

        Kalshi API: GET /markets?series_ticker=...&status=open
        """
        data = self._get(
            "/markets",
            params={
                "series_ticker": series_ticker,
                "status": "open",
                "limit": 100,
            },
        )
        if data and "markets" in data:
            return data["markets"]
        return []

    def get_market(self, ticker: str) -> Optional[dict]:
        """Get a single market by ticker."""
        data = self._get(f"/markets/{ticker}")
        if data and "market" in data:
            return data["market"]
        return None

    # ---- Order Book ----

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[dict]:
        """
        Get the order book for a market.

        Kalshi returns ONLY bids (not asks) because in binary markets:
          - A YES bid at price X is equivalent to a NO ask at (100 - X)
          - The "yes_ask" = 100 - best NO bid

        Response format (using yes_dollars / no_dollars for precision):
        {
            "orderbook": {
                "yes": [[price_cents, quantity], ...],
                "no":  [[price_cents, quantity], ...],
                "yes_dollars": [["0.5500", quantity], ...],
                "no_dollars":  [["0.4500", quantity], ...],
            }
        }
        """
        data = self._get(f"/markets/{ticker}/orderbook", params={"depth": depth})
        if data and "orderbook" in data:
            return data["orderbook"]
        return None

    # ---- Order Placement ----

    def place_order(
        self,
        ticker: str,
        side: str,          # "yes" or "no"
        action: str,        # "buy" or "sell"
        count: int,         # number of contracts
        price_cents: int,   # price in cents (1-99)
        time_in_force: str = "gtc",  # "gtc" or "fill_or_kill"
    ) -> Optional[dict]:
        """
        Place an order on Kalshi.

        API: POST /portfolio/orders
        """
        order_data = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "client_order_id": str(uuid.uuid4()),
            "count": count,
            "type": "limit",
            "time_in_force": time_in_force,
        }

        # Use the dollar-precision fields
        if side == "yes":
            order_data["yes_price"] = price_cents
        else:
            order_data["no_price"] = price_cents

        resp = self._post("/portfolio/orders", order_data)
        if resp and "order" in resp:
            return resp["order"]
        return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a resting order."""
        resp = self._delete(f"/portfolio/orders/{order_id}")
        return resp is not None

    def get_order(self, order_id: str) -> Optional[dict]:
        """Get order status."""
        data = self._get(f"/portfolio/orders/{order_id}", auth=True)
        if data and "order" in data:
            return data["order"]
        return None

    def get_balance(self) -> Optional[float]:
        """Get account balance in dollars."""
        data = self._get("/portfolio/balance", auth=True)
        if data and "balance" in data:
            return data["balance"] / 100.0  # cents → dollars
        return None


# ============================================================================
# MARKET DISCOVERY
# ============================================================================

class MarketDiscovery:
    """
    Discovers active 15-min BTC/ETH UP/DOWN markets on Kalshi.

    Kalshi's 15-min crypto markets use series tickers:
      - KXBTC15M  →  "BTC Up or Down - 15 minutes"
      - KXETH15M  →  "ETH Up or Down - 15 minutes"

    Each series has one open market at a time (the current 15-min window).
    The market ticker follows the pattern: KXBTC15M-26MAR031500
    (series ticker + date/time suffix).

    Each market is binary: YES = price goes UP, NO = price goes DOWN.
    So buying YES = betting UP, buying NO = betting DOWN.
    """

    def __init__(self, config: BotConfig, client: KalshiClient, logger: logging.Logger):
        self.config = config
        self.client = client
        self.logger = logger

    def _get_current_interval_timestamp(self) -> int:
        """
        Calculate the Unix timestamp for the START of the current 15-min interval.
        Kalshi 15-min markets align to exact 15-minute boundaries in UTC.
        """
        now = datetime.now(timezone.utc)
        minutes = (now.minute // self.config.interval_minutes) * self.config.interval_minutes
        interval_start = now.replace(minute=minutes, second=0, microsecond=0)
        return int(interval_start.timestamp())

    def discover_current_markets(self) -> tuple[dict[str, MarketInfo], dict[str, dict]]:
        """
        Discover all active 15-min markets for both BTC and ETH.

        Returns:
          - markets: dict keyed by "{asset}_{direction}" with MarketInfo
          - raw_data: dict keyed by asset ("btc", "eth") with the raw
                      Kalshi market response (for direct price extraction)
        """
        result = {}
        raw_data = {}

        for asset in self.config.assets:
            series_ticker = self.config.series_tickers.get(asset)
            if not series_ticker:
                self.logger.warning(f"No series ticker configured for {asset}")
                continue

            markets = self.client.get_open_markets(series_ticker)
            if not markets:
                self.logger.debug(f"No open markets for {asset.upper()} ({series_ticker})")
                continue

            # Take the first open market (should be the current 15-min window)
            mkt = markets[0]
            ticker = mkt["ticker"]
            title = mkt.get("title", "")
            status = mkt.get("status", "unknown")

            # Store raw market data for price extraction
            raw_data[asset] = mkt

            # Parse expiration time (when the candle resolves)
            # Prefer expiration_time over close_time — close_time is when
            # trading halts, which can be BEFORE the candle actually ends
            close_str = mkt.get("expiration_time") or mkt.get("close_time", "")
            try:
                end_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                end_time = datetime.now(timezone.utc) + timedelta(minutes=15)

            self.logger.info(
                f"Found {asset.upper()} market: {ticker} | "
                f"status={status} | expires={end_time.strftime('%H:%M:%S')} UTC"
            )

            # Create "UP" entry (buy YES on this market)
            result[f"{asset}_up"] = MarketInfo(
                asset=asset,
                direction="up",
                ticker=ticker,
                event_ticker=mkt.get("event_ticker", ticker),
                title=title,
                end_time=end_time,
                status=status,
            )

            # Create "DOWN" entry (buy NO on this same market)
            result[f"{asset}_down"] = MarketInfo(
                asset=asset,
                direction="down",
                ticker=ticker,
                event_ticker=mkt.get("event_ticker", ticker),
                title=title,
                end_time=end_time,
                status=status,
            )

        return result, raw_data


# ============================================================================
# PRICE ENGINE
# ============================================================================

class PriceEngine:
    """
    Fetches real-time prices from the Kalshi API.

    PRIMARY method: Use the yes_ask_dollars / no_ask_dollars fields
    returned directly by GET /markets. These are always populated and
    give the current best ask without a separate orderbook call.

    FALLBACK: Query the orderbook endpoint and derive asks from bids.
    Kalshi's orderbook returns only BIDS:
      - YES ask = 1.00 - best NO bid
      - NO  ask = 1.00 - best YES bid
    """

    def __init__(self, config: BotConfig, client: KalshiClient, logger: logging.Logger):
        self.config = config
        self.client = client
        self.logger = logger

    def get_prices_from_market(self, mkt_data: dict) -> tuple[PriceQuote, PriceQuote]:
        """
        Extract prices directly from the GET /markets response payload.
        This is the most reliable source — Kalshi provides yes_ask_dollars,
        no_ask_dollars, yes_bid_dollars, no_bid_dollars on every market.

        Returns (up_quote, down_quote).
        """
        ticker = mkt_data.get("ticker", "")
        up_quote = PriceQuote(ticker=ticker)
        down_quote = PriceQuote(ticker=ticker)

        # Dollar-precision fields (strings like "0.5600")
        yes_ask_str = mkt_data.get("yes_ask_dollars")
        no_ask_str = mkt_data.get("no_ask_dollars")
        yes_bid_str = mkt_data.get("yes_bid_dollars")
        no_bid_str = mkt_data.get("no_bid_dollars")

        # Cent-precision fallback (integers like 56)
        yes_ask_cents = mkt_data.get("yes_ask")
        no_ask_cents = mkt_data.get("no_ask")
        yes_bid_cents = mkt_data.get("yes_bid")
        no_bid_cents = mkt_data.get("no_bid")

        # Parse YES ask (cost to buy UP)
        if yes_ask_str:
            up_quote.best_yes_ask = float(yes_ask_str)
        elif yes_ask_cents is not None:
            up_quote.best_yes_ask = yes_ask_cents / 100.0

        # Parse NO ask (cost to buy DOWN)
        if no_ask_str:
            down_quote.best_yes_ask = float(no_ask_str)
        elif no_ask_cents is not None:
            down_quote.best_yes_ask = no_ask_cents / 100.0

        # Parse bids for reference
        if yes_bid_str:
            up_quote.best_yes_bid = float(yes_bid_str)
        elif yes_bid_cents is not None:
            up_quote.best_yes_bid = yes_bid_cents / 100.0

        if no_bid_str:
            down_quote.best_yes_bid = float(no_bid_str)
        elif no_bid_cents is not None:
            down_quote.best_yes_bid = no_bid_cents / 100.0

        # Size from market-level fields
        yes_ask_size_str = mkt_data.get("yes_ask_size_fp")
        no_ask_size_str = mkt_data.get("no_ask_size_fp")
        if yes_ask_size_str:
            up_quote.yes_ask_size = float(yes_ask_size_str)
        if no_ask_size_str:
            down_quote.yes_ask_size = float(no_ask_size_str)

        self.logger.info(
            f"Prices for {ticker}: "
            f"YES(UP) ask=${up_quote.best_yes_ask} bid=${up_quote.best_yes_bid} | "
            f"NO(DOWN) ask=${down_quote.best_yes_ask} bid=${down_quote.best_yes_bid}"
        )

        return up_quote, down_quote

    def get_prices_from_orderbook(self, ticker: str) -> tuple[PriceQuote, PriceQuote]:
        """
        Fallback: derive prices from the orderbook endpoint.
        Only needed if market-level prices are missing.
        """
        up_quote = PriceQuote(ticker=ticker)
        down_quote = PriceQuote(ticker=ticker)

        book = self.client.get_orderbook(ticker)
        if not book:
            self.logger.warning(f"No order book for {ticker}")
            return up_quote, down_quote

        # Parse yes_dollars and no_dollars (preferred for precision)
        # Format: [["0.5500", quantity], ...] — BIDS sorted best first
        yes_bids = book.get("yes_dollars") or book.get("yes", [])
        no_bids = book.get("no_dollars") or book.get("no", [])

        best_yes_bid = None
        best_yes_bid_size = None
        best_no_bid = None
        best_no_bid_size = None

        if yes_bids and len(yes_bids) > 0:
            entry = yes_bids[0]
            best_yes_bid = float(entry[0]) if isinstance(entry[0], str) else entry[0] / 100.0
            best_yes_bid_size = float(entry[1]) if len(entry) > 1 else None

        if no_bids and len(no_bids) > 0:
            entry = no_bids[0]
            best_no_bid = float(entry[0]) if isinstance(entry[0], str) else entry[0] / 100.0
            best_no_bid_size = float(entry[1]) if len(entry) > 1 else None

        # Derive asks from opposite side's bids
        if best_no_bid is not None:
            up_quote.best_yes_ask = round(1.0 - best_no_bid, 4)
            up_quote.yes_ask_size = best_no_bid_size
        if best_yes_bid is not None:
            down_quote.best_yes_ask = round(1.0 - best_yes_bid, 4)
            down_quote.yes_ask_size = best_yes_bid_size

        up_quote.best_yes_bid = best_yes_bid
        up_quote.yes_bid_size = best_yes_bid_size
        down_quote.best_yes_bid = best_no_bid
        down_quote.yes_bid_size = best_no_bid_size

        self.logger.info(
            f"Prices (orderbook) for {ticker}: "
            f"YES(UP) ask=${up_quote.best_yes_ask} bid=${best_yes_bid} | "
            f"NO(DOWN) ask=${down_quote.best_yes_ask} bid=${best_no_bid}"
        )

        return up_quote, down_quote

    def get_all_prices(
        self, markets: dict[str, MarketInfo], raw_market_data: dict[str, dict]
    ) -> dict[str, PriceQuote]:
        """
        Get prices for all markets.

        Primary: use market-level ask/bid prices from the GET /markets response.
        Fallback: query orderbook if market-level prices are missing.

        raw_market_data: dict keyed by asset ("btc", "eth") with the raw
                         Kalshi market response dict.
        """
        results = {}

        for asset in ["btc", "eth"]:
            up_key = f"{asset}_up"
            down_key = f"{asset}_down"

            if up_key not in markets:
                continue

            ticker = markets[up_key].ticker
            mkt_raw = raw_market_data.get(asset)

            if mkt_raw:
                # Primary: parse from market-level fields
                up_quote, down_quote = self.get_prices_from_market(mkt_raw)

                # If market-level asks are missing, fall back to orderbook
                if up_quote.best_yes_ask is None or down_quote.best_yes_ask is None:
                    self.logger.info(
                        f"Market-level prices incomplete for {ticker}, "
                        f"falling back to orderbook..."
                    )
                    up_quote, down_quote = self.get_prices_from_orderbook(ticker)
            else:
                up_quote, down_quote = self.get_prices_from_orderbook(ticker)

            results[up_key] = up_quote
            results[down_key] = down_quote

        return results


# ============================================================================
# STRATEGY ENGINE
# ============================================================================

class StrategyEngine:
    """
    Core strategy logic: identify cross-asset arbitrage opportunities.

    We look for:
      Pair A: BTC UP (buy YES on KXBTC15M) + ETH DOWN (buy NO on KXETH15M) combined < threshold
      Pair B: BTC DOWN (buy NO on KXBTC15M) + ETH UP (buy YES on KXETH15M) combined < threshold

    Outcome matrix for Pair A (BTC UP + ETH DOWN):
      BTC ↑, ETH ↓ → WIN both   → collect $2.00 (best case, low probability)
      BTC ↑, ETH ↑ → WIN leg A  → collect $1.00 (profit = $1.00 - combined_cost)
      BTC ↓, ETH ↓ → WIN leg B  → collect $1.00 (profit = $1.00 - combined_cost)
      BTC ↓, ETH ↑ → LOSE both  → collect $0.00 (loss = combined_cost)

    Because BTC and ETH are ~85-95% correlated, the two middle scenarios
    (both up or both down) are the MOST likely (~80-90% combined).
    """

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger

    def evaluate_pairs(
        self,
        markets: dict[str, MarketInfo],
        prices: dict[str, PriceQuote],
    ) -> list[TradePair]:
        """
        Evaluate all possible cross-asset pairs and return those meeting our criteria.
        """
        opportunities = []

        pair_configs = [
            (PairDirection.BTC_UP_ETH_DOWN, "btc_up", "eth_down"),
            (PairDirection.BTC_DOWN_ETH_UP, "btc_down", "eth_up"),
        ]

        for direction, leg_a_key, leg_b_key in pair_configs:
            if leg_a_key not in markets or leg_b_key not in markets:
                self.logger.debug(f"Missing market for pair: {leg_a_key} + {leg_b_key}")
                continue

            leg_a_market = markets[leg_a_key]
            leg_b_market = markets[leg_b_key]

            price_a = prices.get(leg_a_key)
            price_b = prices.get(leg_b_key)

            if not price_a or not price_b:
                self.logger.debug(f"Missing prices for {direction.value}")
                continue

            if price_a.best_yes_ask is None or price_b.best_yes_ask is None:
                self.logger.debug(f"No asks available for {direction.value}")
                continue

            combined = price_a.best_yes_ask + price_b.best_yes_ask
            edge = 1.0 - combined

            if combined > self.config.max_combined_price:
                self.logger.info(
                    f"{direction.value}: combined=${combined:.4f} > "
                    f"max=${self.config.max_combined_price} — SKIP"
                )
                continue

            if edge < self.config.min_edge_to_trade:
                self.logger.info(
                    f"{direction.value}: edge=${edge:.4f} < "
                    f"min=${self.config.min_edge_to_trade} — SKIP"
                )
                continue

            # Check minimum liquidity
            min_contracts_a = self.config.position_size_usd / max(price_a.best_yes_ask, 0.01)
            if price_a.yes_ask_size is not None and price_a.yes_ask_size < min_contracts_a:
                self.logger.info(
                    f"{direction.value} leg A: insufficient liquidity "
                    f"({price_a.yes_ask_size} < {min_contracts_a:.1f})"
                )
                continue

            min_contracts_b = self.config.position_size_usd / max(price_b.best_yes_ask, 0.01)
            if price_b.yes_ask_size is not None and price_b.yes_ask_size < min_contracts_b:
                self.logger.info(
                    f"{direction.value} leg B: insufficient liquidity "
                    f"({price_b.yes_ask_size} < {min_contracts_b:.1f})"
                )
                continue

            # Check time remaining in candle
            now = datetime.now(timezone.utc)
            remaining = (leg_a_market.end_time - now).total_seconds()
            if remaining < self.config.min_remaining_seconds:
                self.logger.info(
                    f"{direction.value}: only {remaining:.0f}s remaining — SKIP"
                )
                continue

            trade = TradePair(
                direction=direction,
                leg_a=leg_a_market,
                leg_b=leg_b_market,
                price_a=price_a.best_yes_ask,
                price_b=price_b.best_yes_ask,
                combined_price=combined,
                edge=edge,
                size_a=price_a.yes_ask_size or 0,
                size_b=price_b.yes_ask_size or 0,
                timestamp=now,
            )
            opportunities.append(trade)

            self.logger.info(
                f"🎯 OPPORTUNITY: {direction.value} | "
                f"leg_a=${price_a.best_yes_ask:.4f} + leg_b=${price_b.best_yes_ask:.4f} = "
                f"${combined:.4f} | edge=${edge:.4f} | "
                f"remaining={remaining:.0f}s"
            )

        opportunities.sort(key=lambda x: x.edge, reverse=True)
        return opportunities


# ============================================================================
# EXECUTION ENGINE
# ============================================================================

class ExecutionEngine:
    """
    Handles order placement, tracking, and settlement on Kalshi.

    Key difference from Polymarket:
      - On Kalshi, UP and DOWN are YES and NO on the SAME market.
      - BTC UP   = BUY YES on KXBTC15M-...
      - BTC DOWN = BUY NO  on KXBTC15M-...
      - Prices are in cents (1-99). $0.55 = 55¢.
    """

    def __init__(self, config: BotConfig, client: KalshiClient, logger: logging.Logger):
        self.config = config
        self.client = client
        self.logger = logger
        self._initialized = False

    def initialize(self):
        """Validate credentials."""
        if self.config.dry_run:
            self.logger.info("🏜️  DRY RUN MODE — no orders will be placed")
            self._initialized = True
            return

        if not self.config.api_key_id:
            raise ValueError("KALSHI_API_KEY_ID not set. Cannot trade.")
        if not self.config.private_key_path and not self.config.private_key_pem:
            raise ValueError("No Kalshi private key configured. Cannot trade.")

        # Test auth by checking balance
        balance = self.client.get_balance()
        if balance is not None:
            self.logger.info(f"✅ Authenticated. Balance: ${balance:.2f}")
        else:
            self.logger.warning("⚠️  Could not verify balance — auth may be misconfigured")

        self._initialized = True
        self.logger.info("✅ Execution engine initialized (LIVE MODE)")

    def _determine_side(self, market_info: MarketInfo) -> str:
        """
        Determine whether to buy YES or NO based on the direction.
        UP → buy YES, DOWN → buy NO.
        """
        if market_info.direction == "up":
            return "yes"
        else:
            return "no"

    def calculate_contracts(self, usd_amount: float, price_per_contract: float) -> int:
        """
        Calculate number of contracts for a given USD amount and price.
        On Kalshi, each contract costs `price` cents and pays $1.00 if it wins.
        """
        if price_per_contract <= 0:
            return 0
        contracts = usd_amount / price_per_contract
        return int(contracts)  # Round down to whole contracts

    def place_pair_trade(self, trade: TradePair) -> OpenPosition:
        """Execute a pair trade (both legs)."""
        pair_id = (
            f"{trade.direction.value}_"
            f"{int(trade.timestamp.timestamp())}"
        )

        contracts_a = self.calculate_contracts(self.config.position_size_usd, trade.price_a)
        contracts_b = self.calculate_contracts(self.config.position_size_usd, trade.price_b)

        # Use the smaller to keep legs balanced
        contracts = min(contracts_a, contracts_b)
        if contracts < 1:
            self.logger.warning(f"Calculated contracts ({contracts}) too small, skipping")
            return OpenPosition(
                pair_id=pair_id,
                direction=trade.direction,
                leg_a_ticker=trade.leg_a.ticker,
                leg_b_ticker=trade.leg_b.ticker,
                status="skipped",
            )

        position = OpenPosition(
            pair_id=pair_id,
            direction=trade.direction,
            leg_a_ticker=trade.leg_a.ticker,
            leg_b_ticker=trade.leg_b.ticker,
            entry_price_a=trade.price_a,
            entry_price_b=trade.price_b,
            combined_entry=trade.combined_price,
            size=contracts,
            entry_time=trade.timestamp,
            expiry_time=trade.leg_a.end_time,
            status="pending",
        )

        side_a = self._determine_side(trade.leg_a)
        side_b = self._determine_side(trade.leg_b)
        price_cents_a = int(round(trade.price_a * 100))
        price_cents_b = int(round(trade.price_b * 100))

        if self.config.dry_run:
            self.logger.info(
                f"🏜️  DRY RUN — Would place pair trade:\n"
                f"   Pair: {trade.direction.value}\n"
                f"   Leg A: BUY {side_a.upper()} {contracts} contracts @ ${trade.price_a:.4f} "
                f"({trade.leg_a.ticker})\n"
                f"   Leg B: BUY {side_b.upper()} {contracts} contracts @ ${trade.price_b:.4f} "
                f"({trade.leg_b.ticker})\n"
                f"   Combined: ${trade.combined_price:.4f} | Edge: ${trade.edge:.4f}\n"
                f"   Total cost: ${contracts * trade.combined_price:.2f}"
            )
            position.status = "filled_dry"
            return position

        # --- LIVE EXECUTION ---
        try:
            # Place Leg A
            self.logger.info(
                f"Placing Leg A: BUY {side_a.upper()} {contracts} @ {price_cents_a}¢ "
                f"on {trade.leg_a.ticker}"
            )
            order_a = self.client.place_order(
                ticker=trade.leg_a.ticker,
                side=side_a,
                action="buy",
                count=contracts,
                price_cents=price_cents_a,
            )
            if order_a:
                position.leg_a_order_id = order_a.get("order_id")

            # Place Leg B
            self.logger.info(
                f"Placing Leg B: BUY {side_b.upper()} {contracts} @ {price_cents_b}¢ "
                f"on {trade.leg_b.ticker}"
            )
            order_b = self.client.place_order(
                ticker=trade.leg_b.ticker,
                side=side_b,
                action="buy",
                count=contracts,
                price_cents=price_cents_b,
            )
            if order_b:
                position.leg_b_order_id = order_b.get("order_id")

            if position.leg_a_order_id and position.leg_b_order_id:
                position.status = "filled"
                self.logger.info(f"✅ Pair trade placed: {pair_id}")
            elif position.leg_a_order_id or position.leg_b_order_id:
                position.status = "partial"
                self.logger.warning(
                    f"⚠️  Partial fill on {pair_id}: "
                    f"A={'✅' if position.leg_a_order_id else '❌'} "
                    f"B={'✅' if position.leg_b_order_id else '❌'}"
                )
                self._handle_partial_fill(position)
            else:
                position.status = "failed"
                self.logger.error(f"❌ Both legs failed for {pair_id}")

        except Exception as e:
            position.status = "error"
            self.logger.error(f"Error executing pair trade {pair_id}: {e}")

        return position

    def _handle_partial_fill(self, position: OpenPosition):
        """Handle case where only one leg filled — try to cancel it."""
        try:
            if position.leg_a_order_id and not position.leg_b_order_id:
                self.logger.info(f"Cancelling Leg A order: {position.leg_a_order_id}")
                self.client.cancel_order(position.leg_a_order_id)
            elif position.leg_b_order_id and not position.leg_a_order_id:
                self.logger.info(f"Cancelling Leg B order: {position.leg_b_order_id}")
                self.client.cancel_order(position.leg_b_order_id)
        except Exception as e:
            self.logger.error(f"Error cancelling partial fill: {e}")


# ============================================================================
# RISK MANAGER
# ============================================================================

class RiskManager:
    """Enforces risk limits and tracks P&L."""

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.daily_pnl: float = 0.0
        self.consecutive_losses: int = 0
        self.total_trades: int = 0
        self.winning_trades: int = 0
        self.losing_trades: int = 0
        self.cooldown_until: Optional[datetime] = None
        self._daily_reset_date = None

    def can_trade(self, num_open_positions: int) -> tuple[bool, str]:
        """Check if we're allowed to open a new position."""
        now = datetime.now(timezone.utc)

        today = now.date()
        if self._daily_reset_date != today:
            self._daily_reset_date = today
            self.daily_pnl = 0.0
            self.logger.info("📅 Daily P&L reset")

        if self.daily_pnl <= -self.config.max_daily_loss_usd:
            return False, f"Daily loss limit hit: ${self.daily_pnl:.2f}"

        if num_open_positions >= self.config.max_open_positions:
            return False, f"Max open positions reached: {num_open_positions}"

        if self.cooldown_until and now < self.cooldown_until:
            remaining = (self.cooldown_until - now).total_seconds()
            return False, f"Cooling down: {remaining:.0f}s remaining"

        if self.consecutive_losses >= self.config.max_consecutive_losses:
            self.cooldown_until = now + timedelta(
                seconds=self.config.cooldown_after_loss_streak
            )
            self.consecutive_losses = 0
            return False, f"Loss streak triggered cooldown ({self.config.cooldown_after_loss_streak}s)"

        return True, "OK"

    def record_trade_result(self, pnl: float):
        """Record the P&L of a resolved trade."""
        self.daily_pnl += pnl
        self.total_trades += 1

        if pnl >= 0:
            self.winning_trades += 1
            self.consecutive_losses = 0
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1

        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0

        self.logger.info(
            f"📊 Trade result: ${pnl:+.4f} | "
            f"Daily P&L: ${self.daily_pnl:+.2f} | "
            f"Record: {self.winning_trades}W/{self.losing_trades}L ({win_rate:.1f}%) | "
            f"Streak: {self.consecutive_losses} consecutive losses"
        )

    def get_stats(self) -> dict:
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        return {
            "daily_pnl": round(self.daily_pnl, 4),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(win_rate, 1),
            "consecutive_losses": self.consecutive_losses,
            "max_daily_loss": self.config.max_daily_loss_usd,
        }


# ============================================================================
# TRADE LOGGER
# ============================================================================

class TradeLogger:
    """Logs all trades to a JSONL file for analysis."""

    def __init__(self, config: BotConfig, logger: logging.Logger):
        self.config = config
        self.logger = logger
        self.filepath = config.trade_log_file

    def log_opportunity(self, trade: TradePair, action: str):
        record = {
            "type": "opportunity",
            "timestamp": trade.timestamp.isoformat(),
            "direction": trade.direction.value,
            "price_a": trade.price_a,
            "price_b": trade.price_b,
            "combined": trade.combined_price,
            "edge": trade.edge,
            "action": action,
        }
        self._write(record)

    def log_execution(self, position: OpenPosition):
        record = {
            "type": "execution",
            "timestamp": position.entry_time.isoformat(),
            "pair_id": position.pair_id,
            "direction": position.direction.value,
            "entry_price_a": position.entry_price_a,
            "entry_price_b": position.entry_price_b,
            "combined_entry": position.combined_entry,
            "size": position.size,
            "status": position.status,
            "leg_a_order": position.leg_a_order_id,
            "leg_b_order": position.leg_b_order_id,
        }
        self._write(record)

    def log_resolution(self, position: OpenPosition, pnl: float):
        record = {
            "type": "resolution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pair_id": position.pair_id,
            "direction": position.direction.value,
            "combined_entry": position.combined_entry,
            "size": position.size,
            "pnl": pnl,
            "status": position.status,
        }
        self._write(record)

    def _write(self, record: dict):
        try:
            with open(self.filepath, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            self.logger.error(f"Error writing trade log: {e}")


# ============================================================================
# MAIN BOT
# ============================================================================

class CrossArbBot:
    """
    Main bot orchestrator. Runs the continuous loop:
      1. Discover current 15-min markets on Kalshi
      2. Fetch order book prices for BTC and ETH
      3. Evaluate pair opportunities
      4. Execute trades that meet criteria
      5. Track open positions and resolve them at expiry
      6. Repeat
    """

    def __init__(self, config: Optional[BotConfig] = None):
        self.config = config or BotConfig.from_env()
        self.logger = setup_logging(self.config)
        self.client = KalshiClient(self.config, self.logger)
        self.discovery = MarketDiscovery(self.config, self.client, self.logger)
        self.price_engine = PriceEngine(self.config, self.client, self.logger)
        self.strategy = StrategyEngine(self.config, self.logger)
        self.execution = ExecutionEngine(self.config, self.client, self.logger)
        self.risk = RiskManager(self.config, self.logger)
        self.trade_log = TradeLogger(self.config, self.logger)
        self.open_positions: list[OpenPosition] = []
        self._running = False
        self._current_interval_ts: Optional[int] = None

    def start(self):
        """Start the bot."""
        self.logger.info("=" * 60)
        self.logger.info("  KALSHI CROSS-ASSET CORRELATION ARB BOT")
        self.logger.info("=" * 60)
        self.logger.info(f"  Mode:             {'DRY RUN' if self.config.dry_run else '🔴 LIVE'}")
        self.logger.info(f"  Exchange:         Kalshi")
        self.logger.info(f"  API:              {self.config.base_url}")
        self.logger.info(f"  Max combined:     ${self.config.max_combined_price}")
        self.logger.info(f"  Position size:    ${self.config.position_size_usd}")
        self.logger.info(f"  Max open:         {self.config.max_open_positions}")
        self.logger.info(f"  Min edge:         ${self.config.min_edge_to_trade}")
        self.logger.info(f"  Max daily loss:   ${self.config.max_daily_loss_usd}")
        self.logger.info(f"  Assets:           {', '.join(self.config.assets)}")
        self.logger.info(f"  Interval:         {self.config.interval_minutes}m")
        self.logger.info(f"  Series:           {self.config.series_tickers}")
        self.logger.info(f"  Poll frequency:   {self.config.poll_interval_seconds}s")
        self.logger.info("=" * 60)

        self.execution.initialize()

        signal.signal(signal.SIGINT, self._shutdown_handler)
        signal.signal(signal.SIGTERM, self._shutdown_handler)

        self._running = True
        self._run_loop()

    def _shutdown_handler(self, signum, frame):
        self.logger.info("\n🛑 Shutdown signal received. Cleaning up...")
        self._running = False

    def _run_loop(self):
        while self._running:
            try:
                self._tick()
            except KeyboardInterrupt:
                self.logger.info("Keyboard interrupt received")
                break
            except Exception as e:
                self.logger.error(f"Error in main loop: {e}", exc_info=True)

            time.sleep(self.config.poll_interval_seconds)

        self._shutdown()

    def _tick(self):
        """Single iteration of the main loop."""

        # --- Step 1: Resolve any expired positions ---
        self._resolve_expired_positions()

        # --- Step 2: Check if we can trade ---
        can_trade, reason = self.risk.can_trade(len(self.open_positions))
        if not can_trade:
            self.logger.debug(f"Cannot trade: {reason}")
            return

        # --- Step 3: Discover current markets ---
        markets, raw_market_data = self.discovery.discover_current_markets()

        required_keys = {"btc_up", "btc_down", "eth_up", "eth_down"}
        if not required_keys.issubset(markets.keys()):
            missing = required_keys - markets.keys()
            self.logger.debug(f"Missing markets: {missing}")
            return

        # --- Step 4: Fetch prices ---
        prices = self.price_engine.get_all_prices(markets, raw_market_data)

        # --- Step 5: Evaluate opportunities ---
        opportunities = self.strategy.evaluate_pairs(markets, prices)

        if not opportunities:
            return

        # --- Step 6: Execute best opportunities ---
        for opp in opportunities:
            can_trade, reason = self.risk.can_trade(len(self.open_positions))
            if not can_trade:
                self.logger.debug(f"Risk limit hit during execution: {reason}")
                break

            already_has = any(
                p.direction == opp.direction and p.status in ("filled", "filled_dry", "pending")
                for p in self.open_positions
            )
            if already_has:
                self.logger.debug(
                    f"Already have position in {opp.direction.value} — skip"
                )
                continue

            self.trade_log.log_opportunity(opp, "EXECUTE")
            position = self.execution.place_pair_trade(opp)
            self.trade_log.log_execution(position)

            if position.status in ("filled", "filled_dry"):
                self.open_positions.append(position)
                self.logger.info(
                    f"📈 Position opened: {position.pair_id} | "
                    f"Cost: ${position.combined_entry * position.size:.2f}"
                )

    def _resolve_expired_positions(self):
        """Check for positions past their expiry and calculate P&L."""
        now = datetime.now(timezone.utc)
        resolved = []

        for pos in self.open_positions:
            if pos.expiry_time and now >= pos.expiry_time:
                if pos.status in ("filled", "filled_dry"):
                    if self.config.dry_run:
                        import random
                        roll = random.random()
                        if roll < 0.05:
                            payout = 2.0 * pos.size
                        elif roll < 0.90:
                            payout = 1.0 * pos.size
                        else:
                            payout = 0.0

                        cost = pos.combined_entry * pos.size
                        pnl = payout - cost
                        pos.pnl = pnl
                        pos.status = "resolved_sim"

                        self.logger.info(
                            f"🏁 RESOLVED (sim): {pos.pair_id} | "
                            f"Cost: ${cost:.2f} | Payout: ${payout:.2f} | "
                            f"P&L: ${pnl:+.2f}"
                        )
                    else:
                        # In live mode, Kalshi settles automatically.
                        # Query settlements for actual P&L.
                        pos.status = "awaiting_resolution"
                        self.logger.info(
                            f"⏳ Position expired, awaiting Kalshi settlement: {pos.pair_id}"
                        )
                        # TODO: Poll GET /portfolio/settlements for actual results
                        pnl = 0

                    self.risk.record_trade_result(pos.pnl)
                    self.trade_log.log_resolution(pos, pos.pnl)
                    resolved.append(pos)

        for pos in resolved:
            self.open_positions.remove(pos)

    def _shutdown(self):
        stats = self.risk.get_stats()
        self.logger.info("=" * 60)
        self.logger.info("  BOT SHUTDOWN — FINAL STATS")
        self.logger.info("=" * 60)
        self.logger.info(f"  Daily P&L:      ${stats['daily_pnl']:+.2f}")
        self.logger.info(f"  Total trades:   {stats['total_trades']}")
        self.logger.info(f"  Win rate:       {stats['win_rate']}%")
        self.logger.info(f"  Open positions: {len(self.open_positions)}")
        self.logger.info("=" * 60)


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    config = BotConfig.from_env()
    bot = CrossArbBot(config)
    bot.start()


if __name__ == "__main__":
    main()
