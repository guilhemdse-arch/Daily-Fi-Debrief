#!/usr/bin/env python3
"""
Rapport quotidien de portefeuille (actions & ETF) envoyé sur Telegram,
avec un score de type "Higgons" (décote + qualité) et un signal
Conserver / Surveiller / Vendre par titre.

Source de données : Yahoo Finance via yfinance (gratuite, non officielle,
couvre aussi bien les tickers US qu'européens). Le script est écrit pour
être tolérant aux pannes : un titre ou une donnée manquante n'empêche
jamais l'envoi du reste du rapport.

Le fichier docs/data.json sert à la fois :
  - de mémoire d'un run à l'autre (pour détecter les changements de signal)
  - de source de données pour le dashboard GitHub Pages (docs/index.html)
Il est donc committé dans le repo à la fin de chaque run par le workflow.

IMPORTANT : les seuils de score ci-dessous sont une approximation inspirée
des critères que William Higgons décrit dans ses interviews publiques
(PER, cash-flow, ROE, ROCE, marge, momentum). Ce n'est pas une réplication
exacte de son modèle propriétaire (notamment il compare les titres entre
eux au sein d'un univers large, ce que ce script ne fait pas ici) : à
prendre comme aide à la décision, pas comme signal automatique fiable à 100%.
"""

import os
import sys
import json
import time
import html
import math
from datetime import datetime

import requests
import yfinance as yf
import pandas as pd

BASE_DIR = os.path.dirname(__file__)
TICKERS_FILE = os.path.join(BASE_DIR, "tickers.json")
HISTORY_FILE = os.path.join(BASE_DIR, "docs", "data.json")

HISTORY_PERIOD = "1y"
RSI_PERIOD = 14
MAX_RETRIES = 3
RETRY_DELAY_SEC = 3
NEWS_ITEMS_PER_TICKER = 2
EARNINGS_LOOKAHEAD_DAYS = 14
TELEGRAM_MAX_LEN = 3800
MAX_HISTORY_ENTRIES = 180  # ~ 9 mois de jours ouvrés conservés dans data.json

# --- Barèmes de score (0-100 par sous-critère), seuils inspirés des ordres de
# grandeur cités par W. Higgons en interview (cf. docstring ci-dessus) ---
PER_TIERS = [(10, 100), (12, 80), (15, 50), (20, 20)]          # PER : plus bas = mieux
PB_TIERS = [(1, 100), (2, 70), (3, 40)]                          # Cours/actif net
PCF_TIERS = [(8, 100), (12, 70), (18, 40)]                       # Cours/cash-flow
ROE_TIERS = [(20, 100), (15, 80), (10, 50), (5, 25)]             # ROE : plus haut = mieux
MARGIN_TIERS = [(15, 100), (10, 70), (5, 40)]                    # Marge opérationnelle
ROCE_TIERS = [(15, 100), (10, 70), (5, 40)]                      # ROCE
DEBT_TIERS = [(0.3, 100), (0.6, 70), (1.0, 40)]                  # Dette nette/fonds propres : plus bas = mieux

MOMENTUM_LOOKBACK_DAYS = 126  # ~ 6 mois de bourse
MOMENTUM_UNDERPERF_THRESHOLD = -15.0  # points de perf relative vs indice -> flag

BENCHMARK_BY_SUFFIX = {
    ".PA": "^FCHI", ".DE": "^GDAXI", ".L": "^FTSE",
    ".AS": "^AEX", ".BR": "^BFX", ".MI": "FTSEMIB.MI",
}
DEFAULT_BENCHMARK = "^GSPC"


# --------------------------------------------------------------------------
# Utilitaires génériques
# --------------------------------------------------------------------------

def load_tickers():
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def retry(fn, *args, **kwargs):
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
    raise last_err


def fmt_num(value, decimals=2, suffix=""):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/d"
    return f"{value:,.{decimals}f}{suffix}".replace(",", " ")


def is_valid_number(value):
    return value is not None and not (isinstance(value, float) and pd.isna(value))


# --------------------------------------------------------------------------
# Indicateurs techniques
# --------------------------------------------------------------------------

def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else float("nan")


def get_benchmark_symbol(symbol: str) -> str:
    for suffix, bench in BENCHMARK_BY_SUFFIX.items():
        if symbol.endswith(suffix):
            return bench
    return DEFAULT_BENCHMARK


def compute_relative_momentum(hist: pd.DataFrame, symbol: str, benchmark_cache: dict):
    """Performance du titre sur ~6 mois, moins celle de l'indice de référence associé
    (choisi selon le suffixe du ticker). Renvoie None si l'historique est trop court."""
    if len(hist) <= MOMENTUM_LOOKBACK_DAYS:
        return None

    ticker_return = (hist["Close"].iloc[-1] / hist["Close"].iloc[-MOMENTUM_LOOKBACK_DAYS] - 1) * 100

    bench_symbol = get_benchmark_symbol(symbol)
    if bench_symbol not in benchmark_cache:
        try:
            bench_hist = retry(yf.Ticker(bench_symbol).history, period=HISTORY_PERIOD, interval="1d")
            benchmark_cache[bench_symbol] = bench_hist
        except Exception:
            benchmark_cache[bench_symbol] = None

    bench_hist = benchmark_cache.get(bench_symbol)
    if bench_hist is None or bench_hist.empty or len(bench_hist) <= MOMENTUM_LOOKBACK_DAYS:
        return None

    bench_return = (bench_hist["Close"].iloc[-1] / bench_hist["Close"].iloc[-MOMENTUM_LOOKBACK_DAYS] - 1) * 100
    return ticker_return - bench_return


# --------------------------------------------------------------------------
# Fondamentaux additionnels (P/CF, ROCE) - extraits des états financiers
# --------------------------------------------------------------------------

def _find_row(df, candidates):
    """Les libellés des états financiers yfinance varient selon les titres/versions :
    on essaie plusieurs libellés candidats et on prend le premier trouvé."""
    if df is None or df.empty:
        return None
    for label in candidates:
        if label in df.index:
            return df.loc[label]
    return None


def compute_pcf_and_roce(ticker, market_cap):
    """Best-effort : renvoie (None, None) si les états financiers sont indisponibles
    ou incomplets pour ce titre, plutôt que de lever une exception."""
    pcf, roce = None, None

    try:
        cashflow = ticker.get_cashflow()
        op_cf_row = _find_row(cashflow, ["Operating Cash Flow", "Total Cash From Operating Activities"])
        if op_cf_row is not None and market_cap:
            op_cf = op_cf_row.iloc[0]
            if is_valid_number(op_cf) and op_cf > 0:
                pcf = market_cap / op_cf
    except Exception:
        pass

    try:
        financials = ticker.get_financials()
        balance = ticker.get_balance_sheet()
        ebit_row = _find_row(financials, ["EBIT", "Operating Income"])
        assets_row = _find_row(balance, ["Total Assets"])
        curr_liab_row = _find_row(balance, ["Current Liabilities", "Total Current Liabilities"])
        if ebit_row is not None and assets_row is not None and curr_liab_row is not None:
            ebit = ebit_row.iloc[0]
            capital_employed = assets_row.iloc[0] - curr_liab_row.iloc[0]
            if is_valid_number(capital_employed) and capital_employed > 0:
                roce = ebit / capital_employed * 100
    except Exception:
        pass

    return pcf, roce


# --------------------------------------------------------------------------
# Récupération des données d'un titre
# --------------------------------------------------------------------------

def fetch_ticker_report(entry: dict, benchmark_cache: dict) -> dict:
    symbol = entry["symbol"]
    name = entry.get("name", symbol)
    is_etf = entry.get("type", "stock").lower() == "etf"

    ticker = yf.Ticker(symbol)

    hist = retry(ticker.history, period=HISTORY_PERIOD, interval="1d")
    if hist is None or hist.empty or len(hist) < 2:
        raise ValueError(f"Historique indisponible pour {symbol}")

    last_close = hist["Close"].iloc[-1]
    prev_close = hist["Close"].iloc[-2]
    day_change_pct = (last_close - prev_close) / prev_close * 100

    last_volume = hist["Volume"].iloc[-1]
    avg_volume_20d = hist["Volume"].iloc[-21:-1].mean() if len(hist) >= 21 else float("nan")

    week52_high = hist["High"].max()
    week52_low = hist["Low"].min()
    pct_from_high = (last_close - week52_high) / week52_high * 100

    rsi = compute_rsi(hist["Close"])
    sma50 = hist["Close"].rolling(50).mean().iloc[-1] if len(hist) >= 50 else float("nan")
    sma200 = hist["Close"].rolling(200).mean().iloc[-1] if len(hist) >= 200 else float("nan")

    trend = None
    if not pd.isna(sma50) and not pd.isna(sma200):
        if sma50 > sma200:
            trend = "haussière (SMA50 au-dessus de la SMA200)"
        else:
            trend = "baissière (SMA50 en dessous de la SMA200)"

    relative_momentum_pct = compute_relative_momentum(hist, symbol, benchmark_cache)

    # Fondamentaux : best-effort, pas toujours disponibles selon le titre/l'ETF
    info = {}
    try:
        info = retry(ticker.get_info)
    except Exception:
        pass

    pe_ratio = info.get("trailingPE") if not is_etf else None
    price_to_book = info.get("priceToBook")
    dividend_yield = info.get("dividendYield")
    beta = info.get("beta") if not is_etf else None
    market_cap = info.get("marketCap")

    roe_pct = None
    if is_valid_number(info.get("returnOnEquity")):
        roe_pct = info["returnOnEquity"] * 100

    operating_margin_pct = None
    if is_valid_number(info.get("operatingMargins")):
        operating_margin_pct = info["operatingMargins"] * 100

    revenue_growth_pct = None
    if is_valid_number(info.get("revenueGrowth")):
        revenue_growth_pct = info["revenueGrowth"] * 100

    debt_to_equity = None
    if is_valid_number(info.get("debtToEquity")):
        # Yahoo renvoie généralement ce champ déjà multiplié par 100 (ex: 45.3 pour un ratio de 0.453)
        debt_to_equity = info["debtToEquity"] / 100

    pcf, roce_pct = (None, None)
    if not is_etf:
        pcf, roce_pct = compute_pcf_and_roce(ticker, market_cap)

    # Actus récentes
    news_items = []
    try:
        if hasattr(ticker, "get_news"):
            raw_news = retry(ticker.get_news, count=5)
        else:
            raw_news = retry(lambda: ticker.news)
        for item in raw_news or []:
            content = item.get("content", item)
            title = content.get("title") or item.get("title")
            provider = content.get("provider") or {}
            publisher = provider.get("displayName") or item.get("publisher") or ""
            if not title:
                continue
            news_items.append({"title": title, "publisher": publisher})
            if len(news_items) >= NEWS_ITEMS_PER_TICKER:
                break
    except Exception:
        pass

    # Agenda : prochaine date de résultats (non applicable aux ETF)
    upcoming_events = []
    if not is_etf:
        try:
            earnings_dates = retry(ticker.get_earnings_dates, limit=4)
            if earnings_dates is not None and not earnings_dates.empty:
                now = pd.Timestamp.now(tz=earnings_dates.index.tz)
                future = earnings_dates[earnings_dates.index >= now]
                if not future.empty:
                    next_date = future.index[0]
                    if (next_date - now).days <= EARNINGS_LOOKAHEAD_DAYS:
                        upcoming_events.append(f"Résultats prévus le {next_date.strftime('%d/%m/%Y')}")
        except Exception:
            pass

    return {
        "symbol": symbol,
        "name": name,
        "is_etf": is_etf,
        "last_close": last_close,
        "day_change_pct": day_change_pct,
        "last_volume": last_volume,
        "avg_volume_20d": avg_volume_20d,
        "week52_high": week52_high,
        "week52_low": week52_low,
        "pct_from_high": pct_from_high,
        "rsi": rsi,
        "sma50": sma50,
        "sma200": sma200,
        "trend": trend,
        "relative_momentum_pct": relative_momentum_pct,
        "pe_ratio": pe_ratio,
        "price_to_book": price_to_book,
        "pcf": pcf,
        "dividend_yield": dividend_yield,
        "beta": beta,
        "roe_pct": roe_pct,
        "operating_margin_pct": operating_margin_pct,
        "roce_pct": roce_pct,
        "debt_to_equity": debt_to_equity,
        "revenue_growth_pct": revenue_growth_pct,
        "news_items": news_items,
        "upcoming_events": upcoming_events,
    }


# --------------------------------------------------------------------------
# Score "à la Higgons" + signal
# --------------------------------------------------------------------------

def score_lower_better(value, tiers):
    if not is_valid_number(value):
        return None
    for threshold, score in tiers:
        if value <= threshold:
            return score
    return 0


def score_higher_better(value, tiers):
    if not is_valid_number(value):
        return None
    for threshold, score in tiers:
        if value >= threshold:
            return score
    return 0


def average_available(scores):
    available = [s for s in scores if s is not None]
    if not available:
        return None
    return sum(available) / len(available)


def compute_higgons_score(r: dict):
    """Renvoie (score 0-100 ou None, liste de flags, libellé du signal)."""
    decote_avg = average_available([
        score_lower_better(r.get("pe_ratio"), PER_TIERS),
        score_lower_better(r.get("price_to_book"), PB_TIERS),
        score_lower_better(r.get("pcf"), PCF_TIERS),
    ])
    qualite_avg = average_available([
        score_higher_better(r.get("roe_pct"), ROE_TIERS),
        score_higher_better(r.get("operating_margin_pct"), MARGIN_TIERS),
        score_lower_better(r.get("debt_to_equity"), DEBT_TIERS),
        score_higher_better(r.get("roce_pct"), ROCE_TIERS),
    ])

    parts = [p for p in [decote_avg, qualite_avg] if p is not None]
    if not parts:
        return None, [], "Données insuffisantes"

    total_score = round(sum(parts) / len(parts))

    flags = []
    if r.get("revenue_growth_pct") is not None and r["revenue_growth_pct"] < 0:
        flags.append("CA en baisse")
    if r.get("relative_momentum_pct") is not None and r["relative_momentum_pct"] < MOMENTUM_UNDERPERF_THRESHOLD:
        flags.append("Sous-performance vs marché")
    valorisation_elevee = decote_avg is not None and decote_avg <= 20
    if valorisation_elevee:
        flags.append("Valorisation élevée")

    if valorisation_elevee or total_score < 40:
        signal = "🔴 Vendre"
    elif flags:
        signal = "🟠 Surveiller"
    elif total_score >= 65:
        signal = "🟢 Conserver / Renforcer"
    else:
        signal = "🟢 Conserver"

    return total_score, flags, signal


# --------------------------------------------------------------------------
# Message Telegram
# --------------------------------------------------------------------------

def build_ticker_block(r: dict) -> str:
    arrow = "🟢" if r["day_change_pct"] >= 0 else "🔴"
    lines = [f"{arrow} <b>{html.escape(r['symbol'])} — {html.escape(r['name'])}</b>"]
    lines.append(f"Cours: {fmt_num(r['last_close'])} ({r['day_change_pct']:+.2f}%)")
    lines.append(f"Volume: {fmt_num(r['last_volume'], 0)} (moy. 20j: {fmt_num(r['avg_volume_20d'], 0)})")
    lines.append(
        f"52 sem: {fmt_num(r['week52_low'])} – {fmt_num(r['week52_high'])} "
        f"({r['pct_from_high']:+.1f}% vs plus haut)"
    )

    tech_bits = [f"RSI(14): {fmt_num(r['rsi'], 1)}"]
    if not pd.isna(r["sma50"]):
        tech_bits.append(f"SMA50: {fmt_num(r['sma50'])}")
    if not pd.isna(r["sma200"]):
        tech_bits.append(f"SMA200: {fmt_num(r['sma200'])}")
    if r["trend"]:
        tech_bits.append(f"tendance {html.escape(r['trend'])}")
    lines.append(" | ".join(tech_bits))

    fond_bits = []
    if r["pe_ratio"]:
        fond_bits.append(f"P/E: {fmt_num(r['pe_ratio'], 1)}")
    if r["dividend_yield"]:
        dy = r["dividend_yield"]
        dy_pct = dy * 100 if dy < 1 else dy
        fond_bits.append(f"Rendement div: {fmt_num(dy_pct, 2, '%')}")
    if r["beta"]:
        fond_bits.append(f"Beta: {fmt_num(r['beta'], 2)}")
    if fond_bits:
        lines.append(" | ".join(fond_bits))

    # Score & signal "à la Higgons"
    score_bits = [f"Score: {r['score'] if r['score'] is not None else 'n/d'}/100", r["signal"]]
    lines.append(" — ".join(score_bits))
    if r["flags"]:
        lines.append("⚑ " + ", ".join(html.escape(f) for f in r["flags"]))

    if r["news_items"]:
        lines.append("📰 Actus:")
        for n in r["news_items"]:
            src = f" ({html.escape(n['publisher'])})" if n["publisher"] else ""
            lines.append(f"  • {html.escape(n['title'])}{src}")

    if r["upcoming_events"]:
        lines.append(f"📅 Agenda: {', '.join(r['upcoming_events'])}")

    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"Erreur Telegram: {resp.status_code} {resp.text}", file=sys.stderr)
    resp.raise_for_status()


def chunk_message(blocks, max_len=TELEGRAM_MAX_LEN):
    chunks, current = [], ""
    for block in blocks:
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) > max_len:
            if current:
                chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# --------------------------------------------------------------------------
# Historique / persistance (docs/data.json)
# --------------------------------------------------------------------------

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"generated_at": None, "tickers": {}}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"generated_at": None, "tickers": {}}


def save_history(history):
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=lambda o: None)


def get_last_signal(history, symbol):
    bucket = history["tickers"].get(symbol)
    if not bucket or not bucket.get("history"):
        return None
    return bucket["history"][-1].get("signal")


def append_history_entry(history, symbol, name, entry):
    bucket = history["tickers"].setdefault(symbol, {"name": name, "history": []})
    bucket["name"] = name
    bucket["history"].append(entry)
    bucket["history"] = bucket["history"][-MAX_HISTORY_ENTRIES:]


def clean_for_json(value):
    """Convertit les NaN/valeurs numpy en types JSON-sérialisables (None si NaN)."""
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):  # numpy scalar (float64, int64, ...)
        value = value.item()
        if isinstance(value, float) and math.isnan(value):
            return None
    return value


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis (secrets GitHub).", file=sys.stderr)
        sys.exit(1)

    tickers = load_tickers()
    now = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    today_label = now.strftime("%A %d %B %Y")

    history = load_history()
    benchmark_cache = {}

    blocks = [f"📈 <b>Récapitulatif du portefeuille — {today_label}</b>"]
    if dashboard_url:
        blocks[0] += f"\n🔗 <a href=\"{html.escape(dashboard_url)}\">Dashboard complet</a>"
 
    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    if dashboard_url:
        blocks.append(f'🔗 <a href="{html.escape(dashboard_url)}">Voir le dashboard complet</a>')
    
    signal_changes = []
    errors = []

    for entry in tickers:
        symbol = entry.get("symbol", "?")
        try:
            report = fetch_ticker_report(entry, benchmark_cache)
            score, flags, signal = compute_higgons_score(report)
            report["score"] = score
            report["flags"] = flags
            report["signal"] = signal

            prev_signal = get_last_signal(history, report["symbol"])
            if prev_signal and prev_signal != signal:
                signal_changes.append(f"{report['symbol']}: {prev_signal} → {signal}")

            append_history_entry(history, report["symbol"], report["name"], {
                "date": today_str,
                "close": clean_for_json(report["last_close"]),
                "day_change_pct": clean_for_json(report["day_change_pct"]),
                "score": score,
                "signal": signal,
                "flags": flags,
                "pe_ratio": clean_for_json(report["pe_ratio"]),
                "roe_pct": clean_for_json(report["roe_pct"]),
            })

            blocks.append(build_ticker_block(report))
        except Exception as e:
            errors.append(f"{symbol}: {e}")
        time.sleep(1)  # espace les appels pour limiter le risque de rate-limit

    if signal_changes:
       insert_at = 2 if dashboard_url else 1
        blocks.insert(insert_at, "🔔 <b>Changements de signal aujourd'hui</b>\n" +
                      "\n".join(html.escape(c) for c in signal_changes))

    if errors:
        blocks.append("⚠️ <b>Titres non récupérés</b>\n" + "\n".join(html.escape(e) for e in errors))

    history["generated_at"] = now.isoformat()
    save_history(history)

    for chunk in chunk_message(blocks):
        send_telegram_message(token, chat_id, chunk)


if __name__ == "__main__":
    main()
