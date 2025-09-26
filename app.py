import os
import ssl  # NOVO: Necessário para Celery SSL
import hmac
import sys
import hashlib
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from celery import Celery  # NOVO: Celery importado aqui para a configuração
from functools import wraps

# ----------------------------------------------------
# 0. Configuração de Logging
# ----------------------------------------------------

def setup_logging(app):
    """
    Configura o sistema de logging para PaaS (stdout/stderr) ou arquivo (local).
    """
    if app.debug:
        # Modo de desenvolvimento
        if not os.path.exists('logs'):
            os.mkdir('logs')
        file_handler = RotatingFileHandler('logs/sofia_dev.log', maxBytes=10240000, backupCount=5)
        file_handler.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
        ))
        file_handler.setLevel(logging.DEBUG)
        app.logger.addHandler(file_handler)
        app.logger.setLevel(logging.DEBUG)
        app.logger.info('Sofia webhook startup in DEBUG mode')
    else:
        # Modo de produção (Render/Gunicorn)
        gunicorn_logger = logging.getLogger('gunicorn.error')
        app.logger.handlers = gunicorn_logger.handlers
        app.logger.setLevel(gunicorn_logger.level)
        app.logger.info('Sofia webhook startup in PRODUCTION mode')

# ----------------------------------------------------
# 1. Validação de Variáveis de Ambiente
# ----------------------------------------------------

def validate_environment():
    """Valida que todas as variáveis necessárias estão configuradas."""
    required_vars = {
        'UPSTASH_REDIS_URL': 'URL do Redis para Celery',
        'VERIFY_TOKEN_WHATSAPP': 'Token de verificação do WhatsApp',
        # As variáveis de Secret são necessárias para a validação HMAC
        'WEBHOOK_SECRET_WHATSAPP': 'Secret para validação HMAC do WhatsApp'
    }
    
    missing_vars = []
    for var, description in required_vars.items():
        if not os.getenv(var):
            missing_vars.append(f"{var} ({description})")
    
    if missing_vars:
        error_msg = f"Variáveis de ambiente faltando ou vazias: {', '.join(missing_vars)}"
        raise EnvironmentError(error_msg)

# ----------------------------------------------------
# 2. Inicialização Flask e Configurações
# ----------------------------------------------------

app = Flask(__name__)
setup_logging(app)

# Validação de ambiente - a aplicação irá parar aqui se algo estiver faltando
try:
    validate_environment()
except EnvironmentError as e:
    app.logger.critical(f"ERRO CRÍTICO NA INICIALIZAÇÃO: {e}")
    print(f"ERRO CRÍTICO NA INICIALIZAÇÃO: {e}", file=sys.stderr)
    sys.exit(1) # Encerra o processo com código de erro

# Configuração e Tokens (lidos das variáveis de ambiente)
# Prioriza UPSTASH_REDIS_URL, mas checa CELERY_BROKER_URL como fallback.
REDIS_URL = os.getenv('UPSTASH_REDIS_URL') or os.getenv('CELERY_BROKER_URL')
VERIFY_TOKEN_WHATSAPP = os.getenv('VERIFY_TOKEN_WHATSAPP')
VERIFY_TOKEN_FACEBOOK = os.getenv('VERIFY_TOKEN_FACEBOOK')
WEBHOOK_SECRET_WHATSAPP = os.getenv('WEBHOOK_SECRET_WHATSAPP', '')
WEBHOOK_SECRET_FACEBOOK = os.getenv('WEBHOOK_SECRET_FACEBOOK', '')

# Rate Limiting para proteção contra spam
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["1000 per hour"],
    storage_uri=REDIS_URL or "memory://", # Usa o Redis configurado para persistência
)

# ----------------------------------------------------
# 3. Configuração do Celery com SSL (CORREÇÃO CRÍTICA)
# ----------------------------------------------------

CELERY_ENABLED = False
celery_app = None

# Configuração SSL/TLS OBRIGATÓRIA para o Redis do Railway
CELERY_SSL_CONFIG = {
    'ssl_cert_reqs': ssl.CERT_NONE
}

try:
    if REDIS_URL:
        celery_app = Celery(
            'sofia_worker',
            broker=REDIS_URL,
            backend=REDIS_URL
        )
        
        # CORREÇÃO CRÍTICA: Força SSL se a URL usar 'rediss://' (o que o Railway exige)
        is_ssl_required = 'rediss://' in REDIS_URL
        
        celery_app.conf.update(
            broker_use_ssl=CELERY_SSL_CONFIG if is_ssl_required else None,
            redis_backend_use_ssl=CELERY_SSL_CONFIG if is_ssl_required else None,
            task_serializer='json',
            accept_content=['json'],
            result_serializer='json',
            timezone='America/Sao_Paulo',
            task_acks_late=True,
            broker_connection_retry_on_startup=True,
            broker_connection_max_retries=10,
        )
        
        # Importa a tarefa (só se a inicialização do Celery for bem-sucedida)
        from celery_worker import process_ai_response
        CELERY_ENABLED = True
        app.logger.info("Celery configurado com sucesso para o Redis")
        
except Exception as e:
    # Este bloco será executado se o Celery não conseguir se conectar ao Broker (Redis)
    app.logger.error(f"ERRO CRÍTICO NA CONFIGURAÇÃO CELERY/REDIS: {e}", exc_info=True)
    
    # Fallback para execução síncrona/simulada. 
    # Isso explica o log [FALLBACK] que vimos.
    def process_ai_response(platform, from_id, message):
        app.logger.warning(f"[FALLBACK] Processamento síncrono: {platform} - {message[:50]}...")
        return {"status": "processed_sync"}

# ----------------------------------------------------
# 4. Funções de Segurança
# ----------------------------------------------------

def verify_webhook_signature(request, secret):
    """Verifica a assinatura HMAC da requisição webhook."""
    # ... (O restante da função de segurança permanece inalterado)
    if not secret:
        if app.debug:
            app.logger.warning("Secret não configurado. Assinatura não verificada (MODO DEBUG)")
            return True
        app.logger.error("Secret para validação de assinatura não configurado em PRODUÇÃO!")
        return False
    
    signature_header = request.headers.get('X-Hub-Signature-256', '')
    if not signature_header.startswith('sha256='):
        app.logger.warning("Assinatura ausente ou formato inválido")
        return False
    
    try:
        received_signature = signature_header[7:]
        
        expected_signature = hmac.new(
            secret.encode('utf-8'),
            request.data,
            hashlib.sha256
        ).hexdigest()
        
        is_valid = hmac.compare_digest(received_signature, expected_signature)
        
        if not is_valid:
            app.logger.warning(f"Assinatura inválida. Recebida: {received_signature[:10]}...")
        
        return is_valid
        
    except Exception as e:
        app.logger.error(f"Erro ao verificar assinatura: {e}")
        return False

def require_webhook_verification(platform):
    """Decorator para validar assinatura de webhooks"""
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if request.method == 'POST':
                if platform == 'whatsapp':
                    secret = WEBHOOK_SECRET_WHATSAPP
                else:
                    secret = WEBHOOK_SECRET_FACEBOOK
                
                if not verify_webhook_signature(request, secret):
                    app.logger.warning(f"Assinatura inválida para {platform}")
                    return jsonify({"error": "Invalid signature"}), 403
            
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# ----------------------------------------------------
# 5. Extractors de Mensagem por Plataforma
# (Esta seção permanece inalterada e robusta)
# ----------------------------------------------------

def extract_whatsapp_message(data):
    """Extrai mensagem do formato WhatsApp Cloud API"""
    try:
        if not data.get("entry"):
            return None, None
            
        value = data["entry"][0]["changes"][0]["value"]
        
        if "statuses" in value:
            app.logger.debug("Evento de status WhatsApp ignorado")
            return None, None
        
        if "messages" in value:
            message_obj = value["messages"][0]
            
            from_id = message_obj.get("from")
            msg_type = message_obj.get("type")
            
            if msg_type == "text":
                return from_id, message_obj["text"]["body"]
            # Inclui tratamento para imagens com legenda e áudio, tornando-o mais robusto
            elif msg_type == "image" and "caption" in message_obj.get("image", {}):
                return from_id, f"[IMAGEM] {message_obj['image']['caption']}"
            elif msg_type == "audio":
                return from_id, "[ÁUDIO RECEBIDO]"
            else:
                app.logger.info(f"Tipo de mensagem WhatsApp não suportado: {msg_type}")
                
    except (KeyError, IndexError, TypeError) as e:
        app.logger.error(f"Erro ao extrair mensagem WhatsApp: {e}")
    
    return None, None

def extract_messenger_message(data):
    """Extrai mensagem do formato Facebook/Instagram Messenger"""
    try:
        if not data.get("entry"):
            return None, None
            
        messaging_events = data["entry"][0].get("messaging", [])
        
        for event in messaging_events:
            sender_id = event.get("sender", {}).get("id")
            
            if "message" in event and "text" in event["message"]:
                return sender_id, event["message"]["text"]
            
            elif "message" in event and "quick_reply" in event["message"]:
                return sender_id, event["message"]["quick_reply"].get("payload", "")
            
            elif "postback" in event:
                return sender_id, event["postback"].get("payload", "")
            
    except (KeyError, IndexError, TypeError) as e:
        app.logger.error(f"Erro ao extrair mensagem Messenger: {e}")
    
    return None, None

MESSAGE_EXTRACTORS = {
    'whatsapp': extract_whatsapp_message,
    'facebook': extract_messenger_message,
    'instagram': extract_messenger_message
}

def extract_message_data(data, platform):
    """Função principal para extrair mensagens usando o extractor correto"""
    extractor = MESSAGE_EXTRACTORS.get(platform)
    if not extractor:
        app.logger.error(f"Plataforma não suportada: {platform}")
        return None, None
    
    return extractor(data)

# ----------------------------------------------------
# 6. Rotas de Monitoramento e Status
# ----------------------------------------------------

@app.route('/')
def home():
    """Página inicial com informações básicas"""
    env_status = "production" if os.environ.get('FLASK_DEBUG', 'False').lower() != 'true' else "development"
    return jsonify({
        "service": "SofIA Multiplataforma - Dinâmica Sports",
        "status": "online",
        "version": "2.1 (Final)",
        "environment": env_status,
        "celery_enabled": CELERY_ENABLED
    })

@app.route("/status")
def status():
    """Endpoint de saúde detalhado"""
    celery_status = "DISABLED"
    redis_status = "DISCONNECTED"
    workers = 0
    
    if CELERY_ENABLED and celery_app:
        try:
            # Tenta inspecionar o Worker (Timeout baixo para não bloquear o endpoint)
            inspect = celery_app.control.inspect(timeout=1.0)
            active_workers = inspect.active()
            
            if active_workers:
                celery_status = "OPERATIONAL"
                workers = len(active_workers)
                redis_status = "CONNECTED"
            else:
                celery_status = "NO_WORKERS" # Broker está conectado, mas Worker não está rodando
                redis_status = "CONNECTED" 
                
        except Exception as e:
            celery_status = f"ERROR"
            redis_status = "UNHEALTHY" # Broker não está conectando
            app.logger.error(f"Erro ao verificar Celery: {e}")
    
    return jsonify({
        "status": "online",
        "timestamp": datetime.utcnow().isoformat(),
        "celery": {
            "status": celery_status,
            "workers": workers,
            "broker": "redis" if REDIS_URL else "none"
        },
        "redis": {
            "status": redis_status,
            "ssl": "enabled" if REDIS_URL and "rediss://" in REDIS_URL else "disabled"
        }
    })

@app.route("/health")
@limiter.limit("10 per minute")
def health():
    """Endpoint simples para health checks (load balancers)"""
    return "OK", 200

# ----------------------------------------------------
# 7. Rotas de Webhook Principal
# ----------------------------------------------------

@app.route("/webhook/whatsapp", methods=['GET', 'POST'])
@require_webhook_verification('whatsapp')
@limiter.limit("100 per minute", methods=["POST"])
def webhook_whatsapp():
    return handle_webhook('whatsapp', VERIFY_TOKEN_WHATSAPP)

@app.route("/webhook/facebook", methods=['GET', 'POST'])
@require_webhook_verification('facebook')
@limiter.limit("100 per minute", methods=["POST"])
def webhook_facebook():
    return handle_webhook('facebook', VERIFY_TOKEN_FACEBOOK)

@app.route("/webhook/instagram", methods=['GET', 'POST'])
@require_webhook_verification('instagram')
@limiter.limit("100 per minute", methods=["POST"])
def webhook_instagram():
    return handle_webhook('instagram', VERIFY_TOKEN_FACEBOOK)

def handle_webhook(platform, expected_token):
    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        token = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")
        
        if mode == 'subscribe' and token == expected_token:
            app.logger.info(f"Webhook {platform} verificado com sucesso")
            return challenge, 200
        
        app.logger.warning(f"Falha na verificação do webhook {platform}")
        return "Token inválido", 403
    
    elif request.method == 'POST':
        try:
            data = request.get_json()
            
            if not data:
                app.logger.warning(f"Payload vazio recebido em {platform}")
                return "OK", 200
            
            from_id, message = extract_message_data(data, platform)
            
            if from_id and message:
                app.logger.info(f"Mensagem de {platform}/{from_id}: {message[:50]}...")
                
                if CELERY_ENABLED:
                    # Tenta despachar a tarefa Celery (Isso falhava antes do SSL fix)
                    task = process_ai_response.delay(platform, from_id, message)
                    app.logger.info(f"Tarefa Celery criada: {task.id}")
                else:
                    # Cai aqui se a conexão Celery/Redis falhou na inicialização
                    process_ai_response(platform, from_id, message)
                    app.logger.warning("Processamento síncrono (Celery indisponível)")
            else:
                app.logger.debug(f"Evento {platform} ignorado (não é mensagem de usuário)")
            
            return "OK", 200
            
        except Exception as e:
            app.logger.error(f"Erro no webhook {platform}: {e}", exc_info=True)
            # Sempre retorna 200 para evitar que a Meta desative o webhook
            return "OK", 200 
    
    return "Método não permitido", 405

# ----------------------------------------------------
# 8. Tratamento de Erros Global
# ----------------------------------------------------

@app.errorhandler(404)
def not_found_error(error):
    return jsonify({"error": "Endpoint não encontrado"}), 404

@app.errorhandler(500)
def internal_error(error):
    app.logger.error(f"Erro interno: {error}", exc_info=True)
    return jsonify({"error": "Erro interno do servidor"}), 500

@app.errorhandler(429)
def ratelimit_handler(e):
    app.logger.warning(f"Rate limit excedido: {e.description}")
    return jsonify({
        "error": "Rate limit excedido",
        "message": str(e.description)
    }), 429

# ----------------------------------------------------
# 9. Inicialização
# ----------------------------------------------------

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug_mode = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    app.run(
        host='0.0.0.0',
        port=port,
        debug=debug_mode
    )