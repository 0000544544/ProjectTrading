import asyncio
import logging
import time
import gc
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timedelta
import numpy as np
import pandas as pd

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from config import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    LOT_SIZE, MAX_SPREAD, MAX_TRADES_PER_DAY,
    MAGIC_NUMBER,
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
SYMBOL              = "BTCUSD"
TIMEFRAME           = 16385          # H1 — mt5.TIMEFRAME_H1
CANDLES_NEEDED      = 300

BB_PERIOD           = 20
BB_DEV              = 2.5
KC_PERIOD           = 20
KC_MULT             = 2.0
ATR_PERIOD          = 14
VOL_SMA_PERIOD      = 20
EMA_TRAIL_PERIOD    = 20

VOL_BREAKOUT_FACTOR = 1.5
SL_ATR_MULT         = 2.0
TP1_RR              = 2.0

LOT_P1              = round(LOT_SIZE * 0.5, 2)
LOT_P2              = round(LOT_SIZE * 0.5, 2)
LOT_MIN             = 0.001
LOT_MAX             = 0.1

DAILY_MAX_LOSS      = -500.0
MAX_CONSECUTIVE_SL  = 3

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("btc_squeeze_h1.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mt5")

state = {
    "trades_today":     0,
    "p1":               None,
    "p2":               None,
    "daily_pnl":        0.0,
    "last_trade_date":  None,
    "last_signal_bar":  None,
    "paused":           False,
    "consecutive_sl":   0,
    "session_stopped":  False,
    "atr_cache":        0.0,
    "wins":             0,
    "losses":           0,
}

# ─────────────────────────────────────────────────────────────────────────────
# ASYNC BRIDGE
# ─────────────────────────────────────────────────────────────────────────────
async def _run(fn, *args):
    return await asyncio.get_event_loop().run_in_executor(_EXECUTOR, fn, *args)

# ─────────────────────────────────────────────────────────────────────────────
# MT5 PRIMITIVES
# ─────────────────────────────────────────────────────────────────────────────
def _connect() -> bool:
    if not MT5_AVAILABLE:
        return False
    if not mt5.initialize():
        log.error(f"mt5.initialize() failed: {mt5.last_error()}")
        return False
    if MT5_LOGIN:
        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            log.error(f"mt5.login() failed: {mt5.last_error()}")
            return False
    log.info("MT5 connected")
    return True

def _disconnect():
    if MT5_AVAILABLE:
        mt5.shutdown()

def _fetch_h1_sync(count: int) -> pd.DataFrame | None:
    if not MT5_AVAILABLE:
        return _simulate_h1(count)
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, count)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df.rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "tick_volume": "Volume",
    }, inplace=True)
    return df

def _simulate_h1(count: int) -> pd.DataFrame:
    np.random.seed(int(time.time()) % 9999)
    base   = 65000.0
    rets   = np.random.randn(count) * 300
    closes = base + np.cumsum(rets)
    hi     = closes + np.abs(np.random.randn(count) * 150)
    lo     = closes - np.abs(np.random.randn(count) * 150)
    op     = closes - np.random.randn(count) * 100
    vol    = np.random.randint(1000, 15000, count).astype(float)
    return pd.DataFrame({"Open": op, "High": hi, "Low": lo, "Close": closes, "Volume": vol})

async def fetch_h1(count: int = CANDLES_NEEDED) -> pd.DataFrame | None:
    return await _run(_fetch_h1_sync, count)

def _tick_sync() -> tuple[float, float]:
    if not MT5_AVAILABLE:
        return 65000.0, 65010.0
    t = mt5.symbol_info_tick(SYMBOL)
    return (t.bid, t.ask) if t else (0.0, 0.0)

async def get_tick() -> tuple[float, float]:
    return await _run(_tick_sync)

async def get_spread_usd() -> float:
    bid, ask = await get_tick()
    return round(ask - bid, 2)

def _positions_sync() -> list:
    if not MT5_AVAILABLE:
        return []
    pos = mt5.positions_get(symbol=SYMBOL)
    if not pos:
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
        }
        for p in pos
    ]

async def get_positions() -> list:
    return await _run(_positions_sync)

def _send_sync(direction: str, lot: float, sl: float, tp: float | None) -> dict | None:
    if not MT5_AVAILABLE:
        ticket = int(time.time() * 1000) % 999999
        return {"ticket": ticket, "direction": direction, "sl": sl, "tp": tp, "lot": lot}
    ot   = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(SYMBOL)
    if not tick:
        return None
    price = tick.ask if direction == "BUY" else tick.bid
    req = {
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       lot,
        "type":         ot,
        "price":        price,
        "sl":           sl,
        "deviation":    30,
        "magic":        MAGIC_NUMBER,
        "comment":      "BTC-SQ-H1",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if tp is not None:
        req["tp"] = tp
    r = mt5.order_send(req)
    if r.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"order_send failed: {r.retcode} {r.comment}")
        return None
    return {"ticket": r.order, "direction": direction, "entry": price,
            "sl": sl, "tp": tp, "lot": lot}

async def send_order(direction: str, lot: float, sl: float,
                     tp: float | None) -> dict | None:
    return await _run(_send_sync, direction, lot, sl, tp)

def _move_sl_sync(ticket: int, new_sl: float) -> bool:
    if not MT5_AVAILABLE:
        return True
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    r = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos[0].tp,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE

async def move_sl(ticket: int, new_sl: float) -> bool:
    return await _run(_move_sl_sync, ticket, new_sl)

def _close_sync(ticket: int) -> bool:
    if not MT5_AVAILABLE:
        return True
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        return False
    p    = pos[0]
    ct   = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(SYMBOL)
    price = tick.bid if p.type == mt5.ORDER_TYPE_BUY else tick.ask
    r = mt5.order_send({
        "action":       mt5.TRADE_ACTION_DEAL,
        "symbol":       SYMBOL,
        "volume":       p.volume,
        "type":         ct,
        "position":     ticket,
        "price":        price,
        "deviation":    30,
        "magic":        MAGIC_NUMBER,
        "comment":      "BTC-SQ-H1-CLOSE",
        "type_time":    mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    })
    return r.retcode == mt5.TRADE_RETCODE_DONE

async def close_position(ticket: int) -> bool:
    return await _run(_close_sync, ticket)

def _is_open_sync(ticket: int) -> bool:
    if not MT5_AVAILABLE:
        return True
    pos = mt5.positions_get(ticket=ticket)
    return pos is not None and len(pos) > 0

async def is_open(ticket: int) -> bool:
    return await _run(_is_open_sync, ticket)

def _get_deal_pnl_sync(ticket: int) -> tuple[float, float | None]:
    if not MT5_AVAILABLE:
        return 0.0, None
    try:
        tf  = datetime.now() - timedelta(days=5)
        tt  = datetime.now() + timedelta(days=1)
        deals = mt5.history_deals_get(tf, tt)
        if not deals:
            return 0.0, None
        for d in reversed(deals):
            if d.position_id == ticket and d.entry == mt5.DEAL_ENTRY_OUT:
                return round(d.profit, 2), round(d.price, 2)
    except Exception as e:
        log.error(f"_get_deal_pnl_sync: {e}")
    return 0.0, None

async def get_deal_pnl(ticket: int) -> tuple[float, float | None]:
    return await _run(_get_deal_pnl_sync, ticket)

# ─────────────────────────────────────────────────────────────────────────────
# VECTORISED INDICATORS
# ─────────────────────────────────────────────────────────────────────────────
def _atr_series(df: pd.DataFrame, period: int) -> pd.Series:
    tr = pd.concat([
        df["High"] - df["Low"],
        (df["High"] - df["Close"].shift(1)).abs(),
        (df["Low"]  - df["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_signals(df: pd.DataFrame) -> dict:
    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    vol    = df["Volume"]

    # ── Bollinger Bands ───────────────────────────────────────────────────────
    bb_mid  = close.rolling(BB_PERIOD).mean()
    bb_std  = close.rolling(BB_PERIOD).std(ddof=0)
    bb_up   = bb_mid + BB_DEV * bb_std
    bb_lo   = bb_mid - BB_DEV * bb_std

    # ── ATR & Keltner Channels ────────────────────────────────────────────────
    atr_s   = _atr_series(df, KC_PERIOD)
    kc_mid  = close.rolling(KC_PERIOD).mean()
    kc_up   = kc_mid + KC_MULT * atr_s
    kc_lo   = kc_mid - KC_MULT * atr_s

    # ── ATR (trade sizing) ────────────────────────────────────────────────────
    atr14   = _atr_series(df, ATR_PERIOD)

    # ── Volume SMA ────────────────────────────────────────────────────────────
    vol_sma = vol.rolling(VOL_SMA_PERIOD).mean()

    # ── EMA 20 (trailing stop anchor for P2) ─────────────────────────────────
    ema20   = close.ewm(span=EMA_TRAIL_PERIOD, adjust=False).mean()

    # ── EMA 200 (macro trend filter) ─────────────────────────────────────────
    ema200  = close.ewm(span=200, adjust=False).mean()

    # ── Squeeze state: BB inside KC ──────────────────────────────────────────
    squeeze = (bb_up < kc_up) & (bb_lo > kc_lo)

    n = len(df)
    prev_sq  = squeeze.iloc[n - 2]   # previous bar was in squeeze
    cur_sq   = squeeze.iloc[n - 1]
    cur_close = close.iloc[n - 1]
    prev_close= close.iloc[n - 2]
    cur_bb_up = bb_up.iloc[n - 1]
    cur_bb_lo = bb_lo.iloc[n - 1]
    cur_vol   = vol.iloc[n - 1]
    cur_vsma  = vol_sma.iloc[n - 1]
    cur_atr14 = atr14.iloc[n - 1]
    cur_ema20 = ema20.iloc[n - 1]

    cur_ema200 = ema200.iloc[n - 1]
    trend_bull = cur_close > cur_ema200
    trend_bear = cur_close < cur_ema200

    # Breakout candle: previous bar in squeeze, current bar closes beyond BB
    # + EMA200 macro trend filter (eliminates counter-trend false breakouts)
    breakout_long  = (
        prev_sq
        and not cur_sq
        and cur_close > cur_bb_up
        and cur_vol   > VOL_BREAKOUT_FACTOR * cur_vsma
        and trend_bull
    )
    breakout_short = (
        prev_sq
        and not cur_sq
        and cur_close < cur_bb_lo
        and cur_vol   > VOL_BREAKOUT_FACTOR * cur_vsma
        and trend_bear
    )

    return {
        "bar_time":       df["time"].iloc[n - 1] if "time" in df.columns else n,
        "close":          round(cur_close, 2),
        "atr":            round(cur_atr14, 2),
        "ema20":          round(cur_ema20, 2),
        "ema200":         round(cur_ema200, 2),
        "trend_bull":     bool(trend_bull),
        "trend_bear":     bool(trend_bear),
        "vol_ratio":      round(cur_vol / cur_vsma, 3) if cur_vsma > 0 else 1.0,
        "squeeze":        bool(cur_sq),
        "prev_squeeze":   bool(prev_sq),
        "breakout_long":  bool(breakout_long),
        "breakout_short": bool(breakout_short),
        "bb_up":          round(cur_bb_up, 2),
        "bb_lo":          round(cur_bb_lo, 2),
        "kc_up":          round(kc_up.iloc[n - 1], 2),
        "kc_lo":          round(kc_lo.iloc[n - 1], 2),
        "ema20_series":   ema20,
    }

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY LOGIC
# ─────────────────────────────────────────────────────────────────────────────
async def attempt_entry(sig: dict, spread: float):
    if spread > MAX_SPREAD:
        log.info(f"Spread {spread} > cap {MAX_SPREAD} — entry rejected")
        return

    direction = "BUY" if sig["breakout_long"] else "SELL"
    entry     = sig["close"]
    atr       = sig["atr"]
    risk      = SL_ATR_MULT * atr

    if direction == "BUY":
        sl  = round(entry - risk, 2)
        tp1 = round(entry + TP1_RR * risk, 2)
    else:
        sl  = round(entry + risk, 2)
        tp1 = round(entry - TP1_RR * risk, 2)

    log.info(
        f"BREAKOUT {direction} @ {entry} | ATR={atr} | SL={sl} | "
        f"TP1={tp1} | VolX={sig['vol_ratio']:.2f} | Spread={spread}"
    )

    # P1 — fixed TP1, SL ATR×2
    r1 = await send_order(direction, LOT_P1, sl, tp1)
    if not r1:
        log.error("P1 order failed")
        return

    # P2 — no TP (runner), same SL
    r2 = await send_order(direction, LOT_P2, sl, None)
    if not r2:
        log.error("P2 order failed — P1 open, runner missing")

    state["p1"] = {
        "ticket":    r1["ticket"],
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "tp1":       tp1,
        "atr":       atr,
        "tp1_hit":   False,
        "closed":    False,
    }
    state["p2"] = {
        "ticket":    r2["ticket"] if r2 else None,
        "direction": direction,
        "entry":     entry,
        "sl":        sl,
        "atr":       atr,
        "closed":    False,
    } if r2 else None

    state["trades_today"]    += 1
    state["atr_cache"]        = atr
    state["last_signal_bar"]  = sig["bar_time"]
    log.info(
        f"P1 #{r1['ticket']} | P2 #{r2['ticket'] if r2 else 'N/A'} | "
        f"SL={sl} TP1={tp1} P2_runner=open"
    )

# ─────────────────────────────────────────────────────────────────────────────
# MONITOR — P1 TP / SL / P2 EMA TRAIL
# ─────────────────────────────────────────────────────────────────────────────
async def monitor(sig: dict):
    p1 = state.get("p1")
    p2 = state.get("p2")

    # ── P1 monitoring ────────────────────────────────────────────────────────
    if p1 and not p1["closed"]:
        if not await is_open(p1["ticket"]):
            pnl, exit_px = await get_deal_pnl(p1["ticket"])
            p1["closed"] = True
            state["daily_pnl"] = round(state["daily_pnl"] + pnl, 2)
            tp_tol = p1["atr"] * 0.05

            if exit_px and abs(exit_px - p1["tp1"]) <= tp_tol:
                # TP1 hit — move P2 SL to breakeven
                p1["tp1_hit"] = True
                state["wins"] += 1
                state["consecutive_sl"] = 0
                if p2 and not p2["closed"]:
                    ok = await move_sl(p2["ticket"], p1["entry"])
                    if ok:
                        p2["sl"] = p1["entry"]
                        log.info(
                            f"P1 TP1 hit @ {exit_px} | PnL={pnl} | "
                            f"P2 SL moved to entry {p1['entry']}"
                        )
            else:
                state["losses"] += 1
                state["consecutive_sl"] += 1
                log.warning(
                    f"P1 SL hit @ {exit_px} | PnL={pnl} | "
                    f"Consec SL={state['consecutive_sl']}"
                )
                if state["consecutive_sl"] >= MAX_CONSECUTIVE_SL:
                    state["session_stopped"] = True
                    log.warning("CIRCUIT BREAKER — session stopped")
                    if p2 and not p2["closed"]:
                        await close_position(p2["ticket"])
                        p2["closed"] = True

    # ── P2 EMA trailing stop ──────────────────────────────────────────────────
    if p2 and not p2["closed"]:
        if not await is_open(p2["ticket"]):
            pnl, exit_px = await get_deal_pnl(p2["ticket"])
            p2["closed"] = True
            state["daily_pnl"] = round(state["daily_pnl"] + pnl, 2)
            if not p1 or not p1.get("tp1_hit"):
                state["losses"] += 1
                state["consecutive_sl"] += 1
            log.info(f"P2 closed @ {exit_px} | PnL={pnl}")
        else:
            # Adjust SL to EMA20 on every closed H1 candle
            ema_val = round(sig["ema20"], 2)
            direction = p2["direction"]
            current_sl = p2["sl"]

            if direction == "BUY":
                # Only trail upward (never widen)
                if ema_val > current_sl:
                    ok = await move_sl(p2["ticket"], ema_val)
                    if ok:
                        log.info(f"P2 trail SL BUY: {current_sl} → {ema_val} (EMA20)")
                        p2["sl"] = ema_val
            else:
                # Trail downward for SELL
                if ema_val < current_sl:
                    ok = await move_sl(p2["ticket"], ema_val)
                    if ok:
                        log.info(f"P2 trail SL SELL: {current_sl} → {ema_val} (EMA20)")
                        p2["sl"] = ema_val

    # ── Clean up closed sequence ──────────────────────────────────────────────
    p1_done = (not p1) or p1["closed"]
    p2_done = (not p2) or p2["closed"]
    if p1_done and p2_done and (p1 or p2):
        log.info(
            f"Sequence closed | Daily PnL={state['daily_pnl']} | "
            f"W={state['wins']} L={state['losses']}"
        )
        state["p1"] = None
        state["p2"] = None

# ─────────────────────────────────────────────────────────────────────────────
# DAILY RESET
# ─────────────────────────────────────────────────────────────────────────────
def _daily_reset(today: date):
    if state["last_trade_date"] == today:
        return
    state["trades_today"]    = 0
    state["daily_pnl"]       = 0.0
    state["last_trade_date"] = today
    state["session_stopped"] = False
    state["consecutive_sl"]  = 0
    state["wins"]            = 0
    state["losses"]          = 0
    log.info(f"Daily reset — {today}")

# ─────────────────────────────────────────────────────────────────────────────
# STARTUP RESTORATION
# ─────────────────────────────────────────────────────────────────────────────
async def restore():
    positions = await get_positions()
    if not positions:
        return
    log.info(f"Restoring {len(positions)} open position(s)")
    for p in positions[:2]:
        slot = "p1" if state["p1"] is None else "p2"
        state[slot] = {
            "ticket":    p["ticket"],
            "direction": p["direction"],
            "entry":     p["entry"],
            "sl":        p["sl"],
            "tp1":       p.get("tp", 0),
            "atr":       state["atr_cache"],
            "tp1_hit":   False,
            "closed":    False,
        }
        log.info(f"Restored {slot} #{p['ticket']}")

# ─────────────────────────────────────────────────────────────────────────────
# MAIN LOOP — H1 CADENCE
# ─────────────────────────────────────────────────────────────────────────────
async def analysis_loop():
    log.info("BTC Squeeze H1 — loop started")
    _last_bar_time = None

    while True:
        try:
            today = date.today()
            _daily_reset(today)

            if state["paused"]:
                await asyncio.sleep(30)
                continue

            if state["session_stopped"]:
                if int(time.time()) % 300 == 0:
                    log.warning("Session stopped (circuit breaker). Resuming tomorrow.")
                await asyncio.sleep(60)
                continue

            if state["daily_pnl"] < DAILY_MAX_LOSS:
                if int(time.time()) % 300 == 0:
                    log.warning(f"Daily stop: PnL={state['daily_pnl']} < {DAILY_MAX_LOSS}")
                await asyncio.sleep(60)
                continue

            df = await fetch_h1(CANDLES_NEEDED)
            if df is None or len(df) < BB_PERIOD + 10:
                await asyncio.sleep(30)
                continue

            sig = compute_signals(df)
            spread = await get_spread_usd()

            # ── Monitor open positions every cycle ───────────────────────────
            has_open = state["p1"] is not None or state["p2"] is not None
            if has_open:
                await monitor(sig)
                await asyncio.sleep(15)
                continue

            # ── Only act on new closed H1 bar ────────────────────────────────
            bar_time = sig["bar_time"]
            if bar_time == _last_bar_time:
                await asyncio.sleep(15)
                continue
            _last_bar_time = bar_time

            if state["trades_today"] >= MAX_TRADES_PER_DAY:
                await asyncio.sleep(30)
                continue

            log.info(
                f"H1 bar={bar_time} | close={sig['close']} | "
                f"EMA200={sig['ema200']} | trend={'BULL' if sig['trend_bull'] else 'BEAR'} | "
                f"squeeze={sig['squeeze']} | prev_sq={sig['prev_squeeze']} | "
                f"VolX={sig['vol_ratio']:.2f} | spread={spread}"
            )

            if sig["breakout_long"] or sig["breakout_short"]:
                await attempt_entry(sig, spread)

        except Exception as e:
            log.error(f"Loop error: {e}", exc_info=True)

        gc.collect()
        await asyncio.sleep(15)

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────
async def main():
    await _run(_connect)
    log.info("BTC Squeeze Breakout H1 — started | P1 TP1@RR2 | P2 EMA20 Trail")
    await restore()
    try:
        await analysis_loop()
    finally:
        _disconnect()
        _EXECUTOR.shutdown(wait=False)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot shutdown")
