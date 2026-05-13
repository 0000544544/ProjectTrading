# 🤖 Gold Scalping Bot - Context & Architecture

Ce fichier sert de mémoire technique pour l'agent Gemini CLI. Il récapitule l'architecture, les règles métier et les objectifs du projet.

## 📌 Vue d'ensemble
Le projet est un système de trading automatisé spécialisé sur l'or (**XAUUSD**) via **MetaTrader 5 (MT5)**. Il repose sur une analyse technique rigoureuse, combinant des indicateurs mathématiques et la détection de patterns de bougies en temps réel.

## 🛠 Architecture Technique
- **Langage** : Python 3.11+
- **Trading** : MetaTrader5 (bibliothèque Python)
- **Communication** : `python-telegram-bot` (Interface de contrôle) & `telethon` (User API pour le copier)
- **Data** : `pandas` et `numpy` pour le calcul des indicateurs (RSI, MACD, EMA 9/20/50/200, Bollinger, ATR, Stochastique).

## 🚀 Composants Clés

### 1. Bot de Scalping (`bot.py`)
- **Stratégie** : Scalping pur sur M1/M5.
- **Logique de Signal** : 
    - **Mode Rebond** : Détection de retournement sur support/résistance avec analyse du volume et de la force du mouvement précédent (2×ATR).
    - **Mode Normal** : Alignement EMA9, RSI et croisement Stochastique/MACD.
- **Gestion du Risque** : 3 modes (`safe`, `risque`, `risque+++`) ajustant les multiplicateurs d'ATR pour les TP/SL et le seuil de score minimal.
- **Magic Number** : `20250101`

### 2. Signal Copier (`signal_copier.py`)
- **Fonctionnement** : Analyse en temps réel les messages de canaux Telegram (ex: Station X).
- **Entrée Rapide** : Capacité à entrer "au marché" dès l'alerte "Gold Sell Now" avant même de recevoir les niveaux TP/SL, puis mise à jour automatique des niveaux une fois le signal complet reçu.
- **Magic Number** : `20250202` (évite les conflits avec le bot principal).

### 3. Contrôle Telegram
Le bot est entièrement pilotable par commandes :
- `/status` : État des trades et du marché.
- `/pnl` : Profit & Loss du jour (récupéré depuis MT5).
- `/be` : Passage immédiat au Breakeven (SL à l'entrée).
- `/closeall` : Fermeture d'urgence de toutes les positions.
- `/mode` : Changement de stratégie à la volée.

## 📏 Règles Métier & Sécurité
- **Symbole Unique** : XAUUSD uniquement.
- **Cooldown** : Temps d'attente obligatoire entre les trades pour éviter le "overtrading".
- **Filtre de Spread** : Blocage du trading si le spread est trop élevé (> MAX_SPREAD).
- **Gestion des Séquences** : Ouverture de 3 positions distinctes (TP1, TP2, TP3). Gestion dynamique du SL (SL au BE quand TP1 est touché, SL au TP1 quand TP2 est touché).

---
*Dernière mise à jour : 23 Avril 2026*
