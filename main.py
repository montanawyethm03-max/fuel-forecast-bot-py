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

BASELINE_PHP      = 56.00
LITERS_PER_BARREL = 159.0
GAL_PER_BARREL    = 42.0

FALLBACK = {
    "date":     "Apr 01, 2026",
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

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

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
    cutoff = date.today() - timedelta(days=days)
    patterns = [r'(\d{4})[/-](\d{2})[/-](\d{2})', r'(\d{4})(\d{2})(\d{2})']
    for pat in patterns:
        m = re.search(pat, href)
        if m:
            try:
                url_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return url_date >= cutoff
            except ValueError:
                continue
    return True

def get_day_label(now: datetime) -> str:
    next_tue = next_tuesday_date(now)
    monitor_start = next_tue - timedelta(days=8)
    monitor_end   = next_tue - timedelta(days=4)
    today = now.date()
    day_num = min(max((today - monitor_start).days + 1, 1), 5)
    if day_num == 1:
        label = monitor_start.strftime("%b %d")
    else:
        end_show = min(today, monitor_end)
        label = f"{monitor_start.strftime('%b %d')}-{end_show.strftime('%d')}"
    final = " (FINAL)" if today >= monitor_end else ""
    return f"Day {day_num} ({label}){final}"

def get_monitoring_windows(now: datetime):
    next_tue  = next_tuesday_date(now)
    mon_start = next_tue - timedelta(days=8)
    fri_end   = next_tue - timedelta(days=4)
    ref_start = mon_start - timedelta(days=7)
    ref_end   = fri_end   - timedelta(days=7)
    return mon_start, fri_end, ref_start, ref_end

# ── Extract PHP/L amounts from article text ──────────────────────────────
def _extract_fuel_amounts(text: str) -> dict:
    """
    Extract fuel price adjustments from news article text.
    Returns dict like {'diesel': 12.50, 'gasoline': 1.30, 'kerosene': 2.00}
    """
    found = {}

    # Split into sentences for tighter matching
    sentences = re.split(r'[.;]\s+', text)

    for sentence in sentences:
        for fuel in ('diesel', 'gasoline', 'kerosene'):
            if fuel in sentence.lower() and fuel not in found:
                # Pattern: fuel_type ... P/peso XX.XX ... per liter
                m = re.search(
                    rf'{fuel}\D{{0,80}}?(?:P|₱)\s*([0-9]+(?:\.[0-9]+)?)',
                    sentence, re.IGNORECASE
                )
                if m:
                    val = float(m.group(1))
                    if 0.10 <= val <= 30.0:  # sanity: pump adjustments are 0.10-30 PHP/L
                        found[fuel] = val

    # Detect direction per fuel type from surrounding context
    # We look at the sentence containing each fuel for its specific direction
    for fuel in ('diesel', 'gasoline', 'kerosene'):
        if fuel not in found:
            continue
        # Find sentences mentioning this fuel
        for sentence in sentences:
            if fuel in sentence.lower():
                if re.search(r'rollback|decrease|drop|down|reduction|cut|lower', sentence, re.IGNORECASE):
                    found[fuel] = -abs(found[fuel])
                # else stays positive (increase is default)
                break

    found['_direction'] = "up"  # overall label, individual signs already set
    return found


def _extract_date_from_text(text: str) -> str:
    m = re.search(r'(?:March|April|May|June|July|August|September|October|November|December|January|February)\s+\d{1,2},?\s+\d{4}', text)
    if m:
        return m.group(0)
    return ""

# ── News Scraping: Bing News RSS (primary discovery) ────────────────────
def _discover_articles_bing(now: datetime) -> list[dict]:
    """
    Use Bing News RSS to find the latest PH fuel price articles.
    Filters to articles published within the current monitoring week only,
    so we don't accidentally use last week's adjustment as this week's forecast.
    """
    next_tue    = next_tuesday_date(now)
    mon_start   = next_tue - timedelta(days=8)  # start of monitoring week
    articles    = []
    queries = [
        "Philippines diesel gasoline price increase rollback per liter",
        "Philippines oil price watch fuel price Tuesday",
        "Philippines pump price diesel kerosene",
    ]
    seen_urls = set()

    for q in queries:
        try:
            r = session.get(
                f"https://www.bing.com/news/search?q={q.replace(' ', '+')}&format=rss",
                timeout=20, headers=UA
            )
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "xml")
            for item in soup.find_all("item")[:5]:
                title = item.find("title")
                link  = item.find("link")
                pub   = item.find("pubDate")
                if not (title and link):
                    continue
                title_text = title.get_text(strip=True)
                link_url   = link.get_text(strip=True)

                # Only fuel-related articles
                if not re.search(r'diesel|gasoline|fuel|oil.price|pump.price|kerosene', title_text, re.IGNORECASE):
                    continue

                # Filter by date: must be within current monitoring week
                if pub:
                    from email.utils import parsedate_to_datetime
                    try:
                        pub_date = parsedate_to_datetime(pub.get_text(strip=True)).date()
                        if pub_date < mon_start:
                            print(f"[SKIP] Too old ({pub_date}): {title_text[:60]}")
                            continue
                    except Exception:
                        pass

                if link_url in seen_urls:
                    continue
                seen_urls.add(link_url)
                articles.append({"title": title_text, "url": link_url})
        except Exception as e:
            print(f"[WARN] Bing RSS query failed: {e}")

    print(f"[INFO] Bing RSS found {len(articles)} current-week fuel articles")
    return articles


def _follow_and_parse(url: str) -> dict:
    """Follow a Bing redirect URL and parse the article for fuel price data."""
    try:
        r = session.get(url, timeout=20, headers=UA, allow_redirects=True)
        if r.status_code != 200:
            return {}
        soup = BeautifulSoup(r.text, "html.parser")
        paragraphs = []
        for p in soup.find_all("p"):
            t = p.get_text(strip=True)
            if t and len(t) > 15:
                paragraphs.append(t)
        full_text = " ".join(paragraphs)
        if not full_text:
            return {}
        amounts = _extract_fuel_amounts(full_text)
        if any(k in amounts for k in ('diesel', 'gasoline', 'kerosene')):
            amounts['_source_url'] = r.url
            amounts['_date'] = _extract_date_from_text(full_text)
            return amounts
    except Exception as e:
        print(f"[WARN] Article parse failed: {e}")
    return {}


def scrape_news_consensus(now: datetime) -> dict | None:
    """
    Scrape multiple news articles via Bing News RSS, extract fuel price
    adjustments, and build a consensus (median of all found values).
    Returns dict like {'diesel': 12.50, 'gasoline': 1.30, 'kerosene': 2.00,
                       'direction': 'up', 'source': 'News consensus (3 articles)'}
    or None if no data found.
    """
    articles = _discover_articles_bing(now)
    if not articles:
        return None

    all_diesel = []
    all_gasoline = []
    all_kerosene = []
    direction = "up"
    source_count = 0
    latest_date = ""

    for art in articles[:8]:  # cap at 8 articles to avoid slowness
        parsed = _follow_and_parse(art["url"])
        if not parsed:
            continue
        source_count += 1
        if 'diesel' in parsed:
            all_diesel.append(parsed['diesel'])
        if 'gasoline' in parsed:
            all_gasoline.append(parsed['gasoline'])
        if 'kerosene' in parsed:
            all_kerosene.append(parsed['kerosene'])
        if parsed.get('_direction'):
            direction = parsed['_direction']
        if parsed.get('_date') and not latest_date:
            latest_date = parsed['_date']

    if not all_diesel and not all_gasoline and not all_kerosene:
        return None

    def median(vals):
        if not vals:
            return 0.0
        s = sorted(vals)
        n = len(s)
        if n % 2 == 1:
            return s[n // 2]
        return round((s[n // 2 - 1] + s[n // 2]) / 2, 2)

    result = {
        "diesel":    median(all_diesel),
        "gasoline":  median(all_gasoline),
        "kerosene":  median(all_kerosene),
        "direction": direction,
        "source":    f"News consensus ({source_count} articles)",
        "date":      latest_date or "this week",
        "raw_diesel":    all_diesel,
        "raw_gasoline":  all_gasoline,
        "raw_kerosene":  all_kerosene,
    }

    print(f"[INFO] News consensus: diesel={all_diesel} gasoline={all_gasoline} kerosene={all_kerosene}")
    print(f"[INFO] Median: D=₱{result['diesel']:.2f} G=₱{result['gasoline']:.2f} K=₱{result['kerosene']:.2f} ({direction})")

    return result

# ── Yahoo Finance NYMEX Fetcher (fallback) ───────────────────────────────
def _fetch_closes(ticker: str) -> list[tuple[date, float]]:
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?interval=1d&range=30d"
        r = session.get(url, timeout=15, headers=UA)
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
    vals = [c for d, c in closes if start <= d <= end]
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)

def get_mops_proxies(now: datetime):
    """
    NYMEX-based MOPS proxy (fallback when no news data available).
    DOE formula: adjustment (PHP/L) = (MOPS_delta USD/bbl) x PHP/USD / 159
    """
    today     = now.date()
    mon_start, fri_end, ref_start, ref_end = get_monitoring_windows(now)

    ho_closes = _fetch_closes("HO=F")
    rb_closes = _fetch_closes("RB=F")

    ho_bbl = [(d, round(c * GAL_PER_BARREL, 2)) for d, c in ho_closes]
    rb_bbl = [(d, round(c * GAL_PER_BARREL, 2)) for d, c in rb_closes]

    cutoff  = min(today, fri_end)
    ho_this = _avg_in_range(ho_bbl, mon_start, cutoff)
    rb_this = _avg_in_range(rb_bbl, mon_start, cutoff)
    ho_ref  = _avg_in_range(ho_bbl, ref_start, ref_end)
    rb_ref  = _avg_in_range(rb_bbl, ref_start, ref_end)

    gasoil_change   = round(ho_this - ho_ref, 2) if ho_ref and ho_this else 0.0
    mogas_change    = round(rb_this - rb_ref, 2) if rb_ref and rb_this else 0.0
    kerosene_change = gasoil_change

    ho_latest = ho_bbl[-1][1] if ho_bbl else 0.0
    rb_latest = rb_bbl[-1][1] if rb_bbl else 0.0

    print(f"[INFO] Monitoring : {mon_start.strftime('%b %d')} - {cutoff.strftime('%b %d')} | Ref: {ref_start.strftime('%b %d')} - {ref_end.strftime('%b %d')}")
    print(f"[INFO] HO  ref avg: ${ho_ref:.2f}/bbl | This week avg: ${ho_this:.2f}/bbl | Change: ${gasoil_change:+.2f}/bbl")
    print(f"[INFO] RB  ref avg: ${rb_ref:.2f}/bbl | This week avg: ${rb_this:.2f}/bbl | Change: ${mogas_change:+.2f}/bbl")

    return ho_latest, gasoil_change, rb_latest, mogas_change, kerosene_change

def get_usd_php() -> float:
    try:
        r = session.get("https://open.er-api.com/v6/latest/USD", timeout=15)
        return round(r.json()["rates"]["PHP"], 2)
    except Exception as e:
        print(f"[WARN] FX fetch failed: {e}")
    return BASELINE_PHP

# ── Official Adjustment Scrapers (for last Tuesday's actual) ─────────────
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
                        timeout=15, headers=UA)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            if "pump-price" in a["href"] and "gmanetwork.com/news" in a["href"] and is_recent_url(a["href"]):
                r2 = session.get(a["href"], timeout=15, headers=UA)
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "GMA News"
                    return True
    except Exception as e:
        print(f"[WARN] GMA scrape failed: {e}")
    return False

def _scrape_inquirer(result: dict) -> bool:
    try:
        r = session.get("https://newsinfo.inquirer.net/?s=fuel+price+tuesday",
                        timeout=15, headers=UA)
        soup = BeautifulSoup(r.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "inquirer.net" in href and re.search(r'fuel|oil|pump|diesel|gasoline', href, re.IGNORECASE) and is_recent_url(href):
                r2 = session.get(href, timeout=15, headers=UA)
                if _parse_adjustment_from_html(r2.text, result):
                    result["source"] = "Inquirer.net"
                    return True
    except Exception as e:
        print(f"[WARN] Inquirer scrape failed: {e}")
    return False

def get_official_adjustment() -> dict:
    result = dict(FALLBACK)
    if _scrape_gma(result):
        return result
    if _scrape_inquirer(result):
        return result
    print("[WARN] Official scrapers failed -- using fallback.")
    return result

# ── Forecast Calculation ─────────────────────────────────────────────────
def calculate_forecast_from_news(consensus: dict) -> dict:
    """Build forecast from scraped news consensus (primary).
    Individual values already carry their sign (negative = rollback)."""
    diesel   = consensus["diesel"]
    gasoline = consensus["gasoline"]
    kerosene = consensus["kerosene"]

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
        "method": "news",
        "source": consensus["source"],
    }


def calculate_forecast_from_nymex(gasoil_change: float, mogas_change: float,
                                   kerosene_change: float, usd_php: float) -> dict:
    """Build forecast from NYMEX formula (fallback)."""
    diesel   = round(gasoil_change   * usd_php / LITERS_PER_BARREL, 2)
    gasoline = round(mogas_change    * usd_php / LITERS_PER_BARREL, 2)
    kerosene = round(kerosene_change * usd_php / LITERS_PER_BARREL, 2)

    diesel   = max(-15, min(15, diesel))
    gasoline = max(-10, min(10, gasoline))
    kerosene = max(-15, min(15, kerosene))

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
        "method": "nymex",
        "source": "NYMEX formula (no news data)",
    }


def get_confidence(weekday: int, method: str) -> str:
    if method == "news":
        return "High" if weekday >= 4 else "Medium-High" if weekday >= 2 else "Medium"
    return "Medium" if weekday >= 4 else "Low-Medium" if weekday >= 2 else "Low"

# ── Message Builder ─────────────────────────────────────────────────────
def build_message(now, ho_price, gasoil_change, rb_price, mogas_change,
                  usd_php, official, forecast):
    dir_arrow  = "⬆" if official["dir"] == "up" else "⬇"
    method     = forecast.get("method", "nymex")
    confidence = get_confidence(now.weekday(), method)
    peso_dir   = "weak" if usd_php > BASELINE_PHP else "strong"
    d_label    = "⬆ increase" if forecast["diesel"]   >= 0 else "⬇ rollback"
    g_label    = "⬆ increase" if forecast["gasoline"]  >= 0 else "⬇ rollback"
    k_label    = "⬆ increase" if forecast["kerosene"]  >= 0 else "⬇ rollback"
    day_label  = get_day_label(now)

    has_official = float(official["diesel"]) > 0

    official_section = (
        f"Official Adjustment ({official['date']}) | {official['source']}\n"
        f"Diesel:   {dir_arrow} P{official['diesel']}/L\n"
        f"Gasoline: {dir_arrow} P{official['gasoline']}/L\n"
        f"Kerosene: {dir_arrow} P{official['kerosene']}/L\n\n"
    ) if has_official else ""

    source_label = forecast.get("source", "NYMEX")

    nymex_section = ""
    if ho_price and rb_price:
        nymex_section = (
            f"\nGasoil (HO=F): {get_direction(gasoil_change)} ${ho_price:.2f}/bbl ({abs(gasoil_change):.2f} chg)\n"
            f"Mogas  (RB=F): {get_direction(mogas_change)} ${rb_price:.2f}/bbl ({abs(mogas_change):.2f} chg)\n"
        )

    return (
        f"Borderline Daily Fuel Forecast\n"
        f"{now.strftime('%b %d, %Y')} | {now.strftime('%I:%M %p')}\n\n"
        f"{official_section}"
        f"Next Adjustment: <b>{next_tuesday_str(now)}</b>\n"
        f"{day_label}\n"
        f"Diesel:   P{forecast['d_low']:.1f}-P{forecast['d_high']:.1f}/L {d_label}\n"
        f"Gasoline: P{forecast['g_low']:.1f}-P{forecast['g_high']:.1f}/L {g_label}\n"
        f"Kerosene: P{forecast['k_low']:.1f}-P{forecast['k_high']:.1f}/L {k_label}\n\n"
        f"Trend: {forecast['trend']}\n"
        f"Confidence: {confidence}\n"
        f"Advice: {forecast['advice']}\n\n"
        f"Source: {source_label}"
        f"{nymex_section}"
        f"USD/PHP: {usd_php} | Peso {peso_dir}"
    )

# ── Telegram Sender ─────────────────────────────────────────────────────
def send_telegram(message: str) -> None:
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = session.post(url, json=data, timeout=30)
        r.raise_for_status()
        print("Forecast sent to Telegram.")
    except requests.exceptions.RequestException as e:
        print(f"Telegram failed: {e}")

# ── Main ───────────────────────────────────────────────────────────────
def main():
    now = datetime.now(ZoneInfo("Asia/Manila"))

    print("Fetching data...")
    official = get_official_adjustment()

    # Primary: scrape news consensus
    print("\n--- Scraping news for forecast ---")
    consensus = scrape_news_consensus(now)

    # Always fetch NYMEX for context
    print("\n--- Fetching NYMEX data ---")
    ho_latest, gasoil_change, rb_latest, mogas_change, kerosene_change = get_mops_proxies(now)
    usd_php = get_usd_php()

    # Pick forecast method
    if consensus and (consensus.get("diesel") or consensus.get("gasoline")):
        print("\n[OK] Using NEWS consensus for forecast")
        forecast = calculate_forecast_from_news(consensus)
    else:
        print("\n[FALLBACK] No news data, using NYMEX formula")
        forecast = calculate_forecast_from_nymex(gasoil_change, mogas_change, kerosene_change, usd_php)

    message = build_message(now, ho_latest, gasoil_change, rb_latest, mogas_change,
                            usd_php, official, forecast)

    print("\n--- MESSAGE PREVIEW ---")
    print(message.encode("utf-8", errors="replace").decode("utf-8"))
    print("-----------------------\n")

    send_telegram(message)

if __name__ == "__main__":
    main()
