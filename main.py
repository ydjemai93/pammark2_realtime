"""
main.py - pam_markII

Ce script Python gère un assistant vocal en temps réel avec Twilio (Media Streams)
et l'API Realtime d'OpenAI (GPT-4o) pour la synthèse vocale.
Il gère à la fois les appels entrants et sortants.

Usage:
  1) Configurez vos variables d'environnement (OPENAI_API_KEY, TWILIO_ACCOUNT_SID, etc.).
  2) Lancez: python main.py
  3) Pour un appel entrant, configurez Twilio pour pointer vers https://<votre_domaine>/incoming-call
  4) Pour un appel sortant, effectuez un POST sur /make-outbound-call en fournissant le numéro "to"
"""

import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv

# Charger les variables d'environnement
load_dotenv()

# Récupérer les clés API et autres variables
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')
SERVER = os.getenv('SERVER')  # exemple: yourdomain.ngrok.io (sans protocole)

# Port par défaut
PORT = int(os.getenv('PORT', 5050))

# Configuration TTS via OpenAI Audio API (streaming realtime TTS HD)
TTS_MODEL = "tts-1-hd"
TTS_VOICE = "alloy"

# Prompts pour le comportement de l'assistant
SYSTEM_MESSAGE = (
    "You are a helpful AI assistant for a call center. Answer in a friendly and natural manner without "
    "mechanically listing your features."
)
INITIAL_ASSISTANT_MESSAGE = "Bonjour, ici Pam. Merci d’avoir pris contact. Comment puis-je vous aider aujourd’hui ?"

# Base de conversation initiale (on l'injecte une seule fois)
BASE_CONVERSATION = [
    { "role": "system", "content": SYSTEM_MESSAGE },
    { "role": "assistant", "content": INITIAL_ASSISTANT_MESSAGE }
]

# Nombre maximum d'échanges conservés dans l'historique (pour limiter le contexte)
CONVERSATION_HISTORY_LIMIT = 4

# Création de l'application FastAPI
app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError("Missing the OpenAI API key. Please set it in the .env file.")

# ---------------------------
# Endpoint pour les appels entrants
# ---------------------------
@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """
    Renvoie un TwiML pour un appel entrant.
    Le TwiML contient un <Connect><Stream> qui redirige l'appel vers le WS /media-stream.
    """
    response = VoiceResponse()
    response.say("Please wait while we connect your call to our AI voice assistant.")
    response.pause(length=1)
    response.say("You may start talking now.")
    host = request.url.hostname
    domain = SERVER if SERVER else host
    # Ici, domain doit être sans protocole, par exemple "yourdomain.ngrok.io"
    connect = Connect()
    connect.stream(url=f"wss://{domain}/media-stream")
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

# ---------------------------
# Endpoint pour les appels sortants
# ---------------------------
@app.post("/make-outbound-call")
async def make_outbound_call(request: Request):
    """
    Déclenche un appel sortant via Twilio en utilisant le même TwiML que pour les appels entrants.
    """
    from twilio.rest import Client as TwilioRestClient
    data = await request.json()
    to_number = data.get("to")
    if not to_number:
        return JSONResponse({"error": "'to' number is required"}, status_code=400)
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return JSONResponse({"error": "Twilio credentials are missing"}, status_code=500)
    client = TwilioRestClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    domain = SERVER if SERVER else request.url.hostname
    if not domain.startswith("http"):
        domain = "https://" + domain
    twiml_url = f"{domain}/incoming-call"
    try:
        call = client.calls.create(
            to=to_number,
            from_=TWILIO_PHONE_NUMBER,
            url=twiml_url
        )
        return JSONResponse({"success": True, "callSid": call.sid})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

# ---------------------------
# WebSocket /media-stream : gestion du pipeline audio
# ---------------------------
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """
    WebSocket pour gérer le flux audio entre Twilio et l'API Realtime d'OpenAI.
    Pipeline: Twilio -> pam_markII -> OpenAI Realtime -> pam_markII -> Twilio
    """
    print("Client connected (Twilio side) - pam_markII media-stream")
    await websocket.accept()
    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws)
        await asyncio.gather(
            receive_from_twilio(websocket, openai_ws),
            send_to_twilio(websocket, openai_ws)
        )

async def receive_from_twilio(ws_twilio, openai_ws):
    """
    Reçoit l'audio et les événements de Twilio et les transmet à OpenAI.
    """
    try:
        async for message in ws_twilio.iter_text():
            data = json.loads(message)
            if data.get("event") == "media" and openai_ws.open:
                audio_payload = data["media"]["payload"]
                audio_append = {
                    "type": "input_audio_buffer.append",
                    "audio": audio_payload
                }
                await openai_ws.send(json.dumps(audio_append))
            elif data.get("event") == "start":
                print("Incoming stream started from Twilio")
    except Exception as e:
        print("Error in receive_from_twilio:", e)

async def send_to_twilio(ws_twilio, openai_ws):
    """
    Reçoit les événements et l'audio TTS depuis OpenAI et les transmet à Twilio.
    """
    try:
        async for openai_message in openai_ws:
            response = json.loads(openai_message)
            if response.get("type") == "response.audio.delta" and response.get("delta"):
                await ws_twilio.send_json({
                    "event": "media",
                    "media": { "payload": response["delta"] }
                })
    except Exception as e:
        print("Error in send_to_twilio:", e)

async def initialize_session(openai_ws):
    """
    Initialise la session Realtime d'OpenAI (voix, instructions, etc.).
    """
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"},
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": TTS_VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

#---------------------------
# L'interaction conversationnelle via WS
#---------------------------
# Pour cet exemple, le pipeline de conversation est géré via OpenAI dans le WS de Realtime.
# Ici, vous pouvez ajouter ou adapter la logique de conversation selon vos besoins.

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
