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
from datetime import datetime, timezone
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
    AUTH_ENDPOINT = "https://api.redgifs.com/v2/auth/temporary"
    NICHES_API = "https://api.redgifs.com/v2/niches"
    SEARCH_API = "https://api.redgifs.com/v2/gifs/search"

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
        self._token = None  # RedGifs temp auth token

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

    def _get_temp_token(self):
        """
        RedGifs v2 API now requires a temporary guest token.
        Hit the auth/temporary endpoint to get one, then set it as Bearer.
        """
        try:
            resp = self.session.get(self.AUTH_ENDPOINT, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            token = data.get("token")
            if token:
                self._token = token
                self.session.headers["Authorization"] = f"Bearer {token}"
                print(f"  [+] Acquired RedGifs temp token")
                return True
            else:
                print(f"  [!] Auth response had no token: {data}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"  [!] Failed to get temp token: {e}")
            return False

    def _resolve_niche_slug(self, niche):
        """
        Resolve a user-provided niche name to a RedGifs niche slug.
        RedGifs niches have slugs like 'blowjobs', 'just-boobs', etc.
        This queries the niches list and matches by name (case-insensitive).

        Caches the full niches list on first call for speed.

        Args:
            niche: user input (e.g. "blowjob", "blowjobs", "Blowjobs")

        Returns:
            niche slug string (e.g. "blowjobs") or None if no match
        """
        if not hasattr(self, '_niches_cache'):
            self._niches_cache = None
            self._niche_tags_cache = {}  # slug → set of related tags

        if self._niches_cache is None:
            self._niches_cache = self._fetch_all_niches()

        niche_lower = niche.lower().strip()

        # Exact slug match
        for n in self._niches_cache:
            if n['id'].lower() == niche_lower:
                return n['id']

        # Name match (case-insensitive)
        for n in self._niches_cache:
            if n['name'].lower() == niche_lower:
                return n['id']

        # Partial match — niche name contains the search term or vice versa
        for n in self._niches_cache:
            if niche_lower in n['name'].lower() or niche_lower in n['id'].lower():
                return n['id']

        # Try singular/plural variants
        if niche_lower.endswith('s'):
            singular = niche_lower[:-1]
            for n in self._niches_cache:
                if singular in n['name'].lower() or singular in n['id'].lower():
                    return n['id']
        else:
            plural = niche_lower + 's'
            for n in self._niches_cache:
                if plural in n['name'].lower() or plural in n['id'].lower():
                    return n['id']

        return None

    def _get_niche_tags(self, slug):
        """
        Fetch the specific niche's detail to get its full related tags list.
        RedGifs assigns each niche a set of related tags — we use these
        to filter results for 100% accuracy.

        Args:
            slug: RedGifs niche slug (e.g. "blowjobs")

        Returns:
            set of lowercase tag strings, or empty set on failure
        """
        if slug in self._niche_tags_cache:
            return self._niche_tags_cache[slug]

        if not self._token:
            self._get_temp_token()

        try:
            resp = self.session.get(f"{self.NICHES_API}/{slug}", timeout=30)
            if resp.status_code != 200:
                return set()

            data = resp.json()
            niche_data = data.get("niche", {})
            tags = niche_data.get("tags", [])

            # Also add the niche name itself as a tag for matching
            niche_name = niche_data.get("name", "")
            if niche_name:
                tags.append(niche_name)

            # Lowercase everything for case-insensitive matching
            tag_set = set(t.lower() for t in tags if t)

            self._niche_tags_cache[slug] = tag_set
            print(f"  [+] Niche '{slug}' related tags: {sorted(tag_set)}")
            return tag_set

        except requests.exceptions.RequestException:
            return set()

    def _fetch_all_niches(self):
        """
        Fetch the full list of RedGifs niches (all pages).
        Caches for the scraper session lifetime.

        Returns:
            list of dicts: [{"id", "name", "gifs", "subscribers", "tags"}, ...]
        """
        if not self._token:
            self._get_temp_token()

        all_niches = []
        page = 1

        while True:
            params = {"count": 100, "page": page}
            try:
                resp = self.session.get(self.NICHES_API, params=params, timeout=30)
                if resp.status_code != 200:
                    break

                data = resp.json()
                niches = data.get("niches", [])
                if not niches:
                    break

                all_niches.extend(niches)

                total_pages = data.get("pages", 1)
                if page >= total_pages:
                    break

                page += 1
                time.sleep(0.3)  # Be nice during niche discovery

            except requests.exceptions.RequestException:
                break

        print(f"  [+] Cached {len(all_niches)} RedGifs niches")
        return all_niches

    def list_niches(self):
        """
        Print all available RedGifs niches with their GIF counts.
        Useful for discovering what's available.

        Usage:
            python scraper.py --list-niches
        """
        if not self._token:
            self._get_temp_token()

        niches = self._fetch_all_niches()
        print(f"\n[*] {len(niches)} RedGifs Niches Available:\n")
        print(f"{'Slug':<30} {'Name':<30} {'GIFs':>10} {'Subs':>10}")
        print("-" * 85)
        for n in sorted(niches, key=lambda x: x.get('gifs', 0), reverse=True):
            print(f"{n['id']:<30} {n['name']:<30} {n.get('gifs', 0):>10,} {n.get('subscribers', 0):>10,}")

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
        Fetch a single page of RedGifs results for a niche.
        Uses the niches feed endpoint for tag-accurate results.
        Falls back to search endpoint if the niche slug isn't found.

        This is a raw fetch — no filtering. Filtering happens in scrape_niche.

        Args:
            niche: search term (e.g. "blowjob")
            count: results per page (max 100 per API)
            order: "top" | "new" | "trending" | "best"
            page: page number for pagination

        Returns:
            list of raw result dicts (unfiltered)
        """
        # Normalize the niche to find the RedGifs niche slug
        niche_slug = self._resolve_niche_slug(niche)

        if niche_slug:
            endpoint = f"{self.NICHES_API}/{niche_slug}/gifs"
            params = {"order": order, "count": count, "page": page}
        else:
            # Fallback to search endpoint
            endpoint = self.SEARCH_API
            params = {"search": niche, "order": order, "count": count, "page": page}

        endpoint_name = 'niches' if niche_slug else 'search'
        print(f"  [*] Fetching {niche} | endpoint={endpoint_name} | order={order} | count={count} | page={page}")

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
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            })

        print(f"  [+] Got {len(results)} results for '{niche}' (page {page})")
        return results

    def scrape_niche(self, niche, target_count=50, order="top"):
        """
        Scrape a niche until target_count is reached, with hybrid tag filtering.
        
        Hybrid approach:
        1. Fetch from niche feed (curated by RedGifs)
        2. Filter results to only keep items matching the niche's related tags
        3. If filtered too many, keep paginating to compensate
        4. Fallback to search endpoint if niche slug not found
        5. Dedup by gif_id across all pages

        Args:
            niche: search term
            target_count: how many unique, tag-matched items to collect
            order: sort order

        Returns:
            list of result dicts (100% tag-accurate)
        """
        # Acquire temp token if we don't have one yet
        if not self._token:
            if not self._get_temp_token():
                print(f"  [!] Cannot scrape without auth token — aborting niche '{niche}'")
                return []

        # Resolve niche slug
        niche_slug = self._resolve_niche_slug(niche)

        # Get the niche's related tags for filtering
        filter_tags = set()
        if niche_slug:
            filter_tags = self._get_niche_tags(niche_slug)
            if not filter_tags:
                # Fallback: use the niche name itself
                filter_tags = {niche.lower(), niche_slug.lower().replace('-', ' ')}

        all_results = []
        seen_ids = set()
        page = 1
        per_page = min(target_count * 2, 100)  # Over-fetch to compensate for filtered items
        consecutive_empty = 0
        total_filtered = 0
        total_dupes = 0

        while len(all_results) < target_count:
            # Calculate how many we still need, over-fetch 2x to account for filtering
            needed = target_count - len(all_results)
            fetch_count = min(needed * 2, 100)
            fetch_count = max(fetch_count, 10)  # Always fetch at least 10 per page

            batch = self.search_niche(niche, count=fetch_count, order=order, page=page)

            if not batch:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    print(f"  [-] 3 empty pages in a row — stopping early for '{niche}'")
                    break
                print(f"  [-] No more results for '{niche}' at page {page}")
                break
            consecutive_empty = 0

            # Filter by niche tags (hybrid accuracy)
            new_items = []
            dupe_count = 0
            tag_filtered = 0

            for item in batch:
                gid = item.get("gif_id")

                # Dedup check
                if gid and gid in seen_ids:
                    dupe_count += 1
                    continue

                # Tag accuracy filter
                if filter_tags:
                    item_tags = set(t.lower() for t in item.get("tags", []))
                    if not item_tags & filter_tags:
                        # Item has no overlapping tags with the niche's related tags
                        tag_filtered += 1
                        continue

                if gid:
                    seen_ids.add(gid)
                new_items.append(item)

            if dupe_count:
                print(f"  [=] Filtered {dupe_count} duplicate(s) from page {page}")
                total_dupes += dupe_count
            if tag_filtered:
                print(f"  [=] Filtered {tag_filtered} off-tag item(s) from page {page}")
                total_filtered += tag_filtered

            all_results.extend(new_items)
            # Cap at target count — don't overshoot
            if len(all_results) >= target_count:
                all_results = all_results[:target_count]
                break
            page += 1
            self._rate_limit_sleep()

        if len(all_results) < target_count:
            print(f"  [-] Could only find {len(all_results)} unique tag-matched results for '{niche}' (target was {target_count})")
        if total_filtered or total_dupes:
            print(f"  [=] Total filtered: {total_filtered} off-tag, {total_dupes} duplicates")

        return all_results

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
    parser.add_argument("--list-niches", action="store_true",
                        help="List all available RedGifs niches and exit")

    args = parser.parse_args()

    # Handle --list-niches
    if args.list_niches:
        scraper = RedGifsScraper(
            delay_range=(args.delay_min, args.delay_max),
        )
        scraper.list_niches()
        return

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
