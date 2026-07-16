"""
mt5_rsi_breakout_bot.py - Estratégia: RSI Mean-Reversion + Breakout (sem Volume)
Traduzido do Pine Script "Hybrid: RSI + Breakout + Dashboard" (© RugSurvivor)

Diferenças do mt5_hybrid_bot.py:
  - SEM filtro de volume (fiel ao Pine original)
  - Trailing Stop ATR nos Breakouts + trailing candle nos RSI (após 1% lucro)
  - Log CSV no mesmo formato padrão

Magic Number: 888333
"""
import os, time, argparse, json, csv, sys
import numpy as np
import MetaTrader5 as mt5
from datetime import datetime, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

MAGIC_NUMBER = 888333
CSV_LOG_FILE = "bot_rsi_breakout_log.csv"

# ── Indicadores ──────────────────────────────────────────────────────────────

def calc_ema(data, period):
    ema = np.full_like(data, np.nan, dtype=float)
    ema[period - 1] = np.mean(data[:period])
    mult = 2.0 / (period + 1)
    for i in range(period, len(data)):
        ema[i] = data[i] * mult + ema[i - 1] * (1 - mult)
    return ema

def calc_rsi(close, period=14):
    deltas = np.diff(close)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    rsi = np.full(len(close), np.nan)
    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])
    rsi[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rsi[i + 1] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return rsi

def calc_atr(high, low, close, period=14):
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i], abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
    atr = np.full_like(tr, np.nan)
    atr[period - 1] = np.mean(tr[:period])
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period
    return atr

def calc_adx(high, low, close, period=14):
    n = len(close)
    alpha = 2.0 / (period + 1)
    pd_buf, nd_buf = np.zeros(n), np.zeros(n)
    plus_di, minus_di = np.zeros(n), np.zeros(n)
    dx_buf, adx = np.zeros(n), np.zeros(n)
    for i in range(1, n):
        tp = high[i] - high[i-1]
        tn = low[i-1] - low[i]
        if tp < 0: tp = 0.0
        if tn < 0: tn = 0.0
        if tp > tn:   tn = 0.0
        elif tp < tn: tp = 0.0
        else:         tp = tn = 0.0
        tr_val = max(abs(high[i]-low[i]), abs(high[i]-close[i-1]), abs(low[i]-close[i-1]))
        if tr_val != 0:
            pd_buf[i] = 100.0 * tp / tr_val
            nd_buf[i] = 100.0 * tn / tr_val
        plus_di[i]  = plus_di[i-1]  + alpha * (pd_buf[i] - plus_di[i-1])
        minus_di[i] = minus_di[i-1] + alpha * (nd_buf[i] - minus_di[i-1])
        denom = plus_di[i] + minus_di[i]
        if denom != 0:
            dx_buf[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom
        adx[i] = adx[i-1] + alpha * (dx_buf[i] - adx[i-1])
    return adx, plus_di, minus_di

# ── CSV Logging ──────────────────────────────────────────────────────────────

def _log_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), CSV_LOG_FILE)

def init_log():
    p = _log_path()
    if not os.path.exists(p):
        with open(p, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(
                ["Ticket","DataHora","Ativo","Tipo","Direcao","Entrada","SL","TP","Regime","Status","Lucro"]
            )

def log_trade_open(ticket, symbol, signal, entry, sl, tp, regime):
    with open(_log_path(), "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            ticket,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            signal["type"],
            signal["direction"],
            entry, sl,
            tp if tp else "TRAIL",
            regime,
            "ABERTO", 0.0,
        ])

def update_pending_logs():
    p = _log_path()
    if not os.path.exists(p):
        return
    rows, updated = [], False
    try:
        with open(p, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header: return
            rows.append(header)
            for row in reader:
                if len(row) >= 11 and row[9] == "ABERTO":
                    ticket = int(row[0])
                    pos = mt5.positions_get(ticket=ticket)
                    if pos is None or len(pos) == 0:
                        deals = mt5.history_deals_get(
                            datetime.now() - timedelta(days=30),
                            datetime.now() + timedelta(days=1))
                        if deals:
                            my = [d for d in deals if d.position_id == ticket]
                            if my:
                                total = sum(d.profit for d in my)
                                row[9] = "FECHADO"
                                row[10] = str(round(total, 2))
                                updated = True
                                pfx = "+" if total > 0 else ""
                                print(f"\n[DIÁRIO] Trade {row[2]} fechou! Ticket: {ticket} | Lucro: {pfx}${total:.2f}\n")
                rows.append(row)
        if updated:
            with open(p, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerows(rows)
    except Exception as e:
        print(f"[!] Erro CSV: {e}")

# ── Bot Principal ────────────────────────────────────────────────────────────

class RSIBreakoutBot:
    def __init__(self, symbols, lot_size=0.1, exchange="demo", max_trades=2):
        self.symbols = symbols
        self.lot_size = lot_size
        self.exchange = exchange
        self.max_trades = max_trades
        # Parâmetros Pine
        self.ema_period      = 200
        self.rsi_period      = 14
        self.rsi_buy         = 40
        self.rsi_sell        = 60
        self.rsi_exit        = 50
        self.adx_period      = 14
        self.adx_threshold   = 20   # Pine default
        self.atr_period      = 14
        self.atr_trail_mult  = 2.0  # Pine: atrMult
        self.breakout_lookback = 20
        self.atr_sl_rsi      = 1.5  # SL para RSI (melhoria, Pine não tinha)
        self.atr_sl_breakout = 2.0
        self.tp_rr_ratio     = 2.0
        # Trailing candle (gatilho % lucro)
        self.trail_trigger_pct = 1.0
        
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

    # ── Conexão MT5 ──
    def connect(self):
        print("[*] Conectando ao MetaTrader 5...")
        try:
            DIR = os.path.dirname(os.path.abspath(__file__))
            acesso_path = os.path.join(DIR, "acesso.json")
            if not os.path.exists(acesso_path):
                acesso_path = os.path.join(os.path.dirname(os.path.dirname(DIR)), "acesso.json")
            with open(acesso_path) as f:
                acesso = json.load(f)
            if self.exchange == "real":
                conta = next((c for c in acesso if "Real" in c.get("plataforma","")), None)
                if not conta and len(acesso) > 2: conta = acesso[2]
            else:
                conta = next((c for c in acesso if "FBS_Demo_RSI" in c.get("plataforma","") and "Real" not in c.get("plataforma","")), None)
                if not conta and len(acesso) > 1: conta = acesso[3]
            if not conta: conta = acesso[3]
            user, pw = conta["login"], conta["senha"]
            server = conta.get("server", "FBS-Real" if "Real" in conta.get("plataforma","") else "FBS-Demo")
            terminal_path = conta.get("terminal", "")
            print(f"[*] Auth: {user} @ {server}")
            if terminal_path:
                print(f"[*] Terminal path: {terminal_path}")
            init_kwargs = {"login": user, "password": pw, "server": server}
            if terminal_path:
                init_kwargs["path"] = terminal_path
            if not mt5.initialize(**init_kwargs):
                print(f"[!] Falha fatal: não conseguiu conectar ao terminal {terminal_path}. Código: {mt5.last_error()}")
                return False
            if not mt5.login(login=user, password=pw, server=server):
                print(f"[!] Login falhou: {mt5.last_error()}"); return False
        except Exception as e:
            print(f"[!] Erro credentials: {e}")
            return False
        print(f"[*] Conectado! MT5 {mt5.version()}")
        return True

    # ── Indicadores ──
    def calculate_indicators(self, symbol):
        rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
        if rates is None or len(rates) < self.ema_period + 10:
            print(f"[!] Dados insuficientes {symbol}"); return None
        close = np.array([r['close'] for r in rates], dtype=float)
        high  = np.array([r['high']  for r in rates], dtype=float)
        low   = np.array([r['low']   for r in rates], dtype=float)
        ema = calc_ema(close, self.ema_period)
        rsi = calc_rsi(close, self.rsi_period)
        adx, pdi, mdi = calc_adx(high, low, close, self.adx_period)
        atr = calc_atr(high, low, close, self.atr_period)
        highest_brk = np.max(close[-self.breakout_lookback - 1 : -1])
        lowest_brk  = np.min(close[-self.breakout_lookback - 1 : -1])
        return {
            "close": close[-1], "high": high[-1], "low": low[-1],
            "prev_high": high[-2], "prev_low": low[-2],
            "ema200": ema[-1],
            "rsi": round(rsi[-1], 2) if not np.isnan(rsi[-1]) else 50.0,
            "adx": round(adx[-1], 2),
            "atr": round(atr[-1], 5),
            "highest_break": highest_brk, "lowest_break": lowest_brk,
        }

    # ── Geração de Sinal ──
    def generate_signal(self, ind):
        bullish  = ind["close"] > ind["ema200"]
        bearish  = ind["close"] < ind["ema200"]
        trending = ind["adx"] > self.adx_threshold
        ranging  = not trending

        # RSI Mean-Reversion (ranging)
        if ranging:
            if ind["rsi"] < self.rsi_buy and bullish:
                return {"type": "RSI", "direction": "LONG",  "sl_mult": self.atr_sl_rsi}
            if ind["rsi"] > self.rsi_sell and bearish:
                return {"type": "RSI", "direction": "SHORT", "sl_mult": self.atr_sl_rsi}

        # Breakout (trending)
        if trending:
            if bullish and ind["close"] > ind["highest_break"]:
                return {"type": "BREAKOUT", "direction": "LONG",  "sl_mult": self.atr_sl_breakout}
            if bearish and ind["close"] < ind["lowest_break"]:
                return {"type": "BREAKOUT", "direction": "SHORT", "sl_mult": self.atr_sl_breakout}
        return None

    # ── Executar Ordem ──
    def execute_trade(self, symbol, signal, atr_val):
        tick = mt5.symbol_info_tick(symbol)
        info = mt5.symbol_info(symbol)
        if not tick or not info: return None
        digits = info.digits
        point  = info.point
        min_sl = max(info.trade_stops_level * point, 10 * point)
        sl_dist = max(atr_val * signal["sl_mult"], min_sl)
        regime = "TREND" if signal["type"] == "BREAKOUT" else "RANGE"

        if signal["direction"] == "LONG":
            entry = tick.ask
            sl = round(entry - sl_dist, digits)
            tp = round(entry + sl_dist * self.tp_rr_ratio, digits) if signal["type"] == "RSI" else 0.0
            otype = mt5.ORDER_TYPE_BUY
        else:
            entry = tick.bid
            sl = round(entry + sl_dist, digits)
            tp = round(entry - sl_dist * self.tp_rr_ratio, digits) if signal["type"] == "RSI" else 0.0
            otype = mt5.ORDER_TYPE_SELL

        comment = f"RB {signal['type']} {signal['direction'][:1]}"
        print(f"\n[EXEC] {comment} {symbol} | Entry:{entry} SL:{sl} TP:{tp if tp else 'TRAIL'}")

        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": symbol,
            "volume": float(self.lot_size), "type": otype, "price": entry,
            "sl": sl, "tp": tp, "deviation": 20, "magic": MAGIC_NUMBER,
            "comment": comment, "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_FOK,
        }
        result = mt5.order_send(req)
        if result.retcode != mt5.TRADE_RETCODE_DONE:
            print(f"  [!] Falha: {result.retcode} - {result.comment}")
            if result.retcode == 10027:
                print("  [DICA] Ative 'Algo Trading' no MetaTrader 5!")
        else:
            print(f"  [$$] OK! Ticket: {result.order}")
            log_trade_open(result.order, symbol, signal, entry, sl, tp, regime)
            if self.telegram:
                msg = (f"✅ <b>{comment} {symbol} EXECUTADA!</b>\n"
                       f"Preço: {entry:.{digits}f}\n"
                       f"SL: {sl:.{digits}f} | TP: {tp if tp else 'TRAIL'}\n"
                       f"Lote: {self.lot_size}")
                self.telegram.send_message(msg)
        return result

    # ── Gestão de Posições (Trailing + RSI Exit) ──
    def manage_positions(self):
        positions = mt5.positions_get()
        if not positions: return
        my_pos = [p for p in positions if p.magic == MAGIC_NUMBER]
        if not my_pos: return

        for pos in my_pos:
            comment = pos.comment
            symbol  = pos.symbol
            is_long = pos.type == mt5.POSITION_TYPE_BUY
            entry   = pos.price_open
            cur     = pos.price_current
            cur_sl  = pos.sl

            # ── RSI EXIT ──
            if "RSI" in comment:
                ind = self.calculate_indicators(symbol)
                if ind is None: continue
                rsi_now = ind["rsi"]
                should_close = (is_long and rsi_now > self.rsi_exit) or (not is_long and rsi_now < self.rsi_exit)
                if should_close:
                    print(f"  [RSI EXIT] {symbol} {'Long' if is_long else 'Short'} RSI={rsi_now}")
                    self._close_position(pos)
                    continue
                # Trailing candle para RSI após 1% lucro
                self._trail_candle(pos, ind, is_long, entry, cur, cur_sl)

            # ── BREAKOUT TRAILING (ATR) ──
            elif "BREAKOUT" in comment:
                ind = self.calculate_indicators(symbol)
                if ind is None: continue
                atr_now = ind["atr"]
                trail_dist = atr_now * self.atr_trail_mult
                digits = mt5.symbol_info(symbol).digits
                if is_long:
                    new_sl = round(ind["high"] - trail_dist, digits)
                    if new_sl > cur_sl:
                        self._modify_sl(pos, new_sl)
                        print(f"  [ATR TRAIL] {symbol} Long SL: {cur_sl}->{new_sl}")
                else:
                    new_sl = round(ind["low"] + trail_dist, digits)
                    if new_sl < cur_sl:
                        self._modify_sl(pos, new_sl)
                        print(f"  [ATR TRAIL] {symbol} Short SL: {cur_sl}->{new_sl}")
                # Candle trailing adicional após 1% lucro
                self._trail_candle(pos, ind, is_long, entry, cur, cur_sl)

    def _trail_candle(self, pos, ind, is_long, entry, cur, cur_sl):
        """Move SL para extremidade da vela anterior quando lucro >= 1%."""
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
        if not info: return
        digits = info.digits
        min_dist = max(info.trade_stops_level * info.point, 10 * info.point)

        if is_long:
            new_sl = round(ind["prev_low"], digits)
            if new_sl > cur_sl and (cur - new_sl) >= min_dist:
                self._modify_sl(pos, new_sl)
                print(f"  [CANDLE TRAIL] {pos.symbol} Long SL->{new_sl} (lucro ~{max(pct,pct_acct):.1f}%)")
        else:
            new_sl = round(ind["prev_high"], digits)
            if (cur_sl == 0 or new_sl < cur_sl) and (new_sl - cur) >= min_dist:
                self._modify_sl(pos, new_sl)
                print(f"  [CANDLE TRAIL] {pos.symbol} Short SL->{new_sl} (lucro ~{max(pct,pct_acct):.1f}%)")

    def _close_position(self, pos):
        tick = mt5.symbol_info_tick(pos.symbol)
        ctype = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if ctype == mt5.ORDER_TYPE_SELL else tick.ask
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": pos.symbol,
               "volume": pos.volume, "type": ctype, "position": pos.ticket,
               "price": price, "magic": MAGIC_NUMBER, "comment": "RB Close"}
        res = mt5.order_send(req)
        if res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"  [FECHADO] Ticket {pos.ticket}")
        else:
            print(f"  [!] Falha fechar {pos.ticket}: {res.comment}")

    def _modify_sl(self, pos, new_sl):
        mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "symbol": pos.symbol,
                        "position": pos.ticket, "sl": new_sl, "tp": pos.tp})

    def has_position(self, symbol):
        positions = mt5.positions_get(symbol=symbol)
        return positions and any(p.magic == MAGIC_NUMBER for p in positions)

    def count_open_positions(self):
        positions = mt5.positions_get()
        if not positions:
            return 0
        return sum(1 for p in positions if p.magic == MAGIC_NUMBER)

    # ── Loop Principal ──
    def run(self, loop=False):
        if not self.connect(): return
        active = [s for s in self.symbols if mt5.symbol_select(s, True)]
        if not active:
            print("[!] Nenhum símbolo ativo."); mt5.shutdown(); return
        init_log()

        print("\n" + "=" * 55)
        print("  [RSI+BREAKOUT BOT] Sem Volume (Pine RugSurvivor)")
        print("=" * 55)
        print(f"  Modo: {'LOOP H1' if loop else 'ONE-SHOT'} | Conta: {self.exchange.upper()}")
        print(f"  Lote: {self.lot_size} | Ativos: {', '.join(active)}")
        print(f"  Magic: {MAGIC_NUMBER}")
        print("=" * 55 + "\n")
        
        if self.telegram:
            self.telegram.send_message(f"🤖 <b>RSI+BREAKOUT BOT INICIADO</b>\nAtivos: {', '.join(active)}")

        def cycle():
            update_pending_logs()
            self.manage_positions()
            for sym in active:
                ind = self.calculate_indicators(sym)
                if ind is None: continue
                regime = "TREND" if ind["adx"] > self.adx_threshold else "RANGE"
                bias   = "Bull" if ind["close"] > ind["ema200"] else "Bear"
                now_s  = datetime.now().strftime("%H:%M:%S")
                print(f"\n[{now_s}] {sym} | {regime} (ADX={ind['adx']}) | Bias: {bias}")
                print(f"  RSI={ind['rsi']} | ATR={ind['atr']:.2f}")
                sig = self.generate_signal(ind)
                if sig is None:
                    print("  -> Sem sinal."); continue
                print(f"  -> SINAL: {sig['type']} {sig['direction']}")
                
                open_count = self.count_open_positions()
                if open_count >= self.max_trades:
                    print(f"  [GATE] Limite de trades simultâneos atingido ({open_count}/{self.max_trades}). Bloqueando trade em {sym}.")
                    if self.telegram:
                        self.telegram.send_message(
                            f"⚠️ <b>[RSI+BREAKOUT] Sinal em {sym} Ignorado!</b>\n"
                            f"Detectado sinal de {sig['direction']} ({sig['type']}), mas o limite de {self.max_trades} trades simultâneos está preenchido."
                        )
                    continue

                self.execute_trade(sym, sig, ind["atr"])

        if not loop:
            cycle(); mt5.shutdown(); return

        try:
            last_h = None
            print("Loop ativo - Sinais H1 | Trailing/logs cada 1 min. CTRL+C sair.\n")
            while True:
                now = datetime.now()
                update_pending_logs()
                self.manage_positions()
                if last_h != now.hour:
                    cycle(); last_h = now.hour
                time.sleep(60)
        except KeyboardInterrupt:
            print("\nSaindo do RSI+Breakout Bot...")
        finally:
            mt5.shutdown()

# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser("MT5 RSI+Breakout Bot (Pine RugSurvivor)")
    parser.add_argument("--symbols", type=str, default="XAUUSD,XAGUSD,EURUSD")
    parser.add_argument("--exchange", type=str, choices=["demo","real"], default="demo")
    parser.add_argument("--lot", type=float, default=0.1)
    parser.add_argument("--max-trades", type=int, default=2, help="Limite máximo de trades simultâneos")
    parser.add_argument("--loop", action="store_true", help="Modo loop contínuo H1")
    args = parser.parse_args()
    BOT_NAME = "BOT RSI BREAKOU"

    os.system(f'title {BOT_NAME}')
    syms = [s.strip() for s in args.symbols.split(",")]
    bot = RSIBreakoutBot(symbols=syms, lot_size=args.lot, exchange=args.exchange, max_trades=args.max_trades)
    bot.run(loop=args.loop)
