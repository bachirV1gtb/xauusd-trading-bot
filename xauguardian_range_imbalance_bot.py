"""
XAU Guardian - Range Imbalance Bot (version cron / GitHub Actions)
====================================================================
Implémente la stratégie :
  1) Range de la bougie M5 d'ouverture (session New York, 15h30 heure serveur broker)
  2) Cassure impulsive du range -> détection d'une zone d'imbalance (FVG) M5
  3) Passage en M1 : on attend un RETEST de cette zone + une bougie de confirmation
  4) Entrée dans le sens de la cassure, lot fixe (0.03), SL sous/au-dessus de la
     mèche de la bougie de retest, TP = risque * 2 (ratio 1:2)

⚠️ Ce script est conçu pour être lancé UNE FOIS PAR APPEL (pas de boucle infinie),
par un cron GitHub Actions toutes les minutes pendant la fenêtre active. Son état
(range détecté, en attente de retest, trade déjà pris aujourd'hui...) est lu/écrit
dans state.json à chaque exécution, pour "se souvenir" d'un appel à l'autre.

⚠️ Vérifie l'heure serveur de TON broker et ajuste SESSION_OPEN_HOUR/MINUTE.
⚠️ Teste OBLIGATOIREMENT sur un compte DÉMO avant tout passage en réel.
"""

import MetaTrader5 as mt5
from datetime import datetime
import json
import os

# ----------------------- CONFIGURATION -----------------------
SYMBOL = "XAUUSD"
TIMEFRAME_RANGE = mt5.TIMEFRAME_M5
TIMEFRAME_ENTRY = mt5.TIMEFRAME_M1

SESSION_OPEN_HOUR = 15                  # heure serveur broker à ajuster
SESSION_OPEN_MINUTE = 30

FIXED_LOT = 0.03
RISK_REWARD_RATIO = 2.0
MAX_CANDLES_WAIT_BREAKOUT = 12
MAX_CANDLES_WAIT_RETEST = 30

MAGIC_NUMBER = 20260902
DEVIATION = 20
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

# Identifiants MT5 (à définir en secrets GitHub Actions -> variables d'environnement)
MT5_LOGIN = os.environ.get("MT5_LOGIN")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER = os.environ.get("MT5_SERVER")


# ----------------------- ÉTAT PERSISTANT -----------------------
def load_state():
    default = {
        "last_reset_date": None,
        "range_high": None,
        "range_low": None,
        "range_time": None,
        "direction": None,
        "awaiting_retest": False,
        "trade_taken_today": False,
    }
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            saved = json.load(f)
        default.update(saved)
    return default


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def maybe_reset_for_new_day(state, now):
    today = now.strftime("%Y-%m-%d")
    if state["last_reset_date"] != today:
        print(f"[JOUR] Nouveau jour ({today}), reset de l'état.")
        state.update({
            "last_reset_date": today,
            "range_high": None,
            "range_low": None,
            "range_time": None,
            "direction": None,
            "awaiting_retest": False,
            "trade_taken_today": False,
        })
    return state


# ----------------------- UTILITAIRES MT5 -----------------------
def connect():
    kwargs = {}
    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        kwargs = {"login": int(MT5_LOGIN), "password": MT5_PASSWORD, "server": MT5_SERVER}
    if not mt5.initialize(**kwargs):
        raise RuntimeError(f"Échec connexion MT5 : {mt5.last_error()}")
    info = mt5.symbol_info(SYMBOL)
    if info is None or not info.visible:
        mt5.symbol_select(SYMBOL, True)
    print(f"[OK] Connecté à MT5, symbole {SYMBOL}")


def get_candles(timeframe, count):
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return []
    return rates


# ----------------------- LOGIQUE STRATÉGIE -----------------------
def find_reference_candle():
    candles = get_candles(TIMEFRAME_RANGE, 50)
    for c in candles:
        t = datetime.fromtimestamp(c["time"])
        if t.hour == SESSION_OPEN_HOUR and t.minute == SESSION_OPEN_MINUTE:
            return c
    return None


def detect_imbalance_breakout(state):
    candles = get_candles(TIMEFRAME_RANGE, MAX_CANDLES_WAIT_BREAKOUT + 3)
    ref_time = state["range_time"]
    post_ref = [c for c in candles if c["time"] > ref_time]
    if len(post_ref) < 3:
        return False

    for i in range(len(post_ref) - 2):
        c1, c2, c3 = post_ref[i], post_ref[i + 1], post_ref[i + 2]

        broke_up = c2["close"] > state["range_high"] and c2["close"] > c2["open"]
        broke_down = c2["close"] < state["range_low"] and c2["close"] < c2["open"]

        if broke_up and c3["low"] > c1["high"]:
            state["direction"] = "buy"
            state["awaiting_retest"] = True
            state["imbalance_top"] = float(c3["low"])
            state["imbalance_bottom"] = float(c1["high"])
            print(f"[SIGNAL] Cassure haussière + imbalance : "
                  f"{state['imbalance_bottom']:.2f} - {state['imbalance_top']:.2f}")
            return True

        if broke_down and c3["high"] < c1["low"]:
            state["direction"] = "sell"
            state["awaiting_retest"] = True
            state["imbalance_top"] = float(c1["low"])
            state["imbalance_bottom"] = float(c3["high"])
            print(f"[SIGNAL] Cassure baissière + imbalance : "
                  f"{state['imbalance_bottom']:.2f} - {state['imbalance_top']:.2f}")
            return True

    return False


def detect_retest_and_confirmation(state):
    candles = get_candles(TIMEFRAME_ENTRY, MAX_CANDLES_WAIT_RETEST)
    if len(candles) < 2:
        return None, None

    for i in range(len(candles) - 1):
        touch = candles[i]
        confirm = candles[i + 1]

        touched_zone = (touch["low"] <= state["imbalance_top"] and
                         touch["high"] >= state["imbalance_bottom"])
        if not touched_zone:
            continue

        if state["direction"] == "buy" and confirm["close"] > confirm["open"] and confirm["close"] > touch["high"]:
            return touch, confirm
        if state["direction"] == "sell" and confirm["close"] < confirm["open"] and confirm["close"] < touch["low"]:
            return touch, confirm

    return None, None


# ----------------------- EXÉCUTION DU TRADE -----------------------
def place_trade(touch_candle, direction):
    tick = mt5.symbol_info_tick(SYMBOL)
    entry_price = tick.ask if direction == "buy" else tick.bid

    if direction == "buy":
        sl = float(touch_candle["low"])
        risk = entry_price - sl
        tp = entry_price + risk * RISK_REWARD_RATIO
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl = float(touch_candle["high"])
        risk = sl - entry_price
        tp = entry_price - risk * RISK_REWARD_RATIO
        order_type = mt5.ORDER_TYPE_SELL

    if risk <= 0:
        print("[SKIP] Risque nul/négatif, trade ignoré.")
        return False

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": FIXED_LOT,
        "type": order_type,
        "price": entry_price,
        "sl": sl,
        "tp": tp,
        "deviation": DEVIATION,
        "magic": MAGIC_NUMBER,
        "comment": "RangeImbalanceRetest",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"[ERREUR] Ordre refusé : {result.retcode} - {result.comment}")
        return False

    print(f"[TRADE] {direction.upper()} {FIXED_LOT} lot | "
          f"entrée={entry_price:.2f} SL={sl:.2f} TP={tp:.2f}")
    return True


# ----------------------- POINT D'ENTRÉE (un seul passage) -----------------------
def main():
    state = load_state()
    now = datetime.now()
    state = maybe_reset_for_new_day(state, now)

    if state["trade_taken_today"]:
        print("[INFO] Trade déjà pris aujourd'hui, rien à faire.")
        save_state(state)
        return

    connect()

    if state["range_high"] is None:
        ref = find_reference_candle()
        if ref is not None:
            state["range_high"] = float(ref["high"])
            state["range_low"] = float(ref["low"])
            state["range_time"] = int(ref["time"])
            print(f"[RANGE] Bougie de référence : H={state['range_high']:.2f} L={state['range_low']:.2f}")
        else:
            print("[INFO] Bougie de référence pas encore disponible.")
        save_state(state)
        mt5.shutdown()
        return

    if not state["awaiting_retest"]:
        detect_imbalance_breakout(state)
        save_state(state)
        mt5.shutdown()
        return

    touch, confirm = detect_retest_and_confirmation(state)
    if confirm is not None:
        success = place_trade(touch, state["direction"])
        if success:
            state["trade_taken_today"] = True
            state["awaiting_retest"] = False

    save_state(state)
    mt5.shutdown()


if __name__ == "__main__":
    main()
