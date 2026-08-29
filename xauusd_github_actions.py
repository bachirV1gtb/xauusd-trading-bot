"""
Script d'alertes de trading XAUUSD vers Telegram — version GitHub Actions (v2, corrigée).
Deux sources de signaux : croisement SMA5/13 + sortie de zone RSI14.

Corrections dans cette version :
- Tous les print() forcent un flush immédiat (flush=True), pour éviter que
  GitHub Actions "avale" les logs à cause de la mise en mémoire tampon.
- Toute erreur inattendue est désormais capturée et affichée avec son
  message complet, au lieu de disparaître silencieusement.

Différences avec la version "PC" :
- S'exécute UNE SEULE FOIS par lancement (GitHub Actions le relance périodiquement).
- Les derniers états (SMA + RSI) sont sauvegardés dans state.json, qui doit être
  commité dans le dépôt entre deux exécutions pour garder la mémoire.
- BOT_TOKEN, TWELVEDATA_API_KEY et CHANNEL_ID sont lus depuis les variables
  d'environnement (GitHub Secrets), jamais écrits en clair dans ce fichier.
"""

import os
import sys
import json
import traceback
import requests
from datetime import datetime, timezone


def log(message):
    """Affiche un message en forçant l'affichage immédiat (anti-buffering)."""
    print(message, flush=True)


# --- Configuration Telegram (lue depuis les variables d'environnement) ---
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHANNEL_ID = os.environ.get("CHANNEL_ID")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

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

STATE_FILE = "state.json"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"last_sma_signal": None, "last_rsi_zone": None}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"last_sma_signal": None, "last_rsi_zone": None}


def save_state(state: dict):
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def send_alert(message: str) -> bool:
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        response = requests.post(TELEGRAM_API_URL, data=payload, timeout=10)
        response.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"[{datetime.now(timezone.utc)}] Erreur envoi Telegram : {e}")
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


def check_open_trade(current_price: float, open_trade: dict):
    """
    Vérifie si le prix actuel a atteint TP1, TP2 ou le SL de la position ouverte.
    Renvoie (messages_a_envoyer, open_trade_mis_a_jour).
    """
    if open_trade is None or open_trade.get("closed"):
        return [], open_trade

    messages = []
    action = open_trade["action"]
    entry = open_trade["entry"]
    tp1, tp2, sl = open_trade["tp1"], open_trade["tp2"], open_trade["sl"]

    if action == "BUY":
        if not open_trade.get("closed") and current_price <= sl:
            messages.append(format_sl_hit(entry, sl))
            open_trade["closed"] = True
        else:
            if not open_trade.get("tp1_hit") and current_price >= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and current_price >= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                open_trade["tp2_hit"] = True
    else:  # SELL
        if not open_trade.get("closed") and current_price >= sl:
            messages.append(format_sl_hit(entry, sl))
            open_trade["closed"] = True
        else:
            if not open_trade.get("tp1_hit") and current_price <= tp1:
                messages.append(format_tp_hit("TP1", entry, tp1))
                open_trade["tp1_hit"] = True
            if not open_trade.get("tp2_hit") and current_price <= tp2:
                messages.append(format_tp_hit("TP2", entry, tp2))
                open_trade["tp2_hit"] = True

    return messages, open_trade


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
    log(f"État précédent chargé : last_sma_signal={last_sma_signal}, last_rsi_zone={last_rsi_zone}, last_candle_time={last_candle_time}, position ouverte={'oui' if open_trade and not open_trade.get('closed') else 'non'}")

    candles = fetch_candles()
    if candles is None or len(candles) < max(LONG_WINDOW, RSI_PERIOD + 1):
        log("Pas assez de données pour calculer les indicateurs, on arrête ici.")
        return

    current_candle_time = candles[-1]["datetime"]
    if last_candle_time is not None and current_candle_time == last_candle_time:
        log(f"Aucune nouvelle bougie depuis la dernière vérification (marché probablement fermé — {current_candle_time}). On arrête ici, pas de recalcul.")
        return

    closes = [c["close"] for c in candles]
    current_price = closes[-1]

    # --- Vérification de la position ouverte (TP1/TP2/SL) ---
    trade_messages, open_trade = check_open_trade(current_price, open_trade)
    for msg in trade_messages:
        sent = send_alert(msg)
        log(f"Alerte position envoyée ({msg.splitlines()[0]}) : {sent}")

    atr = average_true_range(candles, ATR_PERIOD)
    if atr is None:
        log("ATR non calculable, on arrête ici.")
        save_state({
            "last_sma_signal": last_sma_signal,
            "last_rsi_zone": last_rsi_zone,
            "last_candle_time": current_candle_time,
            "open_trade": open_trade,
        })
        return

    if atr < ATR_MIN_THRESHOLD:
        log(f"ATR trop faible ({atr:.3f} < {ATR_MIN_THRESHOLD}) — marché quasi plat, signaux ignorés pour éviter le bruit.")
        save_state({
            "last_sma_signal": last_sma_signal,
            "last_rsi_zone": last_rsi_zone,
            "last_candle_time": current_candle_time,
            "open_trade": open_trade,
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
            }
        last_rsi_zone = current_zone

    if alerts_sent == 0:
        log("Aucun changement détecté, pas d'alerte envoyée.")

    save_state({
        "last_sma_signal": last_sma_signal,
        "last_rsi_zone": last_rsi_zone,
        "last_candle_time": current_candle_time,
        "open_trade": open_trade,
    })
    log("=== Fin de la vérification, état sauvegardé ===")


if __name__ == "__main__":
    try:
        run_once()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)
