#!/usr/bin/env python3
"""
ALERTES ORDER BLOCKS -> TELEGRAM (avec capture du graphique)
Order blocks en 15 MIN, 1H et DAILY.

Toutes les 15 minutes :
  1. récupère les bougies 15 min (XAU/USD, NAS100) et en déduit le 1H et le Daily
  2. détecte les order blocks de chaque timeframe avec la logique de ton indicateur
     (swing 3, recherche 6, notation 5 étoiles : displacement, FVG, volume,
      sweep de liquidité, tendance EMA 50)
  3. dès qu'un order block acheteur ou vendeur est TOUCHÉ (même en cours de
     bougie pour le 1H et le Daily), envoie sur le canal Telegram une image du
     graphique + un message avec la zone (ex : 3900.00 - 3880.00).

Variables d'environnement (secrets GitHub) :
  TELEGRAM_TOKEN       token du bot (obligatoire)
  TELEGRAM_CHAT_ID     ID du canal (par défaut : ton canal XAU Guardian)
  TWELVEDATA_API_KEY   clé TwelveData (pour l'or au prix spot, comme ton courtier)
  MIN_STARS            étoiles minimum pour envoyer une alerte (défaut 2)
  TEST_MODE            "1" = envoie tout de suite des images de test
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np
import pandas as pd
import requests

# ─── RÉGLAGES ────────────────────────────────────────────────────
MARCHES = [
    # name = nom affiché | td = symbole TwelveData | hl = marché Hyperliquid (temps réel)
    # yf = symbole Yahoo (secours) | dec = décimales
    {"name": "XAU/USD", "td": "XAU/USD", "yf": "GC=F", "dec": 2},
    {"name": "NAS100",  "td": None, "hl": "xyz:XYZ100", "yf": "NQ=F", "dec": 1},
]
TFS = ["M15", "H1", "D1"]
STEP = {"M15": timedelta(minutes=15), "H1": timedelta(hours=1), "D1": timedelta(days=1)}
NOM_TF = {"M15": "15 min", "H1": "1H", "D1": "Daily"}
TREND_DE = {"M15": "H1", "H1": "M15", "D1": "H1"}     # timeframe utilisée pour l'étoile "tendance"
MIN_BARS = {"M15": 200, "H1": 60, "D1": 25}
BARRES_GRAPH = {"M15": 90, "H1": 90, "D1": 60}

SWING, LOOK, EXPIRE = 3, 6, 150          # identique à l'indicateur
ATR_LEN, DISP = 14, 1.5
VOL_LEN, VOL_MULT = 20, 1.5
SWEEP_LEN, EMA_LEN = 10, 50
FENETRE = timedelta(minutes=50)          # touches récentes prises en compte à chaque passage
MIN_STARS = int(os.getenv("MIN_STARS", "2"))
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "-1003793742525"
TD_KEY = os.getenv("TWELVEDATA_API_KEY", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ob_state.json")
PARIS = ZoneInfo("Europe/Paris")

# Style "MetaTrader" : fond blanc, bougies haussières bleues, baissières noires
BG, TXT, AXE = "#ffffff", "#222222", "#bfbfbf"
HAUSSE, BAISSE = "#1f2eff", "#111111"
EMA_COL = "#c04fd0"
VERT, ROUGE, GRIS = "#00a83e", "#e0102f", "#9a9a9a"


# ─── DONNÉES ─────────────────────────────────────────────────────
def fetch_twelvedata(symbol, retry=True):
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
        "symbol": symbol, "interval": "15min", "outputsize": 5000,
        "timezone": "UTC", "apikey": TD_KEY, "order": "ASC"})
    js = r.json()
    if "values" not in js:
        if retry and "credits" in str(js.get("message", "")).lower():
            import time
            time.sleep(62)                      # limite par minute atteinte : on réessaie
            return fetch_twelvedata(symbol, retry=False)
        raise RuntimeError(f"TwelveData {symbol} : {js.get('message', js)}")
    df = pd.DataFrame(js["values"])
    df["time"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float) if "volume" in df else 0.0
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()


def fetch_yahoo(symbol):
    import yfinance as yf
    df = yf.download(symbol, period="59d", interval="15m", progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo {symbol} : aucune donnée")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].astype(float)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def fetch_hyperliquid(coin):
    """Nasdaq 100 en temps réel (marché xyz:XYZ100 de Hyperliquid), gratuit et sans clé."""
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    r = requests.post("https://api.hyperliquid.xyz/info", timeout=30, json={
        "type": "candleSnapshot",
        "req": {"coin": coin, "interval": "15m", "startTime": now_ms - 60 * 86400 * 1000, "endTime": now_ms}})
    k = r.json()
    if not isinstance(k, list) or not k:
        raise RuntimeError(f"Hyperliquid {coin} : {k}")
    df = pd.DataFrame(k)
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df.set_index("time")[["open", "high", "low", "close", "volume"]].astype(float).sort_index()
    # on garde seulement les heures où le NAS100 de ton courtier est ouvert
    ny = df.index.tz_convert("America/New_York")
    ferme = (ny.dayofweek == 5) | ((ny.dayofweek == 4) & (ny.hour >= 17)) | \
            ((ny.dayofweek == 6) & (ny.hour < 18)) | (ny.hour == 17)
    return df[~ferme]


def fetch_15m(m):
    """Bougies 15 min CLÔTURÉES."""
    df = None
    if m.get("hl"):
        try:
            df = fetch_hyperliquid(m["hl"])
        except Exception as e:  # noqa: BLE001
            print(f"[{m['name']}] Hyperliquid indisponible ({e}), secours Yahoo")
    if df is None:
        df = fetch_twelvedata(m["td"]) if (m["td"] and TD_KEY) else fetch_yahoo(m["yf"])
    now = datetime.now(timezone.utc)
    df = df[df.index + STEP["M15"] <= now]
    return df[~df.index.duplicated(keep="last")].dropna()


def build_tfs(df15):
    """Renvoie {tf: (df_clôturé, df_affichage)} ; l'affichage inclut la bougie en cours."""
    end = df15.index[-1] + STEP["M15"]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = {"M15": (df15, df15)}
    h1 = df15.resample("1h", label="left", closed="left").agg(agg).dropna()
    d1 = df15.resample("24h", offset="22h", label="left", closed="left").agg(agg).dropna()  # clôture 23h Paris
    for tf, d in (("H1", h1), ("D1", d1)):
        out[tf] = (d[d.index + STEP[tf] <= end], d)
    return out


# ─── DÉTECTION (même logique que l'indicateur) ──────────────────
def atr_wilder(h, l, c, n):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values


def detect(df, step, dft, step_t):
    o, h, l, c, v = (df[k].values for k in ("open", "high", "low", "close", "volume"))
    n = len(df)
    atr = atr_wilder(h, l, c, ATR_LEN)
    vol_avg = pd.Series(v).rolling(VOL_LEN).mean().fillna(0).values
    low_prev = pd.Series(l).rolling(SWEEP_LEN).min().values
    high_prev = pd.Series(h).rolling(SWEEP_LEN).max().values

    # tendance (EMA 50) connue à la clôture de chaque bougie
    et = dft["close"].ewm(span=EMA_LEN, adjust=False).mean()
    tt = pd.DataFrame({"t": dft.index + step_t,
                       "bull": (dft["close"] > et).values,
                       "bear": (dft["close"] < et).values})
    t1 = pd.DataFrame({"t": df.index + step})
    mg = pd.merge_asof(t1, tt, on="t", direction="backward")
    htf_bull = mg["bull"].fillna(False).astype(bool).values
    htf_bear = mg["bear"].fillna(False).astype(bool).values

    last_high = last_low = None
    high_broken = low_broken = True
    obs, active = [], []

    for t in range(n):
        p = t - SWING                                     # pivot confirmé à t
        if p >= SWING:
            win_h, win_l = h[p - SWING:t + 1], l[p - SWING:t + 1]
            if h[p] == win_h.max():
                last_high, high_broken = h[p], False
            if l[p] == win_l.min():
                last_low, low_broken = l[p], False

        for is_bull in (True, False):
            if is_bull and not (last_high is not None and not high_broken and c[t] > last_high):
                continue
            if not is_bull and not (last_low is not None and not low_broken and c[t] < last_low):
                continue
            if is_bull:
                high_broken = True
            else:
                low_broken = True
            for j in range(1, LOOK + 1):
                if t - j - 1 < SWEEP_LEN:
                    break
                if not (c[t - j] < o[t - j] if is_bull else c[t - j] > o[t - j]):
                    continue
                disp = volx = fvg = False
                for k in range(0, j):
                    body = (c[t - k] - o[t - k]) if is_bull else (o[t - k] - c[t - k])
                    disp |= body >= DISP * atr[t - k]
                    volx |= vol_avg[t - k] > 0 and v[t - k] >= VOL_MULT * vol_avg[t - k]
                if j >= 2:
                    for k in range(0, j - 1):
                        fvg |= (l[t - k] > h[t - k - 2]) if is_bull else (h[t - k] < l[t - k - 2])
                sweep = (l[t - j] <= low_prev[t - j - 1]) if is_bull else (h[t - j] >= high_prev[t - j - 1])
                htf_ok = htf_bull[t] if is_bull else htf_bear[t]
                stars = int(disp) + int(fvg) + int(volx) + int(sweep) + int(htf_ok)
                ob = {"bull": is_bull, "start": t - j, "born": t, "top": float(h[t - j]),
                      "bot": float(l[t - j]), "stars": stars, "state": "actif", "end": None}
                obs.append(ob)
                active.append(ob)
                break

        for ob in list(active):
            if t <= ob["born"]:
                continue
            broken = c[t] < ob["bot"] if ob["bull"] else c[t] > ob["top"]
            if broken:
                ob["state"], ob["end"] = "cassé", t
                active.remove(ob)
            elif ob["state"] == "actif":
                touch = l[t] <= ob["top"] if ob["bull"] else h[t] >= ob["bot"]
                if touch:
                    ob["state"] = "touché"
                    ob["touch_idx"] = t
                    ob["touch_time"] = df.index[t] + step
                elif t - ob["born"] > EXPIRE:
                    ob["state"], ob["end"] = "expiré", t
                    active.remove(ob)
    return obs


def touches_en_cours(obs, df_closed, step, df15, n_disp):
    """Touches pendant la bougie 1H / Daily en cours, vues sur les bougies 15 min."""
    if not len(df_closed):
        return
    debut = df_closed.index[-1] + step
    after = df15[df15.index >= debut]
    for ob in obs:
        if ob["state"] != "actif":
            continue
        for ts, row in after.iterrows():
            if (row["close"] < ob["bot"]) if ob["bull"] else (row["close"] > ob["top"]):
                break
            if (row["low"] <= ob["top"]) if ob["bull"] else (row["high"] >= ob["bot"]):
                ob["state"] = "touché"
                ob["touch_idx"] = n_disp - 1
                ob["touch_time"] = ts + STEP["M15"]
                break


# ─── GRAPHIQUE ───────────────────────────────────────────────────
def stars_txt(s):
    return "★" * s + "☆" * (5 - s)


def draw(res, tf, m, focus=None, mode="touch"):
    dec = m["dec"]
    df, disp_df, obs = res[tf]["closed"], res[tf]["disp"], res[tf]["obs"]
    n_disp = len(disp_df)
    s0 = max(0, n_disp - BARRES_GRAPH[tf])
    d = disp_df.iloc[s0:]
    nb = len(d)
    ema = disp_df["close"].ewm(span=EMA_LEN, adjust=False).mean().iloc[s0:].values

    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=110)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)
    lo_y, hi_y = d["low"].min(), d["high"].max()
    if focus is not None:                      # la zone concernée est toujours visible
        lo_y, hi_y = min(lo_y, focus["bot"]), max(hi_y, focus["top"])
    pad = (hi_y - lo_y) * 0.08
    y0, y1 = lo_y - pad, hi_y + pad

    # zones des timeframes supérieures (pointillés)
    sup = {"M15": ["H1", "D1"], "H1": ["D1"], "D1": []}[tf]
    for tf2 in sup:
        for ob in res[tf2]["obs"]:
            if ob["state"] in ("cassé", "expiré") or ob["stars"] < MIN_STARS:
                continue
            if ob["top"] < y0 or ob["bot"] > y1:
                continue
            col = VERT if ob["bull"] else ROUGE
            ax.add_patch(Rectangle((-0.5, ob["bot"]), nb + 3.5, ob["top"] - ob["bot"],
                                   facecolor=col, alpha=0.07, edgecolor=col,
                                   linewidth=1.0, linestyle="--", zorder=1))
            ax.text(nb + 3.3, min(max((ob["top"] + ob["bot"]) / 2, y0 + pad * 0.4), y1 - pad * 0.4),
                    f"{tf2}  {ob['top']:.{dec}f} - {ob['bot']:.{dec}f}  {stars_txt(ob['stars'])}",
                    color=col, fontsize=8, va="center", zorder=5, style="italic")

    # order blocks de la timeframe du graphique
    for ob in obs:
        end = ob["end"] if ob["end"] is not None else n_disp + 2
        if end < s0 or ob["state"] == "expiré" or ob["stars"] < MIN_STARS:
            continue
        if ob["top"] < y0 or ob["bot"] > y1:
            continue
        left = max(ob["start"], s0) - s0 - 0.45
        right = min(end, n_disp + 2) - s0
        if ob["state"] == "cassé":
            col, alpha = GRIS, 0.22
        else:
            col, alpha = (VERT if ob["bull"] else ROUGE), 0.20
        is_focus = focus is not None and ob is focus
        ax.add_patch(Rectangle((left, ob["bot"]), right - left, ob["top"] - ob["bot"],
                               facecolor=col, alpha=0.32 if is_focus else alpha,
                               edgecolor="#ff9800" if is_focus else col,
                               linewidth=2.2 if is_focus else 0.8, zorder=1))
        if ob["state"] != "cassé":
            ax.text(right + 0.4, min(max((ob["top"] + ob["bot"]) / 2, y0 + pad * 0.4), y1 - pad * 0.4),
                    f"{tf}  {ob['top']:.{dec}f} - {ob['bot']:.{dec}f}  {stars_txt(ob['stars'])}",
                    color=col, fontsize=8.5, va="center", zorder=5,
                    fontweight="bold" if is_focus else "normal")

    # bougies
    w = 0.62
    for i, (op, hi, lo, cl) in enumerate(d[["open", "high", "low", "close"]].values):
        colc = HAUSSE if cl >= op else BAISSE
        ax.vlines(i, lo, hi, color=colc, linewidth=0.8, zorder=3)
        ax.add_patch(Rectangle((i - w / 2, min(op, cl)), w, max(abs(cl - op), (hi_y - lo_y) * 0.0015),
                               facecolor=colc, edgecolor=colc, linewidth=0.5, zorder=4))
    ax.plot(np.arange(nb), ema, color=EMA_COL, linewidth=1.3, zorder=2)

    if focus is not None and mode == "new" and focus["born"] >= s0:
        xi = focus["start"] - s0
        y = focus["bot"] - pad * 0.45 if focus["bull"] else focus["top"] + pad * 0.45
        ax.text(max(xi, 0), y, "NOUVEL ORDER BLOCK", color="#ff6d00", fontsize=9, fontweight="bold",
                ha="left", va="top" if focus["bull"] else "bottom", zorder=6)
    elif focus is not None and "touch_idx" in focus and focus["touch_idx"] >= s0:
        xi = focus["touch_idx"] - s0
        if focus["bull"]:
            ax.annotate("TOUCHÉ", (xi, d["low"].iloc[xi]), xytext=(xi, d["low"].iloc[xi] - pad * 0.5),
                        color="#ff6d00", fontsize=9, ha="center", va="top", fontweight="bold", zorder=6,
                        arrowprops=dict(arrowstyle="->", color="#ff6d00"))
        else:
            ax.annotate("TOUCHÉ", (xi, d["high"].iloc[xi]), xytext=(xi, d["high"].iloc[xi] + pad * 0.5),
                        color="#ff6d00", fontsize=9, ha="center", va="bottom", fontweight="bold", zorder=6,
                        arrowprops=dict(arrowstyle="->", color="#ff6d00"))

    last = d["close"].iloc[-1]
    ax.axhline(last, color="#8c8c8c", linewidth=0.8, linestyle=(0, (1, 2)), zorder=1)
    ax.text(nb + 0.4, last, f" {last:.{dec}f} ", color="white", fontsize=8.5, va="center",
            bbox=dict(facecolor="#555555", edgecolor="none", pad=1.5), zorder=7)

    fmt = {"M15": "%d/%m %Hh%M", "H1": "%d/%m %Hh", "D1": "%d/%m"}[tf]
    ticks = list(range(0, nb, max(1, nb // 7)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.index[i].tz_convert(PARIS).strftime(fmt) for i in ticks], color=TXT, fontsize=8)
    ax.tick_params(axis="y", colors=TXT, labelsize=8)
    ax.tick_params(colors=AXE)
    ax.yaxis.tick_right()
    ax.set_xlim(-1, nb + max(14, nb * 0.33))
    ax.set_ylim(y0, y1)
    for sp in ax.spines.values():
        sp.set_color(AXE)
    ax.set_title(f"{m['name']} · {NOM_TF[tf]} · Order blocks", color=TXT, fontsize=13,
                 loc="left", pad=10, fontweight="bold")
    ax.text(0.5, 0.5, "XAU Guardian", transform=ax.transAxes, fontsize=46, color="#000000",
            alpha=0.04, ha="center", va="center", zorder=0)
    fig.subplots_adjust(left=0.02, right=0.93, top=0.92, bottom=0.07)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf


# ─── TELEGRAM ────────────────────────────────────────────────────
def send_photo(img, caption):
    if not TOKEN:
        print("TELEGRAM_TOKEN manquant : message non envoyé.\n" + caption)
        return
    r = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", timeout=60,
                      data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                      files={"photo": ("ob.png", img, "image/png")})
    if not r.ok:
        print("Erreur Telegram :", r.text)
        r.raise_for_status()


def message(ob, tf, last_price, m):
    dec = m["dec"]
    t = ob["touch_time"].tz_convert(PARIS)
    sens = "🟢 <b>ORDER BLOCK ACHETEUR TOUCHÉ</b>" if ob["bull"] else "🔴 <b>ORDER BLOCK VENDEUR TOUCHÉ</b>"
    return (f"{sens}\n\n"
            f"📊 <b>{m['name']}</b> · <b>{NOM_TF[tf]}</b>\n"
            f"📍 Zone : <b>{ob['top']:.{dec}f} - {ob['bot']:.{dec}f}</b>\n"
            f"⭐ Qualité : {stars_txt(ob['stars'])} ({ob['stars']}/5)\n"
            f"💰 Prix actuel : {last_price:.{dec}f}\n"
            f"🕐 {t.strftime('%d/%m à %Hh%M')} (Paris)\n\n"
            f"<i>Ceci n'est pas un conseil financier.</i>")


def message_new(ob, tf, born_time, last_price, m):
    dec = m["dec"]
    t = born_time.tz_convert(PARIS)
    sens = "🆕🟢 <b>NOUVEL ORDER BLOCK ACHETEUR</b>" if ob["bull"] else "🆕🔴 <b>NOUVEL ORDER BLOCK VENDEUR</b>"
    return (f"{sens}\n\n"
            f"📊 <b>{m['name']}</b> · <b>{NOM_TF[tf]}</b>\n"
            f"📍 Zone : <b>{ob['top']:.{dec}f} - {ob['bot']:.{dec}f}</b>\n"
            f"⭐ Qualité : {stars_txt(ob['stars'])} ({ob['stars']}/5)\n"
            f"💰 Prix actuel : {last_price:.{dec}f}\n"
            f"🕐 Créé le {t.strftime('%d/%m à %Hh%M')} (Paris)\n\n"
            f"👀 En attente d'un retour du prix dans la zone.\n"
            f"<i>Ceci n'est pas un conseil financier.</i>")


# ─── PROGRAMME PRINCIPAL ─────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": [], "init": False}


def analyse(df15):
    data = build_tfs(df15)
    res = {}
    for tf in TFS:
        closed, disp = data[tf]
        res[tf] = {"closed": closed, "disp": disp, "obs": []}
    for tf in TFS:
        closed = res[tf]["closed"]
        if len(closed) < MIN_BARS[tf]:
            print(f"   {tf} : pas assez de bougies ({len(closed)})")
            continue
        tft = TREND_DE[tf]
        obs = detect(closed, STEP[tf], res[tft]["closed"], STEP[tft])
        if tf != "M15":
            touches_en_cours(obs, closed, STEP[tf], df15, len(res[tf]["disp"]))
        res[tf]["obs"] = obs
    return res


def main():
    state = load_state()
    sent = set(state.get("sent", []))
    first_run = not state.get("init", False)
    erreurs = 0

    for m in MARCHES:
        try:
            df15 = fetch_15m(m)
        except Exception as e:  # noqa: BLE001
            print(f"[{m['name']}] données indisponibles : {e}")
            erreurs += 1
            continue
        if len(df15) < 300:
            print(f"[{m['name']}] pas assez de bougies ({len(df15)})")
            continue
        res = analyse(df15)
        end15 = df15.index[-1] + STEP["M15"]
        last_price = df15["close"].iloc[-1]
        print(f"[{m['name']}] dernière bougie 15 min clôturée à {end15} | "
              + ", ".join(f"{tf}: {len(res[tf]['obs'])} OB" for tf in TFS))

        if TEST_MODE:
            for tf in TFS:
                if res[tf]["obs"]:
                    send_photo(draw(res, tf, m),
                               f"🧪 <b>Test alertes order blocks</b>\n📊 {m['name']} · {NOM_TF[tf]}\n"
                               f"Vert = acheteur · Rouge = vendeur · Gris = cassé\n"
                               f"Pointillés = zones des timeframes supérieures")
            continue

        limite = end15 - (STEP["M15"] if first_run else FENETRE)
        # 1) order blocks qui viennent d'être CRÉÉS
        for tf in TFS:
            for ob in res[tf]["obs"]:
                born_time = res[tf]["closed"].index[ob["born"]] + STEP[tf]
                if ob["stars"] < MIN_STARS or born_time <= limite:
                    continue
                key = f"C|{m['name']}|{tf}|{res[tf]['closed'].index[ob['start']].isoformat()}|{'B' if ob['bull'] else 'S'}"
                if key in sent:
                    continue
                send_photo(draw(res, tf, m, focus=ob, mode="new"), message_new(ob, tf, born_time, last_price, m))
                sent.add(key)
                print("  nouvel OB envoyé :", key)
        # 2) order blocks TOUCHÉS
        for tf in TFS:
            for ob in res[tf]["obs"]:
                if "touch_time" not in ob or ob["stars"] < MIN_STARS or ob["touch_time"] <= limite:
                    continue
                key = f"{m['name']}|{tf}|{res[tf]['closed'].index[ob['start']].isoformat()}|{'B' if ob['bull'] else 'S'}"
                if key in sent:
                    continue
                send_photo(draw(res, tf, m, focus=ob), message(ob, tf, last_price, m))
                sent.add(key)
                print("  alerte envoyée :", key)

    if not TEST_MODE:
        state = {"sent": sorted(sent)[-1500:], "init": True}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
    if erreurs == len(MARCHES):
        sys.exit(1)


if __name__ == "__main__":
    main()
