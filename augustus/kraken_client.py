#!/usr/bin/env python3
"""
Kraken API client - shared library for Augustus Trading.
Uses krakenex library (battle-tested HMAC signing).

Source: augustus-trading/augustus/kraken_client.py
"""
import os, sys, json, urllib.request
import krakenex


def get_kraken_client():
    """Initialize krakenex client with credentials from environment or .env file."""
    key = os.environ.get('KRAKEN_API_KEY')
    secret = os.environ.get('KRAKEN_SECRET_KEY')

    # Fallback: try to source from .env in project root
    if not key or not secret:
        env_paths = [
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'),
            os.path.expanduser('~/.augustus/.env'),
            '.env',
        ]
        for env_path in env_paths:
            if os.path.exists(env_path):
                with open(env_path) as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith('KRAKEN_API_KEY='):
                            key = line.split('=', 1)[1].strip().strip("'").strip('"')
                        elif line.startswith('KRAKEN_SECRET_KEY='):
                            secret = line.split('=', 1)[1].strip().strip("'").strip('"')
                if key and secret:
                    break

    if not key or not secret:
        raise ValueError(
            "KRAKEN_API_KEY and KRAKEN_SECRET_KEY must be set.\n"
            "Copy .env.example to .env and fill in your Kraken API credentials."
        )
    return krakenex.API(key=key, secret=secret)


def get_balance(k=None):
    """Get account balance (non-zero only)."""
    if k is None:
        k = get_kraken_client()
    result = k.query_private('Balance')
    if result.get('error') and result['error']:
        raise Exception(f"Balance error: {result['error']}")
    bal = result.get('result', {})
    return {a: float(b) for a, b in bal.items() if float(b) > 0}


def show_all_balances(k=None, title="PORTFOLIO COMPLETO"):
    """Print all assets with balance > 0 in a formatted table."""
    bal = get_balance(k)
    if not bal:
        print(f"  {title}: (vazio)")
        return bal

    # Get prices for valuation
    total, breakdown = calculate_portfolio_value(bal, k)

    sep = "=" * 50
    dash = chr(9472) * 50

    print(f"  {sep}")
    print(f"  {title}")
    print(f"  {sep}")
    for asset, qty in sorted(bal.items(), key=lambda x: x[0]):
        val = breakdown.get(asset, 0)
        if val and val != qty and val > 0:
            print(f"  {asset:8s}  {qty:<20.8f}  {val:>8.2f} EUR")
        else:
            print(f"  {asset:8s}  {qty:<20.8f}")
    print(f"  {dash}")
    print(f"  {'TOTAL':8s}  {'':20s}  {total:>8.2f} EUR")
    print(f"  {sep}")
    return bal


def get_trade_balance(k=None, asset='ZEUR'):
    """Get trade balance information."""
    if k is None:
        k = get_kraken_client()
    result = k.query_private('TradeBalance', {'asset': asset})
    if result.get('error') and result['error']:
        raise Exception(f"TradeBalance error: {result['error']}")
    return result.get('result', {})


def get_ticker(pairs):
    """Get public ticker prices. pairs: list of strings like ['XBTEUR', 'ETHEUR']."""
    url = "https://api.kraken.com/0/public/Ticker?pair=" + ",".join(pairs)
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    return data.get('result', {})


def place_order(k, pair, order_type, volume, price=None, order_type_ext='limit', validate=True):
    """
    Place an order on Kraken.

    Args:
        k: krakenex API client
        pair: e.g. 'QUAIEUR', 'XETHzEUR'
        order_type: 'buy' or 'sell'
        volume: amount in base currency (e.g. QUAI amount)
        price: limit price (None for market order)
        order_type_ext: 'limit', 'market', or 'stop-loss'
        validate: if True, only validates (no real order)

    Returns:
        dict with order result
    """
    data = {
        'pair': pair,
        'type': order_type,
        'ordertype': order_type_ext,
        'volume': str(volume),
        'validate': validate,
    }
    if price:
        data['price'] = str(price)

    result = k.query_private('AddOrder', data)
    if result.get('error') and result['error']:
        raise Exception(f"Order error: {result['error']}")
    return result.get('result', {})


def place_stop_loss(k, pair, volume, stop_price, validate=True):
    """
    Place a native stop-loss order on Kraken.
    When price hits stop_price, triggers market sell.
    """
    data = {
        'pair': pair,
        'type': 'sell',
        'ordertype': 'stop-loss',
        'volume': str(volume),
        'price': str(stop_price),
        'validate': validate,
    }

    result = k.query_private('AddOrder', data)
    if result.get('error') and result['error']:
        raise Exception(f"Stop-loss error: {result['error']}")
    return result.get('result', {})


def calculate_portfolio_value(balances=None, k=None):
    """Calculate total portfolio value in EUR."""
    if balances is None:
        balances = get_balance(k)

    asset_to_pair = {
        'ZEUR': None,
        'XXBT': 'XXBTZEUR', 'XBT': 'XXBTZEUR',
        'XETH': 'XETHZEUR', 'ETH': 'XETHZEUR',
        'XXRP': 'XXRPZEUR', 'XRP': 'XXRPZEUR',
        'QUAI': 'QUAIEUR',
        'USUAL': 'USUALEUR',
        'USDT': 'USDTEUR',
        'SOL': 'SOLEUR', 'XSOL': 'SOLEUR',
        'ADA': 'ADAEUR',
        'DOT': 'DOTEUR',
        'LINK': 'LINKEUR',
        'MATIC': 'MATICEUR',
        'AAVE': 'AAVEEUR',
    }

    pairs_needed = []
    asset_pair_map = {}
    for asset in balances:
        pair = asset_to_pair.get(asset)
        if pair:
            if pair not in pairs_needed:
                pairs_needed.append(pair)
            asset_pair_map[asset] = pair

    prices = {}
    if pairs_needed:
        prices = get_ticker(pairs_needed)

    total = 0.0
    breakdown = {}

    for asset, qty in balances.items():
        if asset == 'ZEUR':
            total += qty
            breakdown['EUR'] = qty
        else:
            pair = asset_pair_map.get(asset)
            if pair and pair in prices:
                price = float(prices[pair].get('c', [0])[0])
                val = qty * price
                total += val
                breakdown[asset] = val
            else:
                breakdown[asset] = qty

    return total, breakdown


if __name__ == '__main__':
    k = get_kraken_client()
    show_all_balances(k, "PORTFOLIO COMPLETO")
