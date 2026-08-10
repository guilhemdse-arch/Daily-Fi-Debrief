# Rapport quotidien de portefeuille (Telegram)

Envoie chaque jour de semaine, sur Telegram, un récapitulatif de tes actions/ETF :
cours, variation, volume, position dans le range 52 semaines, RSI, moyennes
mobiles, quelques ratios fondamentaux, les dernières actus et les prochains
résultats annoncés.

## Ce que le script fait — et ne fait pas

- Il **ne prédit pas** les mouvements de cours de demain : aucun outil ne le
  fait de façon fiable. Il te donne les éléments factuels qui permettent de
  te faire ta propre idée (tendance technique, actus récentes, résultats à
  venir).
- L'"agenda" couvre uniquement les dates de résultats des titres que tu
  suis (extraites de Yahoo Finance). Il n'inclut pas le calendrier macro
  général (Fed, BCE, CPI...) — c'est une évolution possible si tu veux
  l'ajouter plus tard, mais ça demande une source de données supplémentaire.
- Source de données : **Yahoo Finance** via la librairie `yfinance`. C'est
  une API non officielle : elle peut ponctuellement échouer ou être
  ralentie par Yahoo. Le script gère ça avec des tentatives automatiques
  et en isolant les erreurs par titre (un titre qui échoue n'empêche pas
  l'envoi du reste du rapport).

## 1. Créer le bot Telegram

1. Ouvre une conversation avec [@BotFather](https://t.me/BotFather) sur Telegram.
2. Envoie `/newbot` et suis les instructions. Tu récupères un **token**
   du type `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
3. Envoie n'importe quel message à ton nouveau bot (pour l'"activer").
4. Récupère ton **chat_id** : ouvre dans un navigateur
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` juste après avoir
   envoyé le message au bot, et repère le champ `"chat":{"id": ...}`.

## 2. Créer le dépôt GitHub

1. Crée un nouveau dépôt GitHub, **idéalement privé** (le fichier
   `tickers.json` liste la composition de ton portefeuille — sans les
   montants, mais autant rester discret).
2. Mets-y tous les fichiers de ce projet en conservant l'arborescence,
   notamment `.github/workflows/daily-report.yml`.
3. Dans **Settings → Secrets and variables → Actions**, ajoute deux
   secrets :
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

## 3. Personnaliser tes titres

Édite `tickers.json`. Format des tickers (celui de Yahoo Finance) :

| Marché              | Format          | Exemple      |
|----------------------|-----------------|--------------|
| US (Nasdaq/NYSE)     | sans suffixe    | `AAPL`       |
| Euronext Paris        | `.PA`           | `MC.PA`      |
| Xetra (Allemagne)    | `.DE`           | `SAP.DE`     |
| Londres               | `.L`            | `HSBA.L`     |

`"type": "etf"` masque le P/E et le beta (peu pertinents pour un ETF) et
garde le rendement du dividende s'il est disponible.

## 4. Tester

Dans l'onglet **Actions** du dépôt, sélectionne le workflow "Rapport
quotidien portefeuille" puis **Run workflow** pour un test immédiat sans
attendre l'horaire programmé.

## 5. Ajuster l'horaire

Le cron est réglé sur 20:15 UTC (~22:15 à Paris en été), du lundi au
vendredi, dans `.github/workflows/daily-report.yml`. Modifie la ligne
`cron:` si tu veux un autre horaire — [crontab.guru](https://crontab.guru)
aide à écrire l'expression.

## Limites connues / pistes d'amélioration

- **yfinance peut renvoyer une erreur ou des données manquantes** de temps
  en temps (limitation Yahoo, changement de leur site). Si ça devient
  fréquent, une piste est de basculer les tickers US vers une API
  officielle comme Finnhub (free tier généreux) et de garder yfinance
  uniquement pour les tickers européens.
- Les **horaires GitHub Actions ne sont pas garantis à la minute près**
  (retard possible en cas de forte charge côté GitHub).
- Le **rendement du dividende** peut occasionnellement être mal formaté
  par Yahoo (le script corrige les cas les plus courants, mais vérifie la
  cohérence du chiffre affiché).
