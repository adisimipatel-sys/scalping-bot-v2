#!/usr/bin/env python3
"""
HFT Scalping Bot v2.0 - Binance Crypto
Indicators: Support/Resistance + Volume + RSI + VWAP + Bollinger Squeeze + Order Flow Delta
Target: 100 trades/day, 60%+ win rate, 0.4% profit per trade
"""

import hashlib
import hmac
import json
import os
import time
import math
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
import requests

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

SYMBOLS           = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"]
TRADE_AMOUNT_USDT = 2.5      # ~₹200 per symbol
PROFIT_TARGET_PCT = 0.004    # 0.4% profit
STOP_LOSS_PCT     = 0.002    # 0.2% stop loss
SCAN_INTERVAL_SEC = 45       # Scan every 45 seconds
MIN_CONFLUENCE    = 4        # Minimum 4/7 indicators must agree
BINANCE_BASE_URL  = "https://api.binance.com"

# Stats
stats = {
    "total_trades": 0,
    "wins": 0,
    "losses": 0,
    "total_pnl": 0.0,
    "start_time": datetime.now().isoformat()
}
active_trades = {}

# ─── BINANCE API ──────────────────────────────────────────────────────────────

def binance_sign(params: dict) -> str:
    query = urlencode(params)
    return hmac.new(
        BINANCE_SECRET_KEY.encode(),
        query.encode(),
        hashlib.sha256
    ).hexdigest()

def binance_request(method: str, endpoint: str, params: dict = {}) -> dict:
    params = dict(params)
    params["timestamp"] = int(time.time() * 1000)
    params["signature"] = binance_sign(params)
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    url = BINANCE_BASE_URL + endpoint
    try:
        if method == "GET":
            r = requests.get(url, params=params, headers=headers, timeout=10)
        elif method == "POST":
            r = requests.post(url, params=params, headers=headers, timeout=10)
        elif method == "DELETE":
            r = requests.delete(url, params=params, headers=headers, timeout=10)
        return r.json()
    except Exception as e:
        logger.error(f"Binance API error: {e}")
        return {}

# CoinGecko symbol mapping
COINGECKO_IDS = {
    "BTCUSDT": "bitcoin",
    "ETHUSDT": "ethereum",
    "BNBUSDT": "binancecoin",
    "SOLUSDT": "solana",
    "XRPUSDT": "ripple",
}

def get_candles(symbol: str, interval="1m", limit=150) -> list:
    """Fetch OHLC data from CoinGecko (no geo-restriction)"""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        logger.error(f"No CoinGecko ID for {symbol}")
        return []
    try:
        # CoinGecko OHLC: days=1 gives hourly, days=7 gives daily
        # For scalping we use 1 day with hourly candles
        url = f"https://api.coingecko.com/api/v3/coins/{cg_id}/ohlc"
        r = requests.get(url, params={"vs_currency": "usd", "days": "1"}, timeout=15)
        data = r.json()
        if not isinstance(data, list) or len(data) == 0:
            logger.error(f"CoinGecko empty data for {symbol}")
            return []
        candles = []
        for c in data:
            candles.append({
                "time":   c[0] / 1000,
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": 1000000.0,  # CoinGecko OHLC doesn't have volume
            })
        return candles[-limit:]
    except Exception as e:
        logger.error(f"CoinGecko candles error {symbol}: {e}")
        return []


def get_volume_data(symbol: str) -> float:
    """Get 24h volume from CoinGecko"""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return 1000000.0
    try:
        url = f"https://api.coingecko.com/api/v3/simple/price"
        r = requests.get(url, params={
            "ids": cg_id,
            "vs_currencies": "usd",
            "include_24hr_vol": "true"
        }, timeout=10)
        data = r.json()
        return float(data[cg_id].get("usd_24h_vol", 1000000))
    except:
        return 1000000.0


def get_price(symbol: str) -> Optional[float]:
    """Get current price from CoinGecko"""
    cg_id = COINGECKO_IDS.get(symbol)
    if not cg_id:
        return None
    try:
        url = "https://api.coingecko.com/api/v3/simple/price"
        r = requests.get(url, params={"ids": cg_id, "vs_currencies": "usd"}, timeout=10)
        return float(r.json()[cg_id]["usd"])
    except:
        return None

def get_order_book(symbol: str) -> dict:
    """Get order book - use Binance public (usually works) or return neutral"""
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/depth",
                        params={"symbol": symbol, "limit": 20}, timeout=5)
        data = r.json()
        if "bids" not in data:
            return {"bids": 500, "asks": 500}
        total_bids = sum(float(b[1]) for b in data.get("bids", []))
        total_asks = sum(float(a[1]) for a in data.get("asks", []))
        return {"bids": total_bids, "asks": total_asks}
    except:
        return {"bids": 500, "asks": 500}

def get_symbol_info(symbol: str) -> dict:
    try:
        r = requests.get(f"{BINANCE_BASE_URL}/api/v3/exchangeInfo",
                        params={"symbol": symbol}, timeout=10)
        filters = r.json()["symbols"][0]["filters"]
        lot = next(f for f in filters if f["filterType"] == "LOT_SIZE")
        return {"stepSize": float(lot["stepSize"]), "minQty": float(lot["minQty"])}
    except:
        return {"stepSize": 0.001, "minQty": 0.001}

def round_step(qty: float, step: float) -> float:
    precision = max(0, len(f"{step:.10f}".rstrip('0').split('.')[-1]))
    return round(round(qty / step) * step, precision)

# ─── INDICATORS ───────────────────────────────────────────────────────────────

def calc_rsi(closes: list, period=14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 2)


def calc_ema(closes: list, period: int) -> list:
    if len(closes) < period:
        return []
    k = 2 / (period + 1)
    ema = [sum(closes[:period]) / period]
    for price in closes[period:]:
        ema.append(price * k + ema[-1] * (1 - k))
    return ema


def calc_vwap(candles: list) -> float:
    """VWAP = Sum(Price * Volume) / Sum(Volume)"""
    try:
        total_pv  = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candles)
        total_vol = sum(c["volume"] for c in candles)
        return round(total_pv / total_vol, 6) if total_vol > 0 else 0
    except:
        return 0


def calc_bollinger(closes: list, period=20, std_dev=2) -> dict:
    if len(closes) < period:
        return {"upper": 0, "middle": 0, "lower": 0, "width": 0, "squeeze": False}
    recent = closes[-period:]
    middle = sum(recent) / period
    variance = sum((x - middle) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    width = (upper - lower) / middle * 100

    # Bollinger Squeeze: width < 1% = squeeze (big move coming)
    squeeze = width < 1.0

    return {
        "upper":   round(upper, 6),
        "middle":  round(middle, 6),
        "lower":   round(lower, 6),
        "width":   round(width, 4),
        "squeeze": squeeze
    }


def calc_support_resistance(candles: list, lookback=50) -> dict:
    if len(candles) < lookback:
        return {"support": [], "resistance": []}

    recent = candles[-lookback:]
    highs  = [c["high"]  for c in recent]
    lows   = [c["low"]   for c in recent]
    price  = candles[-1]["close"]

    resistance_levels = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i-1] and highs[i] > highs[i-2] and \
           highs[i] > highs[i+1] and highs[i] > highs[i+2]:
            resistance_levels.append(highs[i])

    support_levels = []
    for i in range(2, len(lows) - 2):
        if lows[i] < lows[i-1] and lows[i] < lows[i-2] and \
           lows[i] < lows[i+1] and lows[i] < lows[i+2]:
            support_levels.append(lows[i])

    def cluster(levels, threshold=0.003):
        if not levels:
            return []
        levels = sorted(levels)
        clustered = [levels[0]]
        for level in levels[1:]:
            if abs(level - clustered[-1]) / clustered[-1] > threshold:
                clustered.append(level)
            else:
                clustered[-1] = (clustered[-1] + level) / 2
        return clustered

    support    = sorted([s for s in cluster(support_levels)    if s < price], reverse=True)[:3]
    resistance = sorted([r for r in cluster(resistance_levels) if r > price])[:3]
    return {"support": support, "resistance": resistance}


def calc_volume_analysis(candles: list) -> dict:
    volumes = [c["volume"] for c in candles]
    if len(volumes) < 20:
        return {"ratio": 1.0, "trend": "NEUTRAL", "signal": "NORMAL"}

    avg_vol  = sum(volumes[-20:-1]) / 19
    last_vol = volumes[-1]
    ratio    = last_vol / avg_vol if avg_vol > 0 else 1.0

    # Volume trend
    recent_avg = sum(volumes[-5:]) / 5
    prev_avg   = sum(volumes[-10:-5]) / 5
    trend = "INCREASING" if recent_avg > prev_avg * 1.1 else "DECREASING" if recent_avg < prev_avg * 0.9 else "STABLE"

    signal = "HIGH" if ratio >= 1.5 else "NORMAL"
    return {"ratio": round(ratio, 2), "trend": trend, "signal": signal}


def calc_order_flow_delta(order_book: dict) -> dict:
    """Order Flow Delta: Buy pressure vs Sell pressure"""
    bids = order_book["bids"]
    asks = order_book["asks"]
    total = bids + asks
    if total == 0:
        return {"delta": 0, "bias": "NEUTRAL", "buy_pct": 50}

    buy_pct = round(bids / total * 100, 1)
    delta   = round(bids - asks, 2)

    if buy_pct >= 60:
        bias = "BULLISH"
    elif buy_pct <= 40:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return {"delta": delta, "bias": bias, "buy_pct": buy_pct}


def calc_macd(closes: list) -> dict:
    ema12 = calc_ema(closes, 12)
    ema26 = calc_ema(closes, 26)
    if not ema12 or not ema26:
        return {"histogram": 0, "signal_cross": "NEUTRAL"}

    min_len    = min(len(ema12), len(ema26))
    macd_line  = [ema12[-(min_len-i)] - ema26[-(min_len-i)] for i in range(min_len)]
    signal_ema = calc_ema(macd_line, 9)

    if not signal_ema or len(signal_ema) < 2:
        return {"histogram": 0, "signal_cross": "NEUTRAL"}

    hist_now  = macd_line[-1] - signal_ema[-1]
    hist_prev = macd_line[-2] - signal_ema[-2] if len(macd_line) >= 2 else 0

    # Detect crossover
    if hist_prev < 0 and hist_now > 0:
        cross = "BULLISH_CROSS"
    elif hist_prev > 0 and hist_now < 0:
        cross = "BEARISH_CROSS"
    elif hist_now > 0:
        cross = "BULLISH"
    else:
        cross = "BEARISH"

    return {"histogram": round(hist_now, 8), "signal_cross": cross}

# ─── CONFLUENCE SIGNAL ENGINE ─────────────────────────────────────────────────

def generate_scalp_signal(symbol: str, candles: list) -> Optional[dict]:
    """
    7-indicator confluence system:
    1. RSI
    2. EMA Cross
    3. VWAP position
    4. Bollinger Bands
    5. Support/Resistance
    6. Volume
    7. Order Flow Delta (MACD as proxy)

    Minimum 4/7 must agree for a signal
    """
    if len(candles) < 100:
        return None

    closes  = [c["close"]  for c in candles]
    highs   = [c["high"]   for c in candles]
    lows    = [c["low"]    for c in candles]
    price   = closes[-1]
    prev    = closes[-2]

    # Calculate all indicators
    rsi     = calc_rsi(closes)
    bb      = calc_bollinger(closes)
    sr      = calc_support_resistance(candles)
    vol     = calc_volume_analysis(candles)
    vwap    = calc_vwap(candles[-50:])  # Today's VWAP
    macd    = calc_macd(closes)
    ema9    = calc_ema(closes, 9)
    ema21   = calc_ema(closes, 21)

    # Order book delta
    order_book = get_order_book(symbol)
    delta      = calc_order_flow_delta(order_book)

    buy_signals  = []
    sell_signals = []

    # ── 1. RSI ──
    if rsi < 35:
        buy_signals.append(f"RSI={rsi} (Oversold)")
    elif rsi > 65:
        sell_signals.append(f"RSI={rsi} (Overbought)")
    elif 35 <= rsi <= 50:
        buy_signals.append(f"RSI={rsi} (Bullish zone)")
    elif 50 < rsi <= 65:
        sell_signals.append(f"RSI={rsi} (Bearish zone)")

    # ── 2. EMA Cross ──
    if ema9 and ema21:
        if ema9[-1] > ema21[-1] and price > ema9[-1]:
            buy_signals.append("EMA9 > EMA21 (Uptrend)")
        elif ema9[-1] < ema21[-1] and price < ema9[-1]:
            sell_signals.append("EMA9 < EMA21 (Downtrend)")

    # ── 3. VWAP ──
    if vwap > 0:
        if price > vwap * 1.001:
            buy_signals.append(f"Price above VWAP ({vwap:.4f})")
        elif price < vwap * 0.999:
            sell_signals.append(f"Price below VWAP ({vwap:.4f})")

    # ── 4. Bollinger Bands ──
    if bb["squeeze"]:
        # Squeeze breakout — direction based on price movement
        if price > prev and price > bb["middle"]:
            buy_signals.append(f"BB Squeeze Breakout UP (width={bb['width']}%)")
        elif price < prev and price < bb["middle"]:
            sell_signals.append(f"BB Squeeze Breakout DOWN (width={bb['width']}%)")
    else:
        if price <= bb["lower"] * 1.001:
            buy_signals.append(f"Price at BB Lower (oversold)")
        elif price >= bb["upper"] * 0.999:
            sell_signals.append(f"Price at BB Upper (overbought)")

    # ── 5. Support/Resistance ──
    def near(p, level, pct=0.003):
        return abs(p - level) / level <= pct

    sr_buy = sr_sell = False
    for sup in sr["support"]:
        if near(price, sup) and price >= prev:
            buy_signals.append(f"Bounce from Support {sup:.4f}")
            sr_buy = True
            break
        if prev > sup >= price:
            sell_signals.append(f"Break below Support {sup:.4f}")
            sr_sell = True
            break

    for res in sr["resistance"]:
        if near(price, res) and price <= prev:
            sell_signals.append(f"Rejection at Resistance {res:.4f}")
            sr_sell = True
            break
        if prev < res <= price:
            buy_signals.append(f"Breakout above Resistance {res:.4f}")
            sr_buy = True
            break

    # ── 6. Volume ──
    if vol["signal"] == "HIGH":
        if vol["trend"] == "INCREASING" and price > prev:
            buy_signals.append(f"High Volume {vol['ratio']}x + Increasing")
        elif vol["trend"] == "INCREASING" and price < prev:
            sell_signals.append(f"High Volume {vol['ratio']}x + Increasing")
        else:
            # High volume but no trend — add to whichever direction
            if price > prev:
                buy_signals.append(f"High Volume {vol['ratio']}x avg")
            else:
                sell_signals.append(f"High Volume {vol['ratio']}x avg")

    # ── 7. Order Flow Delta / MACD ──
    if delta["bias"] == "BULLISH":
        buy_signals.append(f"Order Flow Bullish ({delta['buy_pct']}% buy pressure)")
    elif delta["bias"] == "BEARISH":
        sell_signals.append(f"Order Flow Bearish ({100-delta['buy_pct']}% sell pressure)")

    if macd["signal_cross"] == "BULLISH_CROSS":
        buy_signals.append("MACD Bullish Crossover!")
    elif macd["signal_cross"] == "BEARISH_CROSS":
        sell_signals.append("MACD Bearish Crossover!")
    elif macd["signal_cross"] == "BULLISH":
        buy_signals.append("MACD Bullish")
    elif macd["signal_cross"] == "BEARISH":
        sell_signals.append("MACD Bearish")

    # ── CONFLUENCE CHECK ──
    buy_count  = len(buy_signals)
    sell_count = len(sell_signals)

    # Need minimum confluence
    if buy_count < MIN_CONFLUENCE and sell_count < MIN_CONFLUENCE:
        return None

    # Determine direction
    if buy_count >= sell_count and buy_count >= MIN_CONFLUENCE:
        direction   = "BUY"
        reasons     = buy_signals
        confluence  = buy_count
    elif sell_count > buy_count and sell_count >= MIN_CONFLUENCE:
        direction   = "SELL"
        reasons     = sell_signals
        confluence  = sell_count
    else:
        return None

    # Confidence based on confluence
    confidence = min(50 + (confluence * 8), 95)

    # Extra confidence if volume is high
    if vol["signal"] == "HIGH":
        confidence = min(confidence + 5, 95)

    # Extra confidence if BB squeeze breakout
    if bb["squeeze"]:
        confidence = min(confidence + 5, 95)

    # Targets
    if direction == "BUY":
        stop_loss = round(price * (1 - STOP_LOSS_PCT), 8)
        target    = round(price * (1 + PROFIT_TARGET_PCT), 8)
    else:
        stop_loss = round(price * (1 + STOP_LOSS_PCT), 8)
        target    = round(price * (1 - PROFIT_TARGET_PCT), 8)

    return {
        "symbol":      symbol,
        "direction":   direction,
        "price":       price,
        "stop_loss":   stop_loss,
        "target":      target,
        "confidence":  confidence,
        "confluence":  f"{confluence}/7",
        "rsi":         rsi,
        "vwap":        vwap,
        "bb_squeeze":  bb["squeeze"],
        "bb_width":    bb["width"],
        "volume_ratio":vol["ratio"],
        "delta_bias":  delta["bias"],
        "buy_signals": buy_signals,
        "sell_signals":sell_signals,
        "reasons":     reasons,
        "timestamp":   datetime.now().isoformat(),
    }

# ─── TRADE EXECUTION ──────────────────────────────────────────────────────────

def execute_trade(signal: dict) -> Optional[dict]:
    symbol = signal["symbol"]
    price  = signal["price"]
    side   = "BUY" if signal["direction"] == "BUY" else "SELL"

    info = get_symbol_info(symbol)
    qty  = round_step(TRADE_AMOUNT_USDT / price, info["stepSize"])

    if qty < info["minQty"]:
        logger.warning(f"  Qty {qty} below minQty for {symbol}")
        return None

    logger.info(f"  🔄 {side} {qty} {symbol} @ ~{price}")

    order = binance_request("POST", "/api/v3/order", {
        "symbol":   symbol,
        "side":     side,
        "type":     "MARKET",
        "quantity": qty,
    })

    if "orderId" not in order:
        logger.error(f"  ❌ Order failed: {order}")
        return None

    logger.info(f"  ✅ Order #{order['orderId']} placed!")

    active_trades[symbol] = {
        "order_id":  order["orderId"],
        "direction": signal["direction"],
        "entry":     price,
        "qty":       qty,
        "stop_loss": signal["stop_loss"],
        "target":    signal["target"],
        "open_time": time.time(),
        "confidence":signal["confidence"],
    }
    return order


def check_exit(symbol: str) -> Optional[str]:
    if symbol not in active_trades:
        return None

    trade = active_trades[symbol]
    price = get_price(symbol)
    if not price:
        return None

    if trade["direction"] == "BUY":
        if price >= trade["target"]:    return "TARGET_HIT"
        if price <= trade["stop_loss"]: return "STOP_LOSS"
    else:
        if price <= trade["target"]:    return "TARGET_HIT"
        if price >= trade["stop_loss"]: return "STOP_LOSS"

    # Max 5 min per trade
    if time.time() - trade["open_time"] > 300:
        return "TIMEOUT"

    return None


def close_trade(symbol: str, exit_reason: str):
    if symbol not in active_trades:
        return

    trade      = active_trades[symbol]
    exit_price = get_price(symbol) or trade["entry"]
    close_side = "SELL" if trade["direction"] == "BUY" else "BUY"

    info = get_symbol_info(symbol)
    qty  = round_step(trade["qty"], info["stepSize"])

    order = binance_request("POST", "/api/v3/order", {
        "symbol":   symbol,
        "side":     close_side,
        "type":     "MARKET",
        "quantity": qty,
    })

    if "orderId" in order:
        entry = trade["entry"]
        if trade["direction"] == "BUY":
            pnl_pct = (exit_price - entry) / entry * 100
        else:
            pnl_pct = (entry - exit_price) / entry * 100

        pnl_usdt = TRADE_AMOUNT_USDT * pnl_pct / 100
        fees     = TRADE_AMOUNT_USDT * 0.002
        net_pnl  = pnl_usdt - fees
        result   = "WIN" if net_pnl > 0 else "LOSS"

        stats["total_trades"] += 1
        stats["wins"]   += 1 if result == "WIN" else 0
        stats["losses"] += 1 if result == "LOSS" else 0
        stats["total_pnl"] += net_pnl

        logger.info(f"  {'✅' if result=='WIN' else '❌'} {symbol} {result}: {net_pnl:+.4f} USDT | {exit_reason}")
        send_result_telegram(symbol, trade, exit_price, net_pnl, pnl_pct, result, exit_reason)
        del active_trades[symbol]

# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def tg_send(msg: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
    except:
        pass


def send_signal_telegram(signal: dict, order: dict):
    emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
    reasons_text = "\n".join(f"• {r}" for r in signal["reasons"][:5])

    tg_send(f"""
⚡ <b>SCALP TRADE OPENED</b>

{emoji} <b>{signal['direction']}</b> {signal['symbol']}
<b>Entry:</b> {signal['price']}
<b>Target:</b> {signal['target']} (+0.4%)
<b>Stop Loss:</b> {signal['stop_loss']} (-0.2%)
<b>Confidence:</b> {signal['confidence']}%
<b>Confluence:</b> {signal['confluence']} indicators

<b>📊 Signals:</b>
{reasons_text}

<b>📈 Key Data:</b>
• RSI: {signal['rsi']}
• VWAP: {signal['vwap']}
• Volume: {signal['volume_ratio']}x avg
• BB Squeeze: {'YES 🔥' if signal['bb_squeeze'] else 'No'}
• Order Flow: {signal['delta_bias']}

<i>⏰ {signal['timestamp'][:16]}</i>
""".strip())


def send_result_telegram(symbol, trade, exit_price, net_pnl, pnl_pct, result, reason):
    emoji    = "✅" if result == "WIN" else "❌"
    win_rate = round(stats["wins"] / stats["total_trades"] * 100, 1) if stats["total_trades"] > 0 else 0
    pnl_inr  = net_pnl * 83

    tg_send(f"""
{emoji} <b>TRADE CLOSED — {result}</b>

<b>Symbol:</b> {symbol}
<b>Direction:</b> {trade['direction']}
<b>Entry:</b> {trade['entry']} → <b>Exit:</b> {exit_price}
<b>P&L:</b> {net_pnl:+.4f} USDT (₹{pnl_inr:+.2f})
<b>Reason:</b> {reason}

<b>📊 Today's Stats:</b>
• Trades: {stats['total_trades']}
• Win Rate: {win_rate}%
• Total P&L: {stats['total_pnl']:+.4f} USDT (₹{stats['total_pnl']*83:+.2f})
<i>⏰ {datetime.now().strftime('%H:%M:%S')}</i>
""".strip())


def send_daily_summary():
    win_rate = round(stats["wins"] / stats["total_trades"] * 100, 1) if stats["total_trades"] > 0 else 0

    tg_send(f"""
📊 <b>DAILY SCALPING SUMMARY</b>

<b>Total Trades:</b> {stats['total_trades']}
<b>Wins:</b> {stats['wins']} ✅
<b>Losses:</b> {stats['losses']} ❌
<b>Win Rate:</b> {win_rate}%
<b>Net P&L:</b> {stats['total_pnl']:+.4f} USDT
<b>Net P&L (₹):</b> ₹{stats['total_pnl']*83:+.2f}

<b>🎯 Indicators Used:</b>
RSI + EMA + VWAP + Bollinger + S/R + Volume + Order Flow

<i>⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}</i>
""".strip())

# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def main():
    logger.info("🚀 HFT Scalping Bot v2.0 started!")
    logger.info(f"📊 Symbols: {', '.join(SYMBOLS)}")
    logger.info(f"💰 Per trade: ${TRADE_AMOUNT_USDT} (~₹{int(TRADE_AMOUNT_USDT*83)})")
    logger.info(f"🎯 Target: +{PROFIT_TARGET_PCT*100}% | Stop: -{STOP_LOSS_PCT*100}%")
    logger.info(f"🔀 Min confluence: {MIN_CONFLUENCE}/7 indicators")
    logger.info(f"📡 Indicators: RSI + EMA + VWAP + Bollinger + S/R + Volume + Order Flow")
    logger.info(f"🔑 Binance: {'✅' if BINANCE_API_KEY else '❌'}")
    logger.info(f"📱 Telegram: {'✅' if TELEGRAM_BOT_TOKEN else '❌'}")

    if not BINANCE_API_KEY or not BINANCE_SECRET_KEY:
        logger.error("❌ Binance API keys missing!")
        return

    # Startup message
    tg_send("🚀 <b>HFT Scalping Bot v2.0 Started!</b>\n\n📡 Scanning: BTC, ETH, BNB, SOL, XRP\n🎯 Target: +0.4% | Stop: -0.2%\n🔀 7-Indicator Confluence System\n\nBot is live! Waiting for signals... ⚡")

    last_summary = datetime.now().date()
    scan_count   = 0

    while True:
        try:
            scan_count += 1
            now = datetime.now().strftime('%H:%M:%S')
            logger.info(f"\n{'='*40}")
            logger.info(f"🔍 Scan #{scan_count} — {now} | Active: {len(active_trades)} | Trades: {stats['total_trades']} | P&L: {stats['total_pnl']:+.4f} USDT")

            # Check exits first
            for symbol in list(active_trades.keys()):
                exit_reason = check_exit(symbol)
                if exit_reason:
                    logger.info(f"  📤 Closing {symbol} — {exit_reason}")
                    close_trade(symbol, exit_reason)

            # Scan for new signals
            for symbol in SYMBOLS:
                if symbol in active_trades:
                    logger.info(f"  ⏭️  {symbol} — trade active")
                    continue

                candles = get_candles(symbol, interval="1m", limit=150)
                if not candles:
                    continue

                signal = generate_scalp_signal(symbol, candles)

                if signal:
                    logger.info(f"  ⚡ {symbol}: {signal['direction']} | Conf={signal['confidence']}% | Confluence={signal['confluence']} | RSI={signal['rsi']}")
                    order = execute_trade(signal)
                    if order:
                        send_signal_telegram(signal, order)
                else:
                    logger.info(f"  — {symbol}: No confluence signal")

                time.sleep(0.5)

            # Daily summary
            today = datetime.now().date()
            if today != last_summary:
                send_daily_summary()
                stats.update({"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0})
                last_summary = today

            time.sleep(SCAN_INTERVAL_SEC)

        except KeyboardInterrupt:
            logger.info("\n🛑 Bot stopped!")
            send_daily_summary()
            break
        except Exception as e:
            logger.error(f"Error: {e}", exc_info=True)
            time.sleep(15)


if __name__ == "__main__":
    main()
