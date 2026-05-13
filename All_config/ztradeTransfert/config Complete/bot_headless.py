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
        "desc":       "SL minimum, haute frequence",
    },
}

state = {
    "trades_today":       0,
    "active_trades":      [],
    "last_signal_time":   0,
    "daily_pnl":          0.0,
    "total_signals_sent": 0,
    "last_trade_date":    None,
    "paused":             False,
    "trading_mode":       "risque",
    "wins":               0,
    "losses":             0,
    "best_trade":         0.0,
    "worst_trade":        0.0,
    "current_streak":     0,
    "history":            [],
    "manual_closes":      set(),
    "nb_trades":          3,
    "lot_size":           LOT_SIZE,
    "cooldown_override":  None,
    "last_sl_time":       0,
}

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES DE RISQUE
# ─────────────────────────────────────────────────────────────────────────────
MAX_RISK_PTS   = 22.0    # [FIX SL CAP] Jamais plus de 22 pts de SL sur XAU
DAILY_MAX_LOSS = -150.0  # [FIX DAILY STOP] Coupe-circuit perte journalière
SESSION_END_H  = 15      # [FIX CUTOFF] Aucun nouveau trade à partir de 15h
SESSION_MID_H  = 12      # [FIX THRESHOLD] Threshold +10 à partir de midi

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
# [FIX RESTORE] Restauration des trades ouverts au redémarrage
# Sans ce fix : au restart, trades_today=0 et active_trades=[] → le bot
# ignore les positions déjà ouvertes et peut en ouvrir de nouvelles par-dessus.
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
            "direction":  p["direction"],
            "entry":      p["entry"],
            "levels":     {
                "sl":  p["sl"],
                "tp1": p["tp"],
                "tp2": p["tp"],
                "tp3": p["tp"],
            },
            "ticket":     p["ticket"],
            "tp_target":  1,
            "tp_alerted": False,
            "sl_alerted": False,
        })

    state["active_trades"] = restored
    # On estime qu'une séquence = 3 sous-trades (1/3, 2/3, 3/3)
    state["trades_today"] = max(state["trades_today"], (len(positions) + 2) // 3)
    log.info(
        f"Restauration complete : {len(restored)} trade(s) repris. "
        f"trades_today={state['trades_today']}"
    )

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

    poc_idx         = volumes.argmax()
    poc             = (edges[poc_idx] + edges[poc_idx + 1]) / 2
    total_vol       = volumes.sum()
    target_vol      = total_vol * 0.70

    sorted_idx      = np.argsort(volumes)[::-1]
    acc             = 0
    va_indices      = []

    for idx in sorted_idx:
        acc += volumes[idx]
        va_indices.append(idx)
        if acc >= target_vol:
            break

    va_high = max((edges[i] + edges[i + 1]) / 2 for i in va_indices)
    va_low  = min((edges[i] + edges[i + 1]) / 2 for i in va_indices)
    return {"poc": round(poc, 2), "value_area_high": round(va_high, 2), "value_area_low": round(va_low, 2)}

def find_swing_levels(df: pd.DataFrame, lookback: int = 30) -> tuple[float, float]:
    recent      = df.tail(lookback)
    swing_high  = recent["High"].max()
    swing_low   = recent["Low"].min()
    return round(swing_high, 2), round(swing_low, 2)

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
    vwap_upper1  = round(vwap + 1.0 * vwap_std, 2)
    vwap_lower1  = round(vwap - 1.0 * vwap_std, 2)
    vwap_upper2  = round(vwap + 2.0 * vwap_std, 2)
    vwap_lower2  = round(vwap - 2.0 * vwap_std, 2)

    dist_vwap    = round(price - vwap, 2)
    dist_vwap_sd = round(dist_vwap / vwap_std, 2) if vwap_std > 0 else 0.0

    vwap_slope = round(vwap_series.diff(5).iloc[-1], 2)

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

    bounce_bull = (
        near_swing_support
        and cur_bull
        and cur_body  > atr * 0.5
        and vol_ratio > 1.4
        and move10    < -atr * 1.5
        and not above_200 or (above_200 and near_poc)
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

def determine_direction(ind: dict) -> str | None:
    # ─────────────────────────────────────────────────────────────────────
    # [FIX VWAP SLOPE] Filtre de distribution : interdit de trader si le
    # VWAP est plat. Une pente < 10% de l'ATR indique un range sans force
    # directionnelle → bruit aléatoire, pas de tendance exploitable.
    # Source : approche de l'autre analyste, adoptée telle quelle.
    # ─────────────────────────────────────────────────────────────────────
    if abs(ind["vwap_slope"]) < ind["atr"] * 0.1:
        return None

    # ─────────────────────────────────────────────────────────────────────
    # [FIX SD CAP] Blocage des BUYs/SELLs à extension extrême (>4.5 SD).
    # Au-delà de 4.5 SD, le marché est en mode "momentum débridé" : le
    # retour vers VWAP peut être violent et imprévisible. On ne scalpe pas
    # un rubber-band tendu à l'extrême.
    # ─────────────────────────────────────────────────────────────────────
    dist = ind["dist_vwap_sd"]
    if dist > 4.5 or dist < -4.5:
        return None

    price     = ind["price"]
    vl1       = ind["vwap_lower1"]
    vu1       = ind["vwap_upper1"]
    above_200 = ind["above_200"]
    above_50  = price > ind["ema50"]   # [FIX EMA50] Alignement court terme

    if ind["bounce_bull"]:
        return "BUY"
    if ind["bounce_bear"]:
        return "SELL"

    vwap_long  = price < vl1
    vwap_short = price > vu1

    struct_long  = ind["near_swing_sup"] or ind["near_poc"]
    struct_short = ind["near_swing_res"] or ind["near_poc"]

    vol_ok = ind["vol_ratio"] > 1.0
    pa_ok  = ind["pattern"] in ("Marteau", "Engulfing haussier", "Pin Bar haussier",
                                  "Shooting Star", "Engulfing baissier", "Pin Bar baissier")

    # ─────────────────────────────────────────────────────────────────────
    # [FIX EMA50] On exige maintenant l'alignement MACRO (EMA200) ET MICRO
    # (EMA50). Cela empêche les achats ("dip buys") quand le prix est déjà
    # sous l'EMA50 : contexte de retournement baissier court terme même si
    # la tendance longue reste haussière.
    # Source : approche de l'autre analyste, adoptée telle quelle.
    # ─────────────────────────────────────────────────────────────────────
    if vwap_long  and struct_long  and above_200 and above_50 and (vol_ok or ind["ema_cross_bull"]):
        return "BUY"
    if vwap_short and struct_short and not above_200 and not above_50 and (vol_ok or ind["ema_cross_bear"]):
        return "SELL"

    if price < ind["vwap_lower2"] and above_200 and vol_ok:
        return "BUY"
    if price > ind["vwap_upper2"] and not above_200 and vol_ok:
        return "SELL"

    return None

def compute_score(ind: dict, direction: str) -> tuple[int, list[str]]:
    score   = 0
    reasons = []
    is_buy  = direction == "BUY"

    dist_sd = abs(ind["dist_vwap_sd"])
    if dist_sd >= 2.0:
        score += 30; reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")
    elif dist_sd >= 1.0:
        score += 18; reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")
    elif dist_sd >= 0.5:
        score += 8;  reasons.append(f"Prix a {dist_sd:.1f} SD du VWAP")

    if is_buy  and ind["vwap_slope"] > 0:
        score += 5; reasons.append("Pente VWAP haussiere")
    elif not is_buy and ind["vwap_slope"] < 0:
        score += 5; reasons.append("Pente VWAP baissiere")

    if is_buy:
        if ind["near_swing_sup"]:
            score += 15; reasons.append(f"Support swing @ {ind['swing_low']}")
        if ind["near_poc"]:
            score += 10; reasons.append(f"POC du volume profile @ {ind['poc']}")
        if ind["price"] < ind["va_low"]:
            score += 10; reasons.append(f"Sous Value Area Low @ {ind['va_low']}")
        if ind["above_200"]:
            score += 5;  reasons.append("Tendance longue haussiere")
    else:
        if ind["near_swing_res"]:
            score += 15; reasons.append(f"Resistance swing @ {ind['swing_high']}")
        if ind["near_poc"]:
            score += 10; reasons.append(f"POC du volume profile @ {ind['poc']}")
        if ind["price"] > ind["va_high"]:
            score += 10; reasons.append(f"Au-dessus Value Area High @ {ind['va_high']}")
        if not ind["above_200"]:
            score += 5;  reasons.append("Tendance longue baissiere")

    vr = ind["vol_ratio"]
    if ind["bounce_bull"] or ind["bounce_bear"]:
        score += 25; reasons.append(f"Rebond structurel sur volume (x{vr})")
    elif vr >= 2.0:
        score += 20; reasons.append(f"Volume spike (x{vr})")
    elif vr >= 1.4:
        score += 12; reasons.append(f"Volume eleve (x{vr})")
    elif vr >= 1.0:
        score += 5;  reasons.append(f"Volume correct (x{vr})")

    if is_buy  and ind["ema_cross_bull"]:
        score += 10; reasons.append("Golden Cross")
    elif not is_buy and ind["ema_cross_bear"]:
        score += 10; reasons.append("Death Cross")

    bull_patterns = {"Marteau", "Engulfing haussier", "Pin Bar haussier"}
    bear_patterns = {"Shooting Star", "Engulfing baissier", "Pin Bar baissier"}
    if is_buy  and ind["pattern"] in bull_patterns:
        score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif not is_buy and ind["pattern"] in bear_patterns:
        score += 15; reasons.append(f"Pattern : {ind['pattern']}")
    elif ind["pattern"] == "Doji":
        score += 3;  reasons.append("Doji")

    return min(score, 100), reasons

def calculate_tp_sl(direction: str, entry: float, atr: float,
                    swing_high: float, swing_low: float) -> dict:
    m = MODES_CONFIG[state.get("trading_mode", "risque")]

    if direction == "BUY":
        sl_structural = round(swing_low - atr * 0.2, 2)
        sl_atr        = round(entry - atr * m["sl_mult"], 2)
        sl            = min(sl_structural, sl_atr)
        sl_floor      = round(entry - atr * max(m["sl_mult"], 1.5), 2)
        sl            = min(sl, sl_floor)
        risk          = abs(entry - sl)

        # ─────────────────────────────────────────────────────────────────
        # [FIX SL CAP] Plafonnement absolu du risque par trade à MAX_RISK_PTS.
        # Sans ce fix : avec un ATR élevé (ex: 23 pts), le SL peut atteindre
        # 42 pts (trade 10:02 = -108 par sous-trade, -324 total).
        # Avec ce fix : risque max = 22 pts = -66 total au pire.
        # ─────────────────────────────────────────────────────────────────
        if risk > MAX_RISK_PTS:
            log.warning(
                f"SL trop large ({risk:.1f} pts) → plafonne a {MAX_RISK_PTS} pts "
                f"(ATR={atr}, entry={entry})"
            )
            sl   = round(entry - MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS

        return {
            "sl":  sl,
            "tp1": round(entry + risk * m["tp1_mult"], 2),
            "tp2": round(entry + risk * m["tp2_mult"], 2),
            "tp3": round(entry + risk * m["tp3_mult"], 2),
        }
    else:
        sl_structural = round(swing_high + atr * 0.2, 2)
        sl_atr        = round(entry + atr * m["sl_mult"], 2)
        sl            = max(sl_structural, sl_atr)
        sl_ceil       = round(entry + atr * max(m["sl_mult"], 1.5), 2)
        sl            = max(sl, sl_ceil)
        risk          = abs(sl - entry)

        # [FIX SL CAP] Même plafonnement côté SELL
        if risk > MAX_RISK_PTS:
            log.warning(
                f"SL trop large ({risk:.1f} pts) → plafonne a {MAX_RISK_PTS} pts "
                f"(ATR={atr}, entry={entry})"
            )
            sl   = round(entry + MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS

        return {
            "sl":  sl,
            "tp1": round(entry - risk * m["tp1_mult"], 2),
            "tp2": round(entry - risk * m["tp2_mult"], 2),
            "tp3": round(entry - risk * m["tp3_mult"], 2),
        }

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
        "comment":      "GoldScalpBotV2",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    })
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Ordre echoue : {result.retcode} - {result.comment}")
        return None
    return {"ticket": result.order, "direction": direction,
            "entry": price, "tp": tp, "sl": sl, "lot": lot}

async def place_orders_async(direction: str, entry: float, levels: dict) -> list[dict]:
    lot = state.get("lot_size", LOT_SIZE)
    nb  = max(1, min(3, state.get("nb_trades", 3)))
    tps = [levels["tp1"], levels["tp2"], levels["tp3"]]
    results = []
    for i in range(nb):
        res = await run_in_mt5_thread(_send_order_sync, direction, tps[i], levels["sl"], lot)
        if res:
            results.append({**res, "tp_target": i + 1})
            log.info(f"Trade place {i+1}/{nb} @ {res.get('entry', entry)} TP{i+1}={tps[i]}")
        else:
            log.error(f"Trade {i+1}/{nb} echoue")
    return results

def _close_trade_sync(ticket: int) -> bool:
    if not MT5_AVAILABLE:
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos  = positions[0]
    ct   = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(SYMBOL)
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
        "comment":      "GoldScalpBotV2 Close",
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
    state["active_trades"] = []
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

async def monitor_active_trades():
    trades = state.get("active_trades", [])
    if not trades:
        return

    bid, _ = await get_current_price()
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

        is_open = await run_in_mt5_thread(_is_open_sync, ticket)

        if not is_open:
            real_pnl, real_exit = await run_in_mt5_thread(_get_closed_pnl_sync, ticket)
            tol      = 5.0
            tp_hit   = real_exit is not None and abs(real_exit - tp_price) <= tol
            sl_hit   = real_exit is not None and abs(real_exit - levels["sl"]) <= tol
            manual   = not tp_hit and not sl_hit

            if manual or ticket in state["manual_closes"]:
                trade["closed"] = True; trade["result"] = "MANUEL"
                state["manual_closes"].discard(ticket)
                if real_pnl != 0:
                    state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
                log.info(f"Trade #{ticket} ferme manuellement. PnL : {real_pnl}")
                continue

            if tp_hit:
                await _handle_tp(trade, trades, tp_target, tp_price, entry, real_pnl or 0)
            elif sl_hit:
                await _handle_sl(trade, ticket, entry, levels, price, real_pnl or 0)

    if trades and all(t.get("closed") for t in trades):
        pnl_seq  = round(sum(t.get("pnl_final", 0) for t in trades), 2)
        state["active_trades"] = []
        log.info(f"Sequence terminee. PnL: {pnl_seq}. PnL jour: {state['daily_pnl']}")

async def _handle_tp(trade, trades, tp_target, tp_price, entry, real_pnl):
    ticket       = trade["ticket"]
    trade["closed"]    = True
    trade["result"]    = f"TP{tp_target}"
    trade["pnl_final"] = real_pnl
    state["daily_pnl"] = round(state["daily_pnl"] + real_pnl, 2)
    state["wins"]     += 1
    state["current_streak"] = state["current_streak"] + 1 if state["current_streak"] >= 0 else 1
    if real_pnl > state["best_trade"]:
        state["best_trade"] = real_pnl
    pips = round(abs(tp_price - entry), 2)
    wr   = _winrate()

    if tp_target == 1:
        still = sum(1 for t in trades if not t.get("closed") and t["ticket"] != ticket)
        for t in trades:
            if not t.get("closed") and t["ticket"] != ticket:
                await move_sl(t["ticket"], entry)
        log.info(f"TP1 touche. +{pips} pts. SL mis a breakeven sur {still} trades. PnL: {state['daily_pnl']} WR: {wr}%")
    elif tp_target == 2:
        tp1_price = trade["levels"].get("tp1", entry)
        for t in trades:
            if not t.get("closed") and t["ticket"] != ticket:
                await move_sl(t["ticket"], tp1_price)
        log.info(f"TP2 touche. +{pips} pts. SL trade 3 a TP1. PnL: {state['daily_pnl']} WR: {wr}%")
    else:
        log.info(f"TP3 touche. +{pips} pts. PnL: {state['daily_pnl']} WR: {wr}%")

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
    state["last_sl_time"] = time.time()
    state["current_streak"] = state["current_streak"] - 1 if state["current_streak"] <= 0 else -1
    if real_pnl < state["worst_trade"]:
        state["worst_trade"] = real_pnl
    wr = _winrate()
    log.info(f"SL touche. Ticket: #{ticket}. Entree {entry} Sortie {price}. PnL: {state['daily_pnl']} WR: {wr}%")

def _winrate() -> int:
    total = state["wins"] + state["losses"]
    return round(state["wins"] / total * 100) if total > 0 else 0

async def analysis_loop():
    log.info("Gold Bot v2 Headless - Boucle M5 demarree (scan 5s)")

    _cache_m5    = None
    _cache_m15   = None
    _cache_h1    = None
    _last_reload = 0.0
    RELOAD_INTERVAL = 60

    while True:
        try:
            today = date.today()
            if state["last_trade_date"] != today:
                for k in ("trades_today","daily_pnl","total_signals_sent","wins",
                          "losses","best_trade","worst_trade","current_streak"):
                    state[k] = 0 if isinstance(state[k], (int, float)) else 0.0
                state["history"]         = []
                state["manual_closes"]   = set()
                state["last_trade_date"] = today
                _last_reload = 0.0
                log.info(f"Reset quotidien - {today}")

            if state["paused"]:
                await asyncio.sleep(5)
                continue

            if state.get("active_trades"):
                await monitor_active_trades()
                await asyncio.sleep(2)
                continue

            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                await asyncio.sleep(10)
                continue

            # ─────────────────────────────────────────────────────────────
            # [FIX DAILY STOP] Coupe-circuit perte journalière.
            # Si le PnL journalier tombe sous DAILY_MAX_LOSS (= -150),
            # on suspend toute nouvelle prise de position jusqu'au lendemain.
            # Empêche la spirale de pertes cumulées.
            # ─────────────────────────────────────────────────────────────
            if state["daily_pnl"] < DAILY_MAX_LOSS:
                if int(time.time()) % 300 == 0:
                    log.warning(
                        f"DAILY STOP : PnL={state['daily_pnl']:.2f} < {DAILY_MAX_LOSS}. "
                        f"Trading suspendu jusqu'a demain."
                    )
                await asyncio.sleep(60)
                continue

            m       = MODES_CONFIG[state.get("trading_mode", "risque")]
            mode_cd = state["cooldown_override"] if state["cooldown_override"] is not None else m["cooldown"]
            time_since = time.time() - state["last_signal_time"]
            if time_since < mode_cd:
                await asyncio.sleep(5)
                continue

            spread = await get_spread()
            if spread > MAX_SPREAD:
                await asyncio.sleep(5)
                continue

            # ─────────────────────────────────────────────────────────────
            # [FIX CUTOFF + THRESHOLD]
            # Remplace l'ancienne logique qui RÉDUISAIT le threshold
            # l'après-midi (ce qui forçait des trades médiocres).
            #
            # Nouvelle logique :
            #   - h >= SESSION_END_H (15h) → hard block, aucun nouveau trade
            #   - h >= SESSION_MID_H (12h) → threshold +10 (plus sélectif)
            #   - Sinon → threshold normal (session principale 5h-12h)
            #   - Post-SL récent (<5 min) → threshold +20 supplémentaire
            #
            # Impact sur les trades d'aujourd'hui :
            #   14:54 score=45, threshold=45+10=55 → BLOQUÉ ✓
            #   12:19 score=55, threshold=45+10=55 → passe (juste) ✓
            #   05:31 score=63, threshold=45       → passe ✓
            #   10:02 score=75, threshold=45       → passe (géré par SL cap) ✓
            # ─────────────────────────────────────────────────────────────
            h = datetime.now().hour

            if h >= SESSION_END_H:
                # Hard block total après 15h
                if int(time.time()) % 300 == 0:
                    log.info(
                        f"Hors session ({h}h >= {SESSION_END_H}h) - "
                        f"Trading desactive jusqu'a demain."
                    )
                ind = None
                gc.collect()
                await asyncio.sleep(60)
                continue

            # Threshold de base
            threshold = m["score_min"]

            # Plus sélectif en milieu/fin de session
            if h >= SESSION_MID_H:
                threshold += 10
                # Note : pas de réduction même si aucun trade pris — c'est voulu.
                # Mieux vaut ne rien faire que forcer un mauvais trade.

            # Pénalité post-SL : cooldown étendu après un stop-loss
            if time.time() - state.get("last_sl_time", 0) < 300:
                threshold += 20
                log.info(f"Post-SL cooldown actif → threshold={threshold}")

            now = time.time()
            if now - _last_reload >= RELOAD_INTERVAL:
                df_m5  = await get_candles(TIMEFRAME_M5,  200)
                df_m15 = await get_candles(TIMEFRAME_M15, 100)
                df_h1  = await get_candles(TIMEFRAME_H1,  100)

                if df_m5 is not None and len(df_m5) >= 50:
                    _cache_m5    = df_m5
                    _cache_m15   = df_m15 if df_m15 is not None else df_m5
                    _cache_h1    = df_h1  if df_h1  is not None else df_m5
                    _last_reload = now
                    log.info(
                        f"M5 recharge | {len(df_m5)} bougies | "
                        f"Prix: {round(df_m5['Close'].iloc[-1],2)} | Spread: {spread}"
                    )
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
                    log.info(
                        f"Signal {direction} | score={score}/{threshold} | "
                        f"VWAP={ind['vwap']} ({ind['dist_vwap_sd']:.1f}SD) | "
                        f"POC={ind['poc']} near={ind['near_poc']} | "
                        f"VolX{ind['vol_ratio']} | {reasons[0] if reasons else ''}"
                    )

                if score >= threshold:
                    levels = calculate_tp_sl(
                        direction, ind["price"], ind["atr"],
                        ind["swing_high"], ind["swing_low"]
                    )

                    log.info(
                        f"Execution {direction} @ {ind['price']} | score={score} | "
                        f"SL={levels['sl']} TP1={levels['tp1']} TP2={levels['tp2']} TP3={levels['tp3']}"
                    )

                    results = await place_orders_async(direction, ind["price"], levels)
                    if results:
                        state["active_trades"] = [
                            {
                                "direction": direction, "entry": ind["price"],
                                "levels": levels, "ticket": r["ticket"],
                                "tp_target": r["tp_target"],
                                "tp_alerted": False, "sl_alerted": False,
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

async def main():
    await run_in_mt5_thread(_mt5_connect)
    log.info("Gold Bot v2 Serveur Headless Demarre")

    # [FIX RESTORE] Récupère les positions ouvertes avant de démarrer la boucle.
    # Sans ce fix : chaque restart remet trades_today=0 et active_trades=[].
    # Le bot peut alors placer de nouveaux trades par-dessus des positions déjà
    # ouvertes (ce qui s'est passé avec le trade 10:02 → restart → trade 12:19
    # ouvert EN PLUS, et le trade 14:54 pris car trades_today=1 < 3 après restart).
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
