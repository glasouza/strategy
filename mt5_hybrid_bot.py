"""
mt5_hybrid_bot.py - Estratégia Híbrida: RSI Mean-Reversion + Breakout com Volume
Traduzido do Pine Script (TradingView) e melhorado com:
  1. Filtro de Volume (tick_volume) em todas as entradas
  2. Stop Loss ATR-based obrigatório (o original não tinha SL em RSI)
  3. Trailing Stop dinâmico nos trades de Breakout
  4. Log CSV para auditoria de P&L

Autor: Orquestrador RL_Trader (strategy env)
Magic Number: 888222
"""
import os
import time
import argparse
import json
import csv
import numpy as np
import MetaTrader5 as mt5
from datetime import datetime, timedelta
import sys

try:
    # pyrefly: ignore [missing-import]
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MAGIC_NUMBER = 888222
CSV_LOG_FILE = "bot_hybrid_log.csv"
telegram = None

# =============================================================================
# INDICADORES TÉCNICOS (Numpy puro - sem dependências pesadas)
# =============================================================================

def calc_ema(data, period):
    """Exponential Moving Average."""
    ema = np.full_like(data, np.nan, dtype=float)
    ema[period - 1] = np.mean(data[:period])
    mult = 2.0 / (period + 1)
    for i in range(period, len(data)):
        ema[i] = data[i] * mult + ema[i - 1] * (1 - mult)
    return ema

def calc_sma(data, period):
    """Simple Moving Average."""
    sma = np.full_like(data, np.nan, dtype=float)
    for i in range(period - 1, len(data)):
        sma[i] = np.mean(data[i - period + 1 : i + 1])
    return sma

def calc_rsi(close, period=14):
    """RSI via Wilder Smoothing."""
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    rsi = np.full(len(close), np.nan)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    if avg_loss == 0:
        rsi[period] = 100.0
    else:
        rsi[period] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            rsi[i + 1] = 100.0
        else:
            rsi[i + 1] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi

def calc_atr(high, low, close, period=14):
    """Average True Range via Wilder Smoothing."""
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))

    atr = np.full_like(tr, np.nan)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    return atr

def calc_adx(high, low, close, period=14):
    """ADX, +DI, -DI — réplica exata do ADX.mq5 (MetaTrader 5).

    O MT5 ADX padrão calcula +DM/TR por barra e aplica EMA no ratio,
    diferente do Wilder clássico que suaviza numerador e denominador
    separadamente.
    """
    n = len(close)
    alpha = 2.0 / (period + 1)

    pd_buf = np.zeros(n)   # +DM/TR por barra
    nd_buf = np.zeros(n)   # -DM/TR por barra
    plus_di = np.zeros(n)  # EMA(pd_buf)
    minus_di = np.zeros(n) # EMA(nd_buf)
    dx_buf = np.zeros(n)   # DX por barra
    adx = np.zeros(n)      # EMA(dx_buf)

    for i in range(1, n):
        tmp_pos = high[i] - high[i - 1]
        tmp_neg = low[i - 1] - low[i]
        if tmp_pos < 0.0:
            tmp_pos = 0.0
        if tmp_neg < 0.0:
            tmp_neg = 0.0
        # Exclusão mútua (igual ao MT5)
        if tmp_pos > tmp_neg:
            tmp_neg = 0.0
        elif tmp_pos < tmp_neg:
            tmp_pos = 0.0
        else:
            tmp_pos = 0.0
            tmp_neg = 0.0

        tr = max(abs(high[i] - low[i]),
                 abs(high[i] - close[i - 1]),
                 abs(low[i] - close[i - 1]))
        if tr != 0.0:
            pd_buf[i] = 100.0 * tmp_pos / tr
            nd_buf[i] = 100.0 * tmp_neg / tr

        # EMA: ExponentialMA do MT5
        plus_di[i] = plus_di[i - 1] + alpha * (pd_buf[i] - plus_di[i - 1])
        minus_di[i] = minus_di[i - 1] + alpha * (nd_buf[i] - minus_di[i - 1])

        denom = plus_di[i] + minus_di[i]
        if denom != 0.0:
            dx_buf[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom

        adx[i] = adx[i - 1] + alpha * (dx_buf[i] - adx[i - 1])

    return adx, plus_di, minus_di


# =============================================================================
# BOT PRINCIPAL
# =============================================================================

class HybridBot:
    def __init__(self, symbols, lot_size=0.1, exchange="demo", max_trades=2):
        self.symbols = symbols
        self.lot_size = lot_size
        self.exchange = exchange
        self.max_trades = max_trades

        # Parâmetros configuráveis (mesmos do Pine Script + melhorias)
        self.ema_period = 200
        self.rsi_period = 14
        self.rsi_buy_thresh = 40
        self.rsi_sell_thresh = 60
        self.rsi_exit_thresh = 50
        self.adx_period = 14
        self.adx_threshold = 25
        self.atr_period = 14
        self.atr_sl_rsi = 1.5       # ATR mult para SL em trades RSI (MELHORIA)
        self.atr_sl_breakout = 2.0   # ATR mult para SL inicial em Breakout
        self.atr_trail_mult = 2.0    # ATR mult para Trailing Stop
        self.breakout_lookback = 20
        self.vol_sma_period = 20
        self.vol_breakout_min = 1.3  # Volume mínimo para Breakout (MELHORIA)
        self.vol_reversion_max = 0.9 # Volume máximo para Mean-Reversion (MELHORIA)
        self.tp_rr_ratio = 2.0      # Risk:Reward para TP
        self.trail_trigger_pct = 1.0 # Gatilho % lucro para trailing candle
        
        # Centralized Telegram
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if base_dir not in sys.path:
            sys.path.append(base_dir)
        try:
            from telegram_utils import TelegramBot
            self.telegram = TelegramBot(os.path.join(base_dir, "telegram.json"))
        except ImportError:
            self.telegram = None
            print("[!] Erro ao carregar telegram_utils.py. Telegram bot desativado.")

    # -----------------------------------------------------------------
    # CONEXÃO MT5
    # -----------------------------------------------------------------
    def connect(self):
        print("[*] Lendo acesso.json e Conectando ao MetaTrader 5...")
        try:
            DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
            acesso_path = os.path.join(DIR_SCRIPT, "acesso.json")
            if not os.path.exists(acesso_path):
                acesso_path = os.path.join(os.path.dirname(os.path.dirname(DIR_SCRIPT)), "acesso.json")
            with open(acesso_path) as f:
                acesso = json.load(f)

            if self.exchange == "real":
                conta = next((c for c in acesso if "Real" in c.get("plataforma", "")), None)
                if not conta and len(acesso) > 2:
                    conta = acesso[4]
            else:
                conta = next((c for c in acesso if "FBS_Demo_hybrid_bot" in c.get("plataforma", "") and "Real" not in c.get("plataforma", "")), None)
                if not conta and len(acesso) > 1:
                    conta = acesso[4]

            if not conta:
                conta = acesso[4]

            user = conta["login"]
            password = conta["senha"]
            server = conta.get("server", "FBS-Real" if "Real" in conta.get("plataforma", "") else "FBS-Demo")
            terminal_path = conta.get("terminal", "")

            print(f"[*] Autenticando: Conta {user} | Server {server} ...")
            if terminal_path:
                print(f"[*] Terminal path: {terminal_path}")

            init_kwargs = {"login": user, "password": password, "server": server}
            if terminal_path:
                init_kwargs["path"] = terminal_path

            if not mt5.initialize(**init_kwargs):
                print(f"[!] Falha fatal: não conseguiu conectar ao terminal {terminal_path}. Código: {mt5.last_error()}")
                return False

            if not mt5.login(login=user, password=password, server=server):
                print(f"[!] Falha mt5.login: {mt5.last_error()}")
                return False

        except Exception as e:
            print(f"[!] Erro credentials: {e}")
            return False

        print(f"[*] Conectado! (MT5 {mt5.version()})")
        return True

    # -----------------------------------------------------------------
    # CALCULAR TODOS OS INDICADORES DE UMA VEZ
    # -----------------------------------------------------------------
    def calculate_indicators(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
        if rates is None or len(rates) < self.ema_period + 10:
            print(f"[!] Dados insuficientes para {symbol} ({len(rates) if rates else 0} barras)")
            return None

        close = np.array([r['close'] for r in rates], dtype=float)
        high = np.array([r['high'] for r in rates], dtype=float)
        low = np.array([r['low'] for r in rates], dtype=float)
        vol = np.array([r['tick_volume'] for r in rates], dtype=float)

        ema = calc_ema(close, self.ema_period)
        rsi = calc_rsi(close, self.rsi_period)
        adx, plus_di, minus_di = calc_adx(high, low, close, self.adx_period)
        atr = calc_atr(high, low, close, self.atr_period)
        vol_sma = calc_sma(vol, self.vol_sma_period)

        # Breakout: máx/mín dos últimos N candles FECHADOS (excluindo o atual)
        highest_break = np.max(close[-self.breakout_lookback - 1 : -1])
        lowest_break = np.min(close[-self.breakout_lookback - 1 : -1])

        v_sma = vol_sma[-1] if not np.isnan(vol_sma[-1]) else 1.0

        return {
            "close": close[-1],
            "high": high[-1],
            "low": low[-1],
            "prev_high": high[-2],
            "prev_low": low[-2],
            "ema200": ema[-1],
            "rsi": round(rsi[-1], 2) if not np.isnan(rsi[-1]) else 50.0,
            "adx": round(adx[-1], 2),
            "plus_di": round(plus_di[-1], 2),
            "minus_di": round(minus_di[-1], 2),
            "atr": round(atr[-1], 5),
            "tick_vol": vol[-1],
            "vol_sma": round(v_sma, 2),
            "vol_ratio": round(vol[-1] / v_sma, 2) if v_sma > 0 else 1.0,
            "highest_break": highest_break,
            "lowest_break": lowest_break,
        }

    # -----------------------------------------------------------------
    # GERAÇÃO DE SINAL
    # -----------------------------------------------------------------
    def generate_signal(self, ind):
        """Retorna dict {type, direction, sl_mult} ou None."""
        bullish = ind["close"] > ind["ema200"]
        bearish = ind["close"] < ind["ema200"]
        trending = ind["adx"] > self.adx_threshold
        ranging = not trending

        # === REGIME RANGING → RSI Mean-Reversion ===
        if ranging:
            # Volume deve estar BAIXO (exaustão) para confirmar reversão
            vol_ok = ind["vol_ratio"] < self.vol_reversion_max

            if ind["rsi"] < self.rsi_buy_thresh and bullish:
                return {
                    "type": "RSI",
                    "direction": "LONG",
                    "sl_mult": self.atr_sl_rsi,
                    "vol_ok": vol_ok,
                    "vol_ratio": ind["vol_ratio"],
                }
            if ind["rsi"] > self.rsi_sell_thresh and bearish:
                return {
                    "type": "RSI",
                    "direction": "SHORT",
                    "sl_mult": self.atr_sl_rsi,
                    "vol_ok": vol_ok,
                    "vol_ratio": ind["vol_ratio"],
                }

        # === REGIME TRENDING → Breakout ===
        if trending:
            # Volume deve estar ALTO para confirmar rompimento
            vol_ok = ind["vol_ratio"] > self.vol_breakout_min

            if bullish and ind["close"] > ind["highest_break"]:
                return {
                    "type": "BREAKOUT",
                    "direction": "LONG",
                    "sl_mult": self.atr_sl_breakout,
                    "vol_ok": vol_ok,
                    "vol_ratio": ind["vol_ratio"],
                }
            if bearish and ind["close"] < ind["lowest_break"]:
                return {
                    "type": "BREAKOUT",
                    "direction": "SHORT",
                    "sl_mult": self.atr_sl_breakout,
                    "vol_ok": vol_ok,
                    "vol_ratio": ind["vol_ratio"],
                }

        return None

    # -----------------------------------------------------------------
    # EXECUÇÃO DE ORDENS
    # -----------------------------------------------------------------
    def execute_trade(self, symbol, signal, atr_value):
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if not tick or not info:
            return None

        digits = info.digits
        point = info.point
        min_sl = max(info.trade_stops_level * point, 10 * point)
        sl_dist = max(atr_value * signal["sl_mult"], min_sl)

        if signal["direction"] == "LONG":
            entry = tick.ask
            sl = round(entry - sl_dist, digits)
            if signal["type"] == "RSI":
                tp = round(entry + sl_dist * self.tp_rr_ratio, digits)
            else:
                tp = 0.0  # Breakout: trailing stop, sem TP fixo
            order_type = mt5.ORDER_TYPE_BUY
            comment = f"HYB {signal['type']} Long"
        else:
            entry = tick.bid
            sl = round(entry + sl_dist, digits)
            if signal["type"] == "RSI":
                tp = round(entry - sl_dist * self.tp_rr_ratio, digits)
            else:
                tp = 0.0
            order_type = mt5.ORDER_TYPE_SELL
            comment = f"HYB {signal['type']} Short"

        print(f"\n[HYBRID] Executando {comment} em {symbol}")
        print(f"  Entrada: {entry} | SL: {sl} | TP: {tp if tp else 'Trailing'}")
        print(f"  ATR: {atr_value:.2f} | Vol Ratio: {signal['vol_ratio']}")

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(self.lot_size),
            "type": order_type,
            "price": entry,
            "sl": sl,
            "tp": tp,
            "deviation": 20,
            "magic": MAGIC_NUMBER,
            "comment": comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }

        result = mt5.order_send(request)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"  [!] Falha: {result.retcode} - {result.comment}")
            if result.retcode == 10027:
                print("  [DICA] Ative o botão 'Algo Trading' no topo do MetaTrader 5!")
        else:
            print(f"  [$$] OK! Ticket: {result.order}")
            log_trade_open(result.order, symbol, signal, entry, sl, tp)
            if self.telegram:
                msg = (f"✅ <b>[HYBRID] {signal['type']} {symbol} EXECUTADA!</b>\n"
                       f"Preço: {entry:.{digits}f}\n"
                       f"SL: {sl:.{digits}f} | TP: {tp if tp else 'TRAIL'}\n"
                       f"Lote: {self.lot_size}")
                self.telegram.send_message(msg)

        return result

    # -----------------------------------------------------------------
    # GESTÃO DE POSIÇÕES ABERTAS
    # -----------------------------------------------------------------
    def manage_open_positions(self):
        """Verifica saídas RSI e atualiza trailing stops em Breakouts."""
        positions = mt5.positions_get()
        if not positions:
            return

        my_positions = [p for p in positions if p.magic == MAGIC_NUMBER]
        if not my_positions:
            return

        for pos in my_positions:
            comment = pos.comment
            symbol = pos.symbol
            is_long = pos.type == mt5.POSITION_TYPE_BUY

            # --- RSI EXIT ---
            if "RSI" in comment:
                ind = self.calculate_indicators(symbol)
                if ind is None:
                    continue
                rsi_now = ind["rsi"]

                should_close = False
                if is_long and rsi_now > self.rsi_exit_thresh:
                    should_close = True
                    print(f"  [RSI EXIT] {symbol} Long - RSI={rsi_now} cruzou {self.rsi_exit_thresh}. Fechando.")
                elif not is_long and rsi_now < self.rsi_exit_thresh:
                    should_close = True
                    print(f"  [RSI EXIT] {symbol} Short - RSI={rsi_now} cruzou {self.rsi_exit_thresh}. Fechando.")

                if should_close:
                    self._close_position(pos)
                    continue
                # Trailing candle para RSI após 1% lucro
                self._trail_candle(pos, ind, is_long)

            # --- TRAILING STOP (Breakout) ---
            elif "BREAKOUT" in comment:
                ind = self.calculate_indicators(symbol)
                if ind is None:
                    continue
                atr_now = ind["atr"]
                trail_dist = atr_now * self.atr_trail_mult
                digits = mt5.symbol_info(symbol).digits

                if is_long:
                    new_sl = round(ind["high"] - trail_dist, digits)
                    if new_sl > pos.sl:
                        self._modify_sl(pos, new_sl)
                        print(f"  [TRAIL] {symbol} Long - SL atualizado: {pos.sl} -> {new_sl}")
                else:
                    new_sl = round(ind["low"] + trail_dist, digits)
                    if new_sl < pos.sl:
                        self._modify_sl(pos, new_sl)
                        print(f"  [TRAIL] {symbol} Short - SL atualizado: {pos.sl} -> {new_sl}")
                # Candle trailing adicional após 1% lucro
                self._trail_candle(pos, ind, is_long)

    def _trail_candle(self, pos, ind, is_long):
        """Move SL para extremidade da vela anterior quando lucro >= 1%."""
        entry = pos.price_open
        cur = pos.price_current
        cur_sl = pos.sl

        if is_long:
            pct = ((cur - entry) / entry) * 100
        else:
            pct = ((entry - cur) / entry) * 100

        # Também checa % do saldo
        acct = mt5.account_info()
        pct_acct = (pos.profit / acct.balance * 100) if acct and acct.balance > 0 else 0.0

        if pct < self.trail_trigger_pct and pct_acct < self.trail_trigger_pct:
            return

        info = mt5.symbol_info(pos.symbol)
        if not info:
            return
        digits = info.digits
        min_dist = max(info.trade_stops_level * info.point, 10 * info.point)

        if is_long:
            new_sl = round(ind["prev_low"], digits)
            if new_sl > cur_sl and (cur - new_sl) >= min_dist:
                self._modify_sl(pos, new_sl)
                print(f"  [CANDLE TRAIL] {pos.symbol} Long SL->{new_sl} (lucro ~{max(pct, pct_acct):.1f}%)")
        else:
            new_sl = round(ind["prev_high"], digits)
            if (cur_sl == 0 or new_sl < cur_sl) and (new_sl - cur) >= min_dist:
                self._modify_sl(pos, new_sl)
                print(f"  [CANDLE TRAIL] {pos.symbol} Short SL->{new_sl} (lucro ~{max(pct, pct_acct):.1f}%)")

    def _close_position(self, pos):
        tick = mt5.symbol_info_tick(pos.symbol)
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if close_type == mt5.ORDER_TYPE_SELL else tick.ask

        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": pos.symbol,
            "volume": pos.volume,
            "type": close_type,
            "position": pos.ticket,
            "price": price,
            "magic": MAGIC_NUMBER,
            "comment": "HYB Close",
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  [FECHADO] Ticket {pos.ticket} fechado com sucesso.")
        else:
            print(f"  [!] Falha ao fechar {pos.ticket}: {result.comment}")

    def _modify_sl(self, pos, new_sl):
        request = {
            "action": mt5.TRADE_ACTION_SLTP,
            "symbol": pos.symbol,
            "position": pos.ticket,
            "sl": new_sl,
            "tp": pos.tp,
        }
        mt5.order_send(request)

    # -----------------------------------------------------------------
    # HAS OPEN POSITION?
    # -----------------------------------------------------------------
    def has_position(self, symbol):
        positions = mt5.positions_get(symbol=symbol)
        if not positions:
            return False
        return any(p.magic == MAGIC_NUMBER for p in positions)

    def count_open_positions(self):
        positions = mt5.positions_get()
        if not positions:
            return 0
        return sum(1 for p in positions if p.magic == MAGIC_NUMBER)

    # -----------------------------------------------------------------
    # LOOP PRINCIPAL
    # -----------------------------------------------------------------
    def run(self, loop=False):
        if not self.connect():
            return

        active_symbols = []
        for sym in self.symbols:
            if mt5.symbol_select(sym, True):
                active_symbols.append(sym)
            else:
                print(f"  [!] {sym} não encontrado no Market Watch.")

        if not active_symbols:
            mt5.shutdown()
            return

        init_log()

        print("\n" + "=" * 55)
        print("   [HYBRID BOT] RSI + Breakout + Volume (Melhorado)")
        print("=" * 55)
        print(f"  Modo: {'LOOP (H1)' if loop else 'ONE-SHOT'}")
        print(f"  Conta: {self.exchange.upper()}")
        print(f"  Lote: {self.lot_size}")
        print(f"  Ativos: {', '.join(active_symbols)}")
        print(f"  Magic: {MAGIC_NUMBER}")
        print("=" * 55 + "\n")
        
        if self.telegram:
            self.telegram.send_message(f"🤖 <b>HYBRID BOT INICIADO</b>\nAtivos: {', '.join(active_symbols)}")

        def cycle():
            update_pending_logs()
            self.manage_open_positions()

            for sym in active_symbols:

                ind = self.calculate_indicators(sym)
                if ind is None:
                    continue

                regime = "TREND" if ind["adx"] > self.adx_threshold else "RANGE"
                bias = "Bull" if ind["close"] > ind["ema200"] else "Bear"
                now_str = datetime.now().strftime("%H:%M:%S")

                print(f"\n[{now_str}] {sym} | Regime: {regime} (ADX={ind['adx']}) | Bias: {bias}")
                print(f"  RSI={ind['rsi']} | ATR={ind['atr']:.2f} | Vol Ratio={ind['vol_ratio']}")

                signal = self.generate_signal(ind)

                if signal is None:
                    print(f"  -> Sem sinal. Neutro.")
                    continue

                if not signal["vol_ok"]:
                    print(f"  -> Sinal {signal['type']} {signal['direction']} detectado MAS volume inadequado (ratio={signal['vol_ratio']}). BLOQUEADO.")
                    continue

                print(f"  -> SINAL CONFIRMADO: {signal['type']} {signal['direction']} (Volume OK: {signal['vol_ratio']}x)")
                
                open_count = self.count_open_positions()
                if open_count >= self.max_trades:
                    print(f"  [GATE] Limite de trades simultâneos atingido ({open_count}/{self.max_trades}). Bloqueando trade em {sym}.")
                    if self.telegram:
                        self.telegram.send_message(
                            f"⚠️ <b>[HYBRID] Sinal em {sym} Ignorado!</b>\n"
                            f"Detectado sinal de {signal['direction']} ({signal['type']}), mas o limite de {self.max_trades} trades simultâneos está preenchido."
                        )
                    continue

                self.execute_trade(sym, signal, ind["atr"])

        # Execução
        if not loop:
            cycle()
            mt5.shutdown()
            return

        try:
            last_hour = None
            print("Loop ativo - Sinais a cada H1 | Logs e trailing a cada 1 min. CTRL+C para sair.\n")
            while True:
                now = datetime.now()
                update_pending_logs()           # Checa fechamentos a cada minuto
                self.manage_open_positions()     # Trailing stop + RSI exit a cada minuto
                if last_hour != now.hour:
                    cycle()
                    last_hour = now.hour
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nSaindo do Hybrid Bot...")
        finally:
            mt5.shutdown()


# =============================================================================
# LOGGING CSV
# =============================================================================

def init_log():
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Ticket", "DataHora", "Ativo", "Tipo", "Direcao", "Entrada", "SL", "TP", "Regime", "VolRatio", "Status", "Lucro"]
            )

def log_trade_open(ticket, symbol, signal, entry, sl, tp):
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            ticket,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            signal["type"],
            signal["direction"],
            entry,
            sl,
            tp if tp else "TRAIL",
            signal["type"],
            signal["vol_ratio"],
            "ABERTO",
            0.0,
        ])

def update_pending_logs():
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    if not os.path.exists(path):
        return

    rows = []
    updated = False
    try:
        with open(path, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header:
                return
            rows.append(header)
            for row in reader:
                if len(row) >= 12 and row[10] == "ABERTO":
                    ticket = int(row[0])
                    pos = mt5.positions_get(ticket=ticket)
                    if pos is None or len(pos) == 0:
                        from_date = datetime.now() - timedelta(days=30)
                        to_date = datetime.now() + timedelta(days=1)
                        deals = mt5.history_deals_get(from_date, to_date)
                        if deals:
                            my_deals = [d for d in deals if d.position_id == ticket]
                            if my_deals:
                                total = sum(d.profit for d in my_deals)
                                row[10] = "FECHADO"
                                row[11] = str(round(total, 2))
                                updated = True
                                prefix = "+" if total > 0 else ""
                                print(f"\n[DIÁRIO] Trade de {row[2]} fechou!")
                                print(f"  Ticket: {ticket} | Lucro: {prefix}${total:.2f}\n")
                rows.append(row)

        if updated:
            with open(path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
    except Exception as e:
        print(f"[!] Erro atualizando log CSV: {e}")


# =============================================================================
# MAIN
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser("MT5 Hybrid Bot - RSI + Breakout + Volume")
    parser.add_argument("--symbols", type=str, default="XAUUSD,XAGUSD,EURUSD", help="Símbolos separados por vírgula")
    parser.add_argument("--exchange", type=str, choices=["demo", "real"], default="demo")
    parser.add_argument("--lot", type=float, default=0.1, help="Volume base")
    parser.add_argument("--max-trades", type=int, default=2, help="Limite máximo de trades simultâneos")
    parser.add_argument("--loop", action="store_true", help="Modo loop contínuo H1")
    args = parser.parse_args()
    BOT_NAME = "BOT HYBRID RSI + Breakout + Volume"

    os.system(f'title {BOT_NAME}')
    symbols = [s.strip() for s in args.symbols.split(",")]
    bot = HybridBot(symbols=symbols, lot_size=args.lot, exchange=args.exchange, max_trades=args.max_trades)
    bot.run(loop=args.loop)
