#!/usr/bin/env python3
"""
Augustus V6 — Strategy Market
==============================
Corre periodicamente e prepara estratégias de trading baseadas em:
1. Volume Anomaly Detection (Karbalaii 2025)
2. Coil/Squeeze Breakout Detection (BB-KC Squeeze, Day et al. 2023)
3. Support/Resistance + Microstructure
4. Fear & Greed regime filter (Farzulla 2026)

Output: /root/.augustus/data/strategy_market.json
Alimenta o Trading Agent com oportunidades pré-analisadas.
"""

import sys, json, os, urllib.request, time
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
SCRIPTS_DIR = HOME / ".hermes/scripts"
DATA_DIR = HOME / ".hermes/data"
STRATEGY_FILE = DATA_DIR / "strategy_market.json"
ANOMALIES_FILE = DATA_DIR / "volume_anomalies.json"

sys.path.insert(0, str(SCRIPTS_DIR))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. MACRO CONTEXT
# ═══════════════════════════════════════════════════════════════════════════════

def get_macro_context():
    """BTC regime, F&G, market structure."""
    try:
        from augustus.kraken_client import get_kraken_client
        k = get_kraken_client()
        
        # BTC
        btc = k.query_public('Ticker', {'pair': 'XXBTZEUR'})
        btc_price = float(list(btc['result'].values())[0]['c'][0])
        
        # SMA50
        ohlc = k.query_public('OHLC', {'pair': 'XXBTZEUR', 'interval': 1440})
        closes = [float(c[4]) for c in ohlc['result']['XXBTZEUR'][-60:]]
        sma50 = sum(closes[-50:]) / 50
        sma20 = sum(closes[-20:]) / 20
        
        regime = "bull" if btc_price > sma50 else "bear"
        strength = (btc_price / sma50 - 1) * 100
        
        # Volatilidade BTC (ATR 7d)
        high_low_range = [abs(float(c[2]) - float(c[3])) / float(c[4]) for c in ohlc['result']['XXBTZEUR'][-7:]]
        avg_volatility = sum(high_low_range) / len(high_low_range) * 100
        
        # Bollinger Bandwidth (squeeze no BTC?)
        bb_mid = sma20
        bb_std = (sum((c - bb_mid)**2 for c in closes[-20:]) / 20) ** 0.5
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_width = (bb_upper - bb_lower) / bb_mid * 100
        btc_squeezing = bb_width < 5  # bandwidth < 5% = squeeze
        
        # ETH/BTC ratio (alt season indicator)
        eth = k.query_public('Ticker', {'pair': 'XETHZEUR'})
        eth_price = float(list(eth['result'].values())[0]['c'][0])
        eth_btc_ratio = eth_price / btc_price
        
        return {
            "btc_eur": btc_price,
            "sma50_eur": sma50,
            "sma20_eur": sma20,
            "regime": regime,
            "regime_strength_pct": round(strength, 1),
            "btc_volatility_7d_pct": round(avg_volatility, 2),
            "bb_width_pct": round(bb_width, 1),
            "btc_squeezing": btc_squeezing,
            "eth_btc_ratio": round(eth_btc_ratio, 6),
            "eth_eur": eth_price,
        }
    except Exception as e:
        return {"error": str(e)}


def get_fear_greed():
    """F&G Index com classificação de regime (Farzulla 2026)."""
    try:
        data = json.loads(urllib.request.urlopen('https://api.alternative.me/fng/?limit=3', timeout=5).read())
        current = data['data'][0]
        fg_value = int(current['value'])
        
        # Classificação Farzulla 2026
        if fg_value < 25:
            regime = "extreme_fear"
        elif fg_value < 45:
            regime = "fear"
        elif fg_value <= 55:
            regime = "neutral"
        elif fg_value <= 75:
            regime = "greed"
        else:
            regime = "extreme_greed"
        
        return {
            "value": fg_value,
            "classification": current['value_classification'],
            "regime": regime,
            "trading_advice": (
                "avoid_trading" if fg_value < 25 or fg_value > 75
                else "cautious_buy" if fg_value < 45
                else "normal" if fg_value <= 55
                else "take_profit"
            ),
            "history": [{"value": int(d['value']), "ts": d['timestamp']} for d in data['data']],
        }
    except Exception as e:
        return {"error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. COIL / SQUEEZE DETECTOR (qualquer ativo)
# ═══════════════════════════════════════════════════════════════════════════════

def detect_coil_squeeze(asset, portfolio_qty=None):
    """
    Detecta padrões de coil/squeeze (BB-KC Squeeze strategy).
    Baseado em: Day et al. (2023), PyQuantLab (2025)
    
    Aplicável a QUALQUER ativo com dados OHLC.
    Retorna None se não detetar squeeze, ou dict com detalhes.
    """
    try:
        from augustus.kraken_client import get_kraken_client
        k = get_kraken_client()
        
        # Mapear nomes Kraken
        if asset in ('XRP',):
            pair = 'XXRPZEUR'
        elif asset in ('BTC',):
            pair = 'XXBTZEUR'
        elif asset in ('ETH',):
            pair = 'XETHZEUR'
        else:
            pair = f"{asset}EUR"
        
        ohlc = k.query_public('OHLC', {'pair': pair, 'interval': 240})
        candles = ohlc['result'][list(ohlc['result'].keys())[0]]
        
        if len(candles) < 30:
            return None
        
        closes = [float(c[4]) for c in candles[-30:]]
        highs = [float(c[2]) for c in candles[-30:]]
        lows = [float(c[3]) for c in candles[-30:]]
        vols = [float(c[6]) for c in candles[-30:]]
        
        current_price = closes[-1]
        
        # Bollinger Bands (20, 2)
        bb_period = 20
        bb_closes = closes[-bb_period:]
        bb_mid = sum(bb_closes) / bb_period
        bb_std = (sum((c - bb_mid)**2 for c in bb_closes) / bb_period) ** 0.5
        bb_upper = bb_mid + 2 * bb_std
        bb_lower = bb_mid - 2 * bb_std
        bb_width = (bb_upper - bb_lower) / bb_mid * 100
        
        # Keltner Channel (20, 1.5x ATR)
        kc_period = 20
        true_ranges = []
        for i in range(1, min(len(highs), kc_period + 1)):
            tr = max(
                highs[-i] - lows[-i],
                abs(highs[-i] - closes[-i-1]),
                abs(lows[-i] - closes[-i-1])
            )
            true_ranges.append(tr)
        atr = sum(true_ranges) / len(true_ranges) if true_ranges else 0
        kc_mid = bb_mid
        kc_upper = kc_mid + 1.5 * atr
        kc_lower = kc_mid - 1.5 * atr
        kc_width = (kc_upper - kc_lower) / kc_mid * 100
        
        # SQUEEZE: BB dentro de KC
        is_squeezing = bb_upper < kc_upper and bb_lower > kc_lower
        
        # Compressão progressiva (bandwidth a diminuir)
        if len(closes) >= 25:
            old_bb_closes = closes[-25:-5]
            old_bb_mid = sum(old_bb_closes) / len(old_bb_closes)
            old_bb_std = (sum((c - old_bb_mid)**2 for c in old_bb_closes) / len(old_bb_closes)) ** 0.5
            old_bb_width = (2 * 2 * old_bb_std) / old_bb_mid * 100
            tightening = bb_width < old_bb_width * 0.85
        else:
            tightening = False
        
        # Suporte/Resistência locais
        support = min(lows[-20:])
        resistance = max(highs[-20:])
        support_dist = (current_price - support) / current_price * 100
        resistance_dist = (resistance - current_price) / current_price * 100
        
        # Volume trend
        vol_recent = sum(vols[-5:]) / 5
        vol_older = sum(vols[-15:-5]) / 10 if len(vols) >= 15 else vol_recent
        vol_declining = vol_recent < vol_older * 0.7  # volume cai durante squeeze
        
        # Scoring
        score = 0
        if is_squeezing:
            score += 4
        if tightening:
            score += 2
        if vol_declining:
            score += 1  # volume a cair durante squeeze = confirmação
        
        # Proximidade do breakout
        if support_dist < 3:
            score += 1  # perto do suporte — pode romper para baixo
        if resistance_dist < 3:
            score += 1  # perto da resistência — pode romper para cima
        
        # Tamanho do range
        range_pct = (resistance - support) / current_price * 100
        if range_pct < 4:
            score += 1  # range muito apertado = breakout iminente
        
        if score < 4:
            return None
        
        # Prever direção (baseado em tendência de fundo)
        sma20_local = sum(closes[-20:]) / 20
        trend = "bullish" if current_price > sma20_local else "bearish"
        
        # Preço atual vs meio do range
        mid_range = (support + resistance) / 2
        position_in_range = (current_price - support) / (resistance - support) * 100 if resistance != support else 50
        
        # Breakout targets
        breakout_up_target = resistance * 1.03
        breakout_down_target = support * 0.97
        
        return {
            "asset": asset,
            "pair": pair,
            "current_price": current_price,
            "support": support,
            "resistance": resistance,
            "range_pct": round(range_pct, 1),
            "position_in_range_pct": round(position_in_range, 1),
            "is_squeezing": is_squeezing,
            "tightening": tightening,
            "vol_declining": vol_declining,
            "bb_width_pct": round(bb_width, 1),
            "trend": trend,
            "coil_score": score,
            "breakout_up_target": breakout_up_target,
            "breakout_down_target": breakout_down_target,
            "portfolio_qty": portfolio_qty,
            "action": "prepare_breakout",
            "strategy": (
                f"COIL {'SQUEEZE' if is_squeezing else 'TIGHTENING'} | "
                f"Range {range_pct:.1f}% | "
                f"Trend {trend} | "
                f"Buy stop >EUR{resistance:.4f}, Sell stop <EUR{support:.4f}"
            ),
        }
    except Exception as e:
        return {"asset": asset, "error": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# 3. OPPORTUNITY AGGREGATOR
# ═══════════════════════════════════════════════════════════════════════════════

def aggregate_opportunities():
    """Junta todas as fontes de sinal num relatório unificado."""
    
    # Macro
    macro = get_macro_context()
    fng = get_fear_greed()
    
    # Portfolio assets
    try:
        from augustus.kraken_client import get_kraken_client, get_balance
        k = get_kraken_client()
        bal = get_balance(k)
        portfolio_assets = [a for a, v in bal.items() if v > 0.001 and a != 'ZEUR']
    except:
        portfolio_assets = []
    
    # Coil/squeeze para ativos do portfolio
    coil_opportunities = []
    for asset in portfolio_assets:
        # Normalizar nome
        asset_clean = asset.replace('X', '') if asset.startswith('X') and len(asset) > 1 else asset
        qty = bal.get(asset, 0)
        result = detect_coil_squeeze(asset_clean, portfolio_qty=qty)
        if result and isinstance(result, dict) and 'coil_score' in result:
            coil_opportunities.append(result)
    coil_opportunities.sort(key=lambda x: -x['coil_score'])
    
    # Volume anomalies (do ficheiro gerado pelo detector)
    volume_opportunities = []
    if ANOMALIES_FILE.exists():
        try:
            with open(ANOMALIES_FILE) as f:
                va_data = json.load(f)
            volume_opportunities = va_data.get('top_opportunities', [])
        except:
            pass
    
    # Montar estratégias recomendadas
    strategies = []
    
    # Estratégia 1: Coil breakouts no portfolio
    for co in coil_opportunities[:5]:
        if co['coil_score'] >= 4:
            strategies.append({
                "type": "coil_breakout",
                "asset": co['asset'],
                "score": co['coil_score'],
                "priority": "high" if co['coil_score'] >= 7 else "medium",
                "details": co,
                "action_required": (
                    f"Preparar ordens condicionais: "
                    f"Buy stop >EUR{co['breakout_up_target']:.4f}, "
                    f"Sell stop <EUR{co['breakout_down_target']:.4f}"
                ),
            })
    
    # Estratégia 2: Volume anomalies
    for va in volume_opportunities[:5]:
        strategies.append({
            "type": "volume_anomaly",
            "asset": va.get('symbol', '???'),
            "score": va.get('bump_score', 0),
            "priority": "high" if va.get('bump_score', 0) >= 8 else "medium",
            "details": va,
            "action_required": (
                f"{'COMPRAR' if va.get('action') == 'buy' else 'VENDER' if va.get('action') == 'sell' else 'OBSERVAR'} "
                f"— {va.get('context', 'unknown')}"
            ),
        })
    
    # Estratégia 3: Fear-driven opportunity
    if fng.get('value', 50) < 35:
        strategies.append({
            "type": "fear_opportunity",
            "score": 7,
            "priority": "medium",
            "details": fng,
            "action_required": f"F&G={fng['value']} — zona de acumulação. Considerar DCA BTC/ETH.",
        })
    
    # Compilar relatório
    report = {
        "timestamp": datetime.now().isoformat(),
        "macro": macro,
        "fear_greed": fng,
        "portfolio_assets_scanned": len(portfolio_assets),
        "coil_opportunities": len(coil_opportunities),
        "volume_anomalies": len(volume_opportunities),
        "strategies": strategies,
        "summary": generate_summary(strategies, macro, fng),
    }
    
    with open(STRATEGY_FILE, 'w') as f:
        json.dump(report, f, indent=2)
    
    return report


def generate_summary(strategies, macro, fng):
    """Gera um resumo humano para o Rafael."""
    lines = []
    lines.append(f"BTC EUR{macro.get('btc_eur', 0):,.0f} | {macro.get('regime', '?').upper()} | F&G {fng.get('value', '?')} ({fng.get('classification', '?')})")
    
    urgent = [s for s in strategies if s['priority'] == 'high']
    medium = [s for s in strategies if s['priority'] == 'medium']
    
    if urgent:
        lines.append(f"🔴 {len(urgent)} oportunidades URGENTES")
        for s in urgent:
            lines.append(f"  {s['type']}: {s['asset']} — {s['action_required']}")
    
    if medium:
        lines.append(f"🟡 {len(medium)} oportunidades médias")
    
    if not strategies:
        lines.append("Nenhuma oportunidade detetada. Mercado lateral ou sem anomalias.")
    
    lines.append(f"F&G trading: {fng.get('trading_advice', 'normal')}")
    
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"=== Strategy Market === {datetime.now().strftime('%d %b %H:%M')}")
    print()
    
    # 1. Macro
    macro = get_macro_context()
    fng = get_fear_greed()
    print(f"BTC: EUR{macro.get('btc_eur', 0):,.0f} | SMA50: EUR{macro.get('sma50_eur', 0):,.0f} | Regime: {macro.get('regime', '?').upper()} ({macro.get('regime_strength_pct', 0):+.1f}%)")
    print(f"F&G: {fng.get('value', '?')} ({fng.get('regime', '?')}) | Advice: {fng.get('trading_advice', '?')}")
    
    if macro.get('btc_squeezing'):
        print(f"⚠️ BTC em SQUEEZE (BB width {macro.get('bb_width_pct', 0):.1f}%) — breakout iminente no mercado global!")
    
    # 2. Run volume anomaly detector
    print("\n--- Volume Anomalies ---")
    try:
        import subprocess
        subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "volume_anomaly_detector.py")],
            timeout=120, cwd=str(SCRIPTS_DIR)
        )
    except Exception as e:
        print(f"Volume detector error: {e}")
    
    # 3. Aggregate
    print("\n--- Strategy Market ---")
    report = aggregate_opportunities()
    
    print(report['summary'])
    print(f"\nRelatório guardado em: {STRATEGY_FILE}")
    
    return report


if __name__ == "__main__":
    main()
