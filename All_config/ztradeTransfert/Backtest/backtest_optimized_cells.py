# ================================================================
# OPTIMISATION ANALYSE DE SENSIBILITÉ — Gold Bot v2
# ================================================================
# Remplace CELLULE 5, ajoute CELLULE 6b, remplace CELLULE 10
#
# Gains attendus :
#   simulate_trade_m1  : iterrows → numpy + searchsorted  (~5-8x)
#   Sensibilité        : pré-calcul indicateurs 1× au lieu de 40×  (~38x)
#   Total estimé       : 71 min → ~2-4 min
# ================================================================


# ============================================================
# CELLULE 5 (REMPLACEMENT) — Simulateur M1 optimisé
# ============================================================
# Changements vs version originale :
#   1. df_m1.index.searchsorted()  au lieu du masque booléen O(n)
#   2. bars['High'].values / bars['Low'].values  au lieu d'iterrows
#   Logique breakeven et priorité TP/SL : identique à l'original

def simulate_trade_m1(direction: str, entry: float, levels: dict,
                       open_time: pd.Timestamp, df_m1: pd.DataFrame) -> list:
    """
    Simule le parcours de prix en M1 pour un trade ouvert à `open_time`.
    Version optimisée : searchsorted + tableaux numpy (pas d'iterrows).
    Logique métier identique à la version originale.
    """
    sl_orig = levels['sl']
    tp1, tp2, tp3 = levels['tp1'], levels['tp2'], levels['tp3']

    # ── [OPT] Slicing rapide via searchsorted (O(log n) vs O(n)) ──
    start_pos = df_m1.index.searchsorted(open_time)
    bars = df_m1.iloc[start_pos:]

    if bars.empty:
        return []

    positions = [
        {'id': 'P1', 'tp': tp1, 'sl': sl_orig,
         'result': None, 'exit_px': None, 'closed': False},
        {'id': 'P2', 'tp': tp2, 'sl': sl_orig,
         'result': None, 'exit_px': None, 'closed': False},
        {'id': 'P3', 'tp': tp3, 'sl': sl_orig,
         'result': None, 'exit_px': None, 'closed': False},
    ]
    tp1_hit = False
    tp2_hit = False

    # ── [OPT] Extraire les tableaux numpy une seule fois ──────────
    highs  = bars['High'].values
    lows   = bars['Low'].values
    closes = bars['Close'].values

    for k in range(len(highs)):
        if all(p['closed'] for p in positions):
            break

        bar_high = highs[k]
        bar_low  = lows[k]

        for p in positions:
            if p['closed']:
                continue

            sl_cur = p['sl']
            tp_cur = p['tp']

            if direction == 'BUY':
                tp_hit = bar_high >= tp_cur
                sl_hit = bar_low  <= sl_cur
            else:
                tp_hit = bar_low  <= tp_cur
                sl_hit = bar_high >= sl_cur

            # Priorité : si les deux touchés, le plus proche de l'entrée d'abord
            if tp_hit and sl_hit:
                dist_tp = abs(tp_cur - entry)
                dist_sl = abs(sl_cur - entry)
                tp_hit  = dist_tp < dist_sl
                sl_hit  = not tp_hit

            if tp_hit:
                p['closed']  = True
                p['exit_px'] = tp_cur
                pts = (tp_cur - entry) if direction == 'BUY' else (entry - tp_cur)
                p['result'] = p['id'].replace('P', 'TP')
                p['pnl']    = round(pts * PNL_PER_POINT, 2)

                # Breakeven : TP1 touché → SL de P2 et P3 → entry
                if p['id'] == 'P1' and not tp1_hit:
                    tp1_hit = True
                    for other in positions:
                        if not other['closed']:
                            other['sl'] = entry          # [FIX BREAKEVEN]

                # SL de P3 → TP1 après TP2
                if p['id'] == 'P2' and not tp2_hit and tp1_hit:
                    tp2_hit = True
                    for other in positions:
                        if not other['closed']:
                            other['sl'] = tp1

            elif sl_hit:
                p['closed']  = True
                p['exit_px'] = p['sl']
                pts = (entry - p['sl']) if direction == 'BUY' else (p['sl'] - entry)
                if abs(p['sl'] - entry) < 0.01:
                    p['result'] = 'BE'
                    p['pnl']    = 0.0
                else:
                    p['result'] = 'SL'
                    p['pnl']    = round(-pts * PNL_PER_POINT, 2)

    # Positions encore ouvertes → fermées au dernier prix
    last_close = closes[-1]
    for p in positions:
        if not p['closed']:
            pts = (last_close - entry) if direction == 'BUY' else (entry - last_close)
            p['result']  = 'OPEN_END'
            p['exit_px'] = last_close
            p['pnl']     = round(pts * PNL_PER_POINT, 2)
            p['closed']  = True

    return positions

print('✅ simulate_trade_m1 optimisé (searchsorted + numpy)')


# ============================================================
# CELLULE 6b — Pré-calcul des indicateurs (à insérer après Cell 6)
# ============================================================
# Principe : compute_indicators() est coûteux (rolling pandas sur 100
# bougies). Pour 40 combinaisons de paramètres, on évite de le recalculer
# 40× en le calculant UNE SEULE FOIS ici et en réutilisant le résultat.

import time
from collections import OrderedDict

def precompute_all_indicators(df_m5: pd.DataFrame, window: int = 100) -> OrderedDict:
    """
    Pré-calcule et stocke les indicateurs pour chaque bougie M5.
    Appel unique (~même durée qu'un run_backtest normal).
    Résultat réutilisé par run_backtest_fast() sans recomputation.
    """
    t0 = time.time()
    candle_times = df_m5.index.tolist()
    precomp = OrderedDict()
    total = len(df_m5)

    for i in range(window, total):
        ts = candle_times[i]
        window_df = df_m5.iloc[i - window: i + 1]
        try:
            precomp[ts] = compute_indicators(window_df)
        except Exception:
            pass  # Bougie ignorée (données manquantes)

        # Affichage de progression toutes les 5000 itérations
        if (i - window) % 5000 == 0:
            pct     = (i - window) / (total - window) * 100
            elapsed = time.time() - t0
            eta     = (elapsed / max(i - window, 1)) * (total - window - (i - window))
            print(f'  {pct:5.1f}% — {i - window:,}/{total - window:,} bougies '
                  f'| Écoulé : {elapsed:.0f}s | ETA : {eta:.0f}s')

    print(f'\n✅ Pré-calcul terminé : {len(precomp):,} timestamps en {time.time()-t0:.1f}s')
    return precomp


def run_backtest_fast(precomp: OrderedDict,
                      df_m1: pd.DataFrame,
                      mode: str = TRADING_MODE,
                      score_min_override: float = None,
                      sl_mult_override: float = None) -> tuple:
    """
    Moteur de backtest optimisé pour la sensibilité.
    Réutilise les indicateurs pré-calculés (precomp) au lieu de les
    recalculer — logique de trading IDENTIQUE à run_backtest().

    Correction : sl_mult_override modifie temporairement MODES_CONFIG
    pour que calculate_tp_sl() l'applique réellement (le code original
    recalculait les levels sans appliquer l'override).
    """
    m = MODES_CONFIG[mode].copy()
    if score_min_override is not None:
        m['score_min'] = score_min_override

    # Appliquer sl_mult temporairement dans MODES_CONFIG
    # (calculate_tp_sl lit directement MODES_CONFIG[mode])
    original_sl_mult = MODES_CONFIG[mode]['sl_mult']
    if sl_mult_override is not None:
        MODES_CONFIG[mode]['sl_mult'] = sl_mult_override

    trades_log      = []
    signals_ignored = []

    daily_pnl      = 0.0
    trades_today   = 0
    last_sl_time   = pd.Timestamp('1970-01-01')
    last_signal_ts = pd.Timestamp('1970-01-01')
    current_date   = None
    daily_stopped  = False

    n_vwap_flat = n_sd_cap = n_no_setup = n_score_low = 0
    n_session_end = n_daily_stop = n_cooldown = 0

    try:
        for ts, ind in precomp.items():
            day = ts.date()

            # ── Reset quotidien ────────────────────────────────────
            if day != current_date:
                current_date  = day
                daily_pnl     = 0.0
                trades_today  = 0
                daily_stopped = False

            if daily_stopped:
                n_daily_stop += 1
                continue

            # ── [FIX DAILY STOP] Coupe-circuit ────────────────────
            if daily_pnl < DAILY_MAX_LOSS:
                daily_stopped = True
                n_daily_stop += 1
                continue

            if trades_today >= MAX_TRADES_PER_DAY:
                continue

            # ── [FIX CUTOFF] Filtre horaire ────────────────────────
            h = ts.hour
            if h >= SESSION_END_H:
                n_session_end += 1
                continue

            # ── Cooldown inter-signaux ─────────────────────────────
            if (ts - last_signal_ts).total_seconds() < m['cooldown']:
                n_cooldown += 1
                continue

            # ── Direction (utilise indicateurs pré-calculés) ───────
            direction, reject_reason = determine_direction(ind)
            if direction is None:
                if   reject_reason == 'vwap_flat': n_vwap_flat += 1
                elif reject_reason == 'sd_cap':    n_sd_cap    += 1
                else:                              n_no_setup  += 1
                signals_ignored.append({'ts': ts, 'reason': reject_reason})
                continue

            # ── Score & Threshold ──────────────────────────────────
            score, _ = compute_score(ind, direction)
            threshold = m['score_min']
            if h >= SESSION_MID_H:                                   # [FIX THRESHOLD +10]
                threshold += 10
            if (ts - last_sl_time).total_seconds() < 300:           # [FIX POST-SL +20]
                threshold += 20

            if score < threshold:
                n_score_low += 1
                signals_ignored.append({
                    'ts': ts, 'reason': f'score={score}<{threshold}',
                    'direction': direction, 'score': score, 'threshold': threshold,
                })
                continue

            # ── Calcul TP/SL (MODES_CONFIG déjà mis à jour si override) ──
            levels = calculate_tp_sl(
                direction, ind['price'], ind['atr'],
                ind['swing_high'], ind['swing_low'], mode=mode,
            )

            entry = ind['price']

            # ── Simulation M1 ──────────────────────────────────────
            positions = simulate_trade_m1(direction, entry, levels, ts, df_m1)
            if not positions:
                continue

            seq_pnl    = sum(p['pnl'] for p in positions)
            daily_pnl  = round(daily_pnl + seq_pnl, 2)
            trades_today  += 1
            last_signal_ts = ts

            if any(p['result'] == 'SL' for p in positions):
                last_sl_time = ts

            for p in positions:
                trades_log.append({
                    'open_time':  ts,
                    'day':        day,
                    'direction':  direction,
                    'entry':      entry,
                    'atr':        ind['atr'],
                    'score':      score,
                    'threshold':  threshold,
                    'sl':         levels['sl'],
                    'tp1':        levels['tp1'],
                    'tp2':        levels['tp2'],
                    'tp3':        levels['tp3'],
                    'position':   p['id'],
                    'result':     p['result'],
                    'exit_px':    p['exit_px'],
                    'pnl':        p['pnl'],
                    'daily_pnl':  daily_pnl,
                    'hour':       h,
                    'vwap_slope': ind['vwap_slope'],
                    'dist_sd':    ind['dist_vwap_sd'],
                    'pattern':    ind['pattern'],
                    'vol_ratio':  ind['vol_ratio'],
                })

    finally:
        # Restaurer sl_mult original dans MODES_CONFIG dans tous les cas
        if sl_mult_override is not None:
            MODES_CONFIG[mode]['sl_mult'] = original_sl_mult

    df_trades_out  = pd.DataFrame(trades_log)
    df_ignored_out = pd.DataFrame(signals_ignored)
    filter_stats = {
        'vwap_flat':   n_vwap_flat,
        'sd_cap':      n_sd_cap,
        'no_setup':    n_no_setup,
        'score_low':   n_score_low,
        'session_end': n_session_end,
        'daily_stop':  n_daily_stop,
        'cooldown':    n_cooldown,
    }
    return df_trades_out, df_ignored_out, filter_stats


# ── Lancer le pré-calcul (une seule fois, même durée qu'un run normal) ──
print('⏳ Pré-calcul des indicateurs en cours (à faire UNE SEULE FOIS)...')
precomp_indicators = precompute_all_indicators(df_m5)
print('✅ Indicateurs pré-calculés — prêt pour la sensibilité rapide.')


# ============================================================
# CELLULE 10 (REMPLACEMENT) — Analyse de Sensibilité RAPIDE
# ============================================================
# Utilise precomp_indicators + run_backtest_fast.
# Les 40 combinaisons ne recalculent plus les indicateurs.

print('🔬 Analyse de sensibilité rapide en cours...\n')
t_sens = time.time()

score_range = [35, 40, 45, 50, 55, 60, 65, 70]
sl_range    = [1.2, 1.5, 1.8, 2.2, 2.5]

results_matrix = []

for sm in score_range:
    for slm in sl_range:
        try:
            dt, _, fs = run_backtest_fast(
                precomp_indicators, df_m1,
                mode=TRADING_MODE,
                score_min_override=sm,
                sl_mult_override=slm,
            )
            if dt.empty:
                results_matrix.append({
                    'score_min': sm, 'sl_mult': slm,
                    'net_profit': 0, 'n_seq': 0, 'winrate': 0, 'pf': 0,
                })
                continue

            seq = dt.groupby('open_time')['pnl'].sum()
            net  = round(seq.sum(), 2)
            n_seq = len(seq)
            wr   = round((seq > 0).sum() / n_seq * 100, 1) if n_seq > 0 else 0
            gl   = abs(dt[dt['pnl'] < 0]['pnl'].sum())
            gw   = dt[dt['pnl'] > 0]['pnl'].sum()
            pf   = round(gw / gl, 2) if gl > 0 else 999

            results_matrix.append({
                'score_min': sm, 'sl_mult': slm,
                'net_profit': net, 'n_seq': n_seq, 'winrate': wr, 'pf': pf,
            })

        except Exception as e:
            results_matrix.append({
                'score_min': sm, 'sl_mult': slm,
                'net_profit': 0, 'n_seq': 0, 'winrate': 0, 'pf': 0,
            })

print(f'✅ Sensibilité terminée en {time.time()-t_sens:.1f}s '
      f'({len(score_range)*len(sl_range)} combinaisons)')

df_sens = pd.DataFrame(results_matrix)

# ── Heatmap Net Profit & Winrate (identique à l'original) ────
pivot    = df_sens.pivot(index='sl_mult', columns='score_min', values='net_profit')
pivot_wr = df_sens.pivot(index='sl_mult', columns='score_min', values='winrate')

fig, axes = plt.subplots(1, 2, figsize=(18, 6))
fig.suptitle('Analyse de Sensibilité — Net Profit & Winrate',
             fontsize=13, fontweight='bold', color='white')

im1 = axes[0].imshow(pivot.values, aspect='auto', cmap='RdYlGn')
axes[0].set_xticks(range(len(pivot.columns)))
axes[0].set_xticklabels(pivot.columns, color='white')
axes[0].set_yticks(range(len(pivot.index)))
axes[0].set_yticklabels(pivot.index, color='white')
axes[0].set_xlabel('Score Min', color='white')
axes[0].set_ylabel('SL Mult', color='white')
axes[0].set_title('Net Profit ($)', color='#00d4ff')
for i in range(len(pivot.index)):
    for j in range(len(pivot.columns)):
        axes[0].text(j, i, f"{pivot.values[i,j]:.0f}",
                     ha='center', va='center', fontsize=8,
                     color='black' if abs(pivot.values[i,j]) < 200 else 'white')
plt.colorbar(im1, ax=axes[0])

im2 = axes[1].imshow(pivot_wr.values, aspect='auto', cmap='RdYlGn', vmin=30, vmax=80)
axes[1].set_xticks(range(len(pivot_wr.columns)))
axes[1].set_xticklabels(pivot_wr.columns, color='white')
axes[1].set_yticks(range(len(pivot_wr.index)))
axes[1].set_yticklabels(pivot_wr.index, color='white')
axes[1].set_xlabel('Score Min', color='white')
axes[1].set_title('Winrate % (séquences)', color='#00d4ff')
for i in range(len(pivot_wr.index)):
    for j in range(len(pivot_wr.columns)):
        axes[1].text(j, i, f"{pivot_wr.values[i,j]:.0f}%",
                     ha='center', va='center', fontsize=8, color='black')
plt.colorbar(im2, ax=axes[1])

plt.tight_layout()
plt.savefig('sensitivity_analysis.png', dpi=130, bbox_inches='tight',
            facecolor='#0d0d0d', edgecolor='none')
plt.show()
print('✅ Analyse de sensibilité terminée → sensitivity_analysis.png')

# Meilleure combinaison
best = df_sens.loc[df_sens['net_profit'].idxmax()]
print(f'\n🏆 Meilleure combinaison : score_min={best["score_min"]} | '
      f'sl_mult={best["sl_mult"]} → Net: {best["net_profit"]:+.2f}$ | WR: {best["winrate"]}%')
