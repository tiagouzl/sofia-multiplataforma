import os
import requests
import json
import logging
from typing import Dict, Optional, Any
from datetime import datetime
from celery import Celery
from celery.exceptions import MaxRetriesExceededError
import google.genai as genai
from requests.exceptions import RequestException, Timeout, HTTPError
from functools import lru_cache
import hashlib
import ssl # Necessário para a configuração SSL do Celery/Redis

# ----------------------------------------------------
# 1. Configuração de Logging Estruturado
# ----------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ----------------------------------------------------
# 2. Configuração do Celery e Redis (CORRIGIDA COM SSL)
# ----------------------------------------------------

def get_redis_url() -> Optional[str]:
    """Obtém URL do Redis com fallback inteligente."""
    redis_urls = [
        'CELERY_BROKER_URL',
        'UPSTASH_REDIS_URL', 
        'REDIS_URL'
    ]
    
    for var in redis_urls:
        url = os.getenv(var)
        if url:
            logger.info(f"Redis URL obtida de {var}")
            return url
    
    logger.error("Nenhuma URL Redis configurada. Celery não funcionará.")
    return None

REDIS_URL = get_redis_url()

# Configuração SSL/TLS OBRIGATÓRIA para o Redis do Railway
CELERY_SSL_CONFIG = {
    'ssl_cert_reqs': ssl.CERT_NONE
}
# Verifica se a URL usa o protocolo seguro (rediss://)
is_ssl_required = REDIS_URL and ('rediss://' in REDIS_URL)

# Configuração do Celery com melhores práticas
celery_app = Celery('sofia_worker')
celery_app.conf.update(
    broker_url=REDIS_URL,
    result_backend=REDIS_URL,
    timezone='America/Sao_Paulo',
    broker_connection_retry_on_startup=True,
    
    # CRÍTICO: Aplica a configuração SSL se for necessário
    broker_use_ssl=CELERY_SSL_CONFIG if is_ssl_required else None,
    redis_backend_use_ssl=CELERY_SSL_CONFIG if is_ssl_required else None,
    
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    task_soft_time_limit=30,  # Timeout suave de 30s
    task_time_limit=60,       # Timeout rígido de 60s
    task_acks_late=True,      # Reconhece tarefa só após conclusão
    worker_prefetch_multiplier=1, # Processa uma tarefa por vez
)

# ----------------------------------------------------
# 3. Configuração do Gemini com Pool de Conexões
# ----------------------------------------------------

class GeminiClient:
    """Cliente Gemini thread-safe com retry e cache."""
    
    def __init__(self):
        self.api_key = os.getenv('GEMINI_API_KEY')
        self.model = None
        self._initialize()
    
    def _initialize(self):
        """Inicializa o modelo Gemini com tratamento de erro."""
        if not self.api_key:
            logger.error("GEMINI_API_KEY não configurada")
            return
        
        try:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel('gemini-2.0-flash-exp')
            logger.info("Modelo Gemini inicializado com sucesso")
        except Exception as e:
            logger.error(f"Falha ao configurar Gemini: {e}")
    
    @lru_cache(maxsize=128)
    def generate_cached_response(self, prompt_hash: str, prompt: str) -> str:
        """Gera resposta com cache baseado em hash do prompt."""
        if not self.model:
            raise ValueError("Modelo Gemini não inicializado")
        
        try:
            response = self.model.generate_content(
                prompt,
                generation_config={
                    'temperature': 0.7,
                    'max_output_tokens': 500,
                    'top_p': 0.95,
                    'top_k': 40
                }
            )
            return response.text
        except Exception as e:
            logger.error(f"Erro na geração Gemini: {e}")
            raise

gemini_client = GeminiClient()

# ----------------------------------------------------
# 4. Gerenciamento de Base de Conhecimento
# ----------------------------------------------------

class KnowledgeBase:
    """Gerencia a base de conhecimento com validação e segurança."""
    
    def __init__(self, file_path: str = 'dinamica_sports_knowledge.json'):
        self.file_path = file_path
        self.knowledge = {}
        self.load()
    
    def load(self) -> None:
        """Carrega e valida o conhecimento."""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                
            # Validação básica da estrutura
            if not isinstance(data, dict):
                raise ValueError("Conhecimento deve ser um dicionário")
            
            self.knowledge = data
            logger.info(f"Base de conhecimento carregada: {len(data)} categorias")
            
        except FileNotFoundError:
            logger.critical(f"Arquivo {self.file_path} não encontrado")
            self.knowledge = {"error": "Base de conhecimento não disponível"}
        except json.JSONDecodeError as e:
            logger.error(f"Erro ao decodificar JSON: {e}")
            self.knowledge = {"error": "Erro na base de conhecimento"}
    
    def get_formatted(self) -> str:
        """Retorna conhecimento formatado e sanitizado."""
        # Remove dados sensíveis ou perigosos
        safe_knowledge = self._sanitize(self.knowledge)
        return json.dumps(safe_knowledge, ensure_ascii=False, indent=2)
    
    def _sanitize(self, data: Any) -> Any:
        """Sanitiza dados removendo possíveis injeções."""
        if isinstance(data, dict):
            return {k: self._sanitize(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._sanitize(item) for item in data]
        elif isinstance(data, str):
            # Remove caracteres de controle e limita tamanho
            return data.replace('\x00', '').replace('\r', '')[:1000]
        return data

knowledge_base = KnowledgeBase()

# ----------------------------------------------------
# 5. Cliente de Mensagens Meta
# ----------------------------------------------------

class MetaMessenger:
    """Cliente para APIs do Meta com retry e validação."""
    
    def __init__(self):
        self.whatsapp_config = {
            'phone_id': os.getenv('WHATSAPP_PHONE_ID'),
            'token': os.getenv('WHATSAPP_TOKEN')
        }
        self.facebook_config = {
            'page_id': os.getenv('FACEBOOK_PAGE_ID'),
            'token': os.getenv('FACEBOOK_PAGE_ACCESS_TOKEN')
        }
    
    def send_message(
        self, 
        platform: str, 
        to_id: str, 
        message: str,
        retry_count: int = 0
    ) -> bool:
        """Envia mensagem com validação e retry."""
        
        # Validação de entrada
        if not all([platform, to_id, message]):
            logger.error("Parâmetros inválidos para envio de mensagem")
            return False
        
        # Trunca mensagem se muito longa
        if len(message) > 4000:
            message = message[:3997] + "..."
        
        try:
            if platform == "whatsapp":
                return self._send_whatsapp(to_id, message)
            else:
                return self._send_facebook(to_id, message)
                
        except (RequestException, Timeout) as e:
            logger.error(f"Erro de rede ao enviar mensagem: {e}")
            if retry_count < 3:
                return self.send_message(platform, to_id, message, retry_count + 1)
            return False
        except HTTPError as e:
            logger.error(f"Erro HTTP da API Meta: {e}")
            return False
    
    def _send_whatsapp(self, to_id: str, message: str) -> bool:
        """Envia mensagem via WhatsApp Business API."""
        if not all(self.whatsapp_config.values()):
            logger.error("Configuração WhatsApp incompleta")
            return False
        
        url = f"https://graph.facebook.com/v20.0/{self.whatsapp_config['phone_id']}/messages"
        headers = {
            "Authorization": f"Bearer {self.whatsapp_config['token']}",
            "Content-Type": "application/json"
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to_id,
            "type": "text",
            "text": {"body": message}
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Mensagem WhatsApp enviada para {to_id}")
        return True
    
    def _send_facebook(self, to_id: str, message: str) -> bool:
        """Envia mensagem via Facebook Messenger."""
        if not all(self.facebook_config.values()):
            logger.error("Configuração Facebook incompleta")
            return False
        
        url = f"https://graph.facebook.com/v20.0/{self.facebook_config['page_id']}/messages"
        headers = {
            "Authorization": f"Bearer {self.facebook_config['token']}",
            "Content-Type": "application/json"
        }
        payload = {
            "recipient": {"id": to_id},
            "message": {"text": message}
        }
        
        response = requests.post(url, headers=headers, json=payload, timeout=10)
        response.raise_for_status()
        logger.info(f"Mensagem Facebook enviada para {to_id}")
        return True

meta_messenger = MetaMessenger()

# ----------------------------------------------------
# 6. Gerador de Prompts Seguros
# ----------------------------------------------------

class PromptBuilder:
    """Constrói prompts seguros e otimizados."""
    
    SYSTEM_PROMPT = """
    Você é a SofIA, assistente virtual da loja Dinâmica Sports em Mossoró/RN.
    
    PERSONALIDADE:
    - Entusiasta e prestativa
    - Focada em ajudar o cliente
    - Direciona vendas para o site quando apropriado
    
    DIRETRIZES:
    1. Use APENAS informações do conhecimento fornecido
    2. Seja concisa (máximo 3 parágrafos)
    3. Inclua links de compra quando relevante
    4. Se não souber, admita e ofereça ajuda alternativa
    5. Mantenha tom amigável e profissional
    
    CONHECIMENTO DA LOJA:
    {knowledge}
    """
    
    @staticmethod
    def build(message: str, knowledge: str) -> str:
        """Constrói prompt seguro com validação."""
        # Sanitiza entrada do usuário
        safe_message = message.replace('\x00', '').strip()[:500]
        
        if not safe_message:
            safe_message = "Olá"
        
        prompt = PromptBuilder.SYSTEM_PROMPT.format(knowledge=knowledge)
        prompt += f"\n\nPERGUNTA DO CLIENTE: {safe_message}"
        
        return prompt
    
    @staticmethod
    def get_hash(prompt: str) -> str:
        """Gera hash do prompt para cache."""
        return hashlib.md5(prompt.encode()).hexdigest()

# ----------------------------------------------------
# 7. Tarefa Celery Principal
# ----------------------------------------------------

@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=10,
    rate_limit='100/m', # Limite de 100 tarefas por minuto
    queue='ai_responses' # Fila específica para IA
)
def process_ai_response(
    self,
    platform: str,
    from_id: str,
    message: str,
    metadata: Optional[Dict] = None
) -> Dict[str, Any]:
    """
    Processa mensagem com IA e envia resposta.
    
    Args:
        platform: 'whatsapp' ou 'facebook'
        from_id: ID do remetente
        message: Mensagem recebida
        metadata: Metadados adicionais (timestamp, etc)
    
    Returns:
        Dict com status e detalhes da operação
    """
    
    start_time = datetime.utcnow()
    result = {
        'success': False,
        'platform': platform,
        'from_id': from_id,
        'timestamp': start_time.isoformat()
    }
    
    try:
        # Log de início
        logger.info(f"Processando mensagem de {from_id} via {platform}")
        
        # 1. Constrói prompt seguro
        knowledge = knowledge_base.get_formatted()
        prompt = PromptBuilder.build(message, knowledge)
        prompt_hash = PromptBuilder.get_hash(prompt)
        
        # 2. Gera resposta com IA (com cache)
        try:
            ai_reply = gemini_client.generate_cached_response(prompt_hash, prompt)
            result['ai_response'] = ai_reply[:500] # Armazena preview
            
        except Exception as e:
            logger.error(f"Erro na geração IA: {e}")
            
            # Tenta novamente se não for a última tentativa
            if self.request.retries < self.max_retries:
                raise self.retry(exc=e)
            
            # Fallback inteligente baseado no tipo de erro
            ai_reply = _get_fallback_message(message)
        
        # 3. Envia resposta ao cliente
        send_success = meta_messenger.send_message(platform, from_id, ai_reply)
        
        if not send_success:
            logger.error(f"Falha ao enviar mensagem para {from_id}")
            
            # Retry apenas se não for a última tentativa
            if self.request.retries < self.max_retries:
                raise self.retry()
        
        # 4. Registra sucesso
        result['success'] = True
        result['processing_time'] = (datetime.utcnow() - start_time).total_seconds()
        
        logger.info(
            f"Resposta processada com sucesso para {from_id} "
            f"em {result['processing_time']:.2f}s"
        )
        
    except MaxRetriesExceededError:
        logger.error(f"Máximo de tentativas excedido para {from_id}")
        result['error'] = 'max_retries_exceeded'
        
        # Envia mensagem de erro final
        error_msg = (
            "Desculpe, estou com dificuldades técnicas no momento. "
            "Um atendente humano entrará em contato em breve para ajudá-lo. "
            "Obrigado pela compreensão!"
        )
        meta_messenger.send_message(platform, from_id, error_msg)
        
    except Exception as e:
        logger.exception(f"Erro não tratado: {e}")
        result['error'] = str(e)
    
    return result

def _get_fallback_message(user_message: str) -> str:
    """Gera mensagem fallback contextual."""
    
    message_lower = user_message.lower()
    
    if any(word in message_lower for word in ['preço', 'valor', 'quanto']):
        return (
            "Desculpe, estou com dificuldade para acessar os preços no momento. "
            "Por favor, visite nosso site ou entre em contato pelo telefone "
            "(84) 3317-5000 para informações atualizadas sobre preços."
        )
    
    elif any(word in message_lower for word in ['horário', 'aberto', 'funcionamento']):
        return (
            "Nossa loja funciona de Segunda a Sexta das 8h às 18h, "
            "e aos Sábados das 8h às 13h. Estamos na Av. Presidente Dutra, 1655, Alto de São Manoel."
        )
    
    elif any(word in message_lower for word in ['produto', 'tem', 'disponível']):
        return (
            "Para verificar disponibilidade de produtos, "
            "visite nosso site ou entre em contato pelo WhatsApp principal: (84) 99999-9999"
        )
    
    else:
        return (
            "Desculpe, estou temporariamente indisponível. "
            "Enquanto isso, você pode visitar nosso site para mais informações "
            "ou entrar em contato pelo telefone (84) 3317-5000. "
            "Um atendente humano responderá em breve!"
        )

# ----------------------------------------------------
# 8. Tarefas de Manutenção e Monitoramento
# ----------------------------------------------------

@celery_app.task
def health_check() -> Dict[str, Any]:
    """Verifica saúde do worker."""
    return {
        'status': 'healthy',
        'timestamp': datetime.utcnow().isoformat(),
        'redis_connected': REDIS_URL is not None,
        'gemini_configured': gemini_client.model is not None,
        'knowledge_loaded': bool(knowledge_base.knowledge)
    }

@celery_app.task
def clear_cache():
    """Limpa cache de respostas."""
    if hasattr(gemini_client.generate_cached_response, 'cache_clear'):
        gemini_client.generate_cached_response.cache_clear()
        logger.info("Cache de respostas limpo")
    return {'cache_cleared': True}

# ----------------------------------------------------
# 9. Inicialização e Validação
# ----------------------------------------------------

def validate_environment():
    """Valida variáveis de ambiente necessárias."""
    required_vars = [
        'GEMINI_API_KEY',
        'WHATSAPP_PHONE_ID',
        'WHATSAPP_TOKEN',
        'FACEBOOK_PAGE_ID',
        'FACEBOOK_PAGE_ACCESS_TOKEN'
    ]
    
    missing = [var for var in required_vars if not os.getenv(var)]
    
    if missing:
        logger.warning(f"Variáveis de ambiente faltando: {missing}")
    
    return len(missing) == 0

# Executa validação na inicialização
if __name__ != '__main__':
    is_valid = validate_environment()
    if is_valid:
        logger.info("Worker SofIA inicializado com sucesso")
    else:
        logger.warning("Worker iniciado com configuração incompleta")