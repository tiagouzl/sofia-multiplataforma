import os
import requests
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

# Variáveis obrigatórias via ambiente
VERIFY_TOKEN = os.environ['VERIFY_TOKEN']
FACEBOOK_VERIFY_TOKEN = os.environ['FACEBOOK_VERIFY_TOKEN']
WHATSAPP_TOKEN = os.environ['WHATSAPP_TOKEN']
PHONE_NUMBER_ID = os.environ['PHONE_NUMBER_ID']

@app.route("/webhook/whatsapp", methods=['GET', 'POST'])
@app.route("/webhook/facebook", methods=['GET', 'POST'])
@app.route("/webhook/instagram", methods=['GET', 'POST'])
def webhook():
    # Detecta plataforma pela URL
    if 'whatsapp' in request.url:
        expected_token = VERIFY_TOKEN
        platform = "WhatsApp"
    else:
        expected_token = FACEBOOK_VERIFY_TOKEN
        platform = "Facebook/Instagram"

    if request.method == 'GET':
        mode = request.args.get("hub.mode")
        token_raw = request.args.get("hub.verify_token")
        challenge = request.args.get("hub.challenge")

        if token_raw is None:
            abort(400)

        token = token_raw.strip()

        if mode == 'subscribe' and token == expected_token:
            print(f"✅ Webhook verificado para {platform}!")
            return challenge, 200
        else:
            return "Token ou modo inválido", 403

    elif request.method == 'POST':
        data = request.get_json()
        print(f"[{platform}] Mensagem recebida:", data)

        if platform == "WhatsApp":
            process_whatsapp_message(data)

        return "OK", 200

@app.route("/status")
def status():
    return jsonify({
        "status": "online",
        "bot": "SofIA - Dinâmica Sports",
        "platforms": ["WhatsApp", "Facebook Messenger", "Instagram"],
        "verify_token_length": len(VERIFY_TOKEN),
        "facebook_verify_token_length": len(FACEBOOK_VERIFY_TOKEN)
    })

def process_whatsapp_message(data):
    try:
        # Valida estrutura mínima
        if "entry" not in data or not data["entry"]:
            print("[WhatsApp] Payload inválido: sem 'entry'")
            return

        entry = data["entry"][0]
        if "changes" not in entry or not entry["changes"]:
            print("[WhatsApp] Payload inválido: sem 'changes'")
            return

        change = entry["changes"][0]
        if "value" not in change:
            print("[WhatsApp] Payload inválido: sem 'value'")
            return

        value = change["value"]
        if "messages" not in value or not value["messages"]:
            print("[WhatsApp] Payload sem mensagens")
            return

        message_data = value["messages"][0]
        from_number = message_data.get("from")
        message_type = message_data.get("type")

        if message_type == "text":
            message_body = message_data["text"]["body"]
            print(f"[WhatsApp] Mensagem de {from_number}: {message_body}")
            send_whatsapp_message(from_number, "Olá! Sou a SofIA, atendente virtual da Dinâmica Sports. Como posso te ajudar?")

    except Exception as e:
        print(f"[WhatsApp] Erro ao processar mensagem: {e}")

def send_whatsapp_message(to_number, message):
    # 🔥 Corrigido: sem espaços na URL!
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message},
    }

    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        print(f"[WhatsApp] Mensagem enviada para {to_number}!")
    except requests.exceptions.RequestException as e:
        print(f"[WhatsApp] Erro ao enviar mensagem: {e}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)