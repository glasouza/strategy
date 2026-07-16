"""
bot_orchestrator.py – Orquestrador Central de Bots de Trading
=============================================================
Gerencia os 5 bots de trading como subprocessos:
  • Lança cada bot com seus parâmetros individuais
  • Monitora saúde (heartbeat por processo)
  • Reinicia automaticamente bots que caíram
  • Escuta comandos via Telegram (/status, /stop, /report, etc.)
  • Envia relatório diário consolidado às 21:00
  • Log de eventos do orquestrador (restart, crash, etc.)

Uso:
  python bot_orchestrator.py                     # Inicia todos os bots habilitados
  python bot_orchestrator.py --bots cluster_eurusd llm_trader   # Inicia apenas esses
  python bot_orchestrator.py --dry-run           # Mostra o que faria sem iniciar
"""

import os
import sys
import json
import time
import csv
import subprocess
import threading
import argparse
import signal
from datetime import datetime, timedelta
from collections import defaultdict
import html

# ── Resolve caminhos ────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RL_TRADER_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

# Importa TelegramBot centralizado
sys.path.insert(0, RL_TRADER_ROOT)
try:
    from telegram_utils import TelegramBot
    telegram_config_path = os.path.join(RL_TRADER_ROOT, "telegram.json")
    if os.path.exists(telegram_config_path):
        telegram = TelegramBot(telegram_config_path)
    else:
        telegram = None
        print(f"[!] telegram.json não encontrado em {RL_TRADER_ROOT}. Notificações desativadas.")
except Exception as e:
    telegram = None
    print(f"[!] Erro ao carregar telegram_utils: {e}")

# ── Constantes ──────────────────────────────────────────────────────────────
CONFIG_FILE = os.path.join(SCRIPT_DIR, "bots_config.json")
ORCH_LOG_FILE = os.path.join(SCRIPT_DIR, "orchestrator_log.csv")
MAX_RESTART_ATTEMPTS = 5
RESTART_COOLDOWN_SEC = 60
DAILY_REPORT_HOUR = 21  # 21h local
HEALTH_CHECK_INTERVAL = 30  # segundos


# =============================================================================
# Classes auxiliares
# =============================================================================

class BotProcess:
    """Wrapper para um subprocesso de bot."""

    def __init__(self, config: dict):
        self.id = config["id"]
        self.name = config["name"]
        self.script = config["script"]
        self.cwd = config["cwd"].strip()
        self.python = config["python"].strip()
        self.args = config.get("args", [])
        self.csv_log = config.get("csv_log", "")
        self.enabled = config.get("enabled", True)

        self.process: subprocess.Popen = None
        self.start_time: datetime = None
        self.restart_count = 0
        self.last_restart: datetime = None
        self.status = "PARADO"
        self.last_output_lines = []
        self.last_read_row_count = 0

    @property
    def is_alive(self) -> bool:
        if self.process is None:
            return False
        return self.process.poll() is None

    @property
    def uptime_str(self) -> str:
        if not self.start_time:
            return "N/A"
        delta = datetime.now() - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours}h {minutes}m {seconds}s"

    @property
    def csv_log_path(self) -> str:
        if not self.csv_log:
            return ""
        return os.path.join(self.cwd, self.csv_log)

    def start(self):
        """Inicia o subprocesso do bot."""
        if self.is_alive:
            print(f"  [{self.id}] Já está rodando (PID {self.process.pid}).")
            return

        script_path = os.path.join(self.cwd, self.script) if not os.path.isabs(self.script) else self.script
        cmd = [self.python, script_path] + [str(a) for a in self.args]
        print(f"  [{self.id}] Iniciando: {' '.join(cmd)}")
        print(f"  [{self.id}] CWD: {self.cwd}")

        # Sanitiza variáveis de ambiente para isolar o ambiente virtual do bot subprocesso
        sub_env = os.environ.copy()
        sub_env.pop("VIRTUAL_ENV", None)
        sub_env["PYTHONIOENCODING"] = "utf-8"  # Força o Python do bot a escrever em UTF-8
        if "PATH" in sub_env:
            sub_env["PATH"] = os.path.abspath(self.cwd) + os.pathsep + sub_env["PATH"]

        try:
            self.process = subprocess.Popen(
                cmd,
                cwd=self.cwd,
                env=sub_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",  # Lê a saída do bot decodificando como UTF-8
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
            )
            self.start_time = datetime.now()
            self.status = "RODANDO"
            print(f"  [{self.id}] ✅ PID: {self.process.pid}")

            # Thread para capturar saída em background
            t = threading.Thread(target=self._capture_output, daemon=True)
            t.start()

        except Exception as e:
            self.status = "ERRO"
            print(f"  [{self.id}] ❌ Falha ao iniciar: {e}")

    def stop(self):
        """Para o subprocesso graciosamente."""
        if not self.is_alive:
            self.status = "PARADO"
            return

        print(f"  [{self.id}] Enviando SIGTERM (PID {self.process.pid})...")
        try:
            self.process.terminate()
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            print(f"  [{self.id}] Forçando kill...")
            self.process.kill()
        self.status = "PARADO"
        self.process = None

    def _capture_output(self):
        """Captura stdout/stderr do subprocesso em buffer circular."""
        try:
            for line in self.process.stdout:
                line = line.rstrip()
                if line:
                    self.last_output_lines.append(line)
                    if len(self.last_output_lines) > 50:
                        self.last_output_lines.pop(0)
        except (ValueError, OSError):
            pass  # Pipe fechado

    def get_csv_summary(self) -> dict:
        """Lê o CSV de log do bot e retorna resumo do dia."""
        result = {"total": 0, "abertos": 0, "fechados": 0, "lucro": 0.0, "wins": 0, "losses": 0}
        path = self.csv_log_path
        if not path or not os.path.exists(path):
            return result

        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader, None)
                if not header:
                    return result

                # Detectar índices por cabeçalho
                idx_data = header.index("DataHora") if "DataHora" in header else 1
                idx_status = header.index("Status") if "Status" in header else -2
                idx_lucro = header.index("Lucro") if "Lucro" in header else -1

                for row in reader:
                    if len(row) <= max(idx_data, abs(idx_status), abs(idx_lucro)):
                        continue
                    # Filtra só trades de hoje
                    if today_str not in row[idx_data]:
                        continue
                    result["total"] += 1
                    status = row[idx_status]
                    if status == "ABERTO":
                        result["abertos"] += 1
                    elif status == "FECHADO":
                        result["fechados"] += 1
                        try:
                            lucro = float(row[idx_lucro])
                            result["lucro"] += lucro
                            if lucro > 0:
                                result["wins"] += 1
                            elif lucro < 0:
                                result["losses"] += 1
                        except ValueError:
                            pass
        except Exception as e:
            print(f"  [{self.id}] Erro ao ler CSV: {e}")
        return result

    def initialize_log_tracking(self):
        """Inicializa o contador de linhas lidas para ignorar trades antigos."""
        path = self.csv_log_path
        if not path or not os.path.exists(path):
            self.last_read_row_count = 0
            return
        
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                rows = list(reader)
                self.last_read_row_count = len(rows)
            print(f"  [{self.id}] Inicializado rastreamento de log com {self.last_read_row_count} linhas existentes.")
        except Exception as e:
            print(f"  [{self.id}] Erro ao ler CSV para inicializar rastreamento: {e}")
            self.last_read_row_count = 0

    def check_new_trades(self) -> list:
        """Lê novas linhas do CSV do bot e retorna dados mapeados."""
        path = self.csv_log_path
        if not path or not os.path.exists(path):
            return []

        new_trades = []
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                rows = list(reader)
                
                total_rows = len(rows)
                if total_rows <= self.last_read_row_count:
                    return []
                
                header = rows[0] if rows else []
                if not header:
                    return []

                # Mapeamento dinâmico e case-insensitive de colunas essenciais
                header_lower = [col.lower().strip() for col in header]
                
                def find_idx(possible_names):
                    for name in possible_names:
                        if name.lower() in header_lower:
                            return header_lower.index(name.lower())
                    return -1

                idx_ticket = find_idx(["ticket"])
                idx_data = find_idx(["datahora", "timestamp", "date", "datetime"])
                idx_ativo = find_idx(["ativo", "symbol", "asset"])
                idx_direcao = find_idx(["direcao", "side", "type", "direction"])
                idx_entrada = find_idx(["entrada", "price", "entry", "entry_price"])
                idx_sl = find_idx(["sl", "stop_loss", "stoploss"])
                idx_tp = find_idx(["tp", "take_profit", "takeproof"])
                idx_status = find_idx(["status"])
                idx_lucro = find_idx(["lucro", "profit"])

                start_idx = max(1, self.last_read_row_count)
                for i in range(start_idx, total_rows):
                    row = rows[i]
                    if len(row) < len(header):
                        continue
                    
                    ticket = row[idx_ticket] if idx_ticket != -1 and idx_ticket < len(row) else "N/A"
                    data_hora = row[idx_data] if idx_data != -1 and idx_data < len(row) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    ativo = row[idx_ativo] if idx_ativo != -1 and idx_ativo < len(row) else "N/A"
                    direcao = row[idx_direcao] if idx_direcao != -1 and idx_direcao < len(row) else "N/A"
                    entrada = row[idx_entrada] if idx_entrada != -1 and idx_entrada < len(row) else "N/A"
                    sl = row[idx_sl] if idx_sl != -1 and idx_sl < len(row) else "N/A"
                    tp = row[idx_tp] if idx_tp != -1 and idx_tp < len(row) else "N/A"
                    status = row[idx_status] if idx_status != -1 and idx_status < len(row) else "N/A"
                    lucro = row[idx_lucro] if idx_lucro != -1 and idx_lucro < len(row) else "N/A"

                    new_trades.append({
                        "ticket": ticket,
                        "data_hora": data_hora,
                        "ativo": ativo,
                        "direcao": direcao,
                        "entrada": entrada,
                        "sl": sl,
                        "tp": tp,
                        "status": status,
                        "lucro": lucro
                    })

                self.last_read_row_count = total_rows

        except Exception as e:
            print(f"  [{self.id}] Erro ao ler novos trades do CSV: {e}")
        
        return new_trades


# =============================================================================
# Orquestrador principal
# =============================================================================

class Orchestrator:
    """Orquestrador central que gerencia todos os bots."""

    def __init__(self, config_path: str, bot_filter: list = None):
        self.config_path = config_path
        self.bots: dict[str, BotProcess] = {}
        self._running = True
        self._telegram_offset = 0
        self._last_daily_report = None

        self._load_config(bot_filter)
        self._init_orch_log()
        
        # Inicializa o rastreamento de logs existentes para todos os bots carregados
        for bot in self.bots.values():
            bot.initialize_log_tracking()

    def _load_config(self, bot_filter: list = None):
        """Carrega bots_config.json."""
        with open(self.config_path, "r", encoding="utf-8") as f:
            configs = json.load(f)

        for cfg in configs:
            bot_id = cfg["id"]
            if bot_filter and bot_id not in bot_filter:
                continue
            if not cfg.get("enabled", True):
                continue
            self.bots[bot_id] = BotProcess(cfg)

        print(f"[ORCH] {len(self.bots)} bot(s) carregados da config.")

    def _init_orch_log(self):
        """Inicializa log CSV do orquestrador."""
        if not os.path.exists(ORCH_LOG_FILE):
            with open(ORCH_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["DataHora", "Evento", "BotID", "Detalhe"])

    def _log_event(self, event: str, bot_id: str = "ORCH", detail: str = ""):
        """Registra evento no log CSV."""
        with open(ORCH_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                event, bot_id, detail
            ])

    # ── Ciclo de vida ────────────────────────────────────────────────────

    def start_all(self):
        """Inicia todos os bots registrados."""
        print("\n" + "=" * 60)
        print("  🎛️  ORQUESTRADOR DE BOTS - INICIANDO")
        print("=" * 60)

        for bot_id, bot in self.bots.items():
            bot.start()
            self._log_event("START", bot_id, f"PID={bot.process.pid if bot.process else 'FALHOU'}")
            time.sleep(2)  # Intervalo para não sobrecarregar o MT5

        msg = (
            f"🎛️ <b>ORQUESTRADOR INICIADO</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bots ativos: {len(self.bots)}\n"
        )
        for bot_id, bot in self.bots.items():
            emoji = "✅" if bot.is_alive else "❌"
            msg += f"  {emoji} {bot.name}\n"
        msg += f"━━━━━━━━━━━━━━━━━━━━━\n"
        msg += f"Use /help para comandos."

        self._send_telegram(msg)
        self._log_event("ORCH_START", detail=f"{len(self.bots)} bots")

    def stop_all(self):
        """Para todos os bots."""
        print("\n[ORCH] Parando todos os bots...")
        for bot_id, bot in self.bots.items():
            bot.stop()
            self._log_event("STOP", bot_id)
        self._send_telegram("🛑 <b>ORQUESTRADOR ENCERRADO</b>\nTodos os bots foram parados.")
        self._log_event("ORCH_STOP")

    # ── Health check e auto-restart ──────────────────────────────────────

    def _health_check(self):
        """Verifica se todos os bots estão rodando e reinicia os que caíram."""
        for bot_id, bot in self.bots.items():
            # Processa novos trades deste bot
            new_trades = bot.check_new_trades()
            for trade in new_trades:
                detalhe = (
                    f"Ticket={trade['ticket']} | Ativo={trade['ativo']} | "
                    f"Direcao={trade['direcao']} | Entrada={trade['entrada']} | "
                    f"SL={trade['sl']} | TP={trade['tp']} | "
                    f"Status={trade['status']} | Lucro={trade['lucro']}"
                )
                self._log_event("TRADE", bot.id, detalhe)
                print(f"  [ORCH] [TRADE] [{bot.id}] {detalhe}")

                # Envia alerta para o Telegram
                msg = (
                    f"🔔 <b>[{bot.name}] - OPERAÇÃO</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📍 <b>Ativo:</b> {trade['ativo']}\n"
                    f"↕️ <b>Direção:</b> {trade['direcao']}\n"
                    f"🎫 <b>Ticket:</b> <code>{trade['ticket']}</code>\n"
                    f"💵 <b>Entrada:</b> {trade['entrada']}\n"
                    f"🛡️ <b>SL:</b> {trade['sl']} | 🎯 <b>TP:</b> {trade['tp']}\n"
                    f"📊 <b>Status:</b> {trade['status']}\n"
                    f"💰 <b>Lucro:</b> {trade['lucro']}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━"
                )
                self._send_telegram(msg)

            if bot.status == "PARADO":
                continue

            if not bot.is_alive:
                exit_code = bot.process.returncode if bot.process else "?"
                print(f"\n[!] [{bot_id}] CAIU! Exit code: {exit_code}")
                if bot.last_output_lines:
                    print(f"  [{bot_id}] Últimas linhas de saída:")
                    for line in bot.last_output_lines[-15:]:
                        print(f"    > {line}")
                self._log_event("CRASH", bot_id, f"exit_code={exit_code}")

                # Verifica cooldown e limites
                now = datetime.now()
                if bot.restart_count >= MAX_RESTART_ATTEMPTS:
                    msg = (f"🚨 <b>{bot.name} DESATIVADO</b>\n"
                           f"Atingiu o limite de {MAX_RESTART_ATTEMPTS} reinícios.\n"
                           f"Intervenção manual necessária.")
                    self._send_telegram(msg)
                    bot.status = "DESATIVADO"
                    self._log_event("DISABLED", bot_id, "max restarts atingido")
                    continue

                if bot.last_restart and (now - bot.last_restart).total_seconds() < RESTART_COOLDOWN_SEC:
                    continue  # Espera o cooldown

                # Reinicia
                bot.restart_count += 1
                bot.last_restart = now
                print(f"  [{bot_id}] Reiniciando... (tentativa {bot.restart_count}/{MAX_RESTART_ATTEMPTS})")
                bot.start()

                if bot.is_alive:
                    self._send_telegram(
                        f"🔄 <b>{bot.name} REINICIADO</b>\n"
                        f"Tentativa {bot.restart_count}/{MAX_RESTART_ATTEMPTS}\n"
                        f"PID: {bot.process.pid}"
                    )
                    self._log_event("RESTART", bot_id, f"tentativa={bot.restart_count}")
                else:
                    self._send_telegram(
                        f"❌ <b>{bot.name} FALHOU AO REINICIAR</b>\n"
                        f"Tentativa {bot.restart_count}/{MAX_RESTART_ATTEMPTS}"
                    )
                    self._log_event("RESTART_FAIL", bot_id)

    # ── Relatório diário ─────────────────────────────────────────────────

    def _build_daily_report(self) -> str:
        """Monta o relatório consolidado do dia."""
        now = datetime.now()
        total_lucro = 0.0
        total_trades = 0
        total_wins = 0
        total_losses = 0

        lines = [
            f"📊 <b>RELATÓRIO DIÁRIO</b>",
            f"📅 {now.strftime('%d/%m/%Y')} às {now.strftime('%H:%M')}",
            f"━━━━━━━━━━━━━━━━━━━━━",
        ]

        for bot_id, bot in self.bots.items():
            emoji = "✅" if bot.is_alive else "❌"
            summary = bot.get_csv_summary()
            lucro = summary["lucro"]
            total_lucro += lucro
            total_trades += summary["total"]
            total_wins += summary["wins"]
            total_losses += summary["losses"]

            lucro_str = f"+${lucro:.2f}" if lucro >= 0 else f"-${abs(lucro):.2f}"
            lines.append(
                f"\n{emoji} <b>{bot.name}</b>\n"
                f"  ⏱️ Uptime: {bot.uptime_str}\n"
                f"  🔄 Restarts: {bot.restart_count}\n"
                f"  📈 Trades hoje: {summary['total']} "
                f"(✅{summary['wins']} ❌{summary['losses']} ⏳{summary['abertos']})\n"
                f"  💰 P/L: {lucro_str}"
            )

        # Resumo geral
        lucro_total_str = f"+${total_lucro:.2f}" if total_lucro >= 0 else f"-${abs(total_lucro):.2f}"
        winrate = f"{(total_wins / (total_wins + total_losses) * 100):.0f}%" if (total_wins + total_losses) > 0 else "N/A"

        lines.append(f"\n━━━━━━━━━━━━━━━━━━━━━")
        lines.append(
            f"📋 <b>CONSOLIDADO</b>\n"
            f"  Trades: {total_trades} | Win Rate: {winrate}\n"
            f"  💰 P/L Total: <b>{lucro_total_str}</b>"
        )

        return "\n".join(lines)

    def _check_daily_report(self):
        """Envia relatório diário na hora configurada."""
        now = datetime.now()
        if now.hour == DAILY_REPORT_HOUR:
            today = now.date()
            if self._last_daily_report != today:
                self._last_daily_report = today
                report = self._build_daily_report()
                self._send_telegram(report)
                self._log_event("DAILY_REPORT")
                print(f"\n[ORCH] Relatório diário enviado às {now.strftime('%H:%M')}.")

    # ── Comandos Telegram ────────────────────────────────────────────────

    def _send_telegram(self, msg: str):
        """Envia mensagem via Telegram centralizado."""
        if telegram:
            telegram.send_message(msg)

    def _handle_telegram_commands(self):
        """Processa comandos recebidos via Telegram."""
        if not telegram:
            return

        data = telegram.get_updates(offset=self._telegram_offset, timeout=1)
        if not data or not data.get("result"):
            return

        for update in data["result"]:
            self._telegram_offset = update["update_id"] + 1
            message = update.get("message", {})
            chat_id = str(message.get("chat", {}).get("id", ""))

            if chat_id != telegram.chat_id:
                continue

            text = message.get("text", "").strip()
            if not text:
                continue

            text_lower = text.lower()
            print(f"  [TELEGRAM IN] {text}")

            if text_lower == "/help":
                self._cmd_help()
            elif text_lower == "/status":
                self._cmd_status_all()
            elif text_lower.startswith("/status "):
                bot_name = text[8:].strip()
                self._cmd_status_bot(bot_name)
            elif text_lower == "/report":
                report = self._build_daily_report()
                self._send_telegram(report)
            elif text_lower.startswith("/restart "):
                bot_name = text[9:].strip()
                self._cmd_restart_bot(bot_name)
            elif text_lower.startswith("/stop "):
                bot_name = text[6:].strip()
                self._cmd_stop_bot(bot_name)
            elif text_lower.startswith("/start "):
                bot_name = text[7:].strip()
                self._cmd_start_bot(bot_name)
            elif text_lower == "/stop_all":
                self._send_telegram("🛑 Parando todos os bots...")
                self._running = False
            elif text_lower.startswith("/logs "):
                bot_name = text[6:].strip()
                self._cmd_logs_bot(bot_name)
            elif text_lower == "/bots":
                self._cmd_list_bots()

    def _find_bot(self, query: str):
        """Encontra um bot pelo ID ou nome (busca parcial, case-insensitive)."""
        q = query.lower().strip()
        # Tenta match exato por ID
        if q in self.bots:
            return self.bots[q]
        # Match parcial por nome ou ID
        for bot_id, bot in self.bots.items():
            if q in bot_id.lower() or q in bot.name.lower():
                return bot
        return None

    def _cmd_help(self):
        self._send_telegram(
            "📖 <b>COMANDOS DO ORQUESTRADOR</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "/status - Status geral de todos os bots\n"
            "/status [nome] - Status detalhado de um bot\n"
            "/bots - Lista todos os bots registrados\n"
            "/report - Relatório consolidado do dia\n"
            "/restart [nome] - Reinicia um bot\n"
            "/stop [nome] - Para um bot\n"
            "/start [nome] - Inicia um bot parado\n"
            "/logs [nome] - Últimas linhas de saída\n"
            "/stop_all - Para o orquestrador inteiro\n"
            "/help - Esta mensagem"
        )

    def _cmd_status_all(self):
        lines = ["📊 <b>STATUS GERAL</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        for bot_id, bot in self.bots.items():
            emoji = {"RODANDO": "🟢", "PARADO": "🔴", "ERRO": "🟠", "DESATIVADO": "⛔"}.get(bot.status, "❓")
            lines.append(f"{emoji} <b>{bot.name}</b> ({bot.status})")
            if bot.is_alive:
                lines.append(f"    ⏱️ {bot.uptime_str} | 🔄 {bot.restart_count} restarts")
        self._send_telegram("\n".join(lines))

    def _cmd_status_bot(self, query: str):
        bot = self._find_bot(query)
        if not bot:
            self._send_telegram(f"❓ Bot '{query}' não encontrado.\nUse /bots para ver a lista.")
            return

        emoji = "🟢" if bot.is_alive else "🔴"
        summary = bot.get_csv_summary()
        lucro = summary["lucro"]
        lucro_str = f"+${lucro:.2f}" if lucro >= 0 else f"-${abs(lucro):.2f}"

        last_lines = "\n".join(bot.last_output_lines[-5:]) if bot.last_output_lines else "(sem saída recente)"
        last_lines_escaped = html.escape(last_lines)

        msg = (
            f"{emoji} <b>{bot.name}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🆔 ID: {bot.id}\n"
            f"📍 Status: {bot.status}\n"
            f"⏱️ Uptime: {bot.uptime_str}\n"
            f"🔄 Restarts: {bot.restart_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📈 Trades hoje: {summary['total']}\n"
            f"  ✅ Wins: {summary['wins']} | ❌ Losses: {summary['losses']}\n"
            f"  ⏳ Abertos: {summary['abertos']}\n"
            f"  💰 P/L: {lucro_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📜 Última saída:\n<code>{last_lines_escaped}</code>"
        )
        self._send_telegram(msg)

    def _cmd_list_bots(self):
        lines = ["🤖 <b>BOTS REGISTRADOS</b>", "━━━━━━━━━━━━━━━━━━━━━"]
        for bot_id, bot in self.bots.items():
            emoji = {"RODANDO": "🟢", "PARADO": "🔴", "ERRO": "🟠", "DESATIVADO": "⛔"}.get(bot.status, "❓")
            lines.append(f"{emoji} <code>{bot.id}</code> → {bot.name}")
        lines.append("\nUse o ID ou nome parcial com /status, /restart, /stop, /start")
        self._send_telegram("\n".join(lines))

    def _cmd_restart_bot(self, query: str):
        bot = self._find_bot(query)
        if not bot:
            self._send_telegram(f"❓ Bot '{query}' não encontrado.")
            return
        bot.stop()
        time.sleep(2)
        bot.restart_count = 0  # Reset no restart manual
        bot.start()
        status = "✅ Reiniciado" if bot.is_alive else "❌ Falhou"
        self._send_telegram(f"🔄 <b>{bot.name}</b>: {status}")
        self._log_event("MANUAL_RESTART", bot.id)

    def _cmd_stop_bot(self, query: str):
        bot = self._find_bot(query)
        if not bot:
            self._send_telegram(f"❓ Bot '{query}' não encontrado.")
            return
        bot.stop()
        self._send_telegram(f"🛑 <b>{bot.name}</b> parado.")
        self._log_event("MANUAL_STOP", bot.id)

    def _cmd_start_bot(self, query: str):
        bot = self._find_bot(query)
        if not bot:
            self._send_telegram(f"❓ Bot '{query}' não encontrado.")
            return
        if bot.is_alive:
            self._send_telegram(f"⚠️ <b>{bot.name}</b> já está rodando.")
            return
        bot.restart_count = 0
        bot.status = "RODANDO"
        bot.start()
        status = "✅ Iniciado" if bot.is_alive else "❌ Falhou"
        self._send_telegram(f"▶️ <b>{bot.name}</b>: {status}")
        self._log_event("MANUAL_START", bot.id)

    def _cmd_logs_bot(self, query: str):
        bot = self._find_bot(query)
        if not bot:
            self._send_telegram(f"❓ Bot '{query}' não encontrado.")
            return
        lines = bot.last_output_lines[-15:] if bot.last_output_lines else ["(sem saída recente)"]
        output = "\n".join(lines)
        # Telegram tem limite de 4096 chars
        if len(output) > 3500:
            output = output[-3500:]
        output_escaped = html.escape(output)
        self._send_telegram(f"📜 <b>Logs: {bot.name}</b>\n<code>{output_escaped}</code>")

    # ── Loop principal ───────────────────────────────────────────────────

    def run(self):
        """Loop principal do orquestrador."""
        self.start_all()

        # Flush mensagens antigas do Telegram
        if telegram:
            data = telegram.get_updates(timeout=5)
            if data and data.get("result"):
                self._telegram_offset = data["result"][-1]["update_id"] + 1

        print("\n[ORCH] 🎛️  Loop de monitoramento ativo. CTRL+C para sair.\n")

        try:
            while self._running:
                self._health_check()
                self._handle_telegram_commands()
                self._check_daily_report()
                time.sleep(HEALTH_CHECK_INTERVAL)
        except KeyboardInterrupt:
            print("\n[ORCH] Interrupção manual...")
        finally:
            self.stop_all()
            print("[ORCH] Encerrado.")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="🎛️ Orquestrador de Bots de Trading")
    parser.add_argument(
        "--bots", nargs="*", default=None,
        help="IDs dos bots para iniciar (default: todos habilitados). Ex: --bots cluster_eurusd llm_trader"
    )
    parser.add_argument(
        "--config", type=str, default=CONFIG_FILE,
        help=f"Caminho do arquivo de configuração (default: {CONFIG_FILE})"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Mostra configuração sem iniciar os bots"
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"[!] Arquivo de configuração não encontrado: {args.config}")
        return

    orch = Orchestrator(config_path=args.config, bot_filter=args.bots)

    if args.dry_run:
        print("\n[DRY RUN] Bots que seriam iniciados:")
        for bot_id, bot in orch.bots.items():
            cmd = [bot.python, bot.script] + [str(a) for a in bot.args]
            print(f"  • {bot.name} ({bot.id})")
            print(f"    CMD: {' '.join(cmd)}")
            print(f"    CWD: {bot.cwd}")
            print(f"    LOG: {bot.csv_log_path}")
            print()
        return

    orch.run()


if __name__ == "__main__":
    main()
