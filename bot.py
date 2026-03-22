"""
Elliot Strategy Bot — Fichier unique
- Scan automatique toutes les 3h sur 8 assets
- Commande /signal EURUSD pour un rapport à la demande
- Analyse : macro, price action, trendlines, SL/TP automatiques
"""

import os
import requests
import time
import threading
from datetime import datetime, timezone

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
ALPHAVANTAGE_KEY  = os.environ.get("ALPHAVANTAGE_KEY", "demo")
NEWSAPI_KEY       = os.environ.get("NEWSAPI_KEY", "")

SCORE_THRESHOLD   = 80          # Score minimum pour envoyer une alerte auto
SCAN_INTERVAL_H   = 1           # Scan toutes les X heures (vérification en continu)

ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "EURCHF", "GBPJPY", "AUDUSD", "XAUUSD", "SPX"]

AV_MAP = {
    "EURUSD": ("FX",  "EUR", "USD"),
    "GBPUSD": ("FX",  "GBP", "USD"),
    "USDJPY": ("FX",  "USD", "JPY"),
    "EURCHF": ("FX",  "EUR", "CHF"),
    "GBPJPY": ("FX",  "GBP", "JPY"),
    "AUDUSD": ("FX",  "AUD", "USD"),
    "XAUUSD": ("ETF", "GLD", None),
    "SPX":    ("ETF", "SPY", None),
}

AV_MAP_H4 = {
    "EURUSD": ("FX_INTRADAY", "EUR", "USD"),
    "GBPUSD": ("FX_INTRADAY", "GBP", "USD"),
    "USDJPY": ("FX_INTRADAY", "USD", "JPY"),
    "EURCHF": ("FX_INTRADAY", "EUR", "CHF"),
    "GBPJPY": ("FX_INTRADAY", "GBP", "JPY"),
    "AUDUSD": ("FX_INTRADAY", "AUD", "USD"),
    "XAUUSD": ("ETF_INTRADAY", "GLD", None),
    "SPX":    ("ETF_INTRADAY", "SPY", None),
}

# ─────────────────────────────────────────────
# TELEGRAM HELPERS
# ─────────────────────────────────────────────

def send_telegram(text: str, chat_id: str = None):
    cid = chat_id or TELEGRAM_CHAT_ID
    if not TELEGRAM_TOKEN or not cid:
        print("⚠️  Token ou Chat ID manquant")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            requests.post(url, json={"chat_id": cid, "text": chunk, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Erreur Telegram: {e}")
        time.sleep(0.3)


def get_updates(offset: int = 0):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    try:
        r = requests.get(url, params={"timeout": 30, "offset": offset}, timeout=35)
        return r.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# FETCH PRIX
# ─────────────────────────────────────────────

def _parse_candles(ts: dict, n: int = 60) -> list:
    """Parse un time series AlphaVantage en liste de candles."""
    ok = lambda c: {
        "open":  float(c.get("1. open",  c.get("open",  0))),
        "high":  float(c.get("2. high",  c.get("high",  0))),
        "low":   float(c.get("3. low",   c.get("low",   0))),
        "close": float(c.get("4. close", c.get("close", 0))),
    }
    dates = sorted(ts.keys(), reverse=True)[:n]
    return [{"date": d, **ok(ts[d])} for d in dates]


def fetch_candles(symbol: str) -> list | None:
    """Bougies Daily (contexte macro / structure long terme)."""
    if symbol not in AV_MAP:
        return None
    kind, a, b = AV_MAP[symbol]
    try:
        if kind == "FX":
            url = (f"https://www.alphavantage.co/query?function=FX_DAILY"
                   f"&from_symbol={a}&to_symbol={b}&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            ts = requests.get(url, timeout=15).json().get("Time Series FX (Daily)", {})
        else:
            url = (f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
                   f"&symbol={a}&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            ts = requests.get(url, timeout=15).json().get("Time Series (Daily)", {})
        return _parse_candles(ts, 30) if ts else None
    except Exception as e:
        print(f"[{symbol}] daily fetch error: {e}")
        return None


def fetch_candles_h4(symbol: str) -> list | None:
    """
    Bougies H4 via AlphaVantage FX_INTRADAY 60min (proxy H4 = 4 bougies 60min).
    On regroupe 4 bougies 60min en une bougie H4.
    """
    if symbol not in AV_MAP_H4:
        return None
    kind, a, b = AV_MAP_H4[symbol]
    try:
        if kind == "FX_INTRADAY":
            url = (f"https://www.alphavantage.co/query?function=FX_INTRADAY"
                   f"&from_symbol={a}&to_symbol={b}&interval=60min"
                   f"&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            ts = requests.get(url, timeout=15).json().get("Time Series FX (60min)", {})
        else:
            url = (f"https://www.alphavantage.co/query?function=TIME_SERIES_INTRADAY"
                   f"&symbol={a}&interval=60min"
                   f"&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            ts = requests.get(url, timeout=15).json().get("Time Series (60min)", {})

        if not ts:
            return None

        raw = _parse_candles(ts, 96)  # 96 bougies 60min = 24 bougies H4

        # Regrouper par blocs de 4 → H4
        h4 = []
        for i in range(0, len(raw) - 3, 4):
            block = raw[i:i+4]
            h4.append({
                "date":  block[0]["date"],
                "open":  block[3]["open"],
                "high":  max(c["high"]  for c in block),
                "low":   min(c["low"]   for c in block),
                "close": block[0]["close"],
            })
        return h4 if h4 else None

    except Exception as e:
        print(f"[{symbol}] H4 fetch error: {e}")
        return None

# ─────────────────────────────────────────────
# ANALYSE MACRO
# ─────────────────────────────────────────────

def analyze_macro():
    score = 0
    signals = []

    if NEWSAPI_KEY:
        try:
            url = (f"https://newsapi.org/v2/everything?q=fed+inflation+interest+rate+gdp"
                   f"&sortBy=publishedAt&language=en&pageSize=8&apiKey={NEWSAPI_KEY}")
            articles = requests.get(url, timeout=10).json().get("articles", [])
            hawk, dove = 0, 0
            for a in articles:
                t = (a.get("title") or "").lower()
                for w in ["hike","hawkish","inflation","tight","restrictive","strong jobs"]:
                    if w in t: hawk += 1
                for w in ["cut","dovish","recession","slowdown","easing","pause","weak"]:
                    if w in t: dove += 1
            if hawk > dove:
                signals.append(f"📰 Sentiment HAWKISH ({hawk} signaux) → USD fort potentiel")
                score += 15
            elif dove > hawk:
                signals.append(f"📰 Sentiment DOVISH ({dove} signaux) → USD faible potentiel")
                score += 15
            else:
                signals.append("📰 Sentiment macro neutre")
                score += 5
        except:
            signals.append("⚠️ News indisponibles")

    now = datetime.now(timezone.utc)
    dow, h = now.weekday(), now.hour
    is_weekend = dow in (5, 6)
    if dow == 4 and 12 <= h <= 16:
        signals.append("⚡ Vendredi US session — possible NFP/données importantes")
        score += 20
    elif dow == 2:
        signals.append("📅 Mercredi — possible FOMC/inventaires")
        score += 10

    return max(0, score), signals, is_weekend

# ─────────────────────────────────────────────
# CONTEXTE MACRO PAR ASSET
# ─────────────────────────────────────────────

ASSET_CONTEXT = {
    "EURUSD": {"type": "fx",  "usd": "quote", "desc": "EUR vs USD — sensible Fed/BCE"},
    "GBPUSD": {"type": "fx",  "usd": "quote", "desc": "GBP vs USD — sensible BoE/Fed"},
    "USDJPY": {"type": "fx",  "usd": "base",  "desc": "USD vs JPY — risk-on/off + BoJ"},
    "EURCHF": {"type": "fx",  "usd": None,    "desc": "EUR vs CHF — flux refuge + SNB"},
    "GBPJPY": {"type": "fx",  "usd": None,    "desc": "GBP vs JPY — paire volatile risk-on"},
    "AUDUSD": {"type": "fx",  "usd": "quote", "desc": "AUD vs USD — corrélé commodités/Chine"},
    "XAUUSD": {"type": "gold","usd": "quote", "desc": "Or — refuge + inverse USD"},
    "SPX":    {"type": "idx", "usd": None,    "desc": "S&P500 — risk-on, sensible Fed"},
}

def get_asset_macro_context(symbol: str, macro_signals: list, is_weekend: bool) -> list:
    """Génère des signaux macro spécifiques à l'asset."""
    ctx = ASSET_CONTEXT.get(symbol, {})
    extra = []

    # Warning weekend uniquement pour FX
    if is_weekend and ctx.get("type") == "fx":
        extra.append("😴 Weekend — spread élargi, liquidité réduite sur FX")

    # Contexte USD selon sentiment macro
    sentiment = ""
    for s in macro_signals:
        if "HAWKISH" in s: sentiment = "hawkish"
        if "DOVISH"  in s: sentiment = "dovish"

    usd_role = ctx.get("usd")
    if sentiment == "hawkish":
        if usd_role == "quote":
            extra.append(f"💡 Sentiment hawkish → USD fort → pression haussière sur {symbol}")
        elif usd_role == "base":
            extra.append(f"💡 Sentiment hawkish → USD fort → favorable à {symbol}")
    elif sentiment == "dovish":
        if usd_role == "quote":
            extra.append(f"💡 Sentiment dovish → USD faible → favorable à {symbol}")
        elif usd_role == "base":
            extra.append(f"💡 Sentiment dovish → USD faible → pression baissière sur {symbol}")

    # Contexte spécifique or
    if ctx.get("type") == "gold":
        if sentiment == "dovish":
            extra.append("🥇 Or : corrélation négative USD + sentiment dovish = favorable")
        elif sentiment == "hawkish":
            extra.append("🥇 Or : USD fort = pression baissière sur l'or à surveiller")

    # Contexte indices
    if ctx.get("type") == "idx":
        if sentiment == "dovish":
            extra.append("📊 Indices : dovish = potentiel risk-on → favorable aux indices")
        elif sentiment == "hawkish":
            extra.append("📊 Indices : hawkish = taux hauts = pression sur les valorisations")

    if extra:
        return extra
    return [f"ℹ️ Pas de signal macro directionnel spécifique pour {symbol}"]


# ─────────────────────────────────────────────
# TRENDLINES
# ─────────────────────────────────────────────

def detect_trendlines(candles):
    """
    Détecte les trendlines en cherchant les pivots hauts/bas
    et calcule si le prix actuel est proche d'une trendline.
    """
    signals = []
    score = 0

    if len(candles) < 10:
        return score, signals

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    current = closes[0]

    # ── Pivot highs (résistances) ──
    pivot_highs = []
    for i in range(2, min(20, len(highs)-2)):
        if highs[i] > highs[i-1] and highs[i] > highs[i+1] and highs[i] > highs[i-2] and highs[i] > highs[i+2]:
            pivot_highs.append((i, highs[i]))

    # ── Pivot lows (supports) ──
    pivot_lows = []
    for i in range(2, min(20, len(lows)-2)):
        if lows[i] < lows[i-1] and lows[i] < lows[i+1] and lows[i] < lows[i-2] and lows[i] < lows[i+2]:
            pivot_lows.append((i, lows[i]))

    # ── Trendline haussière (2 pivot lows ascendants) ──
    if len(pivot_lows) >= 2:
        p1, p2 = pivot_lows[0], pivot_lows[1]
        if p2[1] < p1[1]:  # ascending (index croissant = plus ancien)
            slope = (p1[1] - p2[1]) / (p2[0] - p1[0])
            tl_value_now = p1[1] + slope * p1[0]  # extrapolation
            proximity = abs(current - tl_value_now) / current * 100
            if proximity < 0.8:
                signals.append(f"📐 Prix sur TRENDLINE HAUSSIÈRE (distance: {proximity:.2f}%) → support dynamique")
                score += 20
            elif proximity < 2.0:
                signals.append(f"📐 Prix s'approche de la trendline haussière ({proximity:.2f}%)")
                score += 10

    # ── Trendline baissière (2 pivot highs descendants) ──
    if len(pivot_highs) >= 2:
        p1, p2 = pivot_highs[0], pivot_highs[1]
        if p2[1] > p1[1]:  # descending
            slope = (p1[1] - p2[1]) / (p2[0] - p1[0])
            tl_value_now = p1[1] + slope * p1[0]
            proximity = abs(current - tl_value_now) / current * 100
            if proximity < 0.8:
                signals.append(f"📐 Prix sur TRENDLINE BAISSIÈRE (distance: {proximity:.2f}%) → résistance dynamique")
                score += 15
            elif proximity < 2.0:
                signals.append(f"📐 Prix s'approche de la trendline baissière ({proximity:.2f}%)")
                score += 8

    # ── Canal (trendline haute + basse parallèles) ──
    if len(pivot_highs) >= 2 and len(pivot_lows) >= 2:
        range_highs = max(h[1] for h in pivot_highs[:3]) - min(h[1] for h in pivot_highs[:3])
        range_lows  = max(l[1] for l in pivot_lows[:3])  - min(l[1] for l in pivot_lows[:3])
        if range_highs / closes[0] < 0.02 and range_lows / closes[0] < 0.02:
            signals.append("📦 Structure en CANAL détectée (range serré)")
            score += 8

    if not signals:
        signals.append("📐 Pas de trendline significative détectée")

    return score, signals

# ─────────────────────────────────────────────
# PRICE ACTION
# ─────────────────────────────────────────────

def analyze_price_action(candles):
    if not candles or len(candles) < 10:
        return {"score": 0, "signals": ["⚠️ Données insuffisantes"], "direction": "NEUTRE", "current": 0}

    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    current = closes[0]

    score = 0
    signals = []
    direction = "NEUTRE"

    # ── Tendance (HH/HL ou LH/LL) ──
    rh, oh = max(highs[:5]), max(highs[5:15])
    rl, ol = min(lows[:5]),  min(lows[5:15])
    if rh > oh and rl > ol:
        signals.append("📈 Tendance HAUSSIÈRE — Higher Highs + Higher Lows")
        score += 20; direction = "BUY"
    elif rh < oh and rl < ol:
        signals.append("📉 Tendance BAISSIÈRE — Lower Highs + Lower Lows")
        score += 20; direction = "SELL"
    else:
        signals.append("↔️ Structure de marché indécise / range")
        score += 5

    # ── EMA 5 vs EMA 20 ──
    ema5  = sum(closes[:5])  / 5
    ema20 = sum(closes[:20]) / 20
    if direction == "BUY" and ema5 > ema20:
        signals.append(f"✅ EMA5 > EMA20 — momentum haussier confirmé")
        score += 15
    elif direction == "SELL" and ema5 < ema20:
        signals.append(f"✅ EMA5 < EMA20 — momentum baissier confirmé")
        score += 15
    else:
        signals.append(f"⚠️ EMA en conflit avec la tendance")

    # ── Niveaux clés ──
    h20 = max(highs[:20])
    l20 = min(lows[:20])
    r20 = h20 - l20
    if r20 > 0:
        pos = (current - l20) / r20
        if pos < 0.15:
            signals.append(f"🔑 Prix sur SUPPORT majeur ({l20:.5f}) — zone d'achat potentielle")
            score += 20
            if direction != "SELL": direction = "BUY"
        elif pos > 0.85:
            signals.append(f"🔑 Prix sur RÉSISTANCE majeure ({h20:.5f}) — zone de vente potentielle")
            score += 20
            if direction != "BUY": direction = "SELL"

    # ── Trendlines ──
    tl_score, tl_signals = detect_trendlines(candles)
    score += tl_score
    signals += tl_signals

    # ── Bougie de rejet ──
    c0 = candles[0]
    body  = abs(c0["close"] - c0["open"])
    total = c0["high"] - c0["low"]
    if total > 0 and body / total < 0.35:
        signals.append("🕯️ Bougie de rejet (mèche longue / doji) — potentiel retournement")
        score += 10

    return {"score": min(score, 80), "signals": signals, "direction": direction, "current": current,
            "h20": h20, "l20": l20}

# ─────────────────────────────────────────────
# SL / TP AUTOMATIQUES
# ─────────────────────────────────────────────

def find_structure_levels(candles_h4: list, candles_daily: list, direction: str, current: float) -> dict:
    """
    Identifie les vrais niveaux de structure pour SL et TP.

    SL : derrière le dernier swing low/high H4 significatif + buffer ATR
    TP1 : prochain niveau de résistance/support H4 (1er obstacle)
    TP2 : niveau daily suivant (objectif swing complet)

    Aucun RR fixe — les prix viennent du marché.
    """
    h4 = candles_h4 or []
    daily = candles_daily or []

    highs_h4  = [c["high"]  for c in h4]
    lows_h4   = [c["low"]   for c in h4]
    highs_d   = [c["high"]  for c in daily]
    lows_d    = [c["low"]   for c in daily]

    # ATR H4 approximé sur 14 bougies
    atr = 0
    if len(h4) >= 14:
        ranges = [c["high"] - c["low"] for c in h4[:14]]
        atr = sum(ranges) / len(ranges)
    else:
        atr = current * 0.003  # fallback 0.3%

    # ── Pivot highs H4 (résistances) ──
    pivot_res = []
    for i in range(2, min(len(highs_h4)-2, 20)):
        if highs_h4[i] > highs_h4[i-1] and highs_h4[i] > highs_h4[i+1]            and highs_h4[i] > highs_h4[i-2] and highs_h4[i] > highs_h4[i+2]:
            pivot_res.append(highs_h4[i])

    # ── Pivot lows H4 (supports) ──
    pivot_sup = []
    for i in range(2, min(len(lows_h4)-2, 20)):
        if lows_h4[i] < lows_h4[i-1] and lows_h4[i] < lows_h4[i+1]            and lows_h4[i] < lows_h4[i-2] and lows_h4[i] < lows_h4[i+2]:
            pivot_sup.append(lows_h4[i])

    # ── Niveaux daily (obstacles plus larges) ──
    daily_res = sorted([h for h in highs_d[:20] if h > current], )[:3]
    daily_sup = sorted([l for l in lows_d[:20]  if l < current], reverse=True)[:3]

    if direction == "BUY":
        # SL : dernier swing low H4 sous le prix - buffer ATR
        sup_below = sorted([s for s in pivot_sup if s < current], reverse=True)
        sl_base   = sup_below[0] if sup_below else (current - atr * 2)
        sl        = round(sl_base - atr * 0.5, 5)

        # TP1 : 1ère résistance H4 au-dessus du prix
        res_above_h4 = sorted([r for r in pivot_res if r > current + atr * 0.5])
        tp1 = round(res_above_h4[0], 5) if res_above_h4 else round(current + atr * 3, 5)

        # TP2 : résistance daily suivante (objectif swing)
        res_above_d = sorted([r for r in daily_res if r > tp1 + atr * 0.3])
        tp2 = round(res_above_d[0], 5) if res_above_d else round(current + atr * 6, 5)

        risk = current - sl

    elif direction == "SELL":
        # SL : dernier swing high H4 au-dessus + buffer ATR
        res_above = sorted([r for r in pivot_res if r > current])
        sl_base   = res_above[0] if res_above else (current + atr * 2)
        sl        = round(sl_base + atr * 0.5, 5)

        # TP1 : 1er support H4 sous le prix
        sup_below_h4 = sorted([s for s in pivot_sup if s < current - atr * 0.5], reverse=True)
        tp1 = round(sup_below_h4[0], 5) if sup_below_h4 else round(current - atr * 3, 5)

        # TP2 : support daily suivant
        sup_below_d = sorted([s for s in daily_sup if s < tp1 - atr * 0.3], reverse=True)
        tp2 = round(sup_below_d[0], 5) if sup_below_d else round(current - atr * 6, 5)

        risk = sl - current

    else:
        return None

    if risk <= 0:
        risk = atr

    rr1 = abs(tp1 - current) / risk
    rr2 = abs(tp2 - current) / risk

    return {
        "sl":        sl,
        "tp1":       tp1,
        "tp2":       tp2,
        "rr1":       round(rr1, 2),
        "rr2":       round(rr2, 2),
        "risk_pips": round(abs(risk * 10000), 1),
        "atr_pips":  round(atr * 10000, 1),
    }


def compute_sl_tp(pa: dict, symbol: str, candles_h4: list = None, candles_daily: list = None):
    """Wrapper — utilise find_structure_levels si données H4 dispo."""
    if candles_h4 and candles_daily:
        return find_structure_levels(candles_h4, candles_daily, pa["direction"], pa["current"])
    # Fallback si pas de données H4
    current   = pa["current"]
    h20       = pa.get("h20", current * 1.01)
    l20       = pa.get("l20", current * 0.99)
    atr       = (h20 - l20) / 20
    direction = pa["direction"]
    if direction == "BUY":
        sl   = round(l20 - atr * 0.3, 5)
        tp1  = round(h20, 5)
        tp2  = round(h20 + (h20 - l20) * 0.5, 5)
        risk = current - sl
    elif direction == "SELL":
        sl   = round(h20 + atr * 0.3, 5)
        tp1  = round(l20, 5)
        tp2  = round(l20 - (h20 - l20) * 0.5, 5)
        risk = sl - current
    else:
        return None
    if risk <= 0: risk = atr
    return {
        "sl": sl, "tp1": tp1, "tp2": tp2,
        "rr1": round(abs(tp1 - current) / risk, 2),
        "rr2": round(abs(tp2 - current) / risk, 2),
        "risk_pips": round(abs(risk * 10000), 1),
        "atr_pips":  round(atr * 10000, 1),
    }

# ─────────────────────────────────────────────
# RAPPORT COMPLET
# ─────────────────────────────────────────────

def build_full_report(symbol: str, macro_score: int, macro_signals: list, pa: dict, is_weekend: bool = False, candles_h4: list = None, candles_daily: list = None) -> str:
    total = min(100, macro_score + pa["score"])

    if total >= 80:   quality = "🔥 EXCELLENT"
    elif total >= 60: quality = "✅ VALIDE"
    elif total >= 40: quality = "⚠️ FAIBLE"
    else:             quality = "❌ PAS DE SETUP"

    dir_label = {"BUY": "🟢 ACHAT", "SELL": "🔴 VENTE", "NEUTRE": "⚪ NEUTRE"}.get(pa["direction"], "⚪")

    sltp = compute_sl_tp(pa, symbol, candles_h4, candles_daily)

    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M UTC")

    lines = [
        f"━━━━━━━━━━━━━━━━━━━",
        f"📊 *ELLIOT STRATEGY — {symbol}*",
        f"━━━━━━━━━━━━━━━━━━━",
        f"",
        f"💹 *Prix :* `{pa['current']:.5f}`",
        f"📍 *Signal :* {dir_label}",
        f"⭐ *Score :* `{total}/100` — {quality}",
        f"",
        f"*Scores par étape*",
        f"🔍 Macro : `{macro_score} pts`",
        f"📈 Price Action : `{pa['score']} pts`",
    ]

    if sltp and pa["direction"] != "NEUTRE":
        lines += [
            f"",
            f"━━━ 🎯 Niveaux (structure H4) ━━━",
            f"🛑 *Stop Loss :* `{sltp['sl']}` ({sltp['risk_pips']} pips)",
            f"🎯 *TP1 (résistance H4) :* `{sltp['tp1']}` → R/R `{sltp['rr1']}:1`",
            f"🎯 *TP2 (niveau Daily) :*  `{sltp['tp2']}` → R/R `{sltp['rr2']}:1`",
            f"📏 *ATR H4 :* `{sltp['atr_pips']} pips`",
            f"",
            f"_SL et TP posés sur niveaux de structure réels_",
        ]

    lines += [
        f"",
        f"━━━ 🔍 Macro ━━━",
    ]
    for s in macro_signals:
        lines.append(f"• {s}")

    lines += [f"", f"━━━ 📈 Price Action ━━━"]
    for s in pa["signals"]:
        lines.append(f"• {s}")

    lines += [
        f"",
        f"━━━ ⚠️ Checklist avant d'entrer ━━━",
        f"☐ Vérifier état psychologique (étape 3)",
        f"☐ Confirmer plan de gestion (étape 5)",
        f"☐ Calculer taille de position",
        f"☐ Placer le SL AVANT d'entrer",
        f"",
        f"🕐 _{now}_",
        f"━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)

# ─────────────────────────────────────────────
# SCAN COMPLET
# ─────────────────────────────────────────────

def run_scan(chat_id=None, single_symbol=None):
    cid = chat_id or TELEGRAM_CHAT_ID
    targets = [single_symbol] if single_symbol else ASSETS

    send_telegram(f"🔍 *Scan démarré* — {len(targets)} asset(s)...", cid)

    macro_score, macro_signals, is_weekend = analyze_macro()
    alerts = []

    for symbol in targets:
        print(f"Analyse {symbol}...")
        candles = fetch_candles(symbol)
        if not candles:
            send_telegram(f"⚠️ `{symbol}` — données indisponibles (limite API?)", cid)
            time.sleep(12)  # respecter limite AlphaVantage
            continue

        print(f"  Fetch H4 {symbol}...")
        candles_h4 = fetch_candles_h4(symbol)
        time.sleep(12)

        pa = analyze_price_action(candles)
        report = build_full_report(symbol, macro_score, macro_signals, pa, is_weekend,
                                   candles_h4=candles_h4, candles_daily=candles)
        total = min(100, macro_score + pa["score"])

        # En mode /signal on envoie toujours le rapport
        if single_symbol or total >= SCORE_THRESHOLD:
            send_telegram(report, cid)
            alerts.append(symbol)

        time.sleep(12)  # AlphaVantage free = ~5 req/min

    if not single_symbol:
        if alerts:
            send_telegram(f"✅ *Scan terminé* — {len(alerts)} setup(s) : {', '.join(alerts)}", cid)
        else:
            send_telegram(f"💤 *Scan terminé* — Aucun setup ≥ {SCORE_THRESHOLD}/100 détecté sur {len(targets)} assets.", cid)

# ─────────────────────────────────────────────
# LISTENER COMMANDES TELEGRAM
# ─────────────────────────────────────────────

def handle_commands():
    """Écoute les commandes entrantes en continu."""
    offset = 0
    print("👂 Écoute des commandes Telegram...")

    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            if not text or not chat_id:
                continue

            print(f"[CMD] {chat_id}: {text}")

            # /start — message de bienvenue
            if text.startswith("/start"):
                send_telegram(
                    "👋 *Elliot Strategy Bot actif !*\n\n"
                    "*Commandes disponibles :*\n"
                    "`/signal EURUSD` — rapport complet + SL/TP\n"
                    "`/scan` — scanner tous les assets\n"
                    "`/help` — afficher cette aide\n\n"
                    "Assets disponibles : EURUSD, GBPUSD, USDJPY, EURCHF, GBPJPY, AUDUSD, XAUUSD, SPX",
                    chat_id
                )

            # /help
            elif text.startswith("/help"):
                send_telegram(
                    "*Commandes :*\n"
                    "`/signal EURUSD` — analyse complète avec SL/TP\n"
                    "`/scan` — scan de tous les assets\n"
                    "`/assets` — liste des assets surveillés",
                    chat_id
                )

            # /assets
            elif text.startswith("/assets"):
                send_telegram("📋 *Assets surveillés :*\n" + "\n".join(f"• `{a}`" for a in ASSETS), chat_id)

            # /signal SYMBOL
            elif text.startswith("/signal"):
                parts = text.split()
                if len(parts) < 2:
                    send_telegram("❌ Usage : `/signal EURUSD`", chat_id)
                else:
                    symbol = parts[1].upper()
                    if symbol not in AV_MAP:
                        send_telegram(f"❌ Asset inconnu : `{symbol}`\nDisponibles : {', '.join(ASSETS)}", chat_id)
                    else:
                        threading.Thread(target=run_scan, args=(chat_id, symbol), daemon=True).start()

            # /scan manuel
            elif text.startswith("/scan"):
                threading.Thread(target=run_scan, args=(chat_id, None), daemon=True).start()

        time.sleep(2)

# ─────────────────────────────────────────────
# SCHEDULER AUTO
# ─────────────────────────────────────────────

def auto_scheduler():
    """
    Scan toutes les heures en silencieux.
    Envoie une alerte UNIQUEMENT si score >= 80. Pas de message si rien trouve.
    """
    interval_sec = SCAN_INTERVAL_H * 3600
    time.sleep(60)
    while True:
        print("Scan silencieux...")
        macro_score, macro_signals, is_weekend = analyze_macro()
        for symbol in ASSETS:
            candles = fetch_candles(symbol)
            if not candles:
                time.sleep(12)
                continue
            candles_h4 = fetch_candles_h4(symbol)
            time.sleep(12)
            pa = analyze_price_action(candles)
            total = min(100, macro_score + pa["score"])
            print(f"  {symbol}: {total}/100")
            if total >= SCORE_THRESHOLD and pa["direction"] != "NEUTRE":
                report = build_full_report(symbol, macro_score, macro_signals, pa, is_weekend,
                                           candles_h4=candles_h4, candles_daily=candles)
                send_telegram(f"ALERTE MacroFlow - Score {total}/100\n\n" + report)
            time.sleep(12)
        time.sleep(interval_sec)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("🤖 Elliot Strategy Bot démarré")

    if not TELEGRAM_TOKEN:
        print("❌ TELEGRAM_TOKEN manquant — vérifie les variables d'environnement Railway")
        exit(1)

    send_telegram(
        "🚀 *Bot démarré !*\n"
        "Tape `/start` pour voir les commandes disponibles.\n"
        f"Scan automatique toutes les *{SCAN_INTERVAL_H}h*."
    )

    # Thread 1 : écoute des commandes
    t1 = threading.Thread(target=handle_commands, daemon=True)
    t1.start()

    # Thread 2 : scan automatique
    t2 = threading.Thread(target=auto_scheduler, daemon=True)
    t2.start()

    # Garder le process vivant
    while True:
        time.sleep(60)
