import asyncio
import logging
import time
import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
from collections import defaultdict
import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("MetaTrader5 non disponible - mode simulation")

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    LOT_SIZE, MAX_SPREAD, MAX_TRADES_PER_DAY,
    SYMBOL, TIMEFRAME_M5, TIMEFRAME_M15, TIMEFRAME_H1,
    MAGIC_NUMBER,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot_headless.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

_MT5_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mt5")

async def run_in_mt5_thread(fn, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_MT5_EXECUTOR, fn, *args)

# ─────────────────────────────────────────────────────────────────────────────
# MODES DE TRADING
# Rapport §3.1 : recalibrage R:R — TP1 passe de 1.0× à 1.5× (R:R min 1.5:1)
# Le mode "optimale" est la configuration recommandée pour le déploiement démo.
# score_min=45 / sl_mult=1.5 → meilleure combinaison de la matrice de sensibilité.
# ─────────────────────────────────────────────────────────────────────────────
MODES_CONFIG = {
    "safe": {
        "sl_mult":    2.5,
        "tp1_mult":   1.5, "tp2_mult": 2.5, "tp3_mult": 4.0,
        "score_min":  60,
        "cooldown":   300,
        "label":      "SAFE",
        "desc":       "SL large, peu de trades, setups premium",
    },
    "risque": {
        "sl_mult":    1.8,
        "tp1_mult":   1.2, "tp2_mult": 2.0, "tp3_mult": 3.2,
        "score_min":  45,
        "cooldown":   180,
        "label":      "RISQUE",
        "desc":       "Equilibre, parametres standards",
    },
    "risque+++": {
        "sl_mult":    1.5,
        "tp1_mult":   1.0, "tp2_mult": 1.8, "tp3_mult": 2.8,
        "score_min":  35,
        "cooldown":   90,
        "label":      "RISQUE_MAX",
        "desc":       "SL minimum, haute frequence — legacy",
    },
    # ── NOUVEAU MODE — Rapport §3.1 + §3.2 ──────────────────────────────
    # TP1 : 1.0 → 1.5 | TP2 : 1.8 → 2.5 | TP3 : 2.8 → 4.0
    # Trailing stop ATR actif (remplace BE fixe, voir _handle_tp)
    # score_min=45 / sl_mult=1.5 = optimum backtest tick (Net: +592.83$)
    # ────────────────────────────────────────────────────────────────────
    "optimale": {
        "sl_mult":    1.5,
        "tp1_mult":   1.5,
        "tp2_mult":   2.5,
        "tp3_mult":   4.0,
        "score_min":  45,
        "cooldown":   90,
        "label":      "OPTIMALE",
        "desc":       "RR 1.5+ | Trailing ATR | Dynamic lot | Cooldown adaptatif",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES DE RISQUE
# ─────────────────────────────────────────────────────────────────────────────
MAX_RISK_PTS    = 22.0    # Jamais plus de 22 pts de SL sur XAU
DAILY_MAX_LOSS  = -150.0  # Coupe-circuit perte journalière
SESSION_END_H   = 15      # Aucun nouveau trade à partir de 15h GMT+2
SESSION_MID_H   = 12      # Threshold +10 à partir de midi GMT+2
LOT_MIN         = 0.01    # Lot minimum (dynamic sizing)
LOT_MAX         = 0.05    # Lot maximum (dynamic sizing)

# ─────────────────────────────────────────────────────────────────────────────
# TRAILING STOP CONFIG — Rapport §3.2
# ATR_TRAIL_P1 : SL déplacé à entry + ATR_TRAIL_P1×ATR après TP1 (au lieu de BE)
# ATR_TRAIL_P2 : fraction du move TP1→TP2 lockée après TP2 pour P3
# ─────────────────────────────────────────────────────────────────────────────
ATR_TRAIL_P1   = 0.5   # gain min garanti après TP1 = 0.5×ATR
TP2_LOCK_RATIO = 0.3   # P3 SL = TP1 + 30% du move TP1→TP2

# ─────────────────────────────────────────────────────────────────────────────
# CIRCUIT BREAKER — Rapport §3.6
# Après N SL consécutifs, session stoppée + cooldown cumulatif × 1.5^N
# ─────────────────────────────────────────────────────────────────────────────
MAX_CONSECUTIVE_SL    = 3     # Arrêt session après 3 SL de suite
COOLDOWN_SL_MULTIPLIER = 1.5  # Chaque SL consécutif × 1.5 le cooldown

# ─────────────────────────────────────────────────────────────────────────────
# DELAYED SL — Rapport §3.5
# Après DELAYED_SL_CANDLES bougies M5 sans toucher le SL, on resserre à 1.0×ATR
# ─────────────────────────────────────────────────────────────────────────────
DELAYED_SL_CANDLES    = 2     # Resserre le SL après 2 bougies M5 (10 min)
DELAYED_SL_MULT       = 1.0   # Nouveau SL = entry ± 1.0×ATR

state = {
    "trades_today":        0,
    "active_trades":       [],
    "last_signal_time":    0,
    "daily_pnl":           0.0,
    "total_signals_sent":  0,
    "last_trade_date":     None,
    "paused":              False,
    "trading_mode":        "optimale",   # Mode recommandé par le rapport
    "wins":                0,
    "losses":              0,
    "best_trade":          0.0,
    "worst_trade":         0.0,
    "current_streak":      0,
    "history":             [],
    "manual_closes":       set(),
    "nb_trades":           3,
    "lot_size":            LOT_SIZE,
    "cooldown_override":   None,
    "last_sl_time":        0,
    # ── Nouvelles clés — Rapport analytique ──────────────────────────
    "consecutive_sl":      0,     # Compteur SL consécutifs (§3.6)
    "session_stopped":     False, # Circuit breaker 3×SL (§3.6)
    "pending_entries":     [],    # Entrées séquentielles P2/P3 (§3.3)
    "atr_history":         [],    # Historique ATR pour sizing dynamique (§3.4)
}

# ─────────────────────────────────────────────────────────────────────────────
# CONNEXION MT5
# ─────────────────────────────────────────────────────────────────────────────
def _mt5_connect() -> bool:
    if not MT5_AVAILABLE:
        return False
    if not mt5.initialize():
        log.error(f"MT5 initialize() echoue : {mt5.last_error()}")
        return False
    if MT5_LOGIN:
        ok = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            log.error(f"MT5 login echoue : {mt5.last_error()}")
            return False
    log.info("MT5 connecte")
    return True

def _mt5_disconnect():
    if MT5_AVAILABLE:
        mt5.shutdown()

def _fetch_candles_sync(timeframe: int, count: int) -> pd.DataFrame | None:
    if not MT5_AVAILABLE:
        return _simulate_candles(count)
    rates = mt5.copy_rates_from_pos(SYMBOL, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={
        "open": "Open", "high": "High",
        "low": "Low",   "close": "Close",
        "tick_volume": "Volume",
    }, inplace=True)
    return df

async def get_candles(timeframe: int, count: int = 200) -> pd.DataFrame | None:
    return await run_in_mt5_thread(_fetch_candles_sync, timeframe, count)

def _simulate_candles(count: int) -> pd.DataFrame:
    np.random.seed(int(time.time()) % 1000)
    base   = 2650.0
    closes = [base]
    for _ in range(count - 1):
        closes.append(closes[-1] + np.random.randn() * 2.5)
    closes = np.array(closes)
    return pd.DataFrame({
        "Open":   closes - np.abs(np.random.randn(count) * 0.8),
        "High":   closes + np.abs(np.random.randn(count) * 1.5),
        "Low":    closes - np.abs(np.random.randn(count) * 1.5),
        "Close":  closes,
        "Volume": np.random.randint(200, 3000, count).astype(float),
    })

def _get_tick_sync() -> tuple[float, float]:
    if not MT5_AVAILABLE:
        return 2650.0, 2650.35
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return 0.0, 0.0
    return tick.bid, tick.ask

async def get_current_price() -> tuple[float, float]:
    return await run_in_mt5_thread(_get_tick_sync)

async def get_spread() -> float:
    bid, ask = await get_current_price()
    return round(ask - bid, 2)

def _get_positions_sync() -> list[dict]:
    if not MT5_AVAILABLE:
        return [
            {
                "ticket":    t.get("ticket", 0),
                "direction": t.get("direction", "?"),
                "volume":    state["lot_size"],
                "entry":     t.get("entry", 0),
                "sl":        t.get("levels", {}).get("sl", 0),
                "tp":        t.get("levels", {}).get("tp1", 0),
                "pnl":       0.0,
                "swap":      0.0,
            }
            for t in state.get("active_trades", [])
            if not t.get("closed")
        ]
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return []
    return [
        {
            "ticket":    p.ticket,
            "direction": "BUY" if p.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume":    p.volume,
            "entry":     p.price_open,
            "sl":        p.sl,
            "tp":        p.tp,
            "pnl":       round(p.profit, 2),
            "swap":      round(p.swap, 2),
        }
        for p in positions
    ]

async def get_open_positions() -> list[dict]:
    return await run_in_mt5_thread(_get_positions_sync)

# ─────────────────────────────────────────────────────────────────────────────
# RESTAURATION AU DÉMARRAGE
# ─────────────────────────────────────────────────────────────────────────────
async def restore_state_on_startup():
    """Récupère les positions MT5 ouvertes et reconstruit l'état du bot."""
    positions = await get_open_positions()
    if not positions:
        log.info("Restauration: aucune position ouverte detectee.")
        return

    log.info(f"Restauration: {len(positions)} position(s) ouverte(s) detectee(s) !")
    restored = []
    for p in positions:
        restored.append({
            "direction":    p["direction"],
            "entry":        p["entry"],
            "levels": {
                "sl":  p["sl"],
                "tp1": p["tp"],
                "tp2": p["tp"],
                "tp3": p["tp"],
            },
            "ticket":       p["ticket"],
            "tp_target":    1,
            "tp_alerted":   False,
            "sl_alerted":   False,
            "atr":          None,     # ATR inconnu au restart
            "entry_time":   datetime.now(),
            "sl_tightened": False,
        })

    state["active_trades"] = restored
    state["trades_today"]  = max(state["trades_today"], (len(positions) + 2) // 3)
    log.info(
        f"Restauration complete : {len(restored)} trade(s) repris. "
        f"trades_today={state['trades_today']}"
    )

# ─────────────────────────────────────────────────────────────────────────────
# INDICATEURS TECHNIQUES
# ─────────────────────────────────────────────────────────────────────────────
def compute_vwap(df: pd.DataFrame, window: int = 50) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    tp_vol  = typical * df["Volume"]
    return tp_vol.rolling(window).sum() / df["Volume"].rolling(window).sum()

def compute_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    price_min = df["Low"].min()
    price_max = df["High"].max()
    if price_max == price_min:
        return {"poc": df["Close"].iloc[-1], "value_area_high": price_max, "value_area_low": price_min}

    volumes, edges = np.histogram(df["Close"], bins=bins, weights=df["Volume"])
    poc_idx    = volumes.argmax()
    poc        = (edges[poc_idx] + edges[poc_idx + 1]) / 2
    target_vol = volumes.sum() * 0.70

    sorted_idx = np.argsort(volumes)[::-1]
    acc        = 0
    va_indices = []
    for idx in sorted_idx:
        acc += volumes[idx]
        va_indices.append(idx)
        if acc >= target_vol:
            break

    va_high = max((edges[i] + edges[i + 1]) / 2 for i in va_indices)
    va_low  = min((edges[i] + edges[i + 1]) / 2 for i in va_indices)
    return {"poc": round(poc, 2), "value_area_high": round(va_high, 2), "value_area_low": round(va_low, 2)}

def find_swing_levels(df: pd.DataFrame, lookback: int = 30) -> tuple[float, float]:
    recent     = df.tail(lookback)
    return round(recent["High"].max(), 2), round(recent["Low"].min(), 2)

def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    return round(tr.rolling(window).mean().iloc[-1], 2)

def compute_indicators(df: pd.DataFrame) -> dict:
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    vol    = df["Volume"]
    price  = close.iloc[-1]

    atr = compute_atr(df)

    vwap_series  = compute_vwap(df, window=50)
    vwap         = vwap_series.iloc[-1]
    typical      = (high + low + close) / 3
    vwap_std     = (typical - vwap_series).rolling(50).std().iloc[-1]
    if np.isnan(vwap_std) or vwap_std == 0:
        vwap_std = 1e-9
    vwap_upper1  = round(vwap + 1.0 * vwap_std, 2)
    vwap_lower1  = round(vwap - 1.0 * vwap_std, 2)
    vwap_upper2  = round(vwap + 2.0 * vwap_std, 2)
    vwap_lower2  = round(vwap - 2.0 * vwap_std, 2)

    dist_vwap    = round(price - vwap, 2)
    dist_vwap_sd = round(dist_vwap / vwap_std, 2)
    vwap_slope   = round(vwap_series.diff(5).iloc[-1], 2)

    vp  = compute_volume_profile(df.tail(100))
    poc = vp["poc"]

    near_poc = abs(price - poc) < atr * 0.5

    swing_high, swing_low = find_swing_levels(df, lookback=30)
    near_swing_support    = abs(price - swing_low)  < atr * 1.0
    near_swing_resistance = abs(price - swing_high) < atr * 1.0

    ema200    = close.ewm(span=200, adjust=False).mean().iloc[-1]
    above_200 = price > ema200

    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    ema_cross_bull = (ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-2] <= ema50.iloc[-2])
    ema_cross_bear = (ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-2] >= ema50.iloc[-2])

    avg_vol   = vol.rolling(20).mean().iloc[-1]
    vol_ratio = round(vol.iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
    vol_spike = vol_ratio > 1.4

    cur_open  = df["Open"].iloc[-1]
    cur_close = close.iloc[-1]
    cur_body  = abs(cur_close - cur_open)
    cur_bull  = cur_close > cur_open
    cur_bear  = cur_close < cur_open

    last10 = close.iloc[-11:-1]
    move10 = last10.iloc[-1] - last10.iloc[0]
    above_50 = price > ema50.iloc[-1]

    # [FIX PRECEDENCE] Parenthèses explicites — bug de précédence opérateur dans v1
    bounce_bull = (
        near_swing_support
        and cur_bull
        and cur_body  > atr * 0.5
        and vol_ratio > 1.4
        and move10    < -atr * 1.5
        and (not above_200 or (above_200 and near_poc))
    )

    bounce_bear = (
        near_swing_resistance
        and cur_bear
        and cur_body  > atr * 0.5
        and vol_ratio > 1.4
        and move10    > atr * 1.5
    )

    pattern = _detect_candle_pattern(df)

    return {
        "price":          round(price, 2),
        "atr":            atr,
        "vwap":           round(vwap, 2),
        "vwap_upper1":    vwap_upper1,
        "vwap_lower1":    vwap_lower1,
        "vwap_upper2":    vwap_upper2,
        "vwap_lower2":    vwap_lower2,
        "dist_vwap_sd":   dist_vwap_sd,
        "vwap_slope":     vwap_slope,
        "poc":            poc,
        "va_high":        vp["value_area_high"],
        "va_low":         vp["value_area_low"],
        "near_poc":       near_poc,
        "swing_high":     swing_high,
        "swing_low":      swing_low,
        "near_swing_sup": near_swing_support,
        "near_swing_res": near_swing_resistance,
        "ema200":         round(ema200, 2),
        "ema20":          round(ema20.iloc[-1], 2),
        "ema50":          round(ema50.iloc[-1], 2),
        "above_200":      above_200,
        "above_50":       above_50,
        "ema_cross_bull": ema_cross_bull,
        "ema_cross_bear": ema_cross_bear,
        "vol_ratio":      vol_ratio,
        "vol_spike":      vol_spike,
        "bounce_bull":    bounce_bull,
        "bounce_bear":    bounce_bear,
        "move10":         round(move10, 2),
        "pattern":        pattern,
    }

def _detect_candle_pattern(df: pd.DataFrame) -> str:
    o, h, l, c = (df["Open"].iloc[-1], df["High"].iloc[-1],
                  df["Low"].iloc[-1], df["Close"].iloc[-1])
    body       = abs(c - o)
    candle     = h - l
    if candle == 0:
        return "Doji"
    ratio      = body / candle
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if ratio < 0.1:                                                  return "Doji"
    if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o: return "Marteau"
    if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o: return "Shooting Star"
    prev_o, prev_c = df["Open"].iloc[-2], df["Close"].iloc[-2]
    if c > o and c > prev_o and o < prev_c and prev_c < prev_o:     return "Engulfing haussier"
    if c < o and c < prev_o and o > prev_c and prev_c > prev_o:     return "Engulfing baissier"
    if lower_wick > body * 3:                                        return "Pin Bar haussier"
    if upper_wick > body * 3:                                        return "Pin Bar baissier"
    return "Neutre"

# ─────────────────────────────────────────────────────────────────────────────
# SIGNAL & DIRECTION
# ─────────────────────────────────────────────────────────────────────────────
def determine_direction(ind: dict) -> str | None:
    if abs(ind["vwap_slope"]) < ind["atr"] * 0.1:
        return None

    dist = ind["dist_vwap_sd"]
    if dist > 4.5 or dist < -4.5:
        return None

    price     = ind["price"]
    vl1       = ind["vwap_lower1"]
    vu1       = ind["vwap_upper1"]
    above_200 = ind["above_200"]
    above_50  = ind["above_50"]

    if ind["bounce_bull"]: return "BUY"
    if ind["bounce_bear"]: return "SELL"

    vwap_long  = price < vl1
    vwap_short = price > vu1

    struct_long  = ind["near_swing_sup"] or ind["near_poc"]
    struct_short = ind["near_swing_res"] or ind["near_poc"]

    vol_ok = ind["vol_ratio"] > 1.0

    if vwap_long  and struct_long  and above_200 and above_50  and (vol_ok or ind["ema_cross_bull"]): return "BUY"
    if vwap_short and struct_short and not above_200 and not above_50 and (vol_ok or ind["ema_cross_bear"]): return "SELL"
    if price < ind["vwap_lower2"] and above_200 and vol_ok:    return "BUY"
    if price > ind["vwap_upper2"] and not above_200 and vol_ok: return "SELL"

    return None

def compute_score(ind: dict, direction: str) -> tuple[int, list[str]]:
    score   = 0
    reasons = []
    is_buy  = direction == "BUY"

    dist_sd = abs(ind["dist_vwap_sd"])
    if dist_sd >= 2.0:   score += 30; reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")
    elif dist_sd >= 1.0: score += 18; reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")
    elif dist_sd >= 0.5: score += 8;  reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")

    if is_buy  and ind["vwap_slope"] > 0: score += 5; reasons.append("Pente VWAP haussiere")
    elif not is_buy and ind["vwap_slope"] < 0: score += 5; reasons.append("Pente VWAP baissiere")

    if is_buy:
        if ind["near_swing_sup"]: score += 15; reasons.append(f"Support swing @ {ind['swing_low']}")
        if ind["near_poc"]:       score += 10; reasons.append(f"POC @ {ind['poc']}")
        if ind["price"] < ind["va_low"]: score += 10; reasons.append(f"Sous VAL @ {ind['va_low']}")
        if ind["above_200"]:      score += 5;  reasons.append("Tendance longue haussiere")
    else:
        if ind["near_swing_res"]: score += 15; reasons.append(f"Resistance swing @ {ind['swing_high']}")
        if ind["near_poc"]:       score += 10; reasons.append(f"POC @ {ind['poc']}")
        if ind["price"] > ind["va_high"]: score += 10; reasons.append(f"Au-dessus VAH @ {ind['va_high']}")
        if not ind["above_200"]:  score += 5;  reasons.append("Tendance longue baissiere")

    vr = ind["vol_ratio"]
    if ind["bounce_bull"] or ind["bounce_bear"]: score += 25; reasons.append(f"Rebond structurel (x{vr})")
    elif vr >= 2.0: score += 20; reasons.append(f"Volume spike (x{vr})")
    elif vr >= 1.4: score += 12; reasons.append(f"Volume eleve (x{vr})")
    elif vr >= 1.0: score += 5;  reasons.append(f"Volume correct (x{vr})")

    if is_buy  and ind["ema_cross_bull"]: score += 10; reasons.append("Golden Cross")
    elif not is_buy and ind["ema_cross_bear"]: score += 10; reasons.append("Death Cross")

    bull_patterns = {"Marteau", "Engulfing haussier", "Pin Bar haussier"}
    bear_patterns = {"Shooting Star", "Engulfing baissier", "Pin Bar baissier"}
    if is_buy  and ind["pattern"] in bull_patterns: score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif not is_buy and ind["pattern"] in bear_patterns: score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif ind["pattern"] == "Doji": score += 3; reasons.append("Doji")

    return min(score, 100), reasons

# ─────────────────────────────────────────────────────────────────────────────
# CALCUL TP/SL
# ─────────────────────────────────────────────────────────────────────────────
def calculate_tp_sl(direction: str, entry: float, atr: float,
                    swing_high: float, swing_low: float) -> dict:
    m = MODES_CONFIG[state.get("trading_mode", "optimale")]

    if direction == "BUY":
        sl = min(round(swing_low - atr * 0.2, 2), round(entry - atr * m["sl_mult"], 2))
        sl = min(sl, round(entry - atr * max(m["sl_mult"], 1.5), 2))
        risk = abs(entry - sl)
        if risk > MAX_RISK_PTS:
            log.warning(f"SL cap {risk:.1f} → {MAX_RISK_PTS} pts (ATR={atr})")
            sl   = round(entry - MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS
        return {
            "sl":  sl,
            "tp1": round(entry + risk * m["tp1_mult"], 2),
            "tp2": round(entry + risk * m["tp2_mult"], 2),
            "tp3": round(entry + risk * m["tp3_mult"], 2),
        }
    else:
        sl = max(round(swing_high + atr * 0.2, 2), round(entry + atr * m["sl_mult"], 2))
        sl = max(sl, round(entry + atr * max(m["sl_mult"], 1.5), 2))
        risk = abs(sl - entry)
        if risk > MAX_RISK_PTS:
            log.warning(f"SL cap {risk:.1f} → {MAX_RISK_PTS} pts (ATR={atr})")
            sl   = round(entry + MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS
        return {
            "sl":  sl,
            "tp1": round(entry - risk * m["tp1_mult"], 2),
            "tp2": round(entry - risk * m["tp2_mult"], 2),
            "tp3": round(entry - risk * m["tp3_mult"], 2),
        }

# ─────────────────────────────────────────────────────────────────────────────
# DYNAMIC LOT SIZING — Rapport §3.4
# Modulation du lot en fonction du score, de l'ATR relatif et de l'heure.
# Score 80+ → ×1.5 | Score 45-55 → ×0.7
# ATR > 1.5× médiane 30j → ×0.7 (volatilité anormale)
# Post-midi → ×0.8 (liquidité réduite)
# ─────────────────────────────────────────────────────────────────────────────
def compute_dynamic_lot(score: int, atr: float, h: int) -> float:
    base_lot   = state.get("lot_size", LOT_SIZE)
    multiplier = 1.0

    # Score-based scaling
    if score >= 80:
        multiplier *= 1.5
    elif score <= 55:
        multiplier *= 0.7

    # ATR-based scaling (volatilité relative)
    atr_hist = state.get("atr_history", [])
    if len(atr_hist) >= 20:
        median_atr = float(np.median(atr_hist[-30:]))
        if median_atr > 0 and atr > 1.5 * median_atr:
            multiplier *= 0.7
            log.info(f"Dynamic lot : ATR {atr:.2f} > 1.5×med ({median_atr:.2f}) → ×0.7")

    # Session-based scaling
    if h >= SESSION_MID_H:
        multiplier *= 0.8

    lot = round(base_lot * multiplier, 2)
    lot = max(LOT_MIN, min(LOT_MAX, lot))
    return lot

# ─────────────────────────────────────────────────────────────────────────────
# ENVOI D'ORDRES MT5
# ─────────────────────────────────────────────────────────────────────────────
def _send_order_sync(direction: str, tp: float, sl: float, lot: float) -> dict | None:
    if not MT5_AVAILABLE:
        ticket = int(time.time() * 1000) % 999999
        return {"ticket": ticket, "simulated": True,
                "direction": direction, "tp": tp, "sl": sl, "lot": lot}
    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick       = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return None
    price = tick.ask if direction == "BUY" else tick.bid
    result = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         order_type,
        "price":        price,
        "sl":           sl,
        "tp":           tp,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      "GoldBotV2-Optimale",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    })
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Ordre echoue : {result.retcode} - {result.comment}")
        return None
    return {"ticket": result.order, "direction": direction,
            "entry": price, "tp": tp, "sl": sl, "lot": lot}

# ─────────────────────────────────────────────────────────────────────────────
# OUVERTURE DES POSITIONS — Architecture séquentielle P1/P2/P3 (Rapport §3.3)
# P1 : ouverture immédiate
# P2 : ajoutée à pending_entries, ouverture si prix confirme après 1 bougie M5
# P3 : ajoutée à pending_entries, ouverture si momentum confirme après 2 bougies
# ─────────────────────────────────────────────────────────────────────────────
async def place_orders_async(direction: str, entry: float, levels: dict,
                              atr: float, score: int, h: int) -> list[dict]:
    lot = compute_dynamic_lot(score, atr, h)
    nb  = max(1, min(3, state.get("nb_trades", 3)))
    tps = [levels["tp1"], levels["tp2"], levels["tp3"]]

    results = []

    # P1 : ouverture immédiate
    res = await run_in_mt5_thread(_send_order_sync, direction, tps[0], levels["sl"], lot)
    if res:
        res["tp_target"] = 1
        results.append(res)
        log.info(
            f"P1 ouvert @ {res.get('entry', entry)} | lot={lot} | score={score} | "
            f"TP1={tps[0]} SL={levels['sl']}"
        )

    if nb >= 2:
        # P2 : entrée conditionnelle — confirmation après 1 bougie M5 (5 min)
        confirm_threshold_p2 = round(
            entry + 0.3 * atr if direction == "BUY" else entry - 0.3 * atr, 2
        )
        state["pending_entries"].append({
            "position_number":  2,
            "direction":        direction,
            "entry_signal":     entry,
            "levels":           levels,
            "lot":              lot,
            "tp":               tps[1],
            "confirm_after":    datetime.now() + timedelta(minutes=5),
            "confirm_type":     "price",
            "confirm_threshold": confirm_threshold_p2,
            "atr":              atr,
        })
        log.info(f"P2 en attente | conf_seuil={confirm_threshold_p2} dans 5 min")

    if nb >= 3:
        # P3 : entrée conditionnelle — confirmation de momentum après 2 bougies M5
        state["pending_entries"].append({
            "position_number":  3,
            "direction":        direction,
            "entry_signal":     entry,
            "levels":           levels,
            "lot":              lot,
            "tp":               tps[2],
            "confirm_after":    datetime.now() + timedelta(minutes=10),
            "confirm_type":     "momentum",
            "confirm_threshold": None,  # sera re-calculé sur les indicateurs live
            "atr":              atr,
        })
        log.info("P3 en attente | conf_momentum dans 10 min")

    return results

async def process_pending_entries(ind: dict | None):
    """
    Rapport §3.3 — Séquençage progressif P2/P3.
    Vérifie les entrées en attente et les ouvre si les conditions sont remplies.
    """
    if not state["pending_entries"]:
        return

    now     = datetime.now()
    to_open = []
    to_keep = []

    for pe in state["pending_entries"]:
        if now < pe["confirm_after"]:
            to_keep.append(pe)
            continue

        direction = pe["direction"]
        entry     = pe["entry_signal"]
        atr       = pe["atr"]

        # Vérifier que la séquence parente est encore active
        parent_open = any(not t.get("closed") for t in state.get("active_trades", []))
        if not parent_open:
            log.info(f"P{pe['position_number']} annulee : sequence parente terminee")
            continue

        if pe["confirm_type"] == "price":
            # P2 : prix doit s'être maintenu au-delà du seuil d'inertie
            if ind is None:
                to_keep.append(pe)
                continue
            price = ind["price"]
            threshold = pe["confirm_threshold"]
            if direction == "BUY"  and price >= threshold:
                to_open.append(pe)
                log.info(f"P2 confirmee : prix {price} >= seuil {threshold}")
            elif direction == "SELL" and price <= threshold:
                to_open.append(pe)
                log.info(f"P2 confirmee : prix {price} <= seuil {threshold}")
            else:
                log.info(f"P2 rejetee : prix {price} n'a pas confirme (seuil={threshold})")

        elif pe["confirm_type"] == "momentum":
            # P3 : signal de direction doit être encore actif
            if ind is None:
                to_keep.append(pe)
                continue
            live_dir = determine_direction(ind)
            if live_dir == direction:
                to_open.append(pe)
                log.info(f"P3 confirmee : momentum toujours {direction}")
            else:
                log.info(f"P3 rejetee : momentum perdu (live={live_dir})")

    state["pending_entries"] = to_keep

    for pe in to_open:
        res = await run_in_mt5_thread(
            _send_order_sync, pe["direction"], pe["tp"], pe["levels"]["sl"], pe["lot"]
        )
        if res:
            new_trade = {
                "direction":    pe["direction"],
                "entry":        res.get("entry", pe["entry_signal"]),
                "levels":       pe["levels"],
                "ticket":       res["ticket"],
                "tp_target":    pe["position_number"],
                "tp_alerted":   False,
                "sl_alerted":   False,
                "atr":          pe["atr"],
                "entry_time":   datetime.now(),
                "sl_tightened": False,
            }
            state["active_trades"].append(new_trade)
            log.info(
                f"P{pe['position_number']} ouverte @ {new_trade['entry']} "
                f"lot={pe['lot']} TP={pe['tp']}"
            )

# ─────────────────────────────────────────────────────────────────────────────
# GESTION DES POSITIONS — SL/TP moves
# ─────────────────────────────────────────────────────────────────────────────
def _close_trade_sync(ticket: int) -> bool:
    if not MT5_AVAILABLE:
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos   = positions[0]
    ct    = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick  = mt5.symbol_info_tick(SYMBOL)
    price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
    result = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       pos.volume,
        "type":         ct,
        "position":     ticket,
        "price":        price,
        "deviation":    20,
        "magic":        MAGIC_NUMBER,
        "comment":      "GoldBotV2 Close",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE

async def close_trade(ticket: int) -> bool:
    return await run_in_mt5_thread(_close_trade_sync, ticket)

def _move_sl_sync(ticket: int, new_sl: float) -> bool:
    if not MT5_AVAILABLE:
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos    = positions[0]
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos.tp,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE

async def move_sl(ticket: int, new_sl: float) -> bool:
    return await run_in_mt5_thread(_move_sl_sync, ticket, new_sl)

async def close_all_trades() -> tuple[int, int]:
    positions = await get_open_positions()
    if not positions:
        return 0, 0
    ok = err = 0
    for p in positions:
        if await close_trade(p["ticket"]):
            ok += 1
        else:
            err += 1
    state["active_trades"]  = []
    state["pending_entries"] = []
    return ok, err

def _is_open_sync(ticket: int) -> bool:
    if not MT5_AVAILABLE:
        return True
    pos = mt5.positions_get(ticket=ticket)
    return pos is not None and len(pos) > 0

def _get_closed_pnl_sync(ticket: int) -> tuple[float, float | None]:
    if not MT5_AVAILABLE:
        return 0.0, None
    try:
        time_to   = datetime.now() + timedelta(days=1)
        time_from = datetime.now() - timedelta(days=3)
        deals     = mt5.history_deals_get(time_from, time_to)
        if not deals:
            return 0.0, None
        for deal in reversed(deals):
            if deal.position_id == ticket and deal.entry == mt5.DEAL_ENTRY_OUT:
                return round(deal.profit, 2), round(deal.price, 2)
    except Exception as e:
        log.error(f"_get_closed_pnl_sync : {e}")
    return 0.0, None

# ─────────────────────────────────────────────────────────────────────────────
# MONITORING DES TRADES ACTIFS — Delayed SL inclus (Rapport §3.5)
# ─────────────────────────────────────────────────────────────────────────────
async def monitor_active_trades():
    trades = state.get("active_trades", [])
    if not trades:
        return

    bid, _ = await get_current_price()
    price  = bid
    now    = datetime.now()

    for trade in trades:
        if trade.get("closed"):
            continue

        direction = trade["direction"]
        levels    = trade["levels"]
        ticket    = trade.get("ticket", 0)
        tp_target = trade.get("tp_target", 1)
        entry     = trade.get("entry", 0)
        atr       = trade.get("atr")
        tp_price  = levels.get(f"tp{tp_target}", 0)

        # ── Delayed SL (Rapport §3.5) ──────────────────────────────────
        # Après DELAYED_SL_CANDLES bougies M5 (10 min), resserrer le SL
        # de entry ± 1.5×ATR à entry ± 1.0×ATR si toujours ouvert
        if (
            atr is not None
            and not trade.get("sl_tightened", False)
            and trade.get("entry_time") is not None
        ):
            elapsed_min = (now - trade["entry_time"]).total_seconds() / 60
            if elapsed_min >= DELAYED_SL_CANDLES * 5:
                if direction == "BUY":
                    new_sl = round(entry - DELAYED_SL_MULT * atr, 2)
                    if new_sl > levels["sl"]:  # Seulement si on resserre
                        ok = await move_sl(ticket, new_sl)
                        if ok:
                            levels["sl"] = new_sl
                            trade["sl_tightened"] = True
                            log.info(f"Delayed SL serré #{ticket} : {levels['sl']} → {new_sl}")
                else:
                    new_sl = round(entry + DELAYED_SL_MULT * atr, 2)
                    if new_sl < levels["sl"]:  # Seulement si on resserre
                        ok = await move_sl(ticket, new_sl)
                        if ok:
                            levels["sl"] = new_sl
                            trade["sl_tightened"] = True
                            log.info(f"Delayed SL serré #{ticket} : {levels['sl']} → {new_sl}")

        is_open = await run_in_mt5_thread(_is_open_sync, ticket)

        if not is_open:
            real_pnl, real_exit = await run_in_mt5_thread(_get_closed_pnl_sync, ticket)
            tol    = 5.0
            tp_hit = real_exit is not None and abs(real_exit - tp_price) <= tol
            sl_hit = real_exit is not None and abs(real_exit - levels["sl"]) <= tol
            manual = not tp_hit and not sl_hit

            if manual or ticket in state["manual_closes"]:
                trade["closed"] = True; trade["result"] = "MANUEL"
                state["manual_closes"].discard(ticket)
                if real_pnl != 0:
                    state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
                log.info(f"Trade #{ticket} ferme manuellement. PnL : {real_pnl}")
                continue

            if tp_hit:
                await _handle_tp(trade, trades, tp_target, tp_price, entry, real_pnl)
            elif sl_hit:
                await _handle_sl(trade, ticket, entry, levels, price, real_pnl)

    if trades and all(t.get("closed") for t in trades):
        pnl_seq = round(sum(t.get("pnl_final", 0) for t in trades), 2)
        state["active_trades"] = []
        log.info(f"Sequence terminee. PnL: {pnl_seq}. PnL jour: {state['daily_pnl']}")

# ─────────────────────────────────────────────────────────────────────────────
# GESTION TP — Trailing Stop ATR (Rapport §3.2)
# Remplace le Breakeven fixe (SL → entry) par un trailing dynamique :
#   Après TP1 : SL = entry ± 0.5×ATR  (gain plancher garanti, non zéro)
#   Après TP2 : SL = TP1 + 30% du move TP1→TP2 (lock-in progressif)
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_tp(trade, trades, tp_target, tp_price, entry, real_pnl):
    ticket             = trade["ticket"]
    direction          = trade["direction"]
    atr                = trade.get("atr")
    trade["closed"]    = True
    trade["result"]    = f"TP{tp_target}"
    trade["pnl_final"] = real_pnl
    state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
    state["wins"]     += 1
    state["consecutive_sl"] = 0  # Reset compteur SL consécutifs
    state["current_streak"] = state["current_streak"] + 1 if state["current_streak"] >= 0 else 1
    if real_pnl > state["best_trade"]:
        state["best_trade"] = real_pnl

    pips = round(abs(tp_price - entry), 2)
    wr   = _winrate()
    still = sum(1 for t in trades if not t.get("closed") and t["ticket"] != ticket)

    if tp_target == 1:
        # ── Trailing stop ATR — §3.2 ──────────────────────────────────
        # Au lieu de SL = entry (BE pur, gain = 0),
        # on déplace à entry + 0.5×ATR pour garantir un gain positif.
        if atr and atr > 0:
            if direction == "BUY":
                trailing_sl = round(entry + ATR_TRAIL_P1 * atr, 2)
            else:
                trailing_sl = round(entry - ATR_TRAIL_P1 * atr, 2)
        else:
            trailing_sl = entry  # Fallback BE si ATR inconnu
            log.warning(f"ATR inconnu pour #{ticket} — fallback BE pur")

        for t in trades:
            if not t.get("closed") and t["ticket"] != ticket:
                ok = await move_sl(t["ticket"], trailing_sl)
                if ok:
                    t["levels"]["sl"] = trailing_sl
        log.info(
            f"TP1 touche. +{pips} pts. Trailing SL → {trailing_sl} "
            f"(+{ATR_TRAIL_P1}×ATR) sur {still} trades. PnL: {state['daily_pnl']} WR: {wr}%"
        )

    elif tp_target == 2:
        # ── Lock-in progressif §3.2 — SL P3 = TP1 + 30%(TP2-TP1) ────
        tp1_price = trade["levels"].get("tp1", entry)
        tp2_price = trade["levels"].get("tp2", entry)
        if direction == "BUY":
            lock_sl = round(tp1_price + TP2_LOCK_RATIO * (tp2_price - tp1_price), 2)
        else:
            lock_sl = round(tp1_price - TP2_LOCK_RATIO * (tp1_price - tp2_price), 2)

        for t in trades:
            if not t.get("closed") and t["ticket"] != ticket:
                ok = await move_sl(t["ticket"], lock_sl)
                if ok:
                    t["levels"]["sl"] = lock_sl
        log.info(
            f"TP2 touche. +{pips} pts. Lock-in SL P3 → {lock_sl} "
            f"(TP1+{int(TP2_LOCK_RATIO*100)}% range). PnL: {state['daily_pnl']} WR: {wr}%"
        )
    else:
        log.info(f"TP3 touche. +{pips} pts. PnL: {state['daily_pnl']} WR: {wr}%")

# ─────────────────────────────────────────────────────────────────────────────
# GESTION SL — Cooldown adaptatif cumulatif (Rapport §3.6)
# Après chaque SL, consecutive_sl++ et cooldown effective × 1.5
# Après 3 SL consécutifs → session_stopped = True (circuit breaker)
# ─────────────────────────────────────────────────────────────────────────────
async def _handle_sl(trade, ticket, entry, levels, price, real_pnl):
    if ticket in state["manual_closes"]:
        trade["closed"] = True; trade["result"] = "MANUEL"
        state["manual_closes"].discard(ticket)
        return
    trade["closed"]    = True
    trade["result"]    = "SL"
    trade["pnl_final"] = real_pnl
    state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
    state["losses"]   += 1
    state["last_sl_time"]    = time.time()
    state["consecutive_sl"] += 1
    state["current_streak"]  = state["current_streak"] - 1 if state["current_streak"] <= 0 else -1
    if real_pnl < state["worst_trade"]:
        state["worst_trade"] = real_pnl

    consec = state["consecutive_sl"]
    wr     = _winrate()
    log.warning(
        f"SL touche #{ticket}. Entree {entry} Sortie {price}. "
        f"PnL: {state['daily_pnl']} WR: {wr}% | SL consec: {consec}"
    )

    # Circuit breaker — 3 SL consécutifs → arrêt session
    if consec >= MAX_CONSECUTIVE_SL:
        state["session_stopped"] = True
        log.warning(
            f"CIRCUIT BREAKER : {consec} SL consecutifs — "
            f"Trading suspendu jusqu'a demain."
        )
    else:
        eff_cd = int(MODES_CONFIG[state.get("trading_mode", "optimale")]["cooldown"]
                     * (COOLDOWN_SL_MULTIPLIER ** consec))
        log.info(f"Cooldown adaptatif apres SL #{consec} : {eff_cd}s")

def _winrate() -> int:
    total = state["wins"] + state["losses"]
    return round(state["wins"] / total * 100) if total > 0 else 0

# ─────────────────────────────────────────────────────────────────────────────
# BOUCLE PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────
async def analysis_loop():
    log.info("Gold Bot v2 Headless OPTIMALE — Boucle M5 demarree")

    _cache_m5    = None
    _last_reload = 0.0
    RELOAD_INTERVAL = 60

    while True:
        try:
            today = date.today()

            # Reset quotidien
            if state["last_trade_date"] != today:
                for k in ("trades_today", "daily_pnl", "total_signals_sent",
                          "wins", "losses", "best_trade", "worst_trade", "current_streak"):
                    state[k] = 0 if isinstance(state[k], int) else 0.0
                state["history"]         = []
                state["manual_closes"]   = set()
                state["last_trade_date"] = today
                state["session_stopped"] = False   # Réinitialise le circuit breaker
                state["consecutive_sl"]  = 0
                state["pending_entries"] = []
                _last_reload = 0.0
                log.info(f"Reset quotidien - {today} | Mode: {state['trading_mode'].upper()}")

            if state["paused"]:
                await asyncio.sleep(5)
                continue

            # ── Circuit breaker §3.6 ─────────────────────────────────────
            if state.get("session_stopped"):
                if int(time.time()) % 300 == 0:
                    log.warning("Session stoppee (circuit breaker 3×SL). Reprise demain.")
                await asyncio.sleep(60)
                continue

            # Monitoring des trades actifs
            if state.get("active_trades"):
                await monitor_active_trades()
                await asyncio.sleep(2)
                continue

            # ── Traitement des entrées séquentielles en attente §3.3 ────
            ind_for_pending = None
            if state.get("pending_entries"):
                if _cache_m5 is not None:
                    ind_for_pending = compute_indicators(_cache_m5.tail(150).reset_index(drop=True))
                await process_pending_entries(ind_for_pending)

            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                await asyncio.sleep(10)
                continue

            # ── Daily stop §2 ───────────────────────────────────────────
            if state["daily_pnl"] < DAILY_MAX_LOSS:
                if int(time.time()) % 300 == 0:
                    log.warning(
                        f"DAILY STOP : PnL={state['daily_pnl']:.2f} < {DAILY_MAX_LOSS}. "
                        f"Trading suspendu jusqu'a demain."
                    )
                await asyncio.sleep(60)
                continue

            m        = MODES_CONFIG[state.get("trading_mode", "optimale")]
            base_cd  = state["cooldown_override"] if state["cooldown_override"] is not None else m["cooldown"]

            # ── Cooldown adaptatif cumulatif §3.6 ──────────────────────
            consec   = state.get("consecutive_sl", 0)
            mode_cd  = int(base_cd * (COOLDOWN_SL_MULTIPLIER ** min(consec, 5)))
            time_since = time.time() - state["last_signal_time"]
            if time_since < mode_cd:
                await asyncio.sleep(5)
                continue

            spread = await get_spread()
            if spread > MAX_SPREAD:
                await asyncio.sleep(5)
                continue

            h = datetime.now().hour

            # Hard block après 15h
            if h >= SESSION_END_H:
                if int(time.time()) % 300 == 0:
                    log.info(f"Hors session ({h}h) - Trading desactive.")
                await asyncio.sleep(60)
                continue

            # Threshold de base + ajustements
            threshold = m["score_min"]
            if h >= SESSION_MID_H:
                threshold += 10
            if time.time() - state.get("last_sl_time", 0) < 300:
                threshold += 20
                log.info(f"Post-SL cooldown actif → threshold={threshold}")

            # Rechargement des bougies M5
            now = time.time()
            if now - _last_reload >= RELOAD_INTERVAL:
                df_m5 = await get_candles(TIMEFRAME_M5, 200)
                if df_m5 is not None and len(df_m5) >= 50:
                    _cache_m5    = df_m5
                    _last_reload = now
                    log.info(
                        f"M5 recharge | {len(df_m5)} bougies | "
                        f"Prix: {round(df_m5['Close'].iloc[-1],2)} | Spread: {spread} | Mode: {m['label']}"
                    )
                    # ── MAJ historique ATR §3.4 ──────────────────────────
                    atr_now = compute_atr(df_m5.tail(50).reset_index(drop=True))
                    state["atr_history"].append(atr_now)
                    if len(state["atr_history"]) > 200:
                        state["atr_history"] = state["atr_history"][-200:]
                else:
                    log.warning("MT5 n'a pas renvoye de bougies M5")
                    await asyncio.sleep(10)
                    continue

            if _cache_m5 is None:
                await asyncio.sleep(5)
                continue

            ind       = compute_indicators(_cache_m5.tail(150).reset_index(drop=True))
            direction = determine_direction(ind)

            if direction is None:
                if int(time.time()) % 30 == 0:
                    log.info(
                        f"M5 Scan | Prix:{ind['price']} VWAP:{ind['vwap']} "
                        f"dist:{ind['dist_vwap_sd']:.1f}SD | "
                        f"VolX{ind['vol_ratio']} | BounceUP:{ind['bounce_bull']} DOWN:{ind['bounce_bear']}"
                    )
            else:
                score, reasons = compute_score(ind, direction)

                if int(time.time()) % 10 == 0:
                    lot_preview = compute_dynamic_lot(score, ind["atr"], h)
                    log.info(
                        f"Signal {direction} | score={score}/{threshold} | "
                        f"VWAP={ind['vwap']} ({ind['dist_vwap_sd']:.1f}SD) | "
                        f"POC={ind['poc']} near={ind['near_poc']} | "
                        f"VolX{ind['vol_ratio']} | lot_calc={lot_preview} | "
                        f"{reasons[0] if reasons else ''}"
                    )

                if score >= threshold:
                    levels = calculate_tp_sl(
                        direction, ind["price"], ind["atr"],
                        ind["swing_high"], ind["swing_low"]
                    )

                    log.info(
                        f"Execution {direction} @ {ind['price']} | score={score}/{threshold} | "
                        f"SL={levels['sl']} TP1={levels['tp1']} TP2={levels['tp2']} TP3={levels['tp3']} | "
                        f"ATR={ind['atr']}"
                    )

                    results = await place_orders_async(
                        direction, ind["price"], levels, ind["atr"], score, h
                    )

                    if results:
                        state["active_trades"] = [
                            {
                                "direction":    direction,
                                "entry":        ind["price"],
                                "levels":       levels,
                                "ticket":       r["ticket"],
                                "tp_target":    r["tp_target"],
                                "tp_alerted":   False,
                                "sl_alerted":   False,
                                "atr":          ind["atr"],
                                "entry_time":   datetime.now(),
                                "sl_tightened": False,
                            }
                            for r in results
                        ]
                        state["trades_today"]       += 1
                        state["last_signal_time"]    = time.time()
                        state["total_signals_sent"] += 1
                        _last_reload = 0.0

        except Exception as e:
            log.error(f"Erreur boucle : {e}", exc_info=True)

        ind = None
        gc.collect()
        await asyncio.sleep(5)

# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    await run_in_mt5_thread(_mt5_connect)
    log.info(
        "Gold Bot v2 — Mode OPTIMALE demarre | "
        "RR 1.5+ | Trailing ATR | Dynamic Lot | Cooldown adaptatif"
    )
    await restore_state_on_startup()
    try:
        await analysis_loop()
    finally:
        _mt5_disconnect()
        _MT5_EXECUTOR.shutdown(wait=False)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Extinction du bot")
