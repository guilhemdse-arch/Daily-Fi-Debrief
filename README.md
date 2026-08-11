# Rapport quotidien de portefeuille (Telegram + dashboard)

Envoie chaque jour de semaine, sur Telegram, un récapitulatif de tes actions/ETF
avec un **score et un signal Conserver / Surveiller / Vendre** inspiré de la
méthode de William Higgons, et publie un **dashboard web** (GitHub Pages)
avec l'historique de chaque titre.

## Ce que le script fait — et ne fait pas

- Il **ne prédit pas** les mouvements de cours de demain. Le score donne les
  éléments factuels (décote, qualité, momentum) qui permettent de te faire
  ta propre idée.
- **Le score est une approximation**, pas une réplication exacte de la
  méthode de W. Higgons : les seuils utilisés (PER, P/B, P/CF, ROE, marge,
  ROCE, dette, momentum vs indice) sont inspirés des ordres de grandeur
  qu'il cite en interview, mais son vrai modèle compare les titres entre eux
  au sein d'un large univers de valeurs, ce que ce script ne fait pas — il
  ne regarde que tes titres, indépendamment les uns des autres.
- Certains ratios (P/CF, ROCE) demandent d'extraire le bilan et le compte de
  résultat depuis Yahoo Finance : ils sont "best-effort" et peuvent manquer
  pour certains titres (notamment les petites valeurs) sans faire planter
  le calcul — le score se recalcule alors sur ce qui est disponible.
- Le rapport Telegram met en avant les **changements de signal** du jour en
  premier, pour que tu voies immédiatement ce qui a évolué.

## 1. Créer le bot Telegram

1. Ouvre une conversation avec [@BotFather](https://t.me/BotFather) sur Telegram.
2. Envoie `/newbot` et suis les instructions pour récupérer un **token**.
3. Envoie n'importe quel message à ton nouveau bot.
4. Récupère ton **chat_id** en ouvrant, juste après :
   `https://api.telegram.org/bot<TON_TOKEN>/getUpdates` — repère `"chat":{"id": ...}`.

## 2. Créer le dépôt GitHub

1. Crée un dépôt GitHub, **idéalement privé** (la composition de ton
   portefeuille y sera visible, sans les montants).
2. Mets-y tous les fichiers de ce projet en conservant l'arborescence
   (`.github/workflows/`, `docs/`).
3. Dans **Settings → Secrets and variables → Actions**, ajoute :
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_CHAT_ID`

## 3. Activer le dashboard (GitHub Pages)

1. Dans **Settings → Pages**, choisis **Source: Deploy from a branch**,
   branche `main`, dossier `/docs`. Sauvegarde.
2. GitHub te donne une URL du type `https://<ton-user>.github.io/<repo>/`.
   C'est ton dashboard — il affichera "aucune donnée" tant que le workflow
   n'a pas encore tourné une première fois.
3. Le fichier `docs/data.json` est committé automatiquement par le workflow
   à chaque run (c'est à la fois la mémoire du script et la source de
   données du dashboard) — tu n'as rien à faire manuellement.

Si le commit automatique échoue avec une erreur de permission : va dans
**Settings → Actions → General → Workflow permissions** et sélectionne
**Read and write permissions**.

## 4. Personnaliser tes titres

Édite `tickers.json`. Format des tickers (celui de Yahoo Finance) :

| Marché              | Format          | Exemple      |
|----------------------|-----------------|--------------|
| US (Nasdaq/NYSE)     | sans suffixe    | `AAPL`       |
| Euronext Paris        | `.PA`           | `MC.PA`      |
| Xetra (Allemagne)    | `.DE`           | `SAP.DE`     |
| Londres               | `.L`            | `HSBA.L`     |

`"type": "etf"` masque le P/E, le beta, le P/CF et le ROCE (peu pertinents
pour un ETF) et garde le rendement du dividende s'il est disponible.

## 5. Tester

Onglet **Actions** → workflow "Rapport quotidien portefeuille" → **Run
workflow**, pour un test immédiat. Vérifie ensuite que `docs/data.json` a
bien été mis à jour (un nouveau commit doit apparaître) et que le dashboard
affiche tes titres.

## Comment lire le score et le signal

- **Score 0-100** = moyenne de deux sous-scores : décote (PER, P/B, P/CF) et
  qualité (ROE, marge opérationnelle, dette, ROCE). Si une donnée manque, le
  score se recalcule sur les critères disponibles plutôt que de pénaliser
  le titre.
- **🔴 Vendre** : la décote a disparu (valorisation élevée) ou le score
  global est faible.
- **🟠 Surveiller** : au moins un signal d'alerte (chiffre d'affaires en
  baisse, ou sous-performance de plus de 15 points vs l'indice de référence
  du titre sur 6 mois, sans que la valorisation soit devenue chère).
- **🟢 Conserver / Renforcer** : score ≥ 65 sans aucun signal d'alerte.
- **🟢 Conserver** : le reste des cas favorables.

L'indice de référence utilisé pour le momentum dépend du marché du titre
(CAC 40 pour `.PA`, DAX pour `.DE`, FTSE 100 pour `.L`, S&P 500 par défaut).

## Structure du projet

```
daily_report.py                    # script principal
tickers.json                       # tes titres à suivre
requirements.txt
.github/workflows/daily-report.yml # cron + commit automatique
docs/index.html                    # dashboard (page GitHub Pages)
docs/data.json                     # historique + données du dashboard (généré/mis à jour automatiquement)
```

## Limites connues

- yfinance peut ponctuellement échouer ou renvoyer des données manquantes
  (limitation Yahoo). Le script gère ça avec des tentatives automatiques et
  en isolant les erreurs par titre.
- Les horaires GitHub Actions ne sont pas garantis à la minute près.
- Le rendement du dividende et le ratio dette/fonds propres peuvent être
  mal formatés par Yahoo selon les titres — le script applique une
  correction pour les cas les plus courants, mais vérifie la cohérence des
  chiffres affichés de temps en temps.
- Le score ne remplace pas l'analyse qualitative que W. Higgons dit
  lui-même appliquer en complément (comprendre le business, éviter les
  "value traps") — à traiter comme une aide à la décision.
