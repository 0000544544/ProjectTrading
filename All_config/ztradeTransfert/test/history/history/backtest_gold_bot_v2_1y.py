"""
╔══════════════════════════════════════════════════════════════════════════════╗
║     GOLD BOT V2 — BACKTESTER COMPLET (SAFE / RISQUE / RISQUE+++)            ║
║  Simule fidèlement bot_headless.py sur données historiques XAUUSD M5/M1     ║
║                                                                              ║
║  v2.1 — Corrections audit :                                                  ║
║    ✅ Simulation du spread broker  (+SPREAD pts déduits par trade)           ║
║    ✅ Simulation du slippage       (+SLIPPAGE pts sur entrée ET sortie)      ║
╚══════════════════════════════════════════════════════════════════════════════╝

USAGE :
  1. Mettre le fichier CSV dans le même dossier (ou changer DATA_PATH)
  2. python backtest_gold_bot_v2.py
  3. Les résultats s'affichent dans le terminal + graphiques matplotlib

FORMAT CSV ATTENDU (colonnes obligatoires) :
  time, Open, High, Low, Close, Volume
  (la colonne 'time' peut être en timestamp Unix ou en datetime ISO)

PARAMÈTRES CONFIGURABLES EN BAS DU FICHIER (section CONFIG)
"""

# ─────────────────────────────────────────────────────────────────────────────
# IMPORTS
# ─────────────────────────────────────────────────────────────────────────────
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from matplotlib.patches import FancyBboxPatch
import warnings
warnings.filterwarnings("ignore")

# ─────────────────────────────────────────────────────────────────────────────
# CONSTANTES IDENTIQUES AU BOT
# ─────────────────────────────────────────────────────────────────────────────
MAX_RISK_PTS   = 22.0
DAILY_MAX_LOSS = -150.0
SESSION_START_H = 5
SESSION_END_H  = 15
SESSION_MID_H  = 12
MAX_TRADES_PER_DAY = 3
LOT_SIZE       = 0.01        # ← ajuster selon ton compte
POINT_VALUE    = 100.0       # XAUUSD: 1 lot = 100 oz → 1pt * 100 = $/lot

# ─────────────────────────────────────────────────────────────────────────────
# ← NOUVEAU v2.1 : COÛTS TRANSACTIONNELS RÉALISTES
# ─────────────────────────────────────────────────────────────────────────────
# Ces deux paramètres transforment un backtest "optimiste" en résultat proche
# du live. Ajuste SPREAD selon ton broker (vérifie dans MetaTrader > Symboles).
#
# SPREAD   : coût du spread, déduit une fois par trade (aller).
#            Valeur typique XAUUSD chez les brokers ECN : 0.20 – 0.50 pts
# SLIPPAGE : glissement d'exécution appliqué à CHAQUE leg (entrée + sortie).
#            Sur les news ou forte volatilité, peut atteindre 0.2 – 0.5 pts.
SPREAD   = 0.35   # pts — spread moyen broker (ajustable)
SLIPPAGE = 0.10   # pts — glissement par exécution (ajustable)
# ─────────────────────────────────────────────────────────────────────────────

MODES_CONFIG = {
    "safe": {
        "sl_mult": 2.5, "tp1_mult": 1.5, "tp2_mult": 2.5, "tp3_mult": 4.0,
        "score_min": 60, "cooldown_bars": 60,
        "label": "🛡️  SAFE", "color": "#2ecc71",
        "desc": "SL large, peu de trades, setups premium",
    },
    "risque": {
        "sl_mult": 1.8, "tp1_mult": 1.2, "tp2_mult": 2.0, "tp3_mult": 3.2,
        "score_min": 45, "cooldown_bars": 36,
        "label": "⚡  RISQUE", "color": "#f39c12",
        "desc": "Équilibre, paramètres standards",
    },
    "risque+++": {
        "sl_mult": 1.5, "tp1_mult": 1.0, "tp2_mult": 1.8, "tp3_mult": 2.8,
        "score_min": 35, "cooldown_bars": 18,
        "label": "🔥 RISQUE+++", "color": "#e74c3c",
        "desc": "SL minimum, haute fréquence",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# INDICATEURS (copiés fidèlement depuis bot_headless.py)
# ─────────────────────────────────────────────────────────────────────────────

def compute_vwap(df: pd.DataFrame, window: int = 50) -> pd.Series:
    typical = (df["High"] + df["Low"] + df["Close"]) / 3
    tp_vol  = typical * df["Volume"]
    return tp_vol.rolling(window).sum() / df["Volume"].rolling(window).sum()

def compute_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    price_min = df["Low"].min()
    price_max = df["High"].max()
    if price_max == price_min:
        return {"poc": df["Close"].iloc[-1],
                "value_area_high": price_max, "value_area_low": price_min}
    volumes, edges = np.histogram(df["Close"], bins=bins, weights=df["Volume"])
    poc_idx     = volumes.argmax()
    poc         = (edges[poc_idx] + edges[poc_idx + 1]) / 2
    total_vol   = volumes.sum()
    sorted_idx  = np.argsort(volumes)[::-1]
    acc, va_idx = 0, []
    for idx in sorted_idx:
        acc += volumes[idx]
        va_idx.append(idx)
        if acc >= total_vol * 0.70:
            break
    va_high = max((edges[i] + edges[i + 1]) / 2 for i in va_idx)
    va_low  = min((edges[i] + edges[i + 1]) / 2 for i in va_idx)
    return {"poc": round(poc, 2),
            "value_area_high": round(va_high, 2),
            "value_area_low":  round(va_low, 2)}

def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift()).abs(),
        (df["Low"]  - df["Close"].shift()).abs(),
    ], axis=1).max(axis=1)
    val = tr.rolling(window).mean().iloc[-1]
    return round(val, 2) if not np.isnan(val) else 1.0

def _detect_candle_pattern(df: pd.DataFrame) -> str:
    o, h, l, c = (df["Open"].iloc[-1], df["High"].iloc[-1],
                  df["Low"].iloc[-1], df["Close"].iloc[-1])
    body   = abs(c - o)
    candle = h - l
    if candle == 0:
        return "Doji"
    ratio      = body / candle
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    if ratio < 0.1:                                                   return "Doji"
    if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o:  return "Marteau"
    if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o:  return "Shooting Star"
    prev_o, prev_c = df["Open"].iloc[-2], df["Close"].iloc[-2]
    if c > o and c > prev_o and o < prev_c and prev_c < prev_o:      return "Engulfing haussier"
    if c < o and c < prev_o and o > prev_c and prev_c > prev_o:      return "Engulfing baissier"
    if lower_wick > body * 3:                                         return "Pin Bar haussier"
    if upper_wick > body * 3:                                         return "Pin Bar baissier"
    return "Neutre"

def compute_indicators(df: pd.DataFrame) -> dict:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    vol   = df["Volume"]
    price = close.iloc[-1]

    atr          = compute_atr(df)
    vwap_series  = compute_vwap(df, window=50)
    vwap         = vwap_series.iloc[-1]
    typical      = (high + low + close) / 3
    vwap_std_s   = (typical - vwap_series).rolling(50).std()
    vwap_std     = vwap_std_s.iloc[-1]
    if np.isnan(vwap_std) or vwap_std == 0:
        vwap_std = atr

    vwap_upper1  = round(vwap + 1.0 * vwap_std, 2)
    vwap_lower1  = round(vwap - 1.0 * vwap_std, 2)
    vwap_upper2  = round(vwap + 2.0 * vwap_std, 2)
    vwap_lower2  = round(vwap - 2.0 * vwap_std, 2)
    dist_vwap    = round(price - vwap, 2)
    dist_vwap_sd = round(dist_vwap / vwap_std, 2)
    vwap_slope   = round(vwap_series.diff(5).iloc[-1], 2)

    vp          = compute_volume_profile(df.tail(100))
    poc         = vp["poc"]
    near_poc    = abs(price - poc) < atr * 0.5

    recent          = df.tail(30)
    swing_high      = round(recent["High"].max(), 2)
    swing_low       = round(recent["Low"].min(), 2)
    near_swing_sup  = abs(price - swing_low)  < atr * 1.0
    near_swing_res  = abs(price - swing_high) < atr * 1.0

    ema200     = close.ewm(span=200, adjust=False).mean().iloc[-1]
    above_200  = price > ema200

    ema20      = close.ewm(span=20, adjust=False).mean()
    ema50      = close.ewm(span=50, adjust=False).mean()
    ema_cross_bull = (ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-2] <= ema50.iloc[-2])
    ema_cross_bear = (ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-2] >= ema50.iloc[-2])
    above_50   = price > ema50.iloc[-1]

    avg_vol    = vol.rolling(20).mean().iloc[-1]
    vol_ratio  = round(vol.iloc[-1] / avg_vol, 2) if (avg_vol and avg_vol > 0) else 1.0
    vol_spike  = vol_ratio > 1.4

    cur_open  = df["Open"].iloc[-1]
    cur_close = close.iloc[-1]
    cur_body  = abs(cur_close - cur_open)
    cur_bull  = cur_close > cur_open
    cur_bear  = cur_close < cur_open
    last10    = close.iloc[-11:-1]
    move10    = last10.iloc[-1] - last10.iloc[0] if len(last10) >= 2 else 0.0

    bounce_bull = (
        near_swing_sup and cur_bull and cur_body > atr * 0.5
        and vol_ratio > 1.4 and move10 < -atr * 1.5
        and (not above_200 or (above_200 and near_poc))
    )
    bounce_bear = (
        near_swing_res and cur_bear and cur_body > atr * 0.5
        and vol_ratio > 1.4 and move10 > atr * 1.5
    )
    pattern = _detect_candle_pattern(df)

    return {
        "price": round(price, 2), "atr": atr,
        "vwap": round(vwap, 2), "vwap_slope": vwap_slope,
        "vwap_upper1": vwap_upper1, "vwap_lower1": vwap_lower1,
        "vwap_upper2": vwap_upper2, "vwap_lower2": vwap_lower2,
        "dist_vwap_sd": dist_vwap_sd,
        "poc": poc, "va_high": vp["value_area_high"], "va_low": vp["value_area_low"],
        "near_poc": near_poc,
        "swing_high": swing_high, "swing_low": swing_low,
        "near_swing_sup": near_swing_sup, "near_swing_res": near_swing_res,
        "ema200": round(ema200, 2), "ema20": round(ema20.iloc[-1], 2),
        "ema50": round(ema50.iloc[-1], 2),
        "above_200": above_200, "above_50": above_50,
        "ema_cross_bull": ema_cross_bull, "ema_cross_bear": ema_cross_bear,
        "vol_ratio": vol_ratio, "vol_spike": vol_spike,
        "bounce_bull": bounce_bull, "bounce_bear": bounce_bear,
        "move10": round(move10, 2), "pattern": pattern,
    }

def determine_direction(ind: dict):
    if abs(ind["vwap_slope"]) < ind["atr"] * 0.1:
        return None
    dist = ind["dist_vwap_sd"]
    if dist > 4.5 or dist < -4.5:
        return None
    price = ind["price"]
    if ind["bounce_bull"]: return "BUY"
    if ind["bounce_bear"]: return "SELL"
    vwap_long  = price < ind["vwap_lower1"]
    vwap_short = price > ind["vwap_upper1"]
    struct_long  = ind["near_swing_sup"] or ind["near_poc"]
    struct_short = ind["near_swing_res"] or ind["near_poc"]
    vol_ok = ind["vol_ratio"] > 1.0
    if vwap_long  and struct_long  and ind["above_200"] and ind["above_50"] and (vol_ok or ind["ema_cross_bull"]):
        return "BUY"
    if vwap_short and struct_short and not ind["above_200"] and not ind["above_50"] and (vol_ok or ind["ema_cross_bear"]):
        return "SELL"
    if price < ind["vwap_lower2"] and ind["above_200"] and vol_ok:
        return "BUY"
    if price > ind["vwap_upper2"] and not ind["above_200"] and vol_ok:
        return "SELL"
    return None

def compute_score(ind: dict, direction: str) -> tuple:
    score, reasons = 0, []
    is_buy = direction == "BUY"
    dist_sd = abs(ind["dist_vwap_sd"])
    if dist_sd >= 2.0:  score += 30; reasons.append(f"VWAP {dist_sd:.1f}SD")
    elif dist_sd >= 1.0: score += 18
    elif dist_sd >= 0.5: score += 8
    if is_buy  and ind["vwap_slope"] > 0: score += 5
    elif not is_buy and ind["vwap_slope"] < 0: score += 5
    if is_buy:
        if ind["near_swing_sup"]: score += 15; reasons.append(f"Sup@{ind['swing_low']}")
        if ind["near_poc"]:       score += 10; reasons.append(f"POC@{ind['poc']}")
        if ind["price"] < ind["va_low"]:  score += 10
        if ind["above_200"]:      score += 5
    else:
        if ind["near_swing_res"]: score += 15; reasons.append(f"Res@{ind['swing_high']}")
        if ind["near_poc"]:       score += 10
        if ind["price"] > ind["va_high"]: score += 10
        if not ind["above_200"]:  score += 5
    vr = ind["vol_ratio"]
    if ind["bounce_bull"] or ind["bounce_bear"]: score += 25; reasons.append(f"Bounce x{vr}")
    elif vr >= 2.0: score += 20
    elif vr >= 1.4: score += 12
    elif vr >= 1.0: score += 5
    if is_buy  and ind["ema_cross_bull"]: score += 10; reasons.append("GoldenX")
    elif not is_buy and ind["ema_cross_bear"]: score += 10; reasons.append("DeathX")
    bull_p = {"Marteau", "Engulfing haussier", "Pin Bar haussier"}
    bear_p = {"Shooting Star", "Engulfing baissier", "Pin Bar baissier"}
    if is_buy  and ind["pattern"] in bull_p:  score += 15; reasons.append(ind["pattern"])
    elif not is_buy and ind["pattern"] in bear_p: score += 15; reasons.append(ind["pattern"])
    elif ind["pattern"] == "Doji": score += 3
    return min(score, 100), reasons

def calculate_tp_sl(direction: str, entry: float, atr: float,
                    swing_high: float, swing_low: float, mode: str) -> dict:
    m = MODES_CONFIG[mode]
    if direction == "BUY":
        sl_structural = round(swing_low - atr * 0.2, 2)
        sl_atr        = round(entry - atr * m["sl_mult"], 2)
        sl            = min(sl_structural, sl_atr, round(entry - atr * max(m["sl_mult"], 1.5), 2))
        risk          = abs(entry - sl)
        if risk > MAX_RISK_PTS:
            sl = round(entry - MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS
        return {"sl": sl,
                "tp1": round(entry + risk * m["tp1_mult"], 2),
                "tp2": round(entry + risk * m["tp2_mult"], 2),
                "tp3": round(entry + risk * m["tp3_mult"], 2),
                "risk": risk}
    else:
        sl_structural = round(swing_high + atr * 0.2, 2)
        sl_atr        = round(entry + atr * m["sl_mult"], 2)
        sl            = max(sl_structural, sl_atr, round(entry + atr * max(m["sl_mult"], 1.5), 2))
        risk          = abs(sl - entry)
        if risk > MAX_RISK_PTS:
            sl = round(entry + MAX_RISK_PTS, 2)
            risk = MAX_RISK_PTS
        return {"sl": sl,
                "tp1": round(entry - risk * m["tp1_mult"], 2),
                "tp2": round(entry - risk * m["tp2_mult"], 2),
                "tp3": round(entry - risk * m["tp3_mult"], 2),
                "risk": risk}

# ─────────────────────────────────────────────────────────────────────────────
# MOTEUR DE SIMULATION DE TRADE
# ─────────────────────────────────────────────────────────────────────────────

def calc_pnl(direction: str, entry: float, exit_price: float,
             lot: float = LOT_SIZE) -> float:
    """P&L en dollars pour XAUUSD.
    NOTE : entry et exit_price sont déjà ajustés pour le spread/slippage
    avant d'appeler cette fonction — le calcul brut reste identique.
    """
    multiplier = 1.0 if direction == "BUY" else -1.0
    return round((exit_price - entry) * multiplier * lot * POINT_VALUE, 2)


# ─── NOUVEAU v2.1 ────────────────────────────────────────────────────────────
def _apply_entry_costs(direction: str, raw_entry: float) -> float:
    """
    Ajuste le prix d'entrée pour simuler le spread broker + le slippage.

    Logique :
      • BUY  → on achète à l'Ask = prix affiché + SPREAD, puis slippage en plus.
                Résultat : on paie SPREAD + SLIPPAGE de plus que le mid-price.
      • SELL → on vend au Bid = prix affiché, puis slippage en moins.
                Résultat : on reçoit SLIPPAGE de moins que le mid-price.
                (Le spread est récupéré côté sortie pour les SELL, voir _apply_exit_costs.)

    Ces valeurs dégradent légèrement l'entry price et réduisent donc tous les P&L.
    """
    if direction == "BUY":
        return round(raw_entry + SPREAD + SLIPPAGE, 2)
    else:  # SELL
        return round(raw_entry - SLIPPAGE, 2)


def _apply_exit_costs(direction: str, raw_exit: float) -> float:
    """
    Ajuste le prix de sortie pour simuler le slippage d'exécution.

    Logique :
      • BUY  → on clôture en vendant au Bid = prix - 0 (spread déjà payé à l'entrée).
                On soustrait seulement le slippage de sortie.
      • SELL → on clôture en rachetant à l'Ask = prix + SPREAD.
                On ajoute aussi le slippage de sortie.

    Ces valeurs dégradent le prix de sortie dans le sens défavorable au trade.
    """
    if direction == "BUY":
        return round(raw_exit - SLIPPAGE, 2)
    else:  # SELL
        return round(raw_exit + SPREAD + SLIPPAGE, 2)
# ─────────────────────────────────────────────────────────────────────────────


def simulate_subtrade(direction: str, entry: float, sl: float, tp: float,
                      future_df: pd.DataFrame, be_price: float = None,
                      sl_tp1: float = None) -> dict:
    """
    Simule un sous-trade sur les bougies futures.
    Retourne : {result, exit_price, exit_bar_idx, pnl}

    IMPORTANT v2.1 : entry est déjà le prix ajusté (Ask + slippage pour BUY).
    Le prix de sortie est ajusté via _apply_exit_costs() avant le calcul P&L.
    """
    current_sl = sl

    for idx, row in future_df.iterrows():
        bar_open  = row["Open"]
        bar_high  = row["High"]
        bar_low   = row["Low"]

        if be_price is not None and current_sl == sl:
            current_sl = be_price

        if direction == "BUY":
            if bar_open <= current_sl:
                exit_adj = _apply_exit_costs(direction, current_sl)   # ← NOUVEAU
                return {"result": "SL", "exit": current_sl,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_open >= tp:
                exit_adj = _apply_exit_costs(direction, tp)            # ← NOUVEAU
                return {"result": "TP", "exit": tp,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_low <= current_sl:
                exit_adj = _apply_exit_costs(direction, current_sl)   # ← NOUVEAU
                return {"result": "SL", "exit": current_sl,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_high >= tp:
                exit_adj = _apply_exit_costs(direction, tp)            # ← NOUVEAU
                return {"result": "TP", "exit": tp,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
        else:  # SELL
            if bar_open >= current_sl:
                exit_adj = _apply_exit_costs(direction, current_sl)   # ← NOUVEAU
                return {"result": "SL", "exit": current_sl,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_open <= tp:
                exit_adj = _apply_exit_costs(direction, tp)            # ← NOUVEAU
                return {"result": "TP", "exit": tp,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_high >= current_sl:
                exit_adj = _apply_exit_costs(direction, current_sl)   # ← NOUVEAU
                return {"result": "SL", "exit": current_sl,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}
            if bar_low <= tp:
                exit_adj = _apply_exit_costs(direction, tp)            # ← NOUVEAU
                return {"result": "TP", "exit": tp,
                        "exit_idx": idx, "pnl": calc_pnl(direction, entry, exit_adj)}

    # Timeout : ferme au dernier Close disponible
    last_close = future_df["Close"].iloc[-1]
    exit_adj   = _apply_exit_costs(direction, last_close)              # ← NOUVEAU
    return {"result": "TIMEOUT", "exit": last_close,
            "exit_idx": future_df.index[-1],
            "pnl": calc_pnl(direction, entry, exit_adj)}

def simulate_sequence(direction: str, entry: float, levels: dict,
                      future_df: pd.DataFrame) -> list:
    """
    Simule les 3 sous-trades avec gestion du breakeven et SL glissant.
    entry ici est déjà le prix ajusté (spread + slippage inclus).
    """
    sl   = levels["sl"]
    tps  = [levels["tp1"], levels["tp2"], levels["tp3"]]
    results = []

    r1 = simulate_subtrade(direction, entry, sl, tps[0], future_df)
    results.append(r1)

    sl_t2 = entry if r1["result"] == "TP" else sl
    r2 = simulate_subtrade(direction, entry, sl_t2, tps[1], future_df)
    results.append(r2)

    sl_t3 = tps[0] if r2["result"] == "TP" else sl_t2
    r3 = simulate_subtrade(direction, entry, sl_t3, tps[2], future_df)
    results.append(r3)

    return results

# ─────────────────────────────────────────────────────────────────────────────
# BOUCLE DE BACKTESTING PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────
LOOKBACK       = 200
MAX_HOLD_BARS  = 200

def run_backtest(df: pd.DataFrame, mode: str, verbose: bool = False) -> dict:
    m         = MODES_CONFIG[mode]
    trades    = []

    daily_pnl         = 0.0
    trades_today      = 0
    last_signal_bar   = -9999
    daily_pnl_history = {}
    current_date      = None
    last_sl_bar       = -9999
    equity            = [0.0]
    daily_stop_active = False

    n = len(df)
    bar_idx = LOOKBACK

    while bar_idx < n - MAX_HOLD_BARS:
        row      = df.iloc[bar_idx]
        bar_time = row.get("time", pd.Timestamp(f"2024-01-01 {bar_idx % 24:02d}:00"))

        bar_date = pd.Timestamp(bar_time).date() if hasattr(bar_time, 'date') else None
        if bar_date and bar_date != current_date:
            if current_date is not None:
                daily_pnl_history[current_date] = daily_pnl
            current_date   = bar_date
            daily_pnl      = 0.0
            trades_today   = 0
            daily_stop_active = False

        if daily_stop_active:
            bar_idx += 1; continue

        if daily_pnl < DAILY_MAX_LOSS:
            daily_stop_active = True
            bar_idx += 1; continue

        if trades_today >= MAX_TRADES_PER_DAY:
            bar_idx += 1; continue

        h = pd.Timestamp(bar_time).hour if hasattr(bar_time, 'hour') else 8
        if h >= SESSION_END_H or h < SESSION_START_H:
            bar_idx += 1; continue

        if bar_idx - last_signal_bar < m["cooldown_bars"]:
            bar_idx += 1; continue

        window = df.iloc[bar_idx - LOOKBACK: bar_idx].copy().reset_index(drop=True)
        try:
            ind = compute_indicators(window)
        except Exception:
            bar_idx += 1; continue

        direction = determine_direction(ind)
        if direction is None:
            bar_idx += 1; continue

        score, reasons = compute_score(ind, direction)

        threshold = m["score_min"]
        if h >= SESSION_MID_H:
            threshold += 10
        if bar_idx - last_sl_bar < 60:
            threshold += 20

        if score < threshold:
            bar_idx += 1; continue

        # ── Signal validé ──
        raw_entry = round(ind["price"], 2)

        # ← NOUVEAU v2.1 : application du spread + slippage sur l'entrée
        # Le prix ajusté est utilisé pour TOUS les calculs de P&L.
        # Les niveaux SL/TP restent basés sur le prix brut (logique du bot live).
        entry = _apply_entry_costs(direction, raw_entry)

        levels = calculate_tp_sl(direction, raw_entry, ind["atr"],
                                 ind["swing_high"], ind["swing_low"], mode)

        future = df.iloc[bar_idx + 1: bar_idx + 1 + MAX_HOLD_BARS].reset_index(drop=True)
        if len(future) < 5:
            bar_idx += 1; continue

        sub_results = simulate_sequence(direction, entry, levels, future)

        total_pnl = sum(r["pnl"] for r in sub_results)
        outcomes  = [r["result"] for r in sub_results]
        max_exit_idx = max((r["exit_idx"] for r in sub_results), default=0)

        tps_hit  = outcomes.count("TP")
        sls_hit  = outcomes.count("SL")
        main_res = "TP" if tps_hit >= 2 else ("SL" if sls_hit >= 2 else "MIXED")

        # Coût total de transaction pour ce signal (3 sous-trades)
        # = spread (1x à l'entrée, déjà inclus) + slippage (6x : 3 entrées + 3 sorties)
        # → déjà intégré dans entry ajusté + _apply_exit_costs()
        # On calcule juste le montant pour l'affichage.
        transaction_cost = round((SPREAD + 2 * SLIPPAGE) * 3 * LOT_SIZE * POINT_VALUE, 2)

        trade_record = {
            "bar_idx":          bar_idx,
            "time":             bar_time,
            "direction":        direction,
            "entry_raw":        raw_entry,           # ← NOUVEAU : prix mid
            "entry_adj":        entry,               # ← NOUVEAU : prix réel payé
            "sl":               levels["sl"],
            "tp1":              levels["tp1"],
            "tp2":              levels["tp2"],
            "tp3":              levels["tp3"],
            "risk_pts":         levels["risk"],
            "score":            score,
            "threshold":        threshold,
            "reasons":          ", ".join(reasons),
            "atr":              ind["atr"],
            "transaction_cost": transaction_cost,    # ← NOUVEAU
            "t1_result":        outcomes[0], "t1_pnl": sub_results[0]["pnl"],
            "t2_result":        outcomes[1], "t2_pnl": sub_results[1]["pnl"],
            "t3_result":        outcomes[2], "t3_pnl": sub_results[2]["pnl"],
            "total_pnl":        round(total_pnl, 2),
            "result":           main_res,
        }
        trades.append(trade_record)

        daily_pnl    += total_pnl
        trades_today += 1
        equity.append(round(equity[-1] + total_pnl, 2))
        last_signal_bar = bar_idx

        if sls_hit >= 2:
            last_sl_bar = bar_idx

        bar_idx += max_exit_idx + 2

        if verbose:
            print(f"  [{bar_time}] {direction} score={score}/{threshold} "
                  f"PnL={total_pnl:+.2f}$ ({outcomes}) cumul={equity[-1]:+.2f}$  "
                  f"[costs: -{transaction_cost:.2f}$]")   # ← NOUVEAU

    if current_date and daily_pnl:
        daily_pnl_history[current_date] = daily_pnl

    df_trades = pd.DataFrame(trades) if trades else pd.DataFrame()
    stats = _compute_stats(df_trades, equity, daily_pnl_history)
    stats["mode"]       = mode
    stats["trades_df"]  = df_trades
    stats["equity"]     = equity
    stats["daily_pnl"]  = daily_pnl_history

    return stats

def _compute_stats(df_t: pd.DataFrame, equity: list, daily_pnl: dict) -> dict:
    if df_t.empty:
        return {"n_trades": 0, "n_sequences": 0, "winrate": 0, "total_pnl": 0,
                "avg_pnl": 0, "best": 0, "worst": 0, "max_dd": 0, "sharpe": 0,
                "profit_factor": 0, "avg_risk": 0, "avg_score": 0,
                "n_tp": 0, "n_sl": 0, "n_mixed": 0,
                "total_transaction_costs": 0}  # ← NOUVEAU

    n  = len(df_t)
    wins   = (df_t["total_pnl"] > 0).sum()
    losses = (df_t["total_pnl"] < 0).sum()
    wr     = round(wins / n * 100, 1) if n else 0

    gross_profit = df_t[df_t["total_pnl"] > 0]["total_pnl"].sum()
    gross_loss   = abs(df_t[df_t["total_pnl"] < 0]["total_pnl"].sum())
    pf = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    eq_arr = np.array(equity)
    peak   = np.maximum.accumulate(eq_arr)
    dd     = eq_arr - peak
    max_dd = round(dd.min(), 2)

    daily_vals = list(daily_pnl.values())
    sharpe = 0.0
    if len(daily_vals) > 1:
        dv = np.array(daily_vals)
        sharpe = round((dv.mean() / dv.std() * np.sqrt(252)), 2) if dv.std() > 0 else 0

    # ← NOUVEAU : total des coûts de transaction pour ce mode
    total_costs = round(df_t["transaction_cost"].sum(), 2) if "transaction_cost" in df_t.columns else 0

    return {
        "n_sequences":            n,
        "winrate":                wr,
        "total_pnl":              round(df_t["total_pnl"].sum(), 2),
        "avg_pnl":                round(df_t["total_pnl"].mean(), 2),
        "best":                   round(df_t["total_pnl"].max(), 2),
        "worst":                  round(df_t["total_pnl"].min(), 2),
        "max_dd":                 max_dd,
        "sharpe":                 sharpe,
        "profit_factor":          pf,
        "avg_risk":               round(df_t["risk_pts"].mean(), 2),
        "avg_score":              round(df_t["score"].mean(), 1),
        "n_tp":                   (df_t["result"] == "TP").sum(),
        "n_sl":                   (df_t["result"] == "SL").sum(),
        "n_mixed":                (df_t["result"] == "MIXED").sum(),
        "n_buy":                  (df_t["direction"] == "BUY").sum(),
        "n_sell":                 (df_t["direction"] == "SELL").sum(),
        "total_transaction_costs": total_costs,      # ← NOUVEAU
    }

# ─────────────────────────────────────────────────────────────────────────────
# AFFICHAGE DES RÉSULTATS
# ─────────────────────────────────────────────────────────────────────────────

def print_results(results: dict):
    m = MODES_CONFIG[results["mode"]]
    print(f"\n{'═'*62}")
    print(f"  {m['label']}  —  {m['desc']}")
    print(f"{'═'*62}")
    print(f"  Séquences testées      : {results['n_sequences']}")
    print(f"  Win Rate               : {results['winrate']}%  "
          f"(TP:{results['n_tp']} | SL:{results['n_sl']} | Mixed:{results['n_mixed']})")
    print(f"  PnL Total              : {results['total_pnl']:+.2f}$")
    print(f"  PnL Moyen/trade        : {results['avg_pnl']:+.2f}$")
    print(f"  Meilleur trade         : {results['best']:+.2f}$")
    print(f"  Pire trade             : {results['worst']:+.2f}$")
    print(f"  Max Drawdown           : {results['max_dd']:.2f}$")
    print(f"  Profit Factor          : {results['profit_factor']}")
    print(f"  Sharpe (annualisé)     : {results['sharpe']}")
    print(f"  Risque moyen/trade     : {results['avg_risk']:.1f} pts")
    print(f"  Score moyen            : {results['avg_score']}/100")
    print(f"  BUY/SELL               : {results['n_buy']}/{results['n_sell']}")
    # ← NOUVEAU : ligne coûts transactionnels
    print(f"  ─────────────────────────────────────────────────────")
    print(f"  Coûts transactionnels  : -{results['total_transaction_costs']:.2f}$  "
          f"(spread {SPREAD}pt + slippage {SLIPPAGE}pt × 2 × 3 legs)")
    print(f"  [Rappel : PnL ci-dessus INCLUT déjà ces coûts]")

def plot_results(all_results: list, df_prices: pd.DataFrame):
    n_modes = len(all_results)
    colors  = [MODES_CONFIG[r["mode"]]["color"] for r in all_results]
    labels  = [MODES_CONFIG[r["mode"]]["label"] for r in all_results]

    fig = plt.figure(figsize=(20, 14), facecolor="#0d1117")
    fig.suptitle("⚡ GOLD BOT V2 — RAPPORT DE BACKTESTING XAUUSD  "
                 f"[Spread={SPREAD}pt | Slippage={SLIPPAGE}pt]",   # ← NOUVEAU
                 fontsize=17, fontweight="bold", color="white", y=0.98)

    gs = gridspec.GridSpec(3, n_modes + 1, figure=fig,
                           hspace=0.45, wspace=0.35,
                           left=0.05, right=0.97, top=0.93, bottom=0.06)

    ax_price = fig.add_subplot(gs[0, :])
    _plot_price(ax_price, df_prices, all_results)

    for col, (res, color, label) in enumerate(zip(all_results, colors, labels)):
        ax_eq  = fig.add_subplot(gs[1, col])
        ax_bar = fig.add_subplot(gs[2, col])
        _plot_equity(ax_eq, res, color, label)
        _plot_monthly(ax_bar, res, color, label)

    ax_summary = fig.add_subplot(gs[1:, -1])
    _plot_summary_table(ax_summary, all_results)

    plt.savefig("/mnt/user-data/outputs/backtest_gold_bot.png",
                dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.savefig("/mnt/user-data/outputs/backtest_gold_bot.pdf",
                bbox_inches="tight", facecolor="#0d1117")
    print("\n✅ Graphiques sauvegardés : backtest_gold_bot.png + .pdf")
    plt.show()

def _style_ax(ax, title=""):
    ax.set_facecolor("#161b22")
    ax.spines[:].set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=8)
    ax.xaxis.label.set_color("#8b949e")
    ax.yaxis.label.set_color("#8b949e")
    if title:
        ax.set_title(title, color="white", fontsize=9, fontweight="bold", pad=6)

def _plot_price(ax, df, all_results):
    _style_ax(ax, "XAUUSD — Prix + Signaux de tous les modes")
    if "time" in df.columns:
        x = pd.to_datetime(df["time"])
    else:
        x = range(len(df))
    ax.plot(x, df["Close"], color="#8b949e", linewidth=0.6, alpha=0.8)
    for res, color in zip(all_results, [MODES_CONFIG[r["mode"]]["color"] for r in all_results]):
        if res["trades_df"].empty: continue
        td = res["trades_df"]
        buys  = td[td["direction"] == "BUY"]
        sells = td[td["direction"] == "SELL"]
        if "time" in df.columns:
            buy_x  = pd.to_datetime(buys["time"])
            sell_x = pd.to_datetime(sells["time"])
        else:
            buy_x, sell_x = buys["bar_idx"], sells["bar_idx"]
        ax.scatter(buy_x,  buys["entry_raw"],  marker="^", s=25,   # ← entry_raw
                   color=color, alpha=0.7, zorder=3, linewidths=0)
        ax.scatter(sell_x, sells["entry_raw"], marker="v", s=25,   # ← entry_raw
                   color=color, alpha=0.7, zorder=3, linewidths=0)

def _plot_equity(ax, res, color, label):
    _style_ax(ax, f"Courbe d'équité — {label}")
    eq = res["equity"]
    ax.plot(eq, color=color, linewidth=1.5)
    ax.axhline(0, color="#30363d", linewidth=0.8, linestyle="--")
    ax.fill_between(range(len(eq)), eq, 0,
                    where=[e > 0 for e in eq], alpha=0.15, color=color)
    ax.fill_between(range(len(eq)), eq, 0,
                    where=[e < 0 for e in eq], alpha=0.15, color="#e74c3c")
    final = eq[-1]
    ax.set_ylabel("PnL ($)", fontsize=8)
    ax.annotate(f"Final: {final:+.0f}$", xy=(len(eq)-1, final),
                color=color, fontsize=8, ha="right",
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#161b22", alpha=0.8))

def _plot_monthly(ax, res, color, label):
    _style_ax(ax, f"PnL Mensuel — {label}")
    if not res["daily_pnl"]:
        return
    daily = pd.Series(res["daily_pnl"])
    if daily.empty: return
    daily.index = pd.to_datetime(list(res["daily_pnl"].keys()))
    monthly = daily.resample("ME").sum()
    bar_colors = [color if v >= 0 else "#e74c3c" for v in monthly.values]
    ax.bar(range(len(monthly)), monthly.values, color=bar_colors, width=0.7, alpha=0.85)
    ax.axhline(0, color="#30363d", linewidth=0.8)
    ax.set_xticks(range(len(monthly)))
    ax.set_xticklabels([d.strftime("%b") for d in monthly.index],
                       rotation=45, fontsize=7)
    ax.set_ylabel("PnL ($)", fontsize=8)

def _plot_summary_table(ax, all_results):
    ax.set_facecolor("#161b22")
    ax.axis("off")
    ax.set_title("RÉSUMÉ COMPARATIF", color="white", fontsize=10,
                 fontweight="bold", pad=8)
    rows = [
        ("Séquences",     lambda r: str(r["n_sequences"])),
        ("Win Rate",      lambda r: f"{r['winrate']}%"),
        ("PnL Total",     lambda r: f"{r['total_pnl']:+.0f}$"),
        ("PnL Moyen",     lambda r: f"{r['avg_pnl']:+.0f}$"),
        ("Meilleur",      lambda r: f"{r['best']:+.0f}$"),
        ("Pire",          lambda r: f"{r['worst']:+.0f}$"),
        ("Max DD",        lambda r: f"{r['max_dd']:.0f}$"),
        ("Profit Factor", lambda r: str(r["profit_factor"])),
        ("Sharpe",        lambda r: str(r["sharpe"])),
        ("Score Moyen",   lambda r: f"{r['avg_score']}"),
        ("TP / SL",       lambda r: f"{r['n_tp']} / {r['n_sl']}"),
        ("Costs ($)",     lambda r: f"-{r['total_transaction_costs']:.0f}$"),  # ← NOUVEAU
    ]
    headers = ["Métrique"] + [MODES_CONFIG[r["mode"]]["label"].replace("  ", " ") for r in all_results]
    result_colors = [MODES_CONFIG[r["mode"]]["color"] for r in all_results]

    y = 0.97
    dy = 0.069
    for j, h in enumerate(headers):
        x = j / len(headers)
        ax.text(x, y, h, color="white" if j == 0 else result_colors[j-1],
                fontsize=8, fontweight="bold", transform=ax.transAxes,
                ha="left" if j == 0 else "center")
    y -= dy * 0.8
    ax.axhline(y, color="#30363d", linewidth=0.6, transform=ax.transAxes)
    y -= dy * 0.3

    for row_label, fn in rows:
        y -= dy
        ax.text(0.0, y, row_label, color="#8b949e", fontsize=8,
                transform=ax.transAxes)
        for j, res in enumerate(all_results):
            val = fn(res)
            xc  = (j + 1) / len(headers)
            c   = result_colors[j]
            if "$" in val:
                try:
                    v = float(val.replace("$", "").replace("+", "").replace("-", ""))
                    if "Costs" in row_label:
                        c = "#e74c3c"   # toujours rouge — c'est un coût
                    else:
                        c = "#2ecc71" if v > 0 else ("#e74c3c" if v < 0 else c)
                except:
                    pass
            ax.text(xc, y, val, color=c, fontsize=8,
                    transform=ax.transAxes, ha="center")

# ─────────────────────────────────────────────────────────────────────────────
# CHARGEMENT DES DONNÉES
# ─────────────────────────────────────────────────────────────────────────────

def load_data(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip() for c in df.columns]
    rename = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("open",):   rename[c] = "Open"
        if cl in ("high",):   rename[c] = "High"
        if cl in ("low",):    rename[c] = "Low"
        if cl in ("close",):  rename[c] = "Close"
        if cl in ("volume", "tickvolume", "tick_volume", "vol"): rename[c] = "Volume"
        if cl in ("time", "date", "datetime", "timestamp"): rename[c] = "time"
    df.rename(columns=rename, inplace=True)

    required = ["Open", "High", "Low", "Close", "Volume"]
    missing  = [c for c in required if c not in df.columns]
    
    if missing:
        raise ValueError(f"Colonnes manquantes : {missing}\nColonnes dispo : {list(df.columns)}")

    if "time" in df.columns:
        # Correction ici : suppression de infer_datetime_format
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.sort_values("time").reset_index(drop=True)

    df = df.dropna(subset=required)
    for c in required:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=required).reset_index(drop=True)

    return df

def generate_synthetic_data(n_bars: int = 26280, seed: int = 42) -> pd.DataFrame:
    print("⚠️  Génération de données synthétiques (1 an M5 XAUUSD ~2400$)")
    rng   = np.random.default_rng(seed)
    price = 2380.0
    dates = pd.date_range("2024-01-01 05:00", periods=n_bars, freq="5min")

    rows = []
    trend = 0.0
    for i, dt in enumerate(dates):
        h = dt.hour
        if h < 5 or h >= 22:
            vol_base = 300
        elif 8 <= h <= 16:
            vol_base = 2000
        else:
            vol_base = 1000

        if i % 1000 == 0:
            trend = rng.uniform(-0.02, 0.02)

        atr_base  = 1.8 + 0.8 * (1 if 8 <= h <= 16 else 0)
        move      = rng.normal(trend * 0.5, atr_base)

        if rng.random() < 0.002:
            move += rng.choice([-1, 1]) * rng.uniform(5, 20)

        close  = round(price + move, 2)
        high   = round(max(price, close) + abs(rng.normal(0, atr_base * 0.6)), 2)
        low    = round(min(price, close) - abs(rng.normal(0, atr_base * 0.6)), 2)
        volume = max(100, int(rng.normal(vol_base, vol_base * 0.3)))

        rows.append({
            "time":   dt,
            "Open":   round(price, 2),
            "High":   high,
            "Low":    low,
            "Close":  close,
            "Volume": volume,
        })
        price = close

    df = pd.DataFrame(rows)
    print(f"✅ {len(df)} barres M5 synthétiques générées")
    return df

# ─────────────────────────────────────────────────────────────────────────────
# EXPORT EXCEL DES TRADES
# ─────────────────────────────────────────────────────────────────────────────

def export_trades_excel(all_results: list):
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        wb = openpyxl.Workbook()
        wb.remove(wb.active)

        for res in all_results:
            mode   = res["mode"]
            label  = MODES_CONFIG[mode]["label"].replace("🛡️", "").replace("⚡", "").replace("🔥", "").strip()
            ws = wb.create_sheet(title=label[:31])

            if res["trades_df"].empty:
                ws.append(["Aucun trade généré"])
                continue

            # ← NOUVEAU : entry_raw et entry_adj + transaction_cost dans l'export
            cols = ["time", "direction",
                    "entry_raw", "entry_adj",          # ← NOUVEAU
                    "sl", "tp1", "tp2", "tp3",
                    "risk_pts", "score", "threshold",
                    "transaction_cost",                 # ← NOUVEAU
                    "t1_result", "t1_pnl",
                    "t2_result", "t2_pnl",
                    "t3_result", "t3_pnl",
                    "total_pnl", "result", "reasons"]
            ws.append(cols)

            for cell in ws[1]:
                cell.fill = PatternFill("solid", fgColor="1F4E79")
                cell.font = Font(color="FFFFFF", bold=True)
                cell.alignment = Alignment(horizontal="center")

            for _, row in res["trades_df"][cols].iterrows():
                ws.append(list(row))
                last = ws.max_row
                pnl  = row["total_pnl"]
                fill_col = "1a5c2e" if pnl > 0 else "5c1a1a"
                ws.cell(last, cols.index("total_pnl") + 1).fill = PatternFill("solid", fgColor=fill_col)
                ws.cell(last, cols.index("total_pnl") + 1).font = Font(color="FFFFFF", bold=True)

        ws_sum = wb.create_sheet(title="RESUME", index=0)
        ws_sum.append(["Mode", "Séquences", "WinRate", "PnL Total", "PnL Moyen",
                       "Meilleur", "Pire", "MaxDD", "PF", "Sharpe", "TP", "SL",
                       "Coûts ($)"])  # ← NOUVEAU
        for res in all_results:
            mode = res["mode"]
            ws_sum.append([
                MODES_CONFIG[mode]["label"],
                res["n_sequences"], f"{res['winrate']}%",
                f"{res['total_pnl']:+.2f}$", f"{res['avg_pnl']:+.2f}$",
                f"{res['best']:+.2f}$", f"{res['worst']:+.2f}$",
                f"{res['max_dd']:.2f}$", res["profit_factor"],
                res["sharpe"], res["n_tp"], res["n_sl"],
                f"-{res['total_transaction_costs']:.2f}$",  # ← NOUVEAU
            ])

        path = "/mnt/user-data/outputs/backtest_trades.xlsx"
        wb.save(path)
        print(f"✅ Trades exportés : {path}")
    except ImportError:
        print("⚠️  openpyxl non installé — export Excel désactivé")

# ─────────────────────────────────────────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────────────────────────────────────────

# ══════════════════════════ CONFIG ══════════════════════════
DATA_PATH = "XAUUSD-VIP_M5_1Year_Data.csv"       # ← Mettre le chemin de ton CSV ici, ex: "XAUUSD_M5_2024.csv"
                         #    Si None → données synthétiques générées automatiquement
MODES      = ["safe", "risque", "risque+++"]
VERBOSE    = False       # True = print chaque trade en temps réel
# ════════════════════════════════════════════════════════════

def main():
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       GOLD BOT V2 — BACKTESTER  (XAUUSD)                ║")
    print(f"║  Spread={SPREAD}pt | Slippage={SLIPPAGE}pt/leg (v2.1)          ║")
    print("╚══════════════════════════════════════════════════════════╝\n")

    if DATA_PATH:
        df = load_data(DATA_PATH)
    else:
        df = generate_synthetic_data()

    print(f"\n🚀 Lancement du backtest sur {len(MODES)} modes...\n")

    all_results = []
    for mode in MODES:
        m = MODES_CONFIG[mode]
        print(f"🔄 Mode {m['label']} en cours...", end=" ", flush=True)
        res = run_backtest(df, mode, verbose=VERBOSE)
        all_results.append(res)
        print(f"terminé → {res['n_sequences']} trades | "
              f"WR={res['winrate']}% | PnL={res['total_pnl']:+.2f}$  "
              f"| Costs=-{res['total_transaction_costs']:.2f}$")

    for res in all_results:
        print_results(res)

    print("\n📊 Génération des graphiques...")
    plot_results(all_results, df)

    export_trades_excel(all_results)

    print("\n" + "═" * 62)
    print("  BACKTEST TERMINÉ")
    print("  Fichiers générés dans /outputs :")
    print("    • backtest_gold_bot.png  (dashboard)")
    print("    • backtest_gold_bot.pdf")
    print("    • backtest_trades.xlsx   (tous les trades)")
    print("═" * 62)

if __name__ == "__main__":
    main()
