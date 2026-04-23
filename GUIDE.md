# 🤖 Guide d'installation — Gold Scalping Bot

## Ce que tu vas installer

| Fichier | Rôle |
|---|---|
| `bot.py` | Bot principal — analyse le marché et trade automatiquement |
| `config.py` | Tous tes paramètres (tokens, MT5, lot size...) |
| `backtest.py` | Test sur données historiques (optionnel) |
| `signal_copier.py` | Copie les signaux de canaux Telegram sur MT5 |

---

## ÉTAPE 1 — Installer Python

1. Va sur **python.org/downloads**
2. Télécharge **Python 3.11** ou plus récent
3. ⚠️ Coche bien **"Add Python to PATH"** pendant l'installation
4. Vérifie l'installation :
```
python --version
```
Tu dois voir `Python 3.11.x` ou similaire.

---

## ÉTAPE 2 — Installer les dépendances

Ouvre un terminal (CMD ou PowerShell) et colle cette commande :

```
pip install python-telegram-bot MetaTrader5 anthropic pandas numpy telethon requests
```
ou si la première ne fonctionne pas
```
python -m pip install python-telegram-bot MetaTrader5 anthropic pandas numpy telethon requests
```

Attends que tout s'installe (2-3 minutes).

---

## ÉTAPE 3 — Configurer config.py

Ouvre `config.py` et remplis les valeurs :

### Token Telegram (déjà rempli dans ton fichier)
```python
TELEGRAM_TOKEN   = "ton_token"
TELEGRAM_CHAT_ID = ton_id
```

### Clé Anthropic (déjà remplie)
```python
ANTHROPIC_API_KEY = "sk-ant-..."
```

### MT5
```python
MT5_LOGIN    = 10010434561      # ton numéro de compte
MT5_PASSWORD = "ton_mot_de_passe"
MT5_SERVER   = "MetaQuotes-Demo"  # ou le nom de ton broker
```

### Paramètres trading
```python
LOT_SIZE             = 0.01   # commence toujours avec 0.01 en démo
NB_TRADES_PER_SIGNAL = 3      # 3 positions par signal (TP1+TP2+TP3)
AUTO_TRADE           = True   # True = automatique / False = tu valides
```

---

## ÉTAPE 4 — Lancer le bot principal

1. Place tous les fichiers dans le même dossier (ex: `C:\GoldBot\`)
2. Ouvre un terminal dans ce dossier
3. Lance :
```
python bot.py
```

Tu devrais voir dans le terminal :
```
✅ Connecté à MetaTrader5
⚡ SCALPING BOT DÉMARRÉ — Scan toutes les secondes
📊 Bougies rechargées — M1:200 | Prix:4793.5 | Spread:2.1
```

Et sur Telegram ton bot t'envoie :
```
⚡ Gold Scalping Bot démarré !
```

---

## ÉTAPE 5 — Commandes Telegram disponibles

Une fois lancé, tu peux tout contrôler depuis Telegram :

### 📊 Infos
| Commande | Action |
|---|---|
| `/start` | Voir toutes les commandes |
| `/status` | État du bot + trades ouverts |
| `/prix` | Prix actuel XAUUSD |
| `/pnl` | PnL du jour |
| `/stats` | Dashboard complet (winrate, série, historique) |
| `/trades` | Détail des positions ouvertes |

### 🎮 Contrôle trades
| Commande | Action |
|---|---|
| `/be` | SL → Breakeven sur tous les trades |
| `/closeall` | Fermer TOUS les trades (demande confirmation) |
| `/closetrade 123456` | Fermer un trade précis par ticket |
| `/settrades 2` | Changer le nb de positions par signal (1/2/3) |
| `/setlot 0.05` | Changer la taille du lot |

### ⚙️ Paramètres
| Commande | Action |
|---|---|
| `/safe` | 🛡️ Mode Safe (SL large, peu de trades) |
| `/risque` | ⚡ Mode Risqué (équilibré, défaut) |
| `/risqueppp` | 🔥 Mode Risqué +++ (SL serré, beaucoup de trades) |
| `/mode` | Voir le mode actuel + changer via boutons |
| `/auto` | Trades automatiques |
| `/manuel` | Tu valides chaque signal |
| `/pause` | Mettre en pause (trades ouverts continuent) |
| `/resume` | Reprendre |
| `/reset` | Remettre les stats à zéro |

---

## ÉTAPE 6 — Signal Copier (optionnel)

Le signal copier écoute des canaux Telegram (Station X, Full Margin Only...) et copie leurs signaux sur MT5 automatiquement, en parallèle du bot principal.

### 6a. Obtenir les clés API Telegram

1. Va sur **my.telegram.org**
2. Connecte-toi avec ton numéro de téléphone
3. Clique **"API development tools"**
4. Remplis le formulaire (nom et description au choix)
5. Copie **`App api_id`** (nombre) et **`App api_hash`** (chaîne de caractères)

### 6b. Configurer signal_copier.py

Ouvre `signal_copier.py` et remplis :

```python
TELEGRAM_API_ID   = 12345678           # ton api_id (nombre)
TELEGRAM_API_HASH = "abcdef123456..."  # ton api_hash (texte)
TELEGRAM_PHONE    = "+33612345678"     # ton numéro avec indicatif
```

Laisse `CHANNELS = []` pour l'instant.

### 6c. Premier lancement — trouver les IDs des canaux

Lance le signal copier une première fois :
```
python signal_copier.py
```

Il te demande ton code SMS (envoyé par Telegram) — entre-le.

Ensuite il affiche tous tes canaux avec leurs IDs :
```
═══════════════════════════════════════════════
  📋 VOS CANAUX/GROUPES TELEGRAM :
═══════════════════════════════════════════════
  ID: -1001234567890  →  Station X
  ID: -1009876543210  →  Full Margin Only
  ID: -1001111111111  →  Mon autre canal
═══════════════════════════════════════════════
```

### 6d. Ajouter les IDs dans le fichier

Copie les IDs des canaux que tu veux écouter :
```python
CHANNELS = [-1001234567890, -1009876543210]
```

### 6e. Lancer en parallèle

Ouvre **deux terminaux** :
- Terminal 1 : `python bot.py`
- Terminal 2 : `python signal_copier.py`

Les deux tournent indépendamment. Le bot principal utilise le magic `20250101`, le signal copier utilise `20250202` — aucun conflit.

---

## Problèmes fréquents

### "MT5 initialize() échoué"
→ MT5 doit être ouvert et connecté sur ton PC avant de lancer le bot

### "Aucune bougie M1 retournée"
→ Dans MT5, vérifie que XAUUSD M1 est bien visible dans les graphiques (MT5 ne charge que les symboles ouverts)

### "Telegram bot ne répond pas"
→ Vérifie le TELEGRAM_TOKEN et TELEGRAM_CHAT_ID dans config.py

### "pip n'est pas reconnu"
→ Réinstalle Python en cochant "Add Python to PATH"

### Le bot tourne mais 0 trade
→ Tape `/status` sur Telegram et envoie le fichier `bot.log`

---

## Structure des fichiers

```
GoldBot/
├── bot.py                    ← Bot principal (lancer en premier)
├── config.py                 ← ⚠️ Ta config (ne jamais partager)
├── backtest.py               ← Tests historiques (optionnel)
├── signal_copier.py          ← Copie signaux Telegram (optionnel)
├── bot.log                   ← Logs du bot (créé automatiquement)
└── signal_copier.log         ← Logs du copier (créé automatiquement)
```

---

## Conseils avant de passer en réel

1. **Teste toujours en démo d'abord** — MetaQuotes-Demo dans config.py
2. **Commence avec lot 0.01** — les gains sont petits mais les pertes aussi
3. **Regarde les logs** — `bot.log` te dit exactement ce que fait le bot
4. **Surveille le spread** — XAUUSD spread > 5 pips = évite de trader
5. **Garde `/pause` à portée** — si tu vois quelque chose de bizarre, pause immédiat

---

*Gold Scalping Bot — XAUUSD M1 — Version avec Signal Copier*
