import os

# ==========================================
# 🔐 IDENTIFIANTS META TRADER 5 (VTMarkets)
# ==========================================
MT5_LOGIN = gggggg              # Ton numéro de compte démo (sans guillemets, juste les chiffres)
MT5_PASSWORD = "gggggg"    # Ton mot de passe (Garde les guillemets !)
MT5_SERVER = "VTMarkets-Demo"     # Le nom exact du serveur (Vérifie bien le nom sur ton MT5)

# ==========================================
# ⚙️ PARAMÈTRES DE TRADING DU BOT
# ==========================================
SYMBOL = "XAUUSD-VIP"                 # Le symbole à trader (l'Or)
LOT_SIZE = 0.03                   # Taille du lot de base pour tes trades
MAX_SPREAD = 3.0                  # Sécurité : Spread maximum toléré pour entrer en position (en pips/points)
MAX_TRADES_PER_DAY = 60            # Sécurité : Limite le nombre de trades par jour pour éviter l'overtrading
MAGIC_NUMBER = 100200             # "Plaque d'immatriculation" du bot pour qu'il reconnaisse ses propres trades

# ==========================================
# ⏱️ UNITÉS DE TEMPS (TIMEFRAMES)
# ==========================================
# Ces valeurs correspondent aux codes internes de MetaTrader 5
try:
    import MetaTrader5 as mt5
    TIMEFRAME_M5 = mt5.TIMEFRAME_M5
    TIMEFRAME_M15 = mt5.TIMEFRAME_M15
    TIMEFRAME_H1 = mt5.TIMEFRAME_H1
except ImportError:
    # Valeurs de secours si la librairie n'est pas chargée immédiatement
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 16385
