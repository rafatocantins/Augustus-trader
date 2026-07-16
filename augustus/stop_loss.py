#!/usr/bin/env python3
"""
Augustus Stop-Loss Manager
==========================
Manages automatic stop-losses for all positions.
Mode A: Native Kraken stop-loss orders (when possible)
Mode B: Watchdog via script (fallback, runs every 5 min)

Source: augustus-trading/augustus/stop_loss.py

Usage:
  python3 stop_loss.py --check       # Check and execute stops
  python3 stop_loss.py --register ASSET QTY ENTRY_PRICE  # Register new stop
  python3 stop_loss.py --status      # Show current state
"""

import json, os, sys, time
from datetime import datetime
from pathlib import Path

# Configurable paths
DATA_DIR = Path(os.environ.get('AUGUSTUS_DATA_DIR', Path.home() / '.augustus'))
STATE_FILE = DATA_DIR / 'stop_loss_state.json'
LOG_DIR = DATA_DIR / 'stop_loss_logs'

# Default settings
DEFAULT_STOP_PERCENT = 3.0
TRAILING_PERCENT = 2.0


def load_state():
    """Load current stop-loss state."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"positions": {}, "history": []}


def save_state(state):
    """Save state."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_current_prices():
    """Get current prices for all positions via Kraken ticker."""
    import urllib.request

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
        print(f"[SL] ERROR getting prices: {e}")
        return {}, {}


def register_stop(asset, qty, entry_price, stop_price=None, stop_percent=None):
    """Register a new stop-loss for a position."""
    state = load_state()

    if stop_price is None:
        stop_percent = stop_percent or DEFAULT_STOP_PERCENT
        stop_price = round(entry_price * (1 - stop_percent / 100), 2)

    asset_key = asset.upper()

    state["positions"][asset_key] = {
        "qty": float(qty),
        "entry_price": float(entry_price),
        "stop_price": float(stop_price),
        "stop_percent": stop_percent or DEFAULT_STOP_PERCENT,
        "created_at": datetime.now().isoformat(),
        "trailing_high": float(entry_price),
    }

    save_state(state)

    print(f"[SL] Stop-loss registered: {asset_key}")
    print(f"      Quantity: {qty}")
    print(f"      Entry: {entry_price:.2f} EUR")
    print(f"      Stop: {stop_price:.2f} EUR ({stop_percent or DEFAULT_STOP_PERCENT:.1f}%)")

    return state["positions"][asset_key]


def check_stops(dry_run=False):
    """Check all positions and execute stop-loss if needed."""
    state = load_state()
    positions = state.get("positions", {})

    if not positions:
        print("[SL] No positions with active stop-loss.")
        return

    prices, asset_map = get_current_prices()

    actions = []

    for asset, pos in list(positions.items()):
        pair = asset_map.get(asset)
        if not pair:
            print(f"[SL] Warning: unknown pair for {asset}")
            continue

        current_price = prices.get(pair)
        if current_price is None:
            print(f"[SL] Warning: no price for {asset} ({pair})")
            continue

        stop_price = pos["stop_price"]
        entry_price = pos["entry_price"]
        qty = pos["qty"]

        # Update trailing high
        trailing_high = pos.get("trailing_high", entry_price)
        if current_price > trailing_high:
            pos["trailing_high"] = current_price
            trailing_percent = DEFAULT_STOP_PERCENT
            new_stop = round(current_price * (1 - trailing_percent / 100), 2)
            if new_stop > stop_price:
                pos["stop_price"] = new_stop
                stop_price = new_stop
                print(f"[SL] Trailing updated: {asset} stop now at {stop_price:.2f}")

        # Check if stop was hit
        if current_price <= stop_price:
            print(f"[SL] STOP HIT: {asset}")
            print(f"      Current price: {current_price:.2f}")
            print(f"      Stop: {stop_price:.2f}")
            print(f"      Entry: {entry_price:.2f}")
            print(f"      Change: {((current_price/entry_price)-1)*100:.2f}%")

            if not dry_run:
                try:
                    from augustus.kraken_client import get_kraken_client, place_order

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

                    print(f"[SL] SALE EXECUTED: {qty} {asset} at {current_price:.2f}")

                except Exception as e:
                    print(f"[SL] ERROR selling {asset}: {e}")
                    actions.append({
                        "asset": asset,
                        "error": str(e),
                        "timestamp": datetime.now().isoformat(),
                    })

            del state["positions"][asset]

    if actions:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"stop_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(log_file, "w") as f:
            json.dump(actions, f, indent=2, default=str)

        state["history"].extend(actions)

    save_state(state)
    return actions


def show_status():
    """Show current stop-loss status."""
    state = load_state()
    positions = state.get("positions", {})
    history = state.get("history", [])

    print("=== AUGUSTUS STOP-LOSS MANAGER ===")
    print()

    if not positions:
        print("No positions with active stop-loss.")
    else:
        print(f"Active positions ({len(positions)}):")
        print(f"{'Asset':8s} {'Qty':12s} {'Entry':10s} {'Stop':10s} {'Current':10s} {'Var':8s}")
        print("-" * 60)

        prices, asset_map = get_current_prices()

        for asset, pos in sorted(positions.items()):
            pair = asset_map.get(asset, "")
            current = prices.get(pair, 0)
            var = ((current / pos["entry_price"]) - 1) * 100 if current and pos["entry_price"] else 0

            print(f"{asset:8s} {pos['qty']:<12.6f} {pos['entry_price']:<10.2f} "
                  f"{pos['stop_price']:<10.2f} {current:<10.2f} {var:>+7.2f}%")

    if history:
        recent = history[-5:]
        print(f"\nRecent actions ({len(history)} total):")
        for h in recent:
            if "error" in h:
                print(f"  ❌ {h['asset']}: ERROR - {h['error']}")
            else:
                print(f"  ✅ {h['asset']}: sold {h['qty']} at {h['exit_price']:.2f} "
                      f"(P&L: {h['pnl']:+.2f} EUR / {h['pnl_percent']:+.2f}%)")


def cleanup_stale_positions(max_age_hours=72):
    """Remove positions that should have been sold manually already."""
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
        print(f"[SL] Cleanup: {len(removed)} expired positions removed")
        save_state(state)

    return removed


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Augustus Stop-Loss Manager")
    parser.add_argument("--check", action="store_true", help="Check and execute stops")
    parser.add_argument("--dry-run", action="store_true", help="Simulate only, don't execute")
    parser.add_argument("--status", action="store_true", help="Show current state")
    parser.add_argument("--register", nargs=3, metavar=("ASSET", "QTY", "ENTRY"),
                        help="Register new stop: ASSET QTY ENTRY_PRICE")
    parser.add_argument("--stop-price", type=float, help="Stop price (optional for register)")
    parser.add_argument("--cleanup", action="store_true", help="Remove expired positions")

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
