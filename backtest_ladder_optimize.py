"""
Backtest comparatif : teste PLUSIEURS largeurs de SL (et, en option, un
filtre d'entrée plus strict) sur la MÊME période de 90 jours, pour trouver
un réglage qui réduit le nombre de stop loss sans sacrifier le total de
pips — plutôt que de deviner un nouveau chiffre au hasard.

Réutilise le même moteur d'entrée (SMA/RSI + filtres) que
xauusd_github_actions.py — seuls les paramètres testés varient d'une
variante à l'autre.

Usage :
    export TWELVEDATA_API_KEY="ta_clé"
    python backtest_ladder_optimize.py 90
"""

import os
import sys
import time
import importlib.util
from datetime import datetime, timedelta, timezone

import requests

INTERVAL = "5min"
SYMBOL = "XAU/USD"
TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
REPORT_IMAGE_PATH = "backtest_ladder_optimize_report.png"

LADDER_LEVELS = 6
LADDER_STEP_ATR_RATIO = 0.4
PIP_SIZE = 0.1

# --- Variantes testées : (nom, SL en multiple d'ATR, marge de croisement SMA
# en multiple d'ATR — une marge plus grande = signaux plus rares mais plus
# nets, ce qui réduit aussi souvent les faux départs qui finissent en SL) ---
VARIANTS = [
    {"name": "Actuelle (SL=4.0x, marge=0.35x)", "sl_ratio": 4.0, "margin_ratio": 0.35},
    {"name": "SL élargi (SL=6.0x, marge=0.35x)", "sl_ratio": 6.0, "margin_ratio": 0.35},
    {"name": "SL élargi (SL=8.0x, marge=0.35x)", "sl_ratio": 8.0, "margin_ratio": 0.35},
    {"name": "SL élargi + entrée plus stricte (SL=6.0x, marge=0.6x)", "sl_ratio": 6.0, "margin_ratio": 0.6},
    {"name": "SL élargi + entrée plus stricte (SL=8.0x, marge=0.6x)", "sl_ratio": 8.0, "margin_ratio": 0.6},
]

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


def compute_ladder(action, entry, atr, sl_ratio):
    step = LADDER_STEP_ATR_RATIO * atr
    sl_distance = sl_ratio * atr
    if action == "BUY":
        levels = [entry + step * (i + 1) for i in range(LADDER_LEVELS)]
        sl = entry - sl_distance
    else:
        levels = [entry - step * (i + 1) for i in range(LADDER_LEVELS)]
        sl = entry + sl_distance
    return levels, sl


def simulate(candles: list, sl_ratio: float, margin_ratio: float):
    """Même moteur de signaux que xauusd_github_actions.py, mais avec un SL
    et une marge de croisement paramétrables pour comparer les variantes.
    Compte aussi les POSITIONS complètes (pas seulement les événements) pour
    un taux de réussite honnête."""
    trades = []
    positions = []  # une entrée par position complète : {"outcome": "win"/"loss"/"mixed", "pips": total}
    cumulative_pips = 0

    last_sma_signal = None
    last_rsi_zone = None
    open_trade = None
    cooldown_remaining = 0
    alerts_sent_today = 0
    alerts_sent_date = None

    min_needed = max(bot.LONG_WINDOW, bot.RSI_PERIOD + 1, bot.TREND_WINDOW, bot.ATR_BASELINE_PERIOD + 1) + bot.TREND_SLOPE_LOOKBACK

    def finalize_position(final_label):
        nonlocal cumulative_pips
        pos_pips = sum(e["pips"] for e in open_trade["events"])
        n_levels_hit = sum(1 for e in open_trade["events"] if e["label"].startswith("P"))
        hit_sl = any(e["label"] == "SL" for e in open_trade["events"])
        if hit_sl and n_levels_hit == 0:
            outcome = "loss"
        elif not hit_sl:
            outcome = "win"
        else:
            outcome = "mixed"  # a touché des paliers avant de finir en SL
        positions.append({"outcome": outcome, "pips": pos_pips})
        open_trade["closed"] = True

    def close_at(price, cdate, label):
        nonlocal cumulative_pips
        pips = bot.signed_pips(open_trade["action"], open_trade["entry"], price)
        event = {"label": label, "pips": pips, "date": cdate, "action": open_trade["action"]}
        trades.append(event)
        open_trade["events"].append(event)
        cumulative_pips += pips
        finalize_position(label)

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
                        event = {"label": f"P{idx+1}", "pips": pips, "date": cdate, "action": action}
                        trades.append(event)
                        open_trade["events"].append(event)
                        cumulative_pips += pips
                        open_trade["levels_hit"][idx] = True
                if all(open_trade["levels_hit"]):
                    finalize_position("ALL_LEVELS")

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
                pips = bot.signed_pips(open_trade["action"], open_trade["entry"], entry_price)
                event = {"label": "FLIP", "pips": pips, "date": cdate, "action": open_trade["action"]}
                trades.append(event)
                open_trade["events"].append(event)
                finalize_position("FLIP")
            levels, sl = compute_ladder(action, entry_price, atr, sl_ratio)
            open_trade = {
                "action": action, "entry": entry_price, "levels": levels,
                "levels_hit": [False] * LADDER_LEVELS, "sl": sl, "closed": False,
                "events": [],
            }

        current_sma_signal = "BUY" if short_sma > long_sma else "SELL"
        if last_sma_signal is not None and current_sma_signal != last_sma_signal:
            crossover_margin = abs(short_sma - long_sma)
            margin_ok = crossover_margin >= margin_ratio * atr
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

    # Position encore ouverte à la fin des données : on l'ignore (ni gagnée ni perdue).
    return trades, positions


def print_variant_report(name, trades, positions):
    total_pips = sum(t["pips"] for t in trades)
    n_positions = len(positions)
    n_wins = sum(1 for p in positions if p["outcome"] == "win")
    n_losses = sum(1 for p in positions if p["outcome"] == "loss")
    n_mixed = sum(1 for p in positions if p["outcome"] == "mixed")
    win_rate = (n_wins / n_positions * 100) if n_positions else 0
    loss_rate = (n_losses / n_positions * 100) if n_positions else 0

    print(f"\n--- {name} ---")
    print(f"  Positions complètes       : {n_positions}")
    print(f"  Gagnantes (aucun SL)      : {n_wins} ({win_rate:.1f}%)")
    print(f"  Mixtes (paliers + SL)     : {n_mixed}")
    print(f"  Perdantes (SL direct)     : {n_losses} ({loss_rate:.1f}%)")
    print(f"  Total pips                : {'+' if total_pips >= 0 else ''}{total_pips}")

    return {
        "name": name, "n_positions": n_positions, "n_wins": n_wins,
        "n_losses": n_losses, "n_mixed": n_mixed, "win_rate": win_rate,
        "total_pips": total_pips,
    }


if __name__ == "__main__":
    days = 90
    if len(sys.argv) > 1:
        try:
            days = int(sys.argv[1])
        except ValueError:
            pass
    print(f"Backtest comparatif sur {days} jours ({len(VARIANTS)} variantes)...")
    candles = fetch_historical_candles(days)
    print(f"\nTotal : {len(candles)} bougies récupérées.")
    if len(candles) < 200:
        print("Pas assez de données récupérées.")
        sys.exit(1)

    results = []
    for variant in VARIANTS:
        trades, positions = simulate(candles, variant["sl_ratio"], variant["margin_ratio"])
        results.append(print_variant_report(variant["name"], trades, positions))

    print("\n" + "=" * 78)
    print(f"{'Variante':<55}{'Positions':>8}{'%Perte':>8}{'Pips':>8}")
    print("=" * 78)
    for r in results:
        loss_pct = (r["n_losses"] / r["n_positions"] * 100) if r["n_positions"] else 0
        print(f"{r['name']:<55}{r['n_positions']:>8}{loss_pct:>7.1f}%{r['total_pips']:>+8}")
    print("=" * 78)
    print("\nRAPPEL : test sur données passées, aucune garantie pour l'avenir.")
    print("Choisis la variante avec le meilleur compromis %Perte / Pips totaux.")
