"""
╔══════════════════════════════════════════════╗
║     BACKTEST — GOLD SCALPING BOT XAUUSD     ║
║     Simule les trades sur données réelles    ║
╚══════════════════════════════════════════════╝

USAGE :
    python backtest.py data.csv

FORMAT CSV ATTENDU (export MT5 ou TradingView) :
    Date,Open,High,Low,Close,Volume
"""

import sys
import pandas as pd
import numpy as np

# On réutilise les fonctions du bot
sys.path.insert(0, ".")
from history.bot import compute_indicators, determine_direction, compute_score, calculate_tp_sl


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().capitalize() for c in df.columns]
    rename = {
        "Date": "Date", "Datetime": "Date", "Time": "Date",
        "Open": "Open", "High": "High", "Low": "Low",
        "Close": "Close", "Volume": "Volume", "Tick volume": "Volume"
    }
    df.rename(columns=rename, inplace=True)

    required = ["Open", "High", "Low", "Close"]
    for col in required:
        if col not in df.columns:
            print(f"❌ Colonne manquante : {col}")
            sys.exit(1)

    if "Volume" not in df.columns:
        df["Volume"] = 1000.0

    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    return df.reset_index(drop=True)


def run_backtest(df: pd.DataFrame, score_threshold: int = 35,
                 lot: float = 0.01, max_trades: int = 999) -> list:
    """
    Simule les trades en mode scalping sur les données historiques.

    Logique :
    - Pour chaque bougie, calcule les indicateurs sur les 100 bougies précédentes
    - determine_direction() cherche un signal scalping
    - compute_score() évalue la qualité
    - Si score ≥ seuil → simule le trade sur les bougies suivantes
    - PnL calculé avec les niveaux TP/SL basés sur l'ATR
    """
    trades      = []
    trade_count = 0
    i_min       = 50   # minimum de bougies pour les indicateurs (stoch + EMA200)

    i = i_min
    while i < len(df) - 1:
        if trade_count >= max_trades:
            break

        window = df.iloc[max(0, i - 100): i + 1].copy().reset_index(drop=True)

        try:
            ind       = compute_indicators(window)
            direction = determine_direction(ind)
        except Exception as e:
            i += 1
            continue

        if direction is None:
            i += 1
            continue

        score, reasons = compute_score(ind, direction)
        if score < score_threshold:
            i += 1
            continue

        # Signal détecté — simuler le trade
        entry  = ind["price"]
        levels = calculate_tp_sl(direction, entry, ind["atr"])
        sl     = levels["sl"]
        tp1    = levels["tp1"]
        tp2    = levels["tp2"]
        tp3    = levels["tp3"]

        result     = "EN COURS"
        exit_price = None
        exit_bar   = None

        # Simuler les bougies suivantes (max 50 bougies = ~4h en M5)
        for j in range(i + 1, min(i + 50, len(df))):
            high_j = df["High"].iloc[j]
            low_j  = df["Low"].iloc[j]

            if direction == "BUY":
                if low_j <= sl:
                    result = "SL";  exit_price = sl;  exit_bar = j; break
                if high_j >= tp3:
                    result = "TP3"; exit_price = tp3; exit_bar = j; break
                if high_j >= tp2:
                    result = "TP2"; exit_price = tp2; exit_bar = j; break
                if high_j >= tp1:
                    result = "TP1"; exit_price = tp1; exit_bar = j; break
            else:  # SELL
                if high_j >= sl:
                    result = "SL";  exit_price = sl;  exit_bar = j; break
                if low_j <= tp3:
                    result = "TP3"; exit_price = tp3; exit_bar = j; break
                if low_j <= tp2:
                    result = "TP2"; exit_price = tp2; exit_bar = j; break
                if low_j <= tp1:
                    result = "TP1"; exit_price = tp1; exit_bar = j; break

        if result == "EN COURS":
            exit_price = df["Close"].iloc[min(i + 49, len(df) - 1)]
            exit_bar   = min(i + 49, len(df) - 1)

        # PnL (XAUUSD : 1$ de mouvement = 1$ pour 0.01 lot)
        if direction == "BUY":
            pnl = (exit_price - entry) * lot * 100
        else:
            pnl = (entry - exit_price) * lot * 100

        trades.append({
            "bar":        i,
            "direction":  direction,
            "score":      score,
            "entry":      entry,
            "sl":         sl,
            "tp1":        tp1,
            "tp2":        tp2,
            "tp3":        tp3,
            "exit_price": round(exit_price, 2),
            "result":     result,
            "pnl":        round(pnl, 2),
            "reasons":    ", ".join(reasons[:3]),
        })

        trade_count += 1
        i = (exit_bar or i) + 1

    return trades


def print_report(trades: list, lot: float):
    if not trades:
        print("Aucun trade généré. Essaie d'abaisser le score_threshold.")
        return

    df         = pd.DataFrame(trades)
    total      = len(df)
    wins       = len(df[df["pnl"] > 0])
    losses     = len(df[df["pnl"] <= 0])
    winrate    = round(wins / total * 100, 1)
    total_pnl  = round(df["pnl"].sum(), 2)
    avg_win    = round(df[df["pnl"] > 0]["pnl"].mean(), 2) if wins else 0
    avg_loss   = round(df[df["pnl"] <= 0]["pnl"].mean(), 2) if losses else 0
    best_trade = round(df["pnl"].max(), 2)
    worst_trade= round(df["pnl"].min(), 2)
    tp_counts  = df["result"].value_counts().to_dict()

    # Profit factor = sum des gains / sum des pertes absolues
    sum_gains  = df[df["pnl"] > 0]["pnl"].sum()
    sum_losses = abs(df[df["pnl"] < 0]["pnl"].sum())
    pf         = round(sum_gains / sum_losses, 2) if sum_losses > 0 else float("inf")

    print("\n" + "═" * 55)
    print("   ⚡ RAPPORT BACKTEST — GOLD SCALPING BOT XAUUSD")
    print("═" * 55)
    print(f"  Lot size           : {lot}")
    print(f"  Total trades       : {total}")
    print(f"  ✅ Gagnants        : {wins}  ({winrate}%)")
    print(f"  ❌ Perdants        : {losses}")
    print(f"  💰 PnL total       : {'+' if total_pnl >= 0 else ''}{total_pnl}$")
    print(f"  📊 Profit Factor   : {pf}")
    print(f"  📈 Gain moyen      : +{avg_win}$")
    print(f"  📉 Perte moyenne   : {avg_loss}$")
    print(f"  🏆 Meilleur trade  : +{best_trade}$")
    print(f"  💀 Pire trade      : {worst_trade}$")
    print("─" * 55)
    print("  Résultats :")
    for k, v in sorted(tp_counts.items()):
        print(f"    {k:8s} : {v} trades")
    print("═" * 55)

    print("\n  📋 DÉTAIL DES TRADES :")
    print(f"  {'#':>3} {'Dir':>4} {'Score':>5} {'Entry':>8} {'Exit':>8} {'Result':>6} {'PnL':>7}  Raisons")
    print("  " + "─" * 90)
    for i, t in enumerate(trades, 1):
        sign = "+" if t["pnl"] >= 0 else ""
        print(f"  {i:>3} {t['direction']:>4} {t['score']:>5}  {t['entry']:>8} {t['exit_price']:>8} "
              f"{t['result']:>6} {sign}{t['pnl']:>6}$  {t['reasons'][:45]}")
    print()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage : python backtest.py <fichier.csv>")
        print("\nTest avec données simulées (500 bougies M5)...")
        np.random.seed(42)
        n     = 500
        price = 2650.0
        rows  = []
        for _ in range(n):
            o = price
            c = o + np.random.randn() * 2
            h = max(o, c) + abs(np.random.randn())
            l = min(o, c) - abs(np.random.randn())
            v = np.random.randint(500, 3000)
            rows.append({"Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
            price = c
        df = pd.DataFrame(rows)
    else:
        print(f"📂 Chargement : {sys.argv[1]}")
        df = load_csv(sys.argv[1])

    print(f"📊 {len(df)} bougies chargées")
    trades = run_backtest(df, score_threshold=35, lot=0.01)
    print_report(trades, lot=0.01)
