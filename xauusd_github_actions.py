"""
Script d'alertes de trading XAUUSD vers Telegram — version GitHub Actions (v4).
Deux sources de signaux : croisement SMA5/13 + sortie de zone RSI14.
Sortie en ÉCHELLE DE PALIERS (P1 à P6, espacés proportionnellement à l'ATR)
au lieu de TP1/TP2/TP3 — variante validée par backtest (90.9% de réussite,
+12556 pips sur 90 jours) et adoptée en remplacement de l'ancienne sortie.

Suivi des positions (paliers touchés, SL) + bilan mensuel en image envoyé
automatiquement le 1er de chaque mois (~21h UTC).

Différences avec la version "PC" :
- S'exécute UNE SEULE FOIS par lancement (GitHub Actions le relance périodiquement).
- L'état complet (SMA, RSI, position ouverte, historique de la semaine) est
  sauvegardé dans state.json, commité dans le dépôt entre deux exécutions.
- BOT_TOKEN, TWELVEDATA_API_KEY et CHANNEL_ID sont lus depuis les variables
  d'environnement (GitHub Secrets), jamais écrits en clair dans ce fichier.

Ajout : chaque signal envoyé sur Telegram est aussi enregistré dans Firestore
(collection "signals"), pour que le site XAU Guardian puisse les afficher
sur le tableau de bord des membres. Si FIREBASE_SERVICE_ACCOUNT n'est pas
configuré, cette partie est simplement ignorée (le bot continue de fonctionner
normalement, seul l'affichage sur le site sera vide).
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
import mplfinance as mpf
import pandas as pd

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
    FIREBASE_AVAILABLE = True
except ImportError:
    FIREBASE_AVAILABLE = False


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

# --- Configuration Firestore (site web) ---
FIREBASE_SERVICE_ACCOUNT = os.environ.get("FIREBASE_SERVICE_ACCOUNT")

# --- Configuration SMA ---
SHORT_WINDOW = 5
LONG_WINDOW = 13

# --- Configuration RSI ---
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
RSI_OVERSOLD = 30

# --- Configuration ATR ---
ATR_PERIOD = 14
ATR_MIN_THRESHOLD = 0.5

# --- Configuration de la sortie en ÉCHELLE DE PALIERS (remplace TP1/TP2/SL) ---
# Réglages affinés par backtest comparatif (backtest_ladder_optimize.py) sur
# 90 jours : SL élargi à 6xATR (au lieu de 4x) réduit le taux de perte de
# 6.3% à 3.2% tout en conservant un total de pips légèrement supérieur
# (+12594 vs +12556) — meilleur compromis trouvé parmi les variantes testées.
LADDER_LEVELS = 6
LADDER_STEP_ATR_RATIO = 0.4   # écart entre deux paliers consécutifs
LADDER_SL_ATR_RATIO = 6.0    # SL légèrement élargi pour réduire le taux de stop loss

# --- Configuration du filtre de qualité des signaux ---
TREND_WINDOW = 50
TREND_SLOPE_LOOKBACK = 10
ATR_BASELINE_PERIOD = 50
ATR_RELATIVE_MIN_RATIO = 0.8
MIN_CROSSOVER_ATR_RATIO = 0.35
RSI_SIGNAL_BUFFER = 5
SL_COOLDOWN_CANDLES = 5
DAILY_SIGNAL_LIMIT = 3

# --- Configuration du suivi de position (pips) ---
PIP_SIZE = 0.1

# --- Configuration du graphique d'alerte (chandelles + niveaux) ---
CHART_CANDLE_COUNT = 40
ALERT_CHART_PATH = "alert_chart.png"

# --- Conversion pips -> euros pour le bilan (estimation) ---
PIP_VALUE_EUR_PER_001_LOT = 0.10

# --- Configuration du bilan hebdomadaire ---
WEEKLY_SUMMARY_WEEKDAY = 4
WEEKLY_SUMMARY_HOUR_UTC = 21

# --- Configuration du bilan mensuel (complète le bilan hebdo avec une vue
# plus large : tendance sur le mois, meilleur/pire jour, progression du
# taux de réussite semaine par semaine) ---
MONTHLY_SUMMARY_DAY = 1              # envoyé le 1er de chaque mois...
MONTHLY_SUMMARY_HOUR_UTC = 21        # ...à partir de 21h UTC
MONTHLY_SUMMARY_IMAGE_PATH = "monthly_summary.png"
MONTH_NAMES_FR = [
    "", "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

# --- Configuration du point marché quotidien ---
DAILY_BRIEFING_HOUR_UTC = 7

STATE_FILE = "state.json"
SUMMARY_IMAGE_PATH = "weekly_summary.png"

_firestore_db = None
_firestore_init_attempted = False


def get_firestore_db():
    """Initialise Firebase Admin une seule fois et renvoie le client Firestore.
    Renvoie None si non configuré ou en cas d'échec (le bot continue sans planter)."""
    global _firestore_db, _firestore_init_attempted
    if _firestore_init_attempted:
        return _firestore_db
    _firestore_init_attempted = True

    if not FIREBASE_AVAILABLE:
        log("firebase-admin non installé, synchronisation Firestore désactivée.")
        return None
    if not FIREBASE_SERVICE_ACCOUNT:
        log("FIREBASE_SERVICE_ACCOUNT non configuré, synchronisation Firestore désactivée.")
        return None

    try:
        cred_dict = json.loads(FIREBASE_SERVICE_ACCOUNT)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred)
        _firestore_db = firestore.client()
        log("Connexion Firestore initialisée avec succès.")
        return _firestore_db
    except Exception as e:
        log(f"Erreur d'initialisation Firestore (le bot continue normalement) : {e}")
        return None


def save_signal_to_firestore(action, entry, levels, sl, note, candle_time):
    """Crée un nouveau document dans la collection 'signals'. Renvoie l'ID du
    document créé (à conserver pour les mises à jour de palier/SL), ou None si échec."""
    db = get_firestore_db()
    if db is None:
        return None
    try:
        doc_ref = db.collection("signals").document()
        doc_ref.set({
            "symbol": SYMBOL,
            "action": action,
            "entry": entry,
            "levels": levels,
            "levels_hit": [False] * len(levels),
            "sl": sl,
            "note": note,
            "candle_time": candle_time,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "status": "open",
            "closed": False,
            "result_pips": None,
        })
        log(f"Signal enregistré dans Firestore (id={doc_ref.id}).")
        return doc_ref.id
    except Exception as e:
        log(f"Erreur d'écriture Firestore (signal ignoré côté site, le bot continue) : {e}")
        return None


def update_signal_in_firestore(doc_id, updates: dict):
    """Met à jour un document existant de la collection 'signals'."""
    if not doc_id:
        return
    db = get_firestore_db()
    if db is None:
        return
    try:
        db.collection("signals").document(doc_id).update(updates)
        log(f"Signal Firestore mis à jour (id={doc_id}) : {updates}")
    except Exception as e:
        log(f"Erreur de mise à jour Firestore (le bot continue) : {e}")


def load_state():
    default = {
        "last_sma_signal": None,
        "last_rsi_zone": None,
        "last_candle_time": None,
        "open_trade": None,
        "weekly_trades": [],
        "last_summary_week": None,
        "last_summary_month": None,
        "consecutive_failures": 0,
        "admin_alerted_for_streak": False,
        "last_daily_briefing_date": None,
        "cooldown_candles_remaining": 0,
        "total_pips_all_time": 0,
        "alerts_sent_today": 0,
        "alerts_sent_date": None,
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
    if not ADMIN_CHAT_ID:
        log("ADMIN_CHAT_ID non configuré, alerte de panne non envoyée (mais consignée dans les logs).")
        return False
    payload = {"chat_id": ADMIN_CHAT_ID, "text": message, "parse_mode": "HTML"}
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


def format_alert(action: str, price: float, levels: list, stop_loss: float, note: str = "") -> str:
    emoji = "🟢" if action == "BUY" else "🔴"
    action_label = "J'ACHÈTE" if action == "BUY" else "JE VENDS"
    levels_lines = "\n".join(f"🎯 P{i+1} : {lvl:.0f}" for i, lvl in enumerate(levels))
    msg = (
        f"{emoji} {action_label} {SYMBOL} à {price:.0f}\n\n"
        f"{levels_lines}\n\n"
        f"🔒 SL : {stop_loss:.0f}"
    )
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
                "open": float(c["open"]),
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


def compute_ladder(action: str, entry_price: float, atr: float):
    step = LADDER_STEP_ATR_RATIO * atr
    sl_distance = LADDER_SL_ATR_RATIO * atr
    if action == "BUY":
        levels = [entry_price + step * (i + 1) for i in range(LADDER_LEVELS)]
        stop_loss = entry_price - sl_distance
    else:
        levels = [entry_price - step * (i + 1) for i in range(LADDER_LEVELS)]
        stop_loss = entry_price + sl_distance
    return levels, stop_loss


def pips_between(price_a: float, price_b: float) -> int:
    return round(abs(price_a - price_b) / PIP_SIZE)


def generate_alert_chart(candles: list, action: str, entry_price: float, levels: list, stop_loss: float) -> str:
    window = candles[-CHART_CANDLE_COUNT:] if len(candles) >= CHART_CANDLE_COUNT else candles

    df = pd.DataFrame(
        {
            "Open": [c["open"] for c in window],
            "High": [c["high"] for c in window],
            "Low": [c["low"] for c in window],
            "Close": [c["close"] for c in window],
        },
        index=pd.to_datetime([c["datetime"] for c in window]),
    )

    bg = "#0d0f16"
    green = "#26a69a"
    red = "#ef5350"
    white = "#e8ecf2"
    grey = "#8a93a3"
    level_green = "#2ecc71"

    mc = mpf.make_marketcolors(up=green, down=red, edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(
        base_mpf_style="nightclouds", marketcolors=mc,
        facecolor=bg, figcolor=bg, gridcolor="#20263a", gridstyle="--", y_on_right=True,
        rc={"font.size": 9, "text.color": white, "axes.labelcolor": white,
            "xtick.color": grey, "ytick.color": grey},
    )

    price_min = min(c["low"] for c in window)
    price_max = max(c["high"] for c in window)
    margin = (price_max - price_min) * 0.25 or 1.0
    view_min = price_min - margin
    view_max = price_max + margin

    visible_levels = [(i, lvl) for i, lvl in enumerate(levels) if view_min <= lvl <= view_max]
    sl_visible = view_min <= stop_loss <= view_max

    hlines_vals = [entry_price] + [lvl for _, lvl in visible_levels]
    hlines_colors = [white] + [level_green] * len(visible_levels)
    hlines_styles = ["-"] + ["--"] * len(visible_levels)
    hlines_widths = [1.3] + [1.0] * len(visible_levels)
    if sl_visible:
        hlines_vals.append(stop_loss)
        hlines_colors.append(red)
        hlines_styles.append("--")
        hlines_widths.append(1.3)

    fig, axlist = mpf.plot(
        df, type="candle", style=style, volume=False,
        hlines=dict(hlines=hlines_vals, colors=hlines_colors, linestyle=hlines_styles, linewidths=hlines_widths),
        returnfig=True, figsize=(9, 5.2), tight_layout=True, title="",
    )

    ax = axlist[0]
    ax.set_ylim(view_min, view_max)
    action_label = "ACHAT" if action == "BUY" else "VENTE"
    ax.set_title(f"{action_label} {SYMBOL} — vue récente", color=white, fontsize=13.5,
                 fontweight="bold", loc="left", pad=12)

    label_x = ax.get_xlim()[1] + 0.6
    ax.text(label_x, entry_price, f"ENTRÉE {entry_price:.0f}", color=white, fontsize=9, va="center", fontweight="bold")
    for i, lvl in visible_levels:
        ax.text(label_x, lvl, f"P{i + 1}  {lvl:.0f}", color=level_green, fontsize=8.5, va="center", fontweight="bold")
    if sl_visible:
        ax.text(label_x, stop_loss, f"SL  {stop_loss:.0f}", color=red, fontsize=9, va="center", fontweight="bold")

    offscreen_notes = []
    if not sl_visible:
        offscreen_notes.append((f"SL {stop_loss:.0f} (encore {pips_between(entry_price, stop_loss)} pips plus loin)", red))
    hidden_levels = [lvl for i, lvl in enumerate(levels) if lvl > view_max] if action == "BUY" else [lvl for i, lvl in enumerate(levels) if lvl < view_min]
    if hidden_levels:
        first_hidden = len(visible_levels) + 1
        offscreen_notes.append((f"P{first_hidden} à P{LADDER_LEVELS} plus loin (jusqu'à {hidden_levels[-1]:.0f})", level_green))

    y0 = 0.04
    for note, color in offscreen_notes:
        ax.annotate(note, xy=(0.01, y0), xycoords="axes fraction", color=color, fontsize=8.3, fontweight="bold")
        y0 -= 0.05

    fig.savefig(ALERT_CHART_PATH, facecolor=bg, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return ALERT_CHART_PATH


def signed_pips(action: str, entry: float, exit_price: float) -> int:
    diff = (exit_price - entry) if action == "BUY" else (entry - exit_price)
    return round(diff / PIP_SIZE)


def format_level_hit(level_name: str, entry: float, level_price: float) -> str:
    pips = pips_between(entry, level_price)
    return f"🎯 {level_name} TOUCHÉ 🔥\n{SYMBOL} +{pips} pips ✅"


def format_sl_hit(entry: float, sl_price: float) -> str:
    pips = pips_between(entry, sl_price)
    return f"🔒 SL TOUCHÉ ❌\n{SYMBOL} -{pips} pips"


def format_manual_close(action: str, entry: float, exit_price: float) -> str:
    pips = signed_pips(action, entry, exit_price)
    sign = "+" if pips >= 0 else ""
    emoji = "✅" if pips >= 0 else "❌"
    return f"🔄 POSITION CLÔTURÉE (nouveau signal) {emoji}\n{SYMBOL} {sign}{pips} pips"


def already_in_direction(open_trade, action: str) -> bool:
    return open_trade is not None and not open_trade.get("closed") and open_trade.get("action") == action


def event_date(candle_datetime: str) -> str:
    date_part = candle_datetime.split(" ")[0]
    year, month, day = date_part.split("-")
    return f"{day}/{month}"


def close_previous_trade_if_open(open_trade, current_price: float, current_candle_time: str):
    if open_trade is None or open_trade.get("closed"):
        return None, None
    message = format_manual_close(open_trade["action"], open_trade["entry"], current_price)
    pips = signed_pips(open_trade["action"], open_trade["entry"], current_price)
    event = {
        "label": "FLIP",
        "pips": pips,
        "date": event_date(current_candle_time),
        "action": open_trade["action"],
    }
    open_trade["closed"] = True
    update_signal_in_firestore(open_trade.get("firestore_id"), {
        "closed": True,
        "status": "closed_flip",
        "result_pips": pips,
    })
    return message, event


def check_open_trade(candles: list, open_trade: dict):
    if open_trade is None or open_trade.get("closed"):
        return [], open_trade, []

    messages = []
    events = []
    action = open_trade["action"]
    entry = open_trade["entry"]
    entry_time = open_trade.get("entry_time", "")
    levels = open_trade["levels"]
    levels_hit = open_trade["levels_hit"]
    sl = open_trade["sl"]
    firestore_id = open_trade.get("firestore_id")

    relevant_candles = [c for c in candles if c["datetime"] > entry_time]

    for candle in relevant_candles:
        if open_trade.get("closed"):
            break
        high, low = candle["high"], candle["low"]
        candle_date = event_date(candle["datetime"])

        hit_sl = (low <= sl) if action == "BUY" else (high >= sl)
        if hit_sl:
            messages.append(format_sl_hit(entry, sl))
            pips = -pips_between(entry, sl)
            events.append({"label": "SL", "pips": pips, "date": candle_date, "action": action})
            open_trade["closed"] = True
            update_signal_in_firestore(firestore_id, {"closed": True, "status": "sl_hit", "result_pips": pips})
            continue

        for idx, level in enumerate(levels):
            if levels_hit[idx]:
                continue
            reached = (high >= level) if action == "BUY" else (low <= level)
            if reached:
                level_name = f"P{idx + 1}"
                messages.append(format_level_hit(level_name, entry, level))
                events.append({"label": level_name, "pips": pips_between(entry, level), "date": candle_date, "action": action})
                levels_hit[idx] = True
                update_signal_in_firestore(firestore_id, {"levels_hit": levels_hit, "status": f"{level_name.lower()}_hit"})

        if all(levels_hit):
            open_trade["closed"] = True
            update_signal_in_firestore(firestore_id, {"closed": True, "status": "all_levels_hit"})

    return messages, open_trade, events


def get_week_id(dt: datetime) -> str:
    iso = dt.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def generate_summary_image(week_id: str, trades: list) -> str:
    normalized = []
    for t in trades:
        if isinstance(t, dict):
            normalized.append(t)
        else:
            label, pips = t[0], t[1]
            normalized.append({"label": label, "pips": pips, "date": "?", "action": "?"})

    total_pips = sum(t["pips"] for t in normalized)
    n_wins = sum(1 for t in normalized if t["pips"] >= 0)
    n_total = len(normalized)
    win_rate = (n_wins / n_total * 100) if n_total else 0

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


def classify_signal(doc: dict):
    levels = doc.get("levels") or []
    levels_hit = doc.get("levels_hit") or []
    entry = doc.get("entry")
    status = doc.get("status") or ""
    closed = bool(doc.get("closed"))

    pips = 0
    any_level_hit = False
    for idx, hit in enumerate(levels_hit):
        if hit and idx < len(levels) and entry is not None:
            pips += pips_between(entry, levels[idx])
            any_level_hit = True

    hit_sl = "sl_hit" in status
    result_pips = doc.get("result_pips")
    if hit_sl and isinstance(result_pips, (int, float)):
        pips += result_pips
    elif status == "closed_flip" and isinstance(result_pips, (int, float)):
        pips += result_pips

    outcome = "open"
    if closed:
        if hit_sl and not any_level_hit:
            outcome = "loss"
        elif not hit_sl:
            outcome = "win"
        else:
            outcome = "mixed"

    return pips, outcome, closed


def generate_monthly_summary_image(month_id: str, signals: list) -> str:
    year, month = month_id.split("-")
    month_label = f"{MONTH_NAMES_FR[int(month)]} {year}"

    closed = []
    for doc in signals:
        pips, outcome, is_closed = classify_signal(doc)
        if is_closed:
            day = (doc.get("candle_time") or "").split(" ")[0]
            closed.append({"pips": pips, "outcome": outcome, "day": day})

    n_total = len(closed)
    n_wins = sum(1 for c in closed if c["outcome"] == "win")
    total_pips = sum(c["pips"] for c in closed)
    win_rate = (n_wins / n_total * 100) if n_total else 0

    by_day = {}
    for c in closed:
        by_day.setdefault(c["day"], 0)
        by_day[c["day"]] += c["pips"]
    best_day = max(by_day.items(), key=lambda kv: kv[1]) if by_day else None
    worst_day = min(by_day.items(), key=lambda kv: kv[1]) if by_day else None

    buckets = {}
    for c in closed:
        try:
            day_num = int(c["day"].split("-")[2])
        except (IndexError, ValueError):
            continue
        bucket = (day_num - 1) // 7 + 1
        b = buckets.setdefault(bucket, {"pips": 0, "wins": 0, "total": 0})
        b["pips"] += c["pips"]
        b["total"] += 1
        if c["outcome"] == "win":
            b["wins"] += 1

    bg = "#0d0f16"
    panel = "#171a24"
    gold = "#c6a34e"
    green = "#2ecc71"
    red = "#e74c3c"
    white = "#e8ecf2"
    grey = "#8a93a3"

    fig = plt.figure(figsize=(8, 7.2), facecolor=bg)
    gs = fig.add_gridspec(2, 1, height_ratios=[2.5, 1.3], hspace=0.35)

    ax_top = fig.add_subplot(gs[0])
    ax_top.axis("off")
    ax_top.set_xlim(0, 8)
    ax_top.set_ylim(0, 5)

    ax_top.text(4, 4.65, "XAU GUARDIAN", ha="center", fontsize=20, fontweight="bold", color=gold)
    ax_top.text(4, 4.2, f"BILAN MENSUEL — {month_label.upper()}", ha="center", fontsize=13.5, fontweight="bold", color=white)

    pips_color = green if total_pips >= 0 else red
    sign = "+" if total_pips >= 0 else ""

    ax_top.add_patch(plt.Rectangle((0.3, 2.6), 7.4, 1.35, facecolor=panel, edgecolor="none"))
    ax_top.text(1.9, 3.55, "Positions", ha="center", fontsize=9.5, color=grey)
    ax_top.text(1.9, 3.05, f"{n_total}", ha="center", fontsize=19, fontweight="bold", color=white)
    ax_top.text(4, 3.55, "Taux de réussite", ha="center", fontsize=9.5, color=grey)
    ax_top.text(4, 3.05, f"{win_rate:.0f}%" if n_total else "—", ha="center", fontsize=19, fontweight="bold", color=gold)
    ax_top.text(6.1, 3.55, "Pips cumulés", ha="center", fontsize=9.5, color=grey)
    ax_top.text(6.1, 3.05, f"{sign}{total_pips}", ha="center", fontsize=19, fontweight="bold", color=pips_color)

    if best_day:
        ax_top.text(1.9, 2.05, "Meilleur jour", ha="center", fontsize=9.5, color=grey)
        ax_top.text(1.9, 1.6, f"{best_day[0]}  ({'+' if best_day[1] >= 0 else ''}{best_day[1]} pips)", ha="center", fontsize=11.5, fontweight="bold", color=green)
    if worst_day:
        ax_top.text(6.1, 2.05, "Jour le plus difficile", ha="center", fontsize=9.5, color=grey)
        ax_top.text(6.1, 1.6, f"{worst_day[0]}  ({'+' if worst_day[1] >= 0 else ''}{worst_day[1]} pips)", ha="center", fontsize=11.5, fontweight="bold", color=red)

    if not closed:
        ax_top.text(4, 1.0, "Aucune position clôturée ce mois-ci.", ha="center", fontsize=11, color=grey)

    ax_top.text(4, 0.35, "Résultats réels du bot, calculés automatiquement — informatif uniquement, pas un conseil financier.",
                ha="center", fontsize=7.6, color=grey)

    ax_bottom = fig.add_subplot(gs[1])
    ax_bottom.set_facecolor(bg)
    if buckets:
        weeks = sorted(buckets.keys())
        pips_vals = [buckets[w]["pips"] for w in weeks]
        colors = [green if v >= 0 else red for v in pips_vals]
        ax_bottom.bar([f"Sem. {w}" for w in weeks], pips_vals, color=colors)
        for i, w in enumerate(weeks):
            b = buckets[w]
            wr = (b["wins"] / b["total"] * 100) if b["total"] else 0
            ax_bottom.text(i, pips_vals[i], f"{wr:.0f}%", ha="center",
                            va="bottom" if pips_vals[i] >= 0 else "top", fontsize=8.5, color=white)
        ax_bottom.axhline(0, color="#444", linewidth=0.8)
        ax_bottom.set_title("Tendance semaine par semaine (pips et % de réussite)", color=white, fontsize=10.5)
    else:
        ax_bottom.axis("off")
    ax_bottom.tick_params(colors=grey, labelsize=8.5)
    for spine in ax_bottom.spines.values():
        spine.set_color("#333")

    fig.savefig(MONTHLY_SUMMARY_IMAGE_PATH, facecolor=bg, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return MONTHLY_SUMMARY_IMAGE_PATH, n_total, n_wins, win_rate, total_pips


def check_and_send_monthly_summary(state: dict) -> bool:
    now = datetime.now(timezone.utc)
    is_monthly_time = now.day == MONTHLY_SUMMARY_DAY and now.hour >= MONTHLY_SUMMARY_HOUR_UTC
    if not is_monthly_time:
        return False

    if now.month == 1:
        target_year, target_month = now.year - 1, 12
    else:
        target_year, target_month = now.year, now.month - 1
    month_id = f"{target_year}-{target_month:02d}"

    if state.get("last_summary_month") == month_id:
        return False

    db = get_firestore_db()
    if db is None:
        log("Firestore non configuré, bilan mensuel ignoré pour cette fois.")
        return False

    start_dt = datetime(target_year, target_month, 1, tzinfo=timezone.utc)
    if target_month == 12:
        end_dt = datetime(target_year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_dt = datetime(target_year, target_month + 1, 1, tzinfo=timezone.utc)

    try:
        query = (
            db.collection("signals")
            .where("created_at", ">=", start_dt.isoformat())
            .where("created_at", "<", end_dt.isoformat())
        )
        signals = [doc.to_dict() for doc in query.stream()]
    except Exception as e:
        log(f"Erreur de lecture Firestore pour le bilan mensuel : {e}")
        return False

    log(f"Génération du bilan mensuel pour {month_id} ({len(signals)} signal(aux) trouvé(s)).")
    image_path, n_total, n_wins, win_rate, total_pips = generate_monthly_summary_image(month_id, signals)

    month_label = f"{MONTH_NAMES_FR[target_month]} {target_year}"
    sign = "+" if total_pips >= 0 else ""
    caption = (
        f"📊 <b>Bilan du mois — {month_label}</b>\n"
        f"{n_wins}/{n_total} positions gagnantes ({win_rate:.0f}%)\n"
        f"Résultat cumulé : {sign}{total_pips} pips"
    )
    sent = send_photo(image_path, caption)
    log(f"Bilan mensuel envoyé : {sent}")

    if sent:
        state["last_summary_month"] = month_id
    return sent


def format_daily_briefing(current_price: float, short_sma: float, long_sma: float, rsi: float, atr: float) -> str:
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
    now = datetime.now(timezone.utc)
    is_weekday = now.weekday() <= 4
    is_briefing_time = now.hour >= DAILY_BRIEFING_HOUR_UTC
    if not (is_weekday and is_briefing_time):
        return False

    today_str = now.strftime("%Y-%m-%d")
    if state.get("last_daily_briefing_date") == today_str:
        return False

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

    get_firestore_db()

    state = load_state()
    last_sma_signal = state.get("last_sma_signal")
    last_rsi_zone = state.get("last_rsi_zone")
    last_candle_time = state.get("last_candle_time")
    open_trade = state.get("open_trade")
    weekly_trades = state.get("weekly_trades", [])
    cooldown_candles_remaining = state.get("cooldown_candles_remaining", 0)
    total_pips_all_time = state.get("total_pips_all_time", 0)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if state.get("alerts_sent_date") != today_str:
        alerts_sent_today = 0
    else:
        alerts_sent_today = state.get("alerts_sent_today", 0)

    log(f"État précédent chargé : last_sma_signal={last_sma_signal}, last_rsi_zone={last_rsi_zone}, "
        f"position ouverte={'oui' if open_trade and not open_trade.get('closed') else 'non'}, "
        f"événements cette semaine={len(weekly_trades)}, cooldown restant={cooldown_candles_remaining}, "
        f"total all-time={total_pips_all_time}, alertes aujourd'hui={alerts_sent_today}/{DAILY_SIGNAL_LIMIT}")

    monthly_sent = check_and_send_monthly_summary(state)
    if monthly_sent:
        save_state(state)
        log("=== Fin de la vérification (bilan mensuel envoyé) ===")
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

    if cooldown_candles_remaining > 0:
        cooldown_candles_remaining -= 1
        log(f"Cooldown après SL actif : encore {cooldown_candles_remaining} bougie(s) avant de reprendre de nouvelles entrées.")

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    trade_messages, open_trade, trade_events = check_open_trade(candles, open_trade)
    for msg in trade_messages:
        sent = send_alert(msg)
        log(f"Alerte position envoyée ({msg.splitlines()[0]}) : {sent}")
    for event in trade_events:
        weekly_trades.append(event)
        total_pips_all_time += event["pips"]
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
            "total_pips_all_time": total_pips_all_time,
            "alerts_sent_today": alerts_sent_today,
            "alerts_sent_date": today_str,
        })
        return

    short_sma = simple_moving_average(closes, SHORT_WINDOW)
    long_sma = simple_moving_average(closes, LONG_WINDOW)
    trend_sma = simple_moving_average(closes, TREND_WINDOW)
    rsi = relative_strength_index(closes, RSI_PERIOD)

    trend_sma_prev = None
    if len(closes) >= TREND_WINDOW + TREND_SLOPE_LOOKBACK:
        trend_sma_prev = simple_moving_average(closes[:-TREND_SLOPE_LOOKBACK], TREND_WINDOW)

    if short_sma is not None and long_sma is not None and rsi is not None:
        check_and_send_daily_briefing(state, current_price, short_sma, long_sma, rsi, atr)

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
            "total_pips_all_time": total_pips_all_time,
            "alerts_sent_today": alerts_sent_today,
            "alerts_sent_date": today_str,
        })
        return

    alerts_sent = 0

    if short_sma is not None and long_sma is not None:
        current_sma_signal = "BUY" if short_sma > long_sma else "SELL"
        log(f"SMA : signal actuel={current_sma_signal} (précédent={last_sma_signal}) — SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f}")
        if last_sma_signal is not None and current_sma_signal != last_sma_signal:
            crossover_margin = abs(short_sma - long_sma)
            margin_ok = crossover_margin >= MIN_CROSSOVER_ATR_RATIO * atr
            trend_slope_ok = trend_sma_prev is not None and (
                (current_sma_signal == "BUY" and trend_sma > trend_sma_prev)
                or (current_sma_signal == "SELL" and trend_sma < trend_sma_prev)
            )
            trend_ok = trend_sma is not None and trend_slope_ok and (
                (current_sma_signal == "BUY" and current_price > trend_sma)
                or (current_sma_signal == "SELL" and current_price < trend_sma)
            )
            rsi_ok = rsi is None or not (
                (current_sma_signal == "BUY" and rsi >= RSI_OVERBOUGHT - RSI_SIGNAL_BUFFER)
                or (current_sma_signal == "SELL" and rsi <= RSI_OVERSOLD + RSI_SIGNAL_BUFFER)
            )
            cooldown_ok = cooldown_candles_remaining == 0
            not_duplicate = not already_in_direction(open_trade, current_sma_signal)
            daily_limit_ok = alerts_sent_today < DAILY_SIGNAL_LIMIT

            if margin_ok and trend_ok and rsi_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                close_message, close_event = close_previous_trade_if_open(open_trade, current_price, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    weekly_trades.append(close_event)
                    total_pips_all_time += close_event["pips"]
                    log(f"Position précédente clôturée avant nouveau signal SMA : {sent_close}")

                levels, stop_loss = compute_ladder(current_sma_signal, current_price, atr)
                note = (
                    f"Signal : croisement SMA{SHORT_WINDOW}/SMA{LONG_WINDOW} "
                    f"(SMA{SHORT_WINDOW}={short_sma:.2f} / SMA{LONG_WINDOW}={long_sma:.2f}), "
                    f"confirmé par la tendance de fond (SMA{TREND_WINDOW})"
                )
                message = format_alert(current_sma_signal, current_price, levels, stop_loss, note)
                chart_path = generate_alert_chart(candles, current_sma_signal, current_price, levels, stop_loss)
                sent = send_photo(chart_path, message)
                log(f"Alerte SMA envoyée : {sent}")
                alerts_sent += 1
                alerts_sent_today += 1
                firestore_id = save_signal_to_firestore(current_sma_signal, current_price, levels, stop_loss, note, current_candle_time)
                open_trade = {
                    "action": current_sma_signal,
                    "entry": current_price,
                    "levels": levels,
                    "levels_hit": [False] * LADDER_LEVELS,
                    "sl": stop_loss,
                    "closed": False,
                    "entry_time": current_candle_time,
                    "firestore_id": firestore_id,
                }
            else:
                log(
                    f"Croisement SMA {current_sma_signal} détecté mais filtré "
                    f"(marge suffisante={margin_ok}, tendance de fond favorable={trend_ok}, "
                    f"RSI pas déjà épuisé={rsi_ok}, hors cooldown={cooldown_ok}, "
                    f"pas déjà en position={not_duplicate}, sous la limite quotidienne={daily_limit_ok})."
                )
        last_sma_signal = current_sma_signal

    if rsi is not None:
        current_zone = rsi_zone(rsi)
        log(f"RSI : {rsi:.1f} — zone actuelle={current_zone} (précédente={last_rsi_zone})")

        if last_rsi_zone == "oversold" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma > trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price > trend_sma
            cooldown_ok = cooldown_candles_remaining == 0
            not_duplicate = not already_in_direction(open_trade, "BUY")
            daily_limit_ok = alerts_sent_today < DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                close_message, close_event = close_previous_trade_if_open(open_trade, current_price, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    weekly_trades.append(close_event)
                    total_pips_all_time += close_event["pips"]
                    log(f"Position précédente clôturée avant nouveau signal RSI (BUY) : {sent_close}")

                levels, stop_loss = compute_ladder("BUY", current_price, atr)
                note = f"Signal : RSI sort de survente (RSI={rsi:.1f}), dans le sens de la tendance de fond (SMA{TREND_WINDOW})"
                message = format_alert("BUY", current_price, levels, stop_loss, note)
                chart_path = generate_alert_chart(candles, "BUY", current_price, levels, stop_loss)
                sent = send_photo(chart_path, message)
                log(f"Alerte RSI (BUY) envoyée : {sent}")
                alerts_sent += 1
                alerts_sent_today += 1
                firestore_id = save_signal_to_firestore("BUY", current_price, levels, stop_loss, note, current_candle_time)
                open_trade = {
                    "action": "BUY",
                    "entry": current_price,
                    "levels": levels,
                    "levels_hit": [False] * LADDER_LEVELS,
                    "sl": stop_loss,
                    "closed": False,
                    "entry_time": current_candle_time,
                    "firestore_id": firestore_id,
                }
            else:
                log(f"Sortie de survente détectée mais filtrée (tendance de fond favorable={trend_ok}, "
                    f"hors cooldown={cooldown_ok}, pas déjà en position={not_duplicate}, sous la limite quotidienne={daily_limit_ok}).")
        elif last_rsi_zone == "overbought" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma < trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price < trend_sma
            cooldown_ok = cooldown_candles_remaining == 0
            not_duplicate = not already_in_direction(open_trade, "SELL")
            daily_limit_ok = alerts_sent_today < DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                close_message, close_event = close_previous_trade_if_open(open_trade, current_price, current_candle_time)
                if close_message:
                    sent_close = send_alert(close_message)
                    weekly_trades.append(close_event)
                    total_pips_all_time += close_event["pips"]
                    log(f"Position précédente clôturée avant nouveau signal RSI (SELL) : {sent_close}")

                levels, stop_loss = compute_ladder("SELL", current_price, atr)
                note = f"Signal : RSI sort de surachat (RSI={rsi:.1f}), dans le sens de la tendance de fond (SMA{TREND_WINDOW})"
                message = format_alert("SELL", current_price, levels, stop_loss, note)
                chart_path = generate_alert_chart(candles, "SELL", current_price, levels, stop_loss)
                sent = send_photo(chart_path, message)
                log(f"Alerte RSI (SELL) envoyée : {sent}")
                alerts_sent += 1
                alerts_sent_today += 1
                firestore_id = save_signal_to_firestore("SELL", current_price, levels, stop_loss, note, current_candle_time)
                open_trade = {
                    "action": "SELL",
                    "entry": current_price,
                    "levels": levels,
                    "levels_hit": [False] * LADDER_LEVELS,
                    "sl": stop_loss,
                    "closed": False,
                    "entry_time": current_candle_time,
                    "firestore_id": firestore_id,
                }
            else:
                log(f"Sortie de surachat détectée mais filtrée (tendance de fond favorable={trend_ok}, "
                    f"hors cooldown={cooldown_ok}, pas déjà en position={not_duplicate}, sous la limite quotidienne={daily_limit_ok}).")
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
        "total_pips_all_time": total_pips_all_time,
        "alerts_sent_today": alerts_sent_today,
        "alerts_sent_date": today_str,
    })
    log("=== Fin de la vérification, état sauvegardé ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)
