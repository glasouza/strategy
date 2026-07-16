# 🎛️ Bot Orchestrator – Guia de Operação

## Visão Geral

O **Bot Orchestrator** é o ponto central de controle para os 5 bots de trading.
Ele roda como um processo principal que:
- Inicia cada bot como subprocesso independente
- Monitora saúde (verifica se o processo está vivo a cada 30s)
- Reinicia automaticamente bots que caíram (até 5 tentativas)
- Escuta comandos via Telegram para controle remoto
- Envia relatório diário consolidado às 21h

## Arquitetura

```
bot_orchestrator.py  (processo principal)
  │
  ├── Subprocesso: mt5_intraday_cluster_bot.py  (venv cluster)
  ├── Subprocesso: liv_trader_LSTM_1_2.py       (venv exchange)
  ├── Subprocesso: mt5_llm_trader.py            (venv strategy)
  ├── Subprocesso: mt5_hybrid_bot.py            (venv strategy)
  └── Subprocesso: mt5_rsi_breakout_bot.py      (venv strategy)
```

Cada subprocesso usa seu próprio `python.exe` (venv) e roda no seu próprio diretório.

## Arquivos

| Arquivo | Descrição |
|---|---|
| `bot_orchestrator.py` | Script principal do orquestrador |
| `bots_config.json` | Configuração dos bots (params, paths, args) |
| `orchestrator_log.csv` | Log de eventos (starts, crashes, restarts) |
| `telegram_utils.py` | Classe centralizada de Telegram (na raiz) |

## Como Rodar

### 1. Iniciar todos os bots
```powershell
cd H:\Investimentos\rl_trader\strategy\Scripts
python bot_orchestrator.py
```

### 2. Iniciar apenas bots específicos
```powershell
python bot_orchestrator.py --bots cluster_eurusd llm_trader
```

### 3. Dry-run (ver o que faria sem iniciar)
```powershell
python bot_orchestrator.py --dry-run
```

## Configuração dos Bots (`bots_config.json`)

Cada bot é definido por um objeto JSON:

```json
{
  "id": "cluster_eurusd",
  "name": "Cluster EURUSD",
  "script": "mt5_intraday_cluster_bot.py",
  "cwd": "H:/Investimentos/rl_trader/rl_trader_cluster/Scripts",
  "python": "H:/Investimentos/rl_trader/rl_trader_cluster/Scripts/python.exe",
  "args": ["EURUSD", "--lot", "0.1", "--strategy", "all", "--account", "demo"],
  "csv_log": "bot_cluster_log.csv",
  "enabled": true
}
```

| Campo | Descrição |
|---|---|
| `id` | Identificador único (usado nos comandos Telegram) |
| `name` | Nome amigável (exibido nos relatórios) |
| `script` | Nome do arquivo Python do bot |
| `cwd` | Diretório de trabalho (onde o script roda) |
| `python` | Caminho do python.exe da venv correta |
| `args` | Argumentos de linha de comando |
| `csv_log` | Nome do arquivo CSV de trades |
| `enabled` | `true` para iniciar, `false` para ignorar |

### Exemplos de configuração por bot:

**Cluster Bot (cripto)**:
```json
"args": ["BTCUSD", "--lot", "0.01", "--crypto", "--strategy", "all", "--account", "real"]
```

**LSTM Exchange Bot**:
```json
"args": ["XAUUSD", "--exchange", "real"]
```

**LLM/Hybrid/RSI Bots**:
```json
"args": ["--symbols", "XAUUSD,EURUSD", "--exchange", "demo", "--lot", "0.1", "--loop"]
```

## Comandos Telegram

| Comando | Descrição |
|---|---|
| `/help` | Lista todos os comandos |
| `/status` | Status rápido de todos os bots |
| `/status <nome>` | Status detalhado de um bot específico |
| `/bots` | Lista IDs de todos os bots registrados |
| `/report` | Gera relatório consolidado do dia |
| `/restart <nome>` | Reinicia um bot manualmente |
| `/stop <nome>` | Para um bot específico |
| `/start <nome>` | Inicia um bot que estava parado |
| `/logs <nome>` | Mostra últimas 15 linhas de saída |
| `/stop_all` | Para o orquestrador inteiro |

### Exemplos:
```
/status cluster           → status do Cluster Bot
/status lstm              → status do LSTM Bot  
/restart hybrid           → reinicia o Hybrid Bot
/logs llm                 → últimas linhas do LLM Trader
/stop rsi                 → para o RSI+Breakout Bot
```

> O nome pode ser parcial e case-insensitive. "cluster", "CLUSTER", "Cluster EURUSD" funcionam.

## Auto-Restart

Quando um bot cai:
1. O orquestrador detecta em até 30 segundos
2. Aguarda 60s de cooldown antes de reiniciar
3. Tenta até 5 vezes
4. Se todas falharem, desativa o bot e notifica via Telegram
5. Restart manual via `/restart <nome>` reseta o contador

## Relatório Diário

Enviado automaticamente às 21h, contém:
- Status de cada bot (rodando/parado)
- Uptime e número de restarts
- Trades do dia (wins, losses, abertos)
- P/L individual e consolidado
- Win rate geral

Pode ser solicitado a qualquer momento com `/report`.

## Log do Orquestrador

O arquivo `orchestrator_log.csv` registra eventos como:
- `START` / `STOP` – Bot iniciado/parado
- `CRASH` – Bot caiu (com exit code)
- `RESTART` / `RESTART_FAIL` – Tentativa de reinício
- `DISABLED` – Bot desativado após max restarts
- `MANUAL_RESTART` / `MANUAL_STOP` / `MANUAL_START` – Ações via Telegram
- `DAILY_REPORT` – Relatório diário enviado
- `ORCH_START` / `ORCH_STOP` – Orquestrador iniciou/parou
