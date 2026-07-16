# Augustus Trading

**LLM-driven autonomous crypto trading agent for Kraken.**

Augustus is a self-contained Python agent that manages a cryptocurrency portfolio using Large Language Models. It collects market data, performs sanity validation, asks an LLM to decide what to trade, validates the decision, and executes real trades on Kraken.

> **Status:** In production since June 2026. Managing a ~EUR60 portfolio with proportional trades (15% per trade, EUR3-15 range).

---

## Architecture

```
                   +-----------+
                   |  Kraken   |  Account balances
                   |  Exchange |---------+
                   +-----------+         |
                                         v
+-------------+    +-----------+    +----------+    +--------+    +----------+
| CoinGecko   |--->| Sanity    |--->| LLM      |--->| Risk   |--->| Kraken   |
| Market Data |    | Check     |    | Analysis |    | Module |    | Execution|
+-------------+    +-----------+    +----------+    +--------+    +----------+
     |                  |                |               |              |
     |           [2606.10749]     DeepSeek V4 Pro    Deterministic   Real orders
     |           Threat Surfaces  (configurable)     circuit break   with stop-loss
     |                                                              registration
     +--- 10min cache ---+--- SHA256 state integrity [2606.24322] ---+
```

### Security Layers (Paper-Driven)

| Layer | Paper | What It Does |
|-------|-------|---------------|
| **P1: Sanity Validation** | [2606.10749](https://arxiv.org/abs/2606.10749) | Rejects impossible prices, excessive 24h changes, format errors before data reaches the LLM |
| **P2: State Integrity** | [2606.24322](https://arxiv.org/abs/2606.24322) | SHA256 hash chain on trading state - detects tampering, origin binding |
| **P3: Risk Module** | [2601.04687](https://arxiv.org/abs/2601.04687) | Deterministic circuit breakers: volatility spikes, consecutive losses, position limits |

### Strategy (V4.3, July 2026)

- **Proportional trades:** 15% of portfolio value (EUR3-15 range)
- **Bear regime** (F&G < 30 or BTC < SMA50): 35% cash target, only buy RSI < 15, max 20% per asset
- **Bull regime:** Normal rotation, buy RSI < 35, let winners run
- **Stop-loss:** Native Kraken stop-loss orders, trailing at 3%
- **No new deposits** - the system works with what's there

---

## Live Results

Running on Kraken with a ~EUR60 portfolio, 15-minute intervals:

| Metric | Value |
|--------|-------|
| Portfolio Value | ~EUR60-67 |
| Assets Under Management | 12 coins |
| Trade Size | EUR3-15 (proportional 15%) |
| Trades Executed | 50+ |
| Win Rate | 100% (all profitable exits) |
| Cost Per Run | ~$0.0013 |
| Monthly Cost | ~$0.09 |

*Results as of July 16, 2026. Past performance does not guarantee future results.*

---

## Quick Start

### Prerequisites

- Python 3.10+
- Kraken account with API keys
- DeepSeek API key (or OpenRouter for alternative models)

### 1. Clone and Install

```bash
git clone https://github.com/rafael-tocantins/augustus-trading.git
cd augustus-trading
pip install -r requirements.txt
```

### 2. Configure API Keys

```bash
cp .env.example .env
```

Edit `.env` and add your keys:

```env
# Required: Kraken Exchange
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_SECRET_KEY=your_kraken_secret_key

# Required: LLM Provider (pick one)
DEEPSEEK_API_KEY=your_deepseek_api_key
# OR for OpenRouter:
# OPENROUTER_API_KEY=your_openrouter_key
```

**Kraken API permissions needed:** Query Funds, Query Orders, Create/Modify Orders.

**Never commit `.env` to version control.**

### 3. Run

```bash
# Single analysis run (decides and executes one trade if needed)
python -m augustus.orchestrator --mode trade

# Scan only (no trades)
python -m augustus.orchestrator --mode scan

# With bear market regime override
python -m augustus.orchestrator --regime bear

# Check portfolio balance only
python -m augustus.kraken_client
```

### 4. Automate (Optional)

The **runner** replicates the production scheduling system with 3 layers:

```bash
# Full autonomous mode (every 15 min)
python runner.py --mode full

# Stop-loss watchdog only (every 5 min)
python runner.py --mode stop-loss

# Quick balance check (no LLM, no trades)
python runner.py --mode quick
```

**Recommended crontab setup** (copy from `crontab.example`):

```bash
# Layer 1: Trading (every 15 min — adjust interval as needed)
*/15 * * * * cd ~/augustus-trading && python3 runner.py --mode full >> logs/runner.log 2>&1

# Layer 2: Stop-loss protection (every 5 min)
*/5 * * * * cd ~/augustus-trading && python3 runner.py --mode stop-loss >> logs/stoploss.log 2>&1

# Layer 3: Deep analysis (4x daily at market opens)
0 9,12,15,21 * * * cd ~/augustus-trading && python3 runner.py --mode full >> logs/runner.log 2>&1
```

**To change the monitoring frequency**, edit the first field in your crontab:
- `*/10` = every 10 min (more aggressive)
- `*/30` = every 30 min (more conservative)
- `*/5` = every 5 min (high frequency — beware API rate limits)

See `crontab.example` for the full template with comments.

---

## Configuration

### Choosing Your LLM Model

The system uses two models: a primary (makes decisions) and a validator (optional, reviews decisions). Edit `CONFIG["models"]` in `augustus/orchestrator.py`:

```python
"primary": {
    "provider": "deepseek",          # or "openrouter"
    "model": "deepseek-v4-pro",      # recommended
    "price_input_per_m": 0.435,
    "price_output_per_m": 0.87,
},
```

**Recommended models by budget:**

| Budget | Provider | Model | Monthly Cost |
|--------|----------|-------|-------------|
| Minimal | DeepSeek | deepseek-v4-pro | ~$0.09 |
| Good | OpenRouter | anthropic/claude-sonnet-4 | ~$0.60 |
| Best | OpenRouter | openai/gpt-5-mini | ~$1.20 |

### Thresholds

All trading parameters are in `CONFIG["thresholds"]`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `trade_pct_portfolio` | 0.15 | % of portfolio per trade |
| `max_trade_eur` | 15.0 | Cap per trade in EUR |
| `rsi_oversold` | 35 | RSI threshold to buy |
| `rsi_overbought` | 70 | RSI threshold to sell |
| `bear_cash_target` | 0.35 | Cash target in bear market |

### Adding New Coins

Add your coin to `CONFIG["coin_map"]`:

```python
"coin_map": {
    "AVAX": "avalanche-2",
    "YOUR_COIN": "coin-gecko-id",   # <-- add here
    ...
}
```

The system automatically tracks any coin found in your Kraken balance.

---

## Project Structure

```
augustus-trading/
├── augustus/
│   ├── __init__.py
│   ├── orchestrator.py    # Main trading loop + LLM analysis
│   ├── kraken_client.py   # Kraken API wrapper (balance, orders, ticker)
│   └── stop_loss.py       # Stop-loss manager (native + watchdog)
├── runner.py              # Autonomous scheduler (replaces cron complexity)
├── crontab.example        # Production-grade crontab template
├── results/               # Track record (gitignored)
├── docs/
│   └── ARCHITECTURE.md    # Detailed architecture + paper references
├── .env.example           # API key template
├── requirements.txt       # Python dependencies
├── DISCLAIMER.md          # Legal disclaimer
├── LICENSE                # MIT
└── README.md
```

---

## FAQ

### Why LLM-driven instead of traditional algorithmic trading?

Traditional algos are rigid - they follow fixed rules. LLMs understand context: "RSI is oversold but there's a regulatory announcement pending" or "this pattern looks like a bull trap based on the volume profile." Augustus combines the structure of deterministic risk modules with the flexibility of LLM reasoning.

### Is this safe to run with real money?

**Start with EUR5-10.** The system has multiple safety layers but LLMs can hallucinate and markets are unpredictable. Read [DISCLAIMER.md](DISCLAIMER.md). Test for weeks before increasing.

### Why Kraken?

Lowest EUR trading fees (0.16% taker), reliable API, good EUR pairs, and no KYC issues for small amounts.

### Can I use a different exchange?

The architecture is exchange-agnostic. Swap `kraken_client.py` for your exchange's API wrapper. The orchestrator only depends on `get_balance()`, `get_ticker()`, and `place_order()`.

### Can I run this with a local LLM (Ollama)?

Yes. Set `provider` to `"openrouter"` and point it to your local endpoint, or add a custom provider that hits `http://localhost:11434/v1`.

---

## Papers This Is Built On

| Paper | ID | Applied To |
|-------|----|------------|
| Threat Surfaces in LLM Agents | [2606.10749](https://arxiv.org/abs/2606.10749) | Sanity validation |
| Memory Poisoning Attacks | [2606.24322](https://arxiv.org/abs/2606.24322) | State integrity |
| Byzantine Fault Tolerance | [2605.09076](https://arxiv.org/abs/2605.09076) | Dual-model validation |
| WebCryptoAgent | [2601.04687](https://arxiv.org/abs/2601.04687) | Decoupled risk module |
| Multi-Agent Crypto | [2501.00826](https://arxiv.org/abs/2501.00826) | Hierarchical agent design |

---

## License

MIT. See [LICENSE](LICENSE).

**Built by [Rafael Tocantins](https://rafatocantins.netlify.app).**
