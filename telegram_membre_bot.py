"""
Gestion des membres du canal XAU Guardian — version GitHub Actions.

Deux choses :
1. Accueil des nouveaux membres : le bot poste UNE FOIS un message épinglé
   dans le canal, invitant chacun à cliquer sur un lien pour lui écrire en
   privé. C'est ce clic qui déclenche le vrai message de bienvenue (avec le
   lien de parrainage broker) en message privé.
2. Réponses automatiques simples (mots-clés) aux questions posées en
   message privé au bot (FAQ : TP/SL, pips, inscription broker, risque...).

*** LIMITE IMPORTANTE DE TELEGRAM (pas un choix de conception) ***
Un bot ne peut JAMAIS envoyer un message privé à quelqu'un qui ne lui a pas
écrit en premier — même si cette personne rejoint un canal dont le bot est
administrateur. C'est une protection anti-spam de Telegram, impossible à
contourner. C'est pourquoi l'accueil fonctionne via un lien cliquable
(qui déclenche "/start" côté bot) plutôt que par un message automatique
envoyé dès qu'un membre rejoint le canal.

Fonctionne par sondage (polling) de l'API Telegram toutes les 5 minutes via
GitHub Actions — pas de serveur à maintenir. Le dernier update_id traité est
sauvegardé dans bot_state.json (commité dans le dépôt) pour ne jamais
retraiter le même message deux fois.
"""

import os
import sys
import json
import traceback
import requests
from datetime import datetime, timezone


def log(message):
    print(message, flush=True)


# --- Configuration ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # optionnel : notif privée à l'admin
BOT_USERNAME = os.environ.get("BOT_USERNAME", "TradePulse38Bot")
AVATRADE_LINK = os.environ.get("AVATRADE_LINK", "[LIEN_AVATRADE_A_CONFIGURER]")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_GET_UPDATES_URL = f"{TELEGRAM_API}/getUpdates"
TELEGRAM_SEND_MESSAGE_URL = f"{TELEGRAM_API}/sendMessage"
TELEGRAM_PIN_MESSAGE_URL = f"{TELEGRAM_API}/pinChatMessage"

STATE_FILE = "bot_state.json"

# --- Message posté (une seule fois) et épinglé dans le canal ---
CHANNEL_INVITE_TEXT = (
    "📌 <b>Bienvenue sur XAU Guardian !</b>\n\n"
    "Pour recevoir le message de bienvenue en privé, débloquer l'offre de "
    "notre broker partenaire et pouvoir poser tes questions, clique ici :\n"
    f"👉 https://t.me/{BOT_USERNAME}?start=welcome\n\n"
    "Les alertes automatiques suivent juste en dessous. Bon trading !"
)

# --- Message envoyé en privé quand quelqu'un fait /start ---
WELCOME_MESSAGE = (
    "👋
