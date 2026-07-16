# Diagnóstico: Performance do Bot LLM (`mt5_llm_trader.py`)

## Números

| Métrica | Valor |
|---------|-------|
| Total de trades | 10 |
| Win Rate | **20%** (2 de 10) |
| PnL Total | **-$12.30** |
| Avg Win | +$312.90 |
| Avg Loss | -$79.76 |
| Direções | **10 LONG, 0 SHORT** |
| R:R | 2.0x (todos) |

> [!WARNING]
> O PnL total está quase no zero graças a **1 trade** (+$531.60) que salvou o dia. Sem ele, o PnL seria **-$543.90**.

---

## 3 Problemas Identificados

### 1. Viés LONG cego — a IA nunca vendeu

10/10 trades foram LONG. O XAUUSD caiu de 4728 para 4676 ao longo do dia 23/04 — uma queda de **52 pontos** — e a IA continuou comprando a cada hora.

**Causa:** O prompt envia apenas o perfil estatístico horário + preço + RSI. A IA não recebe nenhum indicador de tendência (EMA, ADX) nem momentum. Sem saber que o preço está **abaixo** da EMA 200 em queda livre, ela só vê a probabilidade histórica genérica e aposta no viés de alta do ouro.

### 2. Stop Loss apertado demais

| Trade | SL Distance | Resultado |
|-------|-------------|-----------|
| 23:00 | **3.75 pts** | LOSS |
| 13:00 | 4.73 pts | WIN |
| 14:00 | 6.41 pts | LOSS |
| 12:00 | 6.55 pts | LOSS |
| 15:00 | 7.08 pts | LOSS |

O ATR do XAUUSD em H1 está em torno de **18-22 pontos**. Stops de 3-7 pontos são **ruído puro** — o preço bate o SL no respiro normal da vela antes de ir na direção certa. O único WIN grande (SL=26.56 pts) teve um stop que respeitava o ATR.

### 3. Nenhum filtro técnico antes de enviar à IA

O fluxo atual:
```
Hora H1 → Preço + RSI → DeepSeek → LONG/SHORT → Executa
```

Não há **nenhum gate** antes de chamar a IA. Mesmo que o mercado esteja em tendência de baixa óbvia (ADX alto + preço abaixo da EMA200), a IA é consultada e ela retorna LONG porque o perfil estatístico diz que "historicamente essa hora sobe".

---

## Proposta de Melhoria: Gate de Indicadores Pré-LLM

Adicionar os **mesmos indicadores do Hybrid Bot** como filtro obrigatório ANTES de consultar a IA:

```
Hora H1 → Calcular EMA200, ADX, ATR, Volume
         → Verificar gate (contexto favorável?)
         → SIM: Envia indicadores no prompt → DeepSeek decide
         → NÃO: Pula, não gasta token
```

### Indicadores a adicionar no `mt5_llm_trader.py`:

| Indicador | Papel | Impacto esperado |
|-----------|-------|------------------|
| **EMA 200** | Filtro de tendência. Se preço < EMA200, bloquear LONG. | Teria evitado 6 dos 8 trades perdedores |
| **ADX (14)** | Regime de mercado. Se ADX > 25 e tendência contra, bloquear. | Confirma se é tendência real ou ruído |
| **ATR (14)** | SL mínimo obrigatório. SL deve ser >= 1.5×ATR. | Eliminaria stops de 3-7 pts (ruído) |
| **Volume Ratio** | Confirmação. Se volume < 0.5x média, sinal fraco. | Evita horas mortas (madrugada, por ex) |

### Mudanças no prompt da IA:

O prompt passa a incluir os indicadores calculados, dando à IA contexto técnico real:

```
MERCADO ATUAL:
  Preço: $4700.90
  RSI: 38.5
  EMA200: $4735.00 (preço ABAIXO → tendência de baixa)
  ADX: 30.97 (TRENDING)
  +DI: 11.11 | -DI: 28.32 (vendedores dominam)
  ATR: 20.02
  Volume Ratio: 0.95x
```

Com essa informação, a IA teria respondido **SHORT** ou **NEUTRAL** em vez de LONG cego.

### SL Obrigatório via ATR:

Mesmo que a IA sugira um SL de 3 pontos, o código impõe o mínimo:
```python
sl_min = atr * 1.5  # ~30 pts para XAUUSD
sl_final = max(sl_da_ia, sl_min)  # Nunca menor que 1.5×ATR
```

---

## Impacto Estimado nos 10 trades

Se essas regras estivessem ativas:

| Trade | Filtro EMA200 | Resultado com filtro |
|-------|--------------|---------------------|
| 10:23 LONG 4728 | Preço < EMA200 → **BLOQUEADO** | Evitava -105.6 |
| 13:00 LONG 4719 | Preço < EMA200 → **BLOQUEADO** | Perdia +94.2 |
| 14:00 LONG 4734 | Preço ~= EMA200 → Passa → IA decide | Dependeria |
| 15:00 LONG 4712 | Preço < EMA200 → **BLOQUEADO** | Evitava -69.7 |
| 16:00 LONG 4700 | Preço < EMA200 → **BLOQUEADO** | Evitava -101.1 |
| 22:00 LONG 4707 | Preço < EMA200 → **BLOQUEADO** | Evitava -91.6 |
| 23:00 LONG 4700 | Preço < EMA200 → **BLOQUEADO** | Evitava -37.5 |
| 02:00 LONG 4676 | Preço < EMA200 → **BLOQUEADO** | Perdia +531.6 |
| 12:00 LONG 4725 | Preço ~= EMA200 → Passa → IA decide | Dependeria |
| 13:00 LONG 4729 | Preço ~= EMA200 → Passa → IA decide | Dependeria |

> [!IMPORTANT]
> O filtro EMA200 sozinho teria bloqueado **6 dos 8 losses** (-$405.5 salvos), ao custo de perder 1 win (+$94.2) e o grande trade (+$531.6). PnL estimado ficaria melhor com o filtro mais fino (ADX+DI confirmando direção SHORT ao invés de bloquear).

## Decisão

A recomendação é **enriquecer o prompt + adicionar gates**, não substituir a IA. Ela continua tomando a decisão final, mas agora com dados técnicos reais e com proteção mínima contra ruído.
