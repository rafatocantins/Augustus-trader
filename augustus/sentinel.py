#!/usr/bin/env python3
"""
SENTINEL MONITOR v1 — Vigilância 24/7 do portfólio + mercado.
Zero tokens. Python puro. Alimenta o orchestrator Augustus V4.2.

Arquitetura:
  1. Portfolio Watch   — dips, pumps, risco nas moedas que tens
  2. Market Scanner    — novas entradas com bump potential
  3. Macro Context     — Fear & Greed, BTC regime, eventos
  4. Alert Engine      — classifica, guarda, dispara orchestrator se urgente

Output: sentinel_alerts.json
Se alerta 🔴 → dispara augustus_orchestrator_v4.py imediatamente.
"""
import sys, os, json, time, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

HOME = Path.home()
DATA_DIR = HOME / ".hermes/data"
SCRIPTS_DIR = HOME / ".hermes/scripts"
ALERTS_FILE = DATA_DIR / "sentinel_alerts.json"
SNAPSHOT_FILE = DATA_DIR / "augustus_snapshot.json"

sys.path.insert(0, str(SCRIPTS_DIR))

# Carregar env vars (Telegram, Kraken, etc.)
env_path = HOME / ".hermes" / ".env"
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

# ═══════════════════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════════════════

PAIRS_SCAN = [
    'ALGOEUR','AVAXEUR','BIOEUR','QUAIEUR','SOLEUR','SUIEUR',
    'USUALEUR','XRPEUR','ADAEUR','DOTEURO','LINKEUR','ATOMEUR',
    'PEPEEUR','DOGEEUR','TAOEUR','NEAREUR','UNIEUR','GALAEUR',
    'JASMYEUR','ARPAEUR','APEEUR','TRXEUR','RENDEREUR',
]

THRESHOLDS = {
    'pump_1h_pct': 15.0,       # 🔴 pump >15% em 1h
    'crash_1h_pct': 8.0,        # 🔴 crash >8% em 1h
    'dip_4h_pct': 5.0,          # 🟡 dip >5% em 4h
    'rsi_urgent_low': 15.0,     # 🔴 RSI < 15
    'rsi_urgent_high': 85.0,    # 🔴 RSI > 85
    'rsi_warn_low': 25.0,       # 🟡 RSI < 25
    'rsi_warn_high': 75.0,      # 🟡 RSI > 75
    'volume_spike_ratio': 3.0,   # volume >3x media
    'bump_score_urgent': 80.0,   # 🔴 oportunidade >80
    'bump_score_warn': 50.0,     # 🟡 oportunidade >50
    'fg_change_urgent': 15.0,    # 🔴 F&G muda >15 pts em 4h
}

# ═══════════════════════════════════════════════════════════════
# KRAKEN API (stdlib, sem dependencias)
# ═══════════════════════════════════════════════════════════════

def get_kraken_client():
    from kraken_lib import get_kraken_client as _get
    return _get()

def get_balance():
    try:
        k = get_kraken_client()
        return k.query_private('Balance')['result']
    except:
        return {}

def get_tickers(pairs):
    try:
        url = f"https://api.kraken.com/0/public/Ticker?pair={','.join(pairs)}"
        resp = urllib.request.urlopen(url, timeout=10)
        return json.loads(resp.read())['result']
    except:
        return {}

def get_ohlc(pair, interval=60):
    try:
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval}"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        for k, v in data['result'].items():
            if k != 'last' and isinstance(v, list):
                return v
    except:
        pass
    return []

# ═══════════════════════════════════════════════════════════════
# 1. PORTFOLIO WATCH
# ═══════════════════════════════════════════════════════════════

def portfolio_watch(balance, tickers):
    alerts = []
    total_eur = 0.0
    portfolio_items = []

    # Mapa de pares Kraken (formato X...Z...)
    PAIR_MAP = {
        'XETH': 'XETHZEUR', 'XXBT': 'XXBTZEUR', 'XXRP': 'XXRPZEUR',
        'XLTC': 'XLTCZEUR', 'XDG': 'XDGEUR',
    }

    for asset, amount_str in balance.items():
        amt = float(amount_str)
        if amt <= 0:
            continue
        if asset == 'ZEUR':
            total_eur += amt
            portfolio_items.append({'asset': 'EUR', 'value_eur': amt, 'price': 1.0})
            continue
        if asset == 'ZUSD':
            continue

        # Usar o par correto da Kraken
        pair = PAIR_MAP.get(asset, f'{asset}EUR')
        ticker = tickers.get(pair, {})
        if not ticker:
            continue

        price = float(ticker['c'][0])
        value = amt * price
        total_eur += value

        # OHLC para 1h e 4h
        ohlc_1h = get_ohlc(pair, 60)
        ohlc_4h = get_ohlc(pair, 240)

        chg_1h = 0.0
        chg_4h = 0.0
        rsi = 50.0

        if len(ohlc_1h) >= 3:
            closes_1h = [float(c[4]) for c in ohlc_1h]
            chg_1h = (closes_1h[-1] - closes_1h[-3]) / closes_1h[-3] * 100 if closes_1h[-3] > 0 else 0
            rsi = calc_rsi(closes_1h)

        if len(ohlc_4h) >= 3:
            closes_4h = [float(c[4]) for c in ohlc_4h]
            chg_4h = (closes_4h[-1] - closes_4h[-3]) / closes_4h[-3] * 100 if closes_4h[-3] > 0 else 0

        item = {
            'asset': asset, 'price': price, 'value_eur': value,
            'chg_1h': round(chg_1h, 2), 'chg_4h': round(chg_4h, 2),
            'rsi': round(rsi, 1),
        }
        portfolio_items.append(item)

        # Severity checks
        severity = None
        reasons = []

        if chg_1h > THRESHOLDS['pump_1h_pct']:
            severity, reasons = 'urgent', [f'PUMP +{chg_1h:.1f}% em 1h']
        elif chg_1h < -THRESHOLDS['crash_1h_pct']:
            severity, reasons = 'urgent', [f'CRASH {chg_1h:.1f}% em 1h']
        elif rsi > 0 and rsi > THRESHOLDS['rsi_urgent_high']:
            severity, reasons = 'urgent', [f'RSI extremo={rsi:.0f}']
        elif rsi > 0 and rsi < THRESHOLDS['rsi_urgent_low']:
            severity, reasons = 'urgent', [f'RSI oversold extremo={rsi:.0f}']
        elif chg_4h < -THRESHOLDS['dip_4h_pct']:
            severity, reasons = 'warning', [f'Dip {chg_4h:.1f}% em 4h']
        elif rsi > 0 and rsi > THRESHOLDS['rsi_warn_high']:
            severity, reasons = 'warning', [f'RSI sobrecomprado={rsi:.0f}']
        elif rsi > 0 and rsi < THRESHOLDS['rsi_warn_low']:
            severity, reasons = 'warning', [f'RSI oversold={rsi:.0f}']

        if severity:
            alerts.append({
                'source': 'portfolio_watch',
                'severity': severity,
                'asset': asset,
                'price': price,
                'value_eur': value,
                'reasons': reasons,
                'data': item,
            })

    return alerts, portfolio_items, total_eur


def calc_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50.0
    gains, losses = 0.0, 0.0
    for i in range(-period, 0):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


# ═══════════════════════════════════════════════════════════════
# 2. MARKET SCANNER
# ═══════════════════════════════════════════════════════════════

def market_scanner(held_assets):
    alerts = []
    tickers = get_tickers(PAIRS_SCAN)

    for pair in PAIRS_SCAN:
        asset = pair.replace('EUR', '')
        if asset in held_assets:
            continue  # ja esta no portfolio watch

        ticker = tickers.get(pair, {})
        if not ticker:
            continue

        price = float(ticker['c'][0])
        vol_24h = float(ticker['v'][1]) if len(ticker.get('v', [])) > 1 else 0
        chg_24h = float(ticker.get('p', [0, 0])[1]) if len(ticker.get('p', [])) > 1 else 0

        ohlc_1h = get_ohlc(pair, 60)
        if len(ohlc_1h) < 5:
            continue

        closes = [float(c[4]) for c in ohlc_1h]
        volumes = [float(c[6]) for c in ohlc_1h]
        current_price = closes[-1]
        rsi = calc_rsi(closes)

        # Volume spike
        avg_vol = sum(volumes[-10:]) / min(len(volumes), 10) if volumes else 1
        last_vol = volumes[-1] if volumes else 0
        vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1

        # ATR / volatility
        highs = [float(c[2]) for c in ohlc_1h]
        lows = [float(c[3]) for c in ohlc_1h]
        atr = sum(h - l for h, l in zip(highs[-14:], lows[-14:])) / min(14, len(highs)) if len(highs) >= 14 else 0
        volatility_pct = (atr / current_price * 100) if current_price > 0 else 0

        # 1h change
        chg_1h = (closes[-1] - closes[-3]) / closes[-3] * 100 if len(closes) >= 3 and closes[-3] > 0 else 0

        # Bump score
        score = 0
        reasons = []

        if volatility_pct > 8:
            score += 20
            reasons.append(f'High vol {volatility_pct:.1f}%')
        elif volatility_pct > 5:
            score += 10

        if chg_1h > 3:
            score += 20
            reasons.append(f'Bump +{chg_1h:.1f}% 1h')
        elif chg_1h > 1:
            score += 8

        if vol_ratio > THRESHOLDS['volume_spike_ratio']:
            score += 15
            reasons.append(f'Vol x{vol_ratio:.1f}')
        elif vol_ratio > 2:
            score += 8

        if rsi < 40:
            score += 10
            reasons.append(f'RSI={rsi:.0f}')
        elif rsi < 50:
            score += 5

        if chg_24h > 5:
            score += 10
            reasons.append(f'Momentum +{chg_24h:.1f}% 24h')

        severity = None
        if score >= THRESHOLDS['bump_score_urgent']:
            severity = 'urgent'
        elif score >= THRESHOLDS['bump_score_warn']:
            severity = 'warning'

        if severity:
            alerts.append({
                'source': 'market_scanner',
                'severity': severity,
                'asset': asset,
                'pair': pair,
                'price': price,
                'score': score,
                'rsi': round(rsi, 1),
                'volatility': round(volatility_pct, 2),
                'vol_ratio': round(vol_ratio, 2),
                'chg_1h': round(chg_1h, 2),
                'chg_24h': round(chg_24h, 2),
                'reasons': reasons,
            })

    return alerts


# ═══════════════════════════════════════════════════════════════
# 3. MACRO CONTEXT
# ═══════════════════════════════════════════════════════════════

def macro_context():
    alerts = []
    macro = {}

    # Fear & Greed
    try:
        url = "https://api.alternative.me/fng/?limit=2"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        current_fg = int(data['data'][0]['value'])
        current_class = data['data'][0]['value_classification']

        macro['fear_greed'] = current_fg
        macro['fg_classification'] = current_class

        if len(data['data']) > 1:
            prev_fg = int(data['data'][1]['value'])
            fg_change = current_fg - prev_fg
            macro['fg_change'] = fg_change

            if abs(fg_change) >= THRESHOLDS['fg_change_urgent']:
                alerts.append({
                    'source': 'macro_context',
                    'severity': 'urgent',
                    'reason': f'F&G mudou {fg_change:+d} pts (agora {current_fg})',
                    'macro': macro,
                })
    except:
        pass

    # BTC price & trend
    try:
        url = "https://api.kraken.com/0/public/Ticker?pair=XXBTZEUR"
        resp = urllib.request.urlopen(url, timeout=10)
        data = json.loads(resp.read())
        ticker = list(data['result'].values())[0]
        btc_price = float(ticker['c'][0])
        btc_chg_24h = float(ticker['p'][1])

        macro['btc_price'] = btc_price
        macro['btc_chg_24h'] = round(btc_chg_24h, 2)

        if btc_chg_24h < -5:
            alerts.append({
                'source': 'macro_context',
                'severity': 'warning',
                'reason': f'BTC {btc_chg_24h:+.1f}% em 24h (€{btc_price:.0f})',
                'macro': macro,
            })
    except:
        pass

    return alerts, macro


# ═══════════════════════════════════════════════════════════════
# 4. ALERT ENGINE
# ═══════════════════════════════════════════════════════════════

def save_alerts(all_alerts, portfolio_items, total_eur, macro):
    now = datetime.now(timezone.utc)
    urgent_count = sum(1 for a in all_alerts if a['severity'] == 'urgent')
    warning_count = sum(1 for a in all_alerts if a['severity'] == 'warning')

    # Load previous snapshot for trend
    prev_total = None
    prev_ts = None
    if SNAPSHOT_FILE.exists():
        try:
            with open(SNAPSHOT_FILE) as f:
                snap = json.load(f)
            prev_total = snap.get('total_eur')
            prev_ts = snap.get('ts')
        except:
            pass

    trend_eur = round(total_eur - prev_total, 2) if prev_total else None
    trend_pct = round((trend_eur / prev_total) * 100, 1) if prev_total and trend_eur is not None else None

    report = {
        'ts': now.isoformat(),
        'total_eur': round(total_eur, 2),
        'prev_total_eur': prev_total,
        'prev_ts': prev_ts,
        'trend_eur': trend_eur,
        'trend_pct': trend_pct,
        'urgent': urgent_count,
        'warning': warning_count,
        'info': len(all_alerts) - urgent_count - warning_count,
        'portfolio': portfolio_items,
        'macro': macro,
        'alerts': all_alerts,
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALERTS_FILE, 'w') as f:
        json.dump(report, f, indent=2, default=str)

    # Save snapshot for next comparison
    with open(SNAPSHOT_FILE, 'w') as f:
        json.dump({'ts': now.isoformat(), 'total_eur': round(total_eur, 2)}, f)

    return report, urgent_count > 0


def dispatch_orchestrator(report):
    """Dispara o orchestrator imediatamente se houver alertas urgentes."""
    urgent_alerts = [a for a in report['alerts'] if a['severity'] == 'urgent']
    if not urgent_alerts:
        return False

    print(f"🔴 {len(urgent_alerts)} alertas URGENTES — a disparar orchestrator...")
    for a in urgent_alerts:
        src = a.get('source', '?')
        asset = a.get('asset', a.get('reason', '?'))
        print(f"   {src}: {asset}")

    # Disparar orchestrator
    try:
        orch_script = SCRIPTS_DIR / "augustus_orchestrator_v4.py"
        cmd = f"cd {HOME} && python3 {orch_script} --mode trade 2>&1"
        import subprocess
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        print(f"   Orchestrator: exit={result.returncode}")
        # Guardar output no log
        log_dir = DATA_DIR / "sentinel_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        with open(log_dir / f"dispatch_{ts}.log", 'w') as f:
            f.write(result.stdout)
            if result.stderr:
                f.write("\n--- STDERR ---\n")
                f.write(result.stderr)

        # Notificar Rafael no Telegram
        try:
            token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
            chat_id = os.environ.get('TELEGRAM_ALLOWED_USERS', '')
            if token and chat_id:
                # Extrair linhas-chave do output
                lines = result.stdout.split('\n')
                summary_lines = [l for l in lines if any(emoji in l for emoji in
                    ['💰','🟢','🔴','🟡','📈','📉','🎯','🛑','✅','⚠️','🚀','COMPRA','VENDA','TRAILING'])]
                summary = '\n'.join(summary_lines[:8]) if summary_lines else 'Orchestrator executado. Ver logs.'
                trend_line = ""
                if report.get('trend_eur') is not None:
                    arrow = "📈" if report['trend_eur'] > 0 else "📉" if report['trend_eur'] < 0 else "➡️"
                    trend_line = f"\n{arrow} {report['trend_eur']:+.2f}€ ({report['trend_pct']:+.1f}%) vs anterior"
                msg = f"🚨 Sentinel dispatch ({ts})\n💰 Portfolio: €{report['total_eur']:.2f}{trend_line}\n🔴 {len(urgent_alerts)} alertas urgentes\n\n{summary[:800]}"
                import urllib.request as ur
                ur.urlopen(ur.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=json.dumps({'chat_id': chat_id, 'text': msg}).encode(),
                    headers={'Content-Type': 'application/json'}
                ), timeout=10)
        except:
            pass

        return True
    except Exception as e:
        print(f"   Erro ao disparar orchestrator: {e}")
        return False


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

def run():
    now = datetime.now(timezone.utc)
    all_alerts = []

    # 1. Portfolio Watch
    balance = get_balance()
    held_assets = set()
    for asset, amt in balance.items():
        if float(amt) > 0 and asset != 'ZEUR' and asset != 'ZUSD':
            held_assets.add(asset)

    if balance:
        PAIR_MAP_RUN = {'XETH': 'XETHZEUR', 'XXBT': 'XXBTZEUR', 'XXRP': 'XXRPZEUR', 'XLTC': 'XLTCZEUR', 'XDG': 'XDGEUR'}
        pairs_held = [PAIR_MAP_RUN.get(a, f'{a}EUR') for a in held_assets]
        tickers = get_tickers(pairs_held) if pairs_held else {}
        pw_alerts, portfolio_items, total_eur = portfolio_watch(balance, tickers)
        all_alerts.extend(pw_alerts)
    else:
        portfolio_items, total_eur = [], 0.0

    # 2. Market Scanner
    ms_alerts = market_scanner(held_assets)
    all_alerts.extend(ms_alerts)

    # 3. Macro Context
    mc_alerts, macro = macro_context()
    all_alerts.extend(mc_alerts)

    # 4. Save & Dispatch
    report, has_urgent = save_alerts(all_alerts, portfolio_items, total_eur, macro)

    # Summary
    trend_str = ""
    if report.get('trend_eur') is not None:
        arrow = "📈" if report['trend_eur'] > 0 else "📉" if report['trend_eur'] < 0 else "➡️"
        trend_str = f" | {arrow} {report['trend_eur']:+.2f}€ ({report['trend_pct']:+.1f}%)"
    print(f"[{now.strftime('%H:%M')}] Sentinel: €{total_eur:.2f}{trend_str} | "
          f"{report['urgent']}🔴 {report['warning']}🟡 {report['info']}🟢 | "
          f"F&G:{macro.get('fear_greed','?')}")

    if has_urgent:
        dispatch_orchestrator(report)

    return report


if __name__ == '__main__':
    run()
