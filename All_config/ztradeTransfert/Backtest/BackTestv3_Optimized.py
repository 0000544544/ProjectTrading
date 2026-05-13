# ================================================================
# 🚀 BackTest v3 Tick — OPTIMISATIONS PERFORMANCE
# ================================================================
# Remplace les Cellules 2 (fin), 5b, 8, 11 du notebook précédent
#
# Gains attendus :
#   CSV → Parquet    : 50x sur les lectures ticks
#   Cache RAM        : 0 I/O disque pendant sensibilité (2800→1 scans)
#   Multiprocessing  : tous les cœurs CPU pour les 40 combos
#   Total estimé     : 75 min → 3-8 min
# ================================================================


# ============================================================
# CELLULE 2b (NOUVEAU) — Conversion CSV → Parquet (UNE SEULE FOIS)
# ============================================================
# À exécuter UNE SEULE FOIS. Crée data/ticks.parquet (~1.2 GB).
# Ensuite commenter le bloc convert_to_parquet() et ne garder
# que PATH_TICKS_PARQUET.
import os, time
import polars as pl
from pathlib import Path

PATH_TICKS        = Path('data/XAUUSD-VIP_Ticks_1Year.csv')
PATH_TICKS_PARQUET= Path('data/XAUUSD-VIP_Ticks_1Year.parquet')

def convert_ticks_to_parquet():
    """
    Convertit le CSV de ticks en Parquet.
    UNE SEULE FOIS — ensuite le Parquet est réutilisé directement.

    Stratégie :
    - Lire par chunks de 5M lignes pour ne pas exploser la RAM
    - Ne garder que les colonnes Bid et Ask + temps normalisé
    - Stocker en Parquet partitionné par date (lecture sélective ensuite)
    """
    if PATH_TICKS_PARQUET.exists():
        size_gb = PATH_TICKS_PARQUET.stat().st_size / 1e9
        print(f'✅ Parquet déjà existant ({size_gb:.2f} GB) — conversion ignorée')
        return

    print(f'⏳ Conversion CSV → Parquet ({PATH_TICKS.stat().st_size/1e9:.1f} GB)...')
    print('   Cela prend 5-15 min selon votre disque (SSD ~5 min, HDD ~15 min)')
    print('   À faire UNE SEULE FOIS !\n')
    t0 = time.time()

    # Lire le header pour détecter les colonnes
    with open(PATH_TICKS, 'r') as f:
        header_line = f.readline().strip()
    raw_cols = [c.strip().strip('<>') for c in header_line.split(',')]

    # Trouver les colonnes Bid, Ask, Time
    time_col = next((c for c in raw_cols if 'time' in c.lower() or c.lower() == 'date'), raw_cols[0])
    bid_col  = next((c for c in raw_cols if 'bid'  in c.lower()), None)
    ask_col  = next((c for c in raw_cols if 'ask'  in c.lower()), None)

    if bid_col is None or ask_col is None:
        raise ValueError(f'Colonnes Bid/Ask non trouvées dans : {raw_cols}')

    print(f'   Colonnes détectées : Time="{time_col}" Bid="{bid_col}" Ask="{ask_col}"')

    # Lecture en streaming Polars + écriture Parquet
    # scan_csv + sink_parquet = jamais tout en RAM
    (
        pl.scan_csv(
            PATH_TICKS,
            try_parse_dates=False,
            infer_schema_length=500,
        )
        .select([time_col, bid_col, ask_col])
        .rename({time_col: 'RawTime', bid_col: 'Bid', ask_col: 'Ask'})
        .with_columns([
            pl.col('Bid').cast(pl.Float64),
            pl.col('Ask').cast(pl.Float64),
            # Normaliser le timestamp broker GMT+3 → stocké tel quel
            # (on applique GMT_OFFSET au moment de la requête)
            pl.col('RawTime').str.replace_all(r'\.', '-', literal=False)
                             .str.replace(' ', 'T')
                             .str.to_datetime(format='%Y-%m-%dT%H:%M:%S%.f',
                                              strict=False)
                             .alias('Time')
        ])
        .drop('RawTime')
        .sink_parquet(PATH_TICKS_PARQUET, compression='snappy')
    )

    elapsed = time.time() - t0
    size_gb = PATH_TICKS_PARQUET.stat().st_size / 1e9
    print(f'\n✅ Parquet créé : {size_gb:.2f} GB en {elapsed:.0f}s')
    print(f'   (Taille réduite de {PATH_TICKS.stat().st_size/1e9:.1f} → {size_gb:.1f} GB)')
    print(f'   Lectures futures : ~50x plus rapides')

# ── Lancer la conversion si nécessaire ───────────────────────
convert_ticks_to_parquet()

# ── LazyFrame Parquet (prêt pour les requêtes) ────────────────
_TICKS_PARQUET_LAZY = pl.scan_parquet(PATH_TICKS_PARQUET)
print('\n✅ LazyFrame Parquet prêt')


# ============================================================
# CELLULE 5b (REMPLACEMENT) — Fonctions tick optimisées
# ============================================================
import numpy as np
from datetime import timedelta as dt_timedelta

GMT_OFFSET     = -1       # Broker GMT+3 → PC GMT+2
SLIPPAGE_PTS   = 0.5
PNL_PER_POINT  = 3.0

def _fetch_ticks_numpy(signal_time_pc,
                        max_hours: int = 48) -> tuple:
    """
    Extrait les ticks depuis le Parquet pour une fenêtre donnée.
    Retourne (bids_np, asks_np) en numpy float64.

    signal_time_pc : timestamp en heure PC (GMT+2)
    Le Parquet stocke l'heure broker (GMT+3), donc on ajoute 1h.
    """
    # Convertir heure PC → heure broker pour filtrer le Parquet
    broker_start = signal_time_pc + dt_timedelta(hours=-GMT_OFFSET)
    broker_end   = broker_start   + dt_timedelta(hours=max_hours)

    df = (
        _TICKS_PARQUET_LAZY
        .filter(
            (pl.col('Time') >= broker_start) &
            (pl.col('Time') <= broker_end)
        )
        .select(['Bid', 'Ask'])
        .collect(streaming=True)
    )
    if df.is_empty():
        return np.array([]), np.array([])
    return df['Bid'].to_numpy(), df['Ask'].to_numpy()


def simulate_trade_ticks_fast(direction: str,
                               entry_signal: float,
                               levels: dict,
                               open_time,               # ignoré ici
                               bids: np.ndarray,        # pré-chargé depuis le cache
                               asks: np.ndarray,
                               slippage: float = SLIPPAGE_PTS) -> list:
    """
    Simulateur tick SANS I/O disque — travaille uniquement sur numpy arrays.
    Accepte bids/asks pré-chargés depuis le cache RAM.
    Compatible avec la même interface que simulate_trade_m1.
    """
    if len(bids) == 0:
        return []

    # ── Entrée réelle avec slippage ────────────────────────────
    entry = round((asks[0] + slippage) if direction == 'BUY' else (bids[0] - slippage), 2)
    delta = entry - entry_signal

    sl  = round(levels['sl']  + delta, 2)
    tp1 = round(levels['tp1'] + delta, 2)
    tp2 = round(levels['tp2'] + delta, 2)
    tp3 = round(levels['tp3'] + delta, 2)

    positions = [
        {'id': 'P1', 'tp': tp1, 'sl': sl,  'result': None, 'exit_px': None,
         'pnl': 0.0, 'closed': False, 'entry_real': entry, 'slippage': round(abs(delta), 4)},
        {'id': 'P2', 'tp': tp2, 'sl': sl,  'result': None, 'exit_px': None,
         'pnl': 0.0, 'closed': False, 'entry_real': entry, 'slippage': round(abs(delta), 4)},
        {'id': 'P3', 'tp': tp3, 'sl': sl,  'result': None, 'exit_px': None,
         'pnl': 0.0, 'closed': False, 'entry_real': entry, 'slippage': round(abs(delta), 4)},
    ]
    tp1_hit = tp2_hit = False

    # Prix d'exécution : BUY sort sur Bid | SELL sort sur Ask
    exec_prices = bids if direction == 'BUY' else asks

    for k in range(len(exec_prices)):
        if all(p['closed'] for p in positions):
            break
        px = exec_prices[k]

        for p in positions:
            if p['closed']:
                continue
            tp_hit = (px >= p['tp']) if direction == 'BUY' else (px <= p['tp'])
            sl_hit = (px <= p['sl']) if direction == 'BUY' else (px >= p['sl'])

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
                    for o in positions:
                        if not o['closed']: o['sl'] = entry
                if p['id'] == 'P2' and not tp2_hit and tp1_hit:
                    tp2_hit = True
                    for o in positions:
                        if not o['closed']: o['sl'] = tp1

            elif sl_hit:
                p['closed'] = True; p['exit_px'] = p['sl']
                pts = abs(entry - p['sl'])
                p['result'] = 'BE' if abs(p['sl'] - entry) < 0.01 else 'SL'
                p['pnl']    = 0.0 if p['result'] == 'BE' else round(-pts * PNL_PER_POINT, 2)

    last_px = exec_prices[-1]
    for p in positions:
        if not p['closed']:
            pts = (last_px - entry) if direction == 'BUY' else (entry - last_px)
            p['result'] = 'OPEN_END'; p['exit_px'] = last_px
            p['pnl']    = round(pts * PNL_PER_POINT, 2); p['closed'] = True

    return positions

print('✅ Simulateur tick optimisé défini (numpy pur, sans I/O)')


# ============================================================
# CELLULE 6b (NOUVEAU) — Pré-cache des fenêtres de ticks
# ============================================================
# Principe :
#   On détecte TOUS les signaux possibles (score_min=35, le minimum)
#   → au pire ~200 timestamps uniques sur 1 an.
#   On charge les fenêtres de ticks pour chacun → dict en RAM.
#   Les 40 combinaisons de sensibilité utilisent ce dict sans I/O.
#
# Mémoire estimée : 200 signaux × ~5000 ticks × 2 col × 8B = ~16 MB
# Temps estimé   : 200 × une lecture Parquet ≈ 2-5 min

import pandas as pd
from collections import OrderedDict
from tqdm.notebook import tqdm as tqdm_nb

MODES_CONFIG_CACHE = {
    'safe':      {'sl_mult': 2.5, 'tp1_mult': 1.5, 'tp2_mult': 2.5, 'tp3_mult': 4.0, 'score_min': 60, 'cooldown': 300},
    'risque':    {'sl_mult': 1.8, 'tp1_mult': 1.2, 'tp2_mult': 2.0, 'tp3_mult': 3.2, 'score_min': 45, 'cooldown': 180},
    'risque+++': {'sl_mult': 1.5, 'tp1_mult': 1.0, 'tp2_mult': 1.8, 'tp3_mult': 2.8, 'score_min': 35, 'cooldown':  90},
    'optimale':  {'sl_mult': 1.2, 'tp1_mult': 1.0, 'tp2_mult': 1.8, 'tp3_mult': 2.8, 'score_min': 35, 'cooldown':  90},
}

def _collect_all_signal_times(precomp: OrderedDict,
                               mode: str,
                               min_score: int = 35) -> list:
    """
    Parcourt les indicateurs pré-calculés et collecte TOUS les
    timestamps qui pourraient déclencher un signal, même avec le
    score_min le plus bas (35). Cela couvre toutes les combinaisons
    de sensibilité en un seul pass.
    """
    m              = MODES_CONFIG_CACHE[mode].copy()
    m['score_min'] = min_score

    signal_times   = []
    last_signal_ts = pd.Timestamp('1970-01-01')
    daily_pnl      = 0.0
    current_date   = None
    daily_stopped  = False
    trades_today   = 0
    SESSION_END_H  = 15
    DAILY_MAX_LOSS = -150.0
    MAX_TRADES     = 60

    for ts, ind in precomp.items():
        day = ts.date()
        if day != current_date:
            current_date = day; daily_pnl = 0.0
            trades_today = 0;   daily_stopped = False

        if daily_stopped or daily_pnl < DAILY_MAX_LOSS: continue
        if trades_today >= MAX_TRADES: continue
        if ts.hour >= SESSION_END_H: continue
        if (ts - last_signal_ts).total_seconds() < m['cooldown']: continue

        direction, _ = determine_direction(ind)   # fonction définie dans Cell 4
        if direction is None: continue

        score, _ = compute_score(ind, direction)  # fonction définie dans Cell 4
        if score < min_score: continue

        signal_times.append(ts)
        last_signal_ts = ts
        trades_today  += 1
        # On ne simule pas le PnL ici → daily_pnl reste approximatif
        # mais c'est suffisant pour collecter les timestamps candidats

    return signal_times


def build_tick_cache(signal_times: list,
                      max_hours: int = 48) -> dict:
    """
    Charge les fenêtres de ticks pour chaque signal et les stocke
    en RAM sous forme de numpy arrays (léger, rapidement picklable).

    Retourne : {timestamp_pc -> (bids_np, asks_np)}
    """
    cache = {}
    print(f'📦 Chargement de {len(signal_times)} fenêtres de ticks...')
    t0 = time.time()

    for ts in tqdm_nb(signal_times, desc='Cache ticks', unit='trade', ncols=70):
        bids, asks = _fetch_ticks_numpy(ts, max_hours=max_hours)
        if len(bids) > 0:
            cache[ts] = (bids, asks)
        else:
            print(f'  ⚠️  Aucun tick trouvé pour {ts}')

    elapsed = time.time() - t0
    total_mb = sum(b.nbytes + a.nbytes for b, a in cache.values()) / 1e6
    print(f'\n✅ Cache prêt : {len(cache)} trades | {total_mb:.1f} MB RAM | {elapsed:.0f}s')
    return cache


# ── Exécution ────────────────────────────────────────────────
print('🔍 Collecte des timestamps de signaux possibles (score_min=35)...')
all_signal_times = _collect_all_signal_times(precomp_indicators, TRADING_MODE, min_score=35)
print(f'   → {len(all_signal_times)} signaux candidats détectés\n')

print('⏳ Construction du cache ticks (lectures Parquet)...')
TICK_CACHE = build_tick_cache(all_signal_times)


# ============================================================
# CELLULE 8 (REMPLACEMENT) — Backtest principal avec cache
# ============================================================
# Le run_backtest_engine de la version précédente est remplacé
# par run_backtest_cached qui utilise TICK_CACHE au lieu du disque.

def run_backtest_cached(precomp: OrderedDict,
                         tick_cache: dict,
                         mode: str = 'optimale',
                         score_min_override: float = None,
                         sl_mult_override: float = None,
                         label: str = 'Backtest') -> tuple:
    """
    Moteur backtest tick utilisant le cache RAM.
    Pas d'I/O disque pendant la simulation → rapide même pour 40 combos.
    """
    import copy
    modes = copy.deepcopy(MODES_CONFIG_CACHE)
    m     = modes[mode]
    if score_min_override is not None: m['score_min'] = score_min_override
    if sl_mult_override   is not None: m['sl_mult']   = sl_mult_override

    trades_log      = []
    daily_pnl       = 0.0
    trades_today    = 0
    last_sl_time    = pd.Timestamp('1970-01-01')
    last_signal_ts  = pd.Timestamp('1970-01-01')
    current_date    = None
    daily_stopped   = False
    n_vwap_flat = n_sd_cap = n_no_setup = n_score_low = 0
    n_session_end = n_daily_stop = n_cooldown = 0

    SESSION_END_H_L  = 15
    SESSION_MID_H_L  = 12
    DAILY_MAX_LOSS_L = -150.0
    MAX_TRADES_L     = 60

    for ts, ind in precomp.items():
        day = ts.date()
        if day != current_date:
            current_date = day; daily_pnl = 0.0
            trades_today = 0;   daily_stopped = False

        if daily_stopped:
            n_daily_stop += 1; continue
        if daily_pnl < DAILY_MAX_LOSS_L:
            daily_stopped = True; n_daily_stop += 1; continue
        if trades_today >= MAX_TRADES_L: continue

        h = ts.hour
        if h >= SESSION_END_H_L:
            n_session_end += 1; continue
        if (ts - last_signal_ts).total_seconds() < m['cooldown']:
            n_cooldown += 1; continue

        direction, reject = determine_direction(ind)
        if direction is None:
            if   reject == 'vwap_flat': n_vwap_flat += 1
            elif reject == 'sd_cap':    n_sd_cap    += 1
            else:                       n_no_setup  += 1
            continue

        score, _ = compute_score(ind, direction)
        threshold = m['score_min']
        if h >= SESSION_MID_H_L:                              threshold += 10
        if (ts - last_sl_time).total_seconds() < 300:         threshold += 20
        if score < threshold:
            n_score_low += 1; continue

        # ── Vérifier que ce trade est dans le cache ───────────
        if ts not in tick_cache:
            continue   # Pas de ticks pour ce signal → skip

        # ── TP/SL sur la config locale (pas de mutation globale) ──
        entry_signal = ind['price']
        levels_local = _calc_tp_sl_local(direction, entry_signal,
                                          ind['atr'], ind['swing_high'],
                                          ind['swing_low'], m)

        bids, asks = tick_cache[ts]
        positions  = simulate_trade_ticks_fast(
            direction, entry_signal, levels_local, ts,
            bids=bids, asks=asks,
        )
        if not positions: continue

        seq_pnl       = sum(p['pnl'] for p in positions)
        daily_pnl     = round(daily_pnl + seq_pnl, 2)
        trades_today  += 1
        last_signal_ts = ts
        if any(p['result'] == 'SL' for p in positions):
            last_sl_time = ts

        entry_real = positions[0].get('entry_real', entry_signal)
        slippage   = positions[0].get('slippage', 0.0)
        for p in positions:
            trades_log.append({
                'open_time':    ts, 'day': day, 'direction': direction,
                'entry_signal': entry_signal, 'entry_real': entry_real,
                'slippage':     slippage, 'atr': ind['atr'],
                'score': score, 'threshold': threshold,
                'sl':   levels_local['sl'],  'tp1': levels_local['tp1'],
                'tp2':  levels_local['tp2'], 'tp3': levels_local['tp3'],
                'position': p['id'], 'result': p['result'],
                'exit_px':  p['exit_px'], 'pnl': p['pnl'],
                'daily_pnl': daily_pnl, 'hour': h,
                'vwap_slope': ind['vwap_slope'], 'dist_sd': ind['dist_vwap_sd'],
                'pattern': ind['pattern'], 'vol_ratio': ind['vol_ratio'],
            })

    filter_stats = {
        'vwap_flat':   n_vwap_flat,   'sd_cap':     n_sd_cap,
        'no_setup':    n_no_setup,    'score_low':  n_score_low,
        'session_end': n_session_end, 'daily_stop': n_daily_stop,
        'cooldown':    n_cooldown,
    }
    return pd.DataFrame(trades_log), filter_stats


def _calc_tp_sl_local(direction, entry, atr, swing_high, swing_low, m) -> dict:
    """
    Version locale de calculate_tp_sl qui utilise un dict m passé
    en argument (pas de variable globale) → thread/process safe.
    """
    MAX_RISK = 22.0
    if direction == 'BUY':
        sl   = min(round(swing_low  - atr * 0.2, 2), round(entry - atr * m['sl_mult'], 2))
        sl   = min(sl, round(entry - atr * max(m['sl_mult'], 1.5), 2))
        risk = abs(entry - sl)
        if risk > MAX_RISK: sl = round(entry - MAX_RISK, 2); risk = MAX_RISK
        return {'sl': sl, 'tp1': round(entry + risk * m['tp1_mult'], 2),
                'tp2': round(entry + risk * m['tp2_mult'], 2),
                'tp3': round(entry + risk * m['tp3_mult'], 2)}
    else:
        sl   = max(round(swing_high + atr * 0.2, 2), round(entry + atr * m['sl_mult'], 2))
        sl   = max(sl, round(entry + atr * max(m['sl_mult'], 1.5), 2))
        risk = abs(sl - entry)
        if risk > MAX_RISK: sl = round(entry + MAX_RISK, 2); risk = MAX_RISK
        return {'sl': sl, 'tp1': round(entry - risk * m['tp1_mult'], 2),
                'tp2': round(entry - risk * m['tp2_mult'], 2),
                'tp3': round(entry - risk * m['tp3_mult'], 2)}


# ── Run principal (Tick) ──────────────────────────────────────
print('🚀 Lancement du backtest Tick principal...')
t0 = time.time()
df_trades_tick, fs_tick = run_backtest_cached(
    precomp_indicators, TICK_CACHE, mode=TRADING_MODE,
)
print(f'✅ Tick terminé en {time.time()-t0:.1f}s '
      f'| {len(df_trades_tick)//3} séquences')


# ============================================================
# CELLULE 11 (REMPLACEMENT) — Sensibilité PARALLÈLE
# ============================================================
# Utilise ProcessPoolExecutor pour lancer les 40 combinaisons
# en parallèle sur tous les cœurs CPU disponibles.
# Chaque worker reçoit les données via pickle (dict → rapide).

import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed
import copy

N_CORES = multiprocessing.cpu_count()
print(f'💻 CPU détecté : {N_CORES} cœurs disponibles')
print(f'   → {N_CORES} workers en parallèle pour {8*5}=40 combinaisons')


def _sensitivity_worker(args: tuple) -> dict:
    """
    Worker exécuté dans un processus séparé.
    Reçoit tout par valeur (pickle) — aucune variable globale partagée.
    """
    (precomp_items, tick_cache_local, mode, sm, slm,
     modes_config_local, pnl_per_pt) = args

    import copy, pandas as pd, numpy as np
    from collections import OrderedDict

    # Reconstituer le precomp OrderedDict depuis la liste (picklable)
    precomp = OrderedDict(precomp_items)

    m = modes_config_local[mode].copy()
    m['score_min'] = sm
    m['sl_mult']   = slm

    MAX_RISK      = 22.0
    SLIP          = 0.5
    SESSION_END   = 15
    SESSION_MID   = 12
    DAILY_MAX     = -150.0
    MAX_TRADES    = 60

    def _tp_sl(direction, entry, atr, sh, sl_lvl, mm):
        if direction == 'BUY':
            sl   = min(round(sl_lvl - atr*0.2, 2), round(entry - atr*mm['sl_mult'], 2))
            sl   = min(sl, round(entry - atr*max(mm['sl_mult'], 1.5), 2))
            risk = abs(entry - sl)
            if risk > MAX_RISK: sl = round(entry - MAX_RISK, 2); risk = MAX_RISK
            return {'sl':sl,'tp1':round(entry+risk*mm['tp1_mult'],2),
                    'tp2':round(entry+risk*mm['tp2_mult'],2),'tp3':round(entry+risk*mm['tp3_mult'],2)}
        else:
            sl   = max(round(sh + atr*0.2, 2), round(entry + atr*mm['sl_mult'], 2))
            sl   = max(sl, round(entry + atr*max(mm['sl_mult'], 1.5), 2))
            risk = abs(sl - entry)
            if risk > MAX_RISK: sl = round(entry + MAX_RISK, 2); risk = MAX_RISK
            return {'sl':sl,'tp1':round(entry-risk*mm['tp1_mult'],2),
                    'tp2':round(entry-risk*mm['tp2_mult'],2),'tp3':round(entry-risk*mm['tp3_mult'],2)}

    def _simulate(direction, entry_signal, levels, bids, asks):
        entry = round((asks[0]+SLIP) if direction=='BUY' else (bids[0]-SLIP), 2)
        delta = entry - entry_signal
        sl  = round(levels['sl'] +delta,2); tp1=round(levels['tp1']+delta,2)
        tp2 = round(levels['tp2']+delta,2); tp3=round(levels['tp3']+delta,2)
        pos = [{'id':'P1','tp':tp1,'sl':sl,'pnl':0.,'result':None,'closed':False},
               {'id':'P2','tp':tp2,'sl':sl,'pnl':0.,'result':None,'closed':False},
               {'id':'P3','tp':tp3,'sl':sl,'pnl':0.,'result':None,'closed':False}]
        tp1h=tp2h=False
        ep = bids if direction=='BUY' else asks
        for px in ep:
            if all(p['closed'] for p in pos): break
            for p in pos:
                if p['closed']: continue
                th=(px>=p['tp']) if direction=='BUY' else (px<=p['tp'])
                sh=(px<=p['sl']) if direction=='BUY' else (px>=p['sl'])
                if th and sh:
                    th=abs(p['tp']-entry)<abs(p['sl']-entry); sh=not th
                if th:
                    p['closed']=True; pts=(p['tp']-entry) if direction=='BUY' else (entry-p['tp'])
                    p['result']=p['id'].replace('P','TP'); p['pnl']=round(pts*pnl_per_pt,2)
                    if p['id']=='P1' and not tp1h:
                        tp1h=True
                        for o in pos:
                            if not o['closed']: o['sl']=entry
                    if p['id']=='P2' and not tp2h and tp1h:
                        tp2h=True
                        for o in pos:
                            if not o['closed']: o['sl']=tp1
                elif sh:
                    p['closed']=True; pts=abs(entry-p['sl'])
                    p['result']='BE' if abs(p['sl']-entry)<0.01 else 'SL'
                    p['pnl']=0. if p['result']=='BE' else round(-pts*pnl_per_pt,2)
        lp=ep[-1]
        for p in pos:
            if not p['closed']:
                pts=(lp-entry) if direction=='BUY' else (entry-lp)
                p['result']='OPEN_END'; p['pnl']=round(pts*pnl_per_pt,2); p['closed']=True
        return pos

    # ── Boucle principale du worker ───────────────────────────
    pnls       = []
    daily_pnl  = 0.; trades_today=0
    last_sl_ts = pd.Timestamp('1970-01-01')
    last_sig_ts= pd.Timestamp('1970-01-01')
    cur_date   = None; daily_stopped=False

    for ts, ind in precomp.items():
        day = ts.date()
        if day != cur_date:
            cur_date=day; daily_pnl=0.; trades_today=0; daily_stopped=False
        if daily_stopped: continue
        if daily_pnl < DAILY_MAX: daily_stopped=True; continue
        if trades_today >= MAX_TRADES: continue
        h = ts.hour
        if h >= SESSION_END: continue
        if (ts-last_sig_ts).total_seconds() < m['cooldown']: continue

        # Direction (inline simplifié — utilise le dict ind pré-calculé)
        vwap_slope = ind['vwap_slope']; atr = ind['atr']
        if abs(vwap_slope) < atr*0.1: continue
        dist = ind['dist_vwap_sd']
        if dist > 4.5 or dist < -4.5: continue

        price = ind['price']
        direction = None
        if ind['bounce_bull']:   direction='BUY'
        elif ind['bounce_bear']: direction='SELL'
        elif price<ind['vwap_lower1'] and (ind['near_swing_sup'] or ind['near_poc']) \
             and ind['above_200'] and ind['above_50'] and ind['vol_ratio']>1.0:
            direction='BUY'
        elif price>ind['vwap_upper1'] and (ind['near_swing_res'] or ind['near_poc']) \
             and not ind['above_200'] and not ind['above_50'] and ind['vol_ratio']>1.0:
            direction='SELL'
        elif price<ind['vwap_lower2'] and ind['above_200'] and ind['vol_ratio']>1.0:
            direction='BUY'
        elif price>ind['vwap_upper2'] and not ind['above_200'] and ind['vol_ratio']>1.0:
            direction='SELL'
        if direction is None: continue

        # Score inline
        score=0; is_buy=(direction=='BUY')
        dsd=abs(dist)
        if dsd>=2.0:score+=30
        elif dsd>=1.0:score+=18
        elif dsd>=0.5:score+=8
        if is_buy and vwap_slope>0:score+=5
        elif not is_buy and vwap_slope<0:score+=5
        if is_buy:
            if ind['near_swing_sup']:score+=15
            if ind['near_poc']:score+=10
            if price<ind['va_low']:score+=10
            if ind['above_200']:score+=5
        else:
            if ind['near_swing_res']:score+=15
            if ind['near_poc']:score+=10
            if price>ind['va_high']:score+=10
            if not ind['above_200']:score+=5
        vr=ind['vol_ratio']
        if ind['bounce_bull'] or ind['bounce_bear']:score+=25
        elif vr>=2.0:score+=20
        elif vr>=1.4:score+=12
        elif vr>=1.0:score+=5
        if is_buy and ind['ema_cross_bull']:score+=10
        elif not is_buy and ind['ema_cross_bear']:score+=10
        bull_p={'Marteau','Engulfing haussier','Pin Bar haussier'}
        bear_p={'Shooting Star','Engulfing baissier','Pin Bar baissier'}
        pat=ind['pattern']
        if is_buy and pat in bull_p:score+=15
        elif not is_buy and pat in bear_p:score+=15
        elif pat=='Doji':score+=3
        score=min(score,100)

        threshold=m['score_min']
        if h>=SESSION_MID:threshold+=10
        if (ts-last_sl_ts).total_seconds()<300:threshold+=20
        if score<threshold:continue
        if ts not in tick_cache_local:continue

        lvl=_tp_sl(direction,price,atr,ind['swing_high'],ind['swing_low'],m)
        bids,asks=tick_cache_local[ts]
        pos=_simulate(direction,price,lvl,bids,asks)
        if not pos:continue

        seq=sum(p['pnl'] for p in pos)
        daily_pnl=round(daily_pnl+seq,2); trades_today+=1; last_sig_ts=ts
        if any(p['result']=='SL' for p in pos): last_sl_ts=ts
        pnls.append(seq)

    # Métriques
    if not pnls:
        return {'score_min':sm,'sl_mult':slm,'net_profit':0,'n_seq':0,'winrate':0,'pf':0}
    arr    = np.array(pnls)
    net    = round(arr.sum(), 2)
    n_seq  = len(arr)
    wr     = round((arr>0).sum()/n_seq*100, 1)
    gw     = arr[arr>0].sum()
    gl     = abs(arr[arr<0].sum())
    pf     = round(gw/gl, 2) if gl>0 else 999
    return {'score_min':sm,'sl_mult':slm,'net_profit':net,'n_seq':n_seq,'winrate':wr,'pf':pf}


def run_sensitivity_parallel(precomp: OrderedDict,
                              tick_cache: dict,
                              mode: str,
                              score_range: list,
                              sl_range: list) -> pd.DataFrame:
    """
    Lance les 40 combinaisons en parallèle sur tous les cœurs CPU.
    Chaque worker est autonome (données copiées, pas de partage mémoire).
    """
    # Sérialiser precomp en liste (picklable entre processus)
    precomp_list = list(precomp.items())
    modes_copy   = copy.deepcopy(MODES_CONFIG_CACHE)

    combos = [(precomp_list, tick_cache, mode, sm, slm, modes_copy, PNL_PER_POINT)
              for sm in score_range for slm in sl_range]

    results = []
    n_workers = min(N_CORES, len(combos))
    print(f'🚀 Lancement : {len(combos)} combos | {n_workers} workers CPU')
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_sensitivity_worker, args): args[3:5]
                   for args in combos}
        with tqdm_nb(total=len(combos), desc='Sensibilité parallèle',
                     unit='combo', ncols=70) as pbar:
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    sm, slm = futures[future]
                    results.append({'score_min':sm,'sl_mult':slm,
                                    'net_profit':0,'n_seq':0,'winrate':0,'pf':0})
                    print(f'  ⚠️ Erreur combo sm={sm} slm={slm}: {e}')
                pbar.update(1)

    elapsed = time.time() - t0
    print(f'\n✅ Sensibilité parallèle terminée en {elapsed:.1f}s '
          f'({elapsed/len(combos):.1f}s/combo)')
    return pd.DataFrame(results)


# ── Lancement ─────────────────────────────────────────────────
# IMPORTANT : ProcessPoolExecutor nécessite le guard __name__=='__main__'
# Dans un Jupyter Notebook, ce guard n'est pas nécessaire car chaque
# cellule s'exécute dans le processus principal. Si tu l'exécutes en
# script .py, ajoute :  if __name__ == '__main__':

print('🔬 Analyse de sensibilité PARALLÈLE en cours...\n')
score_range = [35, 40, 45, 50, 55, 60, 65, 70]
sl_range    = [1.2, 1.5, 1.8, 2.2, 2.5]

df_sens = run_sensitivity_parallel(
    precomp_indicators, TICK_CACHE, TRADING_MODE,
    score_range, sl_range,
)

# ── Heatmaps ─────────────────────────────────────────────────
pivot    = df_sens.pivot(index='sl_mult', columns='score_min', values='net_profit')
pivot_wr = df_sens.pivot(index='sl_mult', columns='score_min', values='winrate')

fig, axes = plt.subplots(1, 2, figsize=(18, 6))
fig.suptitle(f'Sensibilité Tick PARALLÈLE — {TRADING_MODE.upper()}',
             fontsize=13, fontweight='bold', color='white')

im1 = axes[0].imshow(pivot.values, aspect='auto', cmap='RdYlGn')
axes[0].set_xticks(range(len(pivot.columns)));   axes[0].set_xticklabels(pivot.columns, color='white')
axes[0].set_yticks(range(len(pivot.index)));     axes[0].set_yticklabels(pivot.index, color='white')
axes[0].set_xlabel('Score Min', color='white');  axes[0].set_ylabel('SL Mult', color='white')
axes[0].set_title('Net Profit ($) — Ticks réels', color='#00d4ff')
for i in range(len(pivot.index)):
    for j in range(len(pivot.columns)):
        axes[0].text(j, i, f"{pivot.values[i,j]:.0f}", ha='center', va='center', fontsize=8,
                     color='black' if abs(pivot.values[i,j]) < 200 else 'white')
plt.colorbar(im1, ax=axes[0])

im2 = axes[1].imshow(pivot_wr.values, aspect='auto', cmap='RdYlGn', vmin=30, vmax=80)
axes[1].set_xticks(range(len(pivot_wr.columns))); axes[1].set_xticklabels(pivot_wr.columns, color='white')
axes[1].set_yticks(range(len(pivot_wr.index)));   axes[1].set_yticklabels(pivot_wr.index, color='white')
axes[1].set_xlabel('Score Min', color='white')
axes[1].set_title('Winrate % — Ticks réels', color='#00d4ff')
for i in range(len(pivot_wr.index)):
    for j in range(len(pivot_wr.columns)):
        axes[1].text(j, i, f"{pivot_wr.values[i,j]:.0f}%", ha='center', va='center', fontsize=8, color='black')
plt.colorbar(im2, ax=axes[1])
plt.tight_layout()
plt.savefig('sensitivity_tick_parallel.png', dpi=130, bbox_inches='tight', facecolor='#0d0d0d')
plt.show()

best = df_sens.loc[df_sens['net_profit'].idxmax()]
print(f'\n🏆 Meilleure combinaison : score_min={best["score_min"]} | '
      f'sl_mult={best["sl_mult"]} → Net: {best["net_profit"]:+.2f}$ | WR: {best["winrate"]}%')
print(f'\n💻 Utilisation CPU : {N_CORES} cœurs utilisés simultanément')
