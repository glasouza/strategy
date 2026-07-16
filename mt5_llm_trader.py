import os
import time
import argparse
import json
import csv
import numpy as np
import MetaTrader5 as mt5
from datetime import datetime, timedelta
import importlib.util
import sys
import subprocess

# Auto-instalador de dependências essenciais no ambiente virtual
try:
    import pydantic
    import openai
    import requests
    from dotenv import load_dotenv
except ImportError:
    print("[*] Dependências ausentes detectadas no ambiente. Instalando pydantic, openai, python-dotenv e requests...")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "pydantic", "openai", "python-dotenv", "requests"], check=True)
        import importlib
        importlib.invalidate_caches()
        from dotenv import load_dotenv
        print("[*] Dependências instaladas com sucesso!")
    except Exception as e:
        print(f"[!] Erro ao tentar instalar dependências automaticamente: {e}")

try:
    load_dotenv()
except Exception:
    pass

# Redundância de segurança / Fallback manual:
# Se as variáveis de ambiente essenciais ainda não foram carregadas,
# lê diretamente o arquivo .env no diretório deste script.
if not os.getenv('DEEPSEEK_API_KEY') and not os.getenv('OPENAI_API_KEY'):
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    env_file_path = os.path.join(DIR_SCRIPT, ".env")
    if os.path.exists(env_file_path):
        try:
            with open(env_file_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip()
                        # Remove aspas se houver
                        if val.startswith(('"', "'")) and val.endswith(('"', "'")):
                            val = val[1:-1]
                        os.environ[key] = val
        except Exception as e:
            print(f"[!] Erro ao carregar .env manualmente: {e}")

CSV_LOG_FILE = "bot_trades_log.csv"
telegram = None

def init_log():
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    if not os.path.exists(log_path):
        with open(log_path, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(["Ticket", "DataHora", "Ativo", "Direcao", "Entrada", "SL", "TP", "Status", "Lucro"])

def log_trade_open(ticket, symbol, direction, entry, sl, tp):
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    with open(log_path, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([ticket, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, direction, entry, sl, tp, "ABERTO", 0.0])

def update_pending_logs():
    DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
    log_path = os.path.join(DIR_SCRIPT, CSV_LOG_FILE)
    if not os.path.exists(log_path): return
    
    rows = []
    updated = False
    try:
        with open(log_path, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if not header: return
            rows.append(header)
            for row in reader:
                if len(row) < 9: 
                    rows.append(row)
                    continue
                    
                ticket, data, symbol, direcao, entrada, sl, tp, status, lucro = row
                if status == "ABERTO":
                    position_id = int(ticket)
                    # Verifica se a posicao continua aberta
                    pos = mt5.positions_get(ticket=position_id)
                    if pos is None or len(pos) == 0:
                        # Posicao fechada. Puxa historico via deal
                        from_date = datetime.now() - timedelta(days=30)
                        to_date = datetime.now() + timedelta(days=1)
                        deals = mt5.history_deals_get(from_date, to_date)
                        if deals:
                            my_deals = [d for d in deals if d.position_id == position_id]
                            if my_deals:
                                total_profit = sum(d.profit for d in my_deals)
                                status = "FECHADO"
                                lucro = round(total_profit, 2)
                                updated = True
                                prefix = "+" if lucro > 0 else ""
                                print(f"\n[DIÁRIO] Um trade ativo de {symbol} atingiu sua saída!")
                                print(f"         Ticket Original: {ticket}")
                                print(f"         Lucro Fechado: {prefix}${lucro}\n")
                rows.append([ticket, data, symbol, direcao, entrada, sl, tp, status, str(lucro)])
                
        if updated:
            with open(log_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerows(rows)
    except Exception as e:
        print(f"[!] Falha silenciosa ao atualizar diário CSV: {e}")

def load_llm_module():
    import llm_setup_creator
    return llm_setup_creator

def calculate_simple_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 'N/A'
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-(i+1)]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)

# ---------------------------------------------------------------------------
# INDICADORES TÉCNICOS (mesma base do hybrid bot, réplica do MT5)
# ---------------------------------------------------------------------------

def calc_ema(data, period):
    """EMA padrão."""
    alpha = 2.0 / (period + 1)
    ema = np.full_like(data, np.nan, dtype=float)
    ema[0] = data[0]
    for i in range(1, len(data)):
        ema[i] = alpha * data[i] + (1 - alpha) * ema[i - 1]
    return ema

def calc_atr(high, low, close, period=14):
    """ATR via Wilder Smoothing."""
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
    """ADX, +DI, -DI - réplica do ADX.mq5 (MetaTrader 5)."""
    n = len(close)
    alpha = 2.0 / (period + 1)
    pd_buf = np.zeros(n)
    nd_buf = np.zeros(n)
    plus_di = np.zeros(n)
    minus_di = np.zeros(n)
    dx_buf = np.zeros(n)
    adx = np.zeros(n)

    for i in range(1, n):
        tmp_pos = high[i] - high[i - 1]
        tmp_neg = low[i - 1] - low[i]
        if tmp_pos < 0.0: tmp_pos = 0.0
        if tmp_neg < 0.0: tmp_neg = 0.0
        if tmp_pos > tmp_neg:
            tmp_neg = 0.0
        elif tmp_pos < tmp_neg:
            tmp_pos = 0.0
        else:
            tmp_pos = 0.0
            tmp_neg = 0.0
        tr = max(abs(high[i] - low[i]), abs(high[i] - close[i-1]), abs(low[i] - close[i-1]))
        if tr != 0.0:
            pd_buf[i] = 100.0 * tmp_pos / tr
            nd_buf[i] = 100.0 * tmp_neg / tr
        plus_di[i] = plus_di[i-1] + alpha * (pd_buf[i] - plus_di[i-1])
        minus_di[i] = minus_di[i-1] + alpha * (nd_buf[i] - minus_di[i-1])
        denom = plus_di[i] + minus_di[i]
        if denom != 0.0:
            dx_buf[i] = 100.0 * abs(plus_di[i] - minus_di[i]) / denom
        adx[i] = adx[i-1] + alpha * (dx_buf[i] - adx[i-1])
    return adx, plus_di, minus_di

def calc_sma(data, period):
    """SMA simples."""
    sma = np.full_like(data, np.nan, dtype=float)
    for i in range(period - 1, len(data)):
        sma[i] = np.mean(data[i - period + 1 : i + 1])
    return sma

def calculate_technical_context(symbol, ema_period=200, adx_period=14, atr_period=14, vol_period=20):
    """Calcula todos os indicadores técnicos para um ativo e retorna dict."""
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 300)
    if rates is None or len(rates) < ema_period + 10:
        return None

    close = np.array([r['close'] for r in rates], dtype=float)
    high = np.array([r['high'] for r in rates], dtype=float)
    low = np.array([r['low'] for r in rates], dtype=float)
    vol = np.array([r['tick_volume'] for r in rates], dtype=float)

    ema = calc_ema(close, ema_period)
    rsi_arr = calculate_simple_rsi(list(close[-16:]), 14)
    adx, plus_di, minus_di = calc_adx(high, low, close, adx_period)
    atr = calc_atr(high, low, close, atr_period)
    vol_sma = calc_sma(vol, vol_period)

    v_sma = vol_sma[-1] if not np.isnan(vol_sma[-1]) else 1.0

    return {
        "close": close[-1],
        "ema200": round(ema[-1], 5),
        "rsi": rsi_arr if isinstance(rsi_arr, (int, float)) else 50.0,
        "adx": round(adx[-1], 2),
        "plus_di": round(plus_di[-1], 2),
        "minus_di": round(minus_di[-1], 2),
        "atr": round(atr[-1], 5),
        "tick_vol": vol[-1],
        "vol_sma": round(v_sma, 2),
        "vol_ratio": round(vol[-1] / v_sma, 2) if v_sma > 0 else 1.0,
        "price_vs_ema": "ACIMA" if close[-1] > ema[-1] else "ABAIXO",
        "regime": "TREND" if adx[-1] > 25 else "RANGE",
        "bias": "Bull" if plus_di[-1] > minus_di[-1] else "Bear",
    }

def connect_mt5(exchange="demo"):
    print("[*] Lendo acesso.json e Conectando ao MetaTrader 5...")
    try:
        DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
        acesso_path = os.path.join(DIR_SCRIPT, "acesso.json")
        if not os.path.exists(acesso_path):
            acesso_path = os.path.join(os.path.dirname(os.path.dirname(DIR_SCRIPT)), "acesso.json")
            
        with open(acesso_path) as log:
            acesso = json.load(log)
            
        if exchange.lower() == "real":
            conta = next((item for item in acesso if "Real" in item.get("plataforma", "")), None)
            if not conta and len(acesso) > 2: conta = acesso[5]
        else:
            conta = next((item for item in acesso if "FBS_Demo_llm_trader" in item.get("plataforma", "") and "Real" not in item.get("plataforma", "")), None)
            if not conta and len(acesso) > 1: conta = acesso[5]
            
        if not conta:
            conta = acesso[5]
            
        user = conta["login"]
        password = conta["senha"]
        server = conta.get("server")
        terminal_path = conta.get("terminal", "")
        
        if not server:
            server = "FBS-Real" if "Real" in conta.get("plataforma", "") else "FBS-Demo"
            
        print(f"[*] Autenticando: Conta {user} | Server {server} ...")
        if terminal_path:
            print(f"[*] Terminal path: {terminal_path}")
        
        init_kwargs = {"login": user, "password": password, "server": server}
        if terminal_path:
            init_kwargs["path"] = terminal_path
        
        if not mt5.initialize(**init_kwargs):
            print(f"[!] Falha fatal: não conseguiu conectar ao terminal {terminal_path}. Código: {mt5.last_error()}")
            return False
                
        authorized = mt5.login(login=user, password=password, server=server)
        if not authorized:
            print(f"[!] Falha no mt5.login para a conta {user} no servidor {server}. Erro: {mt5.last_error()}")
            return False
            
    except Exception as e:
        print(f"[!] Erro ao ler credentials: {e}")
        return False

    print(f"[*] Conectado Vencedoramente! (MT5 Version: {mt5.version()})")
    return True

def execute_llm_trade(symbol, lot_size, direction, trigger_price, stop_loss, **kwargs):
    """Executa a ordem na corretora baseada na saída estruturada da IA."""
    tick = mt5.symbol_info_tick(symbol)
    symbol_info = mt5.symbol_info(symbol)
    if not tick or not symbol_info:
        print(f"[!] Tick/Symbol info indisponível para {symbol}.")
        return None

    digits = symbol_info.digits
    point = symbol_info.point
    min_stop_dist = max(symbol_info.trade_stops_level * point, 10 * point)

    allowed_deviation = 500 * point 
    tp_ratio = 2.0

    # ATR mínimo para SL (passado como kwarg opcional)
    atr_value = kwargs.get('atr_value', 0)
    
    if direction == "LONG":
        entry_price = tick.ask
        if abs(entry_price - trigger_price) > allowed_deviation:
            print(f"[!] Oportunidade perdida ou Trigger {trigger_price} difere muito do Ask atual {entry_price}. Abortando boleta.")
            return None
            
        sl = stop_loss if stop_loss < entry_price and (entry_price - stop_loss) >= min_stop_dist else entry_price - (500 * point)
        # Forçar SL mínimo de 1.5x ATR
        if atr_value > 0:
            sl_min_dist = atr_value * 1.5
            if (entry_price - sl) < sl_min_dist:
                sl = round(entry_price - sl_min_dist, digits)
                print(f"  [ATR GUARD] SL ajustado para 1.5xATR: {sl} (dist={sl_min_dist:.2f})")
        risk = entry_price - sl
        tp = entry_price + (risk * tp_ratio)
        order_type = mt5.ORDER_TYPE_BUY
        
    elif direction == "SHORT":
        entry_price = tick.bid
        if abs(entry_price - trigger_price) > allowed_deviation:
            print(f"[!] Oportunidade perdida ou Trigger {trigger_price} difere muito do Bid atual {entry_price}. Abortando boleta.")
            return None
            
        sl = stop_loss if stop_loss > entry_price and (stop_loss - entry_price) >= min_stop_dist else entry_price + (500 * point)
        # Forçar SL mínimo de 1.5x ATR
        if atr_value > 0:
            sl_min_dist = atr_value * 1.5
            if (sl - entry_price) < sl_min_dist:
                sl = round(entry_price + sl_min_dist, digits)
                print(f"  [ATR GUARD] SL ajustado para 1.5xATR: {sl} (dist={sl_min_dist:.2f})")
        risk = sl - entry_price
        tp = entry_price - (risk * tp_ratio)
        order_type = mt5.ORDER_TYPE_SELL
        
    else:
        return None

    sl = round(sl, digits)
    tp = round(tp, digits)
    
    print(f"\n[ORQUESTRAÇÃO] Preparando ordem {direction} em {symbol}")
    print(f" => Lote: {lot_size}")
    print(f" => Entrada (Mercado): {entry_price}")
    print(f" => Stop Loss (AI): {sl}")
    print(f" => Take Profit (2.0x): {tp}")

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot_size),
        "type": order_type,
        "price": entry_price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": 999111,
        "comment": "DeepSeek Auto",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_FOK,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[!] Falha na Injeção de Ordem.")
        print(f"    Retcode: {result.retcode} - Descrição: {result.comment}")
        if result.retcode == 10027:
            print("    [!] DICA: Erro 10027 significa que você esqueceu de apertar o botão vermelho 'Algo Trading' / 'AutoTrading' lá no topo do seu terminal MetaTrader! Ligue ele e tente de novo.")
    else:
        print(f"[$$] ORDEM PREENCHIDA COM SUCESSO! Ticket: {result.order}")
        log_trade_open(result.order, symbol, direction, entry_price, sl, tp)
        if telegram:
            msg = (f"✅ <b>[LLM] {direction} {symbol} EXECUTADA!</b>\n"
                   f"Preço: {entry_price:.{digits}f}\n"
                   f"SL: {sl:.{digits}f} | TP: {tp:.{digits}f}\n"
                   f"Lote: {lot_size}")
            telegram.send_message(msg)
        
    return result

def manage_trailing_stops():
    """
    Quando o trade atinge 1% a 2% de lucro (considerando movimento de preço ou saldo da conta),
    move o SL para a mínima da vela anterior (LONG) ou máxima da vela anterior (SHORT).
    """
    positions = mt5.positions_get()
    if not positions:
        return
        
    for pos in positions:
        if pos.magic != 999111:
            continue
            
        symbol = pos.symbol
        pos_type = pos.type
        entry_price = pos.price_open
        current_price = pos.price_current
        current_sl = pos.sl
        
        # 1. Lucro em % de movimento de preço
        if pos_type == mt5.POSITION_TYPE_BUY:
            profit_price_pct = ((current_price - entry_price) / entry_price) * 100
        elif pos_type == mt5.POSITION_TYPE_SELL:
            profit_price_pct = ((entry_price - current_price) / entry_price) * 100
        else:
            continue
            
        # 2. Lucro em % do saldo da conta
        account_info = mt5.account_info()
        profit_account_pct = 0.0
        if account_info and account_info.balance > 0:
            profit_account_pct = (pos.profit / account_info.balance) * 100
            
        # Gatilho de >= 1.0% de lucro (atende Forex/Crypto)
        if profit_price_pct >= 1.0 or profit_account_pct >= 1.0:
            rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_H1, 0, 2)
            if rates is None or len(rates) < 2:
                continue
                
            prev_candle = rates[0]  # índice 0 é a vela fechada anterior
            
            symbol_info = mt5.symbol_info(symbol)
            if not symbol_info:
                continue
                
            digits = symbol_info.digits
            point = symbol_info.point
            min_stop_dist = max(symbol_info.trade_stops_level * point, 10 * point)
            
            new_sl = current_sl
            
            if pos_type == mt5.POSITION_TYPE_BUY:
                min_prev = prev_candle['low']
                # Atualiza se o novo stop (minima anterior) for mais protetor e válido
                if min_prev > current_sl and (current_price - min_prev) >= min_stop_dist:
                    new_sl = round(min_prev, digits)
                    
            elif pos_type == mt5.POSITION_TYPE_SELL:
                max_prev = prev_candle['high']
                # Atualiza se o novo stop (maxima anterior) for mais protetor e válido
                if (current_sl == 0.0 or max_prev < current_sl) and (max_prev - current_price) >= min_stop_dist:
                    new_sl = round(max_prev, digits)
                    
            if new_sl != current_sl and new_sl > 0:
                request = {
                    "action": mt5.TRADE_ACTION_SLTP,
                    "position": pos.ticket,
                    "symbol": symbol,
                    "sl": new_sl,
                    "tp": pos.tp,
                    "magic": 999111
                }
                res = mt5.order_send(request)
                if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                    print(f"\n[*] [TRAILING STOP H1] {symbol} (Ticket: {pos.ticket}) ajustado! Novo SL na extremidade da vela anterior: {new_sl} | Lucro: ~{max(profit_price_pct, profit_account_pct):.2f}%")

def count_open_positions(magic):
    positions = mt5.positions_get()
    if not positions:
        return 0
    return sum(1 for p in positions if p.magic == magic)

def main():
    init_log()  # Garante que o arquivo CSV Base exista

    parser = argparse.ArgumentParser("MT5 DeepSeek Auto-Trader Orchestrator")
    parser.add_argument("--symbols", type=str, default="XAUUSD,XAGUSD,EURUSD", help="Symbols separados por vírgula")
    parser.add_argument("--provider", type=str, choices=["openai", "deepseek"], default="deepseek", help="Provedor LLM")
    parser.add_argument("--api_key", type=str, default=None, help="Sua API Key. Omitir se estiver no .env")
    parser.add_argument("--exchange", type=str, choices=["demo", "real"], default="demo", help="Conta para logar")
    parser.add_argument("--lot", type=float, default=0.1, help="Tamanho do Volume de Trading base")
    parser.add_argument("--max-trades", type=int, default=2, help="Limite máximo de trades simultâneos")
    parser.add_argument("--loop", action='store_true', help="Roda o loop contínuo a cada H1")
    args = parser.parse_args()

    api_key = args.api_key
    if not api_key:
        api_key = os.getenv('DEEPSEEK_API_KEY') if args.provider == 'deepseek' else os.getenv('OPENAI_API_KEY')
    if not api_key:
        print("[!] FATAL: API Key do provedor não fornecida via CLI nem via .env!")
        return

    if not connect_mt5(args.exchange):
        return

    # Centralized Telegram
    global telegram
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if base_dir not in sys.path:
        sys.path.append(base_dir)
    try:
        from telegram_utils import TelegramBot
        telegram = TelegramBot(os.path.join(base_dir, "telegram.json"))
    except ImportError:
        telegram = None
        print("[!] Erro ao carregar telegram_utils.py. Telegram bot desativado.")

    if telegram:
        telegram.send_message(f"🤖 <b>LLM TRADER INICIADO</b>\nAtivos: {args.symbols}")

    # No inicio ou inicialização, varrer log pra atualizar
    update_pending_logs()

    symbol_list = [s.strip() for s in args.symbols.split(",")]
    active_symbols = []
    for sym in symbol_list:
        if mt5.symbol_select(sym, True):
            active_symbols.append(sym)
        else:
            print(f"Symbol {sym} não encontrado no Market Watch. Será ignorado.")
            
    if not active_symbols:
        mt5.shutdown()
        return

    llm_module = load_llm_module()

    print("\n--- MT5 CONECTADO ---")
    print(f"Modo: {'LOOP (H1)' if args.loop else 'ONE-SHOT (Manual)'}")
    print(f"Conta: {args.exchange.upper()}")
    print(f"Lote Execução: {args.lot}")
    print(f"Ativos: {', '.join(active_symbols)}\n")

    def analyze_and_trade():
        update_pending_logs() # Toda hora nova, chame a atualizacao
        manage_trailing_stops() # Checa gatilhos de trailing stop
        
        for sym in active_symbols:

            tick = mt5.symbol_info_tick(sym)
            if tick is None: continue

            current_price = tick.bid
            current_dt = datetime.now()
            hour_eet = current_dt.hour

            # --- Calcular indicadores tecnicos ---
            ctx = calculate_technical_context(sym)
            if ctx is None:
                print(f"  [{sym}] Dados insuficientes para indicadores. Pulando.")
                continue

            print(f"\n[{current_dt.strftime('%H:%M:%S')}] {sym} | {ctx['regime']} (ADX={ctx['adx']}) | Bias: {ctx['bias']}")
            print(f"     Price: {current_price} | EMA200: {ctx['ema200']} ({ctx['price_vs_ema']})")
            print(f"     RSI: {ctx['rsi']} | ATR: {ctx['atr']} | Vol Ratio: {ctx['vol_ratio']}")
            print(f"     +DI: {ctx['plus_di']} | -DI: {ctx['minus_di']}")

            # --- GATE TECNICO: filtrar sinais contra-tendencia ---
            gate_blocked = None
            if ctx['price_vs_ema'] == "ABAIXO" and ctx['regime'] == "TREND" and ctx['bias'] == "Bear":
                gate_blocked = "LONG"
                print(f"     [GATE] Preco ABAIXO EMA200 + Tendencia Bear -> LONGs BLOQUEADOS")
            elif ctx['price_vs_ema'] == "ACIMA" and ctx['regime'] == "TREND" and ctx['bias'] == "Bull":
                gate_blocked = "SHORT"
                print(f"     [GATE] Preco ACIMA EMA200 + Tendencia Bull -> SHORTs BLOQUEADOS")

            # Volume filter: se volume muito baixo, pular
            if ctx['vol_ratio'] < 0.3:
                print(f"     [GATE] Volume muito baixo ({ctx['vol_ratio']}x). Pulando.")
                continue

            profile_target = f"{sym.lower()}_statistical_profile.json"
            DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))
            profile_full = os.path.join(DIR_SCRIPT, profile_target)
            if not os.path.exists(profile_full):
                print(f"     [!] ALERTA: Arquivo {profile_target} nao existe. Sem base empirica para {sym}.")
            
            setup_dict = llm_module.run_llm_setup_analyzer(
                provider=args.provider, 
                api_key=api_key, 
                profile_path=profile_target, 
                current_hour=hour_eet, 
                current_price=current_price,
                current_mt5_rsi=ctx['rsi'],
                technical_context=ctx
            )
            
            if setup_dict:
                direcao = setup_dict.get('direcao_sugerida', 'NEUTRAL').upper()
                gatilho = setup_dict.get('gatilho_entrada', current_price)
                sl = setup_dict.get('stop_loss', current_price)
                
                print(f"    [IA VEREDICTO] Direcao: {direcao}")

                # Gate: bloquear se IA sugere direcao bloqueada
                if gate_blocked and direcao == gate_blocked:
                    print(f"    [GATE OVERRIDE] IA sugeriu {direcao} mas gate tecnico bloqueou. Ignorando.")
                elif direcao in ["LONG", "SHORT"]:
                    open_count = count_open_positions(999111)
                    if open_count >= args.max_trades:
                        print(f"    [GATE] Limite de trades simultâneos atingido ({open_count}/{args.max_trades}). Bloqueando trade em {sym}.")
                        if telegram:
                            telegram.send_message(
                                f"⚠️ <b>[LLM] Sinal em {sym} Ignorado!</b>\n"
                                f"IA sugeriu {direcao}, mas o limite de {args.max_trades} trades simultâneos está preenchido."
                            )
                        continue
                    execute_llm_trade(sym, args.lot, direcao, trigger_price=gatilho, stop_loss=sl, atr_value=ctx['atr'])
                else:
                    print("    => Postura Neutra acatada.")

    if not args.loop:
        analyze_and_trade()
        mt5.shutdown()
        return

    try:
        last_hour_run = None
        print("Entrou em modo Loop. Sinais a cada H1 | Logs atualizados a cada 1 min. CTRL+C para sair.")
        while True:
            now_dt = datetime.now()
            update_pending_logs()  # Checa fechamentos a cada minuto
            manage_trailing_stops() # Ajusta Trailing Stop a cada minuto
            if last_hour_run != now_dt.hour:
                analyze_and_trade()
                last_hour_run = now_dt.hour
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nSaindo...")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    BOT_NAME = "BOT LLM TRADER"

    os.system(f'title {BOT_NAME}')
    main()
