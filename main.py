import os
import re
import requests
from datetime import datetime, timedelta, date, timezone
from zoneinfo import ZoneInfo
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── Config ───────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "@BorderlineDailyFuelForecast")

BASELINE_PHP   = 56.00
LITERS_PER_GAL = 3.785

FALLBACK = {
    "date":     "Mar 31, 2026",
    "diesel":   "0.00",
    "gasoline": "0.00",
    "kerosene": "0.00",
    "dir":      "up",
    "source":   "waiting for GMA / Inquirer / DOE"
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
session.verify = False  # bypass corporate SSL proxy
import urllib3; urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Helpers ─────────────────────────────────────────────────────────────
def get_direction(val: float) -> str:
    return "⬆" if val > 0 else "⬇" if val < 0 else "➡"

def fuel_range(val: float) -> tuple[float, float]:
    margin = abs(val) * 0.20
    low  = max(0.0, round((abs(val) - margin) * 2) / 2)
    high = round((abs(val) + margin) * 2) / 2
    return low, high

def next_tuesday_date(now: datetime) -> date:
    days = (1 - now.weekday() + 7) % 7
    if days == 0: days = 7
    return (now + timedelta(days=days)).date()

def next_tuesday_str(now: datetime) -> str:
    return next_tuesday_date(now).strftime("%b %d, %Y")

def is_recent_url(href: str, days: int = 5) -> bool:
    """Return True if the URL contains a date within the last `days` days."""
    cutoff = date.today() - timedelta(days=days)
    # Match date patterns: /2026/03/31/ or /2026-03-31 or 20260331
    patterns = [
        r'(\d{4})[/-](\d{2})[/-](\d{2})',
        r'(\d{4})(\d{2})(\d{2})',
    ]
    for pat in patterns:
        m = re.search(pat, href)
        if m:
            try:
                url_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return url_date >= cutoff
            except ValueError:
                continue
    return True  # no date in URL, allow through

def get_day_label(now: datetime) -> str:
    """Returns 'Day X (date_range)' for the current monitoring week."""
    next_tue = next_tuesday_date(now)
    monitor_start = next_tue - timedelta(days=8)   # Monday before announcement week
    monitor_end   = next_tue - timedelta(days=4)   # Friday (last monitoring day)
    today = now.date()

    day_num = min(max((today - monitor_start).days + 1, 1), 5)

    if day_num == 1:
        label = monitor_start.strftime("%b %d")
    else:
        end_show = min(today, monitor_end)
        label = f"{monitor_start.strftime('%b %d')}-{end_show.strftime('%d')}"

    final = " (FINAL)" if today >= monitor_end else ""
    return f"Day {day_num} ({label}){final}"

# ── Yahoo Finance Fetcher ────────────────────────────────────────────────
def _fetch_closes(ticker: str) -> list[tuple[date, float]]:
    """Fetch 30d of daily closes. Returns list of (date, close) tuples."""
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=30d"
        r = session.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        data   = r.json()["chart"]["result"][0]
        tss    = data["timestamp"]
        closes = data["indicators"]["quote"][0]["close"]
        result = []
        for ts, c in zip(tss, closes):
            if c is not None:
                d = datetime.fromtimestamp(ts, tz=timezone.utc).date()
                result.append((d, round(c, 4)))
        return result
    except Exception as e:
        print(f"[WARN] {ticker} fetch failed: {e}")
    return []

def _avg_in_range(closes: list[tuple[date, float]], start: date, end: date) -> float:
    """Average close price for dates in [start, end]."""
    vals = [c for d, c in closes if start <= d <= end]
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)


def get_refined_products(now: datetime):
    """
    Dynamic reference windows based on next Tuesday's announcement:
    - HO=F: latest close vs avg Mon-Fri, 3 weeks before next Tuesday
    - RB=F: latest close vs avg Mon-Fri, 2 weeks before next Tuesday
    Returns (ho_price, ho_change, rb_price, rb_change)
    """
    next_tue = next_tuesday_date(now)

    ho_ref_start = next_tue - timedelta(days=22)   # Monday, 3 weeks prior
    ho_ref_end   = next_tue - timedelta(days=18)   # Friday, 3 weeks prior
    rb_ref_start = next_tue - timedelta(days=15)   # Monday, 2 weeks prior
    rb_ref_end   = next_tue - timedelta(days=11)   # Friday, 2 weeks prior

    ho_closes = _fetch_closes("HO=F")
    rb_closes = _fetch_closes("RB=F")

    ho_price = ho_closes[-1][1] if ho_closes else 0.0
    rb_price = rb_closes[-1][1] if rb_closes else 0.0

    ho_ref = _avg_in_range(ho_closes, ho_ref_start, ho_ref_end)
    rb_ref = _avg_in_range(rb_closes, rb_ref_start, rb_ref_end)

    ho_change = round(ho_price - ho_ref, 4) if ho_ref else 0.0
    rb_change = round(rb_price - rb_ref, 4) if rb_ref else 0.0

    print(f"[INFO] HO ref ({ho_ref_start.strftime('%b %d')}-{ho_ref_end.strftime('%d')}): {ho_ref} | Now: {ho_price} | Change: {ho_change}")
    print(f"[INFO] RB ref ({rb_ref_start.strftime('%b %d')}-{rb_ref_end.strftime('%d')}): {rb_ref} | Now: {rb_price} | Change: {rb_change}")

    return ho_price, ho_change, rb_price, rb_change


def get_usd_php() -> float:
    try:
        r = session.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        return round(r.json()["rates"]["PHP"], 2)
    except Exception as e:
        print(f"[WARN] FX fetch failed: {e}")
    return BASELINE_PHP

# ── Official Adjustment Scrapers ─────────────────────────────────────────
def _parse_adjustment_from_html(html: str, result: dict) -> bool:
    d = re.search(r'diesel[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)
    g = re.search(r'gasoline[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)
    k = re.search(r'kerosene[^0-9]*([0-9]+\.[0-9]+)', html, re.IGNORECASE)
    if not (d and g and k):
        return False
    date_match = re.search(r'Tuesday,?\s+([\w]+ \d+,?\s+\d{4})', html)
    result["date"]     = date_match.group(1) if date_match else "latest"
    result["diesel"]   = d.group(1)
    result["gasoline"] = g.group(1)
    result["kerosene"] = k.group(1)
    result["dir"]      = "up" if re.search(r'hike|increas|up', html, re.IGNORECASE) else "down"
    return True


def _scrape_gma(result: dict) -> bool:
    try:
        r = session.get("https://www.gmanetwork.com/news/search/?q=pump+prices+tuesday",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if "pump-price" in a["href"] and "gmanetwork.com/news" in a["href"] and is_recent_url(a["href"]):
                r2 = session.get(a["href"], timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "GMA News"
                    print("[INFO] Official adjustment from GMA News.")
                    return True
    except Exception as e:
        print(f"[WARN] GMA scrape failed: {e}")
    return False


def _scrape_inquirer(result: dict) -> bool:
    try:
        r = session.get("https://newsinfo.inquirer.net/?s=fuel+price+tuesday",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "inquirer.net" in href and re.search(r'fuel|oil|pump|diesel|gasoline', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "Inquirer.net"
                    print("[INFO] Official adjustment from Inquirer.net.")
                    return True
    except Exception as e:
        print(f"[WARN] Inquirer scrape failed: {e}")
    return False


def _scrape_abscbn(result: dict) -> bool:
    try:
        r = session.get("https://news.abs-cbn.com/search?q=pump+price+tuesday",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = "https://news.abs-cbn.com" + href
            if re.search(r'fuel|pump|diesel|gasoline|oil.price', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "ABS-CBN News"
                    print("[INFO] Official adjustment from ABS-CBN News.")
                    return True
    except Exception as e:
        print(f"[WARN] ABS-CBN scrape failed: {e}")
    return False


def _scrape_philstar(result: dict) -> bool:
    try:
        r = session.get("https://www.philstar.com/search#q=pump+price+tuesday&sort=date",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href.startswith("http"):
                href = "https://www.philstar.com" + href
            if "philstar.com" in href and re.search(r'fuel|pump|diesel|gasoline|oil.price', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "PhilStar"
                    print("[INFO] Official adjustment from PhilStar.")
                    return True
    except Exception as e:
        print(f"[WARN] PhilStar scrape failed: {e}")
    return False


def _scrape_mb(result: dict) -> bool:
    try:
        r = session.get("https://mb.com.ph/?s=pump+price+tuesday",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "mb.com.ph" in href and re.search(r'fuel|pump|diesel|gasoline|oil.price', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "Manila Bulletin"
                    print("[INFO] Official adjustment from Manila Bulletin.")
                    return True
    except Exception as e:
        print(f"[WARN] Manila Bulletin scrape failed: {e}")
    return False


def _scrape_news5(result: dict) -> bool:
    try:
        r = session.get("https://interaksyon.philstar.com/?s=pump+price+tuesday",
                        timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "interaksyon" in href and re.search(r'fuel|pump|diesel|gasoline|oil.price', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "News5"
                    print("[INFO] Official adjustment from News5.")
                    return True
    except Exception as e:
        print(f"[WARN] News5 scrape failed: {e}")
    return False


def get_official_adjustment() -> dict:
    result = dict(FALLBACK)
    if _scrape_gma(result):
        return result
    if _scrape_inquirer(result):
        return result
    if _scrape_abscbn(result):
        return result
    if _scrape_philstar(result):
        return result
    if _scrape_mb(result):
        return result
    if _scrape_news5(result):
        return result
    print("[WARN] All scrapers failed — using hardcoded fallback.")
    return result

# ── Forecast Calculation ─────────────────────────────────────────────────
def calculate_forecast(ho_change: float, rb_change: float, usd_php: float) -> dict:
    """
    ho_change: USD/gal change in Heating Oil (proxy for diesel & kerosene)
    rb_change: USD/gal change in RBOB Gasoline (proxy for gasoline)
    Kerosene tracks gasoline (x0.90), not diesel.
    """
    forex_factor = usd_php / BASELINE_PHP

    distillate_est = round((ho_change / LITERS_PER_GAL) * usd_php * forex_factor, 2)
    gasoline_est   = round((rb_change / LITERS_PER_GAL) * usd_php * forex_factor, 2)

    diesel   = max(-15, min(15, round(distillate_est * 1.00, 2)))
    gasoline = max(-10, min(10, round(gasoline_est  * 1.00, 2)))
    kerosene = max(-10, min(10, round(gasoline_est  * 0.90, 2)))   # tracks gasoline

    d_low, d_high = fuel_range(diesel)
    g_low, g_high = fuel_range(gasoline)
    k_low, k_high = fuel_range(kerosene)

    trend = (
        "Big increase"    if diesel > 6    else
        "Slight increase" if diesel > 1    else
        "Flat / Stable"   if -1 <= diesel <= 1 else
        "Slight rollback" if diesel > -6   else
        "Big rollback"
    )

    advice = (
        "Gas up now - prices going up"        if diesel > 6  else
        "Gas up soon - slight increase ahead" if diesel > 1  else
        "No rush - prices stable"             if -1 <= diesel <= 1 else
        "You can wait - rollback expected"
    )

    return {
        "diesel": diesel, "gasoline": gasoline, "kerosene": kerosene,
        "d_low": d_low, "d_high": d_high,
        "g_low": g_low, "g_high": g_high,
        "k_low": k_low, "k_high": k_high,
        "trend": trend, "advice": advice,
        "distillate_est": distillate_est, "gasoline_est": gasoline_est
    }

def get_confidence(weekday: int) -> str:
    return "High" if weekday >= 4 else "Medium" if weekday >= 2 else "Low"

# ── Message Builder ─────────────────────────────────────────────────────
def build_message(now, ho_price, ho_change, rb_price, rb_change,
                  usd_php, official, forecast):
    dir_arrow  = "⬆" if official["dir"] == "up" else "⬇"
    confidence = get_confidence(now.weekday())
    peso_dir   = "weak" if usd_php > BASELINE_PHP else "strong"
    d_label    = "⬆ increase" if forecast["diesel"]   >= 0 else "⬇ rollback"
    g_label    = "⬆ increase" if forecast["gasoline"]  >= 0 else "⬇ rollback"
    k_label    = "⬆ increase" if forecast["kerosene"]  >= 0 else "⬇ rollback"
    day_label  = get_day_label(now)

    has_official = float(official["diesel"]) > 0

    official_section = (
        f"Official Adjustment ({official['date']}) | {official['source']}\n"
        f"Diesel:   {dir_arrow} ₱{official['diesel']}/L\n"
        f"Gasoline: {dir_arrow} ₱{official['gasoline']}/L\n"
        f"Kerosene: {dir_arrow} ₱{official['kerosene']}/L\n\n"
    ) if has_official else ""

    return (
        f"⛽ Borderline Daily Fuel Forecast\n"
        f"🕓 {now.strftime('%b %d, %Y')} | {now.strftime('%I:%M %p')}\n\n"
        f"{official_section}"
        f"📢 {day_label} — Next Adjustment ({next_tuesday_str(now)})\n"
        f"Diesel:   ₱{forecast['d_low']:.1f}-₱{forecast['d_high']:.1f}/L {d_label}\n"
        f"Gasoline: ₱{forecast['g_low']:.1f}-₱{forecast['g_high']:.1f}/L {g_label}\n"
        f"Kerosene: ₱{forecast['k_low']:.1f}-₱{forecast['k_high']:.1f}/L {k_label}\n\n"
        f"Trend: {forecast['trend']}\n"
        f"Confidence: {confidence}\n"
        f"Advice: {forecast['advice']}\n\n"
        f"Heating Oil: {get_direction(ho_change)} ${ho_price:.4f}/gal ({abs(ho_change):.4f} change)\n"
        f"RBOB Gas:    {get_direction(rb_change)} ${rb_price:.4f}/gal ({abs(rb_change):.4f} change)\n"
        f"USD/PHP: {usd_php} | Peso {peso_dir}"
    )

# ── Telegram Sender ─────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
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
    official = get_official_adjustment()
    ho_price, ho_change, rb_price, rb_change = get_refined_products(now)
    usd_php = get_usd_php()

    print(f"HO: ${ho_price:.4f} | Change: {ho_change:.4f}")
    print(f"RB: ${rb_price:.4f} | Change: {rb_change:.4f}")
    print(f"USD/PHP: {usd_php}")

    forecast = calculate_forecast(ho_change, rb_change, usd_php)
    message  = build_message(now, ho_price, ho_change, rb_price, rb_change,
                             usd_php, official, forecast)

    print("\n--- MESSAGE PREVIEW ---")
    print(message.encode("utf-8", errors="replace").decode("utf-8"))
    print("-----------------------\n")

    send_telegram(message)

if __name__ == "__main__":
    main()
