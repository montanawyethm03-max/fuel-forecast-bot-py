import os
import re
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "@BorderlineDailyFuelForecast")

BASELINE_PHP = 56.00

# Fallback DOE values (update manually each Tuesday)
FALLBACK = {
    "date":     "Mar 24, 2026",
    "diesel":   "17.80",
    "gasoline": "10.70",
    "kerosene": "21.90",
    "dir":      "up"
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def get_direction(val: float) -> str:
    if val > 0:  return "⬆"
    if val < 0:  return "⬇"
    return "➡"

def get_sign(val: float) -> str:
    return "+" if val > 0 else ""

def next_tuesday(now: datetime) -> str:
    days = (1 - now.weekday() + 7) % 7  # 1 = Tuesday
    if days == 0:
        days = 7
    return (now + timedelta(days=days)).strftime("%b %d, %Y")

# ── Data Fetchers ──────────────────────────────────────────────────────────────

def get_brent() -> tuple[float, float]:
    """Returns (price, daily_change). Falls back to (75.00, 0) on error."""
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F?interval=1d&range=5d"
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        closes = [c for c in r.json()["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
        if len(closes) >= 2:
            return round(closes[-1], 2), round(closes[-1] - closes[-2], 2)
    except Exception as e:
        print(f"[WARN] Brent fetch failed: {e}")
    return 75.00, 0.0


def get_usd_php() -> float:
    """Returns USD/PHP rate. Falls back to 56.00 on error."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10, verify=False)
        return round(r.json()["rates"]["PHP"], 2)
    except Exception as e:
        print(f"[WARN] FX fetch failed: {e}")
    return BASELINE_PHP


def get_official_adjustment() -> dict:
    """Scrapes latest DOE adjustment from GMA News. Returns fallback on error."""
    result = dict(FALLBACK)
    try:
        search_url = "https://www.gmanetwork.com/news/search/?q=pump+prices+tuesday"
        r = requests.get(search_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"}, verify=False)
        soup = BeautifulSoup(r.text, "html.parser")

        article_url = None
        for a in soup.find_all("a", href=True):
            if "pump-price" in a["href"] and "gmanetwork.com/news" in a["href"]:
                article_url = a["href"]
                break

        if article_url:
            r2 = requests.get(article_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
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
        print(f"[WARN] GMA News scrape failed: {e}")

    return result


# ── Forecast Calculation ───────────────────────────────────────────────────────

def calculate_forecast(brent_price: float, brent_change: float, usd_php: float) -> dict:
    forex_factor  = usd_php / BASELINE_PHP
    barrel_to_ltr = 159
    raw_estimate  = (brent_change / barrel_to_ltr) * usd_php * forex_factor

    abs_change = abs(brent_change)
    dampener   = 0.35 if abs_change > 6 else 0.65 if abs_change > 3 else 1.0
    est        = round(raw_estimate * dampener, 2)

    diesel   = max(-8, min(8,  round(est * 1.1, 2)))
    gasoline = max(-6, min(6,  round(est * 0.9, 2)))
    kerosene = max(-7, min(7,  round(est * 1.0, 2)))

    trend = (
        "Still increasing"  if est > 3  else
        "Slight increase"   if est > 0  else
        "Flat / Stable"     if est == 0 else
        "Slight rollback"   if est > -3 else
        "Big rollback"
    )
    advice = (
        "Gas up now - prices going up"        if est > 3  else
        "Gas up soon - slight increase ahead" if est > 0  else
        "No rush - prices stable"             if est == 0 else
        "You can wait - rollback expected"
    )

    return {
        "diesel": diesel, "gasoline": gasoline, "kerosene": kerosene,
        "trend": trend, "advice": advice, "est": est
    }


def get_confidence(weekday: int) -> str:
    return "High" if weekday >= 4 else "Medium" if weekday >= 2 else "Low"


# ── Build Message ──────────────────────────────────────────────────────────────

def build_message(now: datetime, brent_price: float, brent_change: float,
                  usd_php: float, official: dict, forecast: dict) -> str:
    dir_arrow = "⬆" if official["dir"] == "up" else "⬇"
    confidence = get_confidence(now.weekday())
    d, g, k = forecast["diesel"], forecast["gasoline"], forecast["kerosene"]
    peso_dir = "weak" if usd_php > BASELINE_PHP else "strong"

    return (
        f"⛽ Borderline Daily Fuel Forecast\n"
        f"🕓 {now.strftime('%b %d, %Y')} | {now.strftime('%I:%M %p')}\n\n"
        f"Official Adjustment ({official['date']})\n"
        f"Diesel:   {dir_arrow} ₱{official['diesel']}/L\n"
        f"Gasoline: {dir_arrow} ₱{official['gasoline']}/L\n"
        f"Kerosene: {dir_arrow} ₱{official['kerosene']}/L\n\n"
        f"📢 Next Week Estimate ({next_tuesday(now)})\n"
        f"Diesel:   {get_direction(d)} {get_sign(d)}₱{d}/L\n"
        f"Gasoline: {get_direction(g)} {get_sign(g)}₱{g}/L\n"
        f"Kerosene: {get_direction(k)} {get_sign(k)}₱{k}/L\n\n"
        f"Trend: {forecast['trend']}\n"
        f"Confidence: {confidence}\n"
        f"Advice: {forecast['advice']}\n\n"
        f"Brent: {get_direction(brent_change)} ${brent_price}/bbl ({abs(brent_change)}/day)\n"
        f"USD/PHP: {usd_php} | Peso {peso_dir}"
    )


# ── Send to Telegram ───────────────────────────────────────────────────────────

def send_telegram(message: str) -> None:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message}
    r = requests.post(url, json=data, timeout=10, verify=False)
    if r.ok:
        print("Forecast sent to Telegram.")
    else:
        print(f"Telegram send failed: {r.status_code} {r.text}")


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
