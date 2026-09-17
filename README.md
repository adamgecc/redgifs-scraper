# 🔗 RedGifs Niche Scraper

![Python](https://img.shields.io/badge/Python-3.8+-blue?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)
![AdsPower](https://img.shields.io/badge/AdsPower-Integrated-orange)

A clean, modular Python scraper for collecting RedGifs niche content via the v2 public API. Gathers share-button links, titles, and metadata for Reddit reposting workflows. Includes AdsPower proxy integration for IP rotation.

## ✨ Features

- **API-First** — Uses RedGifs v2 JSON API (no browser rendering, no Selenium, fast)
- **Multi-Niche** — Scrape single or multiple niches in one run
- **Share Links** — Collects `https://www.redgifs.com/watch/{id}` URLs (same as the share button)
- **Metadata Rich** — URL, niche, title, gif_id, tags, scrape timestamp
- **AdsPower Integration** — Pull proxy config from AdsPower browser profiles automatically
- **Rate-Limit Friendly** — Randomized delays + rotating User-Agents
- **JSON Output** — Per-niche files + combined `all_results.json`
- **Airtable Integration** — Auto-push scraped results to Airtable with deduplication
- **CLI Driven** — Full argparse CLI with sensible defaults

## 📁 Project Structure

```
redgifs_scraper/
├── scraper.py            # Main scraper — RedGifs v2 API
├── adspower_proxy.py      # AdsPower proxy integration module
├── airtable_push.py       # Airtable push module (REST API)
├── airtable_schema.py     # Airtable schema builder (creates tables)
├── assign.py              # Auto-assignment module (links → accounts)
├── reddit_poster.py       # Reddit auto-poster (AdsPower + Playwright)
├── requirements.txt       # Python dependencies
├── LICENSE                # MIT License
├── README.md             # You are here
├── data/                 # Output directory (auto-created)
└── screenshots/           # Error screenshots (auto-created)
```

## 🚀 Quick Start

```bash
# Clone
git clone https://github.com/YOUR_USERNAME/redgifs-scraper.git
cd redgifs-scraper

# Install deps
pip install -r requirements.txt

# Run — single niche
python scraper.py --niche blowjob --count 50 --order top

# Run — multiple niches
python scraper.py --niches blowjob,amateur,milf --count 50

# Run — with proxy
python scraper.py --niche blowjob --count 50 --proxy socks5://127.0.0.1:1080

# Run — with AdsPower profile proxy
python scraper.py --niche blowjob --count 50 --adspower-profile YOUR_PROFILE_ID

# Run — scrape + push to Airtable
python scraper.py --niche blowjob --count 50 --airtable --airtable-token patXXX --airtable-base appXXX --airtable-table "Table 1"

# Run — with env vars for Airtable (recommended)
export AIRTABLE_PAT="patXXX"
export AIRTABLE_BASE_ID="appXXX"
export AIRTABLE_TABLE_NAME="Table 1"
python scraper.py --niche blowjob --count 50 --airtable
```

## ⚙️ CLI Arguments

| Flag | Default | Description |
|------|---------|-------------|
| `--niche` | — | Single niche keyword |
| `--niches` | — | Comma-separated list of niches |
| `--count` | `50` | Items per niche |
| `--order` | `top` | Sort order: `top`, `new`, `trending`, `best` |
| `--output` | `data` | Output directory for JSON files |
| `--delay-min` | `1.0` | Minimum delay between API requests (seconds) |
| `--delay-max` | `3.0` | Maximum delay between API requests (seconds) |
| `--proxy` | — | Proxy URL (e.g. `socks5://127.0.0.1:1080`) |
| `--adspower-profile` | — | AdsPower profile ID to pull proxy from |
| `--ua` | random | Custom User-Agent string |
| `--airtable` | off | Push results to Airtable after scraping |
| `--airtable-token` | `AIRTABLE_PAT` | Airtable Personal Access Token |
| `--airtable-base` | `AIRTABLE_BASE_ID` | Airtable base ID |
| `--airtable-table` | `AIRTABLE_TABLE_NAME` | Airtable table name (default: "Table 1") |
| `--no-dedup` | off | Skip Airtable deduplication |

## 📄 Output Format

Each scraped item is a JSON object:

```json
{
  "url": "https://www.redgifs.com/watch/abc123def",
  "niche": "blowjob",
  "title": "Some GIF Title",
  "gif_id": "abc123def",
  "tags": ["amateur", "homemade", "blowjob"],
  "scraped_at": "2026-09-16T12:00:00Z"
}
```

- `url` — The share-button link, ready to paste into Reddit
- `niche` — The search term used to find this item
- `title` — GIF title from RedGifs (falls back to `Untitled_{id}`)
- `gif_id` — RedGifs internal ID
- `tags` — Tag list from RedGifs metadata
- `scraped_at` — UTC timestamp of when this item was scraped

## 🔌 AdsPower Integration

The `adspower_proxy.py` module connects to AdsPower's local API (port 50325) to retrieve the proxy configuration assigned to a browser profile. This lets you route scraper traffic through the same IP your AdsPower profile uses — perfect for maintaining consistent fingerprints.

### How It Works

1. Query AdsPower local API for profile proxy config
2. Build a `requests`-compatible proxy dict
3. Scraper routes all API calls through that proxy
4. Falls back to direct connection if no proxy or AdsPower not running

### Usage

```bash
# Get your profile ID from AdsPower dashboard
python scraper.py --niche blowjob --count 50 --adspower-profile abc123def456
```

### Standalone Proxy Check

```bash
python adspower_proxy.py YOUR_PROFILE_ID
```

## 🗺️ Roadmap

- [x] Core scraper (RedGifs v2 API)
- [x] AdsPower proxy integration
- [x] Airtable push with deduplication
- [x] Multi-niche batch scraping
- [x] JSON output with metadata
- [ ] Cron scheduling wrapper
- [ ] Reddit posting automation (PRAW)
- [ ] Multi-proxy rotation across AdsPower profiles
- [ ] Auto-niche discovery from RedGifs categories page

## ⚠️ Disclaimer

This tool is for educational purposes and personal workflow automation. Respect RedGifs' Terms of Service and rate limits. Ensure you have the right to repost content. Use proxies responsibly.

## 📜 License

MIT — see [LICENSE](LICENSE).
