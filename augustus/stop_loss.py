#!/usr/bin/env python3
"""
Augustus Stop-Loss Manager
==========================
Gerencia stop-loss automaticos para todas as posicoes.
Modo A: Ordens stop-loss nativas na Kraken (quando possivel)
Modo B: Watchdog via script (fallback, corre de 5 em 5 min)

Uso:
  python3 stop_loss_manager.py --check    # Verifica e executa stops
  python3 stop_loss_manager.py --register posicao stop  # Regista novo stop
  python3 stop_loss_manager.py --status   # Mostra estado atual
"""

import json, os, sys, time
from datetime import datetime
from pathlib import Path

# Caminhos
STATE_FILE = Path.home() / ".hermes/data/stop_loss_state.json"
LOG_DIR = Path.home() / ".hermes/data/stop_loss_logs"

# Configuracoes padrao
DEFAULT_STOP_PERCENT = 5.0   # stop-loss a -5% do preco de entrada
TRAILING_PERCENT = 5.0       # trailing stop a -5% (usa o mesmo que o stop inicial)
PRICE_DECIMALS = 6           # precisao para stops (ativos baratos quebram com 2)


def load_state():
    """Carrega estado atual dos stop-losses."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "history": []}


def save_state(state):
    """Guarda estado."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_current_prices():
    """Obtem precos atuais de todas as posicoes via Kraken ticker."""
    import urllib.request
    
    # Mapeamento de ativos para pares Kraken
    asset_pair_map = {
        'XXBT': 'XXBTZEUR', 'XBT': 'XXBTZEUR',
        'XETH': 'XETHZEUR', 'ETH': 'XETHZEUR',
        'SOL': 'SOLEUR', 'XSOL': 'SOLEUR',
        'ADA': 'ADAEUR', 'DOT': 'DOTEUR',
        'LINK': 'LINKEUR', 'AAVE': 'AAVEEUR',
        'USUAL': 'USUALEUR', 'QUAI': 'QUAIEUR',
        'APE': 'APEEUR', 'ALGO': 'ALGOEUR',
        'AVAX': 'AVAXEUR', 'BIO': 'BIOEUR',
        'GAME2': 'GAME2EUR', 'SAND': 'SANDEUR',
        'ARPA': 'ARPAEUR', 'ATOM': 'ATOMEUR',
        'GALA': 'GALAEUR', 'SUI': 'SUIEUR',
        'XRP': 'XXRPZEUR', 'XXRP': 'XXRPZEUR',
        'TRUMP': 'TRUMPEUR', 'TRX': 'TRXEUR',
        'NEAR': 'NEAREUR', 'UNI': 'UNIEUR',
        'PEPE': 'PEPEEUR', 'DOGE': 'DOGEEUR',
        'TAO': 'TAOEUR', 'JASMY': 'JASMYEUR',
    }
    
    pairs = list(set(asset_pair_map.values()))
    url = "https://api.kraken.com/0/public/Ticker?pair=" + ",".join(pairs)
    
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        prices = {}
        for kraken_pair, ticker in data.get('result', {}).items():
            prices[kraken_pair] = float(ticker.get('c', [0])[0])
        return prices, asset_pair_map
    except Exception as e:
        print(f"[SL] ERRO ao obter precos: {e}")
        return {}, {}


def register_stop(asset, qty, entry_price, stop_price=None, stop_percent=None):
    """
    Regista um novo stop-loss para uma posicao.
    Se stop_price nao for dado, calcula com base na percentagem.
    """
    state = load_state()
    
    if stop_price is None:
        pct = stop_percent if stop_percent is not None else DEFAULT_STOP_PERCENT
        # Usar abs() para garantir que a percentagem e sempre subtraida
        pct_abs = abs(pct)
        stop_price = round(entry_price * (1 - pct_abs / 100), PRICE_DECIMALS)
    
    # Normalizar nome do ativo
    asset_key = asset.upper()
    
    state["positions"][asset_key] = {
        "qty": float(qty),
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "stop_percent": abs(stop_percent) if stop_percent else DEFAULT_STOP_PERCENT,
        "created_at": datetime.now().isoformat(),
        "trailing_high": float(entry_price),  # para trailing stop
    }
    
    save_state(state)
    
    print(f"[SL] Stop-loss registado: {asset_key}")
    print(f"      Quantidade: {qty}")
    print(f"      Entrada: {entry_price:.6f} EUR")
    print(f"      Stop: {stop_price:.6f} EUR ({abs(stop_percent) if stop_percent else DEFAULT_STOP_PERCENT:.1f}%)")
    
    return state["positions"][asset_key]


def check_stops(dry_run=False):
    """
    Verifica todas as posicoes e executa stop-loss se necessario.
    Modo B: watchdog via script.
    """
    state = load_state()
    positions = state.get("positions", {})
    
    if not positions:
        print("[SL] Nenhuma posicao com stop-loss ativo.")
        return
    
    prices, asset_map = get_current_prices()
    
    actions = []
    
    for asset, pos in list(positions.items()):
        # Encontrar o par Kraken para este ativo
        pair = asset_map.get(asset)
        if not pair:
            print(f"[SL] Aviso: par desconhecido para {asset}")
            continue
        
        current_price = prices.get(pair)
        if current_price is None:
            print(f"[SL] Aviso: sem preco para {asset} ({pair})")
            continue
        
        stop_price = pos["stop_price"]
        entry_price = pos["entry_price"]
        qty = pos["qty"]
        
        # Verificar ordermin: se a posicao e menor que o minimo, nunca vai conseguir vender
        ordermin_key = f"_ordermin_{asset}"
        ordermin = pos.get(ordermin_key)
        if ordermin is None:
            # Buscar ordermin uma vez e guardar
            try:
                import urllib.request
                pair_url = f"https://api.kraken.com/0/public/AssetPairs?pair={pair}"
                pair_data = json.loads(urllib.request.urlopen(pair_url, timeout=10).read())
                ordermin = float(list(pair_data['result'].values())[0].get('ordermin', 0))
                pos[ordermin_key] = ordermin
            except:
                ordermin = 0
        
        if ordermin > 0 and qty < ordermin:
            # Posicao demasiado pequena para vender - nao tentar
            continue
        
        # Atualizar trailing high (se o preco subiu)
        trailing_high = pos.get("trailing_high", entry_price)
        if current_price > trailing_high:
            pos["trailing_high"] = current_price
            # Usar a mesma percentagem com que o stop foi registado (sempre positiva)
            trailing_percent = abs(pos.get("stop_percent", DEFAULT_STOP_PERCENT))
            new_stop = round(current_price * (1 - trailing_percent / 100), PRICE_DECIMALS)
            # So atualizar se o novo stop for MAIOR que o atual (trailing sobe, nunca desce)
            # E CRITICO: o novo stop nunca pode estar acima do preco atual
            if new_stop > stop_price and new_stop < current_price:
                pos["stop_price"] = new_stop
                stop_price = new_stop
                print(f"[SL] Trailing atualizado: {asset} stop agora a {stop_price:.6f}")
        
        # Verificar se stop foi atingido
        if current_price <= stop_price:
            print(f"[SL] STOP ATINGIDO: {asset}")
            print(f"      Preco atual: {current_price:.6f}")
            print(f"      Stop: {stop_price:.6f}")
            print(f"      Entrada: {entry_price:.6f}")
            pnl_pct = ((current_price / entry_price) - 1) * 100
            print(f"      Diferenca: {pnl_pct:.2f}%")
            
            # Validacao de seguranca: nunca vender se stop_price > entry_price
            # (so acontece se o trailing stop foi corrompido)
            if stop_price > entry_price:
                print(f"[SL] SEGURANCA: Stop ({stop_price:.6f}) acima da entrada ({entry_price:.6f}). Venda bloqueada.")
                continue
            
            if not dry_run:
                # Executar venda
                sell_ok = False
                try:
                    sys.path.insert(0, str(Path.home() / ".hermes/scripts"))
                    from kraken_lib import get_kraken_client, place_order
                    
                    k = get_kraken_client()
                    result = place_order(
                        k, pair=pair, order_type="sell",
                        volume=qty, price=None,
                        order_type_ext="market", validate=False
                    )
                    
                    actions.append({
                        "asset": asset,
                        "qty": qty,
                        "exit_price": current_price,
                        "entry_price": entry_price,
                        "pnl": (current_price - entry_price) * qty,
                        "pnl_percent": ((current_price / entry_price) - 1) * 100,
                        "result": result,
                        "timestamp": datetime.now().isoformat(),
                    })
                    
                    print(f"[SL] VENDA EXECUTADA: {qty} {asset} a {current_price:.6f}")
                    sell_ok = True
                    
                except Exception as e:
                    print(f"[SL] ERRO ao vender {asset}: {e}")
                    actions.append({
                        "asset": asset,
                        "error": str(e),
                        "timestamp": datetime.now().isoformat(),
                    })
                
                # So remover do estado se a venda foi executada com sucesso
                if sell_ok:
                    del state["positions"][asset]
                else:
                    print(f"[SL] Posicao {asset} mantida no estado (venda falhou).")
    
    # Salvar historico e estado
    if actions:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"stop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(log_file, "w") as f:
            json.dump(actions, f, indent=2, default=str)
        
        state["history"].extend(actions)
    
    save_state(state)
    return actions


def show_status():
    """Mostra estado atual dos stop-losses."""
    state = load_state()
    positions = state.get("positions", {})
    history = state.get("history", [])
    
    print(f"=== AUGUSTUS STOP-LOSS MANAGER ===")
    print()
    
    if not positions:
        print("Nenhuma posicao com stop-loss ativo.")
    else:
        print(f"Posicoes ativas ({len(positions)}):")
        print(f"{'Ativo':8s} {'Qty':12s} {'Entrada':10s} {'Stop':10s} {'Atual':10s} {'Var':8s}")
        print("-" * 60)
        
        prices, asset_map = get_current_prices()
        
        for asset, pos in sorted(positions.items()):
            pair = asset_map.get(asset, "")
            current = prices.get(pair, 0)
            var = ((current / pos["entry_price"]) - 1) * 100 if current and pos["entry_price"] else 0
            sl_distance = ((pos["stop_price"] / pos["entry_price"]) - 1) * 100
            
            print(f"{asset:8s} {pos['qty']:<12.6f} {pos['entry_price']:<10.2f} "
                  f"{pos['stop_price']:<10.2f} {current:<10.2f} {var:>+7.2f}%")
    
    if history:
        recent = history[-5:]
        print(f"\nUltimas acoes ({len(history)} total):")
        for h in recent:
            if "error" in h:
                print(f"  ❌ {h['asset']}: ERRO - {h['error']}")
            else:
                print(f"  ✅ {h['asset']}: vendido {h['qty']} a {h['exit_price']:.2f} "
                      f"(P&L: {h['pnl']:+.2f} EUR / {h['pnl_percent']:+.2f}%)")


def cleanup_stale_positions(max_age_hours=72):
    """
    Remove posicoes que ja deviam ter sido vendidas manualmente.
    (prevencao de stops fantasmas)
    """
    state = load_state()
    now = datetime.now()
    removed = []
    
    for asset, pos in list(state["positions"].items()):
        created = datetime.fromisoformat(pos["created_at"])
        age = (now - created).total_seconds() / 3600
        if age > max_age_hours:
            removed.append(asset)
            del state["positions"][asset]
    
    if removed:
        print(f"[SL] Limpeza: {len(removed)} posicoes expiradas removidas")
        save_state(state)
    
    return removed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Augustus Stop-Loss Manager")
    parser.add_argument("--check", action="store_true", help="Verifica e executa stops")
    parser.add_argument("--dry-run", action="store_true", help="So simula, nao executa")
    parser.add_argument("--status", action="store_true", help="Mostra estado atual")
    parser.add_argument("--register", nargs=3, metavar=("ASSET", "QTY", "ENTRY"),
                        help="Regista novo stop: ASSET QTY ENTRY_PRICE")
    parser.add_argument("--stop-price", type=float, help="Preco de stop (opcional no register)")
    parser.add_argument("--cleanup", action="store_true", help="Remove posicoes expiradas")
    
    args = parser.parse_args()
    
    if args.status:
        show_status()
    elif args.check:
        check_stops(dry_run=args.dry_run)
    elif args.register:
        asset, qty, entry = args.register
        register_stop(asset, float(qty), float(entry), 
                     stop_price=args.stop_price)
    elif args.cleanup:
        cleanup_stale_positions()
    else:
        parser.print_help()
