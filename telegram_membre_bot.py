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
VTMARKETS_LINK = os.environ.get("VTMARKETS_LINK", "https://www.vtmarkets.com/trade-now/?utm_source=promo&utm_medium=social&utm_campaign=RAF&utm_term=NA&utm_content=NA&c=F9vORspqDcbRngh10H6f8A%3D%3D")


TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
TELEGRAM_GET_UPDATES_URL = f"{TELEGRAM_API}/getUpdates"
TELEGRAM_SEND_MESSAGE_URL = f"{TELEGRAM_API}/sendMessage"
TELEGRAM_PIN_MESSAGE_URL = f"{TELEGRAM_API}/pinChatMessage"

STATE_FILE = "bot_state.json"

CHANNEL_INVITE_TEXT = (
    "Bienvenue sur XAU Guardian !\n\n"
    "Pour recevoir le message de bienvenue en privé, débloquer l'offre de "
    "notre broker partenaire et pouvoir poser tes questions, clique ici :\n"
    f"https://t.me/{BOT_USERNAME}?start=welcome\n\n"
    "Les alertes automatiques suivent juste en dessous. Bon trading !"
)

WELCOME_MESSAGE = (
    "Bienvenue sur XAU Guardian !\n\n"
    "Tu vas recevoir dans le canal des alertes automatiques sur l'or "
    "(XAU/USD), générées par un bot qui analyse le marché en continu.\n\n"
    "🎁 BONUS DE BIENVENUE 100% sur votre premier dépôt (jusqu'à 1 000 $) "
    "+ 20% sur vos dépôts suivants (jusqu'à 10 000 $ au total) !\n\n"
    f"👉 {VTMARKETS_LINK}\n\n"
    "⚡ Ouverture de compte rapide\n"
    "💰 Plus de capital pour trader\n"
    "📈 Idéal pour accompagner nos signaux XAU Guardian\n\n"
    "ℹ️ Le bonus doit être débloqué via un volume de trading (voir "
    "conditions sur le site). Offre soumise aux T&C de VT Markets.\n\n"
    "⚠️ Le trading comporte des risques, tradez de manière responsable.\n\n"
    "📚 Envie d'apprendre le trading en plus de suivre nos alertes ? "
    "Découvre \"Le Prompt Formation Trading — De Zéro à Autonome\", un "
    "prompt qui transforme ton IA en formateur trading personnel : "
    "14,99€ → https://mezraoui.gumroad.com/l/trade20\n\n"
    "Tu peux aussi me poser tes questions ici en privé — tape \"aide\" "
    "pour voir ce que je sais expliquer automatiquement."
)



FAQ = [
    (["aide", "help", "menu", "commande"], (
        "Voici ce que je peux t'expliquer automatiquement :\n"
        "- \"tp1\" / \"tp2\" / \"sl\" : les niveaux dans les alertes\n"
        "- \"pips\" : comment est calculé un pip sur XAU/USD\n"
        "- \"broker\" / \"inscription\" : comment s'inscrire via le lien partenaire\n"
        "- \"risque\" : rappel sur la gestion du risque\n"
        "- \"horaires\" : quand le marché est ouvert"
    )),
    (["tp1", "tp2", "take profit"], (
        "TP1 et TP2 sont des objectifs de prix (\"Take Profit\") où une partie des "
        "gains est généralement sécurisée. TP1 est le plus proche, TP2 plus loin. "
        "\"TP3 : Ouvert\" dans nos alertes veut dire qu'après TP2, la position peut "
        "continuer sans objectif fixe si le mouvement se poursuit."
    )),
    (["sl", "stop loss", "stoploss"], (
        "Le SL (\"Stop Loss\") est le niveau de prix auquel la position est "
        "considérée clôturée pour limiter la perte si le marché part dans le "
        "mauvais sens. Il est calculé automatiquement selon la volatilité du "
        "marché (ATR), pas à la main."
    )),
    (["pip", "pips"], (
        "Sur nos alertes XAU/USD, 1 pip = 0,10$ de mouvement de prix. "
        "En 0.01 lot (micro-lot), 1 pip vaut environ 0,10€ — c'est une "
        "estimation basée sur une spécification de contrat standard, hors "
        "spread et commissions."
    )),
      (["broker", "inscription", "inscrire", "compte", "vtmarkets", "parrain"], (
        "Pour trader ces alertes avec un broker, inscris-toi via mon lien "
        f"partenaire VT Markets :\n{VTMARKETS_LINK}\n"
        "Bonus de 100% sur ton premier dépôt (jusqu'à 1000$). Ça ne coûte "
        "rien de plus et ça soutient le canal."
    )),

    (["formation", "apprendre", "prompt", "cours"], (
        "Pour apprendre le trading en profondeur, découvre \"Le Prompt "
        "Formation Trading — De Zéro à Autonome\" : 14,99€ → "
        "https://mezraoui.gumroad.com/l/trade20"
    )),

    (["risque", "risk", "combien miser", "combien trader", "taille"], (
       "Rappel important : ne mise jamais plus que ce que tu peux te permettre "
        "de perdre. Les alertes sont informatives, ce n'est pas un conseil "
        "financier personnalisé. Adapte toujours la taille de ta position à "
        "ton capital et à ta tolérance au risque."
    )),
    (["horaire", "ouvert", "ferme", "marche", "week-end", "weekend"], (
        "Le marché de l'or (XAU/USD) est ouvert du dimanche soir au vendredi "
        "soir avec une courte pause chaque jour. Le bot ne génère pas d'alertes "
        "quand le marché est fermé."
    )),
]

FALLBACK_MESSAGE = (
    "Je n'ai pas de réponse automatique pour cette question précise. "
    "Tape \"aide\" pour voir ce que je sais expliquer, ou pose ta question "
    "directement dans le canal, quelqu'un de l'équipe pourra te répondre."
)


def load_state():
    default = {"last_update_id": 0, "channel_invite_posted": False}
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            for k, v in default.items():
                data.setdefault(k, v)
            return data
    except (json.JSONDecodeError, OSError):
        return default


def save_state(state: dict):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def fetch_updates(offset: int):
    params = {
        "offset": offset,
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "chat_member"]),
    }
    try:
        response = requests.get(TELEGRAM_GET_UPDATES_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            log(f"Réponse Telegram getUpdates non OK : {data}")
            return []
        return data.get("result", [])
    except requests.RequestException as e:
        log(f"Erreur réseau getUpdates : {e}")
        return []


def send_dm(chat_id, text: str) -> bool:
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        response = requests.post(TELEGRAM_SEND_MESSAGE_URL, data=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"Erreur envoi message à {chat_id} : {e}")
        return False


def ensure_channel_invite_posted(state: dict):
    if state.get("channel_invite_posted") or not CHANNEL_ID:
        return
    payload = {"chat_id": CHANNEL_ID, "text": CHANNEL_INVITE_TEXT, "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        response = requests.post(TELEGRAM_SEND_MESSAGE_URL, data=payload, timeout=10)
        response.raise_for_status()
        result = response.json()
        message_id = result.get("result", {}).get("message_id")
        log(f"Message d'invitation posté dans le canal (message_id={message_id}).")
        if message_id:
            pin_resp = requests.post(TELEGRAM_PIN_MESSAGE_URL, data={"chat_id": CHANNEL_ID, "message_id": message_id}, timeout=10)
            log(f"Épinglage du message d'invitation : HTTP {pin_resp.status_code}")
        state["channel_invite_posted"] = True
    except requests.RequestException as e:
        log(f"Erreur lors de la publication du message d'invitation : {e}")


def match_faq(text: str):
    text_lower = text.lower()
    for keywords, answer in FAQ:
        if any(kw in text_lower for kw in keywords):
            return answer
    return None


def handle_message(message: dict):
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    text = message.get("text", "") or ""

    if chat_type != "private" or chat_id is None:
        return

    first_name = chat.get("first_name", "quelqu'un")
    log(f"Message reçu de {first_name} ({chat_id}) : {text!r}")

    if text.startswith("/start"):
        sent = send_dm(chat_id, WELCOME_MESSAGE)
        log(f"Message de bienvenue envoyé à {first_name} : {sent}")
        return

    answer = match_faq(text)
    sent = send_dm(chat_id, answer if answer else FALLBACK_MESSAGE)
    log(f"Réponse {'FAQ' if answer else 'par défaut'} envoyée à {first_name} : {sent}")


def handle_chat_member_update(update: dict):
    cmu = update.get("chat_member")
    if not cmu or not ADMIN_CHAT_ID:
        return
    chat = cmu.get("chat", {})
    if CHANNEL_ID and str(chat.get("id")) != str(CHANNEL_ID):
        return
    old_status = (cmu.get("old_chat_member") or {}).get("status")
    new_status = (cmu.get("new_chat_member") or {}).get("status")
    if new_status == "member" and old_status in ("left", "kicked", None):
        user = (cmu.get("new_chat_member") or {}).get("user", {})
        username = user.get("username")
        who = f"@{username}" if username else user.get("first_name", "quelqu'un")
        log(f"Nouveau membre détecté dans le canal : {who}")
        send_dm(ADMIN_CHAT_ID, f"Nouveau membre dans XAU Guardian : {who}")


def run_once():
    log("=== Vérification des messages membres ===")
    if not BOT_TOKEN:
        log("Erreur : BOT_TOKEN manquant (secret non transmis).")
        sys.exit(1)

    state = load_state()
    ensure_channel_invite_posted(state)

    last_update_id = state.get("last_update_id", 0)
    updates = fetch_updates(offset=last_update_id + 1)
    log(f"{len(updates)} nouvelle(s) mise(s) à jour reçue(s).")

    max_update_id = last_update_id
    for update in updates:
        update_id = update.get("update_id", 0)
        max_update_id = max(max_update_id, update_id)

        message = update.get("message")
        if message:
            try:
                handle_message(message)
            except Exception:
                log("Erreur lors du traitement d'un message :")
                log(traceback.format_exc())

        try:
            handle_chat_member_update(update)
        except Exception:
            log("Erreur lors du traitement d'un événement membre :")
            log(traceback.format_exc())

    state["last_update_id"] = max_update_id
    save_state(state)
    log("=== Fin de la vérification ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)
