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
ATR_MIN_THRESHOLD = 0.5  # en dessous de ça (marché quasi plat), on ignore les signaux

# --- Configuration du suivi de position (pips) ---
PIP_SIZE = 0.1  # 1 pip = 0.1 sur XAU/USD

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
    outputsize = max(LONG_WINDOW, ATR_PERIOD, RSI_PERIOD) + 10
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


def format_tp_hit(level_name: str, entry: float, level_price: float) -> str:
    pips = pips_between(entry, level_price)
    return f"🎯 {level_name} TOUCHÉ 🔥\n{SYMBOL} +{pips} pips ✅"


def format_sl_hit(entry: float, sl_price: float) -> str:
    pips = pips_between(entry, sl_price)
    return f"🔒 SL TOUCHÉ ❌\n{SYMBOL} -{pips} pips"


def check_open_trade(candles: list, open_trade: dict):
    """
    Vérifie si TP1, TP2 ou le SL de la position ouverte ont été touchés,
    en examinant le plus haut/plus bas de CHAQUE bougie récupérée (pas
    seulement le dernier prix de clôture) — ça évite de rater un niveau
    touché brièvement entre deux vérifications du bot.
    Renvoie (messages_a_envoyer, open_trade_mis_a_jour, evenements_pips).
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

        if action == "BUY":
            # SL prioritaire si le SL et un TP sont touchés dans la même bougie (prudence)
            if not open_trade.get("closed") and low <= sl:
                messages.append(format_sl_hit(entry, sl))
                events.append(("SL", -pips_between(entry, sl)))
                open_trade["closed"] = True
                continue
            if not open_trade.get("tp1_hit") and high >= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                events.append(("TP1", pips_between(entry, tp1)))
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and high >= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                events.append(("TP2", pips_between(entry, tp2)))
                open_trade["tp2_hit"] = True
        else:  # SELL
            if not open_trade.get("closed") and high >= sl:
                messages.append(format_sl_hit(entry, sl))
                events.append(("SL", -pips_between(entry, sl)))
                open_trade["closed"] = True
                continue
            if not open_trade.get("tp1_hit") and low <= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                events.append(("TP1", pips_between(entry, tp1)))
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and low <= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                events.append(("TP2", pips_between(entry, tp2)))
                open_trade["tp2_hit"] = True

    return messages, open_trade, events


def get_week_id(dt: datetime) -> str:
    """Identifiant unique de semaine ISO (année + numéro de semaine)."""
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def generate_summary_image(week_id: str, trades: list) -> str:
    """Génère l'image de bilan hebdomadaire avec Matplotlib et renvoie son chemin."""
    total_pips = sum(p for _, p in trades)
    n_tp1 = sum(1 for label, _ in trades if label == "TP1")
    n_tp2 = sum(1 for label, _ in trades if label == "TP2")
    n_sl = sum(1 for label, _ in trades if label == "SL")
    n_wins = n_tp1 + n_tp2
    n_total = len(trades)
    win_rate = (n_wins / n_total * 100) if n_total else 0

    bg = "#0d0f16"
    gold = "#c6a34e"
    green = "#2ecc71"
    red = "#e74c3c"
    white = "#e8ecf2"
    grey = "#8a93a3"

    fig, ax = plt.subplots(figsize=(8, 8), facecolor=bg)
    ax.set_facecolor(bg)
    ax.axis("off")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    # Petit badge triangulaire doré au-dessus du titre
    ax.plot([0.485, 0.515, 0.455, 0.485], [0.985, 0.945, 0.945, 0.985], color=gold, linewidth=2, transform=ax.transAxes)
    ax.fill([0.485, 0.515, 0.455], [0.985, 0.945, 0.945], color=gold, alpha=0.25, transform=ax.transAxes)

    ax.text(0.5, 0.90, "XAU GUARDIAN", ha="center", fontsize=26, fontweight="bold", color=gold, transform=ax.transAxes)
    ax.text(0.5, 0.84, "Bilan hebdomadaire", ha="center", fontsize=16, color=white, transform=ax.transAxes)
    ax.text(0.5, 0.79, week_id, ha="center", fontsize=12, color=grey, transform=ax.transAxes)

    pips_color = green if total_pips >= 0 else red
    sign = "+" if total_pips >= 0 else ""
    ax.text(0.5, 0.62, f"{sign}{total_pips} pips", ha="center", fontsize=52, fontweight="bold", color=pips_color, transform=ax.transAxes)
    ax.text(0.5, 0.53, "si toutes les alertes avaient été suivies", ha="center", fontsize=11, color=grey, transform=ax.transAxes)

    stats_y = 0.40
    ax.text(0.5, stats_y, f"{n_total} signaux touchés cette semaine", ha="center", fontsize=13, color=white, transform=ax.transAxes)

    # Ligne de stats avec petits marqueurs colorés au lieu d'emojis
    ax.scatter([0.30], [stats_y - 0.06], color=green, s=60, transform=ax.transAxes, zorder=5)
    ax.text(0.33, stats_y - 0.062, f"TP1 : {n_tp1}", ha="left", fontsize=13, color=white, transform=ax.transAxes)
    ax.scatter([0.50], [stats_y - 0.06], color=green, s=60, transform=ax.transAxes, zorder=5)
    ax.text(0.53, stats_y - 0.062, f"TP2 : {n_tp2}", ha="left", fontsize=13, color=white, transform=ax.transAxes)
    ax.scatter([0.68], [stats_y - 0.06], color=red, s=60, transform=ax.transAxes, zorder=5)
    ax.text(0.71, stats_y - 0.062, f"SL : {n_sl}", ha="left", fontsize=13, color=white, transform=ax.transAxes)

    ax.text(0.5, stats_y - 0.12, f"Taux de réussite : {win_rate:.0f}%", ha="center", fontsize=13, color=white, transform=ax.transAxes)

    ax.plot([0.15, 0.85], [0.16, 0.16], color=gold, linewidth=1, transform=ax.transAxes)
    ax.text(0.5, 0.09, "Signaux automatisés, informatifs uniquement.", ha="center", fontsize=9, color=grey, transform=ax.transAxes)
    ax.text(0.5, 0.05, "Pas un conseil financier personnalisé.", ha="center", fontsize=9, color=grey, transform=ax.transAxes)

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
    total_pips = sum(p for _, p in trades)
    sign = "+" if total_pips >= 0 else ""
    caption = f"📊 <b>Bilan de la semaine {week_id}</b>\nRésultat cumulé : {sign}{total_pips} pips"
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
    log(f"État précédent chargé : last_sma_signal={last_sma_signal}, last_rsi_zone={last_rsi_zone}, "
        f"position ouverte={'oui' if open_trade and not open_trade.get('closed') else 'non'}, "
        f"événements cette semaine={len(weekly_trades)}")

    # --- Vérification du bilan hebdomadaire (indépendant des données de marché) ---
    summary_sent = check_and_send_weekly_summary(state)
    if summary_sent:
        save_state(state)
        log("=== Fin de la vérification (bilan hebdomadaire envoyé) ===")
        return

    candles = fetch_candles()
    if candles is None or len(candles) < max(LONG_WINDOW, RSI_PERIOD + 1):
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

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    # --- Vérification de la position ouverte (TP1/TP2/SL), sur high/low de chaque bougie ---
    trade_messages, open_trade, trade_events = check_open_trade(candles, open_trade)
    for msg in trade_messages:
        sent = send_alert(msg)
        log(f"Alerte position envoyée ({msg.splitlines()[0]}) : {sent}")
    for label, pips in trade_events:
        weekly_trades.append((label, pips))

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
        })
        return

    # --- Point marché quotidien (indépendant du filtre de volatilité) ---
    briefing_short_sma = simple_moving_average(closes, SHORT_WINDOW)
    briefing_long_sma = simple_moving_average(closes, LONG_WINDOW)
    briefing_rsi = relative_strength_index(closes, RSI_PERIOD)
    if briefing_short_sma is not None and briefing_long_sma is not None and briefing_rsi is not None:
        check_and_send_daily_briefing(state, current_price, briefing_short_sma, briefing_long_sma, briefing_rsi, atr)

    if atr < ATR_MIN_THRESHOLD:
        log(f"ATR trop faible ({atr:.3f} < {ATR_MIN_THRESHOLD}) — marché quasi plat, signaux ignorés pour éviter le bruit.")
        save_state({
            **state,
            "last_sma_signal": last_sma_signal,
            "last_rsi_zone": last_rsi_zone,
            "last_candle_time": current_candle_time,
            "open_trade": open_trade,
            "weekly_trades": weekly_trades,
        })
        return

    alerts_sent = 0

    # --- Signal 1 : croisement SMA ---
    short_sma = simple_moving_average(closes, SHORT_WINDOW)
    long_sma = simple_moving_average(closes, LONG_WINDOW)
    if short_sma is not None and long_sma is not None:
        current_sma_signal = "BUY" if short_sma > long_sma else "SELL"
        log(f"SMA : signal actuel={current_sma_signal} (précédent={last_sma_signal}) — SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f}")
        if last_sma_signal is not None and current_sma_signal != last_sma_signal:
            tp1, tp2, stop_loss = compute_tp_sl(current_sma_signal, current_price, atr)
            note = f"Signal : croisement SMA{SHORT_WINDOW}/SMA{LONG_WINDOW} (SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f})"
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
        last_sma_signal = current_sma_signal

    # --- Signal 2 : sortie de zone RSI ---
    rsi = relative_strength_index(closes, RSI_PERIOD)
    if rsi is not None:
        current_zone = rsi_zone(rsi)
        log(f"RSI : {rsi:.1f} — zone actuelle={current_zone} (précédente={last_rsi_zone})")
        if last_rsi_zone == "oversold" and current_zone == "neutral":
            tp1, tp2, stop_loss = compute_tp_sl("BUY", current_price, atr)
            note = f"Signal : RSI sort de survente (RSI={rsi:.1f})"
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
        elif last_rsi_zone == "overbought" and current_zone == "neutral":
            tp1, tp2, stop_loss = compute_tp_sl("SELL", current_price, atr)
            note = f"Signal : RSI sort de surachat (RSI={rsi:.1f})"
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
    })
    log("=== Fin de la vérification, état sauvegardé ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)