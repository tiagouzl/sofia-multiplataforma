import os
from flask import Flask, request, jsonify

app = Flask(__name__)

# Tokens de verificação (vêm do Railway ou .env)
VERIFY_TOKEN = os.environ.get('VERIFY_TOKEN', 'meu_token_de_verificacao')
FACEBOOK_VERIFY_TOKEN = os.environ.get('FACEBOOK_VERIFY_TOKEN', 'meu_facebook_verify_token')

@app.route("/webhook/whatsapp", methods=['GET', 'POST'])
@app.route("/webhook/facebook", methods=['GET', 'POST'])
@app.route("/webhook/instagram", methods=['GET', 'POST'])
def webhook():
    # Detecta qual plataforma está chamando
    if 'whatsapp' in request.url:
        expected_token = VERIFY_TOKEN
    else:
        expected_token = FACEBOOK_VERIFY_TOKEN

    # Verificação do webhook (GET)
    if request.method == 'GET':
        if request.args.get("hub.verify_token") == expected_token:
            return request.args.get("hub.challenge"), 200
        return "Token inválido", 403

    # Recebe mensagem (POST)
    elif request.method == 'POST':
        print("Mensagem recebida:", request.get_json())
        return "OK", 200

@app.route("/status")
def status():
    return jsonify({
        "status": "online",
        "bot": "SofIA - Dinâmica Sports"
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)