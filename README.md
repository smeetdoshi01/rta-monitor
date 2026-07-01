# RTA Allotment Monitor → Telegram

Watches Registrar & Transfer Agent (RTA) websites and sends a Telegram alert the instant a new company name appears in their allotment-status list — i.e., its IPO allotment has just been published.

## RTAs monitored

| RTA | URL |
|---|---|
| Bigshare | ipo.bigshareonline.com |
| Skyline | skylinerta.com |
| Mudra | mudrarta.com |
| Purva Sharegistry | purvashare.com |
| Cameo | ipostatus1.cameoindia.com |
| Integrated Registry | ipostatus.integratedregistry.in |

**MUFG (formerly Link Intime)** not yet included — dropdown is JS-loaded, needs endpoint discovery.

## What you'll receive

**First run per site** (baseline):

> 📡 **Now tracking Bigshare**
> Open page
> 
> 11 companies currently listed for allotment status.
> You'll be alerted whenever a new one appears.

**On new allotment:**

> 🆕 **New allotment(s) at Cameo**
> Open page
> ```
> + TAKYON NETWORKS LIMITED
> + BMW VENTURES LTD
> ```

Silent otherwise. Removals (allotment window closing) are logged but not alerted.

## Setup

1. Create a public GitHub repo, upload these files.
2. **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` — reuse from your BSE monitor
   - `TELEGRAM_CHAT_ID` — reuse from your BSE monitor
3. **Settings → Actions → General → Workflow permissions** → Read and write.
4. **Actions → RTA Allotment Monitor → Run workflow** — you'll get 6 baseline Telegrams (one per RTA).

Runs every 15 min after that.

## Files

- `rta_monitor.py` — scraper, diff, Telegram, state
- `rta_state.json` — auto-generated, stores known companies per RTA
- `requirements.txt` — Python deps (requests, curl_cffi, beautifulsoup4)
- `.github/workflows/rta_monitor.yml` — schedule + runner

## Tweaks

- **Frequency** — cron in `.github/workflows/rta_monitor.yml`
- **Add/remove sites** — edit the `RTAS` dict at the top of `rta_monitor.py`

## Running on VPS instead

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
python rta_monitor.py
```

Cron:
```
*/15 * * * * cd /path/to/rta-monitor && /usr/bin/python3 rta_monitor.py >> monitor.log 2>&1
```
