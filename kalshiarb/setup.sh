#!/usr/bin/env bash
# Setup script for Kalshi cross-arb-15m skill
# Run: bash setup.sh

set -e

echo "=============================================="
echo "  Kalshi Cross-Arb 15m — Setup"
echo "=============================================="

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | awk '{print $2}')
echo "Python version: $PYTHON_VERSION"

# Install dependencies
echo ""
echo "Installing Python dependencies..."
pip install -r requirements.txt 2>&1 | tail -5
echo "✅ Dependencies installed"

# Validate environment
echo ""
echo "Checking environment variables..."

MISSING=0

if [ -z "$KALSHI_API_KEY_ID" ]; then
    echo "  ❌ KALSHI_API_KEY_ID not set"
    MISSING=1
else
    echo "  ✅ API key ID configured"
fi

if [ -z "$KALSHI_PRIVATE_KEY_PATH" ] && [ -z "$KALSHI_PRIVATE_KEY_PEM" ]; then
    echo "  ❌ KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY_PEM) not set"
    MISSING=1
else
    echo "  ✅ Private key configured"
fi

DRY_RUN_VAL=${DRY_RUN:-true}
echo "  ℹ️  DRY_RUN=$DRY_RUN_VAL"

BASE_URL_VAL=${KALSHI_BASE_URL:-https://api.elections.kalshi.com/trade-api/v2}
echo "  ℹ️  KALSHI_BASE_URL=$BASE_URL_VAL"

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "⚠️  Missing required env vars. Set them before running the bot."
    echo "   See SKILL.md for details."
else
    echo ""
    echo "✅ All required environment variables set."
fi

# Test Kalshi API connectivity
echo ""
echo "Testing Kalshi API connectivity..."

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "https://api.elections.kalshi.com/trade-api/v2/markets?limit=1&status=open")
if [ "$HTTP_CODE" -eq 200 ]; then
    echo "  ✅ Kalshi Markets API reachable"
else
    echo "  ❌ Kalshi Markets API returned HTTP $HTTP_CODE"
fi

# Quick test: can we find the BTC 15m series?
echo ""
echo "Checking for BTC/ETH 15-min markets..."
BTC_RESP=$(curl -s "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXBTC15M&status=open&limit=1")
if echo "$BTC_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert len(d.get('markets',[])) > 0" 2>/dev/null; then
    TICKER=$(echo "$BTC_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['markets'][0]['ticker'])")
    echo "  ✅ BTC 15m market found: $TICKER"
else
    echo "  ⚠️  No open BTC 15m market right now (may be between intervals)"
fi

ETH_RESP=$(curl -s "https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker=KXETH15M&status=open&limit=1")
if echo "$ETH_RESP" | python3 -c "import sys,json; d=json.load(sys.stdin); assert len(d.get('markets',[])) > 0" 2>/dev/null; then
    TICKER=$(echo "$ETH_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['markets'][0]['ticker'])")
    echo "  ✅ ETH 15m market found: $TICKER"
else
    echo "  ⚠️  No open ETH 15m market right now (may be between intervals)"
fi

echo ""
echo "=============================================="
echo "  Setup complete!"
echo "  Scanner (no auth):  python3 scanner.py"
echo "  Bot (dry run):      python3 bot.py"
echo "=============================================="
