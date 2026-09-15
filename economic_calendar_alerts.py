"""
Alertes calendrier économique — événements à fort impact sur XAU/USD.

*** CE SCRIPT NE PRÉDIT JAMAIS UNE DIRECTION DE MARCHÉ ***
Il prévient seulement QU'UN événement connu pour provoquer de la volatilité
va se produire (NFP, CPI, décision de taux FOMC, etc.), avec la prévision
et la valeur précédente publiées par les économistes — jamais "ça va monter"
ou "ça va baisser". Personne ne peut connaître la réaction du marché à
l'avance ; ce serait mentir aux abonnés de prétendre le contraire.

Source des données : le flux hebdomadaire public de ForexFactory
(nfs.faireconomy.media), gratuit, sans clé API, utilisé par de nombreux
robots de trading. Limité à 2 requêtes / 5 minutes par ForexFactory —
ce script est donc prévu pour tourner au maximum toutes les 15-30 minutes,
jamais plus souvent.

Fonctionnement :
1. Télécharge le calendrier de la semaine.
2. Garde uniquement les événements USD à fort impact ("High") — ce sont
   ceux qui bougent XAU/USD (l'or est coté en dollars).
3. Pour chaque événement dont l'heure approche (fenêtre configurable,
   par défaut ~45-75 min avant), envoie UNE SEULE fois une alerte avec
   une image récapitulative, si elle n'a pas déjà été envoyée.
4. Le suivi des événements déjà alertés est sauvegardé dans
   economic_calendar_state.json (commité dans le dépôt), avec nettoyage
   automatique des entrées de plus de 7 jours.

Variables d'environnement requises (secrets GitHub, déjà utilisés par
les autres scripts du bot) : BOT_TOKEN, CHANNEL_ID.
"""

import os
import sys
import json
import traceback
import requests
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def log(message):
    print(message, flush=True)


# --- Configuration Telegram ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
TELEGRAM_SEND_PHOTO_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

# --- Configuration du calendrier économique ---
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
RELEVANT_CURRENCY = "USD"           # l'or est coté en dollars
RELEVANT_IMPACTS = {"High"}         # on ignore Low/Medium, trop de bruit

# --- Fenêtre d'alerte : le script doit tourner toutes les ALERT_WINDOW_MINUTES
# minutes au maximum (ex: 30) pour ne rater aucun événement dans la fenêtre. ---
ALERT_LEAD_MINUTES = 60             # on alerte ~1h avant l'événement
ALERT_WINDOW_MINUTES = 30           # tolérance = fréquence d'exécution du job

STATE_FILE = "economic_calendar_state.json"
CARD_IMAGE_PATH = "economic_calendar_card.png"

STATE_RETENTION_DAYS = 7            # nettoyage des vieilles entrées d'état


def load_state():
    default = {"alerted_event_ids": []}
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


def fetch_calendar():
    try:
        response = requests.get(CALENDAR_URL, timeout=20)
        response.raise_for_status()
        return response.json()
    except requests.RequestException as e:
        log(f"Erreur réseau lors de la récupération du calendrier : {e}")
        return None
    except (json.JSONDecodeError, ValueError) as e:
        log(f"Réponse du calendrier illisible (probablement limite de requêtes atteinte) : {e}")
        return None


def parse_event_time(raw_date: str):
    """Le flux ForexFactory donne une date ISO8601 avec fuseau horaire
    (ex: 2026-09-15T12:30:00-04:00). On la convertit en UTC."""
    try:
        dt = datetime.fromisoformat(raw_date)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def make_event_id(event: dict, event_time: datetime) -> str:
    title = event.get("title", "?")
    return f"{title}|{event_time.isoformat()}"


def generate_event_card(event: dict, event_time: datetime) -> str:
    bg = "#0d0f16"
    panel = "#171a24"
    gold = "#c6a34e"
    white = "#e8ecf2"
    grey = "#8a93a3"
    red = "#e74c3c"

    fig, ax = plt.subplots(figsize=(8, 4.6), facecolor=bg)
    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, 8)
    ax.set_ylim(0, 4.6)

    ax.add_patch(plt.Rectangle((0.3, 0.3), 7.4, 4.0, facecolor=panel, edgecolor=gold, linewidth=1.2))

    ax.text(4, 3.85, "⚠ ÉVÉNEMENT ÉCONOMIQUE À VENIR", ha="center", fontsize=15, fontweight="bold", color=red)
    ax.text(4, 3.4, "XAU GUARDIAN", ha="center", fontsize=12, color=gold, fontweight="bold")

    local_str = event_time.strftime("%d/%m/%Y à %H:%M UTC")
    ax.text(4, 2.85, event.get("title", "Événement"), ha="center", fontsize=15.5, fontweight="bold", color=white)
    ax.text(4, 2.45, local_str, ha="center", fontsize=11, color=grey)

    forecast = event.get("forecast") or "—"
    previous = event.get("previous") or "—"
    ax.text(2.6, 1.75, "Prévision (consensus)", ha="center", fontsize=9.5, color=grey)
    ax.text(2.6, 1.35, str(forecast), ha="center", fontsize=14, fontweight="bold", color=white)
    ax.text(5.4, 1.75, "Valeur précédente", ha="center", fontsize=9.5, color=grey)
    ax.text(5.4, 1.35, str(previous), ha="center", fontsize=14, fontweight="bold", color=white)
    ax.plot([4, 4], [1.1, 2.0], color="#333", linewidth=1)

    ax.text(4, 0.65, "Cet événement peut provoquer une forte volatilité sur XAU/USD.",
            ha="center", fontsize=8.6, color=grey)
    ax.text(4, 0.4, "Aucune direction n'est prévisible à l'avance — restez prudent.",
            ha="center", fontsize=8.6, color=grey)

    fig.savefig(CARD_IMAGE_PATH, facecolor=bg, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return CARD_IMAGE_PATH


def send_event_alert(event: dict, event_time: datetime) -> bool:
    image_path = generate_event_card(event, event_time)
    minutes_away = round((event_time - datetime.now(timezone.utc)).total_seconds() / 60)
    caption = (
        f"⚠️ <b>Événement à fort impact dans ~{minutes_away} min</b>\n\n"
        f"📅 {event.get('title', 'Événement économique')}\n"
        f"🕒 {event_time.strftime('%H:%M UTC')}\n\n"
        f"Ce type d'annonce provoque historiquement une forte volatilité sur "
        f"XAU/USD. On ne sait jamais à l'avance dans quel sens le marché va "
        f"réagir — soyez prudents sur vos positions en cours."
    )
    try:
        with open(image_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}
            response = requests.post(TELEGRAM_SEND_PHOTO_URL, data=data, files=files, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"Erreur envoi Telegram (image calendrier) : {e}")
        return False


def run_once():
    log("=== Vérification du calendrier économique ===")
    if not BOT_TOKEN or not CHANNEL_ID:
        log("Erreur : BOT_TOKEN ou CHANNEL_ID manquant (secrets non transmis).")
        sys.exit(1)

    state = load_state()
    alerted_ids = set(state.get("alerted_event_ids", []))

    calendar = fetch_calendar()
    if calendar is None:
        log("Calendrier non récupéré cette fois, on réessaiera au prochain lancement.")
        return

    now = datetime.now(timezone.utc)
    window_start = now + timedelta(minutes=ALERT_LEAD_MINUTES - ALERT_WINDOW_MINUTES / 2)
    window_end = now + timedelta(minutes=ALERT_LEAD_MINUTES + ALERT_WINDOW_MINUTES / 2)

    sent_count = 0
    for event in calendar:
        country = event.get("country", "")
        impact = event.get("impact", "")
        if country != RELEVANT_CURRENCY or impact not in RELEVANT_IMPACTS:
            continue

        event_time = parse_event_time(event.get("date", ""))
        if event_time is None:
            continue

        if not (window_start <= event_time <= window_end):
            continue

        event_id = make_event_id(event, event_time)
        if event_id in alerted_ids:
            continue

        log(f"Événement dans la fenêtre d'alerte : {event.get('title')} à {event_time.isoformat()}")
        sent = send_event_alert(event, event_time)
        log(f"Alerte calendrier envoyée : {sent}")
        if sent:
            alerted_ids.add(event_id)
            sent_count += 1

    # Nettoyage : on ne garde que les IDs d'événements des 7 derniers jours,
    # pour ne pas laisser grossir le fichier d'état indéfiniment.
    cutoff = now - timedelta(days=STATE_RETENTION_DAYS)
    cleaned_ids = []
    for event_id in alerted_ids:
        try:
            _, iso_time = event_id.rsplit("|", 1)
            event_time = datetime.fromisoformat(iso_time)
            if event_time >= cutoff:
                cleaned_ids.append(event_id)
        except ValueError:
            continue

    state["alerted_event_ids"] = cleaned_ids
    save_state(state)
    log(f"=== Fin de la vérification ({sent_count} alerte(s) envoyée(s)) ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)
