"""
╔══════════════════════════════════════════════════════════╗
║         SIGNAL COPIER — TELEGRAM → MT5                  ║
║         Copie les signaux de canaux Telegram             ║
║         sur ton compte MT5 automatiquement               ║
╠══════════════════════════════════════════════════════════╣
║  CANAUX ÉCOUTÉS :                                       ║
║    - Station X                                          ║
║    - Full Margin Only                                   ║
║    (+ tout canal que tu ajoutes dans CHANNELS)          ║
╠══════════════════════════════════════════════════════════╣
║  FONCTIONNEMENT :                                       ║
║    1. Se connecte à Telegram avec ton compte perso      ║
║    2. Écoute les canaux en temps réel                   ║
║    3. Détecte "Gold sell now!" → entre IMMÉDIATEMENT    ║
║    4. Signal complet → met à jour TP/SL sur MT5         ║
║    5. T'envoie une notif sur ton bot Telegram           ║
║                                                         ║
║  MAGIC NUMBER : 20250202 (différent du bot principal)   ║
║  → Les deux bots ne se marchent pas dessus              ║
╠══════════════════════════════════════════════════════════╣
║  INSTALLATION :                                         ║
║    pip install telethon                                 ║
║                                                         ║
║  PREMIÈRE UTILISATION :                                 ║
║    python signal_copier.py                              ║
║    → Il te demande ton numéro de téléphone Telegram     ║
║    → Puis le code SMS reçu                              ║
║    → Crée un fichier session local (une seule fois)     ║
╚══════════════════════════════════════════════════════════╝
"""

import asyncio
import logging
import re
import time
from datetime import datetime

# ── Imports ───────────────────────────────────────────────────────────────────
try:
    from telethon import TelegramClient, events
    from telethon.tl.types import Channel, Chat
    TELETHON_AVAILABLE = True
except ImportError:
    TELETHON_AVAILABLE = False
    print("❌ telethon non installé — pip install telethon")

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    print("⚠️ MetaTrader5 non installé — mode simulation")

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

# ── Config ────────────────────────────────────────────────────────────────────
# Récupère depuis config.py du bot principal
from history.config import (
    MT5_LOGIN, MT5_PASSWORD, MT5_SERVER,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    LOT_SIZE, SYMBOL
)

# ── Paramètres Telegram User API ─────────────────────────────────────────────
# Va sur https://my.telegram.org → API development tools
# Crée une app et copie les valeurs ici
TELEGRAM_API_ID   = 33630903         # ← Remplace par ton api_id (numérique)
TELEGRAM_API_HASH = "de917336f84ac00d5d8e9c42f2a675e8"         # ← Remplace par ton api_hash (string)
TELEGRAM_PHONE    = "+32473394010"         # ← Ton numéro avec indicatif ex: "+33612345678"
SESSION_NAME      = "signal_copier_session"   # Fichier de session créé localement

# ── Canaux à écouter ──────────────────────────────────────────────────────────
# Mets le @username du canal, ou l'URL, ou l'ID numérique
# Pour trouver l'ID d'un canal privé : forward un message vers @userinfobot
# ⚠️  Laisse vide [] la première fois → le bot listera tous tes canaux
# Puis remplace avec les IDs numériques trouvés :
# CHANNELS = [-1001234567890, -1009876543210]
CHANNELS = [-1002108611851, -1002481537588]   # ← remplis après le premier lancement

# ── Paramètres trading ────────────────────────────────────────────────────────
MAGIC_NUMBER    = 20250202     # Différent du bot principal (20250101)
LOT_PER_SIGNAL  = LOT_SIZE     # Lot par trade copié (modifiable)
NB_TRADES       = 3            # Nombre de positions par signal (1=TP1 only, 3=TP1+TP2+TP3)
AUTO_COPY       = True         # True = copie auto / False = notif Telegram + confirmation manuelle
SYMBOLS_FILTER  = ["XAUUSD", "GOLD", "XAU"]   # Symboles à copier (ignore le reste)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | COPIER | %(levelname)s | %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("signal_copier.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)

# ── Stats ─────────────────────────────────────────────────────────────────────
stats = {
    "signals_reçus":  0,
    "trades_placés":  0,
    "trades_ignorés": 0,
    "pnl_total":      0.0,
}

# Trades ouverts par le copier — surveillés pour notifier TP/SL
active_copies: dict = {}

# Entrées rapides en attente de signal complet
# Format : {source: {"direction", "tickets": [], "entry_time", "entry_price"}}
# Quand le signal complet arrive → on met à jour TP/SL sur MT5
pending_quick: dict = {}

# Délai max pour recevoir le signal complet après l'alerte rapide (secondes)
QUICK_ENTRY_TIMEOUT = 120   # 2 minutes pour recevoir le signal complet

# Paramètres dynamiques (modifiables via commandes Telegram du bot principal)
copier_settings = {
    "lot":    LOT_SIZE,   # changeable via /lotcopier
    "trades": 3,          # changeable via /tradescopier
}


# ════════════════════════════════════════════════════════════════════════════════
#  CONNEXION MT5
# ════════════════════════════════════════════════════════════════════════════════

def connect_mt5() -> bool:
    if not MT5_AVAILABLE:
        log.warning("MT5 non disponible — mode simulation")
        return False
    if not mt5.initialize():
        log.error(f"MT5 initialize() échoué : {mt5.last_error()}")
        return False
    if MT5_LOGIN:
        ok = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not ok:
            log.error(f"MT5 login échoué : {mt5.last_error()}")
            return False
    log.info("✅ MT5 connecté")
    return True


def _move_sl(ticket: int, new_sl: float) -> bool:
    """
    Déplace le SL d'une position en préservant son TP existant.
    IMPORTANT : on récupère le TP actuel depuis MT5 avant de modifier
    sinon TRADE_ACTION_SLTP efface le TP → la position perd son objectif.
    """
    if not MT5_AVAILABLE:
        log.info(f"[SIM] SL→{new_sl} pour #{ticket}")
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    pos    = positions[0]
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       new_sl,
        "tp":       pos.tp,   # ← préserve le TP existant
    })
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"SL move échoué #{ticket} : {result.retcode}")
        return False
    log.info(f"✅ SL→{new_sl} (TP conservé @ {pos.tp}) pour #{ticket}")
    return True


# ════════════════════════════════════════════════════════════════════════════════
#  PARSER DE SIGNAUX
# ════════════════════════════════════════════════════════════════════════════════

def parse_signal(text: str) -> dict | None:
    """
    Analyse un message Telegram et extrait le signal de trading.

    Formats supportés :

    Station X :
      🔴 JE VENDS XAUUSD à 4806
      🎯 TP1 : 4803
      🎯 TP2 : 4796
      🎯 TP3 : Ouvert   ← ignoré (pas de prix)
      🔒 SL : 4809

    Full Margin Only :
      Gold Sell now : 4763-4770   ← zone d'entrée → on prend le milieu
      stop loss : 4774.5
      take profit 1 : 4753
      take profit 2 : 4746

    Messages à ignorer :
      "Gold sell now!"              ← pas de niveaux
      "VENTE XAUUSD NOW !"          ← pas de niveaux
      "READY XAUUSD"                ← image de préparation
      "TP1 TOUCHÉ 🔥 +30 PIPS ✅"   ← résultat, pas un signal
    """
    if not text:
        return None

    text_upper = text.upper()

    # ── Filtre les messages non-signaux ──────────────────────────────────────
    # Messages d'alerte sans niveaux (ex: "Gold sell now!", "VENTE NOW!")
    # On les ignore car ils n'ont pas de TP/SL
    noise_patterns = [
        r'tp\d?\s+touch[eé]',           # "TP1 TOUCHÉ"
        r'sl\s+touch[eé]',              # "SL touché"
        r'ready\s+xauusd',              # "READY XAUUSD"
        r'pips\s*[✅✔]',               # "+30 PIPS ✅"
    ]
    for pat in noise_patterns:
        if re.search(pat, text, re.IGNORECASE):
            log.debug(f"Message ignoré (bruit) : {text[:60]}")
            return None

    # ── Vérifie que c'est un signal XAUUSD ──────────────────────────────────
    is_gold = any(s in text_upper for s in SYMBOLS_FILTER)
    if not is_gold:
        return None

    # ── Direction ─────────────────────────────────────────────────────────────
    direction = None
    # Station X : "JE VENDS", "J'ACHÈTE"
    # Full Margin : "Gold Sell now", "Gold Buy now"
    buy_keywords  = ["J'ACHET", "JACHET", "BUY", "LONG", "ACHAT", "🟢"]
    sell_keywords = ["JE VENDS", "JEVENDS", "SELL", "SHORT", "VENTE", "VEND", "🔴"]

    for kw in buy_keywords:
        if kw in text_upper:
            direction = "BUY"
            break
    if direction is None:
        for kw in sell_keywords:
            if kw in text_upper:
                direction = "SELL"
                break

    if direction is None:
        return None

    # ── Prix d'entrée ─────────────────────────────────────────────────────────
    entry = None

    # Station X : "JE VENDS XAUUSD à 4806"
    m = re.search(r'(?:à|@)\s*(\d{4,5}(?:\.\d{1,2})?)', text, re.IGNORECASE)
    if m:
        entry = float(m.group(1))

    # Full Margin Only : "Gold Sell now : 4763-4770" → prend le milieu de la zone
    if entry is None:
        m = re.search(r':\s*(\d{4,5}(?:\.\d{1,2})?)\s*[-–]\s*(\d{4,5}(?:\.\d{1,2})?)', text)
        if m:
            low  = float(m.group(1))
            high = float(m.group(2))
            entry = round((low + high) / 2, 2)
            log.info(f"Zone d'entrée {low}-{high} → milieu {entry}")

    # Autres formats : "entry: 4623", "price: 4623"
    if entry is None:
        m = re.search(r'(?:entry|price|entrée)\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', text, re.IGNORECASE)
        if m:
            entry = float(m.group(1))

    # ── TP1, TP2, TP3 ─────────────────────────────────────────────────────────
    tp1 = tp2 = tp3 = None

    # Station X : "🎯 TP1 : 4803" / "🎯 TP3 : Ouvert" (ignoré)
    # Full Margin : "take profit 1 : 4753"
    tp_patterns = [
        # "take profit 1 : 4753" ou "take profit1 : 4753"
        (r'take\s*profit\s*1\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 1),
        (r'take\s*profit\s*2\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 2),
        (r'take\s*profit\s*3\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 3),
        # "TP1 : 4803" ou "TP1: 4803"
        (r'tp1\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 1),
        (r'tp2\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 2),
        (r'tp3\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 3),
        # "TP : 4753" seul (TP unique)
        (r'(?:^|\n)\s*tp\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 1),
        # "take profit : 4753" sans numéro
        (r'take\s*profit\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)', 1),
    ]
    for pat, num in tp_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if num == 1 and tp1 is None:   tp1 = val
            elif num == 2 and tp2 is None: tp2 = val
            elif num == 3 and tp3 is None: tp3 = val

    # ── SL ────────────────────────────────────────────────────────────────────
    sl = None
    # Station X : "🔒 SL : 4809"
    # Full Margin : "stop loss : 4774.5"
    sl_patterns = [
        r'stop\s*loss\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)',
        r'(?:^|\s)sl\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)',
        r'stop\s*:?\s*(\d{4,5}(?:\.\d{1,2})?)',
    ]
    for pat in sl_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            sl = float(m.group(1))
            break

    # ── Validation ────────────────────────────────────────────────────────────
    if tp1 is None or sl is None:
        log.debug(f"Signal incomplet — tp1={tp1} sl={sl} | {text[:80]}")
        return None

    signal = {
        "direction": direction,
        "entry":     entry,
        "tp1":       tp1,
        "tp2":       tp2,
        "tp3":       tp3,
        "sl":        sl,
        "raw":       text[:200],
    }
    log.info(f"✅ Signal parsé : {direction} Entry:{entry} TP1:{tp1} TP2:{tp2} TP3:{tp3} SL:{sl}")
    return signal


def parse_quick_alert(text: str) -> str | None:
    """
    Détecte les messages d'alerte rapide SANS niveaux TP/SL.
    Ces messages arrivent 2-5 secondes AVANT le signal complet.

    Station X  : "VENTE XAUUSD NOW !" / "ACHAT XAUUSD NOW !"
    Full Margin: "Gold sell now!" / "Gold buy now!"

    Retourne "BUY", "SELL" ou None.
    """
    if not text:
        return None
    text_upper = text.upper()

    # Doit contenir XAUUSD ou GOLD
    if not any(s in text_upper for s in ["XAUUSD", "GOLD", "XAU"]):
        return None

    # Ne doit PAS contenir de niveaux (sinon c'est un signal complet)
    has_levels = bool(re.search(r'(?:tp|sl|stop|take\s*profit)\s*[:\d]', text, re.IGNORECASE))
    if has_levels:
        return None

    # Doit contenir NOW ou être un message court d'alerte
    is_alert = bool(re.search(r'now\s*!?\s*$|now\s*:\s*$', text, re.IGNORECASE)) or len(text.strip()) < 30

    if not is_alert:
        return None

    # Direction
    buy_kw  = ["ACHAT", "BUY", "J'ACHET", "LONG", "🟢"]
    sell_kw = ["VENTE", "SELL", "JE VENDS", "SHORT", "🔴"]

    for kw in sell_kw:
        if kw in text_upper:
            return "SELL"
    for kw in buy_kw:
        if kw in text_upper:
            return "BUY"

    return None


def update_sl_tp_on_mt5(ticket: int, sl: float, tp: float) -> bool:
    """Met à jour le SL et TP d'une position existante sur MT5."""
    if not MT5_AVAILABLE:
        log.info(f"[SIM] Update #{ticket} SL:{sl} TP:{tp}")
        return True
    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return False
    result = mt5.order_send({
        "action":   mt5.TRADE_ACTION_SLTP,
        "position": ticket,
        "sl":       sl,
        "tp":       tp,
    })
    return result.retcode == mt5.TRADE_RETCODE_DONE


# ════════════════════════════════════════════════════════════════════════════════
#  EXÉCUTION MT5
# ════════════════════════════════════════════════════════════════════════════════

def get_temp_levels(direction: str) -> tuple[float, float]:
    """
    Calcule des TP/SL temporaires pour les entrées rapides.
    SL large (8 pips) pour laisser le temps au signal complet d'arriver
    et éviter de se faire sortir avant la mise à jour des vrais niveaux.
    TP temporaire à 6 pips — sera remplacé par le vrai TP du signal complet.
    """
    tick  = mt5.symbol_info_tick(SYMBOL) if MT5_AVAILABLE else None
    price = (tick.ask if direction == "BUY" else tick.bid) if tick else 4800.0
    sl_buffer = 8.0   # SL large = 8 pips pour tenir pendant l'attente du signal
    tp_temp   = 6.0   # TP temporaire — remplacé par vrais TP dans 5-30s
    if direction == "BUY":
        return round(price + tp_temp, 2), round(price - sl_buffer, 2)
    else:
        return round(price - tp_temp, 2), round(price + sl_buffer, 2)


def place_signal_orders(signal: dict) -> list[dict]:
    """
    Place les ordres MT5 depuis un signal parsé.
    Utilise lot et nb_trades depuis copier_settings (modifiables via Telegram).
    """
    direction  = signal["direction"]
    sl         = signal["sl"]
    nb         = copier_settings["trades"]
    lot        = copier_settings["lot"]
    results    = []

    tps_available = [signal["tp1"]]
    if nb >= 2 and signal.get("tp2"): tps_available.append(signal["tp2"])
    if nb >= 3 and signal.get("tp3"): tps_available.append(signal["tp3"])
    while len(tps_available) < nb:
        tps_available.append(tps_available[-1])
    tps_available = tps_available[:nb]

    for i, tp in enumerate(tps_available):
        res = _place_order(direction, tp, sl, lot)
        if res:
            results.append({**res, "tp_target": i + 1})
            log.info(f"✅ Ordre {i+1}/{nb} placé — TP{i+1}:{tp} lot:{lot}")
        else:
            log.error(f"❌ Ordre {i+1}/{nb} échoué")

    return results


def _place_order(direction: str, tp: float, sl: float, lot: float = None) -> dict | None:
    lot = lot or copier_settings["lot"]
    """Place un seul ordre sur MT5."""
    if not MT5_AVAILABLE:
        ticket = int(time.time() * 1000) % 999999
        log.info(f"[SIM] {direction} TP:{tp} SL:{sl}")
        return {"ticket": ticket, "simulated": True}

    order_type = mt5.ORDER_TYPE_BUY if direction == "BUY" else mt5.ORDER_TYPE_SELL
    tick       = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        log.error("Impossible de récupérer le prix MT5")
        return None
    price = tick.ask if direction == "BUY" else tick.bid

    result = mt5.order_send({
        "action":        mt5.TRADE_ACTION_DEAL,
        "symbol":        SYMBOL,
        "volume":        lot,
        "type":          order_type,
        "price":         price,
        "sl":            sl,
        "tp":            tp,
        "deviation":     20,
        "magic":         MAGIC_NUMBER,     # ← magic différent du bot principal
        "comment":       "SignalCopier",
        "type_time":     mt5.ORDER_TIME_GTC,
        "type_filling":  mt5.ORDER_FILLING_IOC,
    })

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"Ordre refusé : {result.retcode} — {result.comment}")
        return None

    return {"ticket": result.order, "direction": direction,
            "price": price, "tp": tp, "sl": sl}


# ════════════════════════════════════════════════════════════════════════════════
#  NOTIFICATION TELEGRAM
# ════════════════════════════════════════════════════════════════════════════════

def send_telegram_notif(text: str):
    """Envoie une notification sur ton bot Telegram."""
    if not REQUESTS_AVAILABLE:
        print(f"[NOTIF] {text}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id":    TELEGRAM_CHAT_ID,
            "text":       text,
            "parse_mode": "Markdown",
        }, timeout=5)
    except Exception as e:
        log.error(f"Notif Telegram échouée : {e}")


# ════════════════════════════════════════════════════════════════════════════════
#  HANDLER MESSAGES TELEGRAM
# ════════════════════════════════════════════════════════════════════════════════

async def handle_message(event):
    """
    Traite chaque message reçu dans les canaux.

    Deux cas :
    1. Alerte rapide ("Gold sell now!", "VENTE XAUUSD NOW!")
       → Entre IMMÉDIATEMENT au prix du marché avec TP/SL temporaires
       → Attend le signal complet pour mettre à jour les niveaux

    2. Signal complet (avec TP1/TP2/SL)
       → Si entrée rapide en attente → met à jour les niveaux sur MT5
       → Sinon → place les ordres normalement
    """
    try:
        text   = event.message.text or ""
        chat   = await event.get_chat()
        source = getattr(chat, 'title', None) or getattr(chat, 'username', '?')

        log.info(f"📨 [{source}] : {text[:80].replace(chr(10), ' ')}")

        now = datetime.now().strftime("%H:%M:%S")

        # ── CAS 0 : Commande SL BE / Close Half depuis le canal ─────────────
        # Station X    : "Mettez votre SL BE"
        # Full Margin  : "CLOSE HALF AND SET BREAKEVEN NOW!"
        be_patterns = [
            r'mettez\s+(?:votre\s+)?sl\s+(?:au\s+)?be',
            r'sl\s+(?:au\s+)?be',
            r'set\s+(?:sl\s+)?breakeven',
            r'move\s+sl\s+to\s+be',
        ]
        close_half_patterns = [
            r'close\s+half',
            r'ferme[rz]?\s+(?:la\s+)?moiti[eé]',
        ]

        is_be       = any(re.search(p, text, re.IGNORECASE) for p in be_patterns)
        is_close_half = any(re.search(p, text, re.IGNORECASE) for p in close_half_patterns)

        if is_be or is_close_half:
            canal_tickets = [
                t for t, d in active_copies.items()
                if d.get("source") == source and not d.get("closed")
            ]
            if canal_tickets:
                msgs = []

                # Close half : ferme le premier trade (TP1)
                if is_close_half and len(canal_tickets) >= 1:
                    ticket_to_close = canal_tickets[0]
                    if MT5_AVAILABLE:
                        positions = mt5.positions_get(ticket=ticket_to_close)
                        if positions:
                            pos = positions[0]
                            direction_close = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                            tick = mt5.symbol_info_tick(SYMBOL)
                            price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
                            result = mt5.order_send({
                                "action": mt5.TRADE_ACTION_DEAL,
                                "symbol": SYMBOL,
                                "volume": pos.volume,
                                "type": direction_close,
                                "position": ticket_to_close,
                                "price": price,
                                "deviation": 20,
                                "magic": MAGIC_NUMBER,
                                "comment": "SignalCopier CloseHalf",
                                "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": mt5.ORDER_FILLING_IOC,
                            })
                            if result.retcode == mt5.TRADE_RETCODE_DONE:
                                active_copies[ticket_to_close]["closed"] = True
                                msgs.append(f"🚪 Trade #{ticket_to_close} fermé")
                    else:
                        active_copies[ticket_to_close]["closed"] = True
                        msgs.append(f"🚪 [SIM] Trade #{ticket_to_close} fermé")

                # BE sur les trades restants
                if is_be:
                    remaining = [t for t in canal_tickets
                                 if not active_copies[t].get("closed")]
                    ok = 0
                    for ticket in remaining:
                        entry = active_copies[ticket].get("entry", 0)
                        if _move_sl(ticket, entry):
                            ok += 1
                    msgs.append(f"🔒 {ok} trade(s) → Breakeven")

                send_telegram_notif(
                    f"📡 *Action depuis [{source}]*\n" + "\n".join(msgs)
                )
                log.info(f"Action BE/CloseHalf depuis [{source}] : {msgs}")
            return

        # ── CAS 1 : Alerte rapide ─────────────────────────────────────────────
        quick_dir = parse_quick_alert(text)
        if quick_dir and AUTO_COPY:
            # Entrée immédiate avec TP/SL temporaires
            temp_tp, temp_sl = get_temp_levels(quick_dir)
            lot = copier_settings["lot"]
            nb  = copier_settings["trades"]

            results = []
            for i in range(nb):
                res = _place_order(quick_dir, temp_tp, temp_sl, lot)
                if res:
                    results.append({**res, "tp_target": i + 1})

            if results:
                tickets = [r["ticket"] for r in results]
                # Stocke dans pending_quick pour mise à jour quand signal complet arrive
                pending_quick[source] = {
                    "direction":  quick_dir,
                    "tickets":    tickets,
                    "entry_time": time.time(),
                    "entry_price": results[0].get("price", 0),
                    "temp_tp":    temp_tp,
                    "temp_sl":    temp_sl,
                }
                emoji = "🟢" if quick_dir == "BUY" else "🔴"
                verb  = "J'ACHÈTE" if quick_dir == "BUY" else "JE VENDS"
                send_telegram_notif(
                    f"⚡ *ENTRÉE RAPIDE — {verb} XAUUSD*\n"
                    f"📡 Source : `{source}`\n"
                    f"Prix : `{results[0].get('price', '?')}`\n"
                    f"⏳ En attente des TP/SL... | {now}"
                )
                log.info(f"⚡ Entrée rapide {quick_dir} depuis [{source}] — tickets: {tickets}")
            return

        # ── CAS 2 : Signal complet ────────────────────────────────────────────
        signal = parse_signal(text)
        if signal is None:
            stats["trades_ignorés"] += 1
            return

        stats["signals_reçus"] += 1

        # Vérifie si on a une entrée rapide en attente pour ce canal
        pending = pending_quick.get(source)
        if (pending
                and pending["direction"] == signal["direction"]
                and time.time() - pending["entry_time"] < QUICK_ENTRY_TIMEOUT):

            # ── Met à jour les niveaux sur MT5 ────────────────────────────────
            tp1 = signal["tp1"]
            sl  = signal["sl"]
            updated = 0
            nb = copier_settings["trades"]
            tps = [signal["tp1"]]
            if nb >= 2 and signal.get("tp2"): tps.append(signal["tp2"])
            if nb >= 3 and signal.get("tp3"): tps.append(signal["tp3"])
            while len(tps) < nb: tps.append(tps[-1])

            for i, ticket in enumerate(pending["tickets"]):
                tp = tps[i] if i < len(tps) else tps[-1]
                if update_sl_tp_on_mt5(ticket, sl, tp):
                    updated += 1
                    # Enregistre dans active_copies avec vrais niveaux
                    active_copies[ticket] = {
                        "direction": signal["direction"],
                        "entry":     pending["entry_price"],
                        "tp1":       signal["tp1"],
                        "tp2":       signal.get("tp2"),
                        "tp3":       signal.get("tp3"),
                        "sl":        sl,
                        "source":    source,
                        "tp_target": i + 1,
                        "lot":       copier_settings["lot"],
                        "closed":    False,
                    }

            del pending_quick[source]
            entry_delay = round(time.time() - pending["entry_time"], 1)
            emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
            send_telegram_notif(
                f"{emoji} *Niveaux mis à jour !* (+{entry_delay}s)\n"
                f"📡 Source : `{source}`\n"
                f"🎯 TP1 : `{signal['tp1']}` | SL : `{signal['sl']}`\n"
                f"✅ {updated} position(s) mise(s) à jour | {now}"
            )
            log.info(f"✅ Niveaux mis à jour sur {updated} trades depuis [{source}]")
            return

        # ── Signal complet sans entrée rapide → placement normal ──────────────
        # MAIS vérifie d'abord si des trades actifs existent déjà pour ce canal
        # Si oui → met à jour leurs niveaux au lieu d'ouvrir de nouveaux trades
        existing_tickets = [
            t for t, d in active_copies.items()
            if d.get("source") == source
            and d.get("direction") == signal["direction"]
            and not d.get("closed")
        ]
        if existing_tickets:
            # Trades existants → met à jour TP/SL sans en ouvrir de nouveaux
            nb  = copier_settings["trades"]
            tps = [signal["tp1"]]
            if nb >= 2 and signal.get("tp2"): tps.append(signal["tp2"])
            if nb >= 3 and signal.get("tp3"): tps.append(signal["tp3"])
            while len(tps) < len(existing_tickets): tps.append(tps[-1])
            updated = 0
            for i, ticket in enumerate(existing_tickets):
                tp = tps[i] if i < len(tps) else tps[-1]
                if update_sl_tp_on_mt5(ticket, signal["sl"], tp):
                    active_copies[ticket].update({
                        "tp1": signal["tp1"],
                        "tp2": signal.get("tp2"),
                        "tp3": signal.get("tp3"),
                        "sl":  signal["sl"],
                    })
                    updated += 1
            emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
            send_telegram_notif(
                f"{emoji} *Niveaux mis à jour* ({source})\n"
                f"🎯 TP1:`{signal['tp1']}` | SL:`{signal['sl']}`\n"
                f"✅ {updated} trade(s) existant(s) mis à jour | {now}"
            )
            log.info(f"✅ Niveaux mis à jour sur {updated} trades existants [{source}]")
            return

        if AUTO_COPY:
            results = place_signal_orders(signal)
            nb_ok   = len(results)
            if nb_ok > 0:
                stats["trades_placés"] += nb_ok
                emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
                verb  = "J'ACHÈTE" if signal["direction"] == "BUY" else "JE VENDS"
                nb_str = f"{nb_ok}/{copier_settings['trades']}"
                msg = (
                    f"{emoji} *{verb} XAUUSD*\n"
                    f"📡 Source : `{source}`\n\n"
                )
                tps_placed = []
                if signal.get("tp1"): tps_placed.append(f"🎯 TP1 : `{signal['tp1']}`")
                if signal.get("tp2") and copier_settings["trades"] >= 2: tps_placed.append(f"🎯 TP2 : `{signal['tp2']}`")
                if signal.get("tp3") and copier_settings["trades"] >= 3: tps_placed.append(f"🎯 TP3 : `{signal['tp3']}`")
                msg += "\n".join(tps_placed)
                msg += f"\n\n🔒 SL : `{signal['sl']}`\n━━━━━━━━━━━━━━━━━━━━\n✅ {nb_str} ordre(s) | {now}"
                send_telegram_notif(msg)
                for r in results:
                    active_copies[r["ticket"]] = {
                        "direction": signal["direction"],
                        "entry":     r.get("price", signal.get("entry", 0)),
                        "tp1":       signal["tp1"],
                        "tp2":       signal.get("tp2"),
                        "tp3":       signal.get("tp3"),
                        "sl":        signal["sl"],
                        "source":    source,
                        "tp_target": r["tp_target"],
                        "lot":       copier_settings["lot"],
                        "closed":    False,
                    }
            else:
                send_telegram_notif(f"⚠️ Signal [{source}] — MT5 a refusé les ordres.")
        else:
            emoji = "🟢" if signal["direction"] == "BUY" else "🔴"
            verb  = "J'ACHÈTE" if signal["direction"] == "BUY" else "JE VENDS"
            msg   = f"{emoji} *{verb} XAUUSD*\n📡 Source : `{source}`\n\n"
            msg  += f"🎯 TP1 : `{signal['tp1']}`\n"
            if signal.get("tp2"): msg += f"🎯 TP2 : `{signal['tp2']}`\n"
            if signal.get("tp3"): msg += f"🎯 TP3 : `{signal['tp3']}`\n"
            msg  += f"\n🔒 SL : `{signal['sl']}`\n━━━━━━━━━━━━━━━━━━━━\n⚠️ _Mode manuel_"
            send_telegram_notif(msg)

        stats["signals_reçus"] += 1

    except Exception as e:
        log.error(f"Erreur handle_message : {e}", exc_info=True)


# ════════════════════════════════════════════════════════════════════════════════
#  BOUCLE PRINCIPALE
# ════════════════════════════════════════════════════════════════════════════════

async def main():
    if not TELETHON_AVAILABLE:
        print("❌ pip install telethon")
        return

    # Vérifie la config
    if TELEGRAM_API_ID == 0 or not TELEGRAM_API_HASH or not TELEGRAM_PHONE:
        print("\n" + "═"*55)
        print("  ⚠️  CONFIGURATION REQUISE")
        print("═"*55)
        print("  1. Va sur https://my.telegram.org")
        print("  2. Connecte-toi avec ton numéro Telegram")
        print("  3. Clique 'API development tools'")
        print("  4. Crée une app (nom et description au choix)")
        print("  5. Copie 'App api_id' et 'App api_hash'")
        print("  6. Colle-les dans ce fichier :")
        print("     TELEGRAM_API_ID   = 12345678")
        print("     TELEGRAM_API_HASH = 'abc123...'")
        print("     TELEGRAM_PHONE    = '+33612345678'")
        print("═"*55)
        return

    # Connexion MT5
    connect_mt5()

    # Connexion Telegram User
    print(f"📱 Connexion Telegram avec {TELEGRAM_PHONE}...")
    print("   (Une session locale sera créée — tu ne devras le faire qu'une fois)")

    client = TelegramClient(SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)
    await client.start(phone=TELEGRAM_PHONE)

    me = await client.get_me()
    log.info(f"✅ Connecté en tant que {me.first_name} (@{me.username})")

    # ── Liste tous tes canaux si CHANNELS est vide ───────────────────────────
    if not CHANNELS or CHANNELS == ["stationx_signals", "fullmarginonly"]:
        print("\n" + "═"*55)
        print("  📋 VOS CANAUX/GROUPES TELEGRAM :")
        print("═"*55)
        async for dialog in client.iter_dialogs():
            if dialog.is_channel or dialog.is_group:
                print(f"  ID: {dialog.id}  →  {dialog.name}")
        print("═"*55)
        print("  Copie les IDs dans CHANNELS dans le fichier")
        print("  Ex: CHANNELS = [-1001234567890, -1009876543210]")
        print("═"*55)
        return

    # Résolution des canaux
    channel_entities = []
    for ch in CHANNELS:
        try:
            entity = await client.get_entity(ch)
            name   = getattr(entity, 'title', str(ch))
            channel_entities.append(entity)
            log.info(f"✅ Canal trouvé : [{name}] (ID: {entity.id})")
        except Exception as e:
            log.error(f"❌ Canal introuvable : {ch} — {e}")
            print(f"   ⚠️  Canal '{ch}' introuvable")

    if not channel_entities:
        print("❌ Aucun canal valide. Arrêt.")
        return

    # Écoute des messages
    @client.on(events.NewMessage(chats=channel_entities))
    async def on_message(event):
        await handle_message(event)

    mode_str = "🤖 AUTO" if AUTO_COPY else "👆 Manuel"
    send_telegram_notif(
        f"📡 *Signal Copier démarré !*\n"
        f"Canaux : {len(channel_entities)}\n"
        f"Mode : `{mode_str}`\n"
        f"Lot : `{LOT_PER_SIGNAL}` | Positions : `{NB_TRADES}`\n"
        f"Magic : `{MAGIC_NUMBER}`\n\n"
        f"En écoute..."
    )

    log.info(f"📡 Signal Copier actif — {len(channel_entities)} canal(ux) | Mode {mode_str}")

    # Surveillance des trades ouverts (TP/SL)
    async def monitor_copies():
        """
        Surveille les trades placés par le copier.
        Détecte quand MT5 ferme une position (TP ou SL atteint)
        et envoie une notification Telegram.
        """
        while True:
            try:
                if active_copies and MT5_AVAILABLE:
                    tickets_done = []
                    for ticket, trade in active_copies.items():
                        # Vérifie si la position est encore ouverte
                        positions = mt5.positions_get(ticket=ticket)
                        still_open = positions is not None and len(positions) > 0

                        if not still_open:
                            # Position fermée — TP ou SL ?
                            direction  = trade["direction"]
                            entry      = trade["entry"]
                            tp_target  = trade["tp_target"]
                            tp_price   = trade.get(f"tp{tp_target}") or trade["tp1"]
                            sl_price   = trade["sl"]
                            source     = trade["source"]

                            # Récupère le prix de clôture depuis l'historique MT5
                            try:
                                from datetime import timedelta
                                deals = mt5.history_deals_get(
                                    datetime.now() - timedelta(hours=24),
                                    datetime.now()
                                )
                                exit_price = None
                                if deals:
                                    for deal in reversed(deals):
                                        if deal.position_id == ticket:
                                            exit_price = deal.price
                                            break
                            except Exception:
                                exit_price = None

                            if exit_price is None:
                                tickets_done.append(ticket)
                                continue

                            # Détermine si c'est TP ou SL
                            if direction == "BUY":
                                is_tp = exit_price >= tp_price * 0.999
                            else:
                                is_tp = exit_price <= tp_price * 1.001

                            pips = round(abs(exit_price - entry), 2)

                            if is_tp:
                                stars = "🔥" * tp_target
                                send_telegram_notif(
                                    f"{stars} *TP{tp_target} TOUCHÉ !* +`{pips}` pts\n"
                                    f"📡 Source : `{source}`\n"
                                    f"Sortie : `{exit_price}`"
                                )
                                log.info(f"🎯 TP{tp_target} touché #{ticket} depuis [{source}] +{pips} pts")
                            else:
                                loss = round(abs(exit_price - entry), 2)
                                send_telegram_notif(
                                    f"🛑 *SL touché* — `-{loss}` pts\n"
                                    f"📡 Source : `{source}`\n"
                                    f"Sortie : `{exit_price}`"
                                )
                                log.info(f"🛑 SL touché #{ticket} depuis [{source}] -{loss} pts")

                            tickets_done.append(ticket)

                    # Retire les trades fermés
                    for t in tickets_done:
                        active_copies.pop(t, None)

            except Exception as e:
                log.error(f"Erreur monitor_copies : {e}")

            await asyncio.sleep(2)   # vérifie toutes les 2 secondes

    # Commandes via Telethon — écoute les messages privés sur TON compte
    # Pas de conflit avec bot.py car on utilise l'API User (Telethon), pas Bot API
    @client.on(events.NewMessage(func=lambda e: e.is_private or e.out))
    async def on_command(event):
        """Écoute les commandes envoyées en message privé sur ton compte Telegram."""
        text = event.message.text or ""
        if not text.startswith("/"):
            return

        if text.startswith("/lotcopier"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    new_lot = float(parts[1])
                    copier_settings["lot"] = new_lot
                    send_telegram_notif(
                        f"✅ *Copier — Lot changé : `{new_lot}`*\n"
                        f"_Appliqué dès le prochain signal_"
                    )
                    log.info(f"Copier lot → {new_lot}")
                except ValueError:
                    send_telegram_notif("❌ Usage : `/lotcopier 0.05`")
            else:
                send_telegram_notif(f"📊 Lot actuel : `{copier_settings['lot']}`\nUsage : `/lotcopier 0.05`")

        elif text.startswith("/tradescopier"):
            parts = text.split()
            if len(parts) == 2:
                try:
                    nb = int(parts[1])
                    copier_settings["trades"] = nb
                    tps = {1: "TP1 seulement", 2: "TP1+TP2", 3: "TP1+TP2+TP3"}
                    send_telegram_notif(
                        f"✅ *Copier — {nb} trade(s) par signal*\n_{tps[nb]}_"
                    )
                    log.info(f"Copier trades → {nb}")
                except ValueError:
                    send_telegram_notif("❌ Usage : `/tradescopier 1`, `2` ou `3`")
            else:
                send_telegram_notif(f"📊 Trades actuel : `{copier_settings['trades']}`\nUsage : `/tradescopier 2`")

        elif text.startswith("/statscopier"):
            # Lit le PnL réel depuis MT5 par magic number
            pnl_realise = 0.0
            wins = losses = 0
            if MT5_AVAILABLE:
                try:
                    from datetime import timedelta
                    from collections import defaultdict
                    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                    deals = mt5.history_deals_get(today, datetime.now())
                    if deals:
                        my_deals = [d for d in deals if d.magic == MAGIC_NUMBER and d.entry == 1]
                        sequences = defaultdict(float)
                        for d in my_deals:
                            sequences[d.position_id] += d.profit
                        for pos_id, pnl in sequences.items():
                            pnl_realise += pnl
                            if pnl >= 0: wins += 1
                            else:        losses += 1
                        pnl_realise = round(pnl_realise, 2)
                except Exception as e:
                    log.error(f"statscopier MT5 erreur : {e}")

            # PnL en cours (positions ouvertes par le copier)
            live_pnl = 0.0
            if MT5_AVAILABLE:
                try:
                    positions = mt5.positions_get(symbol=SYMBOL)
                    if positions:
                        live_pnl = round(sum(
                            p.profit for p in positions if p.magic == MAGIC_NUMBER
                        ), 2)
                except Exception:
                    pass

            total_pnl = round(pnl_realise + live_pnl, 2)
            pnl_sign  = "+" if total_pnl >= 0 else ""
            wr        = round(wins/(wins+losses)*100) if (wins+losses) > 0 else 0
            send_telegram_notif(
                f"📡 *Stats Signal Copier*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"PnL réalisé    : `{pnl_realise:+.2f}$`\n"
                f"PnL en cours   : `{live_pnl:+.2f}$`\n"
                f"PnL total      : `{pnl_sign}{total_pnl}$`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Séquences      : {wins+losses} ({wins}W / {losses}L)\n"
                f"Winrate        : `{wr}%`\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Signaux reçus  : `{stats['signals_reçus']}`\n"
                f"Trades placés  : `{stats['trades_placés']}`\n"
                f"Ignorés        : `{stats['trades_ignorés']}`\n"
                f"Lot actuel     : `{copier_settings['lot']}`\n"
                f"Trades/signal  : `{copier_settings['trades']}`\n"
                f"Magic          : `{MAGIC_NUMBER}`"
            )

    # Stats toutes les heures
    async def log_stats():
        while True:
            await asyncio.sleep(3600)
            log.info(
                f"📊 Stats : signaux={stats['signals_reçus']} "
                f"trades={stats['trades_placés']} "
                f"ignorés={stats['trades_ignorés']}"
            )

    asyncio.create_task(monitor_copies())
    asyncio.create_task(log_stats())
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
