# ================================================================
# 🥇 Gold Bot v2 — Moteur de Backtest Ultra-Fidèle (Tick-by-Tick)
# ================================================================
# Architecture : M5 (signaux) → Ticks (exécution chirurgicale)
# Données      : 1 an | ~141.7M ticks | Polars LazyFrame
# Vs           : M1 (approximatif) pour mesurer l'impact de précision
# ================================================================

# ============================================================
# CELLULE 1 — Imports & Configuration globale
# ============================================================
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import warnings
import time
from datetime import timedelta
from pathlib import Path
from collections import OrderedDict
import tqdm.notebook as tqdmn
from tqdm.notebook import tqdm

# ── Polars pour les ticks (LazyFrame) ────────────────────────
try:
    import polars as pl
    print(f'✅ Polars {pl.__version__} disponible')
except ImportError:
    raise ImportError("Installez polars : pip install polars")

warnings.filterwarnings('ignore')
plt.style.use('dark_background')

# ── Chemins CSV ──────────────────────────────────────────────
PATH_M1    = Path('data/XAUUSD-VIP_M1_1Year.csv')
PATH_M5    = Path('data/XAUUSD-VIP_M5_1Year.csv')
PATH_TICKS = Path('data/XAUUSD-VIP_Ticks_1Year.csv')

# ── Paramètres de trading ────────────────────────────────────
LOT_SIZE        = 0.03
PNL_PER_POINT   = 3.0        # 0.03 lot × 100 $/point (XAU)
MAX_RISK_PTS    = 22.0
DAILY_MAX_LOSS  = -150.0
SESSION_MID_H   = 12         # GMT+2
SESSION_END_H   = 15         # GMT+2
GMT_OFFSET      = -1         # Broker GMT+3 → PC GMT+2
MAX_TRADES_PER_DAY = 60

# ── Friction réelle ──────────────────────────────────────────
SLIPPAGE_PTS    = 0.5        # Latence broker simulée (points)

# ── Modes ────────────────────────────────────────────────────
MODES_CONFIG = {
    'safe':      {'sl_mult': 2.5, 'tp1_mult': 1.5, 'tp2_mult': 2.5, 'tp3_mult': 4.0, 'score_min': 60, 'cooldown': 300},
    'risque':    {'sl_mult': 1.8, 'tp1_mult': 1.2, 'tp2_mult': 2.0, 'tp3_mult': 3.2, 'score_min': 45, 'cooldown': 180},
    'risque+++': {'sl_mult': 1.5, 'tp1_mult': 1.0, 'tp2_mult': 1.8, 'tp3_mult': 2.8, 'score_min': 35, 'cooldown':  90},
    'optimale':  {'sl_mult': 1.2, 'tp1_mult': 1.0, 'tp2_mult': 1.8, 'tp3_mult': 2.8, 'score_min': 35, 'cooldown':  90},
}
TRADING_MODE = 'optimale'

print('✅ Imports et configuration OK')
print(f'   Mode : {TRADING_MODE} | Score min : {MODES_CONFIG[TRADING_MODE]["score_min"]} | SL mult : {MODES_CONFIG[TRADING_MODE]["sl_mult"]}')


# ============================================================
# CELLULE 2 — Chargement M5 & M1 (pandas) + peek ticks (Polars)
# ============================================================
def load_csv(path: Path, label: str) -> pd.DataFrame:
    """Charge un CSV OHLCV broker avec correction GMT+3 → GMT+2."""
    df = pd.read_csv(path, parse_dates=['Time'])
    df.rename(columns={'TickVolume': 'Volume'}, inplace=True)
    df['Time'] = df['Time'] + pd.Timedelta(hours=GMT_OFFSET)
    df.set_index('Time', inplace=True)
    df.sort_index(inplace=True)
    df[['Open', 'High', 'Low', 'Close', 'Volume']] = \
        df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    print(f'✅ {label} chargé : {len(df):,} bougies | {df.index[0]} → {df.index[-1]}')
    return df

df_m5 = load_csv(PATH_M5, 'M5')
df_m1 = load_csv(PATH_M1, 'M1')

# ── Détection du format des ticks (les premières lignes) ─────
print('\n🔍 Détection du format tick CSV...')
with open(PATH_TICKS, 'r') as f:
    for i, line in enumerate(f):
        print(f'  Ligne {i}: {line.strip()}')
        if i >= 3:
            break

# ── Calcul de la taille du fichier ───────────────────────────
tick_size_gb = PATH_TICKS.stat().st_size / 1e9
print(f'\n📊 Fichier ticks : {tick_size_gb:.2f} GB')
print('   → Polars LazyFrame : pas de chargement complet en RAM')
print('   → Filtrage par plage de temps par trade uniquement')


# ============================================================
# CELLULE 3 — Fonctions Indicateurs (répliques exactes du bot)
# ============================================================
def compute_vwap(df: pd.DataFrame, window: int = 50) -> pd.Series:
    typical = (df['High'] + df['Low'] + df['Close']) / 3
    tp_vol  = typical * df['Volume']
    return tp_vol.rolling(window).sum() / df['Volume'].rolling(window).sum()

def compute_volume_profile(df: pd.DataFrame, bins: int = 20) -> dict:
    price_min = df['Low'].min()
    price_max = df['High'].max()
    if price_max == price_min:
        return {'poc': df['Close'].iloc[-1], 'value_area_high': price_max, 'value_area_low': price_min}
    volumes, edges = np.histogram(df['Close'], bins=bins, weights=df['Volume'])
    poc_idx = volumes.argmax()
    poc     = (edges[poc_idx] + edges[poc_idx + 1]) / 2
    target  = volumes.sum() * 0.70
    sorted_i = np.argsort(volumes)[::-1]
    acc, va_i = 0, []
    for idx in sorted_i:
        acc += volumes[idx]; va_i.append(idx)
        if acc >= target: break
    va_high = max((edges[i] + edges[i + 1]) / 2 for i in va_i)
    va_low  = min((edges[i] + edges[i + 1]) / 2 for i in va_i)
    return {'poc': round(poc, 2), 'value_area_high': round(va_high, 2), 'value_area_low': round(va_low, 2)}

def compute_atr(df: pd.DataFrame, window: int = 14) -> float:
    tr = pd.concat([
        df['High'] - df['Low'],
        (df['High'] - df['Close'].shift()).abs(),
        (df['Low']  - df['Close'].shift()).abs(),
    ], axis=1).max(axis=1)
    return round(tr.rolling(window).mean().iloc[-1], 2)

def _detect_candle_pattern(df: pd.DataFrame) -> str:
    o, h, l, c = df['Open'].iloc[-1], df['High'].iloc[-1], df['Low'].iloc[-1], df['Close'].iloc[-1]
    body   = abs(c - o); candle = h - l
    if candle == 0: return 'Doji'
    ratio       = body / candle
    lower_wick  = min(o, c) - l
    upper_wick  = h - max(o, c)
    if ratio < 0.1:                                                    return 'Doji'
    if lower_wick > body * 2 and upper_wick < body * 0.5 and c > o:   return 'Marteau'
    if upper_wick > body * 2 and lower_wick < body * 0.5 and c < o:   return 'Shooting Star'
    prev_o, prev_c = df['Open'].iloc[-2], df['Close'].iloc[-2]
    if c > o and c > prev_o and o < prev_c and prev_c < prev_o:        return 'Engulfing haussier'
    if c < o and c < prev_o and o > prev_c and prev_c > prev_o:        return 'Engulfing baissier'
    if lower_wick > body * 3:                                          return 'Pin Bar haussier'
    if upper_wick > body * 3:                                          return 'Pin Bar baissier'
    return 'Neutre'

def compute_indicators(df: pd.DataFrame) -> dict:
    close = df['Close']; high = df['High']; low = df['Low']; vol = df['Volume']
    price = close.iloc[-1]
    atr   = compute_atr(df)
    vwap_series = compute_vwap(df, window=50)
    vwap  = vwap_series.iloc[-1]
    typical     = (high + low + close) / 3
    vwap_std    = (typical - vwap_series).rolling(50).std().iloc[-1]
    if np.isnan(vwap_std) or vwap_std == 0: vwap_std = 1e-9
    vwap_upper1 = round(vwap + 1.0 * vwap_std, 2)
    vwap_lower1 = round(vwap - 1.0 * vwap_std, 2)
    vwap_upper2 = round(vwap + 2.0 * vwap_std, 2)
    vwap_lower2 = round(vwap - 2.0 * vwap_std, 2)
    dist_vwap   = round(price - vwap, 2)
    dist_vwap_sd= round(dist_vwap / vwap_std, 2)
    vwap_slope  = round(vwap_series.diff(5).iloc[-1], 2)
    vp  = compute_volume_profile(df.tail(100))
    poc = vp['poc']
    near_poc              = abs(price - poc) < atr * 0.5
    swing_high            = round(df.tail(30)['High'].max(), 2)
    swing_low             = round(df.tail(30)['Low'].min(), 2)
    near_swing_support    = abs(price - swing_low)  < atr * 1.0
    near_swing_resistance = abs(price - swing_high) < atr * 1.0
    ema200      = close.ewm(span=200, adjust=False).mean().iloc[-1]
    above_200   = price > ema200
    ema20       = close.ewm(span=20, adjust=False).mean()
    ema50       = close.ewm(span=50, adjust=False).mean()
    ema_cross_bull = (ema20.iloc[-1] > ema50.iloc[-1] and ema20.iloc[-2] <= ema50.iloc[-2])
    ema_cross_bear = (ema20.iloc[-1] < ema50.iloc[-1] and ema20.iloc[-2] >= ema50.iloc[-2])
    avg_vol     = vol.rolling(20).mean().iloc[-1]
    vol_ratio   = round(vol.iloc[-1] / avg_vol, 2) if avg_vol > 0 else 1.0
    vol_spike   = vol_ratio > 1.4
    cur_open    = df['Open'].iloc[-1]; cur_close = close.iloc[-1]
    cur_body    = abs(cur_close - cur_open)
    cur_bull    = cur_close > cur_open; cur_bear = cur_close < cur_open
    last10      = close.iloc[-11:-1]
    move10      = last10.iloc[-1] - last10.iloc[0]
    above_50    = price > ema50.iloc[-1]
    bounce_bull = (near_swing_support and cur_bull and cur_body > atr * 0.5
                   and vol_ratio > 1.4 and move10 < -atr * 1.5
                   and (not above_200 or (above_200 and near_poc)))
    bounce_bear = (near_swing_resistance and cur_bear and cur_body > atr * 0.5
                   and vol_ratio > 1.4 and move10 > atr * 1.5)
    pattern     = _detect_candle_pattern(df)
    return {
        'price': round(price, 2), 'atr': atr,
        'vwap': round(vwap, 2), 'vwap_upper1': vwap_upper1, 'vwap_lower1': vwap_lower1,
        'vwap_upper2': vwap_upper2, 'vwap_lower2': vwap_lower2,
        'dist_vwap_sd': dist_vwap_sd, 'vwap_slope': vwap_slope,
        'poc': poc, 'va_high': vp['value_area_high'], 'va_low': vp['value_area_low'],
        'near_poc': near_poc, 'swing_high': swing_high, 'swing_low': swing_low,
        'near_swing_sup': near_swing_support, 'near_swing_res': near_swing_resistance,
        'ema200': round(ema200, 2), 'ema20': round(ema20.iloc[-1], 2), 'ema50': round(ema50.iloc[-1], 2),
        'above_200': above_200, 'above_50': above_50,
        'ema_cross_bull': ema_cross_bull, 'ema_cross_bear': ema_cross_bear,
        'vol_ratio': vol_ratio, 'vol_spike': vol_spike,
        'bounce_bull': bounce_bull, 'bounce_bear': bounce_bear,
        'move10': round(move10, 2), 'pattern': pattern,
    }

print('✅ Fonctions indicateurs définies')


# ============================================================
# CELLULE 4 — Direction, Score & Niveaux TP/SL
# ============================================================
def determine_direction(ind: dict):
    """Réplique exacte de bot_headless.determine_direction (retourne tuple pour le backtest)."""
    if abs(ind['vwap_slope']) < ind['atr'] * 0.1:
        return None, 'vwap_flat'
    dist = ind['dist_vwap_sd']
    if dist > 4.5 or dist < -4.5:
        return None, 'sd_cap'
    price     = ind['price']
    above_200 = ind['above_200']
    above_50  = ind['above_50']
    if ind['bounce_bull']: return 'BUY',  'bounce_bull'
    if ind['bounce_bear']: return 'SELL', 'bounce_bear'
    vwap_long    = price < ind['vwap_lower1']
    vwap_short   = price > ind['vwap_upper1']
    struct_long  = ind['near_swing_sup'] or ind['near_poc']
    struct_short = ind['near_swing_res'] or ind['near_poc']
    vol_ok       = ind['vol_ratio'] > 1.0
    if vwap_long  and struct_long  and above_200 and above_50  and (vol_ok or ind['ema_cross_bull']): return 'BUY',  'vwap_1sd'
    if vwap_short and struct_short and not above_200 and not above_50 and (vol_ok or ind['ema_cross_bear']): return 'SELL', 'vwap_1sd'
    if price < ind['vwap_lower2'] and above_200 and vol_ok:     return 'BUY',  'vwap_2sd'
    if price > ind['vwap_upper2'] and not above_200 and vol_ok: return 'SELL', 'vwap_2sd'
    return None, 'no_setup'

def compute_score(ind: dict, direction: str):
    """Réplique exacte de bot_headless.compute_score."""
    score = 0; reasons = []; is_buy = direction == 'BUY'
    dist_sd = abs(ind['dist_vwap_sd'])
    if   dist_sd >= 2.0: score += 30; reasons.append(f'SD={dist_sd:.1f}>=2.0 (+30)')
    elif dist_sd >= 1.0: score += 18; reasons.append(f'SD={dist_sd:.1f}>=1.0 (+18)')
    elif dist_sd >= 0.5: score +=  8; reasons.append(f'SD={dist_sd:.1f}>=0.5 (+8)')
    if is_buy and ind['vwap_slope'] > 0:      score += 5; reasons.append('Slope+ (+5)')
    elif not is_buy and ind['vwap_slope'] < 0: score += 5; reasons.append('Slope- (+5)')
    if is_buy:
        if ind['near_swing_sup']:   score += 15; reasons.append('SwingSup (+15)')
        if ind['near_poc']:         score += 10; reasons.append('POC (+10)')
        if ind['price'] < ind['va_low']:  score += 10; reasons.append('BelowVAL (+10)')
        if ind['above_200']:        score +=  5; reasons.append('Above200 (+5)')
    else:
        if ind['near_swing_res']:   score += 15; reasons.append('SwingRes (+15)')
        if ind['near_poc']:         score += 10; reasons.append('POC (+10)')
        if ind['price'] > ind['va_high']: score += 10; reasons.append('AboveVAH (+10)')
        if not ind['above_200']:    score +=  5; reasons.append('Below200 (+5)')
    vr = ind['vol_ratio']
    if ind['bounce_bull'] or ind['bounce_bear']: score += 25; reasons.append(f'Bounce (+25)')
    elif vr >= 2.0: score += 20; reasons.append(f'VolSpike x{vr} (+20)')
    elif vr >= 1.4: score += 12; reasons.append(f'VolHigh x{vr} (+12)')
    elif vr >= 1.0: score +=  5; reasons.append(f'VolOK x{vr} (+5)')
    if is_buy  and ind['ema_cross_bull']: score += 10; reasons.append('GoldenX (+10)')
    elif not is_buy and ind['ema_cross_bear']: score += 10; reasons.append('DeathX (+10)')
    bull_p = {'Marteau', 'Engulfing haussier', 'Pin Bar haussier'}
    bear_p = {'Shooting Star', 'Engulfing baissier', 'Pin Bar baissier'}
    if is_buy  and ind['pattern'] in bull_p:   score += 15; reasons.append(f"{ind['pattern']} (+15)")
    elif not is_buy and ind['pattern'] in bear_p: score += 15; reasons.append(f"{ind['pattern']} (+15)")
    elif ind['pattern'] == 'Doji':             score +=  3; reasons.append('Doji (+3)')
    return min(score, 100), reasons

def calculate_tp_sl(direction: str, entry: float, atr: float,
                    swing_high: float, swing_low: float, mode: str = TRADING_MODE) -> dict:
    """Réplique exacte de bot_headless.calculate_tp_sl avec SL Cap."""
    m = MODES_CONFIG[mode]
    if direction == 'BUY':
        sl  = min(round(swing_low - atr * 0.2, 2), round(entry - atr * m['sl_mult'], 2))
        sl  = min(sl, round(entry - atr * max(m['sl_mult'], 1.5), 2))
        risk = abs(entry - sl)
        if risk > MAX_RISK_PTS:
            sl = round(entry - MAX_RISK_PTS, 2); risk = MAX_RISK_PTS
        return {'sl': sl, 'tp1': round(entry + risk * m['tp1_mult'], 2),
                'tp2': round(entry + risk * m['tp2_mult'], 2), 'tp3': round(entry + risk * m['tp3_mult'], 2)}
    else:
        sl  = max(round(swing_high + atr * 0.2, 2), round(entry + atr * m['sl_mult'], 2))
        sl  = max(sl, round(entry + atr * max(m['sl_mult'], 1.5), 2))
        risk = abs(sl - entry)
        if risk > MAX_RISK_PTS:
            sl = round(entry + MAX_RISK_PTS, 2); risk = MAX_RISK_PTS
        return {'sl': sl, 'tp1': round(entry - risk * m['tp1_mult'], 2),
                'tp2': round(entry - risk * m['tp2_mult'], 2), 'tp3': round(entry - risk * m['tp3_mult'], 2)}

print('✅ Fonctions signal & niveaux définies')


# ============================================================
# CELLULE 5a — Simulateur M1 optimisé (référence de comparaison)
# ============================================================
def simulate_trade_m1(direction: str, entry: float, levels: dict,
                       open_time: pd.Timestamp, df_m1: pd.DataFrame) -> list:
    """Simulateur M1 optimisé (searchsorted + numpy) — utilisé pour comparaison."""
    sl_orig     = levels['sl']
    tp1, tp2, tp3 = levels['tp1'], levels['tp2'], levels['tp3']
    start_pos   = df_m1.index.searchsorted(open_time)
    bars        = df_m1.iloc[start_pos:]
    if bars.empty: return []
    positions   = [
        {'id': 'P1', 'tp': tp1, 'sl': sl_orig, 'result': None, 'exit_px': None, 'closed': False},
        {'id': 'P2', 'tp': tp2, 'sl': sl_orig, 'result': None, 'exit_px': None, 'closed': False},
        {'id': 'P3', 'tp': tp3, 'sl': sl_orig, 'result': None, 'exit_px': None, 'closed': False},
    ]
    tp1_hit = tp2_hit = False
    highs = bars['High'].values; lows = bars['Low'].values; closes = bars['Close'].values
    for k in range(len(highs)):
        if all(p['closed'] for p in positions): break
        bh, bl = highs[k], lows[k]
        for p in positions:
            if p['closed']: continue
            tp_hit = bh >= p['tp'] if direction == 'BUY' else bl <= p['tp']
            sl_hit = bl <= p['sl'] if direction == 'BUY' else bh >= p['sl']
            if tp_hit and sl_hit:
                tp_hit = abs(p['tp'] - entry) < abs(p['sl'] - entry)
                sl_hit = not tp_hit
            if tp_hit:
                p['closed'] = True; p['exit_px'] = p['tp']
                pts = (p['tp'] - entry) if direction == 'BUY' else (entry - p['tp'])
                p['result'] = p['id'].replace('P', 'TP')
                p['pnl']    = round(pts * PNL_PER_POINT, 2)
                if p['id'] == 'P1' and not tp1_hit:
                    tp1_hit = True
                    for other in positions:
                        if not other['closed']: other['sl'] = entry
                if p['id'] == 'P2' and not tp2_hit and tp1_hit:
                    tp2_hit = True
                    for other in positions:
                        if not other['closed']: other['sl'] = tp1
            elif sl_hit:
                p['closed'] = True; p['exit_px'] = p['sl']
                pts = abs(entry - p['sl'])
                p['result'] = 'BE' if abs(p['sl'] - entry) < 0.01 else 'SL'
                p['pnl']    = 0.0 if p['result'] == 'BE' else round(-pts * PNL_PER_POINT, 2)
    last_close = closes[-1]
    for p in positions:
        if not p['closed']:
            pts = (last_close - entry) if direction == 'BUY' else (entry - last_close)
            p['result'] = 'OPEN_END'; p['exit_px'] = last_close
            p['pnl']    = round(pts * PNL_PER_POINT, 2); p['closed'] = True
    return positions

print('✅ Simulateur M1 (référence) défini')


# ============================================================
# CELLULE 5b — Simulateur TICK (Polars LazyFrame) — CŒUR DU BACKTEST
# ============================================================

def _detect_tick_columns(path: Path) -> dict:
    """
    Détecte automatiquement les noms et format des colonnes du fichier tick.
    MT5 exporte en général : Time, Bid, Ask, Last, Volume, Flags
    ou                     : <DATE>,<TIME>,<BID>,<ASK>,...
    """
    with open(path, 'r') as f:
        header = f.readline().strip()
        sample = f.readline().strip()
    print(f'  Header : {header}')
    print(f'  Sample : {sample}')
    cols = [c.strip().strip('<>') for c in header.split(',')]
    # Détecter la colonne temps
    time_col = next((c for c in cols if 'time' in c.lower() or 'date' in c.lower()), cols[0])
    bid_col  = next((c for c in cols if 'bid' in c.lower()), None)
    ask_col  = next((c for c in cols if 'ask' in c.lower()), None)
    if bid_col is None or ask_col is None:
        raise ValueError(f"Colonnes Bid/Ask introuvables dans : {cols}")
    return {'time': time_col, 'bid': bid_col, 'ask': ask_col, 'all_cols': cols}

# ── Détection et chargement du LazyFrame ─────────────────────
print('🔍 Détection des colonnes tick...')
_TICK_COLS = _detect_tick_columns(PATH_TICKS)
print(f'  → Time="{_TICK_COLS["time"]}" | Bid="{_TICK_COLS["bid"]}" | Ask="{_TICK_COLS["ask"]}"')

# LazyFrame : rien n'est chargé en mémoire ici
_TICKS_LAZY = (
    pl.scan_csv(
        PATH_TICKS,
        try_parse_dates=False,     # On parse manuellement pour contrôler le format
        infer_schema_length=1000,
    )
    .rename({_TICK_COLS['time']: 'RawTime', _TICK_COLS['bid']: 'Bid', _TICK_COLS['ask']: 'Ask'})
    .select(['RawTime', 'Bid', 'Ask'])
    .with_columns([
        pl.col('Bid').cast(pl.Float64),
        pl.col('Ask').cast(pl.Float64),
    ])
)
print('✅ LazyFrame ticks prêt (lecture différée)')


def _fetch_ticks_for_trade(signal_time: pd.Timestamp,
                            max_duration_hours: int = 48) -> pl.DataFrame:
    """
    Extrait les ticks pour un trade donné via Polars LazyFrame.
    Ne charge en mémoire que les ticks dans la fenêtre [signal_time, +max_duration_hours].
    Gère automatiquement les deux formats de date MT5 :
      - "2025.11.10 01:00:00.123"  (points)
      - "2025-11-10 01:00:00.123"  (tirets)
    """
    # Construire les bornes de filtre en string (adapté au format MT5)
    t_start = signal_time + pd.Timedelta(hours=GMT_OFFSET)  # Remettre en GMT+3 broker
    t_end   = t_start + pd.Timedelta(hours=max_duration_hours)

    # Format string pour la comparaison (MT5 utilise des points dans les dates)
    fmt_start = t_start.strftime('%Y.%m.%d %H:%M:%S')
    fmt_end   = t_end.strftime('%Y.%m.%d %H:%M:%S')

    ticks = (
        _TICKS_LAZY
        .filter(
            (pl.col('RawTime') >= fmt_start) &
            (pl.col('RawTime') <= fmt_end)
        )
        .collect(streaming=True)
    )
    return ticks


def simulate_trade_ticks(direction: str, entry_signal: float, levels: dict,
                          open_time: pd.Timestamp,
                          slippage: float = SLIPPAGE_PTS) -> list:
    """
    Simule le parcours d'un trade sur données tick réelles.

    Logique d'exécution réelle :
      BUY  → entrée sur Ask + slippage | sorties TP/SL sur Bid
      SELL → entrée sur Bid - slippage | sorties TP/SL sur Ask

    Breakeven identique à M1 : TP1 touché → SL P2/P3 → entry.
    Retourne la même structure que simulate_trade_m1.
    """
    ticks = _fetch_ticks_for_trade(open_time)

    if ticks.is_empty():
        return []

    # ── Prix d'entrée réel avec slippage ──────────────────────
    first_tick = ticks.row(0, named=True)
    if direction == 'BUY':
        entry = round(first_tick['Ask'] + slippage, 2)
    else:
        entry = round(first_tick['Bid'] - slippage, 2)

    # ── Recalcul des niveaux sur entrée réelle (slippage inclus) ──
    # On décale les niveaux du même delta que l'entrée réelle vs signal
    delta  = entry - entry_signal
    sl     = round(levels['sl']  + delta, 2)
    tp1    = round(levels['tp1'] + delta, 2)
    tp2    = round(levels['tp2'] + delta, 2)
    tp3    = round(levels['tp3'] + delta, 2)

    positions = [
        {'id': 'P1', 'tp': tp1, 'sl': sl, 'result': None, 'exit_px': None, 'pnl': 0.0, 'closed': False,
         'entry_real': entry, 'slippage': round(abs(delta), 4)},
        {'id': 'P2', 'tp': tp2, 'sl': sl, 'result': None, 'exit_px': None, 'pnl': 0.0, 'closed': False,
         'entry_real': entry, 'slippage': round(abs(delta), 4)},
        {'id': 'P3', 'tp': tp3, 'sl': sl, 'result': None, 'exit_px': None, 'pnl': 0.0, 'closed': False,
         'entry_real': entry, 'slippage': round(abs(delta), 4)},
    ]
    tp1_hit = tp2_hit = False

    # ── Colonnes numpy pour la boucle ────────────────────────
    bids  = ticks['Bid'].to_numpy()
    asks  = ticks['Ask'].to_numpy()

    for k in range(len(bids)):
        if all(p['closed'] for p in positions): break

        # BUY : sortie TP sur Bid (market bid), SL sur Bid
        # SELL: sortie TP sur Ask (market ask), SL sur Ask
        exec_price = bids[k] if direction == 'BUY' else asks[k]

        for p in positions:
            if p['closed']: continue

            if direction == 'BUY':
                tp_hit = exec_price >= p['tp']
                sl_hit = exec_price <= p['sl']
            else:
                tp_hit = exec_price <= p['tp']
                sl_hit = exec_price >= p['sl']

            if tp_hit and sl_hit:
                tp_hit = abs(p['tp'] - entry) < abs(p['sl'] - entry)
                sl_hit = not tp_hit

            if tp_hit:
                p['closed']  = True
                p['exit_px'] = p['tp']
                pts = (p['tp'] - entry) if direction == 'BUY' else (entry - p['tp'])
                p['result']  = p['id'].replace('P', 'TP')
                p['pnl']     = round(pts * PNL_PER_POINT, 2)

                if p['id'] == 'P1' and not tp1_hit:
                    tp1_hit = True
                    for other in positions:
                        if not other['closed']: other['sl'] = entry

                if p['id'] == 'P2' and not tp2_hit and tp1_hit:
                    tp2_hit = True
                    for other in positions:
                        if not other['closed']: other['sl'] = tp1

            elif sl_hit:
                p['closed']  = True
                p['exit_px'] = p['sl']
                pts          = abs(entry - p['sl'])
                p['result']  = 'BE' if abs(p['sl'] - entry) < 0.01 else 'SL'
                p['pnl']     = 0.0 if p['result'] == 'BE' else round(-pts * PNL_PER_POINT, 2)

    # Positions encore ouvertes → fermées au dernier tick
    last_bid = bids[-1]; last_ask = asks[-1]
    last_exec = last_bid if direction == 'BUY' else last_ask
    for p in positions:
        if not p['closed']:
            pts = (last_exec - entry) if direction == 'BUY' else (entry - last_exec)
            p['result']  = 'OPEN_END'
            p['exit_px'] = last_exec
            p['pnl']     = round(pts * PNL_PER_POINT, 2)
            p['closed']  = True

    return positions

print('✅ Simulateur TICK (Polars) défini')
print(f'   Slippage : {SLIPPAGE_PTS} pt | BUY sur Ask+slip | SELL sur Bid-slip')
print(f'   Sorties  : BUY sur Bid | SELL sur Ask (prix d\'exécution réels MT5)')


# ============================================================
# CELLULE 6 — Pré-calcul des indicateurs (M5, une seule fois)
# ============================================================
def precompute_all_indicators(df_m5: pd.DataFrame, window: int = 100) -> OrderedDict:
    """Pré-calcule tous les indicateurs M5 une seule fois."""
    t0           = time.time()
    candle_times = df_m5.index.tolist()
    precomp      = OrderedDict()
    total        = len(df_m5)

    with tqdm(total=total - window, desc='Pré-calcul indicateurs M5',
              unit='bougies', ncols=80) as pbar:
        for i in range(window, total):
            ts = candle_times[i]
            try:
                precomp[ts] = compute_indicators(df_m5.iloc[i - window: i + 1])
            except Exception:
                pass
            pbar.update(1)

    print(f'\n✅ Pré-calcul terminé : {len(precomp):,} timestamps en {time.time()-t0:.1f}s')
    return precomp

print('⏳ Pré-calcul des indicateurs en cours...')
precomp_indicators = precompute_all_indicators(df_m5)


# ============================================================
# CELLULE 7 — Moteur de Backtest générique (M1 ou Tick)
# ============================================================
def run_backtest_engine(precomp: OrderedDict,
                        simulator_fn,          # simulate_trade_m1 ou simulate_trade_ticks
                        simulator_kwargs: dict, # arguments supplémentaires pour le simulateur
                        mode: str = TRADING_MODE,
                        score_min_override: float = None,
                        sl_mult_override: float = None,
                        label: str = 'Backtest') -> tuple:
    """
    Moteur événementiel générique.
    Accepte n'importe quel simulateur (M1 ou Tick) via simulator_fn.
    """
    m = MODES_CONFIG[mode].copy()
    if score_min_override is not None: m['score_min'] = score_min_override
    original_sl_mult = MODES_CONFIG[mode]['sl_mult']
    if sl_mult_override is not None:   MODES_CONFIG[mode]['sl_mult'] = sl_mult_override

    trades_log      = []
    signals_ignored = []
    daily_pnl       = 0.0
    trades_today    = 0
    last_sl_time    = pd.Timestamp('1970-01-01')
    last_signal_ts  = pd.Timestamp('1970-01-01')
    current_date    = None
    daily_stopped   = False
    n_vwap_flat = n_sd_cap = n_no_setup = n_score_low = 0
    n_session_end = n_daily_stop = n_cooldown = 0

    try:
        items = list(precomp.items())
        with tqdm(total=len(items), desc=f'🚀 {label}', unit='bougies', ncols=80) as pbar:
            for ts, ind in items:
                pbar.update(1)
                day = ts.date()

                if day != current_date:
                    current_date  = day
                    daily_pnl     = 0.0
                    trades_today  = 0
                    daily_stopped = False

                if daily_stopped:
                    n_daily_stop += 1; continue

                if daily_pnl < DAILY_MAX_LOSS:
                    daily_stopped = True; n_daily_stop += 1; continue

                if trades_today >= MAX_TRADES_PER_DAY: continue

                h = ts.hour
                if h >= SESSION_END_H:
                    n_session_end += 1; continue

                if (ts - last_signal_ts).total_seconds() < m['cooldown']:
                    n_cooldown += 1; continue

                direction, reject_reason = determine_direction(ind)
                if direction is None:
                    if   reject_reason == 'vwap_flat': n_vwap_flat += 1
                    elif reject_reason == 'sd_cap':    n_sd_cap    += 1
                    else:                              n_no_setup  += 1
                    signals_ignored.append({'ts': ts, 'reason': reject_reason})
                    continue

                score, _ = compute_score(ind, direction)
                threshold = m['score_min']
                if h >= SESSION_MID_H:                               threshold += 10
                if (ts - last_sl_time).total_seconds() < 300:       threshold += 20

                if score < threshold:
                    n_score_low += 1
                    signals_ignored.append({'ts': ts, 'reason': f'score={score}<{threshold}',
                                             'direction': direction, 'score': score})
                    continue

                levels = calculate_tp_sl(direction, ind['price'], ind['atr'],
                                          ind['swing_high'], ind['swing_low'], mode=mode)
                entry_signal = ind['price']

                # ── Appel du simulateur (M1 ou Tick) ─────────────
                positions = simulator_fn(direction, entry_signal, levels, ts, **simulator_kwargs)
                if not positions: continue

                seq_pnl       = sum(p['pnl'] for p in positions)
                daily_pnl     = round(daily_pnl + seq_pnl, 2)
                trades_today  += 1
                last_signal_ts = ts

                if any(p['result'] == 'SL' for p in positions):
                    last_sl_time = ts

                # Récupérer l'entrée réelle (ticks) ou signal (M1)
                entry_real = positions[0].get('entry_real', entry_signal)
                slippage   = positions[0].get('slippage', 0.0)

                for p in positions:
                    trades_log.append({
                        'open_time':   ts,
                        'day':         day,
                        'direction':   direction,
                        'entry_signal': entry_signal,
                        'entry_real':  entry_real,
                        'slippage':    slippage,
                        'atr':         ind['atr'],
                        'score':       score,
                        'threshold':   threshold,
                        'sl':          levels['sl'],
                        'tp1':         levels['tp1'],
                        'tp2':         levels['tp2'],
                        'tp3':         levels['tp3'],
                        'position':    p['id'],
                        'result':      p['result'],
                        'exit_px':     p['exit_px'],
                        'pnl':         p['pnl'],
                        'daily_pnl':   daily_pnl,
                        'hour':        h,
                        'vwap_slope':  ind['vwap_slope'],
                        'dist_sd':     ind['dist_vwap_sd'],
                        'pattern':     ind['pattern'],
                        'vol_ratio':   ind['vol_ratio'],
                    })

    finally:
        if sl_mult_override is not None:
            MODES_CONFIG[mode]['sl_mult'] = original_sl_mult

    filter_stats = {
        'vwap_flat':   n_vwap_flat,   'sd_cap':      n_sd_cap,
        'no_setup':    n_no_setup,    'score_low':   n_score_low,
        'session_end': n_session_end, 'daily_stop':  n_daily_stop,
        'cooldown':    n_cooldown,
    }
    return pd.DataFrame(trades_log), pd.DataFrame(signals_ignored), filter_stats

print('✅ Moteur de backtest générique défini')


# ============================================================
# CELLULE 8 — Lancement des deux backtests (M1 puis Ticks)
# ============================================================

# ── Backtest M1 (référence approximative) ────────────────────
print('=' * 60)
print('📊 BACKTEST M1 (référence approximative)')
df_trades_m1, df_ignored_m1, fs_m1 = run_backtest_engine(
    precomp_indicators,
    simulate_trade_m1,
    {'df_m1': df_m1},
    mode=TRADING_MODE,
    label='Backtest M1',
)
print(f'✅ M1 terminé : {len(df_trades_m1)//3 if len(df_trades_m1)>0 else 0} séquences')

# ── Backtest Tick (résolution chirurgicale) ───────────────────
print('\n' + '=' * 60)
print('🎯 BACKTEST TICK (résolution réelle — Polars LazyFrame)')
print(f'   Slippage : {SLIPPAGE_PTS} pt | ~{len(df_trades_m1)//3} trades à simuler')
df_trades_tick, df_ignored_tick, fs_tick = run_backtest_engine(
    precomp_indicators,
    simulate_trade_ticks,
    {},                              # Polars accède au LazyFrame global _TICKS_LAZY
    mode=TRADING_MODE,
    label='Backtest Tick',
)
print(f'✅ Tick terminé : {len(df_trades_tick)//3 if len(df_trades_tick)>0 else 0} séquences')


# ============================================================
# CELLULE 9 — Statistiques & Comparaison M1 vs Ticks
# ============================================================
def compute_stats_full(df: pd.DataFrame, fs: dict, label: str) -> dict:
    if df.empty:
        print(f'⚠️ [{label}] Aucun trade.')
        return {}

    seq = df.groupby('open_time')['pnl'].sum()
    net   = round(df['pnl'].sum(), 2)
    gw    = df[df['pnl'] > 0]['pnl'].sum()
    gl    = abs(df[df['pnl'] < 0]['pnl'].sum())
    pf    = round(gw / gl, 2) if gl > 0 else float('inf')
    n_seq = len(seq)
    wr    = round((seq > 0).sum() / n_seq * 100, 1) if n_seq > 0 else 0
    equity = seq.cumsum()
    dd    = round((equity - equity.cummax()).min(), 2)

    print(f'\n{"═"*55}')
    print(f'  [{label}] MODE : {TRADING_MODE.upper()}')
    print(f'  Période      : {df["open_time"].min().date()} → {df["open_time"].max().date()}')
    print(f'  Nb séquences : {n_seq}')
    print(f'  Net Profit   : {net:+.2f} $')
    print(f'  Profit Factor: {pf}')
    print(f'  Max Drawdown : {dd:.2f} $')
    print(f'  Winrate (séq): {wr} %')
    print(f'  Meilleure    : {seq.max():+.2f} $')
    print(f'  Pire         : {seq.min():+.2f} $')

    print(f'\n  Résultats par palier :')
    for tp in ['TP1', 'TP2', 'TP3', 'SL', 'BE', 'OPEN_END']:
        n  = (df['result'] == tp).sum()
        pn = df[df['result'] == tp]['pnl'].sum()
        if n > 0: print(f'    {tp:8s} : {n:4d} fois | PnL cumulé : {pn:+.2f} $')

    if 'slippage' in df.columns and df['slippage'].sum() > 0:
        avg_slip = df.groupby('open_time')['slippage'].first().mean()
        tot_slip_cost = df.groupby('open_time')['slippage'].first().sum() * PNL_PER_POINT * 3
        print(f'\n  Friction (Ticks) :')
        print(f'    Slippage moyen   : {avg_slip:.4f} pts')
        print(f'    Coût total slip  : ~{tot_slip_cost:.2f} $')

    print(f'{"═"*55}')
    return {'net': net, 'pf': pf, 'dd': dd, 'wr': wr, 'n_seq': n_seq, 'equity': equity}

print('\n=== STATISTIQUES M1 ===')
stats_m1 = compute_stats_full(df_trades_m1, fs_m1, 'M1')

print('\n=== STATISTIQUES TICK ===')
stats_tick = compute_stats_full(df_trades_tick, fs_tick, 'Tick')

# ── Tableau comparatif M1 vs Ticks ───────────────────────────
if stats_m1 and stats_tick:
    print('\n' + '═' * 60)
    print('  📊 COMPARAISON : Résolution M1 (approx.) vs Ticks (réel)')
    print('═' * 60)
    metrics = [
        ('Net Profit ($)',    stats_m1['net'],   stats_tick['net'],   '{:+.2f}'),
        ('Profit Factor',     stats_m1['pf'],    stats_tick['pf'],    '{:.2f}'),
        ('Max Drawdown ($)',  stats_m1['dd'],    stats_tick['dd'],    '{:.2f}'),
        ('Winrate %',         stats_m1['wr'],    stats_tick['wr'],    '{:.1f}'),
        ('Nb séquences',      stats_m1['n_seq'], stats_tick['n_seq'], '{:.0f}'),
    ]
    print(f'  {"Métrique":<22} {"M1 (approx)":>14} {"Ticks (réel)":>14} {"Écart":>10}')
    print(f'  {"-"*22} {"-"*14} {"-"*14} {"-"*10}')
    for name, v_m1, v_tick, fmt in metrics:
        diff = v_tick - v_m1
        sign = '+' if diff > 0 else ''
        print(f'  {name:<22} {fmt.format(v_m1):>14} {fmt.format(v_tick):>14} {sign}{diff:>+.2f}')
    print('═' * 60)

    # ── Comparaison trade par trade ───────────────────────────
    if not df_trades_m1.empty and not df_trades_tick.empty:
        print('\n  📋 Comparaison par séquence (Top 10) :')
        seq_m1   = df_trades_m1.groupby('open_time')['pnl'].sum().rename('pnl_m1')
        seq_tick = df_trades_tick.groupby('open_time')['pnl'].sum().rename('pnl_tick')
        comp = pd.concat([seq_m1, seq_tick], axis=1).dropna()
        comp['diff'] = comp['pnl_tick'] - comp['pnl_m1']
        comp['impact'] = comp['diff'].apply(lambda x: '✅' if x > 0 else ('⚠️' if x < 0 else '➖'))
        comp_sorted = comp.sort_values('diff', ascending=False)
        print(comp_sorted.head(10).to_string(float_format='{:+.2f}'.format))
        print(f'\n  Impact moyen par trade : {comp["diff"].mean():+.2f} $')
        print(f'  Trades meilleurs en Tick : {(comp["diff"] > 0).sum()} / {len(comp)}')
        print(f'  Trades pires en Tick     : {(comp["diff"] < 0).sum()} / {len(comp)}')


# ============================================================
# CELLULE 10 — Graphiques (Equity Curves + Comparaison)
# ============================================================
if not df_trades_m1.empty and not df_trades_tick.empty:
    fig = plt.figure(figsize=(22, 16))
    fig.suptitle(
        f'Gold Bot v2 — Backtest Ultra-Fidèle {TRADING_MODE.upper()} | M1 vs Ticks',
        fontsize=14, fontweight='bold', color='white', y=0.99
    )

    # ── (1) Equity curves superposées ────────────────────────
    ax1 = fig.add_subplot(3, 2, (1, 2))
    eq_m1   = stats_m1['equity']
    eq_tick = stats_tick['equity']
    ax1.fill_between(eq_tick.index, eq_tick.values, alpha=0.15, color='#00ff88')
    ax1.plot(eq_m1.index,   eq_m1.values,   linewidth=1.5, color='#ff8800',
             label=f'M1  Net: {eq_m1.iloc[-1]:+.0f}$', linestyle='--', alpha=0.85)
    ax1.plot(eq_tick.index, eq_tick.values, linewidth=2.0, color='#00ff88',
             label=f'Tick Net: {eq_tick.iloc[-1]:+.0f}$')
    ax1.axhline(0, color='white', linewidth=0.5, linestyle='--', alpha=0.4)
    ax1.set_title('📈 Equity Curves — M1 (approx.) vs Ticks (réel)', fontsize=12, color='#00d4ff')
    ax1.set_ylabel('PnL cumulé ($)', color='white')
    ax1.tick_params(colors='white'); ax1.grid(alpha=0.15)
    ax1.legend(fontsize=10)

    # ── (2) PnL journalier Ticks ──────────────────────────────
    ax2 = fig.add_subplot(3, 2, 3)
    daily = df_trades_tick.groupby('day')['pnl'].sum()
    bar_colors = ['#00ff88' if v >= 0 else '#ff4466' for v in daily.values]
    ax2.bar(range(len(daily)), daily.values, color=bar_colors, alpha=0.85)
    ax2.axhline(0, color='white', linewidth=0.5)
    ax2.axhline(DAILY_MAX_LOSS, color='#ff8800', linewidth=1.2, linestyle='--',
                label=f'Daily Stop {DAILY_MAX_LOSS}$')
    ax2.set_title('📅 PnL Journalier (Ticks)', fontsize=11, color='#00d4ff')
    ax2.set_ylabel('PnL ($)', color='white')
    ax2.tick_params(colors='white'); ax2.grid(alpha=0.15, axis='y')
    ax2.legend(fontsize=8)

    # ── (3) Répartition résultats Ticks ───────────────────────
    ax3 = fig.add_subplot(3, 2, 4)
    result_counts = df_trades_tick['result'].value_counts()
    palette = {'TP1': '#00ff88', 'TP2': '#00cc66', 'TP3': '#009944',
               'SL': '#ff4466', 'BE': '#ffdd44', 'OPEN_END': '#888888'}
    colors_pie = [palette.get(r, '#aaaaaa') for r in result_counts.index]
    ax3.pie(result_counts.values, labels=result_counts.index,
            autopct='%1.1f%%', colors=colors_pie,
            textprops={'color': 'white', 'fontsize': 9})
    ax3.set_title('🎯 Résultats Tick (Positions)', fontsize=11, color='#00d4ff')

    # ── (4) Différence M1 vs Tick par trade ───────────────────
    ax4 = fig.add_subplot(3, 2, 5)
    if not df_trades_m1.empty:
        seq_m1   = df_trades_m1.groupby('open_time')['pnl'].sum()
        seq_tick = df_trades_tick.groupby('open_time')['pnl'].sum()
        comp_df  = pd.concat([seq_m1, seq_tick], axis=1, keys=['m1', 'tick']).dropna()
        diff     = comp_df['tick'] - comp_df['m1']
        diff_colors = ['#00ff88' if v >= 0 else '#ff4466' for v in diff.values]
        ax4.bar(range(len(diff)), diff.values, color=diff_colors, alpha=0.85)
        ax4.axhline(0, color='white', linewidth=0.5)
        ax4.set_title('📊 Différence Ticks − M1 par trade ($)', fontsize=11, color='#00d4ff')
        ax4.set_xlabel('Numéro de trade', color='white')
        ax4.set_ylabel('Écart ($)', color='white')
        ax4.tick_params(colors='white'); ax4.grid(alpha=0.15, axis='y')
        ax4.text(0.98, 0.95, f'Moy: {diff.mean():+.1f}$\nTotal: {diff.sum():+.1f}$',
                 transform=ax4.transAxes, va='top', ha='right', color='#ffdd44', fontsize=9)

    # ── (5) Impact des filtres ────────────────────────────────
    ax5 = fig.add_subplot(3, 2, 6)
    labels_f = list(fs_tick.keys())
    values_f = list(fs_tick.values())
    colors_f = ['#ff8800', '#ff4466', '#888888', '#dd4400', '#ff6600', '#cc2200', '#996600']
    bars5 = ax5.barh(labels_f, values_f, color=colors_f[:len(labels_f)], alpha=0.85)
    ax5.set_title('🚦 Signaux Ignorés par Filtre (Tick)', fontsize=11, color='#00d4ff')
    ax5.set_xlabel('Nombre de bougies filtrées', color='white')
    ax5.tick_params(colors='white'); ax5.grid(alpha=0.15, axis='x')
    for bar, val in zip(bars5, values_f):
        ax5.text(bar.get_width() + 10, bar.get_y() + bar.get_height() / 2,
                 f'{val:,}', va='center', color='white', fontsize=8)

    plt.tight_layout(rect=[0, 0, 1, 0.98])
    plt.savefig('backtest_tick_results.png', dpi=150, bbox_inches='tight',
                facecolor='#0d0d0d', edgecolor='none')
    plt.show()
    print('✅ Graphique sauvegardé → backtest_tick_results.png')


# ============================================================
# CELLULE 11 — Analyse de Sensibilité (Tick, rapide)
# ============================================================
print('🔬 Analyse de sensibilité Tick en cours...')
t_sens      = time.time()
score_range = [35, 40, 45, 50, 55, 60, 65, 70]
sl_range    = [1.2, 1.5, 1.8, 2.2, 2.5]
results_matrix = []

for sm in tqdm(score_range, desc='score_min', ncols=60):
    for slm in sl_range:
        try:
            dt, _, fs = run_backtest_engine(
                precomp_indicators,
                simulate_trade_ticks,
                {},
                mode=TRADING_MODE,
                score_min_override=sm,
                sl_mult_override=slm,
                label=f's={sm} sl={slm}',
            )
            if dt.empty:
                results_matrix.append({'score_min': sm, 'sl_mult': slm,
                                        'net_profit': 0, 'n_seq': 0, 'winrate': 0, 'pf': 0})
                continue
            seq  = dt.groupby('open_time')['pnl'].sum()
            net  = round(seq.sum(), 2)
            n_seq = len(seq)
            wr   = round((seq > 0).sum() / n_seq * 100, 1) if n_seq > 0 else 0
            gl   = abs(dt[dt['pnl'] < 0]['pnl'].sum())
            gw   = dt[dt['pnl'] > 0]['pnl'].sum()
            pf   = round(gw / gl, 2) if gl > 0 else 999
            results_matrix.append({'score_min': sm, 'sl_mult': slm,
                                    'net_profit': net, 'n_seq': n_seq, 'winrate': wr, 'pf': pf})
        except Exception as e:
            results_matrix.append({'score_min': sm, 'sl_mult': slm,
                                    'net_profit': 0, 'n_seq': 0, 'winrate': 0, 'pf': 0})

print(f'✅ Sensibilité Tick terminée en {time.time()-t_sens:.1f}s ({len(score_range)*len(sl_range)} combos)')

df_sens  = pd.DataFrame(results_matrix)
pivot    = df_sens.pivot(index='sl_mult', columns='score_min', values='net_profit')
pivot_wr = df_sens.pivot(index='sl_mult', columns='score_min', values='winrate')

fig2, axes2 = plt.subplots(1, 2, figsize=(18, 6))
fig2.suptitle('Sensibilité Tick — Net Profit & Winrate', fontsize=13, fontweight='bold', color='white')

im1 = axes2[0].imshow(pivot.values, aspect='auto', cmap='RdYlGn')
axes2[0].set_xticks(range(len(pivot.columns))); axes2[0].set_xticklabels(pivot.columns, color='white')
axes2[0].set_yticks(range(len(pivot.index)));  axes2[0].set_yticklabels(pivot.index, color='white')
axes2[0].set_xlabel('Score Min', color='white'); axes2[0].set_ylabel('SL Mult', color='white')
axes2[0].set_title('Net Profit ($) — Ticks réels', color='#00d4ff')
for i in range(len(pivot.index)):
    for j in range(len(pivot.columns)):
        axes2[0].text(j, i, f"{pivot.values[i,j]:.0f}", ha='center', va='center', fontsize=8,
                      color='black' if abs(pivot.values[i,j]) < 200 else 'white')
plt.colorbar(im1, ax=axes2[0])

im2 = axes2[1].imshow(pivot_wr.values, aspect='auto', cmap='RdYlGn', vmin=30, vmax=80)
axes2[1].set_xticks(range(len(pivot_wr.columns))); axes2[1].set_xticklabels(pivot_wr.columns, color='white')
axes2[1].set_yticks(range(len(pivot_wr.index)));  axes2[1].set_yticklabels(pivot_wr.index, color='white')
axes2[1].set_xlabel('Score Min', color='white')
axes2[1].set_title('Winrate % — Ticks réels', color='#00d4ff')
for i in range(len(pivot_wr.index)):
    for j in range(len(pivot_wr.columns)):
        axes2[1].text(j, i, f"{pivot_wr.values[i,j]:.0f}%", ha='center', va='center', fontsize=8, color='black')
plt.colorbar(im2, ax=axes2[1])
plt.tight_layout()
plt.savefig('sensitivity_tick.png', dpi=130, bbox_inches='tight', facecolor='#0d0d0d', edgecolor='none')
plt.show()

best = df_sens.loc[df_sens['net_profit'].idxmax()]
print(f'\n🏆 Meilleure combinaison Tick : score_min={best["score_min"]} | '
      f'sl_mult={best["sl_mult"]} → Net: {best["net_profit"]:+.2f}$ | WR: {best["winrate"]}%')


# ============================================================
# CELLULE 12 — Notes & Paramètres de référence
# ============================================================
print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📌 NOTES D'UTILISATION — BackTest v3 Tick
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Architecture :
  M5 → signaux (indicateurs, score, filtres)
  Ticks → exécution chirurgicale par trade

Friction réelle simulée :
  BUY  : entrée Ask + 0.5 pt (slippage)
  SELL : entrée Bid - 0.5 pt (slippage)
  BUY  : sortie TP/SL sur Bid (prix exécution réel MT5)
  SELL : sortie TP/SL sur Ask (prix exécution réel MT5)

Polars LazyFrame :
  141.7M ticks JAMAIS chargés en RAM d'un coup.
  Filtrage par plage de temps par trade uniquement.
  Streaming = mémoire constante (~50-200 MB/trade).

Paramètres fixes :
  MAX_RISK_PTS   = 22.0   (SL Cap absolu)
  DAILY_MAX_LOSS = -150$  (coupe-circuit)
  SESSION_END_H  = 15h    (arrêt nouveaux trades)
  GMT_OFFSET     = -1h    (Broker GMT+3 → PC GMT+2)
  SLIPPAGE_PTS   = 0.5    (latence broker)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")
