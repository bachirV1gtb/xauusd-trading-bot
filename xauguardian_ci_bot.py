"""
XAU Guardian - Range Imbalance Bot (version GitHub Actions, exécution bornée)
================================================================================
Variante de xauguardian_local_bot.py conçue pour tourner sur un runner
GitHub Actions Windows (machine éphémère), sans PC allumé.

Différences avec la version locale :
  - Se connecte via des identifiants passés en variables d'environnement
    (secrets GitHub), pas besoin que MT5 soit déjà ouvert manuellement.
  - Boucle bornée dans le temps (MAX_RUNTIME_MINUTES) au lieu d'un
    "while True" infini, car un job CI doit se terminer.
  - Un seul jour / une seule session traitée par exécution (le workflow
    est planifié pour se déclencher chaque jour de bourse avant l'heure
    d'ouverture de session).

Variables d'environnement requises (à définir comme secrets GitHub) :
  MT5_LOGIN     : numéro de compte (ex: 5055241984)
  MT5_PASSWORD  : mot de passe du compte
  MT5_SERVER    : nom du serveur (ex: MetaQuotes-Demo)
  MT5_PATH      : chemin vers terminal64.exe (optionnel, valeur par défaut ci-dessous)
"""

import os
import sys
import time
import traceback
from datetime import datetime, timezone

import MetaTrader5 as mt5

# ----------------------- CONFIGURATION STRATÉGIE (identique à la version locale) -----------------------
SYMBOL = "XAUUSD"
TIMEFRAME_RANGE = mt5.TIMEFRAME_M5
TIMEFRAME_ENTRY = mt5.TIMEFRAME_M1

SESSION_OPEN_HOUR = 15                  # heure SERVEUR du broker (visible dans MT5), pas ton heure locale
SESSION_OPEN_MINUTE = 30

FIXED_LOT = 0.03
RISK_REWARD_RATIO = 2.0
MAX_CANDLES_WAIT_BREAKOUT = 12          # nb de bougies M5 max pour attendre la cassure
MAX_CANDLES_WAIT_RETEST = 30            # nb de bougies M1 max pour attendre le retest

MAGIC_NUMBER = 20260902
DEVIATION = 20

# ----------------------- CONFIGURATION EXÉCUTION CI -----------------------
MAX_RUNTIME_MINUTES = 150               # sécurité : le job s'arrête après ça quoi qu'il arrive
POLL_SECONDS = 15                       # un peu plus espacé qu'en local pour ménager le runner

MT5_LOGIN = os.environ.get("MT5_LOGIN")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD")
MT5_SERVER = os.environ.get("MT5_SERVER")
MT5_PATH = os.environ.get("MT5_PATH", r"C:\Program Files\MetaTrader 5\terminal64.exe")


def log(message):
    now = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{now} UTC] {message}", flush=True)


# ----------------------- CONNEXION -----------------------
def connect():
    if not MT5_LOGIN or not MT5_PASSWORD or not MT5_SERVER:
        log("[ERREUR] MT5_LOGIN / MT5_PASSWORD / MT5_SERVER manquants (secrets non transmis).")
        sys.exit(1)

    ok = mt5.initialize(
        path=MT5_PATH,
        login=int(MT5_LOGIN),
        password=MT5_PASSWORD,
        server=MT5_SERVER,
        timeout=60000,
    )
    if not ok:
        log(f"[ERREUR] Échec connexion MT5 : {mt5.last_error()}")
        sys.exit(1)

    info = mt5.symbol_info(SYMBOL)
    if info is None or not info.visible:
        mt5.symbol_select(SYMBOL, True)

    account = mt5.account_info()
    log(f"[OK] Connecté à MT5 — compte {account.login if account else '?'} "
        f"({account.server if account else MT5_SERVER}), symbole {SYMBOL}")


def get_candles(timeframe, count):
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return []
    return rates


# ----------------------- LOGIQUE STRATÉGIE (identique à la version locale) -----------------------
def find_reference_candle():
    candles = get_candles(TIMEFRAME_RANGE, 50)
    for c in candles:
        t = datetime.utcfromtimestamp(c["time"])
        if t.hour == SESSION_OPEN_HOUR and t.minute == SESSION_OPEN_MINUTE:
            return c
    return None


def detect_imbalance_breakout(state):
    candles = get_candles(TIMEFRAME_RANGE, MAX_CANDLES_WAIT_BREAKOUT + 3)
    post_ref = [c for c in candles if c["time"] > state["range_time"]]
    if len(post_ref) < 3:
        return False

    for i in range(len(post_ref) - 2):
        c1, c2, c3 = post_ref[i], post_ref[i + 1], post_ref[i + 2]

        broke_up = c2["close"] > state["range_high"] and c2["close"] > c2["open"]
        broke_down = c2["close"] < state["range_low"] and c2["close"] < c2["open"]

        if broke_up and c3["low"] > c1["high"]:
            state["direction"] = "buy"
            state["imbalance_bottom"] = c1["high"]
            state["imbalance_top"] = c3["low"]
            state["awaiting_retest"] = True
            log(f"[SIGNAL] Cassure haussière + imbalance : "
                f"{state['imbalance_bottom']:.2f} - {state['imbalance_top']:.2f}")
            return True

        if broke_down and c3["high"] < c1["low"]:
            state["direction"] = "sell"
            state["imbalance_top"] = c1["low"]
            state["imbalance_bottom"] = c3["high"]
            state["awaiting_retest"] = True
            log(f"[SIGNAL] Cassure baissière + imbalance : "
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
        log("[SKIP] Risque nul/négatif, trade ignoré.")
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
        "comment": "RangeImbalanceRetest-CI",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        log(f"[ERREUR] Ordre refusé : {result.retcode if result else '?'} - "
            f"{result.comment if result else mt5.last_error()}")
        return False

    log(f"[TRADE] {direction.upper()} {FIXED_LOT} lot | "
        f"entrée={entry_price:.2f} SL={sl:.2f} TP={tp:.2f}")
    return True


# ----------------------- BOUCLE PRINCIPALE (bornée) -----------------------
def run():
    connect()
    start = time.monotonic()
    deadline = start + MAX_RUNTIME_MINUTES * 60

    state = {
        "range_high": None, "range_low": None, "range_time": None,
        "direction": None, "imbalance_top": None, "imbalance_bottom": None,
        "awaiting_retest": False,
    }

    log(f"Bot démarré. Fenêtre max : {MAX_RUNTIME_MINUTES} minutes. "
        f"En attente de la bougie d'ouverture de session ({SESSION_OPEN_HOUR:02d}:{SESSION_OPEN_MINUTE:02d} serveur)...")

    trade_taken = False

    while time.monotonic() < deadline and not trade_taken:
        try:
            if state["range_high"] is None:
                ref = find_reference_candle()
                if ref is not None:
                    state["range_high"] = ref["high"]
                    state["range_low"] = ref["low"]
                    state["range_time"] = ref["time"]
                    log(f"[RANGE] Bougie de référence : H={state['range_high']:.2f} L={state['range_low']:.2f}")
                time.sleep(POLL_SECONDS)
                continue

            if not state["awaiting_retest"]:
                found = detect_imbalance_breakout(state)
                if not found:
                    time.sleep(POLL_SECONDS)
                continue

            touch, confirm = detect_retest_and_confirmation(state)
            if confirm is not None:
                trade_taken = place_trade(touch, state["direction"])
                if not trade_taken:
                    # évite de boucler indéfiniment sur un ordre refusé
                    break

            time.sleep(POLL_SECONDS)

        except Exception as e:
            log(f"[ERREUR BOUCLE] {e}")
            log(traceback.format_exc())
            time.sleep(POLL_SECONDS)

    if not trade_taken:
        log("[FIN] Fenêtre de session terminée sans trade pris (comportement normal, "
            "ça n'arrive pas tous les jours).")
    else:
        log("[FIN] Trade pris, fin de l'exécution.")

    mt5.shutdown()


if __name__ == "__main__":
    try:
        run()
    except Exception:
        log("=== ERREUR INATTENDUE ===")
        log(traceback.format_exc())
        sys.exit(1)
