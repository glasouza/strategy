import os
import glob
import json
import pandas as pd
import numpy as np

def load_and_clean_data(data_dir, symbol="XAUUSD", timeframe="H1", start_year=2020):
    """
    Carrega e limpa os arquivos CSV exportados pelo MT5
    """
    print(f"Buscando arquivos {symbol}_{timeframe} em {data_dir} a partir do ano {start_year}...")
    
    # Arquivos contêm nomenclatura como: XAUUSD_Hourly_Ask_2024.01.01_2024.12.30.csv
    # Vamos usar H1 (Hourly_Ask ou Hourly_Bid, usaremos Ask como padrão)
    search_pattern = os.path.join(data_dir, timeframe, f"{symbol}*_Ask_*.csv")
    if timeframe == "H1":
        search_pattern = os.path.join(data_dir, timeframe, f"{symbol}_Hourly_Ask_*.csv")
        
    all_files = glob.glob(search_pattern)
    files_to_process = []
    
    for f in all_files:
        filename = os.path.basename(f)
        try:
            year = int(filename.split('_')[-2].split('.')[0])
            if year >= start_year:
                files_to_process.append(f)
        except Exception as e:
            print(f"Aviso: Não foi possível determinar o ano de {filename}")
            
    if not files_to_process:
        print("Nenhum arquivo encontrado para o período especificado.")
        return None
        
    print(f"Foram encontrados {len(files_to_process)} arquivos válidos.")
    
    df_list = []
    for f in files_to_process:
        print(f"Processando: {os.path.basename(f)}")
        # MT5 default format: delimiter is ';', decimal separator is ','
        try:
            df = pd.read_csv(f, sep=';', decimal=',')
            # Procura a coluna de tempo dinamicamente caso seja 'Time (UTC)' ou 'Time (EET)'
            time_col = next((c for c in df.columns if c.startswith('Time')), 'Time (EET)')
            if time_col != 'Time (EET)':
                df.rename(columns={time_col: 'Time (EET)'}, inplace=True)
                
            df['Time (EET)'] = pd.to_datetime(df['Time (EET)'], format='%Y.%m.%d %H:%M:%S', errors='coerce')
            df_list.append(df)
        except Exception as e:
            print(f"Erro ao processar {f}: {e}")
            
    if not df_list:
        return None
        
    # Combina todos os arquivos
    master_df = pd.concat(df_list, ignore_index=True)
    master_df = master_df.dropna(subset=['Time (EET)'])
    master_df = master_df.sort_values(by='Time (EET)').reset_index(drop=True)
    master_df = master_df.drop_duplicates(subset=['Time (EET)'])
    
    # Strip spaces from column names to avoid KeyError due to trailing spaces like 'Volume '
    master_df.columns = master_df.columns.str.strip()
    
    # Renomear colunas para padrão limpo
    master_df = master_df.rename(columns={
        'Time (EET)': 'time',
        'Open': 'open',
        'High': 'high',
        'Low': 'low',
        'Close': 'close',
        'Volume': 'volume'
    })
    
    # Calcular métricas base
    master_df['range'] = master_df['high'] - master_df['low']
    master_df['direction_points'] = master_df['close'] - master_df['open']
    master_df['is_bullish'] = master_df['direction_points'] > 0
    master_df['is_bearish'] = master_df['direction_points'] < 0
    master_df['hour'] = master_df['time'].dt.hour
    master_df['day_of_week'] = master_df['time'].dt.dayofweek # 0=Monday, 6=Sunday
    
    return master_df

def compute_time_of_day_patterns(df):
    """
    Calcula sazonalidade baseada na hora do dia (0h-23h EET)
    """
    print("Computando padrões por hora do dia...")
    # Agrupa por hora e calcula médias para as métricas
    hourly_stats = df.groupby('hour').agg({
        'range': ['mean', 'median', 'std'],
        'is_bullish': 'mean',
        'is_bearish': 'mean',
        'volume': 'mean'
    }).reset_index()
    
    hourly_stats.columns = ['_'.join(col).strip('_') for col in hourly_stats.columns.values]
    
    # Transforma para dict serializável
    stats_dict = hourly_stats.to_dict(orient='records')
    return stats_dict

def compute_regime_stats(df):
    """
    Identifica características globais (ex: média de ATR) para contextualização.
    """
    print("Computando estatísticas de regime e distribuição...")
    
    avg_range = float(df['range'].mean())
    pct_90_range = float(df['range'].quantile(0.90))
    pct_10_range = float(df['range'].quantile(0.10))
    
    return {
        "global_average_range": round(avg_range, 2),
        "high_volatility_threshold_90th": round(pct_90_range, 2),
        "low_volatility_threshold_10th": round(pct_10_range, 2)
    }

def generate_profile(output_path, data_dir, symbol="XAUUSD", timeframe="H1"):
    print("-- MINERADOR DE PADRÕES INICIADO --")
    df = load_and_clean_data(data_dir=data_dir, symbol=symbol, timeframe=timeframe, start_year=2020)
    
    if df is None or df.empty:
        print("Falha ao gerar perfil: DataFrame vazio ou nulo.")
        return
        
    print(f"Dataset carregado com {len(df)} candles de {df['time'].min()} a {df['time'].max()}.")
    
    tod_patterns = compute_time_of_day_patterns(df)
    regime_stats = compute_regime_stats(df)
    
    profile = {
        "metadata": {
            "symbol": symbol,
            "timeframe": timeframe,
            "period_start": df['time'].min().strftime('%Y-%m-%d'),
            "period_end": df['time'].max().strftime('%Y-%m-%d'),
            "total_candles_analyzed": len(df),
            "timezone_reference": "EET (MetaTrader Default)"
        },
        "regime_context": regime_stats,
        "time_of_day_seasonality": tod_patterns,
        "trading_instructions": (
            "Este arquivo contem a empiria matematica do ativo. "
            "Sempre que o horario de operacao for mencionado, busque as metricas em time_of_day_seasonality para atestar probabilidade. "
            "Ex: Se a hora atual for conhecida por alta probabilidade de queda (is_bearish > 55%), o prompt/LLM deve ser contraintuitivo para comprar "
            "ou exigir confluencia extra."
        )
    }
    
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(profile, f, indent=4, ensure_ascii=False)
        
    print(f"Perfil estatístico salvo em: {output_path}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Pattern Miner para XAUUSD")
    parser.add_argument("--data_dir", type=str, default=r"H:\Investimentos\rl_trader\Historico_Dataset\XAUUSD", help="Diretório onde as pastas H1, M1 residem")
    parser.add_argument("--timeframe", type=str, default="H1", help="Timeframe (Ex: H1, M1)")
    parser.add_argument("--symbol", type=str, default="XAUUSD", help="Ativo")
    parser.add_argument("--out", type=str, default=None, help="Caminho. Deixe vazio para {symbol}_statistical_profile.json")
    args = parser.parse_args()
    
    out_file = args.out if args.out else f"{args.symbol.lower()}_statistical_profile.json"
    generate_profile(output_path=out_file, data_dir=args.data_dir, symbol=args.symbol, timeframe=args.timeframe)
