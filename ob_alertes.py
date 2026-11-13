#!/usr/bin/env python3
"""
ALERTES ORDER BLOCKS -> TELEGRAM (avec capture du graphique)

À chaque bougie 1H clôturée :
  1. récupère les prix (XAU/USD, NAS100...)
  2. détecte les order blocks avec la même logique que ton indicateur
     (swing 3, recherche 6, notation 5 étoiles : displacement, FVG, volume,
      sweep de liquidité, tendance 15 min EMA 50)
  3. si un order block acheteur ou vendeur vient d'être TOUCHÉ, envoie sur
     le canal Telegram une image du graphique (OB verts / rouges, cassés en gris)
     + un message avec la zone (ex : 3900.00 - 3880.00).

Variables d'environnement (secrets GitHub) :
  TELEGRAM_TOKEN       token du bot (obligatoire)
  TELEGRAM_CHAT_ID     ID du canal (par défaut : ton canal XAU Guardian)
  TWELVEDATA_API_KEY   clé TwelveData (pour l'or au prix spot, comme ton courtier)
  MIN_STARS            étoiles minimum pour envoyer une alerte (défaut 2)
  TEST_MODE            "1" = envoie tout de suite une image de test par marché
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
    # name = nom affiché | td = symbole TwelveData | yf = symbole Yahoo (secours) | dec = décimales
    {"name": "XAU/USD", "td": "XAU/USD", "yf": "GC=F", "dec": 2},
    {"name": "NAS100",  "td": None,      "yf": "NQ=F", "dec": 1},
]
SWING, LOOK, EXPIRE = 3, 6, 150          # identique à l'indicateur
ATR_LEN, DISP = 14, 1.5
VOL_LEN, VOL_MULT = 20, 1.5
SWEEP_LEN, EMA_LEN = 10, 50
BARRES_GRAPH = 90                         # bougies affichées sur l'image
MIN_STARS = int(os.getenv("MIN_STARS", "2"))
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"
TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or "-1003793742525"
TD_KEY = os.getenv("TWELVEDATA_API_KEY", "")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ob_state.json")
PARIS = ZoneInfo("Europe/Paris")

BG, GRID, TXT = "#131722", "#2a2e39", "#d1d4dc"
VERT, ROUGE, GRIS = "#00c853", "#ff1744", "#787b86"


# ─── DONNÉES ─────────────────────────────────────────────────────
def fetch_twelvedata(symbol, interval, retry=True):
    r = requests.get("https://api.twelvedata.com/time_series", timeout=30, params={
        "symbol": symbol, "interval": interval, "outputsize": 600,
        "timezone": "UTC", "apikey": TD_KEY, "order": "ASC"})
    js = r.json()
    if "values" not in js:
        if retry and "credits" in str(js.get("message", "")).lower():
            import time
            time.sleep(62)                      # limite par minute atteinte : on réessaie
            return fetch_twelvedata(symbol, interval, retry=False)
        raise RuntimeError(f"TwelveData {symbol} {interval} : {js.get('message', js)}")
    df = pd.DataFrame(js["values"])
    df["time"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    df["volume"] = df["volume"].astype(float) if "volume" in df else 0.0
    return df.set_index("time")[["open", "high", "low", "close", "volume"]].sort_index()


def fetch_yahoo(symbol, interval):
    import yfinance as yf
    df = yf.download(symbol, period="59d", interval=interval, progress=False, auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo {symbol} {interval} : aucune donnée")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].astype(float)
    df.index = pd.to_datetime(df.index, utc=True)
    return df.sort_index()


def fetch(m, tf):
    """tf = '1h' ou '15m'. Renvoie uniquement les bougies CLÔTURÉES."""
    if m["td"] and TD_KEY:
        df = fetch_twelvedata(m["td"], "1h" if tf == "1h" else "15min")
    else:
        df = fetch_yahoo(m["yf"], tf)
    step = timedelta(hours=1) if tf == "1h" else timedelta(minutes=15)
    now = datetime.now(timezone.utc)
    df = df[df.index + step <= now]          # retire la bougie en cours
    return df.dropna()


# ─── DÉTECTION (même logique que l'indicateur) ──────────────────
def atr_wilder(h, l, c, n):
    pc = np.roll(c, 1); pc[0] = c[0]
    tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc)))
    return pd.Series(tr).ewm(alpha=1 / n, adjust=False).mean().values


def detect(df, df15):
    o, h, l, c, v = (df[k].values for k in ("open", "high", "low", "close", "volume"))
    n = len(df)
    atr = atr_wilder(h, l, c, ATR_LEN)
    vol_avg = pd.Series(v).rolling(VOL_LEN).mean().fillna(0).values
    low_prev = pd.Series(l).rolling(SWEEP_LEN).min().values
    high_prev = pd.Series(h).rolling(SWEEP_LEN).max().values

    # tendance 15 min (EMA 50) connue à la clôture de chaque bougie 1H
    e15 = df15["close"].ewm(span=EMA_LEN, adjust=False).mean()
    t15 = pd.DataFrame({"t": df15.index + timedelta(minutes=15),
                        "bull": (df15["close"] > e15).values,
                        "bear": (df15["close"] < e15).values})
    t1 = pd.DataFrame({"t": df.index + timedelta(hours=1)})
    mg = pd.merge_asof(t1, t15, on="t", direction="backward")
    htf_bull = mg["bull"].fillna(False).astype(bool).values
    htf_bear = mg["bear"].fillna(False).astype(bool).values

    last_high = last_low = None
    high_broken = low_broken = True
    obs, active, touches = [], [], []

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
                    ob["touch"] = t
                    touches.append(ob)
                elif t - ob["born"] > EXPIRE:
                    ob["state"], ob["end"] = "expiré", t
                    active.remove(ob)
    return obs, touches


# ─── GRAPHIQUE ───────────────────────────────────────────────────
def stars_txt(s):
    return "★" * s + "☆" * (5 - s)


def draw(df, obs, m, focus=None):
    n = len(df)
    s0 = max(0, n - BARRES_GRAPH)
    d = df.iloc[s0:]
    x = np.arange(len(d))
    fig, ax = plt.subplots(figsize=(12, 6.75), dpi=110)
    fig.patch.set_facecolor(BG); ax.set_facecolor(BG)

    # order blocks visibles sur la fenêtre
    for ob in obs:
        end = ob["end"] if ob["end"] is not None else n + 3
        if end < s0 or ob["state"] == "expiré" or ob["stars"] < MIN_STARS:
            continue
        left = max(ob["start"], s0) - s0 - 0.4
        right = min(end, n + 3) - s0
        if ob["state"] == "cassé":
            col, alpha, lw = GRIS, 0.18, 0.8
        else:
            col, alpha, lw = (VERT if ob["bull"] else ROUGE), 0.22, 1.0
        is_focus = focus is not None and ob is focus
        ax.add_patch(Rectangle((left, ob["bot"]), right - left, ob["top"] - ob["bot"],
                               facecolor=col, alpha=0.38 if is_focus else alpha,
                               edgecolor="#ffd600" if is_focus else col,
                               linewidth=2.2 if is_focus else lw, zorder=1))
        if ob["state"] != "cassé":
            ax.text(right + 0.3, (ob["top"] + ob["bot"]) / 2,
                    f"{ob['top']:.{m['dec']}f} - {ob['bot']:.{m['dec']}f}  {stars_txt(ob['stars'])}",
                    color=col, fontsize=8.5, va="center", zorder=5,
                    fontweight="bold" if is_focus else "normal")

    # bougies
    up = d["close"].values >= d["open"].values
    for i, (op, hi, lo, cl) in enumerate(d[["open", "high", "low", "close"]].values):
        colc = "#26a69a" if up[i] else "#ef5350"
        ax.vlines(i, lo, hi, color=colc, linewidth=0.9, zorder=2)
        ax.add_patch(Rectangle((i - 0.35, min(op, cl)), 0.7, max(abs(cl - op), 1e-9),
                               facecolor=colc, edgecolor=colc, zorder=3))

    if focus is not None and "touch" in focus and focus["touch"] >= s0:
        xi = focus["touch"] - s0
        y = d["low"].iloc[xi] if focus["bull"] else d["high"].iloc[xi]
        ax.annotate("TOUCHÉ", (xi, y), xytext=(xi, y), color="#ffd600", fontsize=9,
                    ha="center", va="top" if focus["bull"] else "bottom", fontweight="bold", zorder=6)

    last = d["close"].iloc[-1]
    ax.axhline(last, color="#2962ff", linewidth=0.8, linestyle="--", zorder=1)
    ax.text(len(d) + 0.3, last, f" {last:.{m['dec']}f}", color="white", fontsize=8.5, va="center",
            bbox=dict(facecolor="#2962ff", edgecolor="none", pad=1.5), zorder=6)

    ticks = list(range(0, len(d), max(1, len(d) // 8)))
    ax.set_xticks(ticks)
    ax.set_xticklabels([d.index[i].tz_convert(PARIS).strftime("%d/%m %Hh") for i in ticks],
                       color=TXT, fontsize=8)
    ax.tick_params(axis="y", colors=TXT, labelsize=8)
    ax.yaxis.tick_right()
    ax.set_xlim(-1, len(d) + 26)
    lo_y, hi_y = d["low"].min(), d["high"].max()
    pad = (hi_y - lo_y) * 0.06
    ax.set_ylim(lo_y - pad, hi_y + pad)
    ax.grid(color=GRID, linewidth=0.6)
    for sp in ax.spines.values():
        sp.set_color(GRID)
    ax.set_title(f"{m['name']} · 1H · Order blocks", color="white", fontsize=13, loc="left", pad=10)
    ax.text(0.5, 0.5, "XAU Guardian", transform=ax.transAxes, fontsize=46, color="white",
            alpha=0.04, ha="center", va="center", zorder=0)
    buf = io.BytesIO()
    fig.subplots_adjust(left=0.03, right=0.93, top=0.92, bottom=0.07)
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


def message(ob, df, m):
    dec = m["dec"]
    t_close = (df.index[ob["touch"]] + timedelta(hours=1)).tz_convert(PARIS)
    if ob["bull"]:
        tete = "🟢 <b>ORDER BLOCK ACHETEUR TOUCHÉ</b>"
    else:
        tete = "🔴 <b>ORDER BLOCK VENDEUR TOUCHÉ</b>"
    return (f"{tete}\n\n"
            f"📊 <b>{m['name']}</b> · 1H\n"
            f"📍 Zone : <b>{ob['top']:.{dec}f} - {ob['bot']:.{dec}f}</b>\n"
            f"⭐ Qualité : {stars_txt(ob['stars'])} ({ob['stars']}/5)\n"
            f"💰 Prix actuel : {df['close'].iloc[-1]:.{dec}f}\n"
            f"🕐 {t_close.strftime('%d/%m à %Hh%M')} (Paris)\n\n"
            f"<i>Ceci n'est pas un conseil financier.</i>")


# ─── PROGRAMME PRINCIPAL ─────────────────────────────────────────
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"sent": [], "init": False}


def main():
    state = load_state()
    sent = set(state.get("sent", []))
    first_run = not state.get("init", False)
    erreurs = 0

    for m in MARCHES:
        try:
            df = fetch(m, "1h")
            df15 = fetch(m, "15m")
        except Exception as e:  # noqa: BLE001
            print(f"[{m['name']}] données indisponibles : {e}")
            erreurs += 1
            continue
        if len(df) < 60:
            print(f"[{m['name']}] pas assez de bougies ({len(df)})")
            continue
        obs, touches = detect(df, df15)
        n = len(df)
        print(f"[{m['name']}] {n} bougies, {len(obs)} OB détectés, dernière clôture {df.index[-1] + timedelta(hours=1)}")

        if TEST_MODE:
            img = draw(df, obs, m)
            send_photo(img, f"🧪 <b>Test alertes order blocks</b>\n📊 {m['name']} · 1H\n"
                            f"Vert = acheteur · Rouge = vendeur · Gris = cassé")
            continue

        # touches récentes (3 dernières bougies) pas encore envoyées
        recent = 1 if first_run else 3
        for ob in touches:
            if ob["touch"] < n - recent or ob["stars"] < MIN_STARS:
                continue
            key = f"{m['name']}|{df.index[ob['start']].isoformat()}|{'B' if ob['bull'] else 'S'}"
            if key in sent:
                continue
            send_photo(draw(df, obs, m, focus=ob), message(ob, df, m))
            sent.add(key)
            print("  alerte envoyée :", key)

    if not TEST_MODE:
        state = {"sent": sorted(sent)[-300:], "init": True}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
    if erreurs == len(MARCHES):
        sys.exit(1)


if __name__ == "__main__":
    main()
