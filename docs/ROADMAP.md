# Augustus Trader — Plano de User Experience

## Objetivo

Transformar o Augustus de um sistema interno num produto que qualquer pessoa com conhecimentos basicos de terminal possa instalar, configurar e gerir.

---

## Fase 1: Instalacao 🎯

### 1A. Via PyPI (recomendado)

```bash
pip install augustus-trader
augustus setup
```

### 1B. Via Git (desenvolvimento)

```bash
git clone https://github.com/rafatocantins/Augustus-trader.git
cd Augustus-trader
pip install -e .
augustus setup
```

### Setup Wizard Interativo

O comando `augustus setup` guia o utilizador:

```
┌──────────────────────────────────────────┐
│        Augustus Trader - Setup           │
├──────────────────────────────────────────┤
│                                          │
│  1. Kraken API Key:   [________]         │
│  2. Kraken Secret:    [________]         │
│  3. LLM Provider:     [DeepSeek ▼]       │
│  4. LLM API Key:      [________]         │
│  5. Modo:             [Paper Trading ▼]  │
│     (Paper = simula, Live = real)        │
│  6. Tamanho trades:   [Conservador ▼]    │
│                                          │
│     [✓] Guardar em ~/.augustus/.env      │
│     [✓] Testar conexao Kraken            │
│     [✓] Testar conexao LLM               │
│                                          │
│           [ INICIAR AUGUSTUS ]           │
└──────────────────────────────────────────┘
```

---

## Fase 2: CLI Completa 💻

```
augustus
├── setup              Configuracao guiada (wizard)
├── run                Executa um ciclo completo (analise + trade)
│   --mode trade       Decide e executa (default)
│   --mode scan        So analisa, nao executa
│   --dry-run          Simula sem enviar ordens
├── watch              Modo continuo (sentinel + stop-loss)
│   --interval 15      Intervalo em minutos (default: 15)
│   --no-trade         So monitoriza, nao executa
├── status             Estado atual do portfolio
│   --json             Output em JSON para scripts
├── backtest           Testa estrategia com dados historicos
│   --days 30          Periodo de backtest
│   --strategy v4      Estrategia a testar
├── config             Ver e editar parametros
│   --list             Lista todos os parametros
│   --set KEY=VALUE    Altera um parametro
│   --reset            Repoe defaults
├── journal            Historico de trades
│   --last 10          Ultimos N trades
│   --stats            Estatisticas agregadas
└── stop               Para todos os processos Augustus
```

---

## Fase 3: Parametros Modificaveis ⚙️

### Parametros de Trading

| Parametro | Default | Range Seguro | Aumentar → | Diminuir → |
|---|---|---|---|---|
| `TRADE_PCT` | 0.15 (15%) | 0.05-0.30 | Trades maiores, mais lucro potencial, mais risco | Trades menores, crescimento lento, mais seguro |
| `MIN_TRADE_EUR` | 3.00 | 1.00-5.00 | Exclui moedas baratas, foco em ativos maiores | Permite micro-trades, maior diversidade |
| `MAX_TRADE_EUR` | 15.00 | 5.00-30.00 | Teto mais alto, concentra mais risco | Teto mais baixo, protege contra overbetting |

### Parametros de Mercado (Bull/Bear)

| Parametro | Default | Range Seguro | Aumentar → | Diminuir → |
|---|---|---|---|---|
| `BEAR_CASH` | 0.35 (35%) | 0.20-0.60 | Mais cash em bear, menos trades, mais seguro | Menos cash, mais agressivo em quedas |
| `BEAR_RSI` | 15 | 10-25 | So compra em crashes extremos, raros trades | Compra em dips moderados, mais trades |
| `BEAR_MAX_POS` | 0.20 (20%) | 0.10-0.40 | Permite concentrar mais num ativo | Forca diversificacao |
| `BULL_RSI_OVERSOLD` | 35 | 25-45 | Compra mais cedo, mais trades, mais risco | Compra mais tarde, menos trades, mais seguro |
| `BULL_RSI_OVERBOUGHT` | 70 | 60-85 | Vende mais tarde, deixa correr mais | Vende mais cedo, realiza lucros mais rapido |

### Parametros de Stop-Loss

| Parametro | Default | Range Seguro | Aumentar → | Diminuir → |
|---|---|---|---|---|
| `STOP_PERCENT` | 5.0 | 3.0-15.0 | Stop mais largo, menos triggers falsos, mais perda maxima | Stop mais apertado, protege melhor, mais whipsaws |
| `TRAILING_PERCENT` | 5.0 | 3.0-15.0 | Trail mais largo, deixa lucros correrem | Trail mais apertado, realiza lucros mais cedo |

### Parametros de Risco

| Parametro | Default | Range Seguro | Aumentar → | Diminuir → |
|---|---|---|---|---|
| `MAX_CONSECUTIVE_LOSSES` | 3 | 2-5 | Tolera mais perdas, recupera de drawdowns | Para mais cedo, mais conservador |
| `MAX_DRAWDOWN_PCT` | 20 | 10-40 | Tolera drawdowns maiores | Protege o capital mais cedo |
| `VOLATILITY_CIRCUIT_BREAKER` | 50 | 30-80 | So para em volatilidade extrema | Para em volatilidade moderada |

---

## Fase 4: Guia de Parametros — O Que Acontece Se...

### "Quero ser mais agressivo"

```bash
augustus config --set TRADE_PCT=0.25       # 25% por trade (mais risco)
augustus config --set BULL_RSI_OVERSOLD=40 # Compra mais cedo
augustus config --set STOP_PERCENT=8       # Stop mais largo (mais risco)
```

**Risco:** Drawdowns maiores, possibilidade de -25% num mes mau.

### "Quero ser mais conservador"

```bash
augustus config --set TRADE_PCT=0.08       # 8% por trade
augustus config --set BEAR_CASH=0.50       # 50% cash em bear
augustus config --set STOP_PERCENT=3       # Stop apertado
```

**Efeito:** Crescimento mais lento, menor volatilidade, protecao maxima.

### "Quero fazer scalping (trades rapidos)"

```bash
augustus config --set BULL_RSI_OVERSOLD=30 # So compra muito oversold
augustus config --set BULL_RSI_OVERBOUGHT=65 # Vende mais cedo
augustus config --set TRAILING_PERCENT=3   # Trail apertado
```

**Efeito:** Mais trades, ganhos menores por trade, mais comissoes.

### "Quero fazer swing trading (semanas)"

```bash
augustus config --set BULL_RSI_OVERBOUGHT=80 # Deixa correr
augustus config --set STOP_PERCENT=10       # Stop largo
augustus config --set TRAILING_PERCENT=10   # Trail largo
```

**Efeito:** Menos trades, ganhos maiores por trade, periodos longos sem acao.

---

## Fase 5: Paper Trading (Simulacao) 🧪

Antes de arriscar dinheiro real, o utilizador pode testar com dados historicos ou em tempo real sem ordens reais.

```bash
# Backtest com dados historicos
augustus backtest --days 90

# Output:
# ┌─────────────────────────────────┐
# │       BACKTEST 90 dias          │
# │  Trades:       18               │
# │  Win rate:     83%              │
# │  P&L:          +12.4%           │
# │  Max drawdown: -8.2%            │
# │  Sharpe:       1.42             │
# └─────────────────────────────────┘

# Simulacao em tempo real (paper trading)
augustus watch --paper --interval 15

# Sempre ativo, usando precos reais, mas sem enviar ordens.
# Regista todos os trades simulados para analise posterior.
```

---

## Fase 6: Monitorizacao e Feedback 📊

### Comandos de Status

```bash
augustus status
# ┌─────────────────────────────────┐
# │      AUGUSTUS TRADER            │
# │  Modo:        Live              │
# │  Regime:      Bull              │
# │  Portfolio:   €XXX              │
# │  Cash:        €XXX              │
# │  Ativos:      10                │
# │  Stops:       10 ativos         │
# │  Ultimo trade: ha 2h            │
# │  Sentinela:   Ativo (3 warn)    │
# └─────────────────────────────────┘

augustus journal --last 5
# ┌──────────────────────────────────────────┐
# │ Data       │ Ação  │ Ativo │ P&L    │    │
# │ 18 Jul 12h │ BUY   │ ATOM  │ —      │    │
# │ 17 Jul 10h │ SELL  │ ALGO  │ +4.2%  │    │
# │ 16 Jul 06h │ SELL  │ ATOM  │ +1.8%  │    │
# └──────────────────────────────────────────┘

augustus journal --stats
# Win rate: 100% | Trades: 50 | Avg P&L: +X%
```

---

## Fase 7: Estrutura de Ficheiros do Utilizador

```
~/.augustus/
├── .env                  # API keys (nunca commitado)
├── config.yaml           # Parametros alterados pelo utilizador
├── trading_state.json    # Estado atual do portfolio
├── journal.json          # Historico de trades
├── backtests/            # Resultados de backtests
├── logs/                 # Logs diarios
└── paper/                # Estado do paper trading
```

---

## Fase 8: Documentacao Necessaria

| Documento | Publico | Conteudo |
|---|---|---|
| `README.md` | Todos | Visao geral, quick start, FAQ |
| `docs/INSTALL.md` | Novos | Guia passo a passo com screenshots |
| `docs/CONFIG.md` | Intermedios | Todos os parametros explicados |
| `docs/STRATEGY.md` | Avancados | Logica da estrategia, paper references |
| `docs/PROOF_OF_CONCEPT.md` | Ceticos | Evidencias, trades reais, TXIDs |
| `docs/SAFETY.md` | Todos | Camadas de seguranca, limites |
| `docs/FAQ.md` | Todos | Perguntas frequentes |
| `docs/CHANGELOG.md` | Todos | Historico de versoes |

---

## Prioridades de Implementacao

| # | Tarefa | Esforco | Impacto |
|---|---|---|---|
| 1 | `setup.py` / `pyproject.toml` para `pip install` | 2h | ⭐⭐⭐⭐⭐ |
| 2 | CLI com Click/Typer (`augustus` command) | 4h | ⭐⭐⭐⭐⭐ |
| 3 | Setup wizard interativo | 3h | ⭐⭐⭐⭐ |
| 4 | `augustus status` e `augustus journal` | 2h | ⭐⭐⭐⭐ |
| 5 | Sistema de configuracao (`augustus config`) | 2h | ⭐⭐⭐⭐ |
| 6 | Paper trading mode | 4h | ⭐⭐⭐ |
| 7 | `augustus backtest` | 6h | ⭐⭐⭐ |
| 8 | `augustus watch` continuo | 3h | ⭐⭐⭐ |
| 9 | Documentacao completa | 4h | ⭐⭐⭐ |
| 10 | Dashboard web simples (FastAPI) | 8h | ⭐⭐ |

---

*Plano para discussao. Sujeito a ajustes com base no feedback.*
