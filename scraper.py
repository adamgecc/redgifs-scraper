"""
RedGifs Niche Scraper
=====================
Scrapes RedGifs niche content via the v2 API.
Collects share-link URLs, niche, and title for Reddit repost workflows.

Usage:
    python scraper.py --niche blowjob --count 50 --order top
    python scraper.py --niches blowjob,amateur,milf --count 50
    python scraper.py --config config.yaml

Output: data/{niche}_results.json
"""

import requests
import json
import time
import random
import os
import argparse
from datetime import datetime
from pathlib import Path

# Optional: AdsPower proxy integration
try:
    from adspower_proxy import get_adspower_proxy
    HAS_ADSPOWER = True
except ImportError:
    HAS_ADSPOWER = False

# Optional: Airtable push integration
try:
    from airtable_push import AirtablePush
    HAS_AIRTABLE = True
except ImportError:
    HAS_AIRTABLE = False


class RedGifsScraper:
    """
    RedGifs v2 API scraper.
    Endpoint: https://api.redgifs.com/v2/gifs/search
    """

    BASE_API = "https://api.redgifs.com/v2"
    WATCH_URL_TEMPLATE = "https://www.redgifs.com/watch/{gif_id}"

    def __init__(self, proxy=None, user_agent=None, delay_range=(1.0, 3.0)):
        """
        Args:
            proxy: dict for requests proxies, e.g. {"http": "socks5://...", "https": "socks5://..."}
            user_agent: custom UA string (rotates if None)
            delay_range: (min, max) seconds between API calls for rate-limit friendliness
        """
        self.session = requests.Session()
        self.proxy = proxy
        self.delay_range = delay_range

        # Browser-like headers — RedGifs API is picky about UA
        self.session.headers.update({
            "User-Agent": user_agent or self._random_ua(),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.redgifs.com/",
            "Origin": "https://www.redgifs.com",
            "Connection": "keep-alive",
        })

        if proxy:
            self.session.proxies.update(proxy)

    @staticmethod
    def _random_ua():
        """Rotate through realistic browser UAs."""
        uas = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        ]
        return random.choice(uas)

    def _rate_limit_sleep(self):
        """Randomized delay between requests to avoid hammering the API."""
        delay = random.uniform(*self.delay_range)
        time.sleep(delay)

    def search_niche(self, niche, count=50, order="top", page=1):
        """
        Search RedGifs for a niche keyword.

        Args:
            niche: search term (e.g. "blowjob")
            count: results per page (max 100 per API)
            order: "top" | "new" | "trending" | "best"
            page: page number for pagination

        Returns:
            list of dicts: [{"url", "niche", "title"}, ...]
        """
        endpoint = f"{self.BASE_API}/gifs/search"
        params = {
            "search": niche,
            "order": order,
            "count": count,
            "page": page,
        }

        print(f"  [*] Fetching {niche} | order={order} | count={count} | page={page}")

        try:
            resp = self.session.get(endpoint, params=params, timeout=30)
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            print(f"  [!] HTTP {resp.status_code if 'resp' in locals() else '?'} — {e}")
            return []
        except requests.exceptions.RequestException as e:
            print(f"  [!] Request failed: {e}")
            return []

        data = resp.json()
        gifs = data.get("gifs", [])

        results = []
        for gif in gifs:
            gif_id = gif.get("id")
            if not gif_id:
                continue

            share_url = self.WATCH_URL_TEMPLATE.format(gif_id=gif_id)
            title = gif.get("title", "").strip() or f"Untitled_{gif_id}"

            results.append({
                "url": share_url,
                "niche": niche,
                "title": title,
                "gif_id": gif_id,
                "tags": gif.get("tags", []),
                "scraped_at": datetime.utcnow().isoformat() + "Z",
            })

        print(f"  [+] Got {len(results)} results for '{niche}' (page {page})")
        return results

    def scrape_niche(self, niche, target_count=50, order="top"):
        """
        Scrape a niche until target_count is reached, paginating as needed.

        Args:
            niche: search term
            target_count: how many items to collect total
            order: sort order

        Returns:
            list of result dicts
        """
        all_results = []
        page = 1
        per_page = min(target_count, 100)  # API max is 100 per page

        while len(all_results) < target_count:
            needed = target_count - len(all_results)
            fetch_count = min(per_page, needed)

            batch = self.search_niche(niche, count=fetch_count, order=order, page=page)

            if not batch:
                print(f"  [-] No more results for '{niche}' at page {page}")
                break

            all_results.extend(batch)
            page += 1
            self._rate_limit_sleep()

        return all_results[:target_count]

    def scrape_multiple(self, niches, count_per_niche=50, order="top"):
        """
        Scrape multiple niches.

        Args:
            niches: list of niche strings
            count_per_niche: target count per niche
            order: sort order

        Returns:
            dict: {niche: [results]}
        """
        all_data = {}
        for niche in niches:
            print(f"\n[*] Scraping niche: {niche}")
            results = self.scrape_niche(niche, target_count=count_per_niche, order=order)
            all_data[niche] = results
            self._rate_limit_sleep()

        return all_data

    @staticmethod
    def save_results(niche, results, output_dir="data"):
        """Save results to JSON file."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        filename = f"{output_dir}/{niche}_results.json"

        with open(filename, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)

        print(f"  [+] Saved {len(results)} results → {filename}")
        return filename

    @staticmethod
    def save_all(all_data, output_dir="data"):
        """Save all niches to individual files + a combined file."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        all_files = []

        # Individual files per niche
        for niche, results in all_data.items():
            f = RedGifsScraper.save_results(niche, results, output_dir)
            all_files.append(f)

        # Combined file
        combined = []
        for niche, results in all_data.items():
            combined.extend(results)

        combined_file = f"{output_dir}/all_results.json"
        with open(combined_file, "w", encoding="utf-8") as f:
            json.dump(combined, f, indent=2, ensure_ascii=False)

        print(f"\n[+] Combined file: {combined_file} ({len(combined)} total items)")
        return all_files


def main():
    parser = argparse.ArgumentParser(
        description="RedGifs Niche Scraper — collects share links for Reddit reposting"
    )
    parser.add_argument("--niche", type=str, help="Single niche to scrape (e.g. blowjob)")
    parser.add_argument("--niches", type=str, help="Comma-separated niches (e.g. blowjob,amateur,milf)")
    parser.add_argument("--count", type=int, default=50, help="Items per niche (default: 50)")
    parser.add_argument("--order", type=str, default="top",
                        choices=["top", "new", "trending", "best"],
                        help="Sort order (default: top)")
    parser.add_argument("--output", type=str, default="data", help="Output directory (default: data)")
    parser.add_argument("--delay-min", type=float, default=1.0, help="Min delay between requests (default: 1.0s)")
    parser.add_argument("--delay-max", type=float, default=3.0, help="Max delay between requests (default: 3.0s)")
    parser.add_argument("--proxy", type=str, help="Proxy URL (e.g. socks5://127.0.0.1:1080)")
    parser.add_argument("--adspower-profile", type=str, help="AdsPower profile ID to pull proxy from")
    parser.add_argument("--ua", type=str, help="Custom User-Agent string")

    # Airtable push args
    parser.add_argument("--airtable", action="store_true",
                        help="Push results to Airtable after scraping")
    parser.add_argument("--airtable-token", type=str,
                        default=os.getenv("AIRTABLE_PAT"),
                        help="Airtable PAT (or set AIRTABLE_PAT env var)")
    parser.add_argument("--airtable-base", type=str,
                        default=os.getenv("AIRTABLE_BASE_ID"),
                        help="Airtable base ID (or set AIRTABLE_BASE_ID env var)")
    parser.add_argument("--airtable-table", type=str,
                        default=os.getenv("AIRTABLE_TABLE_NAME", "Table 1"),
                        help='Airtable table name (default: "Table 1")')
    parser.add_argument("--no-dedup", action="store_true",
                        help="Skip Airtable deduplication (push all records)")

    args = parser.parse_args()

    # Determine niches to scrape
    niches = []
    if args.niches:
        niches = [n.strip() for n in args.niches.split(",") if n.strip()]
    elif args.niche:
        niches = [args.niche.strip()]

    if not niches:
        print("[!] No niches specified. Use --niche or --niches")
        return

    # Proxy setup
    proxy = None
    if args.proxy:
        proxy = {"http": args.proxy, "https": args.proxy}
        print(f"[*] Using proxy: {args.proxy}")
    elif args.adspower_profile and HAS_ADSPOWER:
        proxy = get_adspower_proxy(args.adspower_profile)
        if proxy:
            print(f"[*] AdsPower proxy acquired for profile {args.adspower_profile}")
        else:
            print("[!] Failed to get AdsPower proxy, continuing without proxy")
    elif args.adspower_profile and not HAS_ADSPOWER:
        print("[!] adspower_proxy.py not found — run without --adspower-profile or add the module")

    # Build scraper
    scraper = RedGifsScraper(
        proxy=proxy,
        user_agent=args.ua,
        delay_range=(args.delay_min, args.delay_max),
    )

    print(f"\n[*] RedGifs Scraper Starting")
    print(f"    Niches: {', '.join(niches)}")
    print(f"    Count per niche: {args.count}")
    print(f"    Order: {args.order}")
    print(f"    Output: {args.output}/")
    print(f"    Delay: {args.delay_min}s - {args.delay_max}s")
    print()

    # Scrape
    all_data = scraper.scrape_multiple(niches, count_per_niche=args.count, order=args.order)

    # Save
    scraper.save_all(all_data, output_dir=args.output)

    # Summary
    total = sum(len(v) for v in all_data.values())
    print(f"\n[*] Done! Scraped {total} items across {len(niches)} niche(s)")
    for niche, results in all_data.items():
        print(f"    {niche}: {len(results)} items")

    # Push to Airtable if requested
    if args.airtable:
        if not HAS_AIRTABLE:
            print("\n[!] Airtable module not found — skipping push")
        elif not args.airtable_token or not args.airtable_base:
            print("\n[!] Missing Airtable credentials — set --airtable-token and --airtable-base (or env vars)")
        else:
            print(f"\n[*] Pushing results to Airtable...")
            pusher = AirtablePush(
                token=args.airtable_token,
                base_id=args.airtable_base,
                table_name=args.airtable_table,
            )

            if not pusher.test_connection():
                print("[!] Airtable connection failed — check credentials")
            else:
                # Flatten all results into one list
                all_records = []
                for niche, results in all_data.items():
                    all_records.extend(results)

                pusher.push_records(all_records, dedup=not args.no_dedup)


if __name__ == "__main__":
    main()
