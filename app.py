import os
from flask import Flask, request, jsonify

# Importa a tarefa do Celery. O 'try/except' permite testes locais sem o Celery Worker rodando.
try:
    # A função deve ser a que você definiu no celery_worker.py
    from celery_worker import process_ai_response 
    CELERY_ENABLED = True
except ImportError:
    # Fallback para testes locais
    def process_ai_response(platform, from_id, message):
        print(f"[FALLBACK] Tarefa Celery simulada. IA processaria: {message}")
    CELERY_ENABLED = False
    
app = Flask(__name__)

# Configuração e Tokens (lidos das variáveis de ambiente do Render)
# Nota: Você pode usar uma variável única (VERIFY_TOKEN) se for a mesma para todas as plataformas.
VERIFY_TOKEN_WHATSAPP = os.getenv('VERIFY_TOKEN_WHATSAPP') # Token para verificação do WhatsApp
VERIFY_TOKEN_FACEBOOK = os.getenv('VERIFY_TOKEN_FACEBOOK') # Token para verificação do Facebook/IG

# ----------------------------------------------------
# 1. Rotas de Monitoramento e Status
# ----------------------------------------------------

@app.route('/')
def home():
    """Confirma que o serviço está rodando."""
    return "SofIA Multiplataforma da Dinâmica Sports está online! (Render + Celery)"

@app.route("/status")
def status():
    """Endpoint de saúde para o Render."""
    return jsonify({
        "status": "online",
        "service": "Web Service",
        "celery_connection": "OK" if CELERY_ENABLED else "Aviso: Celery não importado (local dev?)",
        "version": "1.0"
    })

# ----------------------------------------------------
# 2. Rota Principal de Webhook (GET e POST)
# ----------------------------------------------------

# Múltiplos endpoints para roteamento de diferentes plataformas
@app.route("/webhook/whatsapp", methods=['GET', 'POST'])
@app.route("/webhook/facebook", methods=['GET', 'POST'])
@app.route("/webhook/instagram", methods=['GET', 'POST'])
def webhook():
    # Detecta a plataforma com base na URL acessada
    if 'whatsapp' in request.url:
        platform = 'whatsapp'
        expected_token = VERIFY_TOKEN_WHATSAPP
    elif 'facebook' in request.url:
        platform = 'facebook'
        expected_token = VERIFY_TOKEN_FACEBOOK
    elif 'instagram' in request.url:
        platform = 'instagram'
        expected_token = VERIFY_TOKEN_FACEBOOK # Facebook e Instagram compartilham o token na API
    else:
        # Caso de rota não mapeada (segurança)
        return "Not Found", 404

    # 2.1 Lógica GET: Verificação do Webhook
    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == 'subscribe' and token == expected_token:
            print(f"Webhook {platform} VERIFICADO.")
            return challenge, 200
        print(f"Falha na verificação do Webhook {platform}.")
        return "Token inválido", 403

    # 2.2 Lógica POST: Recebimento e Despacho de Mensagens
    elif request.method == 'POST':
        data = request.get_json()
        
        # Tenta extrair a mensagem de forma robusta
        from_id, message = extract_message_data(data, platform)
        
        if from_id and message:
            # Se uma mensagem de texto válida foi encontrada
            if CELERY_ENABLED:
                # Despacha a tarefa para o Celery (Background Worker)
                process_ai_response.delay(platform, from_id, message)
                print(f"Mensagem recebida: '{message}'. Tarefa Celery despachada para {platform}.")
            else:
                # Fallback de execução síncrona/simulada para teste local
                process_ai_response(platform, from_id, message)
        
        # CRUCIAL: Retornar 200 OK imediatamente para evitar timeouts do Meta
        return "OK", 200
    
    return "Método não permitido", 405

# ----------------------------------------------------
# 3. Função de Extração de Payload (Robustez)
# ----------------------------------------------------

def extract_message_data(data, platform):
    """
    Função robusta para extrair o remetente (from_id) e o texto da mensagem,
    ignorando eventos de status ou lida.
    """
    if platform == "whatsapp" and data.get("entry"):
        try:
            # Padrão da API Cloud do WhatsApp
            value = data["entry"][0]["changes"][0]["value"]
            if 'messages' in value:
                message_obj = value['messages'][0]
                # Verifica se é uma mensagem de texto
                if message_obj.get('type') == 'text':
                    return message_obj.get('from'), message_obj['text']['body']
        except (KeyError, IndexError):
            # Ignora eventos de status de leitura, digitação, etc.
            pass

    elif (platform == "facebook" or platform == "instagram") and data.get("entry"):
        try:
            # Padrão da API do Messenger (usada para FB e IG)
            messaging_event = data["entry"][0]["messaging"][0]
            if "message" in messaging_event and "text" in messaging_event["message"]:
                sender_id = messaging_event["sender"]["id"]
                message_text = messaging_event["message"]["text"]
                return sender_id, message_text
        except (KeyError, IndexError):
            # Ignora eventos de postback, status de lida, etc.
            pass
            
    # Retorna None se não for uma mensagem de texto válida para processamento
    return None, None 

# ----------------------------------------------------
# 4. Inicialização (Local)
# ----------------------------------------------------

if __name__ == '__main__':
    # Este bloco é executado apenas se você rodar 'python app.py' localmente.
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))