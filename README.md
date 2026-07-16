# 🤖 RL Trader — Sistema Multi-Bot de Trading Automatizado

> **Plataforma de trading algorítmico** que combina Machine Learning (K-Means, LSTM/PPO), Inteligência Artificial (DeepSeek/OpenAI/Gemini) e Análise Técnica clássica para operar autonomamente via MetaTrader 5.

---

## 📋 Índice

- [Visão Geral da Arquitetura](#-visão-geral-da-arquitetura)
- [Scripts de Treinamento](#-scripts-de-treinamento)
  - [market_clustering.py](#1-market_clusteringpy--treinamento-do-k-means)
  - [xauusd_pattern_miner.py](#2-xauusd_pattern_minerpy--minerador-de-padrões-estatísticos)
- [Scripts Auxiliares (Módulos LLM)](#-scripts-auxiliares-módulos-llm)
  - [llm_setup_creator.py](#3-llm_setup_creatorpy--analisador-de-setup-via-llm)
  - [gemini_setup_creator.py](#4-gemini_setup_creatorpy--analisador-de-setup-via-gemini)
- [Bots de Operação](#-bots-de-operação)
  - [mt5_hybrid_bot.py](#5-mt5_hybrid_botpy--bot-híbrido-rsi--breakout--volume)
  - [mt5_rsi_breakout_bot.py](#6-mt5_rsi_breakout_botpy--bot-rsi--breakout-pine-script)
  - [mt5_llm_trader.py](#7-mt5_llm_traderpy--bot-trader-com-ia-llm)
  - [mt5_intraday_cluster_bot.py](#8-mt5_intraday_cluster_botpy--bot-intraday-cluster-k-means)
  - [liv_trader_LSTM_1_2.py](#9-liv_trader_lstm_1_2py--bot-lstm-reinforcement-learning)
- [Orquestrador Central](#-orquestrador-central)
  - [bot_orchestrator.py](#10-bot_orchestratorpy--orquestrador-central)
- [Arquivos de Configuração](#-arquivos-de-configuração)
- [Comandos de Inicialização](#-comandos-de-inicialização)

---

## 🏗 Visão Geral da Arquitetura

```
┌──────────────────────────────────────────────────────────────┐
│                    bot_orchestrator.py                        │
│              (Gerência, Health Check, Telegram)               │
├───────┬───────────┬───────────┬───────────┬──────────────────┤
│Cluster│   LSTM    │  Hybrid   │RSI+Brkout │   LLM Trader     │
│K-Means│  PPO/RL   │RSI+Vol+Brk│Pine Script│  DeepSeek/OpenAI │
├───────┴───────────┴───────────┴───────────┴──────────────────┤
│                     MetaTrader 5 (MT5)                        │
├──────────────────────────────────────────────────────────────┤
│                   Corretora (FBS / etc.)                      │
└──────────────────────────────────────────────────────────────┘
```

Cada bot roda em seu **próprio ambiente virtual Python** com dependências isoladas e se conecta independentemente ao MT5 com contas/magic numbers separados para evitar conflitos de posições.

---

## 🎓 Scripts de Treinamento

### 1. `market_clustering.py` — Treinamento do K-Means

**📁 Localização:** `rl_trader_cluster/Scripts/market_clustering.py`

**Finalidade:** Treina um pipeline de Machine Learning (RobustScaler → PCA → KMeans) para identificar regimes ocultos de mercado a partir de features técnicas calculadas em dados H1.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Feature Engineering** | Utiliza `MarketFeatureEngine` para calcular 40+ indicadores (momentum, volume, estrutura S/R, candlestick patterns, CVD) |
| **Pré-processamento** | `RobustScaler` para normalização resistente a outliers |
| **Redução Dimensional** | `PCA` com N componentes (default: 4) para remover colinearidade |
| **Clustering** | `KMeans` (ou `MiniBatchKMeans` para datasets > 500K amostras) |
| **Perfil de Clusters** | Calcula probabilidades de reversão/breakout e retorno esperado por cluster |

**Artefatos Gerados (salvos em `Modelos/`):**
- `mt5_market_scaler_{SYMBOL}.pkl` — Scaler treinado
- `mt5_market_pca_{SYMBOL}.pkl` — Modelo PCA
- `mt5_market_kmeans_{SYMBOL}.pkl` — Modelo KMeans
- `mt5_features_list_{SYMBOL}.pkl` — Lista de features usadas
- `mt5_cluster_profile_{SYMBOL}.pkl` — DataFrame com perfil probabilístico
- `market_cluster_analysis_output_{SYMBOL}.png` — Gráfico de análise

**Linha de Comando:**
```bash
# Treinamento padrão para XAUUSD (10 clusters, PCA 4 componentes)
python market_clustering.py XAUUSD

# Customizado: 6 clusters, 8 componentes PCA, preset de volume
python market_clustering.py BTCUSD --k 6 --pca-components 8 --preset volume

# EURUSD com preset compacto
python market_clustering.py EURUSD --k 10 --preset compact_10
```

---

### 2. `xauusd_pattern_miner.py` — Minerador de Padrões Estatísticos

**📁 Localização:** `strategy/Scripts/xauusd_pattern_miner.py`

**Finalidade:** Gera um perfil estatístico JSON a partir de dados históricos H1 exportados do MT5. Calcula sazonalidade por hora do dia (probabilidade bullish/bearish, range médio, volume) e métricas de regime global.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Carga de Dados** | Lê CSVs exportados do MT5 (formato `;` com decimal `,`) desde 2020 |
| **Sazonalidade** | Agrupa por hora (0-23h EET) calculando: média/mediana/desvio do range, % bullish, % bearish, volume médio |
| **Regime Global** | Calcula ATR médio global, threshold de alta volatilidade (percentil 90) e baixa volatilidade (percentil 10) |
| **Saída** | JSON estruturado com `metadata`, `regime_context`, `time_of_day_seasonality` e `trading_instructions` |

**Artefatos Gerados:**
- `{symbol}_statistical_profile.json` — Perfil estatístico para uso pelo LLM Trader

**Linha de Comando:**
```bash
# Gerar perfil do XAUUSD
python xauusd_pattern_miner.py --data_dir "H:\Investimentos\rl_trader\Historico_Dataset\XAUUSD"

# Gerar perfil de outro ativo
python xauusd_pattern_miner.py --symbol EURUSD --data_dir "H:\Investimentos\rl_trader\Historico_Dataset\EURUSD"

# Customizar output
python xauusd_pattern_miner.py --symbol BTCUSD --data_dir "C:\dados\BTCUSD" --out btcusd_profile.json
```

---

## 🔧 Scripts Auxiliares (Módulos LLM)

### 3. `llm_setup_creator.py` — Analisador de Setup via LLM

**📁 Localização:** `strategy/Scripts/llm_setup_creator.py`

**Finalidade:** Módulo que envia contexto técnico + perfil estatístico para uma LLM (DeepSeek ou OpenAI) e recebe de volta um setup de trading estruturado em JSON (direção, gatilho, stop loss). Usado como **módulo importado** pelo `mt5_llm_trader.py`.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Provedores** | DeepSeek (`deepseek-chat`) e OpenAI (`gpt-4o`) |
| **Schema** | `TradingSetup` (Pydantic): ativo, direção, racional, probabilidade, gatilho, stop_loss |
| **Prompt** | Combina indicadores técnicos (EMA200, ADX, DI, ATR, Volume) com dados históricos da hora atual |
| **Regras** | A IA DEVE respeitar tendência EMA200/ADX, nunca operar contra-tendência, e manter SL ≥ 1.5x ATR |
| **Output** | JSON estruturado via `response_format` (OpenAI) ou schema injection (DeepSeek) |

**Linha de Comando (standalone):**
```bash
# Teste standalone com DeepSeek
python llm_setup_creator.py --provider deepseek --profile xauusd_statistical_profile.json --hour 14 --price 2650.5

# Teste com OpenAI
python llm_setup_creator.py --provider openai --api_key sk-xxx --profile eurusd_statistical_profile.json --hour 10 --price 1.0850
```

---

### 4. `gemini_setup_creator.py` — Analisador de Setup via Gemini

**📁 Localização:** `strategy/Scripts/gemini_setup_creator.py`

**Finalidade:** Versão alternativa do analisador de setup usando Google Gemini. Envia perfil estatístico e recebe setup estruturado via `response_schema`.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Modelo** | Gemini 3.1 Pro Preview |
| **Schema** | Mesmo `TradingSetup` (Pydantic) |
| **Saída** | JSON estruturado via `response_mime_type="application/json"` |
| **Temperatura** | 0.1 (precisão analítica) |

**Linha de Comando:**
```bash
python gemini_setup_creator.py --api_key YOUR_GEMINI_KEY --profile xauusd_statistical_profile.json --hour 8 --price 2600.0
```

---

## 📈 Bots de Operação

### 5. `mt5_hybrid_bot.py` — Bot Híbrido: RSI + Breakout + Volume

**📁 Localização:** `strategy/Scripts/mt5_hybrid_bot.py`  
**Magic Number:** `888222`  
**Log CSV:** `bot_hybrid_log.csv`

**Finalidade:** Estratégia traduzida do Pine Script (TradingView) com melhorias: filtro de volume em todas as entradas, Stop Loss ATR obrigatório e Trailing Stop dinâmico.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Indicadores** | EMA(200), RSI(14), ADX(14), ATR(14), Volume SMA(20) — todos calculados com NumPy puro |
| **Regime Ranging** | ADX < 25 → RSI Mean-Reversion (Buy: RSI < 40 + preço acima EMA; Sell: RSI > 60 + preço abaixo EMA) |
| **Regime Trending** | ADX > 25 → Breakout (rompimento de máxima/mínima de 20 períodos) |
| **Filtro Volume** | Ranging: volume ratio < 0.9x (exaustão); Trending: volume ratio > 1.3x (confirmação) |
| **Stop Loss** | RSI: 1.5x ATR; Breakout: 2.0x ATR |
| **Take Profit** | RSI: 2:1 R:R; Breakout: sem TP fixo (trailing stop) |
| **Trailing Stop** | Breakout: ATR × 2.0 baseado na máxima/mínima da vela atual |
| **Saída RSI** | Fecha quando RSI cruza 50 (centro) |
| **Timeframe** | H1 (análise e sinais a cada hora cheia) |
| **Gestão** | Logs e trailing stop atualizados a cada 1 minuto |

**Linha de Comando:**
```bash
# Modo loop contínuo (produção)
python mt5_hybrid_bot.py --symbols XAUUSD,XAGUSD,EURUSD --exchange demo --lot 0.1 --loop

# One-shot (teste manual)
python mt5_hybrid_bot.py --symbols XAUUSD --exchange real --lot 0.05
```

---

### 6. `mt5_rsi_breakout_bot.py` — Bot RSI + Breakout (Pine Script)

**📁 Localização:** `strategy/Scripts/mt5_rsi_breakout_bot.py`  
**Magic Number:** `888333`  
**Log CSV:** `bot_rsi_breakout_log.csv`

**Finalidade:** Tradução fiel do Pine Script "Hybrid: RSI + Breakout + Dashboard" (© RugSurvivor). Sem filtro de volume (diferente do hybrid_bot), com trailing candle adicional.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Indicadores** | EMA(200), RSI(14), ADX(14, threshold=20), ATR(14) — NumPy puro |
| **Regime** | Mesma lógica (ADX > 20 trending vs ranging) porém **sem** filtro de volume |
| **Trailing ATR** | Breakout: 2.0x ATR baseado na máxima/mínima |
| **Trailing Candle** | Após ≥ 1% de lucro (preço ou saldo), move SL para a extremidade da vela anterior (mínima para long, máxima para short) |
| **Saída RSI** | Fecha posição RSI quando RSI cruza 50 |

**Diferenças do Hybrid Bot:**
- Sem filtro de volume (mais agressivo)
- Trailing candle em AMBOS os tipos (RSI e Breakout) após 1% lucro
- Threshold ADX menor (20 vs 25)

**Linha de Comando:**
```bash
# Modo loop contínuo
python mt5_rsi_breakout_bot.py --symbols XAUUSD,XAGUSD,EURUSD --exchange demo --lot 0.1 --loop

# One-shot
python mt5_rsi_breakout_bot.py --symbols XAUUSD --exchange real --lot 0.05
```

---

### 7. `mt5_llm_trader.py` — Bot Trader com IA (LLM)

**📁 Localização:** `strategy/Scripts/mt5_llm_trader.py`  
**Magic Number:** `999111`  
**Log CSV:** `bot_trades_log.csv`

**Finalidade:** Bot que utiliza uma LLM (DeepSeek ou OpenAI) como "cérebro" para decidir direção, combinando indicadores técnicos calculados localmente com perfis estatísticos históricos.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Indicadores Locais** | EMA(200), RSI(14), ADX(14), ATR(14), Volume Ratio — réplica MT5 |
| **Gate Técnico** | Bloqueia LONGs quando preço abaixo EMA200 + tendência Bear; bloqueia SHORTs quando acima + Bull |
| **Gate Volume** | Volume ratio < 0.3x → pula o ativo (mercado morto) |
| **Módulo LLM** | Importa `llm_setup_creator.py` → envia contexto → recebe JSON |
| **Perfil Estatístico** | Carrega `{symbol}_statistical_profile.json` com sazonalidade e regime global |
| **Execução** | Valida desvio preço-gatilho, aplica ATR Guard (SL mín 1.5x ATR), TP fixo 2:1 R:R |
| **Trailing Stop H1** | Após ≥ 1% lucro (preço ou saldo), move SL para extremidade da vela anterior |
| **Auto-install** | Instala automaticamente `pydantic`, `openai`, `python-dotenv` se ausentes |

**Fluxo:**
1. Calcula indicadores técnicos locais (EMA200, ADX, RSI, ATR, Volume)
2. Aplica gate técnico (bloqueia contra-tendência)
3. Envia contexto completo para a LLM
4. LLM retorna: direção, gatilho, stop_loss
5. Gate final: se LLM sugere direção bloqueada → ignora
6. Executa ordem no MT5 com ATR Guard

**Linha de Comando:**
```bash
# Modo loop contínuo com DeepSeek (padrão)
python mt5_llm_trader.py --symbols XAUUSD,XAGUSD,EURUSD --exchange demo --lot 0.1 --loop

# Com OpenAI
python mt5_llm_trader.py --symbols XAUUSD --provider openai --exchange real --lot 0.05 --loop

# Com API key explícita
python mt5_llm_trader.py --symbols EURUSD --provider deepseek --api_key sk-xxx --exchange demo --lot 0.1 --loop
```

---

### 8. `mt5_intraday_cluster_bot.py` — Bot Intraday Cluster (K-Means)

**📁 Localização:** `rl_trader_cluster/Scripts/mt5_intraday_cluster_bot.py`  
**Magic Number:** `234000`  
**Log CSV:** `bot_cluster_log.csv`

**Finalidade:** Bot baseado em Machine Learning que classifica o regime de mercado em tempo real usando um modelo KMeans pré-treinado e consulta uma LLM (DeepSeek) como revisora técnica antes de executar.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Pipeline ML** | `Scaler → PCA → KMeans` carregados de arquivos `.pkl` |
| **Feature Engine** | `MarketFeatureEngine` calcula 40+ features em tempo real a partir de 500 barras H1 |
| **Decisão Base** | Perfil probabilístico do cluster: P(reversão), P(breakout), E[R30] |
| **Estratégias** | `--strategy all/reversal/breakout` — filtra qual tipo de operação é permitida |
| **Camada IA** | DeepSeek como "Revisor Técnico" analisa features, cluster e último resultado → `CONFIRMA/CONTRADIZ/NEUTRO` |
| **Proteção** | Se ambas probabilidades > 50% (Black Swan) → fecha todas as posições |
| **S/R** | Suporte/Resistência estruturais calculados pelo feature_engine (rolling 20 velas) |
| **SL/TP** | Baseados em S/R estrutural com fallback de distância fixa; RR ratio: 2.5:1 |
| **Telegram** | Comandos: `/status`, `/close`, `/exit` |
| **Memória** | Inclui resultado do último trade no prompt da IA para adaptação dinâmica |

**Linha de Comando:**
```bash
# Modo padrão (todas as estratégias)
python mt5_intraday_cluster_bot.py XAUUSD --strategy all

# Apenas reversões, sem Telegram
python mt5_intraday_cluster_bot.py XAUUSD --strategy reversal --no-telegram

# Breakout para EURUSD
python mt5_intraday_cluster_bot.py EURUSD --strategy breakout
```

---

### 9. `liv_trader_LSTM_1_2.py` — Bot LSTM (Reinforcement Learning)

**📁 Localização:** `rl_trader/liv_trader_LSTM_1_2.py` (launcher)  
**📁 Classe Principal:** `rl_trader_exchange/ai_trader_LSTM_1_2.py`  
**Magic Number:** `234000`  
**Log CSV:** `bot_exchange_log.csv`

**Finalidade:** Bot baseado em Reinforcement Learning (PPO/Stable-Baselines3) que usa um modelo LSTM pré-treinado para tomar decisões de trading em dados multi-timeframe (M1 → D1 → H1 → W1).

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Modelo RL** | PPO (Stable-Baselines3) com LSTM, carregado de arquivo `.zip` |
| **Dados** | Dukascopy Python para dados M1 incrementais; MT5 para execução |
| **Features (29)** | Distância de MAs (200/50/20), ADX/DMI, momentum (5/10/20), MACD, RSI, candlestick patterns (hammer, engulfing, shooting star), CVD delta, volume delta, suporte/resistência, volatilidade, posição atual e duração |
| **Multi-TF** | M1 → resample para D1, H1, W1 com merge_asof para enriquecer features |
| **SL/TP** | Baseados em suporte/resistência estrutural (rolling 20 barras) com RR ratio 2.5:1 |
| **Volume Profile** | Calcula POC, VAL, VAH em tempo real a partir de dados M1 |
| **Telegram** | Listener de comandos: `/status`, `/close`, `/exit` |
| **Auto-resolução** | Detecta automaticamente o modelo mais recente para o symbol na pasta `Modelos/` |

**Linha de Comando:**
```bash
# Auto-detecta modelo mais recente do XAUUSD
python liv_trader_LSTM_1_2.py XAUUSD

# Especifica modelo (por substring)
python liv_trader_LSTM_1_2.py XAUUSD --model BEST

# Especifica modelo e histórico completos
python liv_trader_LSTM_1_2.py XAUUSD --model "Modelos/ppo_xauusd_best.zip" --history "M1_history_XAUUSD.csv"

# Sem polling de Telegram
python liv_trader_LSTM_1_2.py XAUUSD --model BEST --no-telegram
```

---

## 🎛 Orquestrador Central

### 10. `bot_orchestrator.py` — Orquestrador Central

**📁 Localização:** `strategy/Scripts/bot_orchestrator.py`

**Finalidade:** Gerencia os 5 bots como subprocessos independentes. Monitora saúde, reinicia automaticamente bots que caíram, envia alertas em tempo real via Telegram e gera relatórios diários consolidados.

**Implementação:**

| Componente | Detalhe |
|---|---|
| **Subprocessos** | Cada bot roda em seu próprio `subprocess.Popen` com ambiente virtual isolado |
| **Health Check** | A cada 30 segundos verifica se todos os processos estão vivos |
| **Auto-Restart** | Até 5 reinícios automáticos com cooldown de 60 segundos |
| **Monitoramento CSV** | Lê os CSVs de log dos bots e envia alertas de trades para o Telegram |
| **Relatório Diário** | Às 21h envia consolidado: trades, win rate, P/L por bot e total |
| **Weekend Standby** | Sexta 20h → para todos os bots; Segunda 9h → inicia todos automaticamente |
| **Telegram** | Comandos: `/status`, `/bots`, `/report`, `/restart`, `/stop`, `/start`, `/logs`, `/stop_all`, `/help` |
| **Log** | `orchestrator_log.csv` com eventos: START, STOP, CRASH, RESTART, TRADE, DAILY_REPORT |

**Comandos Telegram:**

| Comando | Descrição |
|---|---|
| `/help` | Lista todos os comandos |
| `/status` | Status geral de todos os bots |
| `/status [nome]` | Status detalhado de um bot específico |
| `/bots` | Lista todos os bots registrados |
| `/report` | Relatório consolidado do dia |
| `/restart [nome]` | Reinicia um bot |
| `/stop [nome]` | Para um bot |
| `/start [nome]` | Inicia um bot parado |
| `/logs [nome]` | Últimas 15 linhas de saída do bot |
| `/stop_all` | Encerra o orquestrador inteiro |

**Linha de Comando:**
```bash
# Inicia todos os bots habilitados no bots_config.json
python bot_orchestrator.py

# Inicia apenas bots específicos
python bot_orchestrator.py --bots cluster_xagusd llm_trader

# Dry run (mostra o que faria sem iniciar)
python bot_orchestrator.py --dry-run

# Config personalizado
python bot_orchestrator.py --config meu_config.json
```

---

## 📂 Arquivos de Configuração

### `bots_config.json`

Define os 5 bots gerenciados pelo orquestrador:

| ID | Nome | Script | Ambiente |
|---|---|---|---|
| `cluster_xagusd` | Cluster XAGUSD | `mt5_intraday_cluster_bot.py` | `rl_trader_cluster/Scripts/` |
| `lstm_xauusd` | LSTM XAUUSD | `liv_trader_LSTM_1_2.py` | `rl_trader_exchange/` |
| `llm_trader` | LLM Trader | `mt5_llm_trader.py` | `strategy/Scripts/` |
| `hybrid_bot` | Hybrid RSI+Breakout+Vol | `mt5_hybrid_bot.py` | `strategy/Scripts/` |
| `rsi_breakout` | RSI+Breakout (Pine) | `mt5_rsi_breakout_bot.py` | `strategy/Scripts/` |

### `telegram.json`

Configuração do bot Telegram para notificações (token + chat_id).

### `.env`

Chaves de API para os provedores LLM:
```env
DEEPSEEK_API_KEY=sk-xxxx
OPENAI_API_KEY=sk-xxxx
```

### `{symbol}_statistical_profile.json`

Perfis estatísticos gerados pelo `xauusd_pattern_miner.py` para cada ativo (sazonalidade por hora, regime global).

---

## 🚀 Comandos de Inicialização — Referência Rápida

### ⚡ Produção (via Orquestrador — recomendado)

```bash
# Inicia TODOS os 5 bots em modo automático
python bot_orchestrator.py
```

### 🤖 Bots Individuais

```bash
# 1. Cluster Bot (K-Means + DeepSeek Reviewer)
python mt5_intraday_cluster_bot.py XAUUSD --strategy reversal --no-telegram

# 2. LSTM Bot (Reinforcement Learning PPO)
python liv_trader_LSTM_1_2.py XAUUSD --model BEST --no-telegram

# 3. LLM Trader (DeepSeek/OpenAI + Análise Técnica)
python mt5_llm_trader.py --symbols XAUUSD --exchange demo --lot 0.1 --loop

# 4. Hybrid Bot (RSI + Breakout + Volume Filter)
python mt5_hybrid_bot.py --symbols XAUUSD --exchange demo --lot 0.1 --loop

# 5. RSI+Breakout Bot (Pine Script RugSurvivor)
python mt5_rsi_breakout_bot.py --symbols XAUUSD --exchange demo --lot 0.1 --loop
```

### 🎓 Treinamento / Preparação

```bash
# Treinar modelo K-Means para XAUUSD
python market_clustering.py XAUUSD --k 10 --pca-components 4 --preset momentum

# Gerar perfil estatístico para LLM Trader
python xauusd_pattern_miner.py --symbol XAUUSD --data_dir "H:\Investimentos\rl_trader\Historico_Dataset\XAUUSD"

# Teste standalone de análise LLM
python llm_setup_creator.py --provider deepseek --profile xauusd_statistical_profile.json --hour 14 --price 2650.5
```

---

## ⚠️ Pré-requisitos

1. **MetaTrader 5** instalado e logado
2. **Algo Trading** habilitado no MT5 (botão no topo do terminal)
3. **acesso.json** com credenciais das contas MT5
4. **telegram.json** configurado com token do bot e chat_id
5. **`.env`** com chaves de API (DeepSeek/OpenAI) para bots com IA
6. **Modelos treinados** (`.pkl` para Cluster, `.zip` para LSTM) nas pastas `Modelos/`
7. **Perfis estatísticos** (`.json`) gerados pelo pattern miner para o LLM Trader
