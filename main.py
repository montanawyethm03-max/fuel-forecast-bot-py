import os
import re
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import random

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "@BorderlineDailyFuelForecast")

BASELINE_PHP = 56.00

FALLBACK = {
    "date":     "Mar 24, 2026",
    "diesel":   "17.80",
    "gasoline": "10.70",
    "kerosene": "21.90",
    "dir":      "up"
}

# ── HTTP Session with Retry ────────────────────────────────────────────────────
def create_session():
    session = requests.Session()
    retries = Retry(
        total=5,
        backoff_factor=2,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    return session

session = create_session()

# ── Helpers ───────────────────────────────────────────────────────────────────
def get_direction(val: float) -> str:
    if val > 0: return "⬆"
    if val < 0: return "⬇"
    return "➡"

def get_sign(val: float) -> str:
    return "+" if val > 0 else ""

def next_tuesday(now: datetime) -> str:
    days = (1 - now.weekday() + 7) % 7
    if days == 0:
        days = 7
    return (now + timedelta(days=days)).strftime("%b %d, %Y")

# ── Data Fetchers ──────────────────────────────────────────────────────────────
def get_brent():
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?interval=1d&range=10d"
        r = session.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        if len(closes) >= 10:
            this_week_avg = sum(closes[-5:]) / 5
            last_week_avg = sum(closes[-10:-5]) / 5
            change = this_week_avg - last_week_avg
            return round(this_week_avg, 2), round(change, 2)
    except Exception as e:
        print(f"[WARN] Brent fetch failed: {e}")
    return 75.00, 0.0

def get_usd_php():
    try:
        r = session.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        return round(r.json()["rates"]["PHP"], 2)
    except Exception as e:
        print(f"[WARN] FX fetch failed: {e}")
    return BASELINE_PHP

def get_official_adjustment():
    result = dict(FALLBACK)
    try:
        search_url = "https://www.gmanetwork.com/news/search/?q=pump+prices+tuesday"
        r = session.get(search_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")

        article_url = None
        for a in soup.find_all("a", href=True):
            if "pump-price" in a["href"] and "gmanetwork.com/news" in a["href"]:
                article_url = a["href"]
                break

        if article_url:
            r2 = session.get(article_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
            html = r2.text

            date_match = re.search(r'Tuesday,?\s+([\w]+ \d+,?\s+\d{4})', html)
            result["date"] = date_match.group(1) if date_match else "latest"

            d = re.search(r'diesel[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)
            g = re.search(r'gasoline[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)
            k = re.search(r'kerosene[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)

            if d: result["diesel"]   = d.group(1)
            if g: result["gasoline"] = g.group(1)
            if k: result["kerosene"] = k.group(1)

            result["dir"] = "up" if re.search(r'hike|increas|up', html, re.IGNORECASE) else "down"

    except Exception as e:
        print(f"[WARN] GMA scrape failed: {e}")

    return result

# ── Forecast Calculation with range ─────────────────────────────────────────────
def calculate_forecast(brent_price, brent_change, usd_php):
    forex_factor  = usd_php / BASELINE_PHP
    barrel_to_ltr = 159
    raw_estimate  = (brent_change / barrel_to_ltr) * usd_php * forex_factor

    abs_change = abs(brent_change)
    dampener = 1.0 if abs_change > 6 else 0.85 if abs_change > 3 else 0.7
    est = round(raw_estimate * dampener, 2)

    # Apply scaling and realistic min/max
    diesel_base   = est * 1.1
    gasoline_base = est * 0.9
    kerosene_base = est * 1.0

    # Add ± uncertainty for realistic range
    diesel_range   = (round(max(1, diesel_base*0.9),2), round(min(14, diesel_base*1.1),2))
    gasoline_range = (round(max(0.5, gasoline_base*0.7),2), round(min(3, gasoline_base*1.2),2))
    kerosene_range = (round(max(1, kerosene_base*0.8),2), round(min(5, kerosene_base*1.2),2))

    trend = (
        "Still increasing" if est > 3 else
        "Slight increase" if est > 0 else
        "Flat / Stable" if est == 0 else
        "Slight rollback" if est > -3 else
        "Big rollback"
    )

    advice = (
        "Gas up now - prices going up" if est > 3 else
        "Gas up soon - slight increase ahead" if est > 0 else
        "No rush - prices stable" if est == 0 else
        "You can wait - rollback expected"
    )

    return {
        "diesel": diesel_range,
        "gasoline": gasoline_range,
        "kerosene": kerosene_range,
        "trend": trend,
        "advice": advice,
        "est": est
    }

def get_confidence(weekday):
    return "High" if weekday >= 4 else "Medium" if weekday >= 2 else "Low"

# ── Message Builder ────────────────────────────────────────────────────────────
def build_message(now, brent_price, brent_change, usd_php, official, forecast):
    dir_arrow = "⬆" if official["dir"] == "up" else "⬇"
    confidence = get_confidence(now.weekday())
    d_min, d_max = forecast["diesel"]
    g_min, g_max = forecast["gasoline"]
    k_min, k_max = forecast["kerosene"]
    peso_dir = "weak" if usd_php > BASELINE_PHP else "strong"

    return (
        f"⛽ Borderline Daily Fuel Forecast\n"
        f"🕓 {now.strftime('%b %d, %Y')} | {now.strftime('%I:%M %p')}\n\n"
        f"Official Adjustment ({official['date']})\n"
        f"Diesel:   {dir_arrow} ₱{official['diesel']}/L\n"
        f"Gasoline: {dir_arrow} ₱{official['gasoline']}/L\n"
        f"Kerosene: {dir_arrow} ₱{official['kerosene']}/L\n\n"
        f"📢 Next Week Estimate ({next_tuesday(now)})\n"
        f"Diesel:   {get_direction(d_max)} ₱{d_min}-{d_max}/L\n"
        f"Gasoline: {get_direction(g_max)} ₱{g_min}-{g_max}/L\n"
        f"Kerosene: {get_direction(k_max)} ₱{k_min}-{k_max}/L\n\n"
        f"Trend: {forecast['trend']}\n"
        f"Confidence: {confidence}\n"
        f"Advice: {forecast['advice']}\n\n"
        f"Brent: {get_direction(brent_change)} ${brent_price}/bbl ({abs(brent_change)}/day)\n"
        f"USD/PHP: {usd_php} | Peso {peso_dir}"
    )

# ── Telegram Sender ────────────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    try:
        r = session.post(url, json=data, timeout=30)
        r.raise_for_status()
        print("✅ Forecast sent to Telegram.")
    except requests.exceptions.RequestException as e:
        print(f"❌ Telegram failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(ZoneInfo("Asia/Manila"))

    print("Fetching data...")
    brent_price, brent_change = get_brent()
    usd_php  = get_usd_php()
    official = get_official_adjustment()

    print(f"Brent: ${brent_price} | Change: {brent_change} | USD/PHP: {usd_php}")

    forecast = calculate_forecast(brent_price, brent_change, usd_php)
    message  = build_message(now, brent_price, brent_change, usd_php, official, forecast)

    print("\n--- MESSAGE PREVIEW ---")
    print(message)
    print("-----------------------\n")

    send_telegram(message)

if __name__ == "__main__":
    main()
