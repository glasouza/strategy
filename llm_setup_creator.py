import os
import json
import argparse
# pyrefly: ignore [missing-import]
from pydantic import BaseModel, Field
from openai import OpenAI

class TradingSetup(BaseModel):
    ativo: str = Field(description="O ativo analisado")
    direcao_sugerida: str = Field(description="LONG, SHORT ou NEUTRAL")
    racional: str = Field(description="Explicação unindo as métricas.")
    probabilidade_estatistica_hora: str = Field(description="O que a base revelou sobre esta hora específica (porcentagem)")
    gatilho_entrada: float = Field(description="Preço de gatilho")
    stop_loss: float = Field(description="Onde o cenário seria invalidado")

def run_llm_setup_analyzer(provider, api_key, profile_path, current_hour, current_price, current_mt5_rsi=None, technical_context=None):
    if provider.lower() == "openai":
        client = OpenAI(api_key=api_key)
        model = "gpt-4o"
        print(f"Iniciando cliente com OPENAI (Modelo: {model})...")
    elif provider.lower() in ["deepseek", "deepseekcoder"]:
        client = OpenAI(api_key=api_key, base_url="https://api.deepseek.com")
        model = "deepseek-chat"
        print(f"Iniciando cliente com DEEPSEEK (Modelo: {model})...")
    else:
        print(f"Provedor '{provider}' nao suportado.")
        return None
        
    try:
        with open(profile_path, 'r', encoding='utf-8') as f:
            profile = json.load(f)
    except FileNotFoundError:
        print(f"Erro: Arquivo {profile_path} nao encontrado.")
        return None
        
    tod_seasonality = profile.get("time_of_day_seasonality", [])
    current_hour_stats = next((item for item in tod_seasonality if item["hour"] == current_hour), None)

    # Montar bloco de contexto tecnico
    tech_block = ""
    if technical_context:
        ctx = technical_context
        tech_block = f"""
INDICADORES TECNICOS (H1, calculados agora):
  EMA 200: {ctx['ema200']} (preco esta {ctx['price_vs_ema']} da EMA)
  ADX (14): {ctx['adx']} -> Regime: {ctx['regime']}
  +DI: {ctx['plus_di']} | -DI: {ctx['minus_di']} -> {'Compradores dominam' if ctx['bias'] == 'Bull' else 'Vendedores dominam'}
  ATR (14): {ctx['atr']} (volatilidade media por vela H1)
  Volume Ratio: {ctx['vol_ratio']}x da media (20 periodos)

REGRAS OBRIGATORIAS:
  - Se preco ABAIXO da EMA200 E ADX > 25 E -DI > +DI: a tendencia e de BAIXA. Favoreca SHORT ou NEUTRAL.
  - Se preco ACIMA da EMA200 E ADX > 25 E +DI > -DI: a tendencia e de ALTA. Favoreca LONG ou NEUTRAL.
  - Se ADX < 25: mercado lateral, use RSI para mean-reversion.
  - NUNCA sugira LONG contra tendencia de baixa confirmada, nem SHORT contra tendencia de alta confirmada.
  - O stop_loss DEVE estar a pelo menos {ctx['atr'] * 1.5:.2f} pontos do gatilho (1.5x ATR).
"""
    
    prompt = f"""
Voce e um Arquiteto Quantitativo Senior que avalia setups combinando indicadores tecnicos com estatisticas historicas.

MERCADO ATUAL:
Ativo: {profile_path.replace('_statistical_profile.json','').upper()}
Hora Atual (EET): {current_hour}h
Preco Atual Aproximado: ${current_price}
RSI MT5 Atual: {current_mt5_rsi if current_mt5_rsi else 'N/A'}
{tech_block}
PADRAO HISTORICO GERADO NOS ULTIMOS 5 ANOS PARA ESTA HORA ({current_hour}h):
{json.dumps(current_hour_stats, indent=2) if current_hour_stats else 'Dados insuficientes.'}

REGIME GLOBAL:
{json.dumps(profile.get('regime_context', {}), indent=2)}

INSTRUCAO:
1. Analise PRIMEIRO os indicadores tecnicos (EMA200, ADX, DI) para determinar a direcao dominante.
2. Use o padrao historico como CONFIRMACAO, nao como decisao primaria.
3. Se indicadores e estatisticas concordam: sinal forte. Se discordam: prefira NEUTRAL.
4. Estruture um setup e responda estritamente em JSON.
"""

    print("Enviando contexto estatístico para a API...")

    try:
        if provider.lower() == "openai":
            response = client.beta.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": "Você é um bot quantitativo. Responda estritamente em JSON válido."},
                    {"role": "user", "content": prompt}
                ],
                response_format=TradingSetup,
                temperature=0.1
            )
            print("\n--- RESPOSTA DA IA (JSON) ---\n")
            print(response.choices[0].message.content)
            return json.loads(response.choices[0].message.content)
            
        elif provider.lower() in ["deepseek", "deepseekcoder"]:
            json_schema_str = json.dumps(TradingSetup.model_json_schema(), ensure_ascii=False)
            deepseek_prompt = prompt + f"\n\nATENÇÃO: RESPONDA APENAS UM JSON VÁLIDO QUE SIGA ESTE SCHEMA RIGOROSAMENTE SEM MARKDOWN:\n{json_schema_str}"
            
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Você é um assistente JSON puro."},
                    {"role": "user", "content": deepseek_prompt}
                ],
                response_format={"type": "json_object"},
                temperature=0.1
            )
            print("\n--- RESPOSTA DA IA (JSON) ---\n")
            raw_content = response.choices[0].message.content.strip()
            
            if raw_content.startswith("```json"):
                raw_content = raw_content.replace("```json", "", 1)
            if raw_content.endswith("```"):
                raw_content = raw_content.rsplit("```", 1)[0]
                
            raw_content = raw_content.strip()
            print(raw_content)
            return json.loads(raw_content)
            
    except Exception as e:
        print(f"Erro na comunicação API: {e}")
        return None

if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
        
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
                            if val.startswith(('"', "'")) and val.endswith(('"', "'")):
                                val = val[1:-1]
                            os.environ[key] = val
            except Exception as e:
                print(f"[!] Erro ao carregar .env manualmente: {e}")

    
    parser = argparse.ArgumentParser("LLM Setup Creator")
    parser.add_argument("--provider", type=str, choices=["openai", "deepseek"], default="deepseek")
    parser.add_argument("--api_key", type=str, default=None)
    parser.add_argument("--profile", type=str, default="xauusd_statistical_profile.json")
    parser.add_argument("--hour", type=int, default=10)
    parser.add_argument("--price", type=float, default=2605.5)
    args = parser.parse_args()
    
    api_key = args.api_key
    if not api_key:
        api_key = os.getenv('DEEPSEEK_API_KEY') if args.provider == 'deepseek' else os.getenv('OPENAI_API_KEY')
        
    if not api_key:
        print(f"CUIDADO: Nenhuma API Key para {args.provider} configurada/encontrada!")
    else:
        resultado = run_llm_setup_analyzer(args.provider, api_key, args.profile, args.hour, args.price)
        if resultado:
            print("\n[!] Sucesso:")
            print(resultado)
