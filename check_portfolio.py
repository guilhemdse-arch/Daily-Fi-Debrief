import os
import json
import requests
import yfinance as yf

# Indice CAC Mid & Small sur Yahoo Finance
BENCHMARK_TICKER = "EESM.PA"
PERIOD_DAYS = 126  # Environ 6 mois de cotation (jours ouvrés)

def send_telegram_alert(message):
    token = os.environ.get("8981277305:AAFFa5yjVjyJvVoEchTcsCZnhye47NnK-WM")
    chat_id = os.environ.get("8532103043")
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"})

def main():
    with open("positions.json", "r") as f:
        portfolio = json.load(f)

    # Récupération de l'indice de référence
    bench = yf.Ticker(BENCHMARK_TICKER)
    bench_hist = bench.history(period="1y")["Close"]

    if len(bench_hist) < PERIOD_DAYS:
        print(f"Historique insuffisant pour l'indice {BENCHMARK_TICKER}")
        return

    bench_perf = (bench_hist.iloc[-1] - bench_hist.iloc[-PERIOD_DAYS]) / bench_hist.iloc[-PERIOD_DAYS]
    alerts = []

    for stock in portfolio:
        ticker_symbol = stock["ticker"]
        t = yf.Ticker(ticker_symbol)
        hist = t.history(period="1y")["Close"]
        
        if len(hist) < PERIOD_DAYS:
            print(f"Historique insuffisant pour {ticker_symbol}, action ignorée.")
            continue
            
        current_price = hist.iloc[-1]
        stock_perf = (current_price - hist.iloc[-PERIOD_DAYS]) / hist.iloc[-PERIOD_DAYS]
        
        # 1. Calcul du Momentum Relatif face à l'indice
        relative_momentum = stock_perf - bench_perf
        
        # 2. Calcul du PER
        bpa = stock.get("bpa", 0)
        per = current_price / bpa if bpa > 0 else 0

        # Application des seuils d'arbitrage
        if relative_momentum < -0.20:
            alerts.append(f"⚠️ *{stock['nom']}* ({ticker_symbol}) : Sous-performance relative de *{relative_momentum*100:.1f}%* vs indice.")
        
        if per >= 20:
            alerts.append(f"🚨 *{stock['nom']}* ({ticker_symbol}) : PER de *{per:.1f}* (Sortie totale > 20).")
        elif per >= 17:
            alerts.append(f"⚡ *{stock['nom']}* ({ticker_symbol}) : PER de *{per:.1f}* (Allègement > 17).")

    if alerts:
        message = "📊 *Alerte Portefeuille Indépendance AM*\n\n" + "\n\n".join(alerts)
        send_telegram_alert(message)
        print(f"{len(alerts)} alerte(s) envoyée(s) sur Telegram.")
    else:
        print("Aucun signal de vente détecté aujourd'hui.")

if __name__ == "__main__":
    main()
