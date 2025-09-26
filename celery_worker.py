import os
import requests
import json
from celery import Celery
import google.generativeai as genai
from requests.exceptions import RequestException

# ----------------------------------------------------
# 1. Configuração do Celery e Redis (Broker/Backend)
# ----------------------------------------------------

# Lê a URL do Upstash Redis. O Render injetará essa variável.
REDIS_URL = os.getenv('UPSTASH_REDIS_URL')
if not REDIS_URL:
    # Fallback para ambiente de desenvolvimento local
    print("AVISO: UPSTASH_REDIS_URL não configurada. O Celery não funcionará.")

# Cria a aplicação Celery
celery_app = Celery('sofia_worker', broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.timezone = 'America/Sao_Paulo' 
celery_app.conf.broker_connection_retry_on_startup = True # Tenta reconectar ao Redis

# ----------------------------------------------------
# 2. Configuração do Gemini e Conhecimento
# ----------------------------------------------------

# Inicializa o cliente Gemini
try:
    genai.configure(api_key=os.getenv('GEMINI_API_KEY'))
    # Usamos gemini-2.5-flash: rápido, eficiente e otimizado para chat/conhecimento
    GEMINI_MODEL = genai.GenerativeModel('gemini-2.5-flash')
except Exception as e:
    print(f"ERRO: Falha ao configurar o cliente Gemini: {e}")

# Carrega o banco de conhecimento da loja (Executado apenas na inicialização do worker)
def load_knowledge():
    """Carrega o JSON de FAQs e Produtos na memória do Worker."""
    try:
        with open('dinamica_sports_knowledge.json', 'r', encoding='utf-8') as f:
            # Converte o objeto Python em string JSON para passar ao Prompt
            return json.dumps(json.load(f), ensure_ascii=False, indent=2)
    except FileNotFoundError:
        print("ALERTA CRÍTICO: dinamica_sports_knowledge.json não encontrado. Respostas serão genéricas.")
        return ""

STORE_KNOWLEDGE_JSON = load_knowledge()

# ----------------------------------------------------
# 3. Função Auxiliar de Envio (API do Meta)
# ----------------------------------------------------

def send_message(task_self, platform, to_id, message):
    """
    Envia a resposta de volta ao cliente via API do Meta. 
    Usa o self.retry() do Celery em caso de falha de conexão.
    """
    # 3.1 Busca de Tokens e URLs
    if platform == "whatsapp":
        url = f"https://graph.facebook.com/v20.0/{os.getenv('WHATSAPP_PHONE_ID')}/messages"
        token = os.getenv('WHATSAPP_TOKEN')
        payload = {
            "messaging_product": "whatsapp", 
            "to": to_id, 
            "type": "text", 
            "text": {"body": message}
        }
    else: # Assume Facebook/Instagram Messenger
        url = f"https://graph.facebook.com/v20.0/{os.getenv('FACEBOOK_PAGE_ID')}/messages"
        token = os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN')
        payload = {
            "recipient": {"id": to_id}, 
            "message": {"text": message}
        }

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    # 3.2 Tentativa de Envio com Retry (Retentativa)
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status() # Levanta exceção para erros HTTP (4xx ou 5xx)
        print(f"Resposta enviada com sucesso para {to_id} via {platform}.")
        return True
    except RequestException as e:
        # Se a chamada HTTP (rede ou erro do Meta) falhar, o Celery tenta novamente.
        print(f"ERRO: Falha ao enviar mensagem via Meta. Retentando em 10s. Erro: {e}")
        # A tarefa Celery tentará novamente, o que é mais confiável do que falhar.
        raise task_self.retry(exc=e, countdown=10)


# ----------------------------------------------------
# 4. A Tarefa Assíncrona Principal
# ----------------------------------------------------

@celery_app.task(bind=True, max_retries=3) # max_retries: Tenta até 3 vezes se houver falha na IA ou no envio
def process_ai_response(self, platform, from_id, message):
    """
    Executa a lógica da IA em segundo plano.
    """
    try:
        # 4.1 Criação do Prompt com Conhecimento Específico
        prompt = (
            f"Você é a SofIA, assistente virtual da loja Dinâmica Sports (Mossoró/RN). "
            f"Sua persona é entusiasta, prestativa e focada em direcionar a venda para o site. "
            f"Use EXCLUSIVAMENTE o CONHECIMENTO abaixo para responder sobre produtos, preços e horários. "
            f"Sempre que possível, inclua o link de compra e reforce que o cliente pode comprar no site. "
            f"CONHECIMENTO DA LOJA (JSON): {STORE_KNOWLEDGE_JSON}\n\n"
            f"Pergunta do Cliente: '{message}'"
        )
        
        # 4.2 Chamada ao Gemini
        response = GEMINI_MODEL.generate_content(prompt)
        ai_reply = response.text
        
        # 4.3 Envio da Resposta (com retentativa)
        send_message(self, platform, from_id, ai_reply)

    except Exception as e:
        print(f"ERRO GERAL na Tarefa Celery. Erro: {e}")
        
        # 4.4 Tratamento de Erro (Fallback)
        # Se o Gemini falhar após todas as retentativas, envia a mensagem de fallback.
        if self.request.retries >= self.max_retries:
            fallback_reply = "Desculpe, estou com um pequeno problema técnico. Um atendente entrará em contato em breve para te ajudar!"
            # Tenta enviar o fallback sem mais retentativas do Celery (evita loop infinito)
            send_message(self._get_dummy_task(), platform, from_id, fallback_reply) 
        
        # Se não for o último retry, tenta novamente
        raise self.retry(exc=e, countdown=10)

# Função dummy para evitar erro de self na chamada de fallback
def _get_dummy_task():
    class DummyTask:
        def retry(self, exc, countdown):
            pass
    return DummyTask()