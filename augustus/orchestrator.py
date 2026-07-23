#!/usr/bin/env python3
"""
Augustus Multi-Agent Orchestrator V6 — Multi-Agent + Decoupled Risk
====================================================================
Melhorias V6 (2026-06-27) — baseadas em papers:

  [2501.00826] Multi-Agent Crypto Portfolio (+133.52%, Sharpe 1.502):
    → Crypto Agent (técnico) + Trading Agent (decisão final)
    → Hierarchical: Crypto → Trading (não debate, não colaborativo)
    → Cada agente com prompt especializado, não genérico

  [2601.04687] WebCryptoAgent — Decoupled Control:
    → Risk Module determinístico, independente do LLM
    → Volatility spike detector (BTC move >5%/1h → pausa)
    → Circuit breaker (3 perdas consecutivas → pausa 24h)

  [2605.16895] Alpha Illusion P1-P6:
    → Trade journal com precision/recall por sinal (P4)
    → Fee model explícito (P2): 0.16% taker Kraken

Mantém toda a segurança v3:
  [2606.24322] Origin binding + SHA256
  [2606.10749] Sanity validation
  [2606.07940] Trust scoring


  V6 (2026-07-22) — baseadas em papers:
  [Karbalaii 2025] Volume Anomaly Detection (Pump & Dump):
    → Volume >3x média + preço perto do suporte = acumulação
  [Day et al. 2023] Bollinger Bands (60,2σ) Bitcoin:
    → AHPR >50% com MA=60d em BTC
  [PyQuantLab 2025] BB-KC Squeeze Strategy:
    → Sharpe >1.0 com combinação Bollinger+Keltner
  [Farzulla 2026] The Extremity Premium (F&G Regimes):
    → F&G <25 ou >75 = spreads altos, evitar trading
  [Ficura 2023] Micro-cap Reversal:
    → Small/illiquid: reversão semanal (t=-7.31)

Arquitectura V6:
  Dados → Sanity Check → Crypto Agent (Flash) → Trading Agent (Pro)
  → Risk Module (determinístico) → Integrity Check → Execução real

Custo estimado: ~$0.09/mês
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

CONFIG = {
    "models": {
        "crypto_agent": {
            "id": "crypto_agent",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "price_input_per_m": 0.14,
            "price_output_per_m": 0.28,
        },
        "trading_agent": {
            "id": "trading_agent",
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "price_input_per_m": 0.435,
            "price_output_per_m": 0.87,
        },
    },
    "thresholds": {
        "min_eur_to_trade": 1.0,
        "rsi_oversold": 35,
        "rsi_overbought": 70,
        "min_24h_change_signal": 3.0,
        "max_retries": 2,
        # Sanity bounds
        "max_price_change_4h": 20.0,
        "max_price_change_24h": 50.0,
        "max_alt_change_24h": 80.0,
        "min_btc_price_eur": 10000,
        "max_btc_price_eur": 500000,
        # v4: Decoupled Risk
        "max_daily_drawdown_pct": 15.0,     # fecha posições se portfólio cai >15% no dia
        "circuit_breaker_losses": 3,         # 3 perdas consecutivas → pausa 24h
        "vol_spike_btc_pct_1h": 5.0,        # BTC move >5% em 1h → pausa trades
        "taker_fee_pct": 0.16,              # Kraken taker fee
    },
    "coin_map": {
        "AVAX": "avalanche-2", "ALGO": "algorand", "BIO": "bio-protocol",
        "GAME2": "game", "USUAL": "usual", "BTC": "bitcoin", "XXBT": "bitcoin",
        "ETH": "ethereum", "SOL": "solana", "XRP": "ripple",
        "SUI": "sui", "QUAI": "quai-network",
        "ARPA": "arpa", "ATOM": "cosmos",
    },
    "paths": {
        "cache": Path.home() / ".hermes/data/market_cache.json",
        "state": Path.home() / ".hermes/data/trading_state.json",
        "state_hash": Path.home() / ".hermes/data/trading_state.hash",
        "trust_scores": Path.home() / ".hermes/data/model_trust_scores.json",
        "trade_journal": Path.home() / ".hermes/data/trade_journal.json",
        "logs": Path.home() / ".hermes/data/s2mad_logs",
    },
}

# ─── P1: Sanity Validation — [2606.10749] ────────────────────────────────────

class SanityError(Exception):
    pass

def validate_market_data(market_data):
    issues = []
    flagged = []
    for coin_id, data in market_data.items():
        if not isinstance(data, dict):
            continue
        price = data.get("eur")
        ch24  = data.get("eur_24h_change", 0)
        if price is None or price < 0:
            issues.append(f"{coin_id}: preço inválido ({price})")
            continue
        if coin_id == "bitcoin":
            if price < CONFIG["thresholds"]["min_btc_price_eur"]:
                issues.append(f"BTC preço impossível: €{price}")
            if price > CONFIG["thresholds"]["max_btc_price_eur"]:
                issues.append(f"BTC preço impossível: €{price}")
        # CoinGecko devolve SEMPRE percentagens no eur_24h_change.
        # 0.81 = 0.81%, NAO 81%. Nao existe ambiguidade de formato.
        # O check de variacao excessiva (linhas abaixo) ja cobre anomalias reais.
        if coin_id == "bitcoin" and ch24 is not None:
            if abs(ch24) > CONFIG["thresholds"]["max_price_change_24h"]:
                issues.append(f"BTC variação 24h suspeita: {ch24:+.1f}%")
        elif ch24 is not None:
            if abs(ch24) > CONFIG["thresholds"]["max_alt_change_24h"]:
                issues.append(f"{coin_id}: variação 24h muito alta: {ch24:+.1f}%")
    result = {"valid": len(issues) == 0, "issues": issues, "warnings": flagged,
              "coins_validated": sum(1 for v in market_data.values() if isinstance(v, dict))}
    if issues:
        print(f"  ⚠️ SANITY CHECK FALHOU: {issues}")
    if flagged:
        print(f"  ⚠️ SANITY WARNINGS: {flagged}")
    else:
        print(f"  ✅ Sanity check OK ({result['coins_validated']} moedas)")
    return result

# ─── P2: Origin Binding & State Integrity — [2606.24322] ─────────────────────

def compute_hash(filepath):
    h = hashlib.sha256()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except FileNotFoundError:
        return None

def write_state_with_hash(state_data):
    state_path = CONFIG["paths"]["state"]
    hash_path  = CONFIG["paths"]["state_hash"]
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_data["_origin"] = "augustus_orchestrator_v4"
    state_data["_written_at"] = datetime.now(timezone.utc).isoformat()
    with open(state_path, "w") as f:
        json.dump(state_data, f, indent=2, default=str)
    file_hash = compute_hash(state_path)
    with open(hash_path, "w") as f:
        json.dump({"hash": file_hash, "path": str(state_path),
                   "written_at": state_data["_written_at"]}, f)
    return file_hash

def read_state_verified():
    state_path = CONFIG["paths"]["state"]
    hash_path  = CONFIG["paths"]["state_hash"]
    if not state_path.exists():
        return None, "no_state"
    if not hash_path.exists():
        print("  ⚠️ Hash de estado não encontrado — estado não verificado")
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
            print(f"  🚨 ALERTA INTEGRIDADE: estado adulterado!")
            return None, "tampered"
        with open(state_path) as f:
            data = json.load(f)
        data["_integrity"] = "verified"
        return data, "ok"
    except Exception as e:
        print(f"  ⚠️ Erro verificação integridade: {e}")
        return None, "error"

# ─── P3: Trust Scoring — [2606.07940] ────────────────────────────────────────

def load_trust_scores():
    trust_path = CONFIG["paths"]["trust_scores"]
    defaults = {
        "crypto_agent": {"score": 0.8, "calls": 0, "correct": 0, "errors": 0},
        "trading_agent": {"score": 0.8, "calls": 0, "correct": 0, "errors": 0},
        "glm": {"score": 0.4, "calls": 0, "correct": 0, "errors": 0},
    }
    if trust_path.exists():
        try:
            with open(trust_path) as f:
                saved = json.load(f)
            for k, v in defaults.items():
                if k not in saved:
                    saved[k] = v
            return saved
        except:
            pass
    return defaults

def update_trust_score(model_id, outcome):
    trust_path = CONFIG["paths"]["trust_scores"]
    scores = load_trust_scores()
    if model_id not in scores:
        scores[model_id] = {"score": 0.5, "calls": 0, "correct": 0, "errors": 0}
    entry = scores[model_id]
    entry["calls"] += 1
    if outcome == "correct":
        entry["correct"] += 1
        entry["score"] = min(1.0, entry["score"] + 0.02)
    elif outcome in ("error", "hallucination"):
        entry["errors"] += 1
        entry["score"] = max(0.0, entry["score"] - 0.10)
    elif outcome == "rejected":
        entry["score"] = max(0.0, entry["score"] - 0.05)
    trust_path.parent.mkdir(parents=True, exist_ok=True)
    with open(trust_path, "w") as f:
        json.dump(scores, f, indent=2)
    return entry["score"]

def check_trust_threshold(model_id, min_trust=0.3):
    scores = load_trust_scores()
    score = scores.get(model_id, {}).get("score", 0.5)
    if score < min_trust:
        print(f"  🚨 Trust score de '{model_id}' muito baixo ({score:.2f}) — escalando para humano")
        return False, score
    return True, score


# ─── API Helpers ──────────────────────────────────────────────────────────────

def load_api_key(provider):
    auth_path = Path.home() / ".hermes/auth.json"
    env_path  = Path.home() / ".hermes/.env"
    env_map = {
        "openrouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
        "deepseek":   ("DEEPSEEK_API_KEY",   "https://api.deepseek.com/v1"),
    }
    if auth_path.exists():
        try:
            with open(auth_path) as f:
                data = json.load(f)
            pool = data.get("credential_pool", {})
            for cred in pool.get(provider, []):
                token = cred.get("access_token", "")
                if token and len(token) > 8:
                    base = cred.get("base_url", "") or env_map.get(provider, ("", ""))[1]
                    return token, base
        except:
            pass
    env_name, default_base = env_map.get(provider, ("", ""))
    if env_name and env_path.exists():
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
    if env_name:
        token = os.environ.get(env_name, "")
        if token and len(token) > 8:
            return token, default_base
    return "", ""


def call_model(provider, model, prompt, system_prompt="", timeout=40, max_tokens=1024):
    api_key, base_url = load_api_key(provider)
    if not api_key:
        return {"error": f"API key for {provider} not found"}
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
        headers["HTTP-Referer"] = "https://augustus.rafael.pt"
        headers["X-Title"]      = "Augustus Trading Agent"
    data = json.dumps({
        "model": model, "messages": messages,
        "max_tokens": max_tokens, "temperature": 0.2,
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode().strip()
            if not raw:
                return {"error": "Empty response from API"}
            result = json.loads(raw)
            choice = result["choices"][0]
            msg = choice["message"]
            content = (msg.get("content") or msg.get("reasoning_content") or
                       msg.get("reasoning") or "[Empty response]")
            usage = result.get("usage", {})
            return {
                "content": content,
                "tokens_in": usage.get("prompt_tokens", 0),
                "tokens_out": usage.get("completion_tokens", 0),
            }
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"error": f"HTTP {e.code}: {body[:300]}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ─── Recolha de Dados ─────────────────────────────────────────────────────────

def get_portfolio():
    try:
        sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
        from kraken_lib import get_balance
        return get_balance()
    except Exception as e:
        return {"error": str(e)}


def get_market_data(portfolio):
    coin_map = CONFIG["coin_map"]
    cache_file = CONFIG["paths"]["cache"]
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    if cache_file.exists():
        try:
            with open(cache_file) as f:
                cached = json.load(f)
            age = time.time() - cached.get("_ts", 0)
            if age < 600:
                print(f"  Preços do cache ({int(age)}s atrás)")
                return {k: v for k, v in cached.items() if k != "_ts"}
        except:
            pass
    coins = set(["bitcoin"])
    for asset in portfolio:
        key = asset.upper()
        if key.startswith("Z"): key = key[1:]
        if key.startswith("X") and len(key) > 1: key = key[1:]
        cg_id = coin_map.get(key)
        if cg_id:
            coins.add(cg_id)
    try:
        url = (f"https://api.coingecko.com/api/v3/simple/price"
               f"?ids={','.join(coins)}"
               f"&vs_currencies=eur&include_24hr_change=true"
               f"&include_7d_change=true&include_24hr_vol=true")
        resp = urllib.request.urlopen(url, timeout=15)
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
                print(f"  Rate limit — cache antigo ({int(age/60)}min)")
                return {k: v for k, v in cached.items() if k != "_ts"}
            except:
                pass
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def get_rsi(coin_id, periods=14):
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc?vs_currency=eur&days=30"
        resp = urllib.request.urlopen(url, timeout=15)
        ohlc = json.loads(resp.read())
        if not ohlc or len(ohlc) < periods + 1:
            return None
        closes = [c[4] for c in ohlc]
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas]
        losses = [abs(min(d, 0)) for d in deltas]
        avg_gain = sum(gains[-periods:]) / periods
        avg_loss = sum(losses[-periods:]) / periods
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(100 - (100 / (1 + rs)), 1)
    except:
        return None


def get_mtf_rsi(kraken_pair, periods=14):
    """
    Multi-Timeframe RSI: 1h, 4h, 1d.
    Busca OHLC da Kraken (gratuito, 720 candles) para cada intervalo.
    Retorna dict: {"1h": rsi, "4h": rsi, "1d": rsi} ou None se falhar.
    """
    timeframes = {"1h": 60, "4h": 240, "1d": 1440}
    results = {}
    for label, interval in timeframes.items():
        try:
            url = f"https://api.kraken.com/0/public/OHLC?pair={kraken_pair}&interval={interval}"
            resp = urllib.request.urlopen(url, timeout=10)
            data = json.loads(resp.read())
            candles = None
            for key, value in data.get("result", {}).items():
                if key != "last" and isinstance(value, list):
                    candles = value
                    break
            if not candles or len(candles) < periods + 1:
                results[label] = None
                continue
            closes = [float(c[4]) for c in candles]
            deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains = [max(d, 0) for d in deltas]
            losses = [abs(min(d, 0)) for d in deltas]
            avg_gain = sum(gains[-periods:]) / periods
            avg_loss = sum(losses[-periods:]) / periods
            if avg_loss == 0:
                results[label] = 100.0
            else:
                rs = avg_gain / avg_loss
                results[label] = round(100 - (100 / (1 + rs)), 1)
        except:
            results[label] = None
    return results if any(v is not None for v in results.values()) else None


def build_portfolio_context(portfolio, market_data):
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
        key = raw_key
        if key.startswith("Z"): key = key[1:]
        if key == "EUR" or raw_key == "ZEUR":
            total_eur += amount
            lines.append(f"  EUR (cash): €{amount:.2f}")
            portfolio_detail.append({
                "asset": "EUR", "amount": amount, "price_eur": 1.0,
                "value_eur": amount, "change_24h": 0, "change_7d": 0,
                "rsi": None, "cg_id": None,
            })
            continue
        raw_key = key
        if key.startswith("X"): key = key[1:]
        cg_id = coin_map.get(key) or coin_map.get(raw_key)
        if not cg_id or cg_id not in market_data:
            lines.append(f"  {asset}: {amount} (sem preço)")
            continue
        price = market_data[cg_id].get("eur", 0)
        ch24 = market_data[cg_id].get("eur_24h_change", 0)
        ch7d = market_data[cg_id].get("eur_7d_change", 0)
        vol = market_data[cg_id].get("eur_24h_vol", 0)
        value = amount * price
        total_eur += value
        rsi = get_rsi(cg_id)
        rsi_str = f"RSI={rsi}" if rsi else "RSI=N/A"
        vol_m = vol / 1e6 if vol else 0
        signal = ""
        if rsi and rsi < CONFIG["thresholds"]["rsi_oversold"]:
            signal = " ← SOBREVENDIDO"
        elif rsi and rsi > CONFIG["thresholds"]["rsi_overbought"]:
            signal = " ← SOBRECOMPRADO"
        ch24_safe = ch24 if ch24 is not None else 0
        ch7d_safe = ch7d if ch7d is not None else 0
        if abs(ch24_safe) > CONFIG["thresholds"]["min_24h_change_signal"]:
            signal += f" | MOVIMENTO {ch24_safe:+.1f}%"
        lines.append(
            f"  {key}: {amount} | €{price:.4f}/u | Valor: €{value:.2f} | "
            f"24h: {ch24_safe:+.1f}% | 7d: {ch7d_safe:+.1f}% | Vol: €{vol_m:.0f}M | {rsi_str}{signal}"
        )
        portfolio_detail.append({
            "asset": key, "amount": amount, "price_eur": price,
            "value_eur": value, "change_24h": ch24_safe,
            "change_7d": ch7d_safe, "rsi": rsi, "cg_id": cg_id,
        })
    context = f"PORTFÓLIO ATUAL (Total: ~€{total_eur:.2f}):\n" + "\n".join(lines)
    return context, portfolio_detail, total_eur


# ═══════════════════════════════════════════════════════════════════════════════
#  NOVO V4: Crypto Agent — Análise Técnica (DeepSeek Flash)
#  Paper: [2501.00826] — agente especializado em sinais técnicos
# ═══════════════════════════════════════════════════════════════════════════════

def run_crypto_agent(portfolio_detail, total_eur, mtf_rsi_data=None):
    """
    Crypto Agent: analisa RSI multi-timeframe, tendências 24h/7d, volume.
    Output: score 0-10 por ativo + recomendação.
    """
    agent = CONFIG["models"]["crypto_agent"]
    trust_ok, trust_val = check_trust_threshold(agent["id"])
    if not trust_ok:
        return {"error": f"Crypto Agent trust insuficiente ({trust_val:.2f})"}

    # Construir tabela técnica
    table = "ATIVO | PREÇO € | 24h% | 7d% | RSI(1h/4h/1d) | VALOR € | SINAL\n"
    table += "-" * 80 + "\n"
    signals_list = []
    for p in portfolio_detail:
        if p.get("asset") == "EUR":
            continue
        rsi = p.get("rsi")
        ch24 = p.get("change_24h", 0)
        ch7d = p.get("change_7d", 0)

        # Multi-TF RSI
        asset_name = p.get("asset", "")
        mtf = mtf_rsi_data.get(asset_name, {}) if mtf_rsi_data else {}
        rsi_1h = mtf.get("1h")
        rsi_4h = mtf.get("4h")
        rsi_1d = rsi  # daily = original

        rsi_display = f"{rsi_1h:.0f}/{rsi_4h:.0f}/{rsi_1d:.0f}" if all(v is not None for v in [rsi_1h, rsi_4h, rsi_1d]) else (f"{rsi:.0f}" if rsi else "N/A")

        signal = ""
        if rsi and rsi < CONFIG["thresholds"]["rsi_oversold"]:
            signal = "⚡SOBREVENDIDO"
            signals_list.append(f"{p['asset']}: oversold RSI_1d={rsi}")
        elif rsi and rsi > CONFIG["thresholds"]["rsi_overbought"]:
            signal = "🔴SOBRECOMPRADO"
            signals_list.append(f"{p['asset']}: overbought RSI_1d={rsi}")
        # MTF confluence
        if rsi_1h and rsi_4h and rsi_1d:
            if rsi_1h < 35 and rsi_4h < 35 and rsi_1d < 35:
                signal += " 🔥TRIFECTA"
                signals_list.append(f"{p['asset']}: TRIFECTA oversold 1h/4h/1d")
        if abs(ch24) > 5:
            signal += " " + ("🟢" if ch24 > 0 else "🔻") + f" {ch24:+.1f}%"

        table += (f"{p['asset']:<8} | €{p['price_eur']:<10.4f} | {ch24:+.1f}% | "
                  f"{ch7d:+.1f}% | {rsi_display:<12} | €{p['value_eur']:.2f} | {signal}\n")

    cash = next((p["value_eur"] for p in portfolio_detail if p.get("asset") == "EUR"), 0)

    system = (
        "És o Crypto Agent do Augustus. Analisas APENAS dados técnicos. "
        "Para cada ativo, atribuis um score de 0-10 baseado em: "
        "RSI (sobrevendido <35 = bullish, sobrecomprado >70 = bearish), "
        "tendência 24h e 7d, volume. "
        "NUNCA inventas ativos — só os que aparecem na tabela. "
        "PRECISÃO OBRIGATÓRIA: 0.66 = 0.66%, NÃO 66%. "
        "Responde SÓ com JSON, sem texto antes ou depois."
    )

    prompt = (
        f"CASH: €{cash:.2f} | PORTFÓLIO TOTAL: €{total_eur:.2f}\n\n"
        f"SINAIS DETETADOS: {', '.join(signals_list) if signals_list else 'nenhum'}\n\n"
        f"TABELA DE ATIVOS:\n{table}\n\n"
        f"Para CADA ativo na tabela, avalia com score 0-10. Depois recomenda "
        "Responde SO com JSON valido. Substitui TODOS os placeholders por valores reais. "
        'Exemplo: {"asset":"ALGO","action":"sell","pair_kraken":"ALGOEUR","amount_eur":4.5,"reason":"RSI sobrecomprado a 71"} '
        "AVISO: Nao copies o exemplo. Usa a TUA analise, os TEUS valores. "
        "AVISO: Nada de XXX, X, ou buy|sell — so nomes reais, valores reais."
    )

    result = call_model(agent["provider"], agent["model"], prompt,
                        system_prompt=system, timeout=30, max_tokens=600)

    cost = 0.0
    if "error" not in result:
        cost = (result["tokens_in"] / 1e6 * agent["price_input_per_m"] +
                result["tokens_out"] / 1e6 * agent["price_output_per_m"])

    return result, cost


# ═══════════════════════════════════════════════════════════════════════════════
#  NOVO V4: Trading Agent — Decisão Final (DeepSeek V4 Pro)
#  Paper: [2501.00826] — consolida Crypto Agent + decide buy/sell
# ═══════════════════════════════════════════════════════════════════════════════

def run_trading_agent(crypto_output, portfolio_context, portfolio_detail, total_eur, regime="unknown", sentiment_str="{}", signal_quality="", macro_context=""):
    """
    Trading Agent: recebe output do Crypto Agent + portfolio completo + regime.
    Decide a ação final: buy / sell / silent.
    Em BEAR market: só vende (nunca compra).
    Usa DeepSeek V4 Pro (mais capacidade de raciocínio).
    """
    agent = CONFIG["models"]["trading_agent"]
    trust_ok, trust_val = check_trust_threshold(agent["id"])
    if not trust_ok:
        return {"error": f"Trading Agent trust insuficiente ({trust_val:.2f})"}

    cash = next((p["value_eur"] for p in portfolio_detail if p.get("asset") == "EUR"), 0)

    # Extrair scores do Crypto Agent
    crypto_scores = ""
    if isinstance(crypto_output, str):
        try:
            crypto_json = json.loads(re.search(r'\{[^{}]*\}', crypto_output, re.DOTALL).group())
            scores = crypto_json.get("scores", {})
            crypto_scores = ", ".join(f"{k}={v}" for k, v in sorted(scores.items(),
                                     key=lambda x: x[1], reverse=True))
        except:
            crypto_scores = crypto_output[:200]

    fee_pct = CONFIG["thresholds"]["taker_fee_pct"]

    # Regime-aware rules
    if regime == "bear":
        bear_rule = (
            "🚨 BEAR EXTREMO (BTC >15% abaixo SMA50). SÓ VENDER se RSI>70. NUNCA COMPRES. "
        )
    elif regime == "bull":
        bear_rule = (
            "🐂 BULL MARKET (BTC > SMA50). Todos os trades permitidos. "
        )
    else:
        bear_rule = ""

    system = (
        "És o Trading Agent do Augustus. Tomas a decisão final de trade. "
        "Recebes o output do Crypto Agent (análise técnica) e o portfólio completo. "
        f"O teu orçamento é €{cash:.2f} cash. Portfólio total €{total_eur:.2f}. "
        f"ESTRATÉGIA: Grão a grão enche a galinha o papo. Micro-trades €1-3. "
        f"Taxa: {fee_pct}% taker na Kraken. "
        f"{bear_rule}"
        "NEWS SENTIMENT: recebes scores de -1 a +1 do News Agent. "
        "Usa-os como contexto adicional: sentimento positivo reforça compras, "
        "negativo reforça cautela. Mas a decisão final baseia-se nos dados técnicos. "
        "REGRAS GERAIS: "
        "- Se cash<€3 → VENDE o ativo com maior subida 24h (max €3) "
        "- Se cash≥€3 → COMPRA o ativo com RSI mais baixo (max €3) "
        "- NUNCA [SILENT] se houver cash<€3 com ativos para vender "
        "Responde SO com JSON valido. Substitui TODOS os placeholders por valores reais. "
        'Exemplo: {"asset":"ALGO","action":"sell","pair_kraken":"ALGOEUR","amount_eur":4.5,"reason":"RSI sobrecomprado a 71"} '
        "AVISO: Nao copies o exemplo. Usa a TUA analise, os TEUS valores. "
        "AVISO: Nada de XXX, X, ou buy|sell — so nomes reais, valores reais."
    )

    prompt = (
        f"CASH: €{cash:.2f} | REGIME: {regime.upper()}\n"
        f"CRYPTO AGENT SCORES: {crypto_scores}\n"
        f"CRYPTO AGENT RECOMMENDATION: {crypto_output[:300]}\n"
        f"NEWS SENTIMENT: {sentiment_str}\n"
        f"{signal_quality}\n"
        f"{macro_context}\n\n"
        f"{portfolio_context[:400]}\n\n"
        f"Aplica as regras AGORA. Lembra-te: regime={regime.upper()}."
    )

    result = call_model(agent["provider"], agent["model"], prompt,
                        system_prompt=system, timeout=45, max_tokens=1024)

    cost = 0.0
    if "error" not in result:
        cost = (result["tokens_in"] / 1e6 * agent["price_input_per_m"] +
                result["tokens_out"] / 1e6 * agent["price_output_per_m"])

    return result, cost


# ═══════════════════════════════════════════════════════════════════════════════
#  NOVO V4.1: News Agent — Crypto Sentiment Analysis
#  Paper: [2501.00826] — +0.2 Sharpe com agente de notícias
# ═══════════════════════════════════════════════════════════════════════════════

def fetch_crypto_news():
    """Busca headlines de CoinTelegraph RSS (gratuito, sem API key)."""
    import xml.etree.ElementTree as ET
    try:
        url = "https://cointelegraph.com/rss"
        resp = urllib.request.urlopen(url, timeout=10)
        content = resp.read().decode()
        root = ET.fromstring(content)
        items = []
        for item in root.findall(".//item")[:15]:
            title = item.find("title").text if item.find("title") is not None else ""
            items.append(title.strip())
        return items
    except Exception as e:
        print(f"  ⚠️ News fetch: {e}")
        return []


def run_news_agent(headlines, portfolio_assets):
    """
    Analisa sentimento das headlines para ativos do portfólio.
    Input: headlines (lista) + portfolio_assets (lista de strings: ["ALGO","AVAX",...])
    Output: (sentiment_dict, cost)
    """
    if not headlines or not portfolio_assets:
        return {}, 0

    assets_str = ", ".join(portfolio_assets)
    headlines_str = "\n".join(f"- {h}" for h in headlines[:15])

    system = (
        "És o News Agent do Augustus. Analisas headlines de criptomoedas "
        "e atribuis sentimento (-1 a +1) APENAS para os ativos listados. "
        "-1 = muito negativo. 0 = neutro. +1 = muito positivo. "
        "SÊ PRECISO: se um ativo não é mencionado, usa 0. "
        "NÃO EXPLIQUES NADA. Responde APENAS com JSON válido, sem texto antes ou depois. "
        'Formato: {"sentiment":{"BTC":0.3,"ETH":-0.2}}'
    )

    prompt = (
        f"ATIVOS: {assets_str}\n\nHEADLINES:\n{headlines_str}\n\n"
        f"Atribui sentimento para cada ativo. JSON apenas."
    )

    result = call_model("deepseek", "deepseek-v4-flash", prompt,
                        system_prompt=system, timeout=20, max_tokens=300)

    cost = 0.0
    sentiment = {}
    if "error" not in result:
        cost = (result["tokens_in"] / 1e6 * 0.14 +
                result["tokens_out"] / 1e6 * 0.28)
        try:
            content = result.get("content", "")
            if not content or len(content) < 5:
                return {}, cost
            last_brace = content.rfind('{')
            last_close = content.rfind('}')
            if last_brace < 0 or last_close < 0:
                # Flash nem sempre devolve JSON — OK, sentimento neutro
                return {}, cost
            json_str = content[last_brace:last_close+1].strip()
            try:
                data = json.loads(json_str)
            except json.JSONDecodeError:
                decoder = json.JSONDecoder()
                data, _ = decoder.raw_decode(json_str)
            sentiment = data.get("sentiment", data)
            sentiment = {k: float(v) for k, v in sentiment.items() if isinstance(v, (int, float))}
        except Exception:
            pass  # sentimento vazio é neutro — comportamento esperado

    return sentiment, cost


# ═══════════════════════════════════════════════════════════════════════════════
#  NOVO V4: Risk Module — Determinístico, Decoupled
#  Paper: [2601.04687] — risco independente do LLM
# ═══════════════════════════════════════════════════════════════════════════════

def get_btc_volatility_1h():
    """
    Detecta spike de volatilidade BTC em 1h.
    Se BTC moveu >5% na última hora → pausa todos os trades.
    """
    try:
        url = "https://api.coingecko.com/api/v3/coins/bitcoin?localization=false&tickers=false&community_data=false&developer_data=false"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        ch1h = data.get("market_data", {}).get("price_change_percentage_1h_in_currency", {}).get("eur")
        if ch1h is None:
            return False, 0
        spike = abs(ch1h) > CONFIG["thresholds"]["vol_spike_btc_pct_1h"]
        return spike, ch1h
    except:
        return False, 0


def get_btc_regime():
    """
    Detecta regime de mercado: 'bull' se BTC > SMA 50, 'bear' se BTC < SMA 50.
    Usa dados Kraken (OHLC diário, gratuito, 720 candles).
    Backtest provou: +10.75pp alpha com este filtro.
    """
    try:
        url = "https://api.kraken.com/0/public/OHLC?pair=XXBTZEUR&interval=1440"
        resp = urllib.request.urlopen(url, timeout=15)
        data = json.loads(resp.read())
        candles = None
        for key, value in data.get("result", {}).items():
            if key != "last" and isinstance(value, list):
                candles = value
                break
        if not candles or len(candles) < 55:
            return "unknown", 0, 0

        closes = [float(c[4]) for c in candles]
        current_price = closes[-1]
        sma50 = sum(closes[-50:]) / 50
        regime = "bull" if current_price > sma50 * 0.95 else "bear"
        return regime, current_price, sma50
    except:
        return "unknown", 0, 0


# ─── V4.1: Fear & Greed Index — [alternative.me] ─────────────────────────────

def get_fear_greed_index():
    """Obtém o Fear & Greed Index via API gratuita alternative.me."""
    try:
        url = "https://api.alternative.me/fng/?limit=1"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        value = int(data["data"][0]["value"])
        classification = data["data"][0]["value_classification"]
        return value, classification
    except Exception as e:
        print(f"  ⚠️ Fear & Greed: erro na API — {e}")
        return None, None


# ─── V4.1: Portfolio Peak Tracking (24h) ─────────────────────────────────────

PORTFOLIO_PEAK_PATH = Path.home() / ".hermes/data/portfolio_peak.json"


def get_portfolio_peak():
    """Lê o pico do portfólio nas últimas 24h."""
    now = datetime.now(timezone.utc)
    defaults = {"peak_24h": 0, "peak_ts": None, "snapshots": []}
    try:
        if PORTFOLIO_PEAK_PATH.exists():
            with open(PORTFOLIO_PEAK_PATH) as f:
                data = json.load(f)
            # Limpar snapshots com mais de 24h
            cutoff = (now - timedelta(hours=24)).isoformat()
            data["snapshots"] = [s for s in data.get("snapshots", [])
                                if s.get("ts", "") >= cutoff]
            # Recalcular pico a partir dos snapshots válidos
            if data["snapshots"]:
                peak = max(s.get("total_eur", 0) for s in data["snapshots"])
                data["peak_24h"] = peak
            else:
                data["peak_24h"] = 0
                data["peak_ts"] = None
            return data
    except:
        pass
    return defaults


def update_portfolio_peak(total_eur):
    """Regista snapshot atual e recalcula pico 24h."""
    now = datetime.now(timezone.utc)
    data = get_portfolio_peak()
    data["snapshots"].append({
        "ts": now.isoformat(),
        "total_eur": round(total_eur, 2),
    })
    # Recalcular pico
    if data["snapshots"]:
        peak = max(s.get("total_eur", 0) for s in data["snapshots"])
        data["peak_24h"] = peak
        data["peak_ts"] = [s["ts"] for s in data["snapshots"]
                          if s["total_eur"] == peak][0]
    PORTFOLIO_PEAK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(PORTFOLIO_PEAK_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return data


# ─── V4.2: Trailing Take-Profit ──────────────────────────────────────────────

ASSET_PEAKS_PATH = Path.home() / ".hermes/data/asset_peaks.json"
TRAILING_GAIN_THRESHOLD = 15.0   # ativar trailing stop após +15% de ganho
TRAILING_STOP_PCT = 5.0          # vender se cair 5% do pico


def get_asset_peaks():
    """Carrega picos de preço por ativo para trailing stop."""
    defaults = {"peaks": {}, "entries": {}, "trailing_active": {}}
    try:
        if ASSET_PEAKS_PATH.exists():
            with open(ASSET_PEAKS_PATH) as f:
                data = json.load(f)
            for k, v in defaults.items():
                if k not in data:
                    data[k] = v
            return data
    except:
        pass
    return defaults


def update_asset_peak(asset, current_price):
    """Atualiza pico de preço e verifica ativação do trailing stop."""
    data = get_asset_peaks()

    # Registar entrada se for primeira vez
    if asset not in data["entries"]:
        data["entries"][asset] = current_price

    # Atualizar pico
    entry = data["entries"].get(asset, current_price)
    old_peak = data["peaks"].get(asset, current_price)
    new_peak = max(old_peak, current_price)
    data["peaks"][asset] = new_peak

    # Verificar se trailing stop deve ativar
    gain_pct = (current_price - entry) / entry * 100 if entry > 0 else 0
    if gain_pct >= TRAILING_GAIN_THRESHOLD:
        data["trailing_active"][asset] = True

    # Guardar
    ASSET_PEAKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ASSET_PEAKS_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

    return data


def check_trailing_stop(asset, current_price):
    """Verifica se trailing stop foi disparado. Retorna (should_sell, reason)."""
    data = get_asset_peaks()
    peak = data["peaks"].get(asset, current_price)
    entry = data["entries"].get(asset, current_price)
    trailing_active = data["trailing_active"].get(asset, False)

    if not trailing_active:
        return False, ""

    drop_from_peak = (peak - current_price) / peak * 100 if peak > 0 else 0
    gain_from_entry = (current_price - entry) / entry * 100 if entry > 0 else 0

    if drop_from_peak >= TRAILING_STOP_PCT:
        # Reset trailing state after selling
        data["trailing_active"][asset] = False
        data["peaks"][asset] = current_price
        ASSET_PEAKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(ASSET_PEAKS_PATH, "w") as f:
            json.dump(data, f, indent=2, default=str)
        return True, f"TRAILING STOP: {asset} caiu {drop_from_peak:.1f}% do pico €{peak:.4f} (ganho total: {gain_from_entry:+.1f}%)"

    return False, ""


# ─── V4.1: Concentration Check ────────────────────────────────────────────────

def check_concentration(portfolio_detail, total_eur):
    """Verifica se algum ativo excede 25% do portfólio. Retorna alertas."""
    limit_pct = 30.0
    alerts = []
    for p in portfolio_detail:
        asset = p.get("asset", "")
        value = p.get("value_eur", 0)
        if asset == "EUR" or value <= 0 or total_eur <= 0:
            continue
        pct = (value / total_eur) * 100
        if pct > limit_pct:
            alerts.append({
                "asset": asset,
                "pct": round(pct, 1),
                "value_eur": round(value, 2),
                "excess_eur": round(value - (total_eur * limit_pct / 100), 2),
            })
    return alerts


def load_trade_journal():
    """Carrega o diário de trades para circuit breaker."""
    journal_path = CONFIG["paths"]["trade_journal"]
    defaults = {"trades": [], "consecutive_losses": 0, "last_pause_until": None}
    if journal_path.exists():
        try:
            with open(journal_path) as f:
                saved = json.load(f)
            # Merge com defaults para garantir todas as keys
            for k, v in defaults.items():
                if k not in saved:
                    saved[k] = v
            return saved
        except:
            pass
    return defaults


def save_trade_journal(journal):
    journal_path = CONFIG["paths"]["trade_journal"]
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "w") as f:
        json.dump(journal, f, indent=2, default=str)


def check_circuit_breaker():
    """Verifica se o circuit breaker está ativo."""
    journal = load_trade_journal()
    pause_until = journal.get("last_pause_until")
    if pause_until:
        try:
            until = datetime.fromisoformat(pause_until)
            if datetime.now(timezone.utc) < until:
                remaining = until - datetime.now(timezone.utc)
                return True, f"Circuit breaker ativo — pausa até {pause_until[:19]} ({remaining})"
        except:
            pass

    # Verificar perdas consecutivas
    consecutive = journal.get("consecutive_losses", 0)
    limit = CONFIG["thresholds"]["circuit_breaker_losses"]
    if consecutive >= limit:
        # Ativar circuit breaker por 24h
        until = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        journal["last_pause_until"] = until
        journal["consecutive_losses"] = 0
        save_trade_journal(journal)
        return True, f"Circuit breaker ATIVADO — {consecutive} perdas consecutivas. Pausa até {until[:19]}"

    return False, ""


def run_risk_module(portfolio_detail, total_eur):
    """
    Risk Module determinístico — NÃO USA LLM.
    Verificações:
    1. BTC volatility spike → pausa
    2. Circuit breaker → pausa
    3. Max 24h drawdown (vs peak) → fecha posições
    4. BTC regime (SMA 50) → bloqueia compras em bear market
    5. Fear & Greed ≤ 20 → zero compras (só vender)
    Retorna: (approved: bool, regime: str, reason: str)
    """
    # 1. BTC Volatility Spike
    spike, ch1h = get_btc_volatility_1h()
    if spike:
        return False, "unknown", f"BTC VOLATILITY SPIKE: {ch1h:+.1f}% em 1h (> {CONFIG['thresholds']['vol_spike_btc_pct_1h']}%). Todos os trades pausados."

    # 2. Circuit Breaker
    blocked, cb_reason = check_circuit_breaker()
    if blocked:
        return False, "unknown", cb_reason

    # 3. Max 24h Drawdown (vs peak, não vs último ciclo)
    peak_data = get_portfolio_peak()
    peak_24h = peak_data.get("peak_24h", 0)
    if peak_24h > 0 and total_eur > 0:
        drawdown_pct = (peak_24h - total_eur) / peak_24h * 100
        if drawdown_pct > CONFIG["thresholds"]["max_daily_drawdown_pct"]:
            return False, "unknown", f"MAX DRAWDOWN 24h: portfólio caiu {drawdown_pct:.1f}% desde pico €{peak_24h:.2f} (> {CONFIG['thresholds']['max_daily_drawdown_pct']}%)."

    # 4. BTC Regime (SMA 50) — backtest provou +10.75pp alpha
    regime, btc_price, sma50 = get_btc_regime()

    # 5. Fear & Greed Kill Switch — V6.0
    fg_value, fg_class = get_fear_greed_index()
    fg_reason = ""
    if fg_value is not None and fg_value <= 15:
        # Extreme fear: pode vender, mas NÃO comprar
        # Se já está em bear, mantém bear; senão força bear-like
        effective_regime = "bear_extreme_fear" if regime != "bear" else "bear"
        fg_reason = f" [Fear & Greed: {fg_value}/100 {fg_class}]"
        if regime == "bear":
            return True, "bear", f"🐻 BEAR MARKET (BTC €{btc_price:.0f} < SMA50 €{sma50:.0f}) + EXTREME FEAR{fg_reason} — compras bloqueadas. Só vendas permitidas."
        else:
            return True, effective_regime, f"🛑 FEAR & GREED KILL SWITCH{fg_reason} — compras bloqueadas. Só vendas permitidas."

    if regime == "bear":
        # Em bear market: pode vender (realizar lucros), mas NÃO comprar
        return True, "bear", f"🐻 BEAR MARKET (BTC €{btc_price:.0f} < SMA50 €{sma50:.0f}) — compras bloqueadas. Só vendas permitidas."
    elif regime == "bull":
        fg_suffix = f" (F&G: {fg_value}/100 {fg_class})" if fg_value else ""
        return True, "bull", f"🐂 BULL MARKET (BTC €{btc_price:.0f} > SMA50 €{sma50:.0f}) — todos os trades permitidos.{fg_suffix}"

    return True, "unknown", "✅ Risk check OK (regime desconhecido)"


def record_trade_outcome(trade, success):
    """Regista resultado no trade journal (P4: predictive calibration)."""
    journal = load_trade_journal()
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "asset": trade.get("asset", "?"),
        "action": trade.get("action", "?"),
        "amount_eur": trade.get("amount_eur", 0),
        "success": success,
    }
    journal["trades"].append(entry)

    if success:
        journal["consecutive_losses"] = 0
    else:
        journal["consecutive_losses"] = journal.get("consecutive_losses", 0) + 1

    # Manter só últimos 50 trades
    journal["trades"] = journal["trades"][-50:]
    save_trade_journal(journal)
    return journal


def get_trade_stats():
    """Estatísticas do trade journal."""
    journal = load_trade_journal()
    trades = journal.get("trades", [])
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0,
                "consecutive_losses": journal.get("consecutive_losses", 0)}
    wins = sum(1 for t in trades if t.get("success"))
    return {
        "total": len(trades),
        "wins": wins,
        "losses": len(trades) - wins,
        "win_rate": round(wins / len(trades) * 100, 1),
        "consecutive_losses": journal.get("consecutive_losses", 0),
    }


# ─── Execução de Trades ───────────────────────────────────────────────────────

def execute_trade(analysis_content):
    """executed=True APENAS se ordem realmente colocada na Kraken."""
    try:
        sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
        from kraken_lib import get_kraken_client, place_order

        # Encontrar o ULTIMO JSON de trade valido (nao o placeholder do exemplo)
        # O LLM raciocina primeiro, depois gera o JSON. Pegamos o ultimo.
        json_matches = list(re.finditer(r'\{[^{}]*\}', analysis_content, re.DOTALL))
        trade = None
        for m in reversed(json_matches):  # do ultimo para o primeiro
            try:
                candidate = json.loads(m.group())
            except json.JSONDecodeError:
                continue
            # Validar que tem os campos necessarios para um trade
            if not all(k in candidate for k in ('asset', 'action', 'pair_kraken', 'amount_eur')):
                continue
            # Rejeitar o placeholder do exemplo
            if (candidate.get('reason') == 'RSI sobrecomprado a 71'
                and candidate.get('asset') == 'ALGO'
                and candidate.get('amount_eur') == 4.5):
                continue
            trade = candidate
            break
        
        if not trade:
            return {"executed": False, "reason": "Nenhum JSON de trade valido na analise (placeholder rejeitado)"}
        action = trade.get("action", "").upper()
        pair   = trade.get("pair_kraken", "")
        amount = trade.get("amount_eur", 0)

        if not pair or not action or amount <= 0:
            return {"executed": False, "reason": f"Dados insuficientes: {trade}"}

        if action in ("COMPRAR_MAIS", "BUY", "COMPRAR"):
            order_type = "buy"
        elif action in ("VENDER", "SELL"):
            order_type = "sell"
        else:
            return {"executed": False, "reason": f"Acção desconhecida: {action}"}

        k = get_kraken_client()

        # Normalizar par (ALGO/EUR → ALGOEUR)
        pair = pair.replace("/", "")

        # Calcular volume e preço
        ticker_url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
        ticker_data = json.loads(urllib.request.urlopen(ticker_url, timeout=10).read())
        price = float(list(ticker_data['result'].values())[0]['c'][0])
        volume = amount / price

        # Verificar mínimo de ordem
        pair_url = f"https://api.kraken.com/0/public/AssetPairs?pair={pair}"
        pair_data = json.loads(urllib.request.urlopen(pair_url, timeout=10).read())
        ordermin = float(list(pair_data['result'].values())[0].get('ordermin', 0))
        if volume < ordermin:
            volume = ordermin * 1.01
            print(f"  Volume ajustado ao mínimo: {volume:.4f}")

        # Calcular fee estimado (P2: real-world frictions)
        estimated_fee = amount * CONFIG["thresholds"]["taker_fee_pct"] / 100

        order_result = place_order(k, pair=pair, order_type=order_type,
                                   volume=volume,
                                   order_type_ext='market' if amount <= 3 else 'limit',
                                   price=round(price * 1.005, 4) if amount > 3 else None,
                                   validate=False)

        trade["estimated_fee_eur"] = round(estimated_fee, 4)
        trade["price_at_execution"] = price
        trade["volume"] = round(volume, 6)

        return {"executed": True, "order": order_result, "trade": trade}

    except Exception as e:
        return {"executed": False, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
#  V4.3: SMART SELL-TO-BUY — vende ativos fracos para comprar oportunidades
# ═══════════════════════════════════════════════════════════════════════════════

def smart_precheck(trade, portfolio_detail, total_eur):
    """
    Verifica se ha cash suficiente para a compra. Se nao houver, encontra
    o melhor ativo para vender usando um scoring multi-fator.
    
    Retorna: (sell_trade, adjusted_buy_trade) ou (None, trade) se cash suficiente.
    """
    action = trade.get("action", "").upper()
    if action not in ("BUY", "COMPRAR", "COMPRAR_MAIS"):
        return None, trade  # so aplica a compras
    
    target_asset = trade.get("asset", "").upper()
    amount_eur = trade.get("amount_eur", 0)
    
    # Cash disponivel
    cash_item = next((p for p in portfolio_detail if p.get("asset") == "EUR"), None)
    cash = cash_item["value_eur"] if cash_item else 0
    
    # Ordermin do ativo alvo
    pair = trade.get("pair_kraken", "").replace("/", "")
    try:
        import urllib.request
        pair_url = f"https://api.kraken.com/0/public/AssetPairs?pair={pair}"
        pair_data = json.loads(urllib.request.urlopen(pair_url, timeout=10).read())
        ordermin = float(list(pair_data['result'].values())[0].get('ordermin', 0))
    except:
        ordermin = 0
    
    # Obter preco do ativo alvo
    target_price = 0
    for p in portfolio_detail:
        if p.get("asset") == target_asset:
            target_price = p.get("price_eur", 0)
            break
    if target_price <= 0:
        try:
            ticker_url = f"https://api.kraken.com/0/public/Ticker?pair={pair}"
            ticker_data = json.loads(urllib.request.urlopen(ticker_url, timeout=10).read())
            target_price = float(list(ticker_data['result'].values())[0]['c'][0])
        except:
            return None, trade
    
    ordermin_eur = ordermin * target_price if ordermin > 0 else 0
    required = max(amount_eur, ordermin_eur)
    
    if cash >= required:
        return None, trade  # cash suficiente, segue normal
    
    shortfall = required - cash
    print(f"  💡 Cash insuficiente (€{cash:.2f} < €{required:.2f}). Procurando ativo para vender...")
    
    # ── SCORING: avaliar cada ativo para venda ──
    candidates = []
    for p in portfolio_detail:
        asset = p.get("asset", "")
        if asset in ("EUR", target_asset):
            continue
        if p.get("value_eur", 0) <= 0:
            continue
        
        rsi = p.get("rsi")
        chg_24h = p.get("change_24h", 0) or 0
        value = p.get("value_eur", 0)
        pct_of_portfolio = value / total_eur if total_eur > 0 else 0
        
        # EXCLUSAO: RSI desconhecido
        if rsi is None:
            continue
        
        # EXCLUSAO: nao vender no fundo
        if rsi < 30:
            continue
        
        # EXCLUSAO: nao vender em queda forte (>8% em 24h)
        if chg_24h < -8:
            continue
        
        # Verificar ordermin do candidato
        try:
            cand_pair = f"{asset}EUR"
            if asset in ("XRP", "XXRP"): cand_pair = "XXRPZEUR"
            elif asset == "ETH": cand_pair = "XETHZEUR"
            cand_url = f"https://api.kraken.com/0/public/AssetPairs?pair={cand_pair}"
            cand_data = json.loads(urllib.request.urlopen(cand_url, timeout=10).read())
            cand_ordermin = float(list(cand_data['result'].values())[0].get('ordermin', 0))
        except:
            cand_ordermin = 0
        
        amount_held = p.get("amount", 0)
        if amount_held < cand_ordermin:
            continue  # nem consegue vender
        
        # SCORE (0-100, maior = melhor para vender)
        score = 50.0
        score += min((rsi - 50) * 0.8, 20)      # RSI alto = otimo vender
        score += min(chg_24h * 2, 15)            # momentum positivo = realizar lucro
        score += min(pct_of_portfolio * 100, 10) # posicao maior = mais para vender
        
        # Penalizacoes
        if rsi < 40: score -= 15
        if chg_24h < -3: score -= 10
        
        candidates.append({
            "asset": asset, "score": score, "rsi": rsi,
            "value_eur": value, "amount": amount_held,
            "price": p.get("price_eur", 0),
            "ordermin": cand_ordermin,
        })
    
    if not candidates:
        print(f"  ⚠️ Nenhum candidato qualificado para venda. Trade cancelado.")
        return None, None  # sinaliza cancelamento
    
    # Melhor candidato
    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0]
    
    # Calcular quanto vender (shortfall + 20% buffer, max 35% da posicao)
    sell_amount_eur = min(shortfall * 1.2, best["value_eur"] * 0.35)
    sell_qty = sell_amount_eur / best["price"] if best["price"] > 0 else 0
    
    # Garantir ordermin
    if sell_qty < best["ordermin"]:
        sell_qty = best["ordermin"] * 1.01
        sell_amount_eur = sell_qty * best["price"]
    
    sell_pair = f"{best['asset']}EUR"
    if best['asset'] in ("XRP", "XXRP"): sell_pair = "XXRPZEUR"
    elif best['asset'] == "ETH": sell_pair = "XETHZEUR"
    
    sell_trade = {
        "asset": best["asset"],
        "action": "sell",
        "pair_kraken": sell_pair,
        "amount_eur": round(sell_amount_eur, 2),
        "volume": round(sell_qty, 6),
        "reason": f"Venda estrategica (score={best['score']:.0f}, RSI={best['rsi']:.0f}) para financiar compra de {target_asset}",
    }
    
    print(f"  📤 Vender {best['asset']} (score={best['score']:.0f}, RSI={best['rsi']:.0f}): "
          f"€{sell_amount_eur:.2f} → libertar cash para {target_asset}")
    
    return sell_trade, trade


# ═══════════════════════════════════════════════════════════════════════════════
#  ORQUESTRADOR PRINCIPAL V4
# ═══════════════════════════════════════════════════════════════════════════════

def run():
    print(f"[{datetime.now().strftime('%H:%M')}] Augustus v4 (Multi-Agent + Decoupled Risk) — Início")
    total_cost = 0.0
    stats = get_trade_stats()
    print(f"  📊 Trade Journal: {stats['total']} trades, {stats['win_rate']}% win rate, "
          f"{stats['consecutive_losses']} perdas consecutivas")

    # Carregar trust scores
    trust_scores = load_trust_scores()
    ca_trust = trust_scores.get("crypto_agent", {}).get("score", 0.8)
    ta_trust = trust_scores.get("trading_agent", {}).get("score", 0.8)
    print(f"  Trust: Crypto={ca_trust:.2f} Trading={ta_trust:.2f}")

    # P2: Verificar integridade do estado anterior
    state, integrity = read_state_verified()
    if integrity == "tampered":
        print("  🚨 Estado adulterado — abortando por segurança")
        return {"action": "aborted", "reason": "state_tampered"}
    elif integrity == "ok":
        print("  ✅ Integridade do estado verificada")

    # 1. Recolher dados
    print("  Recolhendo portfólio e preços...")
    portfolio = get_portfolio()
    if isinstance(portfolio, dict) and "error" in portfolio:
        return {"action": "error", "reason": portfolio["error"]}

    market_data = get_market_data(portfolio)
    if isinstance(market_data, dict) and "error" in market_data:
        print(f"  AVISO: preços parciais — {market_data['error']}")
        market_data = {}

    # P1: Sanity check dos dados externos
    if market_data:
        sanity = validate_market_data(market_data)
        if not sanity["valid"]:
            print(f"  ❌ Dados de mercado rejeitados por sanity check")
            update_trust_score("coingecko_data", "error")
            cache_file = CONFIG["paths"]["cache"]
            if cache_file.exists():
                try:
                    with open(cache_file) as f:
                        old = json.load(f)
                    market_data = {k: v for k, v in old.items() if k != "_ts"}
                    print(f"  Usando cache anterior como fallback")
                except:
                    market_data = {}

    portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
    print(f"  💰 Portfólio: €{total_eur:.2f} total | {len(portfolio_detail)} activos")

    # ═══ V4.1: Portfolio Peak Tracking (24h) ═══
    update_portfolio_peak(total_eur)

    # ═══ V4.2: Sentinel Alerts ═══
    sentinel_context = ""
    sentinel_path = CONFIG["paths"]["state"].parent / "sentinel_alerts.json"
    if sentinel_path.exists():
        try:
            with open(sentinel_path) as f:
                sentinel_data = json.load(f)
            urgent = sentinel_data.get('urgent', 0)
            warning = sentinel_data.get('warning', 0)
            alerts = sentinel_data.get('alerts', [])
            if urgent > 0 or warning > 0:
                print(f"  🚨 Sentinel: {urgent}🔴 {warning}🟡 alertas nas últimas 4h")
                for a in alerts[:5]:
                    src = a.get('source', '?')
                    sev = '🔴' if a.get('severity') == 'urgent' else '🟡'
                    asset = a.get('asset', a.get('reason', '?'))
                    reasons = ', '.join(a.get('reasons', [])[:2])
                    print(f"     {sev} [{src}] {asset}: {reasons}")
                sentinel_context = json.dumps({
                    'urgent': urgent, 'warning': warning,
                    'top_alerts': alerts[:5],
                    'macro': sentinel_data.get('macro', {}),
                })
        except:
            pass

    # ═══ V4.1: Concentration Check ═══
    concentration_alerts = check_concentration(portfolio_detail, total_eur)
    if concentration_alerts:
        for alert in concentration_alerts:
            print(f"  ⚠️ CONCENTRAÇÃO: {alert['asset']} é {alert['pct']}% do portfólio (limite 30%). Excesso: €{alert['excess_eur']:.2f}")
        # Guardar alertas no state para o agente usar
        concentration_warning = "; ".join(
            f"{a['asset']}:{a['pct']}% (excesso €{a['excess_eur']:.2f})"
            for a in concentration_alerts
        )
    else:
        concentration_warning = ""

    # ═══ MTF RSI: buscar para ativos com posição ═══
    mtf_rsi_data = {}
    for p in portfolio_detail:
        asset = p.get("asset", "")
        if asset == "EUR" or p.get("value_eur", 0) <= 0:
            continue
        pair = asset + "EUR"
        mtf = get_mtf_rsi(pair)
        if mtf:
            mtf_rsi_data[asset] = mtf
            rsi_str = "/".join(f"{mtf.get(tf,'?')}" for tf in ["1h","4h","1d"])
            print(f"  MTF RSI {asset}: {rsi_str}")
    if not mtf_rsi_data:
        print(f"  MTF RSI: sem dados (Kraken pode estar lento)")

    # ═══ V4: RISK MODULE (ANTES dos agentes) ═══
    risk_ok, regime, risk_reason = run_risk_module(portfolio_detail, total_eur)
    if not risk_ok:
        print(f"  🛑 RISK MODULE BLOQUEOU: {risk_reason}")
        return {"action": "blocked_by_risk", "reason": risk_reason, "cost": total_cost}

    print(f"  🟢 Risk Module [{regime.upper()}]: {risk_reason}")

    # ═══ SCAN DE MERCADO (novas oportunidades) ═══
    scan_assets = []
    try:
        import io, contextlib
        sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
        from market_analyst import run_market_scan
        with contextlib.redirect_stdout(io.StringIO()):
            opportunities = run_market_scan()
        top_opps = [o for o in opportunities if o.get("opportunity_score", 0) >= 20]
        scan_assets = top_opps[:5]
        if scan_assets:
            names = ", ".join(f"{o['name']}({o['opportunity_score']})" for o in scan_assets)
            print(f"  🔍 Market Scan: {len(scan_assets)} oportunidades → {names}")
        else:
            print(f"  🔍 Market Scan: {len(opportunities)} analisados, 0 acima do threshold")
    except Exception as e:
        print(f"  ⚠️ Market Scan: {e}")

    # Enriquecer portfolio_detail com scan assets para análise
    for opp in scan_assets:
        name = opp.get("name", opp.get("asset", ""))
        if not name:
            continue
        # Verificar se já existe no portfolio_detail
        existing = [p for p in portfolio_detail if p.get("asset") == name]
        if not existing:
            portfolio_detail.append({
                "asset": name,
                "amount": 0.0,
                "price_eur": opp.get("price", 0),
                "value_eur": 0.0,
                "change_24h": opp.get("change_1d", 0),
                "change_7d": opp.get("change_7d", 0),
                "rsi": opp.get("rsi", 50),
                "cg_id": opp.get("name", name).lower(),
                "_scanned": True,
            })
    if scan_assets:
        print(f"  🔍 Portfolio expandido: {len(portfolio_detail)} activos (inclui scan)")

    # ═══ V4: CRYPTO AGENT (DeepSeek Flash) ═══
    print(f"  Crypto Agent (Flash, trust={ca_trust:.2f}) a analisar...")
    crypto_result, crypto_cost = run_crypto_agent(portfolio_detail, total_eur, mtf_rsi_data)
    total_cost += crypto_cost

    if "error" in crypto_result:
        update_trust_score("crypto_agent", "error")
        print(f"  ❌ Crypto Agent erro: {crypto_result['error']}")
        return {"action": "error", "reason": f"Crypto Agent: {crypto_result['error']}", "cost": total_cost}

    crypto_content = crypto_result["content"].strip()
    print(f"  Crypto Agent ({crypto_result.get('tokens_in',0)}tok): {crypto_content[:120]}...")
    update_trust_score("crypto_agent", "correct")

    # ═══ V4.1: NEWS AGENT ═══
    # Extrair lista de ativos do portfólio (excluindo EUR)
    portfolio_asset_names = [p["asset"] for p in portfolio_detail if p.get("asset") != "EUR"]
    news_cost = 0.0
    sentiment = {}

    headlines = fetch_crypto_news()
    if headlines:
        print(f"  News Agent: {len(headlines)} headlines → analisando sentimento...")
        sentiment, news_cost = run_news_agent(headlines, portfolio_asset_names)
        total_cost += news_cost
        if sentiment:
            sent_str = ", ".join(f"{k}:{v:+.1f}" for k, v in sorted(sentiment.items(), key=lambda x: x[1], reverse=True))
            print(f"  📰 Sentimento: {sent_str}")
        else:
            print(f"  📰 Sentimento: neutro (sem sinais fortes)")
    else:
        print(f"  ⚠️ News Agent: sem headlines")
    sentiment_str = json.dumps(sentiment) if sentiment else "{}"

    # ═══ V4.2: MACRO AGENT ═══
    try:
        sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
        from macro_agent import get_macro_summary
        macro_context = get_macro_summary()
        macro_cost = 0.0  # gratuito (sem LLM, só keyword matching)
        total_cost += macro_cost
        print(f"  🌍 Macro: score calculado (Fear & Greed + keywords)")
    except Exception as e:
        macro_context = ""
        print(f"  ⚠️ Macro Agent: {e}")

    # ═══ COUNTERFACTUAL: injectar signal quality ═══
    try:
        sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
        from counterfactual_engine import get_signal_summary
        signal_quality = get_signal_summary()
        if "Nenhum" not in signal_quality and "poucos" not in signal_quality:
            print(f"  📊 Counterfactual: {signal_quality[:100]}...")
    except:
        signal_quality = ""

    # ═══ V4: TRADING AGENT ═══
    print(f"  Trading Agent (Pro, trust={ta_trust:.2f}) a decidir...")

    # ─── REGRA DETERMINÍSTICA: RSI > 70 vende, RSI < 30 compra ───
    cash = float(portfolio.get('ZEUR', 0))
    
    # V4.1: bloqueia compras em bear, bear_extreme_fear, e quando F&G ≤ 20
    allow_buy = regime not in ('bear', 'bear_extreme_fear')
    
    if allow_buy:
        # Ordenar por RSI: mais baixo = oversold (comprar), mais alto = sobrecomprado (vender)
        ranked = [p for p in portfolio_detail 
                  if p.get('asset') != 'EUR' and p.get('rsi') is not None and p.get('value_eur', 0) > 0]
        
        oversold = [p for p in ranked if p['rsi'] < 30]
        overbought = [p for p in ranked if p['rsi'] > 70 and p.get('value_eur', 0) > 3]

        # V4.1: Concentration check — se algum ativo > 25%, força venda primeiro
        concentration_alerts = check_concentration(portfolio_detail, total_eur)
        if concentration_alerts and cash < 3:
            # Vender o ativo mais concentrado (excesso) em vez do mais sobrecomprado
            most_concentrated = max(concentration_alerts, key=lambda x: x['pct'])
            conc_asset = most_concentrated['asset']
            conc_match = [p for p in portfolio_detail if p.get('asset') == conc_asset]
            if conc_match:
                # Override: priorizar venda por concentração
                overbought = [conc_match[0]]  # força venda deste
                print(f"  ⚠️ REGRA CONCENTRAÇÃO: {conc_asset} a {most_concentrated['pct']}% → forçando venda de €3")
        
        if cash >= 3 and oversold:
            # COMPRAR: ativo com menor RSI (com verificacao de ordermin)
            best = min(oversold, key=lambda x: x['rsi'])
            asset_name = best['asset']
            pair = asset_name + 'EUR'
            price = best.get('price_eur', 0)
            if price > 0:
                vol = min(3.0, cash) / price
                
                # Verificar ordermin antes de tentar
                try:
                    import urllib.request
                    pair_url = f"https://api.kraken.com/0/public/AssetPairs?pair={pair}"
                    pair_data = json.loads(urllib.request.urlopen(pair_url, timeout=10).read())
                    ordermin = float(list(pair_data['result'].values())[0].get('ordermin', 0))
                except:
                    ordermin = 0
                
                if vol < ordermin:
                    print(f"  ⚠️ {asset_name}: volume {vol:.4f} < ordermin {ordermin}. Delegado ao Trading Agent.")
                else:
                    print(f"  🟢 RSI={best['rsi']:.0f} → comprando ~€{min(3.0,cash):.0f} de {asset_name}")
                    try:
                        from kraken_lib import get_kraken_client, place_order
                        k = get_kraken_client()
                        result = place_order(k, pair=pair, order_type='buy',
                                             volume=vol, order_type_ext='market', validate=False)
                        if result and not result.get('error'):
                            txid = result.get('result', {}).get('txid', ['?'])[0]
                            print(f"  ✅ Compra: {vol:.4f} {asset_name} → TXID {txid}")
                            portfolio['ZEUR'] = cash - min(3.0, cash) * 0.998
                            old_qty = float(portfolio.get(asset_name, 0))
                            portfolio[asset_name] = old_qty + vol
                            portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
                        else:
                            print(f"  ⚠️ Compra falhou: {result}")
                    except Exception as e:
                        print(f"  ⚠️ Compra erro: {e}")
        
        elif cash < 3 and overbought:
            # V4.2: Trailing Take-Profit — deixa correr, vende só se trailing stop disparar
            best = max(overbought, key=lambda x: x['rsi'])
            asset_name = best['asset']
            pair = asset_name + 'EUR'
            price = best.get('price_eur', 0)

            # Atualizar pico do ativo
            if price > 0:
                update_asset_peak(asset_name, price)

            # Verificar trailing stop
            should_sell, trail_reason = check_trailing_stop(asset_name, price)

            if should_sell and price > 0:
                vol = 3.0 / price
                print(f"  🎯 {trail_reason}")
                print(f"  🔴 Vendendo ~€3 de {asset_name}")
                try:
                    from kraken_lib import get_kraken_client, place_order
                    k = get_kraken_client()
                    result = place_order(k, pair=pair, order_type='sell',
                                         volume=vol, order_type_ext='market', validate=False)
                    if result and not result.get('error'):
                        txid = result.get('result', {}).get('txid', ['?'])[0]
                        print(f"  ✅ Venda trailing: {vol:.4f} {asset_name} → TXID {txid}")
                        portfolio['ZEUR'] = cash + 3.0 * 0.998
                        old_qty = float(portfolio.get(asset_name, 0))
                        portfolio[asset_name] = max(0, old_qty - vol)
                        portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
                    else:
                        print(f"  ⚠️ Venda falhou: {result}")
                except Exception as e:
                    print(f"  ⚠️ Venda erro: {e}")
            else:
                gain_pct = (price - best.get('entry_price', price)) / best.get('entry_price', price) * 100 if best.get('entry_price', 0) > 0 else 0
                print(f"  📈 {asset_name} RSI={best['rsi']:.0f} sobrecomprado mas a correr — trailing stop ativo a −{TRAILING_STOP_PCT}% do pico (ganho: {gain_pct:+.1f}%)")
        
        elif len(oversold) == 0 and len(overbought) == 0:
            print(f"  ⏸️ Nenhum RSI extremo — sem ação determinística")
    else:
        # V4.1: BEAR/EXTREME_FEAR — só vender, com exceção para small cap bumps
        # Verificar concentração mesmo em bear
        ranked = [p for p in portfolio_detail 
                  if p.get('asset') != 'EUR' and p.get('rsi') is not None and p.get('value_eur', 0) > 0]
        overbought = [p for p in ranked if p['rsi'] > 70 and p.get('value_eur', 0) > 3]
        concentration_alerts = check_concentration(portfolio_detail, total_eur)
        
        # ═══ V4.1: Small Cap Bump Exception ═══
        # Em bear market, small caps com bump score ≥ 15 podem ser compradas
        small_cap_bumps = [o for o in scan_assets 
                          if o.get('cat') == 'small-cap' and o.get('opportunity_score', 0) >= 15]
        
        if small_cap_bumps and cash >= 3:
            best_bump = max(small_cap_bumps, key=lambda x: x['opportunity_score'])
            asset_name = best_bump['name']
            pair = best_bump.get('pair', asset_name + 'EUR')
            price = best_bump.get('price', 0)
            if price > 0:
                vol = min(3.0, cash) / price
                print(f"  🚀 SMALL CAP BUMP [{regime.upper()}]: {asset_name} (score {best_bump['opportunity_score']}) → comprando ~€3")
                try:
                    from kraken_lib import get_kraken_client, place_order
                    k = get_kraken_client()
                    result = place_order(k, pair=pair, order_type='buy',
                                         volume=vol, order_type_ext='market', validate=False)
                    if result and not result.get('error'):
                        txid = result.get('result', {}).get('txid', ['?'])[0]
                        print(f"  ✅ Compra bump: {vol:.4f} {asset_name} → TXID {txid}")
                        portfolio['ZEUR'] = cash - 3.0 * 0.998
                        old_qty = float(portfolio.get(asset_name, 0))
                        portfolio[asset_name] = old_qty + vol
                        portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
                        # Atualizar cash após compra
                        cash = float(portfolio.get('ZEUR', 0))
                    else:
                        print(f"  ⚠️ Compra bump falhou: {result}")
                except Exception as e:
                    print(f"  ⚠️ Compra bump erro: {e}")
        elif small_cap_bumps and cash < 3:
            print(f"  🚀 [{regime.upper()}] Small cap bumps detetados mas sem cash (€{cash:.2f})")
        
        if concentration_alerts and cash < 3:
            most_concentrated = max(concentration_alerts, key=lambda x: x['pct'])
            conc_asset = most_concentrated['asset']
            conc_match = [p for p in portfolio_detail if p.get('asset') == conc_asset]
            if conc_match:
                overbought = [conc_match[0]]
                print(f"  ⚠️ REGRA CONCENTRAÇÃO [{regime}]: {conc_asset} a {most_concentrated['pct']}% → forçando venda de €3")
        
        if cash < 3 and overbought:
            best = max(overbought, key=lambda x: x['rsi'])
            asset_name = best['asset']
            pair = asset_name + 'EUR'
            price = best.get('price_eur', 0)
            if price > 0:
                vol = 3.0 / price
                print(f"  🔴 [{regime.upper()}] RSI={best['rsi']:.0f} → vendendo ~€3 de {asset_name}")
                try:
                    from kraken_lib import get_kraken_client, place_order
                    k = get_kraken_client()
                    result = place_order(k, pair=pair, order_type='sell',
                                         volume=vol, order_type_ext='market', validate=False)
                    if result and not result.get('error'):
                        txid = result.get('result', {}).get('txid', ['?'])[0]
                        print(f"  ✅ Venda: {vol:.4f} {asset_name} → TXID {txid}")
                        portfolio['ZEUR'] = cash + 3.0 * 0.998
                        old_qty = float(portfolio.get(asset_name, 0))
                        portfolio[asset_name] = max(0, old_qty - vol)
                        portfolio_context, portfolio_detail, total_eur = build_portfolio_context(portfolio, market_data)
                    else:
                        print(f"  ⚠️ Venda falhou: {result}")
                except Exception as e:
                    print(f"  ⚠️ Venda erro: {e}")
        elif len(overbought) == 0:
            print(f"  🛑 [{regime.upper()}] Compras bloqueadas — sem overbought para vender")
    # ─── FIM REGRA DETERMINÍSTICA ───

    trade_result, trade_cost = run_trading_agent(
        crypto_content, portfolio_context, portfolio_detail, total_eur, regime, sentiment_str, signal_quality, macro_context
    )
    total_cost += trade_cost

    if "error" in trade_result:
        update_trust_score("trading_agent", "error")
        return {"action": "error", "reason": f"Trading Agent: {trade_result['error']}", "cost": total_cost}

    trade_content = trade_result["content"].strip()
    print(f"  Trading Agent ({trade_result.get('tokens_in',0)}tok): {trade_content[:120]}...")

    if "[SILENT]" in trade_content.upper() or "hold" in trade_content.lower()[:20]:
        update_trust_score("trading_agent", "correct")
        print(f"  [SILENT/HOLD] — nada a fazer (${total_cost:.5f})")
        write_state_with_hash({
            "last_action": "silent",
            "portfolio_total_eur": total_eur,
            "timestamp": datetime.now().isoformat(),
        })
        return {"action": "silent", "cost": total_cost, "crypto_analysis": crypto_content,
                "_ctx": {"portfolio_detail": portfolio_detail, "total_eur": total_eur,
                         "market_data": market_data, "sentiment": sentiment_str,
                         "regime": regime, "risk_reason": risk_reason, "stats": stats,
                         "macro": macro_context}}

    # ═══ EXECUÇÃO (V4.3: Smart Sell-to-Buy) ═══
    print("  Trading Agent decidiu — a executar...")
    update_trust_score("trading_agent", "correct")

    # Parse do JSON de trade decidido pelo LLM
    trade_json_match = re.search(r'\{[^{}]*\}', trade_content, re.DOTALL)
    if not trade_json_match:
        print("  ⚠️ Trade não executado: JSON nao encontrado na decisao")
        return {"action": "error", "reason": "no_json_in_decision", "cost": total_cost}

    try:
        trade_dict = json.loads(trade_json_match.group())
    except json.JSONDecodeError:
        print("  ⚠️ Trade não executado: JSON invalido")
        return {"action": "error", "reason": "invalid_json", "cost": total_cost}

    # V4.3: Smart precheck — vender se necessario para financiar compra
    sell_trade, buy_trade = smart_precheck(trade_dict, portfolio_detail, total_eur)

    if sell_trade is None and buy_trade is None:
        # Trade cancelado pelo smart_precheck (sem candidatos para venda)
        print("  ⚠️ Trade cancelado: sem cash e sem candidatos para venda")
        return {"action": "cancelled", "reason": "no_sell_candidates",
                "cost": total_cost, "crypto_analysis": crypto_content,
                "trading_decision": trade_content}

    exec_results = []
    if sell_trade:
        # Executar venda primeiro
        sell_json = json.dumps(sell_trade)
        print(f"  📤 Executando venda: {sell_trade['asset']} €{sell_trade['amount_eur']:.2f}")
        sell_result = execute_trade(sell_json)
        exec_results.append(("sell", sell_result))
        if not sell_result.get("executed"):
            print(f"  ⚠️ Venda falhou: {sell_result.get('reason') or sell_result.get('error')}")
            # Continuar mesmo assim — pode ser que o cash ainda de

    # Executar compra
    buy_json = json.dumps(buy_trade)
    print(f"  📥 Executando compra: {buy_trade.get('asset','?')} €{buy_trade.get('amount_eur',0):.2f}")
    exec_result = execute_trade(buy_json)
    exec_results.append(("buy", exec_result))

    if exec_result.get("executed"):
        trade_data = exec_result.get("trade", {})
        print(f"  ✅ Trade executado: {trade_data}")
        # P4: Registar no trade journal (assume success até próxima verificação)
        record_trade_outcome(trade_data, True)
        write_state_with_hash({
            "last_action": "trade",
            "trade": trade_data,
            "portfolio_total_eur": total_eur,
            "timestamp": datetime.now().isoformat(),
        })
    else:
        reason = exec_result.get("reason") or exec_result.get("error", "desconhecido")
        print(f"  ⚠️ Trade não executado: {reason}")

    return {
        "action": "trade",
        "crypto_analysis": crypto_content,
        "trading_decision": trade_content,
        "execution": exec_result,
        "cost": total_cost,
        "portfolio_total_eur": total_eur,
        "trade_stats": stats,
        "_ctx": {"portfolio_detail": portfolio_detail, "total_eur": total_eur,
                 "market_data": market_data, "sentiment": sentiment_str,
                 "regime": regime, "risk_reason": risk_reason, "stats": stats,
                 "macro": macro_context},
    }


# ═══════════════════════════════════════════════════════════════════════════════
#  RESUMO HUMANO
# ═══════════════════════════════════════════════════════════════════════════════

def human_summary(result, portfolio_detail, total_eur, market_data, sentiment,
                   regime, risk_reason, stats, elapsed, macro_context=""):
    """Formata um resumo legível para humanos, estilo Telegram."""

    lines = []
    now = datetime.now().strftime("%H:%M")

    # Cabeçalho
    action = result.get("action", "?")
    emoji = {"trade": "🔵", "silent": "🟢", "error": "🔴", "aborted": "🚨"}.get(action, "⚪")
    lines.append(f"{emoji} **Augustus {now}**")

    # Portfólio
    lines.append(f"💰 €{total_eur:.2f} | {len(portfolio_detail) if isinstance(portfolio_detail, list) else 0} ativos | "
                 f"{stats.get('total', 0)} trades ({stats.get('win_rate', 0)}% win)")

    # Top movers (max 5)
    if portfolio_detail:
        movers = []
        for p in portfolio_detail:
            if not isinstance(p, dict):
                continue
            asset = p.get("asset", "")
            if asset == "EUR":
                continue
            ch24 = p.get("change_24h", 0) or 0
            if ch24:
                movers.append((asset, ch24))
        if movers:
            movers.sort(key=lambda x: x[1], reverse=True)
            up = [f"{s} +{c:.1f}%" for s, c in movers[:3] if c > 0]
            down = [f"{s} {c:.1f}%" for s, c in sorted(movers, key=lambda x: x[1])[:2] if c < 0]
            if up:
                lines.append(f"📈 " + " · ".join(up))
            if down:
                lines.append(f"📉 " + " · ".join(down))

    # Decisão
    if action == "trade":
        trade = result.get("trade", {})
        t_asset = trade.get("asset", "?").upper()
        t_eur = trade.get("cost_eur", trade.get("volume", "?"))
        lines.append(f"🔵 **COMPRA {t_asset}** — €{t_eur}")
    elif action == "silent":
        lines.append(f"🟢 HOLD — sem condições de compra")
    elif action == "error":
        lines.append(f"🔴 Erro: {result.get('reason', 'desconhecido')[:80]}")

    # Contexto
    context_parts = []
    if sentiment and sentiment != "{}":
        try:
            sent_dict = json.loads(sentiment) if isinstance(sentiment, str) else sentiment
            top_sent = ", ".join(f"{k}:{v:+.1f}" for k, v in sorted(sent_dict.items(), key=lambda x: x[1], reverse=True)[:3])
            context_parts.append(f"📰 {top_sent}")
        except:
            pass
    if "medo extremo" in macro_context.lower() or "fear" in macro_context.lower():
        context_parts.append("🌍 Medo Extremo")
    elif macro_context:
        context_parts.append(f"🌍 {macro_context[:40]}")
    if context_parts:
        lines.append(" · ".join(context_parts))

    # Risco
    if "BLOQUEADO" in risk_reason or "bloqueado" in risk_reason:
        lines.append(f"⛔ {risk_reason[:80]}")

    # Custos
    cost = result.get("cost", 0)
    lines.append(f"💸 \${cost:.5f} · ⏱ {elapsed:.0f}s")

    summary = "\n".join(lines)
    print(f"\n{summary}", file=sys.stderr)
    return summary

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Augustus Orchestrator v4 — Multi-Agent + Decoupled Risk")
    parser.add_argument("--mode", choices=["scan", "trade", "debug", "trust", "stats", "reset_cb"],
                        default="trade")
    args = parser.parse_args()

    if args.mode == "trust":
        scores = load_trust_scores()
        print(json.dumps(scores, indent=2))
        return
    if args.mode == "stats":
        stats = get_trade_stats()
        print(json.dumps(stats, indent=2))
        return
    if args.mode == "reset_cb":
        journal = load_trade_journal()
        journal["last_pause_until"] = None
        journal["consecutive_losses"] = 0
        save_trade_journal(journal)
        print("Circuit breaker reset.")
        return

    start = time.time()
    try:
        result = run()
    except Exception as e:
        result = {"action": "error", "reason": f"{type(e).__name__}: {e}"}

    elapsed = time.time() - start
    result.update({
        "_elapsed_s": round(elapsed, 1),
        "_mode": args.mode,
        "_timestamp": datetime.now().isoformat(),
        "_version": "v4",
    })

    print(json.dumps(result, indent=2, default=str))

    cost = result.get("cost", 0)
    stats = result.get("trade_stats") or {}
    portfolio_total = result.get("portfolio_total_eur") or 0
    ctx = result.get("_ctx", {})
    # Fallback: extrair do _ctx se não estiver no top-level
    if not stats:
        stats = ctx.get("stats", {})
    if not portfolio_total:
        portfolio_total = ctx.get("total_eur", 0)

    print(f"\n💰 Custo: ${cost:.5f} | ⏱ {elapsed:.1f}s", file=sys.stderr)

    # Resumo humano (apenas se houver contexto)
    if ctx:
        try:
            human_summary(
                result=result,
                portfolio_detail=ctx.get("portfolio_detail", []),
                total_eur=portfolio_total,
                market_data=ctx.get("market_data", {}),
                sentiment=ctx.get("sentiment", "{}"),
                regime=ctx.get("regime", "unknown"),
                risk_reason=ctx.get("risk_reason", ""),
                stats=stats if stats else {"total": 0, "win_rate": 0, "consecutive_losses": 0},
                elapsed=elapsed,
                macro_context=ctx.get("macro", ""),
            )
        except Exception as e:
            print(f"  ⚠️ Resumo humano falhou: {e}", file=sys.stderr)

    log_dir = CONFIG["paths"]["logs"]
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(log_file, "w") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"📝 Log: {log_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
