import os

# ==========================================
# 🔐 MT5 CREDENTIALS
# ==========================================
MT5_LOGIN    = 0                      # Replace with your account number (int)
MT5_PASSWORD = "your_password"
MT5_SERVER   = "YourBroker-Demo"

# ==========================================
# ⚙️ TRADING PARAMETERS
# ==========================================
SYMBOL            = "BTCUSD"
LOT_SIZE          = 0.02              # Base lot — split 50/50 between P1 and P2
MAX_SPREAD        = 50.0              # Max spread in USD (BTC spreads are wide)
MAX_TRADES_PER_DAY = 5               # Conservative for H1 trend model
MAGIC_NUMBER      = 200300

# ==========================================
# ⏱️ TIMEFRAMES
# ==========================================
try:
    import MetaTrader5 as mt5
    TIMEFRAME_H1 = mt5.TIMEFRAME_H1
except ImportError:
    TIMEFRAME_H1 = 16385
