"""
Backtest de la stratégie XAU Guardian sur des données historiques réelles.

Réutilise EXACTEMENT les mêmes fonctions de calcul (SMA, RSI, ATR, TP/SL,
filtres) que le bot en production (xauusd_github_actions.py), pour garantir
que ce backtest teste bien la stratégie réellement déployée — pas une
approximation qui pourrait diverger avec le temps.

À exécuter dans le Codespace (qui a accès à Internet), PAS dans l'environnement
de développement de Claude (qui n'a pas d'accès réseau) :

    python backtest_strategy.py

Résultats affichés : nombre de trades, répartition TP1/TP2/SL/FLIP, taux de
réussite, total de pips, drawdown maximum. Un rapport image est aussi généré
(backtest_report.png).

IMPORTANT : un backtest sur des données passées ne garantit rien sur les
performances futures. Le marché change, la stratégie qui a bien marché hier
peut ne plus fonctionner demain. C'est un outil d'aide à la décision, pas
une preuve de rentabilité.
"""

import os
import sys
import time
import importlib.util
from datetime import datetime, timedelta, timezone

import requests

# --- Configuration du backtest ---
BACKTEST_DAYS = 60          # nombre de jours d'historique à tester (ajustable)
INTERVAL = "5min"
SYMBOL = "XAU/USD"
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
REPORT_IMAGE_PATH = "backtest_report.png"

# --- Charger le vrai fichier du bot pour réutiliser SES fonctions exactes ---
# Ça garantit que le backtest teste la stratégie RÉELLEMENT en production,
# pas une copie qui pourrait diverger avec le temps.
BOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_github_actions.py")

# Le module du bot lit BOT_TOKEN etc. via variables d'environnement au chargement
# (sans planter s'ils sont absents) — on met des valeurs bidon pour l'import.
os.environ.setdefault("BOT_TOKEN", "backtest")
os.environ.setdefault("CHANNEL_ID", "backtest")
os.environ.setdefault("TWELVEDATA_API_KEY", os.environ.get("TWELVEDATA_API_KEY", ""))

spec = importlib.util.spec_from_file_location("bot", BOT_FILE)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")


def fetch_historical_candles(days: int):
    """Récupère `days` jours de bougies 5 min via Twelve Data, avec pagination
    (l'API limite à 5000 bougies par appel, soit environ 17 jours de 5min)."""
    if not TWELVEDATA_API_KEY:
        print("ERREUR : TWELVEDATA_API_KEY manquant. Lance ce script dans un environnement "
              "où cette variable est définie (ex: export TWELVEDATA_API_KEY=ta_cle avant de lancer).")
        sys.exit(1)

    all_candles = []
    end_date = datetime.now(timezone.utc)
    remaining_days = days
    chunk_days = 16  # marge de sécurité sous la limite de 5000 bougies/appel

    while remaining_days > 0:
        this_chunk = min(chunk_days, remaining_days)
        start_date = end_date - timedelta(days=this_chunk)
        params = {
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "start_date": start_date.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "apikey": TWELVEDATA_API_KEY,
            "outputsize": 5000,
        }
        print(f"Récupération : {start_date.date()} -> {end_date.date()} ...")
        try:
            response = requests.get(TWELVEDATA_URL, params=params, timeout=30)
            data = response.json()
            if "values" not in data:
                print(f"  Réponse inattendue (probablement quota atteint) : {data}")
                break
            chunk_candles = [
                {
                    "high": float(c["high"]),
                    "low": float(c["low"]),
                    "close": float(c["close"]),
                    "datetime": c["datetime"],
                }
                for c in reversed(data["values"])
            ]
            print(f"  {len(chunk_candles)} bougies reçues.")
            all_candles = chunk_candles + all_candles
        except requests.RequestException as e:
            print(f"  Erreur réseau : {e}")
            break

        end_date = start_date
        remaining_days -= this_chunk
        time.sleep(8)  # respecte les limites de requêtes/minute du plan gratuit

    # Dédoublonnage par horodatage, au cas où deux tranches se chevauchent
    seen = set()
    deduped = []
    for c in all_candles:
        if c["datetime"] not in seen:
            seen.add(c["datetime"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["datetime"])
    return deduped


def simulate(candles: list):
    """
    Rejoue la stratégie candle par candle, en réutilisant les mêmes fonctions
    et les mêmes filtres que xauusd_github_actions.py (SMA, RSI, ATR, tendance
    de fond, marge de croisement, cooldown après SL, limite quotidienne).
    Renvoie la liste des événements (trades fermés) et la courbe de pips cumulés.
    """
    trades = []       # liste de dicts {label, pips, date, action}
    equity_curve = []  # pips cumulés après chaque événement, pour le graphique

    last_sma_signal = None
    last_rsi_zone = None
    open_trade = None
    cooldown_remaining = 0
    alerts_sent_today = 0
    alerts_sent_date = None
    cumulative_pips = 0

    min_needed = max(bot.LONG_WINDOW, bot.RSI_PERIOD + 1, bot.TREND_WINDOW, bot.ATR_BASELINE_PERIOD + 1) + bot.TREND_SLOPE_LOOKBACK

    for i in range(min_needed, len(candles)):
        window = candles[max(0, i - 300):i + 1]  # fenêtre glissante suffisante pour tous les indicateurs
        closes = [c["close"] for c in window]
        current_candle = candles[i]
        current_price = current_candle["close"]
        current_date_str = current_candle["datetime"].split(" ")[0]

        # Reset du compteur quotidien si on change de jour (comme en production)
        if alerts_sent_date != current_date_str:
            alerts_sent_today = 0
            alerts_sent_date = current_date_str

        # Cooldown : avance d'un cran à chaque nouvelle bougie
        if cooldown_remaining > 0:
            cooldown_remaining -= 1

        # --- Vérification de la position ouverte (TP1/TP2/SL) sur CETTE bougie ---
        if open_trade is not None and not open_trade.get("closed"):
            high, low = current_candle["high"], current_candle["low"]
            action = open_trade["action"]
            entry = open_trade["entry"]
            tp1, tp2, sl = open_trade["tp1"], open_trade["tp2"], open_trade["sl"]
            candle_date = bot.event_date(current_candle["datetime"])

            if action == "BUY":
                if low <= sl:
                    pips = -bot.pips_between(entry, sl)
                    trades.append({"label": "SL", "pips": pips, "date": candle_date, "action": action})
                    cumulative_pips += pips
                    equity_curve.append(cumulative_pips)
                    open_trade["closed"] = True
                    cooldown_remaining = bot.SL_COOLDOWN_CANDLES
                else:
                    if not open_trade.get("tp1_hit") and high >= tp1:
                        pips = bot.pips_between(entry, tp1)
                        trades.append({"label": "TP1", "pips": pips, "date": candle_date, "action": action})
                        cumulative_pips += pips
                        equity_curve.append(cumulative_pips)
                        open_trade["tp1_hit"] = True
                    if not open_trade.get("tp2_hit") and high >= tp2:
                        pips = bot.pips_between(entry, tp2)
                        trades.append({"label": "TP2", "pips": pips, "date": candle_date, "action": action})
                        cumulative_pips += pips
                        equity_curve.append(cumulative_pips)
                        open_trade["tp2_hit"] = True
            else:  # SELL
                if high >= sl:
                    pips = -bot.pips_between(entry, sl)
                    trades.append({"label": "SL", "pips": pips, "date": candle_date, "action": action})
                    cumulative_pips += pips
                    equity_curve.append(cumulative_pips)
                    open_trade["closed"] = True
                    cooldown_remaining = bot.SL_COOLDOWN_CANDLES
                else:
                    if not open_trade.get("tp1_hit") and low <= tp1:
                        pips = bot.pips_between(entry, tp1)
                        trades.append({"label": "TP1", "pips": pips, "date": candle_date, "action": action})
                        cumulative_pips += pips
                        equity_curve.append(cumulative_pips)
                        open_trade["tp1_hit"] = True
                    if not open_trade.get("tp2_hit") and low <= tp2:
                        pips = bot.pips_between(entry, tp2)
                        trades.append({"label": "TP2", "pips": pips, "date": candle_date, "action": action})
                        cumulative_pips += pips
                        equity_curve.append(cumulative_pips)
                        open_trade["tp2_hit"] = True

        if len(closes) < min_needed:
            continue

        short_sma = bot.simple_moving_average(closes, bot.SHORT_WINDOW)
        long_sma = bot.simple_moving_average(closes, bot.LONG_WINDOW)
        trend_sma = bot.simple_moving_average(closes, bot.TREND_WINDOW)
        rsi = bot.relative_strength_index(closes, bot.RSI_PERIOD)
        atr = bot.average_true_range(window, bot.ATR_PERIOD)
        atr_baseline = bot.average_true_range(window, bot.ATR_BASELINE_PERIOD)

        trend_sma_prev = None
        if len(closes) >= bot.TREND_WINDOW + bot.TREND_SLOPE_LOOKBACK:
            trend_sma_prev = bot.simple_moving_average(closes[:-bot.TREND_SLOPE_LOOKBACK], bot.TREND_WINDOW)

        if short_sma is None or long_sma is None or rsi is None or atr is None:
            continue

        min_atr_required = max(bot.ATR_MIN_THRESHOLD, bot.ATR_RELATIVE_MIN_RATIO * atr_baseline) if atr_baseline else bot.ATR_MIN_THRESHOLD
        if atr < min_atr_required:
            last_sma_signal = "BUY" if short_sma > long_sma else "SELL"
            last_rsi_zone = bot.rsi_zone(rsi)
            continue

        # --- Signal 1 : croisement SMA ---
        current_sma_signal = "BUY" if short_sma > long_sma else "SELL"
        if last_sma_signal is not None and current_sma_signal != last_sma_signal:
            crossover_margin = abs(short_sma - long_sma)
            margin_ok = crossover_margin >= bot.MIN_CROSSOVER_ATR_RATIO * atr
            trend_slope_ok = trend_sma_prev is not None and (
                (current_sma_signal == "BUY" and trend_sma > trend_sma_prev)
                or (current_sma_signal == "SELL" and trend_sma < trend_sma_prev)
            )
            trend_ok = trend_sma is not None and trend_slope_ok and (
                (current_sma_signal == "BUY" and current_price > trend_sma)
                or (current_sma_signal == "SELL" and current_price < trend_sma)
            )
            rsi_ok = not (
                (current_sma_signal == "BUY" and rsi >= bot.RSI_OVERBOUGHT - bot.RSI_SIGNAL_BUFFER)
                or (current_sma_signal == "SELL" and rsi <= bot.RSI_OVERSOLD + bot.RSI_SIGNAL_BUFFER)
            )
            cooldown_ok = cooldown_remaining == 0
            not_duplicate = not bot.already_in_direction(open_trade, current_sma_signal)
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT

            if margin_ok and trend_ok and rsi_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                if open_trade is not None and not open_trade.get("closed"):
                    pips = bot.signed_pips(open_trade["action"], open_trade["entry"], current_price)
                    trades.append({"label": "FLIP", "pips": pips, "date": bot.event_date(current_candle["datetime"]), "action": open_trade["action"]})
                    cumulative_pips += pips
                    equity_curve.append(cumulative_pips)
                tp1, tp2, sl = bot.compute_tp_sl(current_sma_signal, current_price, atr)
                open_trade = {
                    "action": current_sma_signal, "entry": current_price, "tp1": tp1, "tp2": tp2, "sl": sl,
                    "tp1_hit": False, "tp2_hit": False, "closed": False,
                }
                alerts_sent_today += 1
        last_sma_signal = current_sma_signal

        # --- Signal 2 : sortie de zone RSI ---
        current_zone = bot.rsi_zone(rsi)
        if last_rsi_zone == "oversold" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma > trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price > trend_sma
            cooldown_ok = cooldown_remaining == 0
            not_duplicate = not bot.already_in_direction(open_trade, "BUY")
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                if open_trade is not None and not open_trade.get("closed"):
                    pips = bot.signed_pips(open_trade["action"], open_trade["entry"], current_price)
                    trades.append({"label": "FLIP", "pips": pips, "date": bot.event_date(current_candle["datetime"]), "action": open_trade["action"]})
                    cumulative_pips += pips
                    equity_curve.append(cumulative_pips)
                tp1, tp2, sl = bot.compute_tp_sl("BUY", current_price, atr)
                open_trade = {
                    "action": "BUY", "entry": current_price, "tp1": tp1, "tp2": tp2, "sl": sl,
                    "tp1_hit": False, "tp2_hit": False, "closed": False,
                }
                alerts_sent_today += 1
        elif last_rsi_zone == "overbought" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma < trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price < trend_sma
            cooldown_ok = cooldown_remaining == 0
            not_duplicate = not bot.already_in_direction(open_trade, "SELL")
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                if open_trade is not None and not open_trade.get("closed"):
                    pips = bot.signed_pips(open_trade["action"], open_trade["entry"], current_price)
                    trades.append({"label": "FLIP", "pips": pips, "date": bot.event_date(current_candle["datetime"]), "action": open_trade["action"]})
                    cumulative_pips += pips
                    equity_curve.append(cumulative_pips)
                tp1, tp2, sl = bot.compute_tp_sl("SELL", current_price, atr)
                open_trade = {
                    "action": "SELL", "entry": current_price, "tp1": tp1, "tp2": tp2, "sl": sl,
                    "tp1_hit": False, "tp2_hit": False, "closed": False,
                }
                alerts_sent_today += 1
        last_rsi_zone = current_zone

    return trades, equity_curve


def print_report(trades: list, equity_curve: list, days_covered: int, n_candles: int):
    if not trades:
        print("\nAucun trade généré sur cette période avec les réglages actuels.")
        print("Soit la période est trop calme, soit les filtres sont très stricts (voulu).")
        return

    total_pips = sum(t["pips"] for t in trades)
    n_wins = sum(1 for t in trades if t["pips"] >= 0)
    n_total = len(trades)
    win_rate = n_wins / n_total * 100

    by_label = {}
    for t in trades:
        by_label.setdefault(t["label"], []).append(t["pips"])

    # Drawdown maximum (plus grande chute depuis un sommet, sur la courbe de pips cumulés)
    peak = equity_curve[0] if equity_curve else 0
    max_drawdown = 0
    for value in equity_curve:
        peak = max(peak, value)
        drawdown = peak - value
        max_drawdown = max(max_drawdown, drawdown)

    print("\n" + "=" * 55)
    print(f"RÉSULTATS DU BACKTEST — {days_covered} jours ({n_candles} bougies 5min)")
    print("=" * 55)
    print(f"Nombre total d'événements : {n_total}")
    print(f"Taux de réussite global   : {win_rate:.1f}% ({n_wins}/{n_total})")
    print(f"Total de pips             : {'+' if total_pips >= 0 else ''}{total_pips}")
    print(f"Drawdown maximum          : -{max_drawdown} pips (pire chute depuis un sommet)")
    print(f"Moyenne pips/événement    : {total_pips / n_total:+.1f}")
    print("-" * 55)
    for label in ["TP1", "TP2", "SL", "FLIP"]:
        if label in by_label:
            vals = by_label[label]
            print(f"  {label:5s} : {len(vals):4d} événements, total {sum(vals):+6d} pips, moyenne {sum(vals)/len(vals):+.1f}")
    print("=" * 55)
    print("\nRAPPEL : ceci est un test sur le PASSÉ. Aucune garantie que ces résultats")
    print("se reproduisent sur les marchés futurs. À utiliser comme indicateur relatif")
    print("pour comparer des réglages entre eux, pas comme une promesse de gain.")

    # --- Graphique de la courbe de pips cumulés ---
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(10, 5), facecolor="#0d0f16")
        ax.set_facecolor("#0d0f16")
        color = "#2ecc71" if total_pips >= 0 else "#e74c3c"
        ax.plot(range(len(equity_curve)), equity_curve, color=color, linewidth=1.5)
        ax.axhline(0, color="#555", linewidth=0.8)
        ax.set_title(f"Courbe de pips cumulés — {days_covered} jours de backtest", color="white", fontsize=13)
        ax.set_xlabel("Événements (TP1/TP2/SL/FLIP)", color="#aaa")
        ax.set_ylabel("Pips cumulés", color="#aaa")
        ax.tick_params(colors="#aaa")
        for spine in ax.spines.values():
            spine.set_color("#333")
        fig.savefig(REPORT_IMAGE_PATH, facecolor="#0d0f16", dpi=130, bbox_inches="tight")
        plt.close(fig)
        print(f"\nGraphique sauvegardé : {REPORT_IMAGE_PATH}")
    except Exception as e:
        print(f"(Graphique non généré : {e})")


if __name__ == "__main__":
    days = BACKTEST_DAYS
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass

    print(f"Backtest sur {days} jours de données XAU/USD (5min)...")
    candles = fetch_historical_candles(days)
    print(f"\nTotal : {len(candles)} bougies récupérées.")

    if len(candles) < 200:
        print("Pas assez de données récupérées pour un backtest fiable. Vérifie ta clé API "
              "et ton quota Twelve Data (le plan gratuit limite le nombre de requêtes/jour).")
        sys.exit(1)

    trades, equity_curve = simulate(candles)
    print_report(trades, equity_curve, days, len(candles))
