"""
RTA Allotment Monitor → Telegram

Watches multiple Registrar & Transfer Agent (RTA) websites and alerts when
a new company name appears in their allotment-status dropdown/table. That
means the allotment for that IPO has just been published.

Sites covered:
  - Bigshare Online
  - Skyline Financial Services
  - Mudra RTA
  - Purva Sharegistry
  - Cameo Corporate Services
  - Integrated Registry Management Services

TODO: MUFG (formerly Link Intime) — needs endpoint discovery, dropdown is
JS-loaded.
"""
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from curl_cffi import requests as http_client
    USE_IMPERSONATE = True
except ImportError:
    import requests as http_client
    USE_IMPERSONATE = False

import requests as tg_requests
from bs4 import BeautifulSoup


STATE_FILE = "rta_state.json"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# A company name looks like something that contains one of these tokens.
# Filters out language options, "Select PAN", etc.
COMPANY_TOKENS = ("LIMITED", "LTD", "PVT", "PRIVATE")


# ---------- Parsers ----------

def _companies_from_dropdown(soup):
    """
    Find the <select> whose options look like company names, return them
    as a list (in original order).
    """
    for select in soup.find_all("select"):
        options = [opt.get_text().strip() for opt in select.find_all("option")]
        company_like = [
            o for o in options
            if o and any(tok in o.upper() for tok in COMPANY_TOKENS)
        ]
        if company_like:
            return company_like
    return []


def parse_bigshare(html):
    return _companies_from_dropdown(BeautifulSoup(html, "html.parser"))


def parse_skyline(html):
    return _companies_from_dropdown(BeautifulSoup(html, "html.parser"))


def parse_mudra(html):
    return _companies_from_dropdown(BeautifulSoup(html, "html.parser"))


def parse_purva(html):
    return _companies_from_dropdown(BeautifulSoup(html, "html.parser"))


def parse_cameo(html):
    return _companies_from_dropdown(BeautifulSoup(html, "html.parser"))


def fetch_kfin_companies():
    """
    KFin's IPO Allotment Status page is a React SPA. The company list is
    NOT fetched via API — it's embedded directly in the main JS bundle as
    a JSON.parse('[{"clientId":"...","name":"..."}, ...]') call.

    Steps:
      1. Fetch the HTML at ipostatus.kfintech.com/
      2. Extract the main JS bundle path from a <script src="..."> tag
         (filename has a hash that changes on redeploy)
      3. Fetch that bundle
      4. Locate the JSON.parse call with clientId + name entries
      5. Parse it out and return the names
    """
    import re

    base = "https://ipostatus.kfintech.com"

    def _get(url):
        kwargs = {"headers": HEADERS, "timeout": 30}
        if USE_IMPERSONATE:
            kwargs["impersonate"] = "chrome124"
        r = http_client.get(url, **kwargs)
        r.raise_for_status()
        return r.text

    try:
        html = _get(base + "/")
    except Exception as e:
        print(f"  ✗ HTML fetch failed: {e}")
        return []

    m = re.search(r'src="([^"]*main\.[a-z0-9]+\.js)"', html)
    if not m:
        print("  ✗ main JS bundle URL not found in HTML")
        return []
    bundle_path = m.group(1).lstrip("./")
    bundle_url = f"{base}/{bundle_path}"
    print(f"  · bundle: {bundle_path}")

    try:
        js = _get(bundle_url)
    except Exception as e:
        print(f"  ✗ bundle fetch failed: {e}")
        return []

    # Find JSON.parse('[{...clientId...name...}]') — the array with the IPOs
    idx = js.find("JSON.parse('[{\\\"clientId")
    if idx < 0:
        # Try the un-escaped variant just in case
        idx = js.find("JSON.parse('[{\"clientId")
    if idx < 0:
        print("  ✗ JSON.parse block for IPO list not found in bundle")
        return []

    # From idx, find the first ' after "JSON.parse('", then the matching closing '
    quote_start = js.find("'", idx)
    if quote_start < 0:
        return []
    quote_end = js.find("']", quote_start + 1)
    if quote_end < 0:
        return []
    literal = js[quote_start + 1: quote_end + 1]

    try:
        entries = json.loads(literal)
    except Exception as e:
        print(f"  ✗ JSON parse failed: {e}")
        return []

    names = []
    for entry in entries:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
            if name:
                names.append(name)
    return names


def fetch_mufg_companies():
    """
    MUFG (formerly Link Intime) uses a two-step API:
      1. POST /Initial_Offer/IPO.aspx/generateToken → returns {"d": "<token>"}
      2. POST /Initial_Offer/IPO.aspx/GetDetails with the token
         → returns {"d": "<XML with <NewDataSet><Table><companyname>...>"}

    Returns list of company names, or [] on failure.

    Note: 'clientid' is a best-guess payload param name. If the API returns
    empty on the second call, try 'token', 'code', or 'key' in payload_key.
    """
    base = "https://in.mpms.mufg.com/Initial_Offer/IPO.aspx"
    api_headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://in.mpms.mufg.com/Initial_Offer/public-issues.html",
        "Origin": "https://in.mpms.mufg.com",
        "User-Agent": HEADERS["User-Agent"],
    }

    def _post(url, body):
        kwargs = {"headers": api_headers, "timeout": 30,
                  "data": json.dumps(body)}
        if USE_IMPERSONATE:
            kwargs["impersonate"] = "chrome124"
        return http_client.post(url, **kwargs)

    # Step 1: get token
    try:
        r1 = _post(f"{base}/generateToken", {})
        r1.raise_for_status()
        token_wrapper = r1.json()
        token = str(token_wrapper.get("d", "")).strip()
        if not token:
            print(f"  · generateToken returned no token: {token_wrapper}")
            return []
        print(f"  · token acquired: {token[:6]}...")
    except Exception as e:
        print(f"  ✗ generateToken failed: {e}")
        return []

    # Step 2: get company list. Try likely param names.
    # From what we've seen in the response, 'clientid' is the most common
    # Link Intime / MUFG convention. Fall back to other names if empty.
    xml_str = ""
    for payload_key in ("clientid", "token", "clientId", "key"):
        try:
            r2 = _post(f"{base}/GetDetails", {payload_key: token})
            r2.raise_for_status()
            wrapper = r2.json()
            candidate = str(wrapper.get("d", "")).strip()
            if candidate and "<NewDataSet" in candidate:
                xml_str = candidate
                print(f"  · GetDetails worked with payload key '{payload_key}'")
                break
            else:
                print(f"  · GetDetails empty with '{payload_key}': {candidate[:80]}")
        except Exception as e:
            print(f"  · GetDetails errored with '{payload_key}': {e}")

    if not xml_str:
        return []

    # Parse XML: <NewDataSet><Table><companyname>Name</companyname></Table>...
    try:
        root = ET.fromstring(xml_str)
        names = []
        for table in root.findall(".//Table"):
            cn = table.find("companyname")
            if cn is not None and cn.text:
                names.append(cn.text.strip())
        return names
    except ET.ParseError as e:
        print(f"  ✗ XML parse failed: {e}")
        return []


def parse_integrated(html):
    """
    Integrated Registry doesn't use tables. The "IPO Allotment Advertisement"
    list lives inside:
        <section class="NewsAd CommonSectionCls">
          <div class="Circular_FlexTable">
            <div class="Circular-flex-Row">
              <div class="Circular-cell">1</div>
              <div class="Circular-cell">Company Name Limited</div>
              <div class="Circular-cell FlixDiv">...download PDF...</div>
    We extract company names from that specific section.
    """
    soup = BeautifulSoup(html, "html.parser")
    names = []
    seen = set()

    section = soup.find("section", class_="NewsAd")
    if section is None:
        return []

    for cell in section.find_all("div", class_="Circular-cell"):
        text = cell.get_text().strip()
        if not text or len(text) > 200:
            continue
        upper = text.upper()
        if not any(tok in upper for tok in COMPANY_TOKENS):
            continue
        if text not in seen:
            seen.add(text)
            names.append(text)
    return names


RTAS = {
    "MUFG": {
        "custom_fetcher": fetch_mufg_companies,
        "url": "https://in.mpms.mufg.com/Initial_Offer/public-issues.html",
    },
    "KFin": {
        "custom_fetcher": fetch_kfin_companies,
        "url": "https://ipostatus.kfintech.com/",
    },
    "Bigshare": {
        "url": "https://ipo.bigshareonline.com/ipo_status.html",
        "parser": parse_bigshare,
    },
    "Skyline": {
        "url": "https://www.skylinerta.com/ipo.php",
        "parser": parse_skyline,
    },
    "Mudra": {
        "url": "https://mudrarta.com/ipo.php",
        "parser": parse_mudra,
    },
    "Purva Sharegistry": {
        "url": "https://www.purvashare.com/investor-service/ipo-query",
        "parser": parse_purva,
    },
    "Cameo": {
        "url": "https://ipostatus1.cameoindia.com/",
        "parser": parse_cameo,
    },
    "Integrated Registry": {
        "url": "https://ipostatus.integratedregistry.in/RegistrarsToSTANew.aspx?od=2",
        "parser": parse_integrated,
    },
}


# ---------- Telegram ----------

def send_telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("⚠️ Telegram creds missing. Message would have been:\n" + message)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(message) > 4000:
        message = message[:4000] + "\n\n... (truncated)"
    try:
        r = tg_requests.post(api, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=20)
        if not r.ok:
            print(f"Telegram error {r.status_code}: {r.text}")
    except Exception as e:
        print(f"Telegram exception: {e}")


# ---------- State ----------

def load_state():
    p = Path(STATE_FILE)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state):
    Path(STATE_FILE).write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))


# ---------- Fetching ----------

def fetch_html(url):
    try:
        kwargs = {"headers": HEADERS, "timeout": 30}
        if USE_IMPERSONATE:
            kwargs["impersonate"] = "chrome124"
        r = http_client.get(url, **kwargs)
        r.raise_for_status()
        return r.text
    except Exception as e:
        print(f"  ✗ fetch failed: {e}")
        return None


# ---------- Normalization ----------

def normalize_name(s):
    """Make company names comparable across small formatting differences."""
    s = s.upper().strip()
    # Strip common suffix markers that toggle without being real changes
    for suffix in (" (INACTIVE)", " (ACTIVE)"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    # Collapse whitespace
    s = " ".join(s.split())
    return s


# ---------- Per-RTA check ----------

def check_rta(name, config, state):
    print(f"→ {name}")
    # MUFG uses a custom API-based fetcher instead of HTML parsing
    if "custom_fetcher" in config:
        try:
            companies = config["custom_fetcher"]()
        except Exception as e:
            print(f"  ✗ custom fetcher error: {e}")
            return
        html = ""  # for debug save consistency
    else:
        html = fetch_html(config["url"])
        if not html:
            return
        try:
            companies = config["parser"](html)
        except Exception as e:
            print(f"  ✗ parser error: {e}")
            return

    if not companies:
        # Save the HTML so we can see what the parser missed
        try:
            import re
            from pathlib import Path as _P
            debug_dir = _P("debug")
            debug_dir.mkdir(exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_-]", "_", name)
            (debug_dir / f"{safe}_empty.html").write_text(html, encoding="utf-8")
            print(f"  ⚠ no companies extracted → debug/{safe}_empty.html saved")
        except Exception as e:
            print(f"  ⚠ no companies extracted (couldn't save debug: {e})")
        return

    # Map normalized_key -> display_name so we alert with the pretty name
    current = {}
    for c in companies:
        current[normalize_name(c)] = c
    current_keys = set(current.keys())

    entry = state.get(name, {})
    prev_keys = set(entry.get("companies", []))

    if not prev_keys:
        # First run — capture baseline, one Telegram summary
        state[name] = {
            "companies": sorted(current_keys),
            "last_checked": datetime.now(IST).isoformat(),
        }
        send_telegram(
            f"📡 <b>Now tracking {name}</b>\n"
            f"<a href=\"{config['url']}\">Open page</a>\n\n"
            f"{len(current_keys)} companies currently listed for allotment status.\n"
            f"You'll be alerted whenever a new one appears."
        )
        print(f"  ✓ baseline captured ({len(current_keys)} companies)")
        return

    new_keys = current_keys - prev_keys
    removed_keys = prev_keys - current_keys

    if new_keys:
        new_display = "\n".join(f"+ {current[k]}" for k in sorted(new_keys))
        send_telegram(
            f"🆕 <b>New allotment(s) at {name}</b>\n"
            f"<a href=\"{config['url']}\">Open page</a>\n\n"
            f"<pre>{new_display}</pre>"
        )
        print(f"  ✓ {len(new_keys)} new: {sorted(new_keys)}")

    if removed_keys:
        # Just log, don't alert — a removed company means an old IPO cleared
        # from the RTA's listing, which isn't actionable.
        print(f"  · {len(removed_keys)} dropped from listing: {sorted(removed_keys)}")

    if not new_keys and not removed_keys:
        print(f"  · no change ({len(current_keys)} companies)")

    state[name] = {
        "companies": sorted(current_keys),
        "last_checked": datetime.now(IST).isoformat(),
    }


# ---------- Main ----------

def main():
    state = load_state()
    print(f"Checking {len(RTAS)} RTA sites (impersonate={'on' if USE_IMPERSONATE else 'off'})")
    for rta_name, config in RTAS.items():
        try:
            check_rta(rta_name, config, state)
        except Exception as e:
            print(f"  ✗ unexpected error on {rta_name}: {e}")
    save_state(state)
    print("Done.")


if __name__ == "__main__":
    main()
