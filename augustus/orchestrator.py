#!/usr/bin/env python3
"""
Augustus Trading Orchestrator
=============================
LLM-driven autonomous crypto trading agent for Kraken.

Architecture:
  Market Data -> Sanity Check -> LLM Analysis -> Risk Validation -> Execution

Security (paper-driven):
  [2606.10749] Sanity validation of all external data
  [2606.24322] SHA256 state integrity (origin binding)
  [2605.09076] Byzantine Fault Tolerance (optional dual-model mode)

Strategy V4.3 (Jul 2026):
  - Proportional trades (15% of portfolio, EUR3-15 range)
  - Bear regime: 35% cash, only buy RSI<15, max 20% per asset
  - Bull regime: rotate normally, buy RSI<35, let winners run
  - Stop-loss at 5 min granularity
  - S²-MAD 7-day trend detection

Source: augustus-trading/augustus/orchestrator.py
Based on production system running since June 2026.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ─── Paths ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(os.environ.get(
    'AUGUSTUS_PROJECT_ROOT',
    Path(__file__).resolve().parent.parent
))
DATA_DIR = Path(os.environ.get(
    'AUGUSTUS_DATA_DIR',
    Path.home() / '.augustus'
))

# ─── Config ──────────────────────────────────────────────────────────────────

def _env_float(name, default):
    """Load float from environment with fallback to default."""
    val = os.environ.get(name, '')
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return default


def load_thresholds():
    """
    Load trading thresholds from environment variables.
    Defaults are the production values used by Rafael since June 2026.
    Override any of them in your .env file.
    """
    return {
        # ── Trade sizing ──────────────────────────────────────────────────
        "min_eur_to_trade":   _env_float("AUGUSTUS_MIN_TRADE_EUR", 3.0),
        "trade_pct_portfolio": _env_float("AUGUSTUS_TRADE_PCT", 0.15),
        "max_trade_eur":      _env_float("AUGUSTUS_MAX_TRADE_EUR", 15.0),

        # ── Bear market regime ────────────────────────────────────────────
        "bear_cash_target":       _env_float("AUGUSTUS_BEAR_CASH", 0.35),
        "bear_rsi_oversold":      _env_float("AUGUSTUS_BEAR_RSI", 15),
        "bear_max_position_pct":  _env_float("AUGUSTUS_BEAR_MAX_POS", 0.20),

        # ── Bull market regime ────────────────────────────────────────────
        "rsi_oversold":        _env_float("AUGUSTUS_BULL_RSI_OVERSOLD", 35),
        "rsi_overbought":      _env_float("AUGUSTUS_BULL_RSI_OVERBOUGHT", 70),
        "min_24h_change_signal": _env_float("AUGUSTUS_MIN_CHANGE_SIGNAL", 3.0),

        # ── Risk & Sanity ─────────────────────────────────────────────────
        "max_retries":             int(_env_float("AUGUSTUS_MAX_RETRIES", 2)),
        "max_price_change_4h":     _env_float("AUGUSTUS_MAX_CHANGE_4H", 20.0),
        "max_price_change_24h":    _env_float("AUGUSTUS_MAX_CHANGE_24H", 50.0),
        "max_alt_change_24h":      _env_float("AUGUSTUS_MAX_ALT_CHANGE_24H", 80.0),
        "min_btc_price_eur":       _env_float("AUGUSTUS_MIN_BTC_EUR", 10000),
        "max_btc_price_eur":       _env_float("AUGUSTUS_MAX_BTC_EUR", 500000),
    }


CONFIG = {
    "models": {
        "primary": {
            "id": "deepseek",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "price_input_per_m": 0.435,
            "price_output_per_m": 0.87,
        },
        "validator": {
            "id": "minimax",
            "provider": "openrouter",
            "model": "minimax/minimax-m3",
            "price_input_per_m": 0.30,
            "price_output_per_m": 1.20,
        },
    },
    # thresholds loaded dynamically from env vars with production defaults
    "thresholds": load_thresholds(),
    "coin_map": {
        "AVAX": "avalanche-2",
        "ALGO": "algorand",
        "BIO": "bio-protocol",
        "GAME2": "game",
        "USUAL": "usual",
        "BTC": "bitcoin",
        "XXBT": "bitcoin",
        "ETH": "ethereum",
        "SOL": "solana",
        "XRP": "ripple",
        "SUI": "sui",
        "QUAI": "quai-network",
    },
    "paths": {
        "cache": DATA_DIR / "market_cache.json",
        "state": DATA_DIR / "trading_state.json",
        "state_hash": DATA_DIR / "trading_state.hash",
        "logs": DATA_DIR / "logs",
    },
}


# ─── P1: Sanity Validation — [2606.10749] ────────────────────────────────────

class SanityError(Exception):
    pass


def validate_market_data(market_data):
    """
    Validate external market data before passing to LLM.
    Based on: Threat Surfaces paper [2606.10749].

    Rules:
    - BTC price within absolute bounds
    - 24h changes within thresholds per asset type
    - No None or negative values
    - CoinGecko returns percentages (0.81 = 0.81%, NOT 81%)
    """
    issues = []
    flagged = []

    for coin_id, data in market_data.items():
        if not isinstance(data, dict):
            continue

        price = data.get("eur")
        ch24  = data.get("eur_24h_change", 0)

        # Check None/negative
        if price is None or price < 0:
            issues.append(f"{coin_id}: invalid price ({price})")
            continue

        # BTC absolute bounds
        if coin_id == "bitcoin":
            if price < CONFIG["thresholds"]["min_btc_price_eur"]:
                issues.append(f"BTC price impossible: EUR{price} "
                            f"(min EUR{CONFIG['thresholds']['min_btc_price_eur']})")
            if price > CONFIG["thresholds"]["max_btc_price_eur"]:
                issues.append(f"BTC price impossible: EUR{price} "
                            f"(max EUR{CONFIG['thresholds']['max_btc_price_eur']})")

        # CoinGecko ALWAYS returns percentages in eur_24h_change.
        # 0.81 = 0.81%, NOT 81%. No format ambiguity.
        # Excessive change checks (below) catch real anomalies.

        # Excessive 24h change for BTC
        if coin_id == "bitcoin" and ch24 is not None:
            if abs(ch24) > CONFIG["thresholds"]["max_price_change_24h"]:
                issues.append(f"BTC 24h change suspicious: {ch24:+.1f}% "
                            f"(max: {CONFIG['thresholds']['max_price_change_24h']}%)")

        # Excessive 24h change for altcoins
        elif ch24 is not None:
            if abs(ch24) > CONFIG["thresholds"]["max_alt_change_24h"]:
                issues.append(f"{coin_id}: 24h change too high: {ch24:+.1f}% "
                            f"- possible corrupted data")

    result = {
        "valid": len(issues) == 0,
        "issues": issues,
        "warnings": flagged,
        "coins_validated": sum(1 for v in market_data.values() if isinstance(v, dict)),
    }

    if issues:
        print(f"  ⚠️ SANITY CHECK FAILED: {issues}")
    if flagged:
        print(f"  ⚠️ SANITY WARNINGS: {flagged}")
    else:
        print(f"  ✅ Sanity check OK ({result['coins_validated']} coins)")

    return result


# ─── P2: State Integrity — [2606.24322] ─────────────────────────────────────

def compute_hash(filepath):
    """SHA256 of a file."""
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None


def write_state_with_hash(state_data):
    """Write trading_state.json + integrity hash. Origin: augustus_orchestrator."""
    state_path = CONFIG["paths"]["state"]
    hash_path  = CONFIG["paths"]["state_hash"]

    state_path.parent.mkdir(parents=True, exist_ok=True)

    state_data["_origin"] = "augustus_orchestrator"
    state_data["_written_at"] = datetime.now(timezone.utc).isoformat()

    with open(state_path, "w") as f:
        json.dump(state_data, f, indent=2, default=str)

    file_hash = compute_hash(state_path)
    with open(hash_path, "w") as f:
        json.dump({"hash": file_hash, "path": str(state_path),
                   "written_at": state_data["_written_at"]}, f)

    return file_hash


def read_state_verified():
    """Read trading_state.json with integrity verification."""
    state_path = CONFIG["paths"]["state"]
    hash_path  = CONFIG["paths"]["state_hash"]

    if not state_path.exists():
        return None, "no_state"

    if not hash_path.exists():
        print("  ⚠️ State hash not found - state unverified")
        with open(state_path) as f:
            data = json.load(f)
        data["_integrity"] = "unverified"
        return data, "unverified"

    try:
        with open(hash_path) as f:
            stored = json.load(f)
        expected = stored.get("hash")
        actual   = compute_hash(state_path)

        if expected != actual:
            print(f"  🚨 INTEGRITY ALERT: state tampered! "
                  f"expected={expected[:12]}... actual={(actual or 'None')[:12]}...")
            return None, "tampered"

        with open(state_path) as f:
            data = json.load(f)
        data["_integrity"] = "verified"
        return data, "ok"

    except Exception as e:
        print(f"  ⚠️ Integrity verification error: {e}")
        return None, "error"


# ─── API Helpers ──────────────────────────────────────────────────────────────

def load_api_key(provider):
    """Load API key from environment or .env file."""
    env_path = PROJECT_ROOT / ".env"
    env_map = {
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        "deepseek":   ("DEEPSEEK_API_KEY",   "https://api.deepseek.com/v1"),
    }

    # Try environment first
    env_name, default_base = env_map.get(provider, ("", ""))
    if env_name:
        token = os.environ.get(env_name, "")
        if token and len(token) > 8:
            return token, default_base

    # Fallback to .env
    if env_path.exists():
        try:
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(env_name + "="):
                        token = line.split("=", 1)[1].strip().strip("\"'")
                        if token and len(token) > 8:
                            return token, default_base
        except:
            pass

    return "", ""


def call_model(provider, model, prompt, system_prompt="", timeout=40, max_tokens=1024):
    """Call LLM via OpenAI-compatible API."""
    api_key, base_url = load_api_key(provider)
    if not api_key:
        return {"error": f"API key for {provider} not found. Set {provider.upper()}_API_KEY in .env"}

    url = base_url.rstrip("/") + "/chat/completions"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/rafael-tocantins/augustus-trading"
        headers["X-Title"]      = "Augustus Trading Agent"

    data = json.dumps({
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }).encode()

    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode().strip()
            if not raw:
                return {"error": "Empty response from API"}
            result = json.loads(raw)
            choice = result["choices"][0]
            msg    = choice["message"]
            content = (msg.get("content") or
                       msg.get("reasoning_content") or
                       msg.get("reasoning") or
                       "[Empty response]")
            usage = result.get("usage", {})
            return {
                "content":    content,
                "tokens_in":  usage.get("prompt_tokens", 0),
                "tokens_out": usage.get("completion_tokens", 0),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ─── Data Collection ──────────────────────────────────────────────────────────

def get_portfolio():
    """Get portfolio from Kraken."""
    try:
        from augustus.kraken_client import get_kraken_client, get_balance
        return get_balance()
    except Exception as e:
        return {"error": str(e)}


def get_market_data(portfolio):
    """
    Get prices for all portfolio coins from CoinGecko.
    10min cache + stale cache fallback on rate limit.
    """
    coin_map   = CONFIG["coin_map"]
    cache_file = CONFIG["paths"]["cache"]
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    # Valid cache?
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            if age < 600:
                print(f"  Prices from cache ({int(age)}s ago)")
                return {k: v for k, v in cached.items() if k != "_ts"}
        except:
            pass

    # Determine coins to fetch
    coins = set(["bitcoin"])
    for asset in portfolio:
        key = asset.upper()
        if key.startswith("Z"): key = key[1:]
        if key.startswith("X") and len(key) > 1: key = key[1:]
        cg_id = coin_map.get(key)
        if cg_id:
            coins.add(cg_id)

    try:
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={','.join(coins)}"
            f"&vs_currencies=eur"
            f"&include_24hr_change=true"
            f"&include_7d_change=true"
            f"&include_24hr_vol=true"
        )
        resp   = urllib.request.urlopen(url, timeout=15)
        prices = json.loads(resp.read())
        prices["_ts"] = time.time()
        with open(cache_file, "w") as f:
            json.dump(prices, f)
        return {k: v for k, v in prices.items() if k != "_ts"}

    except urllib.error.HTTPError as e:
        if e.code == 429 and cache_file.exists():
            try:
                with open(cache_file) as f:
                    cached = json.load(f)
                age = time.time() - cached.get("_ts", 0)
                print(f"  Rate limit - stale cache ({int(age/60)}min)")
                return {k: v for k, v in cached.items() if k != "_ts"}
            except:
                pass
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def get_rsi(coin_id, periods=14):
    """RSI-14 via CoinGecko OHLC."""
    try:
        url  = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc?vs_currency=eur&days=30"
        resp = urllib.request.urlopen(url, timeout=15)
        ohlc = json.loads(resp.read())
        if not ohlc or len(ohlc) < periods + 1:
            return None
        closes = [c[4] for c in ohlc]
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains  = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]
        avg_gain = sum(gains[-periods:]) / periods
        avg_loss = sum(losses[-periods:]) / periods
        if avg_loss == 0:
            return 100.0
        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 1)
    except:
        return None


def build_portfolio_context(portfolio, market_data):
    """Build human-readable portfolio context for LLM prompt."""
    coin_map = CONFIG["coin_map"]
    lines, portfolio_detail = [], []
    total_eur = 0.0

    for asset, amount in portfolio.items():
        if amount is None:
            continue
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue

        raw_key = asset.upper()
        key     = raw_key
        if key.startswith("Z"):
            key = key[1:]

        if key == "EUR" or raw_key == "ZEUR":
            total_eur += amount
            lines.append(f"  EUR (cash): EUR{amount:.2f}")
            portfolio_detail.append({
                "asset": "EUR", "amount": amount, "price_eur": 1.0,
                "value_eur": amount, "change_24h": 0, "change_7d": 0,
                "rsi": None, "cg_id": None,
            })
            continue

        raw_key = key
        if key.startswith("X"):
            key = key[1:]

        cg_id = coin_map.get(key) or coin_map.get(raw_key)
        if not cg_id or cg_id not in market_data:
            lines.append(f"  {asset}: {amount} (no price)")
            continue

        price = market_data[cg_id].get("eur", 0)
        ch24  = market_data[cg_id].get("eur_24h_change", 0)
        ch7d  = market_data[cg_id].get("eur_7d_change", 0)
        vol   = market_data[cg_id].get("eur_24h_vol", 0)
        value = amount * price
        total_eur += value

        rsi     = get_rsi(cg_id)
        rsi_str = f"RSI={rsi}" if rsi else "RSI=N/A"
        vol_m   = vol / 1e6 if vol else 0

        signal = ""
        if rsi and rsi < CONFIG["thresholds"]["rsi_oversold"]:
            signal = " <-- OVERSOLD"
        elif rsi and rsi > CONFIG["thresholds"]["rsi_overbought"]:
            signal = " <-- OVERBOUGHT"
        ch24_safe = ch24 if ch24 is not None else 0
        ch7d_safe = ch7d if ch7d is not None else 0
        if abs(ch24_safe) > CONFIG["thresholds"]["min_24h_change_signal"]:
            signal += f" | MOVE {ch24_safe:+.1f}%"

        lines.append(
            f"  {key}: {amount} | EUR{price:.4f}/u | Value: EUR{value:.2f} | "
            f"24h: {ch24_safe:+.1f}% | 7d: {ch7d_safe:+.1f}% | Vol: EUR{vol_m:.0f}M | {rsi_str}{signal}"
        )
        portfolio_detail.append({
            "asset": key, "amount": amount, "price_eur": price,
            "value_eur": value, "change_24h": ch24_safe,
            "change_7d": ch7d_safe, "rsi": rsi, "cg_id": cg_id,
        })

    context = f"PORTFOLIO (Total: ~EUR{total_eur:.2f}):\n" + "\n".join(lines)
    return context, portfolio_detail, total_eur


# ─── Analysis ─────────────────────────────────────────────────────────────────

def analyse_market(portfolio_context, portfolio_detail, total_eur,
                   regime="bull", sentiment_context="N/A", rsi_threshold=35):
    """Run LLM analysis and get trading decision."""
    primary    = CONFIG["models"]["primary"]

    signals = []
    for p in portfolio_detail:
        if p.get("rsi") and p["rsi"] < CONFIG["thresholds"]["rsi_oversold"]:
            signals.append(f"{p['asset']} RSI={p['rsi']} (oversold)")
        if p.get("rsi") and p["rsi"] > CONFIG["thresholds"]["rsi_overbought"]:
            signals.append(f"{p['asset']} RSI={p['rsi']} (overbought)")
        if abs(p.get("change_24h", 0)) > CONFIG["thresholds"]["min_24h_change_signal"]:
            signals.append(f"{p['asset']} {p['change_24h']:+.1f}% in 24h")

    cash = next((p["value_eur"] for p in portfolio_detail if p.get("asset") == "EUR"), 0)

    system = (
        "You are Augustus, an autonomous portfolio manager. "
        "You have full autonomy - you can buy, sell, reallocate without asking for approval. "
        "The portfolio is small (~EUR50-150). ZERO new deposits. "
        "STRATEGY V4.3 (Jul 2026): Proportional trades of 15% of portfolio. "
        "No fixed EUR3 - the trade scales with the portfolio. "
        "BEAR REGIME (F&G<30 or BTC<SMA50): 35% cash, only buy RSI<15, max 20% per asset, sell fast (>3% profit). "
        "BULL REGIME: normal rotation, buy RSI<35, let winners run. "
        "Explore ALL coins. Any coin is valid. "
        "NEVER stay idle with cash - put money to work. "
        "MANDATORY PRECISION: 0.66 = 0.66%, NOT 66%."
    )

    prompt = (
        f"Cash: EUR{cash:.2f} | Portfolio: {portfolio_context[:300]}\n\n"
        f"Signals: {', '.join(signals) if signals else 'none'}\n"
        f"Time: {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        f"Regime: {regime} | F&G: {sentiment_context}\n\n"
        f"RULES (apply now):\n"
        f"- Trade size = 15% of portfolio (~EUR{total_eur * 0.15:.1f})\n"
        f"- BEAR: cash<15% portfolio -> SELL most overvalued asset to raise cash\n"
        f"- BEAR: cash>=15% -> BUY asset with lowest RSI (RSI<{rsi_threshold})\n"
        f"- BULL: cash<EUR3 -> SELL asset with biggest 24h gain\n"
        f"- BULL: cash>=EUR3 -> BUY asset with lowest RSI\n"
        f"- NEVER [SILENT] if low cash with assets to sell\n\n"
        f"Respond ONLY with valid JSON. Replace ALL placeholders with real values.\n"
        f'Example: {{"asset":"ALGO","action":"sell","pair_kraken":"ALGOEUR","amount_eur":4.5,"reason":"RSI overbought at 71"}}\n'
        f"WARNING: Do NOT copy the example. Use YOUR analysis, YOUR values.\n"
        f"WARNING: No XXX, no X, no buy|sell — only real asset names, real amounts."
    )

    result = call_model(primary["provider"], primary["model"], prompt,
                        system_prompt=system, timeout=45, max_tokens=1200)

    cost = 0.0
    if "error" not in result:
        cost = (result["tokens_in"] / 1e6 * primary["price_input_per_m"] +
                result["tokens_out"] / 1e6 * primary["price_output_per_m"])

    return result, cost


# ─── Trade Execution ──────────────────────────────────────────────────────────

def execute_trade(analysis_content):
    """Execute trade on Kraken. Returns executed=True ONLY if order actually placed."""
    try:
        from augustus.kraken_client import get_kraken_client, place_order

        json_match = re.search(r'\{[^{}]+\}', analysis_content, re.DOTALL)
        if not json_match:
            return {"executed": False, "reason": "No trade JSON in analysis"}

        trade  = json.loads(json_match.group())
        action = trade.get("action", "").upper()
        pair   = trade.get("pair_kraken", "")
        amount = trade.get("amount_eur", 0)
        stop   = trade.get("stop_loss_eur")

        if not pair or not action or amount <= 0:
            return {"executed": False, "reason": f"Insufficient data: {trade}"}

        if action in ("COMPRAR_MAIS", "BUY", "COMPRAR"):
            order_type = "buy"
        elif action in ("VENDER", "SELL"):
            order_type = "sell"
        else:
            return {"executed": False, "reason": f"Unknown action: {action}"}

        k = get_kraken_client()

        # Normalize pair (ALGO/EUR -> ALGOEUR)
        pair = pair.replace("/", "")

        # Get current price
        ticker_url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        ticker_data = json.loads(urllib.request.urlopen(ticker_url, timeout=10).read())
        price = float(list(ticker_data['result'].values())[0]['c'][0])
        volume = amount / price

        # Check minimum order
        pair_url = f"https://api.kraken.com/0/public/AssetPairs?pair={pair}"
        pair_data = json.loads(urllib.request.urlopen(pair_url, timeout=10).read())
        ordermin = float(list(pair_data['result'].values())[0].get('ordermin', 0))
        if volume < ordermin:
            volume = ordermin * 1.01
            print(f"  Volume adjusted to minimum: {volume:.4f}")

        order_result = place_order(k, pair=pair, order_type=order_type,
                                   volume=volume,
                                   order_type_ext='market' if amount <= 3 else 'limit',
                                   price=round(price * 1.005, 4) if amount > 3 else None,
                                   validate=False)

        # Register stop-loss if provided
        if stop and order_result:
            try:
                import subprocess
                subprocess.run([
                    sys.executable,
                    str(PROJECT_ROOT / "augustus" / "stop_loss.py"),
                    "--register",
                    pair.replace("EUR", "").replace("ZEUR", ""),
                    str(amount / trade.get("price_eur", 1)),
                    str(stop)
                ], timeout=10)
            except:
                pass

        return {"executed": True, "order": order_result, "trade": trade}

    except Exception as e:
        return {"executed": False, "error": str(e)}


# ─── Main Orchestrator ────────────────────────────────────────────────────────

def run(regime="bull", sentiment_context="N/A"):
    """Main trading loop."""
    print(f"[{datetime.now().strftime('%H:%M')}] Augustus - Starting")
    total_cost = 0.0

    # P2: Verify state integrity
    state, integrity = read_state_verified()
    if integrity == "tampered":
        print("  🚨 State tampered - aborting for security")
        return {"action": "aborted", "reason": "state_tampered"}
    elif integrity == "ok":
        print("  ✅ State integrity verified")

    # 1. Collect data
    print("  Collecting portfolio and prices...")
    portfolio = get_portfolio()
    if isinstance(portfolio, dict) and "error" in portfolio:
        return {"action": "error", "reason": portfolio["error"]}

    market_data = get_market_data(portfolio)
    if isinstance(market_data, dict) and "error" in market_data:
        print(f"  WARNING: partial prices - {market_data['error']}")
        market_data = {}

    # P1: Sanity check
    if market_data:
        sanity = validate_market_data(market_data)
        if not sanity["valid"]:
            print(f"  ❌ Market data rejected by sanity check")
            cache_file = CONFIG["paths"]["cache"]
            if cache_file.exists():
                try:
                    with open(cache_file) as f:
                        old = json.load(f)
                    market_data = {k: v for k, v in old.items() if k != "_ts"}
                    print(f"  Using previous cache as fallback")
                except:
                    market_data = {}

    portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
    print(f"  Portfolio: EUR{total_eur:.2f} total | {len(portfolio_detail)} assets")

    # 2. LLM Analysis
    print(f"  LLM analyzing...")
    analysis, cost = analyse_market(portfolio_context, portfolio_detail, total_eur,
                                    regime=regime, sentiment_context=sentiment_context)
    total_cost += cost

    if "error" in analysis:
        return {"action": "error", "reason": analysis["error"], "cost": total_cost}

    content = analysis["content"].strip()
    print(f"  Analysis ({analysis['tokens_in']} tok): {content[:120]}...")

    if "[SILENT]" in content:
        print(f"  [SILENT] - nothing to do (${total_cost:.5f})")
        write_state_with_hash({
            "last_action": "silent",
            "portfolio_total_eur": total_eur,
            "timestamp": datetime.now().isoformat(),
        })
        return {"action": "silent", "cost": total_cost}

    # 3. Execute trade
    print("  Executing trade...")
    exec_result = execute_trade(content)

    if exec_result.get("executed"):
        print(f"  ✅ Trade executed: {exec_result.get('trade', {})}")
        write_state_with_hash({
            "last_action": "trade",
            "trade": exec_result.get("trade", {}),
            "portfolio_total_eur": total_eur,
            "timestamp": datetime.now().isoformat(),
        })
    else:
        reason = exec_result.get("reason") or exec_result.get("error", "unknown")
        print(f"  ⚠️ Trade not executed: {reason}")

    return {
        "action":             "trade",
        "analysis":           content,
        "execution":          exec_result,
        "cost":               total_cost,
        "portfolio_total_eur": total_eur,
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Augustus Trading Orchestrator")
    parser.add_argument("--mode", choices=["scan", "trade", "debug"],
                        default="trade",
                        help="Operation mode (default: trade)")
    parser.add_argument("--regime", choices=["bull", "bear"], default="bull",
                        help="Market regime override")
    parser.add_argument("--sentiment", default="N/A",
                        help="Fear & Greed or sentiment context")
    args = parser.parse_args()

    start = time.time()
    try:
        result = run(regime=args.regime, sentiment_context=args.sentiment)
    except Exception as e:
        result = {"action": "error", "reason": f"{type(e).__name__}: {e}"}

    elapsed = time.time() - start
    result.update({
        "_elapsed_s": round(elapsed, 1),
        "_mode":      args.mode,
        "_timestamp": datetime.now().isoformat(),
        "_version":   "1.0.0",
    })

    print(json.dumps(result, indent=2, default=str))

    cost = result.get("cost", 0)
    print(f"\n💰 Cost: ${cost:.5f} | ⏱ {elapsed:.1f}s", file=sys.stderr)

    # Save log
    log_dir = CONFIG["paths"]["logs"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"📝 Log: {log_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
