"""
Backtest d'une variante à TP EN ÉCHELLE (inspirée du style observé sur le
canal YassoGOLD : plusieurs paliers de profit rapprochés au lieu de 2 TP
espacés, avec un SL plus large qui laisse de la marge).

Réutilise EXACTEMENT le même moteur d'entrée (signaux SMA/RSI + filtres)
que xauusd_github_actions.py — seule la STRUCTURE DE SORTIE change. Ça
permet de savoir si "leur" style de gestion de sortie améliore les
résultats sur NOTRE logique d'entrée déjà éprouvée, sans avoir à deviner
leur logique d'entrée (invisible depuis l'extérieur du canal).

Différences avec leur système observé :
- Paliers espacés proportionnellement à l'ATR (adaptatif), pas un nombre
  fixe de points — leurs "3 points" n'ont de sens qu'au niveau de prix et
  à la volatilité du moment où les captures ont été prises.
- Une fois tous les paliers atteints, le trade est clôturé (pas de suivi
  "illimité" au-delà, contrairement à leurs TP4/TP5 parfois dépassés) —
  simplification assumée pour un backtest clair.

IMPORTANT : les captures du canal ne montraient que des gains (aucun SL
visible) — impossible de connaître leur vrai taux de réussite depuis
l'extérieur. Ce backtest mesure honnêtement CETTE variante sur nos
données, pas "leur" performance réelle.

Usage :
    export TWELVEDATA_API_KEY="ta_clé"
    python backtest_ladder.py 90
"""

import os
import sys
import time
import importlib.util
from datetime import datetime, timedelta, timezone

import requests

BACKTEST_DAYS = 90
INTERVAL = "5min"
SYMBOL = "XAU/USD"
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
REPORT_IMAGE_PATH = "backtest_ladder_report.png"

# --- Paramètres de l'échelle de TP (inspirés du ratio observé : paliers
# resserrés, SL environ 4x plus loin que le premier palier) ---
LADDER_LEVELS = 6
LADDER_STEP_ATR_RATIO = 0.4   # écart entre deux paliers consécutifs
LADDER_SL_ATR_RATIO = 4.0     # SL nettement plus loin que le premier palier
PIP_SIZE = 0.1

BOT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "xauusd_github_actions.py")
os.environ.setdefault("BOT_TOKEN", "backtest")
os.environ.setdefault("CHANNEL_ID", "backtest")
os.environ.setdefault("TWELVEDATA_API_KEY", os.environ.get("TWELVEDATA_API_KEY", ""))

spec = importlib.util.spec_from_file_location("bot", BOT_FILE)
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

TWELVEDATA_API_KEY = os.environ.get("TWELVEDATA_API_KEY")


def fetch_historical_candles(days: int):
    if not TWELVEDATA_API_KEY:
        print("ERREUR : TWELVEDATA_API_KEY manquant.")
        sys.exit(1)
    all_candles = []
    end_date = datetime.now(timezone.utc)
    remaining_days = days
    chunk_days = 16
    while remaining_days > 0:
        this_chunk = min(chunk_days, remaining_days)
        start_date = end_date - timedelta(days=this_chunk)
        params = {
            "symbol": SYMBOL, "interval": INTERVAL,
            "start_date": start_date.strftime("%Y-%m-%d %H:%M:%S"),
            "end_date": end_date.strftime("%Y-%m-%d %H:%M:%S"),
            "apikey": TWELVEDATA_API_KEY, "outputsize": 5000,
        }
        print(f"Récupération : {start_date.date()} -> {end_date.date()} ...")
        try:
            response = requests.get(TWELVEDATA_URL, params=params, timeout=30)
            data = response.json()
            if "values" not in data:
                print(f"  Réponse inattendue : {data}")
                break
            chunk = [
                {"high": float(c["high"]), "low": float(c["low"]), "close": float(c["close"]), "datetime": c["datetime"]}
                for c in reversed(data["values"])
            ]
            print(f"  {len(chunk)} bougies reçues.")
            all_candles = chunk + all_candles
        except requests.RequestException as e:
            print(f"  Erreur réseau : {e}")
            break
        end_date = start_date
        remaining_days -= this_chunk
        time.sleep(8)
    seen = set()
    deduped = []
    for c in all_candles:
        if c["datetime"] not in seen:
            seen.add(c["datetime"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["datetime"])
    return deduped


def compute_ladder(action, entry, atr):
    step = LADDER_STEP_ATR_RATIO * atr
    sl_distance = LADDER_SL_ATR_RATIO * atr
    if action == "BUY":
        levels = [entry + step * (i + 1) for i in range(LADDER_LEVELS)]
        sl = entry - sl_distance
    else:
        levels = [entry - step * (i + 1) for i in range(LADDER_LEVELS)]
        sl = entry + sl_distance
    return levels, sl


def simulate(candles: list):
    """Même moteur de signaux (SMA/RSI + filtres) que xauusd_github_actions.py,
    mais avec une sortie en échelle de paliers au lieu de TP1/TP2/TP3."""
    trades = []
    equity_curve = []
    cumulative_pips = 0

    last_sma_signal = None
    last_rsi_zone = None
    open_trade = None  # {"action", "entry", "levels", "levels_hit", "sl", "closed"}
    cooldown_remaining = 0
    alerts_sent_today = 0
    alerts_sent_date = None

    min_needed = max(bot.LONG_WINDOW, bot.RSI_PERIOD + 1, bot.TREND_WINDOW, bot.ATR_BASELINE_PERIOD + 1) + bot.TREND_SLOPE_LOOKBACK

    def close_at(price, cdate, label):
        nonlocal cumulative_pips
        pips = bot.signed_pips(open_trade["action"], open_trade["entry"], price)
        trades.append({"label": label, "pips": pips, "date": cdate, "action": open_trade["action"]})
        cumulative_pips += pips
        equity_curve.append(cumulative_pips)
        open_trade["closed"] = True

    for i in range(min_needed, len(candles)):
        window = candles[max(0, i - 300):i + 1]
        closes = [c["close"] for c in window]
        candle = candles[i]
        current_price = candle["close"]
        current_date_str = candle["datetime"].split(" ")[0]
        cdate = bot.event_date(candle["datetime"])

        if alerts_sent_date != current_date_str:
            alerts_sent_today = 0
            alerts_sent_date = current_date_str
        if cooldown_remaining > 0:
            cooldown_remaining -= 1

        # --- Suivi de la position ouverte : paliers puis SL ---
        if open_trade is not None and not open_trade.get("closed"):
            high, low = candle["high"], candle["low"]
            action = open_trade["action"]
            sl = open_trade["sl"]

            hit_sl = (low <= sl) if action == "BUY" else (high >= sl)
            if hit_sl:
                close_at(sl, cdate, "SL")
                cooldown_remaining = bot.SL_COOLDOWN_CANDLES
            else:
                for idx, level in enumerate(open_trade["levels"]):
                    if open_trade["levels_hit"][idx]:
                        continue
                    reached = (high >= level) if action == "BUY" else (low <= level)
                    if reached:
                        pips = bot.pips_between(open_trade["entry"], level)
                        trades.append({"label": f"P{idx+1}", "pips": pips, "date": cdate, "action": action})
                        cumulative_pips += pips
                        equity_curve.append(cumulative_pips)
                        open_trade["levels_hit"][idx] = True
                if all(open_trade["levels_hit"]):
                    open_trade["closed"] = True  # tous les paliers atteints, position terminée

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

        if None in (short_sma, long_sma, rsi, atr):
            continue

        min_atr_required = max(bot.ATR_MIN_THRESHOLD, bot.ATR_RELATIVE_MIN_RATIO * atr_baseline) if atr_baseline else bot.ATR_MIN_THRESHOLD
        if atr < min_atr_required:
            last_sma_signal = "BUY" if short_sma > long_sma else "SELL"
            last_rsi_zone = bot.rsi_zone(rsi)
            continue

        def open_new(action, entry_price):
            nonlocal open_trade
            if open_trade is not None and not open_trade.get("closed"):
                close_at(entry_price, cdate, "FLIP")
            levels, sl = compute_ladder(action, entry_price, atr)
            open_trade = {
                "action": action, "entry": entry_price, "levels": levels,
                "levels_hit": [False] * LADDER_LEVELS, "sl": sl, "closed": False,
            }

        # --- Signal 1 : croisement SMA (mêmes filtres que le bot réel) ---
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
            not_duplicate = not (open_trade is not None and not open_trade.get("closed") and open_trade.get("action") == current_sma_signal)
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT

            if margin_ok and trend_ok and rsi_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                open_new(current_sma_signal, current_price)
                alerts_sent_today += 1
        last_sma_signal = current_sma_signal

        # --- Signal 2 : sortie de zone RSI ---
        current_zone = bot.rsi_zone(rsi)
        if last_rsi_zone == "oversold" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma > trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price > trend_sma
            cooldown_ok = cooldown_remaining == 0
            not_duplicate = not (open_trade is not None and not open_trade.get("closed") and open_trade.get("action") == "BUY")
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                open_new("BUY", current_price)
                alerts_sent_today += 1
        elif last_rsi_zone == "overbought" and current_zone == "neutral":
            trend_slope_ok = trend_sma_prev is not None and trend_sma < trend_sma_prev
            trend_ok = trend_sma is not None and trend_slope_ok and current_price < trend_sma
            cooldown_ok = cooldown_remaining == 0
            not_duplicate = not (open_trade is not None and not open_trade.get("closed") and open_trade.get("action") == "SELL")
            daily_limit_ok = alerts_sent_today < bot.DAILY_SIGNAL_LIMIT
            if trend_ok and cooldown_ok and not_duplicate and daily_limit_ok:
                open_new("SELL", current_price)
                alerts_sent_today += 1
        last_rsi_zone = current_zone

    return trades, equity_curve


def print_report(trades, equity_curve, days_covered, n_candles):
    if not trades:
        print("\nAucun trade généré sur cette période avec ces réglages.")
        return

    total_pips = sum(t["pips"] for t in trades)
    n_wins = sum(1 for t in trades if t["pips"] >= 0)
    n_total = len(trades)
    win_rate = n_wins / n_total * 100

    by_label = {}
    for t in trades:
        by_label.setdefault(t["label"], []).append(t["pips"])

    peak = equity_curve[0] if equity_curve else 0
    max_dd = 0
    for v in equity_curve:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)

    n_positions_opened = sum(1 for t in trades if t["label"] in ("SL", "FLIP")) or 1

    print("\n" + "=" * 60)
    print(f"BACKTEST — TP EN ÉCHELLE — {days_covered} jours ({n_candles} bougies)")
    print("=" * 60)
    print(f"Nombre total d'événements : {n_total} (paliers + clôtures)")
    print(f"Taux de réussite global   : {win_rate:.1f}% ({n_wins}/{n_total})")
    print(f"Total de pips             : {'+' if total_pips >= 0 else ''}{total_pips}")
    print(f"Drawdown maximum          : -{max_dd} pips")
    print(f"Moyenne pips/événement    : {total_pips / n_total:+.1f}")
    print("-" * 60)
    for label in sorted(by_label.keys(), key=lambda x: (x not in ("SL", "FLIP"), x)):
        vals = by_label[label]
        print(f"  {label:4s} : {len(vals):4d} événements, total {sum(vals):+6d} pips, moyenne {sum(vals)/len(vals):+.1f}")
    print("=" * 60)
    print("\nRAPPEL : test sur données passées, aucune garantie pour l'avenir.")
    print("Comparer ce total de pips à celui du bot actuel (backtest_strategy.py)")
    print("sur la MÊME période pour juger objectivement laquelle est la meilleure.")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, ax = plt.subplots(figsize=(10, 5), facecolor="#0d0f16")
        ax.set_facecolor("#0d0f16")
        color = "#2ecc71" if total_pips >= 0 else "#e74c3c"
        ax.plot(range(len(equity_curve)), equity_curve, color=color, linewidth=1.5)
        ax.axhline(0, color="#555", linewidth=0.8)
        ax.set_title(f"TP en échelle — courbe de pips cumulés ({days_covered}j)", color="white", fontsize=13)
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
    print(f"Backtest TP EN ÉCHELLE sur {days} jours...")
    candles = fetch_historical_candles(days)
    print(f"\nTotal : {len(candles)} bougies récupérées.")
    if len(candles) < 200:
        print("Pas assez de données récupérées.")
        sys.exit(1)
    trades, equity_curve = simulate(candles)
    print_report(trades, equity_curve, days, len(candles))
