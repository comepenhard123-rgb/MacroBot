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

def fetch_candles(symbol: str):
    """Retourne une liste de dicts {date, open, high, low, close} ou None."""
    if symbol not in AV_MAP:
        return None
    kind, a, b = AV_MAP[symbol]
    try:
        if kind == "FX":
            url = (f"https://www.alphavantage.co/query?function=FX_DAILY"
                   f"&from_symbol={a}&to_symbol={b}&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            r = requests.get(url, timeout=15).json()
            ts = r.get("Time Series FX (Daily)", {})
            ok = lambda c: {"open": float(c["1. open"]), "high": float(c["2. high"]),
                            "low":  float(c["3. low"]),  "close": float(c["4. close"])}
        else:
            url = (f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
                   f"&symbol={a}&outputsize=compact&apikey={ALPHAVANTAGE_KEY}")
            r = requests.get(url, timeout=15).json()
            ts = r.get("Time Series (Daily)", {})
            ok = lambda c: {"open": float(c["1. open"]), "high": float(c["2. high"]),
                            "low":  float(c["3. low"]),  "close": float(c["4. close"])}

        if not ts:
            return None
        dates = sorted(ts.keys(), reverse=True)[:30]
        candles = [{"date": d, **ok(ts[d])} for d in dates]
        return candles
    except Exception as e:
        print(f"[{symbol}] fetch error: {e}")
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
    if dow == 4 and 12 <= h <= 16:
        signals.append("⚡ Vendredi US session — possible NFP/données importantes")
        score += 20
    elif dow == 2:
        signals.append("📅 Mercredi — possible FOMC/inventaires")
        score += 10
    elif dow in (5, 6):
        signals.append("😴 Weekend — liquidité faible, éviter les nouvelles positions FX")
        score -= 10

    return max(0, score), signals

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

def compute_sl_tp(pa: dict, symbol: str):
    """
    Calcule SL et TP basés sur les niveaux clés et l'ATR approx.
    Retourne un dict avec sl, tp1, tp2, rr.
    """
    current = pa["current"]
    h20     = pa.get("h20", current * 1.01)
    l20     = pa.get("l20", current * 0.99)
    direction = pa["direction"]

    atr_approx = (h20 - l20) / 20  # ATR simplifié

    if direction == "BUY":
        sl  = round(l20 - atr_approx * 0.3, 5)
        tp1 = round(current + (current - sl) * 1.5, 5)
        tp2 = round(current + (current - sl) * 2.5, 5)
        risk = current - sl
    elif direction == "SELL":
        sl  = round(h20 + atr_approx * 0.3, 5)
        tp1 = round(current - (sl - current) * 1.5, 5)
        tp2 = round(current - (sl - current) * 2.5, 5)
        risk = sl - current
    else:
        return None

    rr = (tp1 - current) / risk if direction == "BUY" else (current - tp1) / risk
    return {"sl": sl, "tp1": tp1, "tp2": tp2, "rr": round(abs(rr), 2), "risk_pips": round(abs(risk * 10000), 1)}

# ─────────────────────────────────────────────
# RAPPORT COMPLET
# ─────────────────────────────────────────────

def build_full_report(symbol: str, macro_score: int, macro_signals: list, pa: dict) -> str:
    total = min(100, macro_score + pa["score"])

    if total >= 80:   quality = "🔥 EXCELLENT"
    elif total >= 60: quality = "✅ VALIDE"
    elif total >= 40: quality = "⚠️ FAIBLE"
    else:             quality = "❌ PAS DE SETUP"

    dir_label = {"BUY": "🟢 ACHAT", "SELL": "🔴 VENTE", "NEUTRE": "⚪ NEUTRE"}.get(pa["direction"], "⚪")

    sltp = compute_sl_tp(pa, symbol)

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
            f"━━━ 🎯 SL / TP ━━━",
            f"🛑 *Stop Loss :* `{sltp['sl']}`",
            f"🎯 *TP1 (1.5R) :* `{sltp['tp1']}`",
            f"🎯 *TP2 (2.5R) :* `{sltp['tp2']}`",
            f"📐 *R/R :* `{sltp['rr']}:1`",
            f"⚠️ *Risque :* `{sltp['risk_pips']} pips`",
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

    macro_score, macro_signals = analyze_macro()
    alerts = []

    for symbol in targets:
        print(f"Analyse {symbol}...")
        candles = fetch_candles(symbol)
        if not candles:
            send_telegram(f"⚠️ `{symbol}` — données indisponibles (limite API?)", cid)
            time.sleep(12)  # respecter limite AlphaVantage
            continue

        pa = analyze_price_action(candles)
        report = build_full_report(symbol, macro_score, macro_signals, pa)
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
        macro_score, macro_signals = analyze_macro()
        for symbol in ASSETS:
            candles = fetch_candles(symbol)
            if not candles:
                time.sleep(12)
                continue
            pa = analyze_price_action(candles)
            total = min(100, macro_score + pa["score"])
            print(f"  {symbol}: {total}/100")
            if total >= SCORE_THRESHOLD and pa["direction"] != "NEUTRE":
                report = build_full_report(symbol, macro_score, macro_signals, pa)
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
