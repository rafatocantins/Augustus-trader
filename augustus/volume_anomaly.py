#!/usr/bin/env python3
"""
Augustus V6 — Volume Anomaly Detector
=====================================
Baseado em: Karbalaii (2025) "Detecting Crypto Pump-and-Dump Schemes" 
           e Maqsood et al. (2026) "Real-Time Detection of Abnormal Volume Price Events"

Estratégia:
  - Detecta spikes de volume (>3x média 7d) em ativos com market cap < EUR500M
  - Classifica o contexto do spike (perto do suporte = acumulação, perto da resistência = distribuição)
  - Gera alertas de oportunidade para o Trading Agent

Output: /root/.augustus/data/volume_anomalies.json
"""

import sys, json, os, urllib.request, time
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
SCRIPTS_DIR = HOME / ".hermes/scripts"
DATA_DIR = HOME / ".hermes/data"
ANOMALIES_FILE = DATA_DIR / "volume_anomalies.json"

sys.path.insert(0, str(SCRIPTS_DIR))

# ─── Config ──────────────────────────────────────────────────────────────────
CONFIG = {
    "volume_spike_multiplier": 3.0,      # Volume > 3x média 7d = anomalia
    "min_volume_eur": 500_000,           # Volume mínimo 24h para considerar
    "max_market_cap_eur": 500_000_000,   # Foco em small/mid caps (<EUR500M)
    "min_market_cap_eur": 1_000_000,     # Excluir dust (<EUR1M)
    "near_support_pct": 5.0,             # "Perto do suporte" = <5% acima
    "near_resistance_pct": 5.0,          # "Perto da resistência" = <5% abaixo
    "rsi_overbought": 70,
    "rsi_oversold": 35,
    "top_n_to_scan": 200,                # Analisar top 200 por volume
    "min_bump_score": 6,                 # Score mínimo para gerar alerta
    "cache_minutes": 60,                 # Cache de preços
}


def fetch_market_data():
    """Busca top N moedas por volume do CoinGecko."""
    url = (
        f"https://api.coingecko.com/api/v3/coins/markets"
        f"?vs_currency=eur&order=volume_desc"
        f"&per_page={CONFIG['top_n_to_scan']}&page=1"
        f"&sparkline=false"
        f"&price_change_percentage=1h,24h,7d"
    )
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=20).read())
        return data
    except Exception as e:
        print(f"CoinGecko error: {e}", file=sys.stderr)
        return []


def get_kraken_ohlc(pair, interval=240, limit=24):
    """Busca OHLC da Kraken. interval em minutos. Retorna candles."""
    try:
        from augustus.kraken_client import get_kraken_client
        k = get_kraken_client()
        ohlc = k.query_public('OHLC', {'pair': pair, 'interval': interval})
        pair_key = list(ohlc['result'].keys())[0]
        candles = ohlc['result'][pair_key]
        return candles[-limit:]
    except:
        return []


def analyze_volume_anomaly(coin, kraken_pair=None):
    """Analisa um ativo individual para anomalias de volume."""
    mcap = coin.get('market_cap', 0) or 0
    vol_24h = coin.get('total_volume', 0) or 0
    price = coin.get('current_price', 0) or 0
    ch24 = coin.get('price_change_percentage_24h_in_currency', 0) or 0
    ch7d = coin.get('price_change_percentage_7d_in_currency', 0) or 0
    
    # Filtros básicos
    if mcap < CONFIG['min_market_cap_eur']:
        return None
    if mcap > CONFIG['max_market_cap_eur']:
        return None
    if vol_24h < CONFIG['min_volume_eur']:
        return None
    if price <= 0:
        return None
    
    # Tentar obter OHLC da Kraken para análise de volume detalhada
    if kraken_pair:
        candles = get_kraken_ohlc(kraken_pair, interval=240, limit=42)  # 7 dias em 4h
    else:
        candles = []
    
    # Volume anomaly detection
    vol_spike = False
    vol_ratio = 1.0
    
    if len(candles) >= 30:
        # Calcular volume médio 7d (42 candles de 4h = 7 dias)
        vols = [float(c[6]) for c in candles[-42:]]
        avg_vol_7d = sum(vols[:-1]) / (len(vols) - 1)  # excluir candle atual
        last_vol = vols[-1]
        if avg_vol_7d > 0:
            vol_ratio = last_vol / avg_vol_7d
            vol_spike = vol_ratio >= CONFIG['volume_spike_multiplier']
    else:
        # Fallback: usar dados CoinGecko
        # Não temos volume histórico, estimar com market cap/volume ratio
        vol_spike = False  # Não podemos confirmar sem OHLC
    
    if not vol_spike:
        return None
    
    # Suporte/Resistência (últimos 7 dias de candles)
    support = price * 0.95  # fallback
    resistance = price * 1.05  # fallback
    
    if len(candles) >= 20:
        lows = [float(c[3]) for c in candles[-20:]]
        highs = [float(c[2]) for c in candles[-20:]]
        support = min(lows)
        resistance = max(highs)
    
    support_dist = (price - support) / price * 100
    resistance_dist = (resistance - price) / price * 100
    
    near_support = support_dist < CONFIG['near_support_pct']
    near_resistance = resistance_dist < CONFIG['near_resistance_pct']
    
    # Bump scoring (0-10)
    score = 0
    
    # Volume spike magnitude
    if vol_ratio >= 5:
        score += 4
    elif vol_ratio >= 4:
        score += 3
    elif vol_ratio >= 3:
        score += 2
    
    # Contexto: acumulação ou distribuição?
    context = "neutral"
    if near_support and ch24 < 3:  # spike perto do suporte, sem pump excessivo
        score += 3
        context = "accumulation"
    elif near_support and ch24 >= 3:
        score += 2
        context = "early_breakout"
    elif near_resistance and ch24 > 5:
        score += 1
        context = "distribution"
    elif near_resistance:
        context = "resistance_test"
    
    # 7d trend bonus: melhor entrar após queda (reversal) do que após pump
    if ch7d < -5 and near_support:
        score += 2  # "blood in the streets" + volume = bottom
    elif ch7d > 20:
        score -= 1  # já subiu muito, risco de FOMO
    
    # RSI filter
    rsi = None
    if len(candles) >= 15:
        # Simplified RSI calculation from OHLC
        closes = [float(c[4]) for c in candles[-15:]]
        gains = [max(closes[i] - closes[i-1], 0) for i in range(1, len(closes))]
        losses = [max(closes[i-1] - closes[i], 0) for i in range(1, len(closes))]
        avg_gain = sum(gains) / len(gains) if gains else 0
        avg_loss = sum(losses) / len(losses) if losses else 0.0001
        rs = avg_gain / avg_loss if avg_loss > 0 else 0
        rsi = 100 - (100 / (1 + rs))
    
    if rsi is not None:
        if rsi > CONFIG['rsi_overbought']:
            score -= 2  # já está sobrecomprado
        elif rsi < CONFIG['rsi_oversold']:
            score += 1  # oversold + volume = reversal signal
    
    # Market cap category
    if mcap < 50_000_000:
        cat = "micro"
        score += 1  # micro-caps têm maior potencial de bump
    elif mcap < 200_000_000:
        cat = "small"
    else:
        cat = "mid"
    
    if score < CONFIG['min_bump_score']:
        return None
    
    # Determinar ação recomendada
    if context in ("accumulation", "early_breakout"):
        action = "buy"
    elif context == "distribution" and rsi and rsi > 65:
        action = "sell"
    else:
        action = "watch"
    
    return {
        "symbol": coin.get('symbol', '???').upper(),
        "name": coin.get('name', '???'),
        "price_eur": price,
        "market_cap_eur": mcap,
        "volume_24h_eur": vol_24h,
        "volume_ratio": round(vol_ratio, 2),
        "change_24h_pct": round(ch24, 2),
        "change_7d_pct": round(ch7d, 2),
        "support_eur": round(support, 8),
        "resistance_eur": round(resistance, 8),
        "support_dist_pct": round(support_dist, 1),
        "resistance_dist_pct": round(resistance_dist, 1),
        "rsi": round(rsi, 1) if rsi else None,
        "context": context,
        "category": cat,
        "bump_score": score,
        "action": action,
        "kraken_pair": kraken_pair,
    }


def find_kraken_pairs(coins):
    """Tenta encontrar pares Kraken para os ativos."""
    try:
        from augustus.kraken_client import get_kraken_client
        k = get_kraken_client()
        pairs = k.query_public('AssetPairs')
        available = set()
        for pair_name, info in pairs['result'].items():
            if pair_name.endswith('EUR'):
                base = info.get('base', '')
                available.add(base)
                # Também adicionar sem prefixo X
                if base.startswith('X') and len(base) > 1:
                    available.add(base[1:])
        return available
    except:
        return set()


def main():
    print(f"=== Volume Anomaly Detector === {datetime.now().strftime('%H:%M')}")
    
    # 1. Fetch market data
    coins = fetch_market_data()
    if not coins:
        print("Sem dados de mercado")
        return
    
    # 2. Find Kraken pairs
    kraken_pairs = find_kraken_pairs(coins)
    
    # 3. Analyze each coin
    anomalies = []
    for coin in coins:
        symbol = coin.get('symbol', '').upper()
        
        # Tentar encontrar par Kraken
        kraken_pair = None
        if symbol in kraken_pairs:
            kraken_pair = f"{symbol}EUR"
        elif f"X{symbol}" in kraken_pairs:
            kraken_pair = f"X{symbol}ZEUR"
        
        result = analyze_volume_anomaly(coin, kraken_pair)
        if result:
            anomalies.append(result)
    
    # 4. Sort by bump score
    anomalies.sort(key=lambda x: -x['bump_score'])
    
    # 5. Display
    print(f"\n{'Symbol':8s} {'Score':>5s} {'Vol.Ratio':>9s} {'24h':>7s} {'7d':>7s} {'Context':<18s} {'Action':>6s} {'Price':>10s} {'MCap':>12s}")
    print("-" * 95)
    
    for a in anomalies:
        print(f"{a['symbol']:8s} {a['bump_score']:>3}/10 {a['volume_ratio']:>7.1f}x {a['change_24h_pct']:+6.1f}% {a['change_7d_pct']:+6.1f}% {a['context']:<18s} {a['action']:>6s} EUR{a['price_eur']:>8.4f} EUR{a['market_cap_eur']:>10,.0f}")
    
    # 6. Save
    output = {
        "timestamp": datetime.now().isoformat(),
        "total_scanned": len(coins),
        "anomalies_found": len(anomalies),
        "top_opportunities": anomalies[:10],
        "all_anomalies": anomalies,
    }
    
    with open(ANOMALIES_FILE, 'w') as f:
        json.dump(output, f, indent=2)
    
    print(f"\n{len(anomalies)} anomalias detetadas em {len(coins)} ativos")
    print(f"Top 3 oportunidades:")
    for a in anomalies[:3]:
        print(f"  {a['symbol']} — Score {a['bump_score']}/10 — {a['context']} — {'COMPRAR' if a['action']=='buy' else 'VENDER' if a['action']=='sell' else 'OBSERVAR'}")
    
    return anomalies


if __name__ == "__main__":
    main()
