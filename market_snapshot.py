"""
Instantané des marchés pour les briefs et revues de Claude.

Récupère via yfinance les cours de clôture du portefeuille, des indices, taux,
devises, matières premières et cryptos, calcule quelques indicateurs techniques
et écrit :
  - docs/market.json      : données complètes (lu par les tâches planifiées Claude)
  - docs/market-latest.md : résumé lisible (tableau)

Aucune quantité ni montant personnel n'est écrit : le dépôt est public.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(BASE_DIR, "docs", "market.json")
OUT_MD = os.path.join(BASE_DIR, "docs", "market-latest.md")
PARIS = ZoneInfo("Europe/Paris")
HISTORY_DAYS = 260          # ~1 an de séances conservées par symbole
STALE_AFTER_DAYS = 4        # une clôture plus vieille que ça est signalée

# clé lisible -> (symboles Yahoo par ordre de préférence, nom, catégorie)
UNIVERSE: dict[str, tuple[list[str], str, str]] = {
    # Portefeuille
    "VINCI": (["DG.PA"], "VINCI", "portefeuille"),
    "ASML": (["ASML.AS"], "ASML", "portefeuille"),
    "SAP": (["SAP.DE"], "SAP", "portefeuille"),
    "LDC": (["LOUP.PA"], "LDC", "portefeuille"),
    "MSFT": (["MSFT"], "Microsoft", "portefeuille"),
    "META": (["META"], "Meta Platforms", "portefeuille"),
    "GOOG": (["GOOG"], "Alphabet", "portefeuille"),
    "NVDA": (["NVDA"], "Nvidia", "portefeuille"),
    "NOVO": (["NOVO-B.CO", "NOVA.DE"], "Novo Nordisk", "portefeuille"),
    "EUNL": (["EUNL.DE"], "iShares Core MSCI World", "portefeuille"),
    "SXR8": (["SXR8.DE"], "iShares Core S&P 500 (cap.)", "portefeuille"),
    "IUSA": (["IUSA.AS", "IUSA.L"], "iShares Core S&P 500 (dist.)", "portefeuille"),
    "EXW1": (["EXW1.DE"], "iShares Core Euro Stoxx 50 (dist.)", "portefeuille"),
    "EUN2": (["EUN2.DE"], "iShares Core Euro Stoxx 50 (cap.)", "portefeuille"),
    "PAEEM": (["PAEEM.PA"], "Amundi PEA Émergents", "portefeuille"),
    # Indices
    "CAC40": (["^FCHI"], "CAC 40", "indice"),
    "EUROSTOXX50": (["^STOXX50E"], "Euro Stoxx 50", "indice"),
    "DAX": (["^GDAXI"], "DAX", "indice"),
    "SP500": (["^GSPC"], "S&P 500", "indice"),
    "NASDAQ": (["^IXIC"], "Nasdaq Composite", "indice"),
    "MSCI_WORLD_USD": (["URTH"], "MSCI World (ETF URTH, USD)", "indice"),
    "VIX": (["^VIX"], "VIX", "indice"),
    # Futures
    "SP500_FUT": (["ES=F"], "Future S&P 500", "future"),
    "NASDAQ_FUT": (["NQ=F"], "Future Nasdaq 100", "future"),
    # Taux
    "US10Y": (["^TNX"], "Taux US 10 ans (%)", "taux"),
    # Devises (cotation : 1 EUR = x devise)
    "EURUSD": (["EURUSD=X"], "EUR/USD", "devise"),
    "EURDKK": (["EURDKK=X"], "EUR/DKK", "devise"),
    "EURGBP": (["EURGBP=X"], "EUR/GBP", "devise"),
    # Matières premières
    "BRENT": (["BZ=F"], "Pétrole Brent ($)", "matiere"),
    "OR": (["GC=F"], "Or ($/once)", "matiere"),
    "BLE": (["ZW=F"], "Blé CBOT (cents/boisseau)", "matiere"),
    "MAIS": (["ZC=F"], "Maïs CBOT (cents/boisseau)", "matiere"),
    # Crypto
    "BTC": (["BTC-EUR"], "Bitcoin (€)", "crypto"),
    "ETH": (["ETH-EUR"], "Ethereum (€)", "crypto"),
}


def retry(fn, *args, attempts=3, delay=3, **kwargs):
    last = None
    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(delay * (i + 1))
    raise last


def num(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(x) or math.isinf(x) else round(x, nd)


def rsi14(close: pd.Series) -> float | None:
    if len(close) < 15:
        return None
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] else float("inf")
    return num(100 - 100 / (1 + rs), 1)


def perf_since(close: pd.Series, days: int) -> float | None:
    if close.empty:
        return None
    target = close.index[-1] - timedelta(days=days)
    past = close[close.index <= target]
    if past.empty:
        return None
    return num((close.iloc[-1] / past.iloc[-1] - 1) * 100, 2)


def session_close(sym: str):
    """Heure de clôture locale du marché d'un symbole, ou None s'il cote 24 h/24."""
    if sym.endswith(("=X", "=F", "-EUR", "-USD")):
        return None
    if sym.endswith((".PA", ".AS", ".DE")) or sym in ("^FCHI", "^STOXX50E", "^GDAXI"):
        return ZoneInfo("Europe/Paris"), (17, 40)
    if sym.endswith(".CO"):
        return ZoneInfo("Europe/Copenhagen"), (17, 5)
    if sym.endswith(".L"):
        return ZoneInfo("Europe/London"), (16, 40)
    return ZoneInfo("America/New_York"), (16, 5)


def is_provisional(sym: str, last_date, now) -> bool:
    """True si la dernière ligne est celle d'une séance pas encore clôturée."""
    sc = session_close(sym)
    if sc is None:
        return last_date >= now.date()
    tz, (h, m) = sc
    local = now.astimezone(tz)
    return last_date == local.date() and (local.hour, local.minute) < (h, m)


def fetch_history(symbols: list[str]):
    """Renvoie (symbole retenu, historique nettoyé, devise) pour le premier symbole qui répond."""
    errors = []
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            hist = retry(t.history, period="2y", interval="1d", auto_adjust=False)
            if hist is None or hist.empty:
                raise ValueError("historique vide")
            # Point clé : on retire les lignes sans clôture (séance en cours, données partielles)
            hist = hist.dropna(subset=["Close"])
            hist = hist[hist["Close"] > 0]
            if len(hist) < 2:
                raise ValueError("moins de 2 clôtures valides")
            meta = getattr(t, "history_metadata", None) or {}
            currency = meta.get("currency") or ""
            return sym, hist, currency, t
        except Exception as e:  # noqa: BLE001
            errors.append(f"{sym}: {e}")
    raise RuntimeError(" ; ".join(errors))


def fundamentals(t) -> dict:
    try:
        info = retry(t.get_info, attempts=2)
    except Exception:  # noqa: BLE001
        return {}
    keys = {
        "trailingPE": "per", "forwardPE": "per_forward", "priceToBook": "price_to_book",
        "dividendYield": "rendement_dividende", "marketCap": "capitalisation",
        "targetMeanPrice": "objectif_moyen_analystes", "numberOfAnalystOpinions": "nb_analystes",
        "recommendationKey": "consensus", "returnOnEquity": "roe", "operatingMargins": "marge_operationnelle",
        "revenueGrowth": "croissance_ca", "earningsGrowth": "croissance_bpa", "freeCashflow": "free_cash_flow",
        "debtToEquity": "dette_sur_fonds_propres",
    }
    out = {}
    for k, v in keys.items():
        val = info.get(k)
        out[v] = val if isinstance(val, str) else num(val, 4)
    ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
    if ts:
        out["prochains_resultats"] = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    return out


def to_eur(value, currency, fx):
    if value is None:
        return None
    c = (currency or "").upper()
    if c == "EUR":
        return value
    if currency == "GBp":  # pence
        return value / 100 / fx["GBP"] if fx.get("GBP") else None
    rate = fx.get(c)
    return value / rate if rate else None


def build():
    now = datetime.now(timezone.utc)
    today = now.date()
    data, history, errors, raw = {}, {}, {}, {}

    for key, (symbols, name, cat) in UNIVERSE.items():
        try:
            sym, hist, currency, t = fetch_history(symbols)
            raw[key] = (sym, hist, currency, t, name, cat)
        except Exception as e:  # noqa: BLE001
            errors[key] = str(e)

    def last(key):
        return float(raw[key][1]["Close"].iloc[-1]) if key in raw else None

    fx = {"EUR": 1.0, "USD": last("EURUSD"), "DKK": last("EURDKK"), "GBP": last("EURGBP")}

    for key, (sym, hist, currency, t, name, cat) in raw.items():
        close = hist["Close"]
        close.index = pd.to_datetime(close.index).tz_localize(None) if close.index.tz is not None else pd.to_datetime(close.index)
        last_date = close.index[-1].date()
        c, p = float(close.iloc[-1]), float(close.iloc[-2])
        one_year = close[close.index >= close.index[-1] - timedelta(days=365)]
        ytd = close[close.index.year == close.index[-1].year]
        prev_year = close[close.index.year < close.index[-1].year]
        row = {
            "nom": name, "categorie": cat, "symbole": sym, "devise": currency,
            "date": last_date.isoformat(),
            "cloture": num(c), "cloture_precedente": num(p),
            "variation_jour_pct": num((c / p - 1) * 100, 2),
            "cloture_eur": num(to_eur(c, currency, fx)) if cat == "portefeuille" else None,
            "plus_haut_52s": num(one_year.max()), "plus_bas_52s": num(one_year.min()),
            "ecart_plus_haut_pct": num((c / one_year.max() - 1) * 100, 2),
            "mm20": num(close.rolling(20).mean().iloc[-1]) if len(close) >= 20 else None,
            "mm50": num(close.rolling(50).mean().iloc[-1]) if len(close) >= 50 else None,
            "mm200": num(close.rolling(200).mean().iloc[-1]) if len(close) >= 200 else None,
            "rsi14": rsi14(close),
            "perf_1s_pct": perf_since(close, 7), "perf_1m_pct": perf_since(close, 30),
            "perf_3m_pct": perf_since(close, 91), "perf_1a_pct": perf_since(close, 365),
            "perf_ytd_pct": num((c / float(prev_year.iloc[-1]) - 1) * 100, 2) if not prev_year.empty else None,
            "perime": (today - last_date).days > STALE_AFTER_DAYS,
            "provisoire": is_provisional(sym, last_date, now),
        }
        if row["mm50"] and row["mm200"]:
            row["tendance"] = "haussière" if row["mm50"] > row["mm200"] else "baissière"
        if cat == "portefeuille" and key not in {"EUNL", "SXR8", "IUSA", "EXW1", "EUN2", "PAEEM"}:
            row["fondamentaux"] = fundamentals(t)
        data[key] = row
        tail = close.iloc[-HISTORY_DAYS:]
        history[key] = [[d.date().isoformat(), num(v)] for d, v in tail.items()]

    out = {
        "generated_at": now.isoformat(timespec="seconds"),
        "generated_at_paris": now.astimezone(PARIS).strftime("%Y-%m-%d %H:%M"),
        "source": "Yahoo Finance via yfinance (clôtures quotidiennes ; lignes incomplètes retirées)",
        "notes": {
            "devises": "EURUSD, EURDKK, EURGBP = nombre d'unités de devise pour 1 euro ; cloture_eur = cloture / taux",
            "US10Y": "rendement en %",
            "perime": "true si la dernière clôture date de plus de %d jours" % STALE_AFTER_DAYS,
            "provisoire": "true si la séance n'est pas encore clôturée : cloture = dernier cours, variation intraday",
        },
        "fx": {k: num(v, 6) for k, v in fx.items()},
        "donnees": data,
        "historique": history,
        "erreurs": errors,
    }
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    def fmt(v, nd=2):
        return "—" if v is None else f"{v:,.{nd}f}".replace(",", " ").replace(".", ",")

    lines = [f"# Marchés — instantané du {out['generated_at_paris']} (heure de Paris)", "",
             "| Clé | Nom | Date | Clôture | Devise | Var. jour | En € | Perf 1 m | RSI 14 |",
             "|---|---|---|---:|---|---:|---:|---:|---:|"]
    for k, r in data.items():
        lines.append(f"| {k} | {r['nom']} | {r['date']}{' ⚠️ périmé' if r['perime'] else ''}{' ⏳ en séance' if r['provisoire'] else ''} | {fmt(r['cloture'])} | {r['devise']} | "
                     f"{fmt(r['variation_jour_pct'])} % | {fmt(r['cloture_eur'])} | {fmt(r['perf_1m_pct'])} % | {fmt(r['rsi14'], 1)} |")
    if errors:
        lines += ["", "## Erreurs", *[f"- {k} : {v}" for k, v in errors.items()]]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"{len(data)} symboles écrits, {len(errors)} erreurs")
    if len(data) < len(UNIVERSE) / 2:
        raise SystemExit("Trop d'échecs : vérifier yfinance")


if __name__ == "__main__":
    build()
