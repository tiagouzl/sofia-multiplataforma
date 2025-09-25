import os
from flask import Flask, request, jsonify, abort

app = Flask(__name__)

# Tokens obrigatórios via variáveis de ambiente
VERIFY_TOKEN = os.environ['VERIFY_TOKEN']
FACEBOOK_VERIFY_TOKEN = os.environ['FACEBOOK_VERIFY_TOKEN']

@app.route("/webhook/whatsapp", methods=['GET', 'POST'])
@app.route("/webhook/facebook", methods=['GET', 'POST'])
@app.route("/webhook/instagram", methods=['GET', 'POST'])
def webhook():
    # Detecta a plataforma com base na URL
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

        # Debug detalhado
        print("=== DEBUG WEBHOOK ===")
        print(f"Plataforma: {platform}")
        print(f"Mode: '{mode}'")
        print(f"Token recebido: '{token_raw}'")
        print(f"Token esperado: '{expected_token}'")
        print(f"Challenge: '{challenge}'")

        # Validação rigorosa
        if token_raw is None:
            print("❌ ERRO: Token não recebido")
            abort(400)

        token = token_raw.strip()

        if mode == 'subscribe' and token == expected_token:
            print("✅ SUCESSO - Webhook verificado!")
            return challenge, 200
        else:
            print("❌ ERRO - Token ou modo inválido")
            return f"Token inválido. Recebido: '{token}'", 403

    elif request.method == 'POST':
        # Recebe mensagens (sem processar ainda)
        data = request.get_json()
        print(f"[{platform}] Mensagem recebida:", data)
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)