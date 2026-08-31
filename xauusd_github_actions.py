"""
Script d'alertes de trading XAUUSD vers Telegram — version GitHub Actions (v3).
Deux sources de signaux : croisement SMA5/13 + sortie de zone RSI14.
Suivi des positions (TP1/TP2/SL touchés) + bilan hebdomadaire en image
envoyé automatiquement à la clôture du marché (vendredi soir, ~21h UTC).

Différences avec la version "PC" :
- S'exécute UNE SEULE FOIS par lancement (GitHub Actions le relance périodiquement).
- L'état complet (SMA, RSI, position ouverte, historique de la semaine) est
  sauvegardé dans state.json, commité dans le dépôt entre deux exécutions.
- BOT_TOKEN, TWELVEDATA_API_KEY et CHANNEL_ID sont lus depuis les variables
  d'environnement (GitHub Secrets), jamais écrits en clair dans ce fichier.
"""

import os
import sys
import json
import traceback
import requests
from datetime import datetime, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt


def log(message):
    """Affiche un message en forçant l'affichage immédiat (anti-buffering)."""
    print(message, flush=True)


# --- Configuration Telegram ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID")  # optionnel : alertes privées de panne
TELEGRAM_SEND_MESSAGE_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
TELEGRAM_SEND_PHOTO_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

# --- Configuration de la surveillance de panne ---
FAILURE_ALERT_THRESHOLD = 3  # nombre d'échecs consécutifs avant d'alerter l'admin

# --- Configuration Twelve Data ---
TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")
SYMBOL = "XAU/USD"
INTERVAL = "5min"
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"

# --- Configuration SMA ---
SHORT_WINDOW = 5
LONG_WINDOW = 13

# --- Configuration RSI ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# --- Configuration Take Profit / Stop Loss (basé sur l'ATR) ---
ATR_PERIOD = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP1_MULTIPLIER = 1.0
ATR_TP2_MULTIPLIER = 2.5
ATR_MIN_THRESHOLD = 0.5  # plancher absolu de sécurité (marché quasiment mort)

# --- Configuration du filtre de qualité des signaux ---
# Objectif : moins de signaux, mais plus fiables. Trois garde-fous s'ajoutent
# au croisement SMA et à la sortie de zone RSI bruts :
TREND_WINDOW = 50               # SMA "de fond" utilisée comme filtre de tendance générale
ATR_BASELINE_PERIOD = 50        # période de référence pour juger si la volatilité actuelle est normale
ATR_RELATIVE_MIN_RATIO = 0.6    # l'ATR courant doit valoir au moins 60% de sa moyenne récente
MIN_CROSSOVER_ATR_RATIO = 0.15  # écart minimum SMA courte/longue au croisement (fraction de l'ATR) pour ignorer les croisements "au ras des pâquerettes"
SL_COOLDOWN_CANDLES = 3         # nb de bougies (5 min) à attendre après un SL avant d'accepter un nouveau signal

# --- Configuration du suivi de position (pips) ---
PIP_SIZE = 0.1  # 1 pip = 0.1 sur XAU/USD

# --- Conversion pips -> euros pour le bilan (estimation) ---
# Base : spécification de contrat standard XAU/USD (100 oz par lot de 1.00),
# la plus répandue chez les brokers. À 0.01 lot, 1 pip du bot (0,10$ de
# mouvement) vaut 100 oz x 0.01 x 0,10$ = 0,10$/pip, assimilé ici à 0,10€/pip.
# C'est une ESTIMATION : elle ne tient pas compte du spread, des commissions,
# ni du taux de change USD/EUR réel — le contrat exact peut varier selon ton
# broker (à vérifier dans ses spécifications XAUUSD si besoin de précision).
PIP_VALUE_EUR_PER_001_LOT = 0.10

# --- Configuration du bilan hebdomadaire ---
WEEKLY_SUMMARY_WEEKDAY = 4   # 0=lundi ... 4=vendredi
WEEKLY_SUMMARY_HOUR_UTC = 21  # heure UTC à partir de laquelle on considère le marché fermé

# --- Configuration du point marché quotidien ---
DAILY_BRIEFING_HOUR_UTC = 7  # ~9h à Paris (hors changement d'heure) — ajustable

STATE_FILE = "state.json"
SUMMARY_IMAGE_PATH = "weekly_summary.png"


def load_state():
    default = {
        "last_sma_signal": None,
        "last_rsi_zone": None,
        "last_candle_time": None,
        "open_trade": None,
        "weekly_trades": [],
        "last_summary_week": None,
        "consecutive_failures": 0,
        "admin_alerted_for_streak": False,
        "last_daily_briefing_date": None,
        "cooldown_candles_remaining": 0,
    }
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


def send_admin_alert(message: str) -> bool:
    """Envoie un message privé à l'administrateur (toi), séparé du canal public."""
    if not ADMIN_CHAT_ID:
        log("ADMIN_CHAT_ID non configuré, alerte de panne non envoyée (mais consignée dans les logs).")
        return False
    payload = {
        "chat_id": ADMIN_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }
    try:
        response = requests.post(TELEGRAM_SEND_MESSAGE_URL, data=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"[{datetime.now(timezone.utc)}] Erreur envoi alerte admin : {e}")
        return False


def send_alert(message: str) -> bool:
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(TELEGRAM_SEND_MESSAGE_URL, data=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"[{datetime.now(timezone.utc)}] Erreur envoi Telegram (message) : {e}")
        return False


def send_photo(image_path: str, caption: str) -> bool:
    try:
        with open(image_path, "rb") as f:
            files = {"photo": f}
            data = {"chat_id": CHANNEL_ID, "caption": caption, "parse_mode": "HTML"}
            response = requests.post(TELEGRAM_SEND_PHOTO_URL, data=data, files=files, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"[{datetime.now(timezone.utc)}] Erreur envoi Telegram (photo) : {e}")
        return False


def format_alert(action: str, price: float, tp1: float, tp2: float, stop_loss: float, note: str = "") -> str:
    emoji = "🟢" if action == "BUY" else "🔴"
    action_label = "J'ACHÈTE" if action == "BUY" else "JE VENDS"
    msg = (
        f"{emoji} {action_label} {SYMBOL} à {price:.0f}\n\n"
        f"🎯 TP1 : {tp1:.0f}\n"
        f"🎯 TP2 : {tp2:.0f}\n"
        f"🎯 TP3 : Ouvert\n\n"
        f"🔒 SL : {stop_loss:.0f}"
    )
    if note:
        msg += f"\n\n{note}"
    return msg


def fetch_candles():
    outputsize = max(LONG_WINDOW, ATR_PERIOD, RSI_PERIOD, TREND_WINDOW, ATR_BASELINE_PERIOD) + 20
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": outputsize,
        "apikey": TWELVEDATA_API_KEY,
    }
    log(f"Appel à Twelve Data : symbol={SYMBOL}, interval={INTERVAL}, outputsize={outputsize}")
    try:
        response = requests.get(TWELVEDATA_URL, params=params, timeout=15)
        log(f"Twelve Data a répondu avec le code HTTP {response.status_code}")
        response.raise_for_status()
        data = response.json()
        if "values" not in data:
            log(f"Réponse inattendue de Twelve Data (pas de 'values') : {data}")
            return None
        candles = [
            {
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"]),
                "datetime": c["datetime"],
            }
            for c in reversed(data["values"])
        ]
        log(f"{len(candles)} bougies récupérées avec succès.")
        return candles
    except requests.RequestException as e:
        log(f"Erreur réseau lors de la récupération des prix : {e}")
        return None


def simple_moving_average(values, window):
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def average_true_range(candles, period):
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return sum(true_ranges[-period:]) / period


def relative_strength_index(closes, period):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def rsi_zone(rsi_value):
    if rsi_value >= RSI_OVERBOUGHT:
        return "overbought"
    if rsi_value <= RSI_OVERSOLD:
        return "oversold"
    return "neutral"


def compute_tp_sl(action: str, entry_price: float, atr: float):
    sl_distance = atr * ATR_SL_MULTIPLIER
    tp1_distance = atr * ATR_TP1_MULTIPLIER
    tp2_distance = atr * ATR_TP2_MULTIPLIER
    if action == "BUY":
        stop_loss = entry_price - sl_distance
        tp1 = entry_price + tp1_distance
        tp2 = entry_price + tp2_distance
    else:
        stop_loss = entry_price + sl_distance
        tp1 = entry_price - tp1_distance
        tp2 = entry_price - tp2_distance
    return tp1, tp2, stop_loss


def pips_between(price_a: float, price_b: float) -> int:
    """Calcule l'écart entre deux prix en pips (1 pip = PIP_SIZE)."""
    return round(abs(price_a - price_b) / PIP_SIZE)


def signed_pips(action: str, entry: float, exit_price: float) -> int:
    """Écart en pips entre l'entrée et un prix de sortie, signé : positif si
    le trade est gagnant pour la direction prise (BUY ou SELL), négatif sinon."""
    diff = (exit_price - entry) if action == "BUY" else (entry - exit_price)
    return round(diff / PIP_SIZE)


def format_tp_hit(level_name: str, entry: float, level_price: float) -> str:
    pips = pips_between(entry, level_price)
    return f"🎯 {level_name} TOUCHÉ 🔥\n{SYMBOL} +{pips} pips ✅"


def format_sl_hit(entry: float, sl_price: float) -> str:
    pips = pips_between(entry, sl_price)
    return f"🔒 SL TOUCHÉ ❌\n{SYMBOL} -{pips} pips"


def format_manual_close(action: str, entry: float, exit_price: float) -> str:
    """Message envoyé quand une position encore ouverte est clôturée au prix
    courant parce qu'un nouveau signal (SMA ou RSI) vient d'arriver, pour ne
    jamais perdre le suivi d'un trade en cours ni fausser le bilan."""
    pips = signed_pips(action, entry, exit_price)
    sign = "+" if pips >= 0 else ""
    emoji = "✅" if pips >= 0 else "❌"
    return f"🔄 POSITION CLÔTURÉE (nouveau signal) {emoji}\n{SYMBOL} {sign}{pips} pips"


def already_in_direction(open_trade, action: str) -> bool:
    """Vrai si une position est déjà ouverte dans la même direction que le
    nouveau signal — évite de clôturer puis rouvrir immédiatement la même
    position (au même prix) si le SMA et le RSI signalent la même direction
    au même run, ce qui ne ferait qu'envoyer une alerte redondante."""
    return open_trade is not None and not open_trade.get("closed") and open_trade.get("action") == action


def event_date(candle_datetime: str) -> str:
    """Extrait la date (JJ/MM) d'un horodatage de bougie, pour regrouper le
    bilan par jour dans l'image récapitulative."""
    date_part = candle_datetime.split(" ")[0]  # "YYYY-MM-DD"
    year, month, day = date_part.split("-")
    return f"{day}/{month}"


def close_previous_trade_if_open(open_trade, current_price: float, weekly_trades: list, current_candle_time: str):
    """Si une position est encore ouverte (ni TP2 ni SL touché), la clôture au
    prix courant AVANT qu'un nouveau signal ne l'écrase dans l'état. Sans ça,
    un trade en cours disparaît silencieusement : plus aucun message de suivi
    n'est envoyé au canal, et il n'est jamais compté dans le bilan hebdomadaire
    (ni gagnant, ni perdant), ce qui fausse le taux de réussite affiché.
    Renvoie le message à envoyer, ou None s'il n'y avait rien à clôturer."""
    if open_trade is None or open_trade.get("closed"):
        return None
    message = format_manual_close(open_trade["action"], open_trade["entry"], current_price)
    pips = signed_pips(open_trade["action"], open_trade["entry"], current_price)
    weekly_trades.append({
        "label": "FLIP",
        "pips": pips,
        "date": event_date(current_candle_time),
        "action": open_trade["action"],
    })
    open_trade["closed"] = True
    return message


def check_open_trade(candles: list, open_trade: dict):
    """
    Vérifie si TP1, TP2 ou le SL de la position ouverte ont été touchés,
    en examinant le plus haut/plus bas de CHAQUE bougie récupérée (pas
    seulement le dernier prix de clôture) — ça évite de rater un niveau
    touché brièvement entre deux vérifications du bot.
    Renvoie (messages_a_envoyer, open_trade_mis_a_jour, evenements).
    Chaque événement est un dict {label, pips, date, action} — la date et la
    direction (BUY/SELL) permettent de regrouper le bilan par jour et
    d'afficher "ACHAT OR"/"VENTE OR" dans l'image récapitulative.
    """
    if open_trade is None or open_trade.get("closed"):
        return [], open_trade, []

    messages = []
    events = []
    action = open_trade["action"]
    entry = open_trade["entry"]
    entry_time = open_trade.get("entry_time", "")
    tp1, tp2, sl = open_trade["tp1"], open_trade["tp2"], open_trade["sl"]

    # On ne regarde que les bougies survenues APRÈS l'ouverture de la position,
    # pour ne pas déclencher un faux signal à partir de prix antérieurs à l'entrée.
    relevant_candles = [c for c in candles if c["datetime"] > entry_time]

    for candle in relevant_candles:
        if open_trade.get("closed"):
            break
        high, low = candle["high"], candle["low"]
        candle_date = event_date(candle["datetime"])

        if action == "BUY":
            # SL prioritaire si le SL et un TP sont touchés dans la même bougie (prudence)
            if not open_trade.get("closed") and low <= sl:
                messages.append(format_sl_hit(entry, sl))
                events.append({"label": "SL", "pips": -pips_between(entry, sl), "date": candle_date, "action": action})
                open_trade["closed"] = True
                continue
            if not open_trade.get("tp1_hit") and high >= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                events.append({"label": "TP1", "pips": pips_between(entry, tp1), "date": candle_date, "action": action})
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and high >= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                events.append({"label": "TP2", "pips": pips_between(entry, tp2), "date": candle_date, "action": action})
                open_trade["tp2_hit"] = True
        else:  # SELL
            if not open_trade.get("closed") and high >= sl:
                messages.append(format_sl_hit(entry, sl))
                events.append({"label": "SL", "pips": -pips_between(entry, sl), "date": candle_date, "action": action})
                open_trade["closed"] = True
                continue
            if not open_trade.get("tp1_hit") and low <= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                events.append({"label": "TP1", "pips": pips_between(entry, tp1), "date": candle_date, "action": action})
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and low <= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                events.append({"label": "TP2", "pips": pips_between(entry, tp2), "date": candle_date, "action": action})
                open_trade["tp2_hit"] = True

    return messages, open_trade, events


def get_week_id(dt: datetime) -> str:
    """Identifiant unique de semaine ISO (année + numéro de semaine)."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def generate_summary_image(week_id: str, trades: list) -> str:
    """Génère l'image de bilan hebdomadaire avec Matplotlib et renvoie son chemin.
    Regroupe les événements réellement enregistrés par le bot, jour par jour
    (ACHAT OR / VENTE OR, résultat en pips, coche verte ou croix rouge), avec
    un sous-bilan par jour puis les totaux de la semaine en bas. Uniquement
    des chiffres réels — rien n'est saisi à la main."""
    # Compat avec d'anciennes entrées (label, pips) sans date/direction.
    normalized = []
    for t in trades:
        if isinstance(t, dict):
            normalized.append(t)
        else:
            label, pips = t[0], t[1]
            normalized.append({"label": label, "pips": pips, "date": "?", "action": "?"})

    total_pips = sum(t["pips"] for t in normalized)
    n_flip_win = sum(1 for t in normalized if t["label"] == "FLIP" and t["pips"] >= 0)
    n_wins = sum(1 for t in normalized if t["pips"] >= 0)
    n_total = len(normalized)
    win_rate = (n_wins / n_total * 100) if n_total else 0

    # Regroupement par jour, dans l'ordre d'apparition.
    days = []
    by_day = {}
    for t in normalized:
        d = t["date"]
        if d not in by_day:
            by_day[d] = []
            days.append(d)
        by_day[d].append(t)

    bg = "#0d0f16"
    box_bg = "#171a24"
    gold = "#c6a34e"
    green = "#2ecc71"
    red = "#e74c3c"
    white = "#e8ecf2"
    grey = "#8a93a3"

    # --- Hauteur calculée dynamiquement pour que rien ne se chevauche,
    # quel que soit le nombre de jours / trades de la semaine.
    LINE_H = 0.34
    DAY_BILAN_H = 0.42
    DAY_GAP = 0.22
    HEADER_H = 2.35
    FOOTER_H = 2.5
    body_h = sum(len(by_day[d]) * LINE_H + DAY_BILAN_H + DAY_GAP for d in days) if days else 0.7
    total_h = HEADER_H + body_h + FOOTER_H
    width = 8.0

    fig, ax = plt.subplots(figsize=(width, total_h), facecolor=bg)
    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, width)
    ax.set_ylim(0, total_h)

    def y_at(offset_from_top):
        return total_h - offset_from_top

    # --- En-tête ---
    cursor = 0.55
    ax.plot(
        [width / 2 - 0.13, width / 2 + 0.13, width / 2 - 0.13, width / 2],
        [y_at(cursor) + 0.16, y_at(cursor), y_at(cursor), y_at(cursor) + 0.16],
        color=gold, linewidth=1.6,
    )
    cursor += 0.55
    ax.text(width / 2, y_at(cursor), "XAU GUARDIAN", ha="center", fontsize=25, fontweight="bold", color=gold)
    cursor += 0.42
    ax.text(width / 2, y_at(cursor), "BILAN DE LA SEMAINE", ha="center", fontsize=15, fontweight="bold", color=white)
    cursor += 0.32
    ax.text(width / 2, y_at(cursor), week_id, ha="center", fontsize=10.5, color=grey)
    cursor += 0.4

    # --- Corps : un bloc par jour ---
    box_left, box_right = 0.35, width - 0.35
    if not days:
        ax.text(width / 2, y_at(cursor + 0.35), "Aucune position clôturée cette semaine.", ha="center", fontsize=12, color=grey)
        cursor += 0.7
    for d in days:
        day_trades = by_day[d]
        box_h = len(day_trades) * LINE_H + DAY_BILAN_H + 0.12
        ax.add_patch(plt.Rectangle((box_left, y_at(cursor + box_h)), box_right - box_left, box_h,
                                    facecolor=box_bg, edgecolor="none", zorder=1))
        cursor += 0.24
        for t in day_trades:
            action_label = "ACHAT OR" if t["action"] == "BUY" else ("VENTE OR" if t["action"] == "SELL" else "POSITION")
            win = t["pips"] >= 0
            sign = "+" if win else ""
            mark_color = green if win else red
            mark = "✓" if win else "✗"
            ax.text(box_left + 0.18, y_at(cursor), f"{d}  {action_label}", ha="left", va="center", fontsize=11.5, color=white, zorder=2)
            ax.text(box_right - 0.85, y_at(cursor), f"{sign}{t['pips']}PIPS", ha="right", va="center", fontsize=11.5, fontweight="bold", color=mark_color, zorder=2)
            ax.text(box_right - 0.20, y_at(cursor), mark, ha="center", va="center", fontsize=13, fontweight="bold", color=mark_color, zorder=2)
            cursor += LINE_H
        day_wins = sum(1 for t in day_trades if t["pips"] >= 0)
        cursor += DAY_BILAN_H * 0.75
        ax.text(width / 2, y_at(cursor), f"BILAN : {day_wins}/{len(day_trades)}", ha="center", fontsize=13.5, fontweight="bold", color=gold, zorder=2)
        cursor += DAY_BILAN_H * 0.25 + DAY_GAP

    # --- Totaux de la semaine ---
    pips_color = green if total_pips >= 0 else red
    sign = "+" if total_pips >= 0 else ""
    total_eur = total_pips * PIP_VALUE_EUR_PER_001_LOT
    if n_total:
        ax.plot([width * 0.2, width * 0.8], [y_at(cursor), y_at(cursor)], color=gold, linewidth=1)
        cursor += 0.55
        ax.text(width / 2, y_at(cursor), f"BILAN TRADES : {n_wins}/{n_total}", ha="center", fontsize=14, fontweight="bold", color=white)
        cursor += 0.45
        ax.text(width / 2, y_at(cursor), f"BILAN PIPS : {sign}{total_pips} pips (~{sign}{total_eur:.0f}€ en 0.01 lot)",
                ha="center", fontsize=13.5, fontweight="bold", color=pips_color)
        cursor += 0.45
        ax.text(width / 2, y_at(cursor), f"{win_rate:.0f}% DE RÉUSSITE", ha="center", fontsize=14, fontweight="bold", color=gold)
        cursor += 0.35
        ax.text(width / 2, y_at(cursor), "Conversion en euros estimée (spéc. contrat standard, hors spread/commissions)",
                ha="center", fontsize=8, color=grey)
        cursor += 0.35
    else:
        cursor += 0.3
    ax.text(width / 2, y_at(cursor), "Résultats réels du bot, calculés automatiquement — informatif uniquement,",
            ha="center", fontsize=8.3, color=grey)
    cursor += 0.24
    ax.text(width / 2, y_at(cursor), "pas un conseil financier personnalisé.", ha="center", fontsize=8.3, color=grey)

    fig.savefig(SUMMARY_IMAGE_PATH, facecolor=bg, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return SUMMARY_IMAGE_PATH


def check_and_send_weekly_summary(state: dict) -> bool:
    """Si on est en fin de semaine (clôture du marché) et que le bilan n'a pas
    encore été envoyé cette semaine, génère et envoie l'image. Renvoie True si envoyé."""
    now = datetime.now(timezone.utc)
    is_closing_time = now.weekday() == WEEKLY_SUMMARY_WEEKDAY and now.hour >= WEEKLY_SUMMARY_HOUR_UTC
    if not is_closing_time:
        return False

    week_id = get_week_id(now)
    if state.get("last_summary_week") == week_id:
        return False  # déjà envoyé cette semaine

    trades = state.get("weekly_trades", [])
    log(f"Clôture hebdomadaire détectée ({week_id}) — génération du bilan ({len(trades)} événements).")

    image_path = generate_summary_image(week_id, trades)
    total_pips = sum((t["pips"] if isinstance(t, dict) else t[1]) for t in trades)
    total_eur = total_pips * PIP_VALUE_EUR_PER_001_LOT
    sign = "+" if total_pips >= 0 else ""
    caption = (
        f"📊 <b>Bilan de la semaine {week_id}</b>\n"
        f"Résultat cumulé : {sign}{total_pips} pips (~{sign}{total_eur:.0f}€ en 0.01 lot)"
    )
    sent = send_photo(image_path, caption)
    log(f"Bilan hebdomadaire envoyé : {sent}")

    state["last_summary_week"] = week_id
    state["weekly_trades"] = []  # on repart à zéro pour la semaine suivante
    return True


def format_daily_briefing(current_price: float, short_sma: float, long_sma: float, rsi: float, atr: float) -> str:
    """Message purement descriptif — état du marché, pas une recommandation d'achat/vente."""
    trend = "haussière 🟢" if short_sma > long_sma else "baissière 🔴"
    if rsi >= RSI_OVERBOUGHT:
        rsi_zone_label = "surachat"
    elif rsi <= RSI_OVERSOLD:
        rsi_zone_label = "survente"
    else:
        rsi_zone_label = "neutre"
    date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")

    return (
        f"📅 <b>Point marché — {SYMBOL}</b>\n"
        f"{date_str}\n\n"
        f"💰 Prix actuel : {current_price:.2f}\n"
        f"📊 Tendance (SMA{SHORT_WINDOW}/{LONG_WINDOW}) : {trend}\n"
        f"📈 RSI{RSI_PERIOD} : {rsi:.1f} (zone {rsi_zone_label})\n"
        f"〰️ Volatilité (ATR) : {atr:.2f}\n\n"
        f"<i>Information descriptive, pas une recommandation. Une alerte sera envoyée dès qu'un signal se déclenche.</i>"
    )


def check_and_send_daily_briefing(state: dict, current_price: float, short_sma: float, long_sma: float, rsi: float, atr: float) -> bool:
    """Envoie le point marché quotidien une seule fois par jour ouvré, à l'heure configurée."""
    now = datetime.now(timezone.utc)
    is_weekday = now.weekday() <= 4  # lundi à vendredi
    is_briefing_time = now.hour >= DAILY_BRIEFING_HOUR_UTC
    if not (is_weekday and is_briefing_time):
        return False

    today_str = now.strftime("%Y-%m-%d")
    if state.get("last_daily_briefing_date") == today_str:
        return False  # déjà envoyé aujourd'hui

    message = format_daily_briefing(current_price, short_sma, long_sma, rsi, atr)
    sent = send_alert(message)
    log(f"Point marché quotidien envoyé : {sent}")
    state["last_daily_briefing_date"] = today_str
    return sent


def run_once():
    log("=== Démarrage de la vérification ===")

    if not BOT_TOKEN or not TWELVEDATA_API_KEY or not CHANNEL_ID:
        log("Erreur : BOT_TOKEN, CHANNEL_ID ou TWELVEDATA_API_KEY manquant (secrets non transmis).")
        sys.exit(1)

    log("Secrets bien reçus (BOT_TOKEN, CHANNEL_ID, TWELVEDATA_API_KEY présents).")

    state = load_state()
    last_sma_signal = state.get("last_sma_signal")
    last_rsi_zone = state.get("last_rsi_zone")
    last_candle_time = state.get("last_candle_time")
    open_trade = state.get("open_trade")
    weekly_trades = state.get("weekly_trades", [])
    cooldown_candles_remaining = state.get("cooldown_candles_remaining", 0)
    log(f"État précédent chargé : last_sma_signal={last_sma_signal}, last_rsi_zone={last_rsi_zone}, "
        f"position ouverte={'oui' if open_trade and not open_trade.get('closed') else 'non'}, "
        f"événements cette semaine={len(weekly_trades)}, cooldown restant={cooldown_candles_remaining}")

    # --- Vérification du bilan hebdomadaire (indépendant des données de marché) ---
    summary_sent = check_and_send_weekly_summary(state)
    if summary_sent:
        save_state(state)
        log("=== Fin de la vérification (bilan hebdomadaire envoyé) ===")
        return

    min_candles_required = max(LONG_WINDOW, RSI_PERIOD + 1, TREND_WINDOW, ATR_BASELINE_PERIOD + 1)
    candles = fetch_candles()
    if candles is None or len(candles) < min_candles_required:
        log("Pas assez de données pour calculer les indicateurs, on arrête ici.")
        consecutive_failures = state.get("consecutive_failures", 0) + 1
        admin_alerted = state.get("admin_alerted_for_streak", False)
        log(f"Échecs consécutifs : {consecutive_failures}")
        if consecutive_failures >= FAILURE_ALERT_THRESHOLD and not admin_alerted:
            alert_msg = (
                f"⚠️ <b>XAU Guardian — Problème détecté</b>\n\n"
                f"Le bot n'arrive plus à récupérer les prix depuis {consecutive_failures} exécutions consécutives.\n"
                f"Vérifie ton quota Twelve Data ou les logs GitHub Actions."
            )
            send_admin_alert(alert_msg)
            admin_alerted = True
            log("Alerte de panne envoyée à l'admin.")
        save_state({
            **state,
            "consecutive_failures": consecutive_failures,
            "admin_alerted_for_streak": admin_alerted,
        })
        return

    # Récupération réussie : on remet le compteur d'échecs à zéro
    if state.get("consecutive_failures", 0) > 0:
        log("Récupération des données réussie après une série d'échecs — compteur remis à zéro.")
        if state.get("admin_alerted_for_streak"):
            send_admin_alert("✅ XAU Guardian — Le bot fonctionne à nouveau normalement.")
    state["consecutive_failures"] = 0
    state["admin_alerted_for_streak"] = False

    current_candle_time = candles[-1]["datetime"]
    if last_candle_time is not None and current_candle_time == last_candle_time:
        log(f"Aucune nouvelle bougie depuis la dernière vérification (marché probablement fermé — {current_candle_time}). On arrête ici, pas de recalcul.")
        return

    # Une nouvelle bougie est arrivée : le cooldown après un SL avance d'un cran.
    if cooldown_candles_remaining > 0:
        cooldown_candles_remaining -= 1
        log(f"Cooldown après SL actif : encore {cooldown_candles_remaining} bougie(s) avant de reprendre de nouvelles entrées.")

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    # --- Vérification de la position ouverte (TP1/TP2/SL), sur high/low de chaque bougie ---
    trade_messages, open_trade, trade_events = check_open_trade(candles, open_trade)
    for msg in trade_messages:
        sent = send_alert(msg)
        log(f"Alerte position envoyée ({msg.splitlines()[0]}) : {sent}")
    for event in trade_events:
        weekly_trades.append(event)
        if event["label"] == "SL":
            cooldown_candles_remaining = SL_COOLDOWN_CANDLES
            log(f"SL touché — cooldown de {SL_COOLDOWN_CANDLES} bougies activé avant le prochain signal.")

    atr = average_true_range(candles, ATR_PERIOD)
    if atr is None:
        log("ATR non calculable, on arrête ici.")
        save_state({
            **state,
            "last_sma_signal": last_sma_signal,
            "last_rsi_zone": last_rsi_zone,
            "last_candle_time": current_candle_time,
            "open_trade": open_trade,
            "weekly_trades": weekly_trades,
            "cooldown_candles_remaining": cooldown_candles_remaining,
        })
        return

    # Indicateurs calculés une seule fois et réutilisés pour le point quotidien
    # ET pour les deux signaux ci-dessous (plus de double calcul divergent).
    short_sma = simple_moving_average(closes, SHORT_WINDOW)
    long_sma = simple_moving_average(closes, LONG_WINDOW)
    trend_sma = simple_moving_average(closes, TREND_WINDOW)  # tendance de fond, filtre de qualité
    rsi = relative_strength_index(closes, RSI_PERIOD)

    # --- Point marché quotidien (indépendant du filtre de volatilité) ---
    if short_sma is not None and long_sma is not None and rsi is not None:
        check_and_send_daily_briefing(state, current_price, short_sma, long_sma, rsi, atr)

    # Filtre de volatilité ADAPTATIF : on compare l'ATR courant à sa propre
    # moyenne récente (ATR_BASELINE_PERIOD bougies) plutôt qu'à un seuil fixe.
    # Un seuil fixe ne s'adapte pas si le marché devient globalement plus ou
    # moins volatil sur plusieurs mois — celui-ci s'ajuste tout seul.
    atr_baseline = average_true_range(candles, ATR_BASELINE_PERIOD)
    min_atr_required = max(ATR_MIN_THRESHOLD, ATR_RELATIVE_MIN_RATIO * atr_baseline) if atr_baseline else ATR_MIN_THRESHOLD
    if atr < min_atr_required:
        log(f"ATR trop faible par rapport à son niveau récent ({atr:.3f} < {min_atr_required:.3f}, "
            f"moyenne sur {ATR_BASELINE_PERIOD} bougies={atr_baseline}) — marché trop calme, signaux ignorés.")
        save_state({
            **state,
            "last_sma_signal": last_sma_signal,
            "last_rsi_zone": last_rsi_zone,
            "last_candle_time": current_candle_time,
            "open_trade": open_trade,
            "weekly_trades": weekly_trades,
            "cooldown_candles_remaining": cooldown_candles_remaining,
        })
        return

    alerts_sent = 0

    # --- Signal 1 : croisement SMA, filtré par la tendance de fond (SMA50),
    # l'ampleur du croisement et le cooldown après un SL ---
    if short_sma is not None and long_sma is not None:
        current_sma_signal = "BUY" if short_sma > long_sma else "SELL"
        log(f"SMA : signal actuel={current_sma_signal} (précédent={last_sma_signal}) — SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f}")
        if last_sma_signal is not None and current_sma_signal != last_sma_signal:
            # 1) Le croisement doit être net, pas un frôlement dans le bruit.
            crossover_margin = abs(short_sma - long_sma)
            margin_ok = crossover_margin >= MIN_CROSSOVER_ATR_RATIO * atr
            # 2) On ne prend le signal que s'il va dans le sens de la tendance
            #    de fond (SMA50) — on évite d'acheter en pleine tendance baissière.
            trend_ok = trend_sma is not None and (
                (current_sma_signal == "BUY" and current_price > trend_sma)
                or (current_sma_signal == "SELL" and current_price < trend_sma)
            )
            # 3) On évite d'entrer si le RSI est déjà à bout de course dans le
            #    sens du signal (acheter alors que le RSI est déjà en surachat, etc.).
            rsi_ok = rsi is None or not (
                (current_sma_signal == "BUY" and rsi >= RSI_OVERBOUGHT)
                or (current_sma_signal == "SELL" and rsi <= RSI_OVERSOLD)
            )
            # 4) Pas de nouvelle entrée juste après un SL (cooldown).
            cooldown_ok = cooldown_candles_remaining == 0
            # 5) On n'est pas déjà en position dans la même direction (évite
            #    une clôture + réouverture redondante si SMA et RSI sont d'accord).
            not_duplicate = not already_in_direction(open_trade, current_sma_signal)

            if margin_ok and trend_ok and rsi_ok and cooldown_ok and not_duplicate:
                # Si une position est encore ouverte (précédent signal SMA ou
                # RSI), on la clôture au prix courant avant d'en ouvrir une
                # nouvelle — sinon elle disparaîtrait silencieusement de
                # l'état et du bilan.
                close_message = close_previous_trade_if_open(open_trade, current_price, weekly_trades, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    log(f"Position précédente clôturée avant nouveau signal SMA : {sent_close}")

                tp1, tp2, stop_loss = compute_tp_sl(current_sma_signal, current_price, atr)
                note = (
                    f"Signal : croisement SMA{SHORT_WINDOW}/SMA{LONG_WINDOW} "
                    f"(SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f}), "
                    f"confirmé par la tendance de fond (SMA{TREND_WINDOW})"
                )
                message = format_alert(current_sma_signal, current_price, tp1, tp2, stop_loss, note)
                sent = send_alert(message)
                log(f"Alerte SMA envoyée : {sent}")
                alerts_sent += 1
                open_trade = {
                    "action": current_sma_signal,
                    "entry": current_price,
                    "tp1": tp1,
                    "tp2": tp2,
                    "sl": stop_loss,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "closed": False,
                    "entry_time": current_candle_time,
                }
            else:
                log(
                    f"Croisement SMA {current_sma_signal} détecté mais filtré "
                    f"(marge suffisante={margin_ok}, tendance de fond favorable={trend_ok}, "
                    f"RSI pas déjà épuisé={rsi_ok}, hors cooldown={cooldown_ok}, "
                    f"pas déjà en position={not_duplicate})."
                )
        last_sma_signal = current_sma_signal

    # --- Signal 2 : sortie de zone RSI, filtré par la tendance de fond (SMA50)
    # et le cooldown après un SL — on ne prend le retournement que s'il va
    # dans le sens du fond du marché, pas contre lui. ---
    if rsi is not None:
        current_zone = rsi_zone(rsi)
        log(f"RSI : {rsi:.1f} — zone actuelle={current_zone} (précédente={last_rsi_zone})")

        if last_rsi_zone == "oversold" and current_zone == "neutral":
            trend_ok = trend_sma is not None and current_price > trend_sma
            cooldown_ok = cooldown_candles_remaining == 0
            not_duplicate = not already_in_direction(open_trade, "BUY")
            if trend_ok and cooldown_ok and not_duplicate:
                close_message = close_previous_trade_if_open(open_trade, current_price, weekly_trades, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    log(f"Position précédente clôturée avant nouveau signal RSI (BUY) : {sent_close}")

                tp1, tp2, stop_loss = compute_tp_sl("BUY", current_price, atr)
                note = f"Signal : RSI sort de survente (RSI={rsi:.1f}), dans le sens de la tendance de fond (SMA{TREND_WINDOW})"
                message = format_alert("BUY", current_price, tp1, tp2, stop_loss, note)
                sent = send_alert(message)
                log(f"Alerte RSI (BUY) envoyée : {sent}")
                alerts_sent += 1
                open_trade = {
                    "action": "BUY",
                    "entry": current_price,
                    "tp1": tp1,
                    "tp2": tp2,
                    "sl": stop_loss,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "closed": False,
                    "entry_time": current_candle_time,
                }
            else:
                log(f"Sortie de survente détectée mais filtrée (tendance de fond favorable={trend_ok}, "
                    f"hors cooldown={cooldown_ok}, pas déjà en position={not_duplicate}).")
        elif last_rsi_zone == "overbought" and current_zone == "neutral":
            trend_ok = trend_sma is not None and current_price < trend_sma
            cooldown_ok = cooldown_candles_remaining == 0
            not_duplicate = not already_in_direction(open_trade, "SELL")
            if trend_ok and cooldown_ok and not_duplicate:
                close_message = close_previous_trade_if_open(open_trade, current_price, weekly_trades, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    log(f"Position précédente clôturée avant nouveau signal RSI (SELL) : {sent_close}")

                tp1, tp2, stop_loss = compute_tp_sl("SELL", current_price, atr)
                note = f"Signal : RSI sort de surachat (RSI={rsi:.1f}), dans le sens de la tendance de fond (SMA{TREND_WINDOW})"
                message = format_alert("SELL", current_price, tp1, tp2, stop_loss, note)
                sent = send_alert(message)
                log(f"Alerte RSI (SELL) envoyée : {sent}")
                alerts_sent += 1
                open_trade = {
                    "action": "SELL",
                    "entry": current_price,
                    "tp1": tp1,
                    "tp2": tp2,
                    "sl": stop_loss,
                    "tp1_hit": False,
                    "tp2_hit": False,
                    "closed": False,
                    "entry_time": current_candle_time,
                }
            else:
                log(f"Sortie de surachat détectée mais filtrée (tendance de fond favorable={trend_ok}, "
                    f"hors cooldown={cooldown_ok}, pas déjà en position={not_duplicate}).")
        last_rsi_zone = current_zone

    if alerts_sent == 0 and not trade_messages:
        log("Aucun changement détecté, pas d'alerte envoyée.")

    save_state({
        **state,
        "last_sma_signal": last_sma_signal,
        "last_rsi_zone": last_rsi_zone,
        "last_candle_time": current_candle_time,
        "open_trade": open_trade,
        "weekly_trades": weekly_trades,
        "cooldown_candles_remaining": cooldown_candles_remaining,
    })
    log("=== Fin de la vérification, état sauvegardé ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)