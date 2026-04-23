import sqlite3
import logging
import os
from datetime import datetime

log = logging.getLogger(__name__)

DB_NAME = "trading_history.db"

def init_db():
    """Initialise la base de données et crée la table trades si elle n'existe pas."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        ticket INTEGER PRIMARY KEY,
        magic INTEGER,
        symbol TEXT,
        direction TEXT,
        open_time TEXT,
        close_time TEXT,
        open_price REAL,
        close_price REAL,
        lot_size REAL,
        pnl_usd REAL,
        pnl_points REAL,
        stop_loss REAL,
        tp1 REAL,
        tp2 REAL,
        tp3 REAL,
        exit_type TEXT,
        duration_sec INTEGER,
        trading_mode TEXT,
        score INTEGER,
        reasons TEXT,
        rsi REAL,
        stoch_k REAL,
        stoch_d REAL,
        macd_hist REAL,
        atr REAL,
        spread REAL,
        ema9 REAL,
        ema20 REAL,
        ema50 REAL,
        ema200 REAL,
        pattern TEXT,
        vol_ratio REAL
    )
    """)
    conn.commit()
    conn.close()
    log.info(f"💾 Base de données {DB_NAME} initialisée.")

def save_open_trade(trade_data: dict):
    """
    Enregistre l'ouverture d'un trade avec tout son contexte technique.
    trade_data doit contenir toutes les clés nécessaires.
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # On utilise INSERT OR REPLACE au cas où le bot essaierait de ré-ouvrir un ticket existant
        query = """
        INSERT OR REPLACE INTO trades (
            ticket, magic, symbol, direction, open_time, open_price, 
            lot_size, stop_loss, tp1, tp2, tp3, trading_mode, score, 
            reasons, rsi, stoch_k, stoch_d, macd_hist, atr, spread, 
            ema9, ema20, ema50, ema200, pattern, vol_ratio
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        
        values = (
            trade_data['ticket'], trade_data.get('magic', 0), trade_data.get('symbol', 'XAUUSD'),
            trade_data['direction'], datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            trade_data['entry'], trade_data['lot'], trade_data['levels']['sl'],
            trade_data['levels']['tp1'], trade_data['levels']['tp2'], trade_data['levels']['tp3'],
            trade_data.get('trading_mode'), trade_data.get('score'), trade_data.get('reasons'),
            trade_data.get('rsi'), trade_data.get('stoch_k'), trade_data.get('stoch_d'),
            trade_data.get('macd_hist'), trade_data.get('atr'), trade_data.get('spread'),
            trade_data.get('ema9'), trade_data.get('ema20'), trade_data.get('ema50'),
            trade_data.get('ema200'), trade_data.get('pattern'), trade_data.get('vol_ratio')
        )
        
        cursor.execute(query, values)
        conn.commit()
        conn.close()
        log.debug(f"💾 Trade #{trade_data['ticket']} enregistré en base.")
    except Exception as e:
        log.error(f"❌ Erreur save_open_trade : {e}")

def update_closed_trade(ticket: int, close_data: dict):
    """
    Met à jour un trade existant lors de sa fermeture.
    close_data contient : close_price, pnl_usd, pnl_points, exit_type, duration_sec
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        query = """
        UPDATE trades SET 
            close_time = ?, close_price = ?, pnl_usd = ?, 
            pnl_points = ?, exit_type = ?, duration_sec = ?
        WHERE ticket = ?
        """
        
        values = (
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            close_data['close_price'], close_data['pnl_usd'],
            close_data['pnl_points'], close_data['exit_type'],
            close_data['duration_sec'], ticket
        )
        
        cursor.execute(query, values)
        conn.commit()
        conn.close()
        log.info(f"✅ Trade #{ticket} mis à jour dans la base (PnL: {close_data['pnl_usd']}$)")
    except Exception as e:
        log.error(f"❌ Erreur update_closed_trade : {e}")
