#!/usr/bin/env python3
"""
Augustus Config Manager
=======================
Le config.yaml, aplica overrides do .env, valida ranges, mostra estado.

Uso:
  python -m augustus.config              # Mostra toda a config atual
  python -m augustus.config --set KEY=VALUE  # Altera um parametro
  python -m augustus.config --reset          # Repoe defaults
  python -m augustus.config --validate       # Valida sem alterar

Source: augustus-trading/augustus/config.py
"""

import os
import sys
import yaml
from pathlib import Path
from copy import deepcopy

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.yaml"
USER_CONFIG = Path.home() / ".augustus" / "config.yaml"

# ─── Safe ranges for validation ───────────────────────────────────────────────

SAFE_RANGES = {
    "trading.trade_pct":             (0.01, 0.50),
    "trading.min_trade_eur":         (0.50, 10.0),
    "trading.max_trade_eur":         (2.0, 50.0),
    "bull.rsi_oversold":             (15, 50),
    "bull.rsi_overbought":           (50, 90),
    "bear.cash_target":              (0.10, 0.80),
    "bear.rsi_buy":                  (5, 30),
    "bear.max_position":             (0.05, 0.50),
    "stop_loss.stop_percent":        (1.0, 20.0),
    "stop_loss.trailing_percent":    (1.0, 20.0),
    "risk.max_consecutive_losses":   (1, 10),
    "risk.max_daily_drawdown_pct":   (5.0, 50.0),
    "risk.vol_spike_btc_pct_1h":    (2.0, 15.0),
}

DESCRIPTIONS = {
    "trading.trade_pct":             "% do portfolio por trade",
    "trading.min_trade_eur":         "Minimo EUR por trade",
    "trading.max_trade_eur":         "Maximo EUR por trade (teto)",
    "bull.rsi_oversold":             "RSI abaixo disto → compra em bull",
    "bull.rsi_overbought":           "RSI acima disto → venda em bull",
    "bear.cash_target":              "% cash em bear market",
    "bear.rsi_buy":                  "RSI maximo para comprar em bear",
    "bear.max_position":             "% maxima do portfolio por ativo em bear",
    "stop_loss.stop_percent":        "Stop-loss: -X% abaixo da entrada",
    "stop_loss.trailing_percent":    "Trailing stop: -X% abaixo do pico",
    "risk.max_consecutive_losses":   "Perdas consecutivas → pausa 24h",
    "risk.max_daily_drawdown_pct":   "Drawdown diario maximo → fecha tudo",
    "risk.vol_spike_btc_pct_1h":    "BTC move >X% em 1h → pausa",
}

# Aliases: nomes curtos que o utilizador pode usar
ALIASES = {
    "trade_pct":          "trading.trade_pct",
    "min_trade":          "trading.min_trade_eur",
    "max_trade":          "trading.max_trade_eur",
    "bull_rsi_buy":       "bull.rsi_oversold",
    "bull_rsi_sell":      "bull.rsi_overbought",
    "bear_cash":          "bear.cash_target",
    "bear_rsi":           "bear.rsi_buy",
    "bear_max_pos":       "bear.max_position",
    "stop":               "stop_loss.stop_percent",
    "trailing":           "stop_loss.trailing_percent",
    "stop_percent":       "stop_loss.stop_percent",
    "trailing_percent":   "stop_loss.trailing_percent",
    "max_losses":         "risk.max_consecutive_losses",
    "max_drawdown":       "risk.max_daily_drawdown_pct",
    "vol_spike":          "risk.vol_spike_btc_pct_1h",
}


def _resolve_key(key):
    """Resolve alias or return the key as-is."""
    return ALIASES.get(key, key)


def load_config():
    """Load config from YAML, apply env var overrides."""
    if not CONFIG_FILE.exists():
        print(f"⚠️  {CONFIG_FILE} nao encontrado. A criar default...")
        _create_default_config()

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    # Apply environment variable overrides
    env_map = {
        "AUGUSTUS_TRADE_PCT":            "trading.trade_pct",
        "AUGUSTUS_MIN_TRADE_EUR":        "trading.min_trade_eur",
        "AUGUSTUS_MAX_TRADE_EUR":        "trading.max_trade_eur",
        "AUGUSTUS_BULL_RSI_OVERSOLD":    "bull.rsi_oversold",
        "AUGUSTUS_BULL_RSI_OVERBOUGHT":  "bull.rsi_overbought",
        "AUGUSTUS_BEAR_CASH":            "bear.cash_target",
        "AUGUSTUS_BEAR_RSI":             "bear.rsi_buy",
        "AUGUSTUS_BEAR_MAX_POS":         "bear.max_position",
        "AUGUSTUS_STOP_PERCENT":         "stop_loss.stop_percent",
        "AUGUSTUS_TRAILING_PERCENT":     "stop_loss.trailing_percent",
    }

    for env_var, path in env_map.items():
        val = os.environ.get(env_var)
        if val is not None:
            try:
                _set_nested(config, path, float(val))
            except (ValueError, KeyError):
                pass

    return config


def _get_nested(d, path):
    """Get nested dict value by dotted path."""
    keys = path.split(".")
    for k in keys:
        d = d[k]
    return d


def _set_nested(d, path, value):
    """Set nested dict value by dotted path."""
    keys = path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def validate_config(config):
    """Check all values are in safe ranges. Returns list of warnings."""
    warnings = []
    for path, (lo, hi) in SAFE_RANGES.items():
        try:
            val = _get_nested(config, path)
        except (KeyError, TypeError):
            warnings.append(f"❌ {path}: parametro em falta")
            continue

        if not (lo <= val <= hi):
            desc = DESCRIPTIONS.get(path, "")
            warnings.append(
                f"⚠️  {path}={val} fora do range seguro [{lo}-{hi}] — {desc}"
            )
    return warnings


def show_config(config):
    """Pretty-print current configuration."""
    print()
    print("┌─────────────────────────────────────────────────┐")
    print("│           AUGUSTUS — Configuracao Atual          │")
    print("├─────────────────────────────────────────────────┤")
    print("│ TAMANHO DOS TRADES                              │")
    print(f"│   trade_pct:          {config['trading']['trade_pct']:.0%} do portfolio".replace("%", "%"))
    print(f"│   min_trade_eur:      €{config['trading']['min_trade_eur']:.2f}")
    print(f"│   max_trade_eur:      €{config['trading']['max_trade_eur']:.2f}")
    print("│                                                 │")
    print("│ BULL MARKET                                     │")
    print(f"│   comprar RSI <       {config['bull']['rsi_oversold']}")
    print(f"│   vender RSI >        {config['bull']['rsi_overbought']}")
    print("│                                                 │")
    print("│ BEAR MARKET                                     │")
    print(f"│   cash target:        {config['bear']['cash_target']:.0%}".replace("%", "%"))
    print(f"│   comprar RSI <       {config['bear']['rsi_buy']}")
    print(f"│   max por ativo:      {config['bear']['max_position']:.0%}".replace("%", "%"))
    print("│                                                 │")
    print("│ STOP-LOSS                                       │")
    print(f"│   stop:               -{config['stop_loss']['stop_percent']}%")
    print(f"│   trailing:           -{config['stop_loss']['trailing_percent']}%")
    print("│                                                 │")
    print("│ RISCO                                           │")
    print(f"│   max perdas seguidas: {config['risk']['max_consecutive_losses']}")
    print(f"│   max drawdown diario: {config['risk']['max_daily_drawdown_pct']}%")
    print(f"│   circuit breaker BTC: {config['risk']['vol_spike_btc_pct_1h']}%")
    print("│                                                 │")
    print("│ FONTE                                           │")
    env_overrides = []
    for env_var in ["AUGUSTUS_TRADE_PCT", "AUGUSTUS_MIN_TRADE_EUR",
                     "AUGUSTUS_BULL_RSI_OVERSOLD", "AUGUSTUS_BEAR_CASH",
                     "AUGUSTUS_STOP_PERCENT"]:
        if os.environ.get(env_var):
            env_overrides.append(env_var)
    if env_overrides:
        print(f"│ .env overrides: {', '.join(env_overrides[:3])}")
    else:
        print("│ Fonte: config.yaml (sem overrides .env)")
    print("└─────────────────────────────────────────────────┘")
    print()


def set_param(key, value_str):
    """Set a parameter in config.yaml."""
    key = _resolve_key(key)
    if key not in SAFE_RANGES:
        print(f"❌ Parametro desconhecido: {key}")
        print(f"   Parametros disponiveis: {', '.join(SAFE_RANGES.keys())}")
        return

    try:
        value = float(value_str)
    except ValueError:
        print(f"❌ Valor invalido: {value_str} (esperado numero)")
        return

    lo, hi = SAFE_RANGES[key]
    if not (lo <= value <= hi):
        desc = DESCRIPTIONS.get(key, "")
        print(f"⚠️  {key}={value} esta fora do range seguro [{lo}-{hi}]")
        print(f"   {desc}")
        resp = input("   Continuar mesmo assim? (s/N): ")
        if resp.lower() != "s":
            print("   Cancelado.")
            return

    # Load, update, save
    if not CONFIG_FILE.exists():
        _create_default_config()

    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f)

    _set_nested(config, key, value)

    with open(CONFIG_FILE, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    desc = DESCRIPTIONS.get(key, "")
    print(f"✅ {key} = {value} ({desc})")
    print(f"   Guardado em {CONFIG_FILE}")


def _create_default_config():
    """Create default config.yaml if missing."""
    import shutil
    template = PROJECT_ROOT / "config.yaml"
    if template.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(template, CONFIG_FILE)


# ─── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--set" in sys.argv:
        idx = sys.argv.index("--set")
        if idx + 1 < len(sys.argv):
            pair = sys.argv[idx + 1]
            if "=" in pair:
                key, value = pair.split("=", 1)
                set_param(key, value)
            else:
                print("❌ Formato: --set chave=valor")
        else:
            print("❌ Formato: --set chave=valor")
    elif "--reset" in sys.argv:
        _create_default_config()
        print("✅ Configuracao reposta para defaults.")
    elif "--validate" in sys.argv:
        config = load_config()
        warnings = validate_config(config)
        if warnings:
            for w in warnings:
                print(w)
            sys.exit(1)
        else:
            print("✅ Todos os parametros dentro dos ranges seguros.")
    else:
        config = load_config()
        warnings = validate_config(config)
        if warnings:
            for w in warnings:
                print(w)
            print()
        show_config(config)
