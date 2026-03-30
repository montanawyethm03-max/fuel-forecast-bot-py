import os
import re
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Config ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "@BorderlineDailyFuelForecast")

BASELINE_PHP = 56.00  # baseline peso for calculation

FALLBACK = {
    "date":     "Mar 24, 2026",
    "diesel":   "17.80",
    "gasoline": "10.70",
    "kerosene": "21.90",
    "dir":      "up"
}

# ── HTTP Session with Retry ───────────────────────────────────────────────
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

# ── Helpers ─────────────────────────────────────────────────────────────
def get_direction(val: float) -> str:
    return "⬆" if val > 0 else "⬇" if val < 0 else "➡"

def get_sign(val: float) -> str:
    return "+" if val > 0 else ""

def fuel_range(val: float) -> tuple[float, float]:
    margin = abs(val) * 0.20
    low  = max(0.0, round((abs(val) - margin) * 2) / 2)
    high = round((abs(val) + margin) * 2) / 2
    return low, high

def next_tuesday(now: datetime) -> str:
    days = (1 - now.weekday() + 7) % 7
    if days == 0: days = 7
    return (now + timedelta(days=days)).strftime("%b %d, %Y")

# ── Data Fetchers ───────────────────────────────────────────────────────
def get_brent():
    """Fetch Brent closes and compute weekly avg change (current 5-day avg vs prior 5-day avg)."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?interval=1d&range=15d"
        r = session.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        if len(closes) >= 10:
            current_week_avg = sum(closes[-5:]) / 5
            prev_week_avg    = sum(closes[-10:-5]) / 5
            weekly_change    = round(current_week_avg - prev_week_avg, 2)
            return round(closes[-1], 2), weekly_change
        elif len(closes) >= 2:
            return round(closes[-1], 2), round(closes[-1] - closes[-2], 2)
    except Exception as e:
        print(f"[WARN] Brent fetch failed: {e}")
    return 75.00, 0.0

def get_usd_php():
    """Fetch USD/PHP exchange rate."""
    try:
        r = session.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        return round(r.json()["rates"]["PHP"], 2)
    except Exception as e:
        print(f"[WARN] FX fetch failed: {e}")
    return BASELINE_PHP

def get_official_adjustment():
    """Scrape official DOE-aligned fuel adjustments from news sources."""
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

# ── Forecast Calculation ─────────────────────────────────────────────────
def calculate_forecast(brent_price, brent_change, usd_php):
    """Compute realistic local fuel estimate based on Brent + USD/PHP + dampener."""
    barrel_to_ltr = 159
    forex_factor = usd_php / BASELINE_PHP

    raw_estimate = (brent_change / barrel_to_ltr) * usd_php * forex_factor

    abs_change = abs(brent_change)
    dampener = 0.75 if abs_change > 15 else 0.90 if abs_change > 5 else 1.0
    est = round(raw_estimate * dampener, 2)

    diesel   = max(-8,  min(8,  round(est * 1.1, 2)))
    gasoline = max(-6,  min(6,  round(est * 0.9, 2)))
    kerosene = max(-7,  min(7,  round(est * 1.0, 2)))

    d_low, d_high = fuel_range(diesel)
    g_low, g_high = fuel_range(gasoline)
    k_low, k_high = fuel_range(kerosene)

    trend = (
        "Big increase"    if est > 6  else
        "Slight increase" if est > 1  else
        "Flat / Stable"   if -1 <= est <= 1 else
        "Slight rollback" if est > -6 else
        "Big rollback"
    )

    advice = (
        "Gas up now - prices going up"        if est > 6  else
        "Gas up soon - slight increase ahead" if est > 1  else
        "No rush - prices stable"             if -1 <= est <= 1 else
        "You can wait - rollback expected"
    )

    return {
        "diesel": diesel, "gasoline": gasoline, "kerosene": kerosene,
        "d_low": d_low, "d_high": d_high,
        "g_low": g_low, "g_high": g_high,
        "k_low": k_low, "k_high": k_high,
        "trend": trend, "advice": advice, "est": est
    }

def get_confidence(weekday):
    return "High" if weekday >= 4 else "Medium" if weekday >= 2 else "Low"

# ── Message Builder ─────────────────────────────────────────────────────
def build_message(now, brent_price, brent_change, usd_php, official, forecast):
    dir_arrow  = "⬆" if official["dir"] == "up" else "⬇"
    confidence = get_confidence(now.weekday())
    peso_dir   = "weak" if usd_php > BASELINE_PHP else "strong"
    d_label    = "⬆ increase" if forecast["diesel"]   >= 0 else "⬇ rollback"
    g_label    = "⬆ increase" if forecast["gasoline"]  >= 0 else "⬇ rollback"
    k_label    = "⬆ increase" if forecast["kerosene"]  >= 0 else "⬇ rollback"

    return (
        f"⛽ Borderline Daily Fuel Forecast\n"
        f"🕓 {now.strftime('%b %d, %Y')} | {now.strftime('%I:%M %p')}\n\n"
        f"Official Adjustment ({official['date']})\n"
        f"Diesel:   {dir_arrow} ₱{official['diesel']}/L\n"
        f"Gasoline: {dir_arrow} ₱{official['gasoline']}/L\n"
        f"Kerosene: {dir_arrow} ₱{official['kerosene']}/L\n\n"
        f"📢 Next Week Estimate ({next_tuesday(now)})\n"
        f"Diesel:   ₱{forecast['d_low']:.2f}-₱{forecast['d_high']:.2f}/L {d_label}\n"
        f"Gasoline: ₱{forecast['g_low']:.2f}-₱{forecast['g_high']:.2f}/L {g_label}\n"
        f"Kerosene: ₱{forecast['k_low']:.2f}-₱{forecast['k_high']:.2f}/L {k_label}\n\n"
        f"Trend: {forecast['trend']}\n"
        f"Confidence: {confidence}\n"
        f"Advice: {forecast['advice']}\n\n"
        f"Brent: {get_direction(brent_change)} ${brent_price}/bbl ({abs(brent_change)}/wk avg)\n"
        f"USD/PHP: {usd_php} | Peso {peso_dir}"
    )

# ── Telegram Sender ─────────────────────────────────────────────────────
def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}

    try:
        r = session.post(url, json=data, timeout=30)
        r.raise_for_status()
        print("✅ Forecast sent to Telegram.")
    except requests.exceptions.RequestException as e:
        print(f"❌ Telegram failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────
def main():
    now = datetime.now(ZoneInfo("Asia/Manila"))

    print("Fetching data...")
    brent_price, brent_change = get_brent()
    usd_php = get_usd_php()
    official = get_official_adjustment()

    print(f"Brent: ${brent_price} | Change: {brent_change} | USD/PHP: {usd_php}")

    forecast = calculate_forecast(brent_price, brent_change, usd_php)
    message = build_message(now, brent_price, brent_change, usd_php, official, forecast)

    print("\n--- MESSAGE PREVIEW ---")
    print(message)
    print("-----------------------\n")

    send_telegram(message)

if __name__ == "__main__":
    main()
