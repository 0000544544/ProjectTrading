"""
╔══════════════════════════════════════════════════╗
║       GOLD SCALPING BOT — XAUUSD M5             ║
║       Bot Hybride IA + Indicateurs Techniques    ║
║       MODE SCALPING — Trades rapides             ║
╠══════════════════════════════════════════════════╣
║  COMMANDES TELEGRAM :                           ║
║  /start      — Démarrer / voir les commandes    ║
║  /status     — État du bot + trades ouverts     ║
║  /pnl        — PnL du jour                      ║
║  /trades     — Détail de tous les trades actifs ║
║  /closeall   — Fermer TOUS les trades           ║
║  /closetrade — Fermer un trade spécifique       ║
║  /be         — SL → Breakeven sur tous trades   ║
║  /pause      — Mettre le bot en pause           ║
║  /resume     — Reprendre le bot                 ║
║  /auto       — Mode automatique                 ║
║  /manuel     — Mode manuel (validation signal)  ║
║  /mode       — Voir le mode actuel              ║
║  /prix       — Prix actuel XAUUSD               ║
║  /reset      — Reset stats journalières         ║
╚══════════════════════════════════════════════════╝

PRÉREQUIS :
    pip install python-telegram-bot MetaTrader5 anthropic pandas numpy

LANCEMENT :
    python bot.py
"""

import asyncio
import logging
import time
from datetime import datetime, date
import numpy as np
import pandas as pd

# ── Imports conditionnels ─────────────────────────────────────────────────────
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️  MetaTrader5 non installé — mode simulation activé")

try:
    from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("❌ python-telegram-bot non installé — pip install python-telegram-bot")
    class Update: pass
    class InlineKeyboardButton: pass
    class InlineKeyboardMarkup:
        def __init__(self, *a, **kw): pass
    class Application:
        @staticmethod
        def builder(): return type('B', (), {'token': lambda self,t: self, 'build': lambda self: None})()
    class CommandHandler: pass
    class CallbackQueryHandler: pass
    class ContextTypes:
        DEFAULT_TYPE = None

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False
    print("❌ anthropic non installé — pip install anthropic")

# ── Config ────────────────────────────────────────────────────────────────────
from config import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    ANTHROPIC_API_KEY,
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    MODE, LOT_SIZE, MAX_SPREAD,
    SCORE_THRESHOLD, MAX_TRADES_PER_DAY,
    NB_TRADES_PER_SIGNAL, AUTO_BE_ON_TP1, AUTO_TRADE,
    SYMBOL, TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── État global ───────────────────────────────────────────────────────────────
# ── Paramètres par mode de trading ──────────────────────────────────────────
# SAFE      : SL large, TP conservateur, score élevé, cooldown long
# RISQUE    : Paramètres équilibrés (défaut)
# RISQUE+++ : SL serré, TP agressif, score bas, cooldown court
MODES_CONFIG = {
    "safe": {
        "sl_mult":   1.5,
        "tp1_mult":  1.2, "tp2_mult": 2.2, "tp3_mult": 3.5,
        "score_min": 55,    # score élevé = seulement les très bons setups
        "cooldown":  300,
        "label":     "🛡️ SAFE",
        "desc":      "SL large · Peu de trades · Meilleurs setups seulement",
    },
    "risque": {
        "sl_mult":   0.8,
        "tp1_mult":  1.0, "tp2_mult": 1.8, "tp3_mult": 2.8,
        "score_min": 42,    # remonté de 30 → 42 pour éviter les trades foireux
        "cooldown":  120,
        "label":     "⚡ RISQUÉ",
        "desc":      "Équilibré · Paramètres standards",
    },
    "risque+++": {
        "sl_mult":   0.5,
        "tp1_mult":  0.8, "tp2_mult": 1.5, "tp3_mult": 2.5,
        "score_min": 30,    # score bas mais filtre momentum actif
        "cooldown":  60,
        "label":     "🔥 RISQUÉ +++",
        "desc":      "SL serré · Beaucoup de trades · Plus de risque",
    },
}

state = {
    "trades_today":        0,
    "active_trades":       [],
    "last_signal_time":    0,
    "daily_pnl":           0.0,
    "total_signals_sent":  0,
    "last_trade_date":     None,
    "paused":              False,
    "trading_mode":        "risque",   # safe | risque | risque+++
    # Stats
    "wins":                0,
    "losses":              0,
    "best_trade":          0.0,
    "worst_trade":         0.0,
    "current_streak":      0,
    "history":             [],
    # Fermetures manuelles (ne comptent pas comme SL)
    "manual_closes":       set(),
    # Paramètres ajustables via Telegram
    "nb_trades":           3,
    "lot_size":            LOT_SIZE,
    "cooldown_override":   None,
    "last_sl_time":        0,       # timestamp du dernier SL
}

pending_signals = {}


# ════════════════════════════════════════════════════════════════════════════════
#  CONNEXION MT5
# ════════════════════════════════════════════════════════════════════════════════

def connect_mt5() -> bool:
    if not MT5_AVAILABLE:
        log.warning("MT5 non disponible — mode simulation")
        return False
    if not mt5.initialize():
        log.error(f"MT5 initialize() échoué : {mt5.last_error()}")
        return False
    if MT5_LOGIN:
        ok = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            log.error(f"MT5 login échoué : {mt5.last_error()}")
            return False
    log.info("✅ Connecté à MetaTrader5")
    return True


def disconnect_mt5():
    if MT5_AVAILABLE:
        mt5.shutdown()


# ════════════════════════════════════════════════════════════════════════════════
#  DONNÉES DE MARCHÉ
# ════════════════════════════════════════════════════════════════════════════════

def get_candles(timeframe: int, count: int = 100) -> pd.DataFrame | None:
    if not MT5_AVAILABLE:
        return _simulate_candles(count)
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        log.error("Impossible de récupérer les bougies MT5")
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={"open": "Open", "high": "High",
                        "low": "Low", "close": "Close",
                        "tick_volume": "Volume"}, inplace=True)
    return df


def _simulate_candles(count: int) -> pd.DataFrame:
    np.random.seed(int(time.time()) % 1000)
    base   = 2650.0
    closes = [base]
    for _ in range(count - 1):
        closes.append(closes[-1] + np.random.randn() * 1.5)
    closes = np.array(closes)
    return pd.DataFrame({
        "Open":   closes - np.abs(np.random.randn(count) * 0.5),
        "High":   closes + np.abs(np.random.randn(count) * 1.2),
        "Low":    closes - np.abs(np.random.randn(count) * 1.2),
        "Close":  closes,
        "Volume": np.random.randint(100, 2000, count).astype(float),
    })


def get_current_price() -> tuple[float, float]:
    if not MT5_AVAILABLE:
        return 2650.0, 2650.2
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return 0.0, 0.0
    return tick.bid, tick.ask


def get_spread() -> float:
    bid, ask = get_current_price()
    return round(ask - bid, 2)


def get_open_positions_mt5() -> list[dict]:
    """
    Récupère les positions ouvertes directement depuis MT5.
    Plus fiable que l'état interne du bot.
    """
    if not MT5_AVAILABLE:
        # En simulation, retourne les trades de l'état interne
        result = []
        for t in state.get("active_trades", []):
            if not t.get("closed"):
                result.append({
                    "ticket":    t.get("ticket", 0),
                    "direction": t.get("direction", "?"),
                    "volume":    LOT_SIZE,
                    "entry":     t.get("entry", 0),
                    "sl":        t.get("levels", {}).get("sl", 0),
                    "tp":        t.get("levels", {}).get("tp1", 0),
                    "pnl":       0.0,
                    "swap":      0.0,
                })
        return result
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return []
    return [{
        "ticket":    p.ticket,
        "direction": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
        "volume":    p.volume,
        "entry":     p.price_open,
        "sl":        p.sl,
        "tp":        p.tp,
        "pnl":       round(p.profit, 2),
        "swap":      round(p.swap, 2),
    } for p in positions]


# ════════════════════════════════════════════════════════════════════════════════
#  CALCUL DES INDICATEURS
# ════════════════════════════════════════════════════════════════════════════════

def compute_indicators(df: pd.DataFrame) -> dict:
    """
    Calcule tous les indicateurs techniques pour le scalping.

    RSI(14)          : force relative 0-100
    MACD(12,26,9)    : tendance et momentum
    EMA 9/20/50/200  : alignement multi-niveaux
    Bollinger(20,2)  : zones de surextension
    ATR(14)          : volatilité, base des TP/SL
    Stochastique(5,3): oscillateur scalping rapide
    Support/Résistance swing sur 50 bougies
    Volume spike
    Patterns bougie
    """
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]

    # RSI
    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rs    = gain / loss.replace(0, np.nan)
    rsi   = (100 - 100 / (1 + rs)).iloc[-1]

    # MACD
    ema12           = close.ewm(span=12, adjust=False).mean()
    ema26           = close.ewm(span=26, adjust=False).mean()
    macd_line       = ema12 - ema26
    signal_line     = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist       = macd_line - signal_line
    macd_cross_bull = (macd_line.iloc[-1] > signal_line.iloc[-1] and
                       macd_line.iloc[-2] <= signal_line.iloc[-2])
    macd_cross_bear = (macd_line.iloc[-1] < signal_line.iloc[-1] and
                       macd_line.iloc[-2] >= signal_line.iloc[-2])

    # EMA
    ema9   = close.ewm(span=9,   adjust=False).mean().iloc[-1]
    ema20  = close.ewm(span=20,  adjust=False).mean().iloc[-1]
    ema50  = close.ewm(span=50,  adjust=False).mean().iloc[-1]
    ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
    price  = close.iloc[-1]

    # Bollinger
    sma20  = close.rolling(20).mean()
    std20  = close.rolling(20).std()
    bb_up  = (sma20 + 2 * std20).iloc[-1]
    bb_low = (sma20 - 2 * std20).iloc[-1]
    bb_mid = sma20.iloc[-1]

    # ATR
    tr  = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(14).mean().iloc[-1]

    # Stochastique(5,3) — oscillateur principal scalping
    lowest_low   = low.rolling(5).min()
    highest_high = high.rolling(5).max()
    stoch_range  = (highest_high - lowest_low).replace(0, np.nan)
    stoch_k      = ((close - lowest_low) / stoch_range * 100).rolling(3).mean()
    stoch_d      = stoch_k.rolling(3).mean()
    sk = stoch_k.iloc[-1]
    sd = stoch_d.iloc[-1]
    stoch_cross_bull = (stoch_k.iloc[-1] > stoch_d.iloc[-1] and
                        stoch_k.iloc[-2] <= stoch_d.iloc[-2])
    stoch_cross_bear = (stoch_k.iloc[-1] < stoch_d.iloc[-1] and
                        stoch_k.iloc[-2] >= stoch_d.iloc[-2])

    # Support / Résistance
    recent_high     = high.iloc[-50:].max()
    recent_low      = low.iloc[-50:].min()
    near_support    = abs(price - recent_low)  < atr * 1.5
    near_resistance = abs(price - recent_high) < atr * 1.5

    # Volume
    avg_vol   = vol.rolling(20).mean().iloc[-1]
    vol_spike = vol.iloc[-1] > avg_vol * 1.3
    vol_ratio = round(vol.iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0

    pattern = detect_candle_pattern(df)

    # ── Détecteur de rebond sur creux/sommet ──────────────────────────────────
    # Un rebond = grosse chute/montée rapide + première bougie retournement + volume
    #
    # REBOND HAUSSIER (signal BUY) — 3 conditions :
    #   1. Grosse chute récente : la somme des 3 dernières bougies baissières
    #      dépasse 2×ATR → mouvement significatif, pas du bruit
    #   2. La bougie actuelle est haussière ET son volume dépasse la moyenne
    #      → le marché reprend de la vigueur
    #   3. Le prix est proche du support récent (creux des 50 dernières bougies)
    #      → rebond sur un niveau clé, pas en plein vide
    #
    # REBOND BAISSIER (signal SELL) — même logique inversée

    # Calcul chute/montée récente sur les 5 dernières bougies
    last5_close = close.iloc[-6:-1]   # 5 bougies avant la dernière
    last5_move  = last5_close.iloc[-1] - last5_close.iloc[0]   # déplacement total

    # Bougie actuelle
    cur_open  = df["Open"].iloc[-1]
    cur_close = close.iloc[-1]
    cur_vol   = vol.iloc[-1]
    cur_bull  = cur_close > cur_open   # bougie haussière
    cur_bear  = cur_close < cur_open   # bougie baissière
    cur_body  = abs(cur_close - cur_open)

    # Rebond haussier : chute avant + bougie haussière sur volume + proche support
    bounce_bull = (
        last5_move < -atr * 2.0          # grosse chute récente (2×ATR min)
        and cur_bull                      # bougie actuelle repart à la hausse
        and cur_body > atr * 0.2          # corps significatif (pas un micro rebond)
        and cur_vol > avg_vol * 1.2       # volume au-dessus de la moyenne
        and abs(price - recent_low) < atr * 2.5   # proche du support
    )

    # Rebond baissier : montée avant + bougie baissière sur volume + proche résistance
    bounce_bear = (
        last5_move > atr * 2.0            # grosse montée récente (2×ATR min)
        and cur_bear                      # bougie actuelle repart à la baisse
        and cur_body > atr * 0.2          # corps significatif
        and cur_vol > avg_vol * 1.2       # volume au-dessus de la moyenne
        and abs(price - recent_high) < atr * 2.5   # proche de la résistance
    )

    return {
        "rsi":              round(rsi, 2),
        "macd_line":        round(macd_line.iloc[-1], 4),
        "macd_signal":      round(signal_line.iloc[-1], 4),
        "macd_hist":        round(macd_hist.iloc[-1], 4),
        "macd_cross_bull":  macd_cross_bull,
        "macd_cross_bear":  macd_cross_bear,
        "ema9":             round(ema9, 2),
        "ema20":            round(ema20, 2),
        "ema50":            round(ema50, 2),
        "ema200":           round(ema200, 2),
        "price":            round(price, 2),
        "bb_up":            round(bb_up, 2),
        "bb_low":           round(bb_low, 2),
        "bb_mid":           round(bb_mid, 2),
        "atr":              round(atr, 2),
        "stoch_k":          round(sk, 2),
        "stoch_d":          round(sd, 2),
        "stoch_cross_bull": stoch_cross_bull,
        "stoch_cross_bear": stoch_cross_bear,
        "near_support":     near_support,
        "near_resistance":  near_resistance,
        "vol_spike":        vol_spike,
        "vol_ratio":        vol_ratio,
        "pattern":          pattern,
        "recent_high":      round(recent_high, 2),
        "recent_low":       round(recent_low, 2),
        "bounce_bull":      bounce_bull,
        "bounce_bear":      bounce_bear,
        "last5_move":       round(last5_move, 2),

    }


def detect_candle_pattern(df: pd.DataFrame) -> str:
    o, h, l, c = (df["Open"].iloc[-1], df["High"].iloc[-1],
                  df["Low"].iloc[-1],  df["Close"].iloc[-1])
    body        = abs(c - o)
    candle      = h - l
    if candle == 0:
        return "Doji"
    ratio       = body / candle
    lower_wick  = min(o, c) - l
    upper_wick  = h - max(o, c)
    if ratio < 0.1:                                                 return "Doji"
    if lower_wick > body*2 and upper_wick < body*0.5 and c > o:    return "Marteau (haussier)"
    if upper_wick > body*2 and lower_wick < body*0.5 and c < o:    return "Shooting Star (baissier)"
    prev_o, prev_c = df["Open"].iloc[-2], df["Close"].iloc[-2]
    if c > o and c > prev_o and o < prev_c and prev_c < prev_o:    return "Engulfing haussier"
    if c < o and c < prev_o and o > prev_c and prev_c > prev_o:    return "Engulfing baissier"
    if lower_wick > body * 3:                                       return "Pin Bar haussier"
    if upper_wick > body * 3:                                       return "Pin Bar baissier"
    return "Bougie neutre"


# ════════════════════════════════════════════════════════════════════════════════
#  DIRECTION SCALPING
# ════════════════════════════════════════════════════════════════════════════════

def determine_direction(ind: dict) -> str | None:
    """
    STRATÉGIE SCALPING — Deux modes de détection :

    MODE NORMAL (indicateurs alignés) :
      BUY  : RSI < 58 + prix > EMA9 + stoch favorable + MACD positif
      SELL : RSI > 42 + prix < EMA9 + stoch favorable + MACD négatif

    MODE REBOND (mouvement fort + retournement) :
      BUY  : grosse chute récente (2×ATR) + bougie haussière sur volume
             + proche support → signal même si MACD/stoch pas encore alignés
      SELL : grosse montée récente (2×ATR) + bougie baissière sur volume
             + proche résistance → même logique inversée

    Pourquoi le mode rebond ?
      Les indicateurs comme le MACD et le stoch ont un retard de 1-3 bougies.
      Sur un rebond fort, attendre qu'ils s'alignent = entrer trop tard.
      Le rebond détecte le retournement DÈS la première bougie de reprise,
      avant que les indicateurs confirment — c'est là que le prix est le meilleur.
    """
    price = ind["price"]
    ema9  = ind["ema9"]
    rsi   = ind["rsi"]
    sk    = ind["stoch_k"]
    sd    = ind["stoch_d"]

    # ── MODE REBOND (priorité — signal précoce) ───────────────────────────────
    # Condition supplémentaire : RSI pas en zone extrême opposée
    # (évite un rebond BUY quand RSI > 70 = encore suracheté)
    if ind["bounce_bull"] and rsi < 65:
        return "BUY"

    if ind["bounce_bear"] and rsi > 35:
        return "SELL"

    # ── MODE NORMAL (indicateurs alignés) ────────────────────────────────────
    if (rsi < 58
            and price > ema9
            and sk < 60 and (ind["stoch_cross_bull"] or sk > sd)
            and (ind["macd_hist"] > 0 or ind["macd_cross_bull"])):
        return "BUY"

    if (rsi > 42
            and price < ema9
            and sk > 40 and (ind["stoch_cross_bear"] or sk < sd)
            and (ind["macd_hist"] < 0 or ind["macd_cross_bear"])):
        return "SELL"

    return None


# ════════════════════════════════════════════════════════════════════════════════
#  SCORE SCALPING
# ════════════════════════════════════════════════════════════════════════════════

def compute_score(ind: dict, direction: str) -> tuple[int, list[str]]:
    """Score sur 100 — adapté scalping. Stoch > MACD > EMA9 > Pattern > Volume."""
    score   = 0
    reasons = []
    is_buy  = direction == "BUY"

    # RSI (10 pts)
    if is_buy and ind["rsi"] < 35:
        score += 10; reasons.append(f"RSI survendu ({ind['rsi']})")
    elif is_buy and ind["rsi"] < 50:
        score += 6;  reasons.append(f"RSI favorable BUY ({ind['rsi']})")
    elif not is_buy and ind["rsi"] > 65:
        score += 10; reasons.append(f"RSI suracheté ({ind['rsi']})")
    elif not is_buy and ind["rsi"] > 50:
        score += 6;  reasons.append(f"RSI favorable SELL ({ind['rsi']})")

    # Stochastique (20 pts)
    if is_buy:
        if ind["stoch_cross_bull"] and ind["stoch_k"] < 40:
            score += 20; reasons.append(f"Stoch croisement BUY zone basse ({ind['stoch_k']:.0f})")
        elif ind["stoch_cross_bull"]:
            score += 12; reasons.append(f"Stoch croisement BUY ({ind['stoch_k']:.0f})")
        elif ind["stoch_k"] < 30:
            score += 8;  reasons.append(f"Stoch survendu ({ind['stoch_k']:.0f})")
    else:
        if ind["stoch_cross_bear"] and ind["stoch_k"] > 60:
            score += 20; reasons.append(f"Stoch croisement SELL zone haute ({ind['stoch_k']:.0f})")
        elif ind["stoch_cross_bear"]:
            score += 12; reasons.append(f"Stoch croisement SELL ({ind['stoch_k']:.0f})")
        elif ind["stoch_k"] > 70:
            score += 8;  reasons.append(f"Stoch suracheté ({ind['stoch_k']:.0f})")

    # MACD (15 pts)
    if is_buy and ind["macd_cross_bull"]:
        score += 15; reasons.append("Croisement MACD haussier")
    elif is_buy and ind["macd_hist"] > 0:
        score += 7;  reasons.append("MACD momentum positif")
    elif not is_buy and ind["macd_cross_bear"]:
        score += 15; reasons.append("Croisement MACD baissier")
    elif not is_buy and ind["macd_hist"] < 0:
        score += 7;  reasons.append("MACD momentum négatif")

    # EMA9 (15 pts)
    p = ind["price"]
    if is_buy and p > ind["ema9"] > ind["ema20"]:
        score += 15; reasons.append("Prix > EMA9 > EMA20")
    elif is_buy and p > ind["ema9"]:
        score += 8;  reasons.append("Prix au-dessus EMA9")
    elif not is_buy and p < ind["ema9"] < ind["ema20"]:
        score += 15; reasons.append("Prix < EMA9 < EMA20")
    elif not is_buy and p < ind["ema9"]:
        score += 8;  reasons.append("Prix en-dessous EMA9")

    # Pattern (15 pts)
    bullish = ["Marteau (haussier)", "Engulfing haussier", "Pin Bar haussier"]
    bearish = ["Shooting Star (baissier)", "Engulfing baissier", "Pin Bar baissier"]
    if is_buy and ind["pattern"] in bullish:
        score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif not is_buy and ind["pattern"] in bearish:
        score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif ind["pattern"] == "Doji":
        score += 5;  reasons.append("Doji — indécision")

    # Support / Résistance (10 pts)
    if is_buy and ind["near_support"]:
        score += 10; reasons.append(f"Proche support ({ind['recent_low']})")
    elif not is_buy and ind["near_resistance"]:
        score += 10; reasons.append(f"Proche résistance ({ind['recent_high']})")

    # Volume (10 pts)
    if ind["vol_spike"]:
        score += 10; reasons.append(f"Volume spike (×{ind['vol_ratio']})")
    elif ind["vol_ratio"] > 1.0:
        score += 5;  reasons.append(f"Volume ×{ind['vol_ratio']}")

    # Bollinger (5 pts)
    if is_buy and ind["price"] <= ind["bb_low"]:
        score += 5; reasons.append("Prix sur Bollinger basse")
    elif not is_buy and ind["price"] >= ind["bb_up"]:
        score += 5; reasons.append("Prix sur Bollinger haute")

    # Rebond (bonus 25 pts) — signal fort, prioritaire
    if is_buy and ind.get("bounce_bull"):
        score += 25; reasons.append(f"Rebond haussier (chute {abs(ind.get('last5_move',0)):.1f} pts)")
    elif not is_buy and ind.get("bounce_bear"):
        score += 25; reasons.append(f"Rebond baissier (montée {abs(ind.get('last5_move',0)):.1f} pts)")

    return min(score, 100), reasons


# ════════════════════════════════════════════════════════════════════════════════
#  TP / SL SCALPING
# ════════════════════════════════════════════════════════════════════════════════

def calculate_tp_sl(direction: str, entry: float, atr: float) -> dict:
    """TP/SL calculés selon le mode actif (safe / risque / risque+++)."""
    m = MODES_CONFIG[state.get("trading_mode", "risque")]
    if direction == "BUY":
        return {
            "sl":  round(entry - atr * m["sl_mult"],   2),
            "tp1": round(entry + atr * m["tp1_mult"],  2),
            "tp2": round(entry + atr * m["tp2_mult"],  2),
            "tp3": round(entry + atr * m["tp3_mult"],  2),
        }
    else:
        return {
            "sl":  round(entry + atr * m["sl_mult"],   2),
            "tp1": round(entry - atr * m["tp1_mult"],  2),
            "tp2": round(entry - atr * m["tp2_mult"],  2),
            "tp3": round(entry - atr * m["tp3_mult"],  2),
        }


# ════════════════════════════════════════════════════════════════════════════════
#  ANALYSE IA (Claude)
# ════════════════════════════════════════════════════════════════════════════════

async def ai_confirm_signal(ind: dict, direction: str, score: int,
                             reasons: list[str], df_m5: pd.DataFrame,
                             df_m15: pd.DataFrame, df_h1: pd.DataFrame) -> tuple[bool, str]:
    """Claude valide le contexte global pour éviter les trades contre tendance."""
    if not ANTHROPIC_AVAILABLE:
        return True, "Auto-confirmé (API non disponible)"

    client  = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    last_5  = df_m5.tail(5)[["Open", "High", "Low", "Close", "Volume"]].to_string(index=False)
    h1_ema  = df_h1["Close"].ewm(span=20).mean().iloc[-1]
    h1_dir  = "HAUSSIER" if df_h1["Close"].iloc[-1] > h1_ema else "BAISSIER"
    m15_ind = compute_indicators(df_m15.tail(50).reset_index(drop=True))

    prompt = f"""Tu es expert en scalping GOLD (XAUUSD) M5.

SIGNAL : {direction} | SCORE : {score}/100
RAISONS : {', '.join(reasons)}

INDICATEURS M5 :
- Prix : {ind['price']} | ATR : {ind['atr']}
- RSI(14) : {ind['rsi']} | Stoch K/D : {ind['stoch_k']}/{ind['stoch_d']}
- MACD hist : {ind['macd_hist']} | Cross bull: {ind['macd_cross_bull']} | Cross bear: {ind['macd_cross_bear']}
- EMA9: {ind['ema9']} | EMA20: {ind['ema20']} | EMA50: {ind['ema50']}
- Bollinger : [{ind['bb_low']} — {ind['bb_mid']} — {ind['bb_up']}]
- Volume : {ind['vol_ratio']}x | Pattern : {ind['pattern']}
- Proche support : {ind['near_support']} ({ind['recent_low']})
- Proche résistance : {ind['near_resistance']} ({ind['recent_high']})

CONTEXTE :
- Tendance H1 : {h1_dir}
- RSI M15 : {round(m15_ind['rsi'], 1)} | EMA9 M15 : {m15_ind['ema9']}

DERNIÈRES BOUGIES M5 :
{last_5}

Réponds uniquement : CONFIRME ou REFUSE — [explication courte, 2 lignes max]"""

    try:
        response  = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}]
        )
        text      = response.content[0].text.strip()
        confirmed = text.upper().startswith("CONFIRME")
        return confirmed, text
    except Exception as e:
        log.error(f"Erreur API Anthropic : {e}")
        return True, "Auto-confirmé (erreur API)"


# ════════════════════════════════════════════════════════════════════════════════
#  EXÉCUTION MT5
# ════════════════════════════════════════════════════════════════════════════════

def place_orders(direction: str, entry: float, levels: dict,
                 lot: float = None, nb: int = None) -> list[dict]:
    """
    Place nb trades avec des TP différents :
      Trade 1 → TP1 | Trade 2 → TP2 | Trade 3 → TP3
    nb et lot viennent de l'état global (modifiables via Telegram).
    """
    lot = lot or state.get("lot_size", LOT_SIZE)
    nb  = nb  or state.get("nb_trades", 3)
    nb  = max(1, min(3, nb))   # clamp entre 1 et 3
    tps    = [levels["tp1"], levels["tp2"], levels["tp3"]]
    labels = ["TP1 (rapide)", "TP2 (moyen)", "TP3 (étendu)"]
    results = []
    for i in range(nb):
        res = _send_order(direction, entry, tps[i], levels["sl"], lot)
        if res:
            results.append({**res, "tp_target": i + 1})
            log.info(f"✅ Trade {i+1}/{nb} — {labels[i]} @ {tps[i]} lot={lot}")
        else:
            log.error(f"❌ Trade {i+1}/{nb} échoué — {labels[i]}")
    return results


def _send_order(direction: str, entry: float, tp: float,
                sl: float, lot: float) -> dict | None:
    if not MT5_AVAILABLE:
        ticket = int(time.time() * 1000) % 999999
        log.info(f"[SIM] {direction} @ {entry} TP:{tp} SL:{sl}")
        return {"ticket": ticket, "simulated": True,
                "direction": direction, "entry": entry,
                "tp": tp, "sl": sl, "lot": lot}

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick       = mt5.symbol_info_tick(SYMBOL)
    price      = tick.ask if direction == "BUY" else tick.bid

    result = mt5.order_send({
        "action":        mt5.TRADE_ACTION_DEAL,
        "symbol":        SYMBOL,
        "volume":        lot,
        "type":          order_type,
        "price":         price,
        "sl":            sl,
        "tp":            tp,
        "deviation":     20,
        "magic":         20250101,
        "comment":       "GoldScalpBot",
        "type_time":     mt5.ORDER_TIME_GTC,
        "type_filling":  mt5.ORDER_FILLING_IOC,
    })
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Ordre échoué : {result.retcode} — {result.comment}")
        return None
    return {"ticket": result.order, "direction": direction,
            "entry": price, "tp": tp, "sl": sl, "lot": lot}


def close_trade(ticket: int) -> bool:
    """Ferme un trade par son ticket."""
    if not MT5_AVAILABLE:
        log.info(f"[SIM] Close #{ticket}")
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"Position #{ticket} introuvable")
        return False
    pos             = positions[0]
    direction_close = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick            = mt5.symbol_info_tick(SYMBOL)
    price           = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    result = mt5.order_send({
        "action":        mt5.TRADE_ACTION_DEAL,
        "symbol":        SYMBOL,
        "volume":        pos.volume,
        "type":          direction_close,
        "position":      ticket,
        "price":         price,
        "deviation":     20,
        "magic":         20250101,
        "comment":       "GoldScalpBot Close",
        "type_time":     mt5.ORDER_TIME_GTC,
        "type_filling":  mt5.ORDER_FILLING_IOC,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE


def close_all_trades() -> tuple[int, int]:
    """Ferme toutes les positions ouvertes sur SYMBOL."""
    if not MT5_AVAILABLE:
        nb = len([t for t in state.get("active_trades", []) if not t.get("closed")])
        state["active_trades"] = []
        return nb, 0
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return 0, 0
    ok = err = 0
    for pos in positions:
        if close_trade(pos.ticket):
            ok += 1
        else:
            err += 1
    state["active_trades"] = []
    return ok, err


def move_sl_to_be(ticket: int, entry: float = 0) -> bool:
    """
    Déplace le SL au prix d'entrée (Breakeven).
    IMPORTANT : récupère le TP actuel depuis MT5 et le préserve.
    Sans ça, TRADE_ACTION_SLTP efface le TP → les trades perdent leur objectif.
    """
    if not MT5_AVAILABLE:
        log.info(f"[SIM] SL→BE @ {entry} pour #{ticket}")
        return True

    # Récupère la position pour avoir entry ET tp actuels
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        log.warning(f"SL BE : position #{ticket} introuvable")
        return False
    pos = positions[0]
    if entry == 0:
        entry = pos.price_open

    # Inclure le TP existant pour ne pas l'effacer
    request = {
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       entry,
        "tp":       pos.tp,   # ← CRITIQUE : préserve le TP existant
    }
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"SL BE échoué #{ticket} : {result.retcode} — {result.comment}")
        return False
    log.info(f"✅ SL→BE @ {entry} TP conservé @ {pos.tp} pour #{ticket}")
    return True


def move_sl_to_level(ticket: int, new_sl: float) -> bool:
    """
    Déplace le SL à un niveau précis en préservant le TP.
    Utilisé pour passer le SL au TP1 quand TP2 est atteint.
    """
    if not MT5_AVAILABLE:
        log.info(f"[SIM] SL→{new_sl} pour #{ticket}")
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos    = positions[0]
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos.tp,   # ← préserve le TP
    })
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"SL move échoué #{ticket} : {result.retcode}")
        return False
    log.info(f"✅ SL→{new_sl} TP conservé @ {pos.tp} pour #{ticket}")
    return True


def move_all_sl_to_be() -> tuple[int, int]:
    """Passe le SL au breakeven sur toutes les positions ouvertes."""
    if not MT5_AVAILABLE:
        count = len([t for t in state.get("active_trades", []) if not t.get("closed")])
        return count, 0
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return 0, 0
    ok = err = 0
    for pos in positions:
        if move_sl_to_be(pos.ticket, pos.price_open):
            ok += 1
        else:
            err += 1
    return ok, err


# ════════════════════════════════════════════════════════════════════════════════
#  MESSAGES TELEGRAM
# ════════════════════════════════════════════════════════════════════════════════

def build_signal_message(direction: str, ind: dict, levels: dict,
                          score: int, reasons: list[str], ai_comment: str) -> tuple:
    """Message de signal — style Station X, propre et lisible sur mobile."""
    emoji    = "🟢" if direction == "BUY" else "🔴"
    emoji_sl = "🔴" if direction == "BUY" else "🟢"
    rr       = round(abs(levels["tp2"] - ind["price"]) / abs(ind["price"] - levels["sl"]), 2)
    now      = datetime.now().strftime("%H:%M")
    top3     = reasons[:3]

    text = (
        f"{emoji} *J'ACHÈTE XAUUSD* à `{ind['price']}`\n"
        if direction == "BUY" else
        f"{emoji} *JE VENDS XAUUSD* à `{ind['price']}`\n"
    ) + (
        f"\n"
        f"🎯 TP1 : `{levels['tp1']}`\n"
        f"🎯 TP2 : `{levels['tp2']}`\n"
        f"🎯 TP3 : `{levels['tp3']}`\n"
        f"\n"
        f"🔒 SL : `{levels['sl']}`\n"
        f"\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 Score : `{score}/100` | R/R : `1:{rr}` | {now}\n"
        f"📉 RSI `{ind['rsi']}` · Stoch `{ind['stoch_k']:.0f}` · ATR `{ind['atr']}`\n"
    )
    if top3:
        text += "\n" + "\n".join(f"  ✅ {r}" for r in top3)

    signal_id = str(int(time.time()))
    pending_signals[signal_id] = {
        "direction": direction, "entry": ind["price"],
        "levels": levels, "score": score,
    }
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ PLACER L'ORDRE", callback_data=f"place_{signal_id}"),
        InlineKeyboardButton("❌ IGNORER",         callback_data=f"ignore_{signal_id}"),
    ]])
    return text, keyboard


def build_tp_message(tp_num: int, price: float, ticket: int, entry: float) -> tuple:
    """Message TP atteint — gros et visible, avec boutons d'action."""
    pips     = round(abs(price - entry), 2)
    pips_str = f"+{pips}"
    stars    = "🔥" * tp_num   # TP1=🔥 TP2=🔥🔥 TP3=🔥🔥🔥

    text = (
        f"{stars} *TP{tp_num} TOUCHÉ !* {stars}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 *{pips_str} PIPS* ✅\n"
        f"Prix de sortie : `{price}`\n"
        f"Ticket : #{ticket}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Que veux-tu faire ?"
    )
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔒 SL → Breakeven",   callback_data=f"be_{ticket}"),
            InlineKeyboardButton("🚪 Close maintenant", callback_data=f"close_{ticket}"),
        ],
        [InlineKeyboardButton("▶️ Laisser courir → TP suivant", callback_data=f"hold_{ticket}")]
    ])
    return text, keyboard


# ════════════════════════════════════════════════════════════════════════════════
#  COMMANDES TELEGRAM
# ════════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    mode = "🤖 AUTO" if AUTO_TRADE else "👆 Manuel"
    await update.message.reply_text(
        "⚡ *Gold Scalping Bot*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *INFOS*\n"
        "/status     — État du bot\n"
        "/prix       — Prix actuel XAUUSD\n"
        "/pnl        — PnL du jour\n"
        "/stats      — Dashboard complet (winrate, série, historique)\n"
        "/trades     — Trades ouverts\n"
        "/mode       — Mode actuel\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "🎮 *CONTRÔLE TRADES*\n"
        "/be         — SL → Breakeven (tous)\n"
        "/closeall   — Fermer TOUS les trades\n"
        "/closetrade — Fermer 1 trade (ex: /closetrade 123456)\n"
        "/settrades    — Nb positions par signal (1/2/3)\n"
        "/setlot       — Taille du lot par trade\n"
        "/setcooldown  — Cooldown entre trades (ex: /setcooldown 30)\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "⚙️ *PARAMÈTRES*\n"
        "/mode       — Mode actuel + changer\n"
        "/safe       — 🛡️ Mode Safe\n"
        "/risque     — ⚡ Mode Risqué\n"
        "/risqueppp  — 🔥 Mode Risqué +++\n"
        "/auto       — Trading automatique\n"
        "/manuel     — Trading manuel\n"
        "/pause      — Mettre en pause\n"
        "/resume     — Reprendre\n"
        "/reset      — Reset stats du jour\n\n"
        f"Mode actuel : *{mode}*",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bid, ask   = get_current_price()
    spread     = round(ask - bid, 2)
    paused_txt = "⏸ EN PAUSE" if state["paused"] else "▶️ Actif"
    positions  = get_open_positions_mt5()

    if positions:
        lines = []
        for p in positions:
            sign = "+" if p["pnl"] >= 0 else ""
            lines.append(
                f"  #{p['ticket']} {p['direction']} @ {p['entry']} | {sign}{p['pnl']}$"
            )
        trade_info = "\n".join(lines)
    else:
        trade_info = "  Aucun trade ouvert"

    await update.message.reply_text(
        f"📊 *Status Gold Scalp Bot*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"État       : {paused_txt}\n"
        f"Mode trading : `{MODES_CONFIG[state.get('trading_mode','risque')]['label']}`\n"
        f"Exécution  : `{'🤖 AUTO' if AUTO_TRADE else '👆 Manuel'}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Prix    : `{bid}` (spread: {spread})\n"
        f"📈 Trades  : {state['trades_today']}/{MAX_TRADES_PER_DAY} aujourd'hui\n"
        f"💵 PnL     : `{state['daily_pnl']:.2f}$`\n"
        f"📨 Signaux : {state['total_signals_sent']}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Positions/signal : `{state.get('nb_trades', 3)}` | Lot : `{state.get('lot_size', LOT_SIZE)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Positions :*\n{trade_info}",
        parse_mode="Markdown"
    )


async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    bid, ask = get_current_price()
    spread   = round(ask - bid, 2)
    await update.message.reply_text(
        f"💰 *XAUUSD*\n"
        f"Bid : `{bid}`\n"
        f"Ask : `{ask}`\n"
        f"Spread : `{spread}`",
        parse_mode="Markdown"
    )


async def cmd_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # PnL réalisé depuis MT5 par magic number
    mt5_stats = await _get_mt5_stats_by_magic(MAGIC_NUMBER)
    positions = get_open_positions_mt5()
    live_pnl  = round(sum(p["pnl"] for p in positions), 2)
    total     = round(mt5_stats["pnl"] + live_pnl, 2)
    sign      = "+" if total >= 0 else ""
    await update.message.reply_text(
        f"💰 *PnL du jour — Bot principal*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"PnL réalisé  : `{mt5_stats['pnl']:+.2f}$`\n"
        f"PnL en cours : `{live_pnl:+.2f}$`\n"
        f"Total        : `{sign}{total}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Magic : `{MAGIC_NUMBER}` | Séquences : {mt5_stats['wins']+mt5_stats['losses']}",
        parse_mode="Markdown"
    )


async def cmd_trades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche toutes les positions ouvertes avec boutons BE/Close par ticket."""
    positions = get_open_positions_mt5()
    if not positions:
        await update.message.reply_text("✅ Aucune position ouverte.", parse_mode="Markdown")
        return

    lines = [f"📋 *{len(positions)} position(s) ouverte(s)*\n━━━━━━━━━━━━━━━━━━━━━"]
    for p in positions:
        emoji = "🟢" if p["direction"] == "BUY" else "🔴"
        sign  = "+" if p["pnl"] >= 0 else ""
        lines.append(
            f"{emoji} *#{p['ticket']}* — {p['direction']}\n"
            f"  Entrée : `{p['entry']}` | Lot : `{p['volume']}`\n"
            f"  SL : `{p['sl']}` | TP : `{p['tp']}`\n"
            f"  PnL : `{sign}{p['pnl']}$`"
        )

    buttons = []
    for p in positions:
        buttons.append([
            InlineKeyboardButton(f"🔒 BE #{p['ticket']}",    callback_data=f"be_{p['ticket']}"),
            InlineKeyboardButton(f"🚪 Close #{p['ticket']}", callback_data=f"close_{p['ticket']}"),
        ])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


async def cmd_closeall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ferme tous les trades — demande confirmation d'abord."""
    positions = get_open_positions_mt5()
    if not positions:
        await update.message.reply_text("✅ Aucune position à fermer.", parse_mode="Markdown")
        return

    total_pnl = sum(p["pnl"] for p in positions)
    sign      = "+" if total_pnl >= 0 else ""
    keyboard  = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⚠️ OUI — Fermer {len(positions)} trade(s)", callback_data="confirm_closeall"),
        InlineKeyboardButton("❌ Annuler", callback_data="cancel_closeall"),
    ]])
    await update.message.reply_text(
        f"⚠️ *Fermer TOUS les trades ?*\n"
        f"{len(positions)} position(s) | PnL actuel : `{sign}{total_pnl:.2f}$`",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_closetrade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ferme un trade spécifique : /closetrade 123456"""
    args = ctx.args
    if not args:
        await update.message.reply_text(
            "Usage : `/closetrade <ticket>`\nEx: `/closetrade 123456`",
            parse_mode="Markdown"
        )
        return
    try:
        ticket = int(args[0])
    except ValueError:
        await update.message.reply_text("❌ Ticket invalide.", parse_mode="Markdown")
        return

    # Marque comme fermeture manuelle AVANT de fermer
    state["manual_closes"].add(ticket)
    ok = close_trade(ticket)
    if ok:
        for t in state.get("active_trades", []):
            if t.get("ticket") == ticket:
                t["closed"] = True
                t["result"] = "MANUEL"
        await update.message.reply_text(
            f"🚪 *Trade #{ticket} fermé manuellement.*\n_Non compté dans les stats_",
            parse_mode="Markdown"
        )
    else:
        state["manual_closes"].discard(ticket)
        await update.message.reply_text(
            f"❌ Impossible de fermer #{ticket}.\nVérifie que le ticket existe dans MT5.",
            parse_mode="Markdown"
        )


async def cmd_be(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Déplace le SL au prix d'entrée (Breakeven) sur tous les trades.
    Ne ferme AUCUN trade — juste modifie le SL dans MT5.
    """
    positions = get_open_positions_mt5()
    if not positions:
        await update.message.reply_text("✅ Aucune position ouverte.", parse_mode="Markdown")
        return

    ok = err = 0
    details = []
    for p in positions:
        if move_sl_to_be(p["ticket"], p["entry"]):
            ok += 1
            details.append(f"  ✅ #{p['ticket']} — SL → `{p['entry']}`")
        else:
            err += 1
            details.append(f"  ❌ #{p['ticket']} — erreur MT5")

    msg = f"🔒 *Breakeven appliqué sur {ok} trade(s)*\n"
    msg += "\n".join(details)
    if err > 0:
        msg += f"\n\n❌ {err} erreur(s) — vérifie MT5"
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Met le bot en pause (stop les nouveaux signaux, garde les trades ouverts)."""
    state["paused"] = True
    log.info("⏸ Bot mis en pause")
    await update.message.reply_text(
        "⏸ *Bot en pause.*\n"
        "Plus aucun signal ne sera généré.\n"
        "Les trades ouverts continuent.\n\n"
        "_/resume pour reprendre_",
        parse_mode="Markdown"
    )


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Reprend le bot après une pause."""
    state["paused"] = False
    log.info("▶️ Bot repris")
    await update.message.reply_text(
        "▶️ *Bot repris !*\nScan du marché en cours...",
        parse_mode="Markdown"
    )


async def cmd_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Affiche le mode actuel et permet de changer via boutons."""
    m      = MODES_CONFIG[state.get("trading_mode", "risque")]
    auto   = "🤖 AUTO" if AUTO_TRADE else "👆 Manuel"
    paused = " | ⏸ PAUSE" if state["paused"] else ""
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🛡️ SAFE",       callback_data="mode_safe"),
        InlineKeyboardButton("⚡ RISQUÉ",      callback_data="mode_risque"),
        InlineKeyboardButton("🔥 RISQUÉ +++",  callback_data="mode_risqueppp"),
    ]])
    await update.message.reply_text(
        f"⚙️ *Mode actuel :* {m['label']}{paused}\n"
        f"Trading : {auto}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_{m['desc']}_\n"
        f"SL : `{m['sl_mult']}×ATR` | TP1 : `{m['tp1_mult']}×ATR`\n"
        f"Score min : `{m['score_min']}` | Cooldown : `{m['cooldown']}s`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Positions/signal : `{state.get('nb_trades',3)}` | Lot : `{state.get('lot_size', LOT_SIZE)}`\n"
        f"Cooldown : `{state['cooldown_override'] if state['cooldown_override'] is not None else str(m['cooldown'])+'s (auto)'}` \n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Choisis un mode :",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


async def cmd_safe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["trading_mode"] = "safe"
    m = MODES_CONFIG["safe"]
    log.info("Mode SAFE activé")
    await update.message.reply_text(
        f"🛡️ *Mode SAFE activé !*\n"
        f"SL large · Score élevé · Peu de trades\n"
        f"SL={m['sl_mult']}×ATR | Score min={m['score_min']} | Cooldown={m['cooldown']}s",
        parse_mode="Markdown"
    )


async def cmd_risque(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["trading_mode"] = "risque"
    m = MODES_CONFIG["risque"]
    log.info("Mode RISQUÉ activé")
    await update.message.reply_text(
        f"⚡ *Mode RISQUÉ activé !*\n"
        f"Équilibré · Paramètres standards\n"
        f"SL={m['sl_mult']}×ATR | Score min={m['score_min']} | Cooldown={m['cooldown']}s",
        parse_mode="Markdown"
    )


async def cmd_risqueppp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["trading_mode"] = "risque+++"
    m = MODES_CONFIG["risque+++"]
    log.info("Mode RISQUÉ+++ activé")
    await update.message.reply_text(
        f"🔥 *Mode RISQUÉ +++ activé !*\n"
        f"⚠️ SL serré · Beaucoup de trades · Plus de risque\n"
        f"SL={m['sl_mult']}×ATR | Score min={m['score_min']} | Cooldown={m['cooldown']}s",
        parse_mode="Markdown"
    )


async def cmd_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global AUTO_TRADE
    AUTO_TRADE = True
    log.info("Mode AUTO activé")
    await update.message.reply_text(
        "🤖 *Mode AUTO activé !*\nLe bot place les trades sans confirmation.\n_/manuel pour changer_",
        parse_mode="Markdown"
    )


async def cmd_manuel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global AUTO_TRADE
    AUTO_TRADE = False
    log.info("Mode MANUEL activé")
    await update.message.reply_text(
        "👆 *Mode MANUEL activé !*\nTu valideras chaque signal.\n_/auto pour changer_",
        parse_mode="Markdown"
    )



async def _get_mt5_stats_by_magic(magic: int) -> dict:
    """
    Lit l'historique MT5 filtré par magic number.
    Retourne PnL réalisé du jour, wins, losses, séquences.
    """
    if not MT5_AVAILABLE:
        return {"pnl": 0.0, "wins": 0, "losses": 0, "sequences": [], "best": 0.0, "worst": 0.0}

    try:
        from datetime import timedelta
        today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        deals = mt5.history_deals_get(today_start, datetime.now())
        if not deals:
            return {"pnl": 0.0, "wins": 0, "losses": 0, "sequences": [], "best": 0.0, "worst": 0.0}

        # Filtre par magic number et garde seulement les clôtures (entry=1)
        my_deals = [d for d in deals if d.magic == magic and d.entry == 1]

        # Groupe par position_id pour compter les séquences (pas les trades individuels)
        from collections import defaultdict
        sequences = defaultdict(list)
        for d in my_deals:
            sequences[d.position_id].append(d.profit)

        total_pnl = 0.0
        wins = losses = 0
        best = worst = 0.0
        seq_list = []

        for pos_id, profits in sequences.items():
            seq_pnl = round(sum(profits), 2)
            total_pnl += seq_pnl
            if seq_pnl >= 0:
                wins += 1
            else:
                losses += 1
            if seq_pnl > best:   best  = seq_pnl
            if seq_pnl < worst:  worst = seq_pnl
            seq_list.append(seq_pnl)

        return {
            "pnl":       round(total_pnl, 2),
            "wins":      wins,
            "losses":    losses,
            "sequences": seq_list[-5:],   # 5 dernières
            "best":      best,
            "worst":     worst,
        }
    except Exception as e:
        log.error(f"_get_mt5_stats_by_magic erreur : {e}")
        return {"pnl": 0.0, "wins": 0, "losses": 0, "sequences": [], "best": 0.0, "worst": 0.0}


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Stats du jour basées sur le magic number MT5 — données réelles."""
    # Stats depuis MT5 par magic number
    mt5_stats = await _get_mt5_stats_by_magic(MAGIC_NUMBER)

    wins   = mt5_stats["wins"]
    losses = mt5_stats["losses"]
    total  = wins + losses
    wr     = round(wins / total * 100) if total > 0 else 0
    pnl    = mt5_stats["pnl"]

    # PnL en cours (positions ouvertes)
    positions = get_open_positions_mt5()
    live_pnl  = round(sum(p["pnl"] for p in
                          [p for p in positions]), 2)
    total_pnl = round(pnl + live_pnl, 2)

    filled     = round(wr / 10)
    bar        = "🟩" * filled + "⬜" * (10 - filled)
    pnl_sign   = "+" if total_pnl >= 0 else ""
    best_sign  = "+" if mt5_stats["best"] >= 0 else ""

    # Série en cours
    streak = state["current_streak"]
    if streak > 0:   streak_txt = f"🔥 {streak} win(s) d'affilée"
    elif streak < 0: streak_txt = f"❄️ {abs(streak)} perte(s) d'affilée"
    else:            streak_txt = "➖ Pas encore de série"

    # Dernières séquences
    last_trades = ""
    for seq_pnl in reversed(mt5_stats["sequences"]):
        icon = "✅" if seq_pnl >= 0 else "❌"
        sign = "+" if seq_pnl >= 0 else ""
        last_trades += f"  {icon} `{sign}{seq_pnl}$`\n"
    if not last_trades:
        last_trades = "  Aucun trade fermé aujourd'hui\n"

    await update.message.reply_text(
        f"📊 *STATS DU JOUR — Bot principal*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 PnL total   : `{pnl_sign}{total_pnl}$`\n"
        f"   Réalisé     : `{pnl:+.2f}$`\n"
        f"   En cours    : `{live_pnl:+.2f}$`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Séquences   : {total} ({wins}W / {losses}L)\n"
        f"📈 Winrate     : `{wr}%`\n"
        f"{bar}\n"
        f"🏆 Meilleur    : `{best_sign}{mt5_stats['best']}$`\n"
        f"💀 Pire        : `{mt5_stats['worst']}$`\n"
        f"⚡ Série       : {streak_txt}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*5 dernières séquences :*\n"
        f"{last_trades}",
        parse_mode="Markdown"
    )

async def cmd_settrades(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Change le nombre de positions par signal : /settrades 1|2|3
    Ex: /settrades 2 → 2 trades par signal (TP1 + TP2)
    """
    args = ctx.args
    if not args:
        nb = state.get("nb_trades", 3)
        await update.message.reply_text(
            f"📊 *Positions par signal : `{nb}`*\n\n"
            f"Pour changer : `/settrades 1`, `/settrades 2`, `/settrades 3`\n"
            f"• 1 trade → TP1 seulement\n"
            f"• 2 trades → TP1 + TP2\n"
            f"• 3 trades → TP1 + TP2 + TP3",
            parse_mode="Markdown"
        )
        return
    try:
        nb = int(args[0])
        if nb not in (1, 2, 3):
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Valeur invalide. Utilise 1, 2 ou 3.", parse_mode="Markdown")
        return
    state["nb_trades"] = nb
    tps = {1: "TP1 seulement", 2: "TP1 + TP2", 3: "TP1 + TP2 + TP3"}
    log.info(f"nb_trades → {nb}")
    await update.message.reply_text(
        f"✅ *{nb} position(s) par signal*\n_{tps[nb]}_",
        parse_mode="Markdown"
    )


async def cmd_setlot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Change la taille du lot par trade : /setlot 0.01
    Ex: /setlot 0.05
    """
    args = ctx.args
    if not args:
        lot = state.get("lot_size", LOT_SIZE)
        await update.message.reply_text(
            f"📊 *Lot actuel : `{lot}`*\n\n"
            f"Pour changer : `/setlot 0.01` (micro) · `/setlot 0.05` · `/setlot 0.1` (mini)\n"
            f"⚠️ Sur compte démo 100k, max recommandé : `0.1`",
            parse_mode="Markdown"
        )
        return
    try:
        lot = float(args[0])
        if lot <= 0 or lot > 10:
            raise ValueError
    except ValueError:
        await update.message.reply_text("❌ Valeur invalide. Ex: `/setlot 0.01`", parse_mode="Markdown")
        return
    old_lot = state.get("lot_size", LOT_SIZE)
    state["lot_size"] = lot
    log.info(f"lot_size : {old_lot} → {lot}")
    await update.message.reply_text(
        f"✅ *Lot changé : `{old_lot}` → `{lot}`*\n"
        f"_Appliqué dès le prochain signal_",
        parse_mode="Markdown"
    )


async def cmd_setcooldown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Change le cooldown entre trades : /setcooldown 60
    /setcooldown 0   → pas de cooldown du tout
    /setcooldown     → affiche le cooldown actuel et remet celui du mode
    /setcooldown auto → remet le cooldown du mode actif
    """
    args = ctx.args
    m    = MODES_CONFIG[state.get("trading_mode", "risque")]

    if not args:
        current = state["cooldown_override"]
        mode_cd = m["cooldown"]
        if current is None:
            txt = f"⏱️ Cooldown actuel : `{mode_cd}s` (mode {m['label']})"
        else:
            txt = f"⏱️ Cooldown actuel : `{current}s` (override manuel)\nMode {m['label']} = `{mode_cd}s`"
        txt += "\n\nUsage : `/setcooldown 30` | `/setcooldown 0` | `/setcooldown auto`"
        await update.message.reply_text(txt, parse_mode="Markdown")
        return

    if args[0].lower() == "auto":
        state["cooldown_override"] = None
        await update.message.reply_text(
            f"✅ Cooldown remis automatique : `{m['cooldown']}s` (mode {m['label']})",
            parse_mode="Markdown"
        )
        return

    try:
        cd = int(args[0])
        if cd < 0:
            raise ValueError
        state["cooldown_override"] = cd
        log.info(f"Cooldown override → {cd}s")
        if cd == 0:
            await update.message.reply_text(
                "⚡ *Cooldown désactivé !*\n_Le bot peut trader en continu_\n⚠️ Risque de surtrading",
                parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(
                f"✅ *Cooldown : `{cd}s`*\n_Appliqué immédiatement_",
                parse_mode="Markdown"
            )
    except ValueError:
        await update.message.reply_text(
            "❌ Valeur invalide.\nEx: `/setcooldown 30` | `/setcooldown 0` | `/setcooldown auto`",
            parse_mode="Markdown"
        )


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["trades_today"]       = 0
    state["daily_pnl"]          = 0.0
    state["total_signals_sent"] = 0
    state["wins"]               = 0
    state["losses"]             = 0
    state["best_trade"]         = 0.0
    state["worst_trade"]        = 0.0
    state["current_streak"]     = 0
    state["history"]            = []
    state["manual_closes"]      = set()
    log.info("Reset stats journalières")
    await update.message.reply_text("🔄 *Stats remises à zéro.*", parse_mode="Markdown")


# ════════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER (boutons inline)
# ════════════════════════════════════════════════════════════════════════════════

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    # Valider signal (mode manuel)
    if data.startswith("place_"):
        signal_id = data.split("_", 1)[1]
        sig = pending_signals.get(signal_id)
        if not sig:
            await query.edit_message_text("⚠️ Signal expiré.")
            return
        results = place_orders(sig["direction"], sig["entry"], sig["levels"], LOT_SIZE, NB_TRADES_PER_SIGNAL)
        if results:
            tickets = [str(r["ticket"]) for r in results]
            tps     = [sig["levels"][f"tp{i+1}"] for i in range(len(results))]
            state["active_trades"] = [
                {**sig, "ticket": r["ticket"], "tp_target": r["tp_target"],
                 "tp_alerted": False, "sl_alerted": False}
                for r in results
            ]
            state["trades_today"] += 1
            detail = "\n".join(
                f"  Trade {i+1} → TP{i+1}: `{tps[i]}` | #{tickets[i]}"
                for i in range(len(results))
            )
            await query.edit_message_text(
                f"✅ *{len(results)} ordres placés !*\n"
                f"{sig['direction']} @ `{sig['entry']}`\n\n{detail}\n"
                f"🛑 SL : `{sig['levels']['sl']}`",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("❌ Erreur placement des ordres.")

    elif data.startswith("ignore_"):
        await query.edit_message_text("❌ Signal ignoré.")

    # SL → Breakeven (depuis bouton TP ou /trades)
    elif data.startswith("be_"):
        ticket = int(data.split("_")[1])
        # Cherche l'entry dans l'état interne, sinon move_sl_to_be prend depuis MT5
        entry = 0
        for t in state.get("active_trades", []):
            if t.get("ticket") == ticket:
                entry = t.get("entry", 0)
                break
        ok = move_sl_to_be(ticket, entry)
        await query.edit_message_text(
            f"🔒 *SL → Breakeven !* (#{ticket})" if ok
            else f"❌ Erreur SL BE #{ticket} — vérifie MT5",
            parse_mode="Markdown"
        )

    # Close un trade
    elif data.startswith("close_"):
        ticket = int(data.split("_")[1])
        state["manual_closes"].add(ticket)
        ok     = close_trade(ticket)
        if ok:
            for t in state.get("active_trades", []):
                if t.get("ticket") == ticket:
                    t["closed"] = True
                    t["result"] = "MANUEL"
            await query.edit_message_text(
                f"🚪 *Trade #{ticket} fermé manuellement.*\n_Non compté dans les stats_",
                parse_mode="Markdown"
            )
        else:
            state["manual_closes"].discard(ticket)
            await query.edit_message_text(f"❌ Erreur fermeture #{ticket}", parse_mode="Markdown")

    elif data.startswith("hold_"):
        await query.edit_message_text("✅ Trade laissé courir vers TP suivant.")

    # Changement de mode via boutons
    elif data.startswith("mode_"):
        mode_map = {
            "mode_safe":      "safe",
            "mode_risque":    "risque",
            "mode_risqueppp": "risque+++",
        }
        new_mode = mode_map.get(data, "risque")
        state["trading_mode"] = new_mode
        m = MODES_CONFIG[new_mode]
        log.info(f"Mode changé → {m['label']}")
        await query.edit_message_text(
            f"{m['label']} *activé !*\n"
            f"SL={m['sl_mult']}×ATR | TP1={m['tp1_mult']}×ATR\n"
            f"Score min={m['score_min']} | Cooldown={m['cooldown']}s",
            parse_mode="Markdown"
        )

    # Confirmation closeall
    elif data == "confirm_closeall":
        # Marque tous comme manuels avant fermeture
        for t in state.get("active_trades", []):
            if not t.get("closed"):
                state["manual_closes"].add(t.get("ticket", 0))
        ok, err = close_all_trades()
        msg = f"🚪 *{ok} trade(s) fermé(s) manuellement*\n_Non comptés dans les stats_"
        if err > 0:
            msg += f"\n❌ {err} erreur(s)"
        await query.edit_message_text(msg, parse_mode="Markdown")

    elif data == "cancel_closeall":
        await query.edit_message_text("❌ Fermeture annulée.")


# ════════════════════════════════════════════════════════════════════════════════
#  SURVEILLANCE DES TRADES OUVERTS
# ════════════════════════════════════════════════════════════════════════════════

def _is_position_still_open(ticket: int) -> bool:
    """Vérifie si une position est encore ouverte dans MT5."""
    if not MT5_AVAILABLE:
        return True
    positions = mt5.positions_get(ticket=ticket)
    return positions is not None and len(positions) > 0


def _get_closed_trade_pnl(ticket: int) -> float:
    """Récupère le PnL réel d'un trade fermé depuis l'historique MT5."""
    if not MT5_AVAILABLE:
        return 0.0
    try:
        from datetime import timedelta
        deals = mt5.history_deals_get(datetime.now() - timedelta(hours=24), datetime.now())
        if deals is None:
            return 0.0
        for deal in reversed(deals):
            if deal.position_id == ticket:
                return round(deal.profit, 2)
    except Exception as e:
        log.error(f"_get_closed_trade_pnl erreur : {e}")
    return 0.0


def _get_closed_trade_pnl_price(ticket: int) -> float | None:
    """
    Récupère le prix de clôture réel depuis l'historique MT5.
    Permet de distinguer SL / TP / fermeture manuelle avec précision.
    Retourne None si non trouvé.
    """
    if not MT5_AVAILABLE:
        return None
    try:
        from datetime import timedelta
        deals = mt5.history_deals_get(datetime.now() - timedelta(hours=24), datetime.now())
        if deals is None:
            return None
        for deal in reversed(deals):
            if deal.position_id == ticket and deal.entry == 1:  # entry=1 = clôture
                return round(deal.price, 2)
    except Exception as e:
        log.error(f"_get_closed_trade_pnl_price erreur : {e}")
    return None


async def monitor_active_trade(app):
    """
    Surveille les 3 trades actifs.
    Pour chaque trade :
      - Si MT5 disponible : vérifie si la position est encore ouverte
        (MT5 ferme automatiquement quand TP ou SL est atteint)
      - Si simulation : vérifie le prix par rapport aux niveaux
    Envoie une notification Telegram à chaque fermeture.
    """
    trades = state.get("active_trades", [])
    if not trades:
        return

    bid, _ = get_current_price()
    price  = bid

    for trade in trades:
        if trade.get("closed"):
            continue

        direction = trade["direction"]
        levels    = trade["levels"]
        ticket    = trade.get("ticket", 0)
        tp_target = trade.get("tp_target", 1)
        entry     = trade.get("entry", 0)
        tp_price  = levels.get(f"tp{tp_target}", 0)

        # Détection fermeture : MT5 OU prix
        if MT5_AVAILABLE:
            # MT5 ferme la position tout seul quand TP/SL atteint
            # On détecte ça en vérifiant si la position existe encore
            position_closed = not _is_position_still_open(ticket)
            tp_hit = position_closed and (
                (direction == "BUY"  and price >= tp_price) or
                (direction == "SELL" and price <= tp_price)
            )
            sl_hit = position_closed and (
                (direction == "BUY"  and price <= levels["sl"]) or
                (direction == "SELL" and price >= levels["sl"])
            )
            # Si fermé sans qu'on sache pourquoi → on regarde le prix
            if position_closed and not tp_hit and not sl_hit:
                tp_hit = (direction == "BUY"  and price >= tp_price) or (direction == "SELL" and price <= tp_price)
                sl_hit = not tp_hit
        else:
            # Simulation : on compare le prix aux niveaux
            tp_hit = (direction == "BUY"  and price >= tp_price) or \
                     (direction == "SELL" and price <= tp_price)
            sl_hit = (direction == "BUY"  and price <= levels["sl"]) or \
                     (direction == "SELL" and price >= levels["sl"])

        # ── Détection fermeture manuelle (app MT5) ──────────────────────────
        # Compare le prix de clôture RÉEL (historique MT5) aux niveaux TP/SL
        # Si le prix de clôture est proche du SL → c'est un SL, pas manuel
        # Si proche du TP → c'est un TP
        # Sinon → fermeture manuelle
        if MT5_AVAILABLE and not trade.get("closed"):
            position_gone = not _is_position_still_open(ticket)
            if position_gone:
                # Prix réel de clôture depuis l'historique MT5
                real_exit = _get_closed_trade_pnl_price(ticket)

                if real_exit is not None:
                    # Tolérance large : le SL peut avoir été déplacé au BE ou à TP1
                    # donc on vérifie contre TOUS les niveaux connus
                    tol = 5.0
                    tp_reached = (
                        abs(real_exit - tp_price) <= tol or
                        abs(real_exit - levels.get("tp1", 0)) <= tol or
                        abs(real_exit - levels.get("tp2", 0)) <= tol or
                        abs(real_exit - levels.get("tp3", 0)) <= tol
                    )
                    sl_reached = (
                        abs(real_exit - levels["sl"]) <= tol or
                        abs(real_exit - entry) <= tol   # SL au BE
                    )
                else:
                    # Fallback sur prix actuel
                    tp_reached = (direction == "BUY"  and price >= tp_price) or                                  (direction == "SELL" and price <= tp_price)
                    sl_reached = (direction == "BUY"  and price <= levels["sl"]) or                                  (direction == "SELL" and price >= levels["sl"])

                if not tp_reached and not sl_reached:
                    # Fermeture manuelle — on récupère le vrai PnL
                    real_pnl = _get_closed_trade_pnl(ticket)
                    trade["closed"]    = True
                    trade["result"]    = "MANUEL"
                    trade["pnl_final"] = real_pnl
                    log.info(f"ℹ️ Trade #{ticket} fermé manuellement — PnL réel: {real_pnl}$")
                    if real_pnl != 0:
                        state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
                    await app.bot.send_message(
                        TELEGRAM_CHAT_ID,
                        f"ℹ️ *Trade #{ticket} fermé manuellement*\nPnL : `{real_pnl:+.2f}$` (non compté dans winrate)",
                        parse_mode="Markdown"
                    )
                    continue

        if tp_hit and not trade.get("tp_alerted"):
            trade["tp_alerted"] = True
            trade["closed"]     = True
            trade["result"]     = f"TP{tp_target}"
            pips                = round(abs(tp_price - entry), 2)
            pnl_dollar          = round(pips * LOT_SIZE * 100, 2)
            trade["pnl_final"]  = pnl_dollar
            state["daily_pnl"]  = round(state["daily_pnl"] + pnl_dollar, 2)
            state["wins"]      += 1
            state["current_streak"] = state["current_streak"] + 1 if state["current_streak"] >= 0 else 1
            if pnl_dollar > state["best_trade"]:
                state["best_trade"] = pnl_dollar
            state["history"].append({
                "direction": direction, "entry": entry,
                "result": f"TP{tp_target}", "pnl": pnl_dollar,
                "time": datetime.now().strftime("%H:%M")
            })
            wins_today   = state["wins"]
            losses_today = state["losses"]
            wr = round(wins_today/(wins_today+losses_today)*100) if (wins_today+losses_today) > 0 else 0

            if tp_target == 1:
                # TP1 → SL des trades 2 et 3 au breakeven (entrée)
                still_open = 0
                for other in trades:
                    if not other.get("closed") and other["ticket"] != ticket:
                        move_sl_to_be(other["ticket"], other.get("entry", 0))
                        still_open += 1
                await app.bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"🔥 *TP1 TOUCHÉ !* +`{pips}` pts\n"
                    f"🔒 SL → Breakeven sur {still_open} trade(s) restant(s) ✅\n"
                    f"▶️ Trades 2 et 3 continuent vers TP2/TP3\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 PnL jour : `{state['daily_pnl']:+.2f}$` | Winrate : `{wr}%`",
                    parse_mode="Markdown"
                )
            elif tp_target == 2:
                # TP2 → SL du trade 3 au niveau TP1 (profit garanti)
                tp1_price = levels.get("tp1", entry)
                for other in trades:
                    if not other.get("closed") and other["ticket"] != ticket:
                        move_sl_to_level(other["ticket"], tp1_price)
                await app.bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"🔥🔥 *TP2 TOUCHÉ !* +`{pips}` pts\n"
                    f"🔒 SL du trade 3 → TP1 (`{tp1_price}`) ✅\n"
                    f"▶️ Trade 3 continue vers TP3\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 PnL jour : `{state['daily_pnl']:+.2f}$` | Winrate : `{wr}%`",
                    parse_mode="Markdown"
                )
            else:
                # TP3 — séquence terminée avec succès
                await app.bot.send_message(
                    TELEGRAM_CHAT_ID,
                    f"🔥🔥🔥 *TP3 TOUCHÉ !* +`{pips}` pts\n"
                    f"💰 PnL jour : `{state['daily_pnl']:+.2f}$` | Winrate : `{wr}%`",
                    parse_mode="Markdown"
                )

        elif sl_hit and not trade.get("sl_alerted"):
            # Vérifie que c'est pas une fermeture manuelle
            if ticket in state["manual_closes"]:
                trade["closed"]  = True
                trade["result"]  = "MANUEL"
                state["manual_closes"].discard(ticket)
                continue

            trade["sl_alerted"] = True
            trade["closed"]     = True
            trade["result"]     = "SL"
            loss                = round(abs(levels["sl"] - entry), 2)
            pnl_dollar          = -round(loss * LOT_SIZE * 100, 2)
            trade["pnl_final"]  = pnl_dollar
            state["daily_pnl"]  = round(state["daily_pnl"] + pnl_dollar, 2)
            state["losses"]    += 1
            state["last_sl_time"] = time.time()
            state["current_streak"] = state["current_streak"] - 1 if state["current_streak"] <= 0 else -1
            if pnl_dollar < state["worst_trade"]:
                state["worst_trade"] = pnl_dollar
            state["history"].append({
                "direction": direction, "entry": entry,
                "result": "SL", "pnl": pnl_dollar,
                "time": datetime.now().strftime("%H:%M")
            })
            wins_today   = state["wins"]
            losses_today = state["losses"]
            wr = round(wins_today/(wins_today+losses_today)*100) if (wins_today+losses_today) > 0 else 0
            streak_txt = f"⚠️ {abs(state['current_streak'])} pertes d'affilée !" if state["current_streak"] < -2 else ""
            await app.bot.send_message(
                TELEGRAM_CHAT_ID,
                f"🛑 *SL touché* — #{ticket}\n"
                f"Entrée `{entry}` → Sortie `{price}` | `-{loss}` pts\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"📉 PnL jour : `{state['daily_pnl']:+.2f}$` | Winrate : `{wr}%`"
                + (f"\n{streak_txt}" if streak_txt else ""),
                parse_mode="Markdown"
            )

    if trades and all(t.get("closed") for t in trades):
        # Résumé de la séquence complète
        wins_seq  = [t for t in trades if t.get("result") == "TP"]
        sl_seq    = [t for t in trades if t.get("result") == "SL"]
        pnl_seq   = round(sum(t.get("pnl_final", 0) for t in trades), 2)
        sign      = "+" if pnl_seq >= 0 else ""
        emoji_sum = "✅" if pnl_seq >= 0 else "❌"
        state["active_trades"] = []
        await app.bot.send_message(
            TELEGRAM_CHAT_ID,
            f"{emoji_sum} *Séquence terminée*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"PnL séquence : `{sign}{pnl_seq}$`\n"
            f"📊 PnL jour  : `{state['daily_pnl']:+.2f}$`\n"
            f"\n"
            f"▶️ En attente du prochain signal...",
            parse_mode="Markdown"
        )


# ════════════════════════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE SCALPING
# ════════════════════════════════════════════════════════════════════════════════

async def analysis_loop(app):
    """
    BOUCLE SCALPING — scan toutes les SECONDES, sans API IA.

    Pourquoi 1 seconde ?
      Le scalping M5 peut avoir des signaux qui durent 10-30 secondes.
      Attendre 10s entre chaque scan = rater des entrées au bon prix.
      Avec 1s, on entre au moment exact où les conditions sont réunies.

    Pourquoi sans IA ?
      L'API Claude ajoute 1-3 secondes de latence par signal.
      En scalping, 2 secondes de retard = entrée au mauvais prix.
      Le score (0-100) calculé localement suffit à filtrer les mauvais trades.

    Optimisation des données :
      Les bougies M5 ne changent que toutes les 5 minutes.
      On ne les recharge depuis MT5 que toutes les 30 secondes,
      et on recalcule les indicateurs localement à chaque tick.
      Ça économise les appels réseau et accélère la boucle.

    Flux :
      1. Reset quotidien à minuit
      2. Pause → attend
      3. Surveillance trades actifs (toutes les 500ms si trade ouvert)
      4. Limite journalière atteinte → attend
      5. Cooldown entre trades (60s)
      6. Spread trop large → passe
      7. Reload bougies si nécessaire (toutes les 30s)
      8. Calcul indicateurs (< 1ms)
      9. determine_direction() + compute_score()
      10. Seuil adaptatif selon heure/trades du jour
      11. Exécution directe ou signal Telegram
    """
    log.info("⚡ SCALPING BOT DÉMARRÉ — Scan toutes les secondes (sans IA)")

    # Délai entre deux trades (2 min = 40-50 trades/jour)
    COOLDOWN = 120

    # Cache des bougies — rechargées depuis MT5 toutes les 30s seulement
    # (les bougies M5 ne changent pas à chaque seconde)
    _cache_m5   = None
    _cache_m15  = None
    _cache_h1   = None
    _last_reload = 0.0
    RELOAD_INTERVAL = 30   # secondes entre chaque rechargement MT5

    while True:
        try:
            # ── Reset quotidien ──────────────────────────────────────────────
            today = date.today()
            if state["last_trade_date"] != today:
                state["trades_today"]       = 0
                state["daily_pnl"]          = 0.0
                state["last_trade_date"]    = today
                state["total_signals_sent"] = 0
                state["wins"]               = 0
                state["losses"]             = 0
                state["best_trade"]         = 0.0
                state["worst_trade"]        = 0.0
                state["current_streak"]     = 0
                state["history"]            = []
                state["manual_closes"]      = set()
                _last_reload = 0.0
                log.info(f"🔄 Reset quotidien — {today}")

            # ── Pause ────────────────────────────────────────────────────────
            if state["paused"]:
                await asyncio.sleep(1)
                continue

            # ── Surveillance trades actifs (priorité absolue) ────────────────
            if state.get("active_trades"):
                await monitor_active_trade(app)
                await asyncio.sleep(0.5)   # 500ms quand trade ouvert
                continue

            # ── Limite journalière ───────────────────────────────────────────
            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                await asyncio.sleep(5)
                continue

            # ── Cooldown entre trades (selon le mode actif) ──────────────────
            # Score progressif après un trade (plus de blocage total)
            mode_cooldown = state["cooldown_override"] if state["cooldown_override"] is not None                 else MODES_CONFIG[state.get("trading_mode", "risque")]["cooldown"]
            time_since_trade = time.time() - state["last_signal_time"]
            if time_since_trade < mode_cooldown:
                ratio = 1 - (time_since_trade / mode_cooldown)
                _cd_score_bonus = round(ratio * 20)
            else:
                _cd_score_bonus = 0

            # ── Spread ──────────────────────────────────────────────────────
            spread = get_spread()
            if spread > MAX_SPREAD:
                await asyncio.sleep(1)
                continue

            # ── Rechargement bougies (toutes les 30s) ────────────────────────
            now = time.time()
            if now - _last_reload >= RELOAD_INTERVAL:
                # On charge M1 comme timeframe principal (tu trades en M1)
                df_m1  = get_candles(TIMEFRAME_M1,  200)
                df_m5  = get_candles(TIMEFRAME_M5,  100)
                df_h1  = get_candles(TIMEFRAME_H1,  100)
                if df_m1 is not None and len(df_m1) >= 30:
                    _cache_m5  = df_m1   # on utilise M1 comme base principale
                    _cache_m15 = df_m5   if df_m5 is not None else df_m1
                    _cache_h1  = df_h1   if df_h1  is not None else df_m1
                    _last_reload = now
                    log.info(f"📊 Bougies rechargées — M1:{len(df_m1)} | Prix:{round(df_m1['Close'].iloc[-1],2)} | Spread:{get_spread()}")
                else:
                    log.warning(f"⚠️ MT5 n'a pas renvoyé de bougies M1 — retry dans 5s")
                    await asyncio.sleep(5)
                    continue

            # Pas encore de données au démarrage
            if _cache_m5 is None:
                log.info("⏳ En attente des premières bougies MT5...")
                await asyncio.sleep(1)
                continue

            # ── Calcul indicateurs ────────────────────────────────────────────
            ind = compute_indicators(_cache_m5.tail(100).reset_index(drop=True))

            # ── Direction ────────────────────────────────────────────────────
            direction = determine_direction(ind)
            if direction is None:
                # Log toutes les 30s pour voir l'état des indicateurs
                if int(time.time()) % 30 == 0:
                    log.info(
                        f"🔍 Scan | Prix:{ind['price']} RSI:{ind['rsi']} "
                        f"Stoch:{ind['stoch_k']:.0f}/{ind['stoch_d']:.0f} "
                        f"MACD:{ind['macd_hist']:.3f} EMA9:{ind['ema9']} "
                        f"Bounce↑:{ind['bounce_bull']} Bounce↓:{ind['bounce_bear']}"
                    )
                await asyncio.sleep(1)
                continue

            # ── Score ────────────────────────────────────────────────────────
            score, reasons = compute_score(ind, direction)

            # ── Seuil adaptatif ──────────────────────────────────────────────
            # Plus la journée avance sans trade, plus on accepte des signaux
            # moins parfaits pour garantir l'activité quotidienne
            # Seuil selon le mode actif
            m         = MODES_CONFIG[state.get("trading_mode", "risque")]
            base_score = m["score_min"]
            h = datetime.now().hour
            # Seuil adaptatif en fin de journée si peu de trades
            if   state["trades_today"] == 0 and h >= 17: threshold = max(15, base_score - 15)
            elif state["trades_today"] == 0 and h >= 15: threshold = max(20, base_score - 10)
            elif state["trades_today"] <  3 and h >= 13: threshold = max(25, base_score - 5)
            else:                                         threshold = base_score

            # Score +10 pendant 30s après un SL
            time_since_sl = time.time() - state.get("last_sl_time", 0)
            if time_since_sl < 30:
                threshold += 10

            # Score progressif lié au cooldown
            threshold += _cd_score_bonus

            # Log seulement toutes les 10s pour éviter le spam
            if int(time.time()) % 10 == 0:
                log.info(
                    f"⚡ {direction} | {score}/{threshold} "
                    f"(base={MODES_CONFIG[state.get('trading_mode','risque')]['score_min']}"
                    f"+cd={_cd_score_bonus}+sl={10 if time_since_sl < 30 else 0}) | "
                    f"RSI:{ind['rsi']} Stoch:{ind['stoch_k']:.0f} ATR:{ind['atr']} | "
                    f"{', '.join(reasons[:2])}"
                )

            if score < threshold:
                await asyncio.sleep(1)
                continue

            # ── Signal validé — Niveaux TP/SL ───────────────────────────────
            levels = calculate_tp_sl(direction, ind["price"], ind["atr"])

            # ── Exécution ou signal Telegram ─────────────────────────────────
            if AUTO_TRADE:
                results = place_orders(direction, ind["price"], levels)
                if results:
                    state["active_trades"] = [
                        {
                            "direction":  direction,
                            "entry":      ind["price"],
                            "levels":     levels,
                            "ticket":     r["ticket"],
                            "tp_target":  r["tp_target"],
                            "tp_alerted": False,
                            "sl_alerted": False,
                        }
                        for r in results
                    ]
                    state["trades_today"]       += 1
                    state["last_signal_time"]    = time.time()
                    state["total_signals_sent"] += 1
                    _last_reload = 0.0   # force reload bougies après un trade

                    emoji = "🟢" if direction == "BUY" else "🔴"
                    verb  = "J'ACHÈTE" if direction == "BUY" else "JE VENDS"
                    top3  = " · ".join(reasons[:3])
                    now   = datetime.now().strftime("%H:%M")
                    nb_ok = len(results)
                    await app.bot.send_message(
                        TELEGRAM_CHAT_ID,
                        f"{emoji} *{verb} XAUUSD* à `{ind['price']}`\n"
                        f"📡 Source : `🤖 Bot (analyse propre)`\n"
                        f"\n"
                        f"🎯 Trade 1 → TP1 : `{levels['tp1']}`\n"
                        f"🎯 Trade 2 → TP2 : `{levels['tp2']}`\n"
                        f"🎯 Trade 3 → TP3 : `{levels['tp3']}`\n"
                        f"\n"
                        f"🔒 SL : `{levels['sl']}` (commun aux 3)\n"
                        f"\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Score `{score}/100` · {nb_ok}/3 ordres · {now}\n"
                        f"✅ {top3}",
                        parse_mode="Markdown"
                    )
                    log.info(f"✅ {direction} @ {ind['price']} score {score}")
                else:
                    log.error("❌ MT5 a rejeté l'ordre")
            else:
                # Mode manuel — signal envoyé sur Telegram, tu valides
                comment = f"Score {score}/100 — {', '.join(reasons[:2])}"
                text, keyboard = build_signal_message(
                    direction, ind, levels, score, reasons, comment
                )
                await app.bot.send_message(
                    TELEGRAM_CHAT_ID, text,
                    parse_mode="Markdown", reply_markup=keyboard
                )
                state["last_signal_time"]    = time.time()
                state["total_signals_sent"] += 1
                log.info(f"📨 Signal Telegram : {direction} score {score}")

        except Exception as e:
            log.error(f"Erreur boucle : {e}", exc_info=True)

        await asyncio.sleep(1)


# ════════════════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════════════════

async def main():
    if not TELEGRAM_AVAILABLE:
        print("❌ Telegram non disponible. pip install python-telegram-bot")
        return

    connect_mt5()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("prix",        cmd_prix))
    app.add_handler(CommandHandler("pnl",         cmd_pnl))
    app.add_handler(CommandHandler("trades",      cmd_trades))
    app.add_handler(CommandHandler("closeall",    cmd_closeall))
    app.add_handler(CommandHandler("closetrade",  cmd_closetrade))
    app.add_handler(CommandHandler("be",          cmd_be))
    app.add_handler(CommandHandler("pause",       cmd_pause))
    app.add_handler(CommandHandler("resume",      cmd_resume))
    app.add_handler(CommandHandler("mode",        cmd_mode))
    app.add_handler(CommandHandler("safe",        cmd_safe))
    app.add_handler(CommandHandler("risque",      cmd_risque))
    app.add_handler(CommandHandler("risqueppp",   cmd_risqueppp))
    app.add_handler(CommandHandler("auto",        cmd_auto))
    app.add_handler(CommandHandler("manuel",      cmd_manuel))
    app.add_handler(CommandHandler("stats",       cmd_stats))
    app.add_handler(CommandHandler("settrades",   cmd_settrades))
    app.add_handler(CommandHandler("setcooldown",  cmd_setcooldown))
    app.add_handler(CommandHandler("setlot",      cmd_setlot))
    app.add_handler(CommandHandler("reset",       cmd_reset))
    app.add_handler(CallbackQueryHandler(handle_callback))

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    log.info("⚡ Gold Scalping Bot démarré")
    await app.bot.send_message(
        TELEGRAM_CHAT_ID,
        f"⚡ *Gold Scalping Bot démarré !*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Mode : `Scalping M5` | Lot : `{LOT_SIZE}`\n"
        f"Trades/jour max : `{MAX_TRADES_PER_DAY}`\n"
        f"Trading : `{'🤖 AUTO' if AUTO_TRADE else '👆 Manuel'}`\n\n"
        f"Tape /start pour voir toutes les commandes",
        parse_mode="Markdown"
    )

    try:
        await analysis_loop(app)
    finally:
        disconnect_mt5()
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())