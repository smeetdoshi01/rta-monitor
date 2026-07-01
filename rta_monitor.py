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
