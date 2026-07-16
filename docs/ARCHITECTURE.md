# Architecture Deep Dive

## Data Flow

```
1. Kraken API ──> get_portfolio()  ──> balances dict
2. CoinGecko  ──> get_market_data() ──> prices + 24h/7d changes
3. CoinGecko  ──> get_rsi()         ──> RSI-14 per coin
4. All data   ──> build_portfolio_context() ──> structured prompt context
5. Prompt     ──> analyse_market()  ──> LLM returns JSON trade decision
6. JSON       ──> execute_trade()   ──> Kraken order placed
7. Order ID   ──> stop_loss.py      ──> Stop-loss registered
```

## Sanity Validation (P1)

Based on [2606.10749] Threat Surfaces in LLM Agents.

Before any data reaches the LLM prompt:

1. **BTC absolute bounds:** Price must be EUR10k-500k
2. **24h change bounds:** BTC < 50%, alts < 80%
3. **Format validation:** No None/negative prices
4. **Source tracking:** CoinGecko returns percentages (0.81 = 0.81%)

## State Integrity (P2)

Based on [2606.24322] Memory Poisoning Defenses.

- Every state write produces a SHA256 hash stored separately
- State reads verify hash before trusting data
- Origin binding: `_origin` field tracks which component wrote the state

## Strategy Decision Tree

```
Is BTC > SMA50?
├── YES (Bull Market)
│   ├── Cash < EUR3? ──> SELL asset with biggest 24h gain
│   └── Cash >= EUR3? ──> BUY asset with lowest RSI (RSI < 35)
└── NO (Bear Market)
    ├── Cash < 15% portfolio? ──> SELL most overvalued asset
    └── Cash >= 15%? ──> BUY asset with RSI < 15
```

## Cost Model

Per-run token costs (DeepSeek V4 Pro):

| Component | Tokens In | Tokens Out | Cost |
|-----------|-----------|------------|------|
| Portfolio context | ~300 | - | - |
| LLM Analysis | ~800 | ~200 | ~$0.0005 |
| Total per run | | | ~$0.0013 |

At 15-minute intervals: ~96 runs/day = ~$0.125/day = ~$3.75/month theoretical max.
Actual usage with [SILENT] responses: ~$0.09/month.

## Adding a Custom Model Provider

Add a provider entry in `load_api_key()`:

```python
"my_provider": ("MY_PROVIDER_API_KEY", "https://api.my-provider.com/v1"),
```

Then configure the model:

```python
"primary": {
    "provider": "my_provider",
    "model": "my-model-name",
    "price_input_per_m": 0.50,
    "price_output_per_m": 1.00,
},
```
