#!/usr/bin/env python3
"""
Rapport quotidien de portefeuille (actions & ETF) envoyé sur Telegram.

Source de données : Yahoo Finance via la librairie yfinance (gratuite,
non officielle). Ce choix a été fait car yfinance couvre aussi bien les
tickers US que les tickers européens (suffixes .PA, .DE, .L, ...), ce
qu'une API "officielle" gratuite type Finnhub ne fait pas.

Contrepartie assumée : yfinance peut ponctuellement échouer ou être
limité par Yahoo. Le script est donc écrit pour être tolérant aux
pannes (retry + gestion par titre : un titre en échec n'empêche pas
l'envoi du reste du rapport).
"""

import os
import sys
import json
import time
import html
from datetime import datetime, timedelta

import requests
import yfinance as yf
import pandas as pd

TICKERS_FILE = os.path.join(os.path.dirname(__file__), "tickers.json")
HISTORY_PERIOD = "1y"
RSI_PERIOD = 14
MAX_RETRIES = 3
RETRY_DELAY_SEC = 3
NEWS_ITEMS_PER_TICKER = 2
EARNINGS_LOOKAHEAD_DAYS = 14
TELEGRAM_MAX_LEN = 3800  # marge sous la limite Telegram de 4096 caractères


def load_tickers():
    with open(TICKERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def retry(fn, *args, **kwargs):
    """Ré-essaie un appel réseau/yfinance en cas d'erreur ponctuelle (rate-limit, etc.)."""
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_err = e
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY_SEC * attempt)
    raise last_err


def compute_rsi(close: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else float("nan")


def fmt_num(value, decimals=2, suffix=""):
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/d"
    return f"{value:,.{decimals}f}{suffix}".replace(",", " ")


def fetch_ticker_report(entry: dict) -> dict:
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

    # Fondamentaux : best-effort, pas toujours disponibles selon le titre/l'ETF
    info = {}
    try:
        info = retry(ticker.get_info)
    except Exception:
        pass

    pe_ratio = info.get("trailingPE") if not is_etf else None
    dividend_yield = info.get("dividendYield")
    beta = info.get("beta") if not is_etf else None

    # Actus récentes
    news_items = []
    try:
        if hasattr(ticker, "get_news"):
            raw_news = retry(ticker.get_news, count=5)
        else:
            raw_news = retry(lambda: ticker.news)
        for item in raw_news or []:
            content = item.get("content", item)  # compat anciennes/nouvelles versions yfinance
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

    # Agenda : prochaine date de résultats si dans la fenêtre définie
    # (non applicable aux ETF, qui n'ont pas de publication de résultats)
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
        "pe_ratio": pe_ratio,
        "dividend_yield": dividend_yield,
        "beta": beta,
        "news_items": news_items,
        "upcoming_events": upcoming_events,
    }


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
        # Yahoo renvoie parfois un ratio (0.006) et parfois déjà un pourcentage (0.6) selon les titres.
        dy = r["dividend_yield"]
        dy_pct = dy * 100 if dy < 1 else dy
        fond_bits.append(f"Rendement div: {fmt_num(dy_pct, 2, '%')}")
    if r["beta"]:
        fond_bits.append(f"Beta: {fmt_num(r['beta'], 2)}")
    if fond_bits:
        lines.append(" | ".join(fond_bits))

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
    """Regroupe les blocs en messages sous la limite de taille Telegram."""
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


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID doivent être définis (secrets GitHub).", file=sys.stderr)
        sys.exit(1)

    tickers = load_tickers()
    today = datetime.now().strftime("%A %d %B %Y")

    blocks = [f"📈 <b>Récapitulatif du portefeuille — {today}</b>"]
    errors = []

    for entry in tickers:
        try:
            report = fetch_ticker_report(entry)
            blocks.append(build_ticker_block(report))
        except Exception as e:
            errors.append(f"{entry.get('symbol', '?')}: {e}")
        time.sleep(1)  # espace les appels pour limiter le risque de rate-limit

    if errors:
        blocks.append("⚠️ <b>Titres non récupérés</b>\n" + "\n".join(html.escape(e) for e in errors))

    for chunk in chunk_message(blocks):
        send_telegram_message(token, chat_id, chunk)


if __name__ == "__main__":
    main()
