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
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding

# Load .env file if present
try:
    from dotenv import load_dotenv
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        load_dotenv(env_path)
        print(f"Loaded configuration from {env_path}")
except ImportError:
    pass  # dotenv not installed, rely on environment


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
    max_combined_price: float = 0.89       # Only enter when both legs cost <= this (11% edge minimum)
    position_size_usd: float = 5.00        # USD per trade (per pair, so $5 on each leg)
    max_open_positions: int = 3            # Max concurrent open pair trades
    min_edge_to_trade: float = 0.11        # Minimum discount below $1.00 to consider (11% edge)

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
        Message = timestamp_ms + METHOD + full_path (without query params).
        
        Important: path must include the full API path including /trade-api/v2
        """
        self._load_private_key()
        
        # Extract the API path from base_url (e.g., /trade-api/v2)
        from urllib.parse import urlparse
        parsed = urlparse(self.base_url)
        api_base_path = parsed.path  # e.g., /trade-api/v2
        
        # Combine API base path with endpoint path
        full_path = api_base_path + path
        
        # Remove query parameters for signing
        path_without_query = full_path.split("?")[0]
        
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
        time_in_force: str = "ioc",  # "ioc" (Immediate or Cancel) or "gfd" (Good for Day)
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
        }
        
        # Kalshi appears to not accept time_in_force parameter
        # Orders default to IOC (Immediate or Cancel) behavior

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

    def get_market_result(self, ticker: str) -> Optional[str]:
        """
        Get the settlement result for a market.
        Returns "yes" if YES side won, "no" if NO side won, None if not settled yet.
        """
        market = self.get_market(ticker)
        if not market:
            return None
        
        status = market.get("status", "")
        result = market.get("result", "")
        
        # Market must be closed/settled to have a result
        if status in ("closed", "settled", "finalized") and result:
            return result.lower()  # "yes" or "no"
        
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

    def discover_current_markets(self) -> dict[str, MarketInfo]:
        """
        Discover all active 15-min markets for both BTC and ETH.

        Returns dict keyed by "{asset}_{direction}" e.g.:
          "btc_up"   → MarketInfo for BTC YES (UP)
          "btc_down" → MarketInfo for BTC NO  (DOWN)
          "eth_up"   → MarketInfo for ETH YES (UP)
          "eth_down" → MarketInfo for ETH NO  (DOWN)

        On Kalshi, each 15-min market is a single binary market:
          - Buying YES = betting price goes UP
          - Buying NO  = betting price goes DOWN (equivalent to buying YES on a "DOWN" market)

        So for our cross-arb:
          - "btc_up"   means BUY YES on KXBTC15M
          - "btc_down" means BUY NO  on KXBTC15M
          - "eth_up"   means BUY YES on KXETH15M
          - "eth_down" means BUY NO  on KXETH15M
        """
        result = {}

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

            # Parse close/expiration time
            close_str = mkt.get("close_time") or mkt.get("expiration_time", "")
            try:
                # Try ISO format first
                if close_str and "T" in close_str:
                    end_time = datetime.fromisoformat(close_str.replace("Z", "+00:00"))
                else:
                    # Fallback: derive from current interval
                    now = datetime.now(timezone.utc)
                    minutes = (now.minute // self.config.interval_minutes + 1) * self.config.interval_minutes
                    if minutes >= 60:
                        end_time = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
                    else:
                        end_time = now.replace(minute=minutes, second=0, microsecond=0)
            except (ValueError, AttributeError) as e:
                self.logger.warning(f"Failed to parse close time '{close_str}': {e}")
                end_time = datetime.now(timezone.utc) + timedelta(minutes=15)

            self.logger.info(
                f"Found {asset.upper()} market: {ticker} | "
                f"status={status} | closes={end_time.strftime('%H:%M:%S')} UTC"
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
                ticker=ticker,           # Same ticker — we just buy the NO side
                event_ticker=mkt.get("event_ticker", ticker),
                title=title,
                end_time=end_time,
                status=status,
            )

        return result


# ============================================================================
# PRICE ENGINE
# ============================================================================

class PriceEngine:
    """
    Fetches real-time prices from the Kalshi order book.

    CRITICAL: Kalshi's order book returns only BIDS (not asks).
    In a binary market:
      - YES ask = 100 - best NO bid (in cents)
      - NO  ask = 100 - best YES bid (in cents)

    So to find the cost to BUY YES:
      look at yes_dollars bids → the best (highest) YES bid is NOT what we pay.
      We pay the YES ask, which = 100 - best NO bid.

    Or more practically from the response:
      - yes_dollars: bids to buy YES → highest = best YES bid
      - no_dollars:  bids to buy NO  → highest = best NO bid
      - YES ask = $1.00 - best NO bid
      - NO  ask = $1.00 - best YES bid
    """

    def __init__(self, config: BotConfig, client: KalshiClient, logger: logging.Logger):
        self.config = config
        self.client = client
        self.logger = logger

    def get_prices(self, ticker: str) -> tuple[PriceQuote, PriceQuote]:
        """
        Get prices for both YES (UP) and NO (DOWN) from one order book query.

        Returns (up_quote, down_quote) where:
          - up_quote.best_yes_ask   = cost to buy YES (go long UP)
          - down_quote.best_yes_ask = cost to buy NO  (go long DOWN)
        """
        up_quote = PriceQuote(ticker=ticker)
        down_quote = PriceQuote(ticker=ticker)

        book = self.client.get_orderbook(ticker)
        if not book:
            self.logger.warning(f"No order book for {ticker}")
            return up_quote, down_quote

        # Parse yes_dollars and no_dollars (preferred for precision)
        # Format: [["0.5500", quantity], ...]
        # Kalshi returns bids sorted ASCENDING (lowest first)
        # The BEST bid is the LAST element (highest price)
        yes_bids = book.get("yes_dollars") or book.get("yes", [])
        no_bids = book.get("no_dollars") or book.get("no", [])

        best_yes_bid = None
        best_yes_bid_size = None
        best_no_bid = None
        best_no_bid_size = None

        if yes_bids and len(yes_bids) > 0:
            try:
                entry = yes_bids[-1]  # LAST = best (highest)
                if len(entry) >= 1:
                    best_yes_bid = float(entry[0]) if isinstance(entry[0], str) else entry[0] / 100.0
                if len(entry) >= 2:
                    best_yes_bid_size = float(entry[1])
            except (ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"Error parsing YES bids for {ticker}: {e}")

        if no_bids and len(no_bids) > 0:
            try:
                entry = no_bids[-1]  # LAST = best (highest)
                if len(entry) >= 1:
                    best_no_bid = float(entry[0]) if isinstance(entry[0], str) else entry[0] / 100.0
                if len(entry) >= 2:
                    best_no_bid_size = float(entry[1])
            except (ValueError, TypeError, IndexError) as e:
                self.logger.warning(f"Error parsing NO bids for {ticker}: {e}")

        # Derive asks from the opposite side's bids
        # YES ask = 1.00 - best NO bid
        if best_no_bid is not None and best_no_bid > 0 and best_no_bid < 1.0:
            up_quote.best_yes_ask = round(1.0 - best_no_bid, 4)
            up_quote.yes_ask_size = best_no_bid_size

        # NO ask = 1.00 - best YES bid
        if best_yes_bid is not None and best_yes_bid > 0 and best_yes_bid < 1.0:
            down_quote.best_yes_ask = round(1.0 - best_yes_bid, 4)
            down_quote.yes_ask_size = best_yes_bid_size

        # Store bids too
        up_quote.best_yes_bid = best_yes_bid
        up_quote.yes_bid_size = best_yes_bid_size
        down_quote.best_yes_bid = best_no_bid
        down_quote.yes_bid_size = best_no_bid_size

        self.logger.debug(
            f"Prices for {ticker}: "
            f"YES(UP) ask=${up_quote.best_yes_ask} bid=${best_yes_bid} | "
            f"NO(DOWN) ask=${down_quote.best_yes_ask} bid=${best_no_bid}"
        )

        return up_quote, down_quote

    def get_all_prices(
        self, markets: dict[str, MarketInfo]
    ) -> dict[str, PriceQuote]:
        """
        Get prices for all markets. Since BTC UP and BTC DOWN share a ticker,
        we only query once per asset.

        Returns dict keyed by "{asset}_{direction}" with PriceQuote values.
        """
        results = {}
        queried_tickers = set()

        for asset in ["btc", "eth"]:
            up_key = f"{asset}_up"
            down_key = f"{asset}_down"

            if up_key not in markets:
                continue

            ticker = markets[up_key].ticker

            # Only query each ticker once (UP and DOWN share the same ticker on Kalshi)
            if ticker in queried_tickers:
                continue
            queried_tickers.add(ticker)

            up_quote, down_quote = self.get_prices(ticker)
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

            # SAFETY CHECK: Both legs must have same expiry time
            if leg_a_market.end_time != leg_b_market.end_time:
                self.logger.warning(
                    f"⚠️  EXPIRY MISMATCH: {direction.value} — "
                    f"BTC expires {leg_a_market.end_time} vs ETH expires {leg_b_market.end_time}"
                )
                continue

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
                self.logger.debug(
                    f"{direction.value}: combined=${combined:.4f} > "
                    f"max=${self.config.max_combined_price} — SKIP"
                )
                continue

            if edge < self.config.min_edge_to_trade:
                self.logger.debug(
                    f"{direction.value}: edge=${edge:.4f} < "
                    f"min=${self.config.min_edge_to_trade} — SKIP"
                )
                continue

            # Check minimum liquidity
            min_contracts_a = self.config.position_size_usd / max(price_a.best_yes_ask, 0.01)
            if price_a.yes_ask_size is not None and price_a.yes_ask_size < min_contracts_a:
                self.logger.debug(
                    f"{direction.value} leg A: insufficient liquidity "
                    f"({price_a.yes_ask_size} < {min_contracts_a:.1f})"
                )
                continue

            min_contracts_b = self.config.position_size_usd / max(price_b.best_yes_ask, 0.01)
            if price_b.yes_ask_size is not None and price_b.yes_ask_size < min_contracts_b:
                self.logger.debug(
                    f"{direction.value} leg B: insufficient liquidity "
                    f"({price_b.yes_ask_size} < {min_contracts_b:.1f})"
                )
                continue

            # Check time remaining in candle
            now = datetime.now(timezone.utc)
            remaining = (leg_a_market.end_time - now).total_seconds()
            if remaining < self.config.min_remaining_seconds:
                self.logger.debug(
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

        # SAFETY CHECK: Verify both legs have same expiry time
        if trade.leg_a.end_time != trade.leg_b.end_time:
            self.logger.error(
                f"❌ EXPIRY MISMATCH: Cannot trade pairs with different expiries!\n"
                f"   Leg A ({trade.leg_a.ticker}): {trade.leg_a.end_time}\n"
                f"   Leg B ({trade.leg_b.ticker}): {trade.leg_b.end_time}\n"
                f"   This would create an asymmetric position."
            )
            return OpenPosition(
                pair_id=pair_id,
                direction=trade.direction,
                leg_a_ticker=trade.leg_a.ticker,
                leg_b_ticker=trade.leg_b.ticker,
                status="expiry_mismatch",
            )

        # Calculate contracts based on COMBINED price to ensure:
        # 1. Equal contract counts on both legs
        # 2. Total cost stays within position_size_usd budget
        # 3. Proper arbitrage math
        combined_price = trade.price_a + trade.price_b
        contracts = int(self.config.position_size_usd / combined_price)
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
            "leg_a_ticker": position.leg_a_ticker,
            "leg_b_ticker": position.leg_b_ticker,
            "expiry_time": position.expiry_time.isoformat() if position.expiry_time else None,
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
# OBSERVATION WINDOW TRACKER
# ============================================================================

@dataclass
class ObservationWindow:
    """Track an opportunity over its observation period"""
    direction: PairDirection
    first_seen: float  # timestamp
    best_edge: float
    best_combined_price: float


class OpportunityTracker:
    """
    Manages observation windows for opportunities.
    
    Enters when edge reaches DESIRED threshold OR window expires with MINIMUM edge.
    Collapses window when close to market expiry.
    """
    
    DESIRED_EDGE = 0.15  # Instant entry
    MINIMUM_EDGE = 0.05  # Accept at window expiry
    OBSERVATION_WINDOW_SECONDS = 15
    COLLAPSE_WINDOW_SECONDS = 180  # < 3 min = no window
    
    def __init__(self, logger):
        self.logger = logger
        self.windows: dict[str, ObservationWindow] = {}
    
    def evaluate(self, opportunity: TradePair, time_to_close: int) -> Optional[str]:
        """Returns 'ENTER', 'WAIT', or 'SKIP'"""
        direction_key = opportunity.direction.value
        current_time = time.time()
        
        # Collapse window if close to expiry
        observation_window = (0 if time_to_close < self.COLLAPSE_WINDOW_SECONDS 
                             else self.OBSERVATION_WINDOW_SECONDS)
        
        # Get or create window
        window = self.windows.get(direction_key)
        if window is None:
            window = ObservationWindow(
                direction=opportunity.direction,
                first_seen=current_time,
                best_edge=opportunity.edge,
                best_combined_price=opportunity.combined_price
            )
            self.windows[direction_key] = window
            self.logger.debug(
                f"👁️ Observation: {direction_key} | Edge: ${opportunity.edge:.3f} | "
                f"Window: {observation_window}s"
            )
        
        # Update best edge
        if opportunity.edge > window.best_edge:
            window.best_edge = opportunity.edge
            self.logger.debug(f"📈 Best edge: {direction_key} ${opportunity.edge:.3f}")
        
        # DECISION: Immediate entry if >= DESIRED
        if opportunity.edge >= self.DESIRED_EDGE:
            self.logger.info(
                f"🎯 DESIRED EDGE: {direction_key} ${opportunity.edge:.3f} - ENTER NOW"
            )
            del self.windows[direction_key]
            return 'ENTER'
        
        # DECISION: Window expired?
        elapsed = current_time - window.first_seen
        if elapsed >= observation_window:
            if opportunity.edge >= self.MINIMUM_EDGE:
                self.logger.info(
                    f"⏰ WINDOW EXPIRED: {direction_key} ${opportunity.edge:.3f} "
                    f"(best: ${window.best_edge:.3f}) - ENTER"
                )
                del self.windows[direction_key]
                return 'ENTER'
            else:
                self.logger.debug(
                    f"⏰ EXPIRED: {direction_key} ${opportunity.edge:.3f} < "
                    f"minimum - SKIP"
                )
                del self.windows[direction_key]
                return 'SKIP'
        
        # Still observing
        self.logger.debug(
            f"⏳ Observing: {direction_key} | Current: ${opportunity.edge:.3f} | "
            f"Best: ${window.best_edge:.3f} | {observation_window - elapsed:.1f}s left"
        )
        return 'WAIT'
    
    def clear_stale_windows(self):
        """Remove windows older than 60s"""
        current_time = time.time()
        stale = [k for k, w in self.windows.items() 
                if current_time - w.first_seen > 60]
        for k in stale:
            del self.windows[k]




# ============================================================================
# (HEDGE MANAGER REMOVED - Single pair strategy only)
# ============================================================================




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
        self.opportunity_tracker = OpportunityTracker(self.logger)
        self.open_positions: list[OpenPosition] = []
        self._running = False
        self._current_interval_ts: Optional[int] = None

    def start(self):
        """Start the bot."""
        self.logger.info("=" * 60)
        self.logger.info("  KALSHI CROSS-ASSET CORRELATION ARB BOT")
        self.logger.info("=" * 60)
        self.logger.info(f"  Mode:             {'🏜️  DRY RUN' if self.config.dry_run else '🔴 LIVE TRADING'}")
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

        # Safety check for live trading
        if not self.config.dry_run:
            self.logger.warning("⚠️  LIVE TRADING MODE DETECTED ⚠️")
            self.logger.warning("This bot will place REAL orders with REAL money.")
            self.logger.warning("Press Ctrl+C within 5 seconds to abort...")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                self.logger.info("Aborted by user")
                sys.exit(0)
            self.logger.warning("Proceeding with LIVE trading...")
        else:
            self.logger.info("🏜️  Running in DRY RUN mode — no real orders will be placed")

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

        # --- Step 2: Log new intervals (but don't gate on them) ---
        now = datetime.now(timezone.utc)
        current_interval_ts = self._calculate_interval_timestamp(now)
        
        # Log when a new interval starts
        if self._current_interval_ts != current_interval_ts:
            self._current_interval_ts = current_interval_ts
            self.logger.info(f"🔔 New 15-min interval started: {now.strftime('%H:%M:%S')} UTC")

        # --- Step 3: Check if we can trade ---
        can_trade, reason = self.risk.can_trade(len(self.open_positions))
        if not can_trade:
            self.logger.info(f"Cannot trade: {reason}")
            return

        # --- Step 4: Discover current markets ---
        markets = self.discovery.discover_current_markets()

        required_keys = {"btc_up", "btc_down", "eth_up", "eth_down"}
        if not required_keys.issubset(markets.keys()):
            missing = required_keys - markets.keys()
            self.logger.warning(f"Missing markets: {missing}")
            return

        # --- Step 5: Fetch prices ---
        prices = self.price_engine.get_all_prices(markets)

        # --- Step 6: Evaluate opportunities ---
        opportunities = self.strategy.evaluate_pairs(markets, prices)

        if not opportunities:
            # Opportunities are logged at debug level to reduce spam
            return

        time_to_close = 900  # Default 15 minutes
        if markets.get('btc_up'):
            btc_market = markets['btc_up']
            if btc_market.end_time:
                time_to_close = max(0, int((btc_market.end_time - now).total_seconds()))
        # --- Step 6.5: Filter through observation windows ---
        # Only "mature" opportunities (hit desired edge OR window expired) proceed
        mature_opportunities = []
        for opp in opportunities:
            decision = self.opportunity_tracker.evaluate(opp, time_to_close)
            if decision == 'ENTER':
                mature_opportunities.append(opp)
            # 'WAIT' = still observing, 'SKIP' = rejected
        
        # Cleanup stale observation windows
        self.opportunity_tracker.clear_stale_windows()
        
        if not mature_opportunities:
            # Still observing, nothing ready yet
            return


        # --- Step 7: Execute best opportunities (single pair strategy) ---
        # Build set of tickers already in open positions to prevent same-window re-entry
        active_tickers = set()
        for pos in self.open_positions:
            if pos.leg_a_ticker:
                active_tickers.add(pos.leg_a_ticker)
            if pos.leg_b_ticker:
                active_tickers.add(pos.leg_b_ticker)

        for opp in mature_opportunities:
            can_trade, reason = self.risk.can_trade(len(self.open_positions))
            if not can_trade:
                self.logger.debug(f"Risk limit hit during execution: {reason}")
                break

            # Skip if either ticker is already in an open position (prevents same-window stacking)
            opp_tickers = {opp.leg_a.ticker, opp.leg_b.ticker}
            if opp_tickers & active_tickers:
                self.logger.debug(f"Skipping duplicate window entry: {opp_tickers & active_tickers} already open")
                continue

            self.trade_log.log_opportunity(opp, "EXECUTE")
            position = self.execution.place_pair_trade(opp)
            self.trade_log.log_execution(position)

            if position.status in ("filled", "filled_dry"):
                self.open_positions.append(position)
                # Add newly opened tickers to active set so subsequent opps this cycle skip them
                if position.leg_a_ticker:
                    active_tickers.add(position.leg_a_ticker)
                if position.leg_b_ticker:
                    active_tickers.add(position.leg_b_ticker)
                self.logger.info(
                    f"📈 Position opened: {position.pair_id} | "
                    f"Cost: ${position.combined_entry * position.size:.2f}"
                )


    def _calculate_interval_timestamp(self, dt: datetime) -> int:
        """Calculate timestamp for the start of the current interval."""
        minutes = (dt.minute // self.config.interval_minutes) * self.config.interval_minutes
        interval_start = dt.replace(minute=minutes, second=0, microsecond=0)
        return int(interval_start.timestamp())

    def _resolve_expired_positions(self):
        """Check for positions past their expiry and calculate P&L using real market data."""
        now = datetime.now(timezone.utc)
        resolved = []

        for pos in self.open_positions:
            if pos.expiry_time and now >= pos.expiry_time:
                if pos.status in ("filled", "filled_dry"):
                    # Always fetch real market outcomes from Kalshi
                    # (works in both live and paper trading modes)
                    payout = self._calculate_real_payout(pos)
                    
                    if payout is None:
                        # Market not settled yet, check again later
                        continue
                    
                    cost = pos.combined_entry * pos.size
                    pnl = payout - cost
                    pos.pnl = pnl
                    pos.status = "resolved_real" if self.config.dry_run else "resolved"

                    self.logger.info(
                        f"🏁 RESOLVED (real data): {pos.pair_id} | "
                        f"Cost: ${cost:.2f} | Payout: ${payout:.2f} | "
                        f"P&L: ${pnl:+.2f}"
                    )

                    self.risk.record_trade_result(pos.pnl)
                    self.trade_log.log_resolution(pos, pos.pnl)
                    resolved.append(pos)

        for pos in resolved:
            self.open_positions.remove(pos)

    def _calculate_real_payout(self, pos: OpenPosition) -> Optional[float]:
        """
        Calculate payout based on real Kalshi market results.
        
        Returns:
            - Float: total payout in dollars
            - None: market not settled yet
        """
        # Get settlement results for both legs
        result_a = self.client.get_market_result(pos.leg_a_ticker)
        result_b = self.client.get_market_result(pos.leg_b_ticker)
        
        if result_a is None or result_b is None:
            # Not settled yet
            return None
        
        # Determine which side we bought for each leg based on direction
        # pos.direction tells us the strategy:
        # - btc_up_eth_down: we bought BTC YES + ETH NO
        # - btc_down_eth_up: we bought BTC NO + ETH YES
        
        direction_str = pos.direction.value if hasattr(pos.direction, 'value') else str(pos.direction)
        
        payout_a = 0.0
        payout_b = 0.0
        
        if "btc_up_eth_down" in direction_str:
            # Leg A: bought BTC YES
            payout_a = pos.size if result_a == "yes" else 0.0
            # Leg B: bought ETH NO
            payout_b = pos.size if result_b == "no" else 0.0
        elif "btc_down_eth_up" in direction_str:
            # Leg A: bought BTC NO
            payout_a = pos.size if result_a == "no" else 0.0
            # Leg B: bought ETH YES
            payout_b = pos.size if result_b == "yes" else 0.0
        else:
            self.logger.warning(f"Unknown direction: {direction_str}")
            return 0.0
        
        total_payout = payout_a + payout_b
        
        self.logger.debug(
            f"Position {pos.pair_id} | "
            f"Leg A ({pos.leg_a_ticker}): {result_a} → ${payout_a:.2f} | "
            f"Leg B ({pos.leg_b_ticker}): {result_b} → ${payout_b:.2f} | "
            f"Total: ${total_payout:.2f}"
        )
        
        return total_payout

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
