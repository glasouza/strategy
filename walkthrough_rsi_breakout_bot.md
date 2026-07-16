# Walkthrough: MT5 RSI + Breakout Bot

Este documento descreve a arquitetura, as regras de negócio e o guia de uso para o **`mt5_rsi_breakout_bot.py`**. Este bot é uma tradução direta do Pine Script "Hybrid: RSI + Breakout + Dashboard" do TradingView (© RugSurvivor), focado em operar regimes de tendência e lateralização.

---

## 1. Visão Geral Estratégica

O bot opera identificando o **regime de mercado** através do indicador ADX e aplica a estratégia mais apropriada para aquele momento:

1. **Regime de Lateralização (RANGE)**: Utiliza uma estratégia de **RSI Mean-Reversion** (Reversão à Média). Compra quando sobrevendido e vende quando sobrecomprado, desde que a favor da tendência principal.
2. **Regime de Tendência (TREND)**: Utiliza uma estratégia de **Breakout** (Rompimento). Entra a favor da tendência quando o preço rompe as máximas ou mínimas recentes.

> **Nota**: Ao contrário do `mt5_hybrid_bot.py`, este script **não utiliza filtros de volume**, mantendo-se 100% fiel ao código Pine original.

---

## 2. Indicadores e Filtros (Gatekeepers)

O bot calcula os seguintes indicadores a cada hora (H1) ou sob demanda:

* **ADX (Average Directional Index) [14 períodos]**: Define o regime.
  * `ADX > 20`: Mercado em Tendência (TREND).
  * `ADX <= 20`: Mercado Lateral (RANGE).
* **EMA 200 (Exponential Moving Average)**: Define o viés (bias) macro.
  * Preço > EMA200: Viés de Alta (Bullish).
  * Preço < EMA200: Viés de Baixa (Bearish).
* **RSI (Relative Strength Index) [14 períodos]**: Gatilho para entradas em RANGE.
* **Breakout Lookback [20 períodos]**: Gatilho para entradas em TREND (Máxima/Mínima das últimas 20 barras).
* **ATR (Average True Range) [14 períodos]**: Utilizado para dimensionamento do Stop Loss (SL) e Trailing Stop.

---

## 3. Regras de Entrada e Saída

### Cenário A: Mercado Lateral (ADX <= 20) -> Reversão com RSI
* **LONG (Compra)**: RSI < 40 **E** Preço > EMA 200.
* **SHORT (Venda)**: RSI > 60 **E** Preço < EMA 200.
* **Saída Padrão (Exit)**: Quando o RSI cruza a linha de 50 (retorno à média).
* **Stop Loss Base**: 1.5x o valor do ATR atual.

### Cenário B: Mercado em Tendência (ADX > 20) -> Rompimento (Breakout)
* **LONG (Compra)**: Preço rompe a MÁXIMA dos últimos 20 candles **E** Preço > EMA 200.
* **SHORT (Venda)**: Preço rompe a MÍNIMA dos últimos 20 candles **E** Preço < EMA 200.
* **Saída Padrão (Exit)**: N/A. Depende inteiramente do Trailing Stop.
* **Stop Loss Base**: 2.0x o valor do ATR atual.

---

## 4. Proteções Avançadas de Trailing Stop

Para garantir a proteção do capital, o bot conta com dois sistemas de Trailing Stop:

1. **ATR Trailing (Exclusivo Breakout)**:
   * Assim como no Pine, o Stop Loss persegue o preço a uma distância de `2.0 * ATR`.
2. **Candle Trailing (Híbrido)**:
   * Uma **melhoria** em relação ao Pine original.
   * Quando qualquer trade (RSI ou Breakout) atinge um **lucro de 1%** (seja no movimento de preço ou relativo ao saldo da conta), o bot aciona o Candle Trailing.
   * O Stop Loss é movido para a **Mínima da vela H1 anterior** (para Longs) ou **Máxima da vela H1 anterior** (para Shorts).

---

## 5. Sistema de Auditoria (Logs)

O bot gera um arquivo de log local chamado `bot_rsi_breakout_log.csv`. 

**Formato das Colunas:**
`Ticket | DataHora | Ativo | Tipo | Direcao | Entrada | SL | TP | Regime | Status | Lucro`

**Como funciona:**
1. Ao abrir uma ordem, ela é salva com `Status = ABERTO` e `Lucro = 0.0`.
2. Em modo `--loop`, o bot verifica a cada 1 minuto se alguma ordem pendente foi encerrada pela corretora (por atingir SL, TP ou Trailing Stop).
3. Se encerrada, ele atualiza o arquivo alterando para `Status = FECHADO` e preenche o lucro ou prejuízo real obtido.

---

## 6. Como Rodar (CLI - Command Line Interface)

Abra o terminal PowerShell (no ambiente ativado) ou bash e utilize os seguintes comandos dentro da pasta `strategy/Scripts`.

### Parâmetros Disponíveis:
* `--symbols`: Lista de ativos separados por vírgula (ex: `XAUUSD,EURUSD,BTCUSD`). Padrão: `XAUUSD,XAGUSD,EURUSD`.
* `--exchange`: Qual conta do `acesso.json` usar. Opções: `demo` ou `real`. Padrão: `demo`.
* `--lot`: Tamanho do lote da operação (ex: `0.01` ou `0.1`). Padrão: `0.1`.
* `--loop`: Flag booleana. Se presente, o bot roda de forma contínua em background. Se omitida, ele avalia o mercado 1 vez e desliga.

### Exemplo 1: Modo One-Shot (Análise Rápida)
Roda apenas uma vez, avalia os sinais do momento atual e desliga. Útil para rodar via agendador de tarefas do Windows.
```bash
python mt5_rsi_breakout_bot.py --symbols XAUUSD,USDJPY --exchange demo --lot 0.05
```

### Exemplo 2: Modo Contínuo (Servidor)
Roda em loop. A avaliação de compra/venda é feita na virada de cada hora H1. O gerenciamento de risco (trailing stops e verificação de saídas) é feito a cada 1 minuto.
```bash
python mt5_rsi_breakout_bot.py --symbols XAUUSD,EURUSD --exchange real --lot 0.1 --loop
```

### Dicas de Ouro ⚠️
1. **AutoTrading**: Certifique-se de que o botão vermelho/verde `Algo Trading` (ou `AutoTrading`) no topo do seu terminal MetaTrader 5 está **ATIVADO** (Verde). Se não estiver, o bot apresentará o erro de retcode `10027`.
2. **Ativos Visíveis**: Os ativos solicitados no comando `--symbols` precisam estar visíveis na janela "Market Watch" (Observação do Mercado) no MetaTrader.
3. **Credenciais**: O arquivo `acesso.json` deve estar na mesma pasta do script, contendo os logins da corretora.

Foi implementado tralling stop e controle de no maximo 2 trades simultaneos
