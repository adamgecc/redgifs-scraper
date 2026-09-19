"""
Subreddit Scraper
=================
Scrapes Reddit user profiles to find which subreddits they post to,
then adds those subreddits to the Airtable Subreddits table.

Usage:
    python subreddit_scraper.py --user "username1" --user "username2"
    python subreddit_scraper.py --users "user1,user2,user3"
    python subreddit_scraper.py --user "username" --dry-run
"""

import requests
import json
import os
import argparse
import time
from typing import List, Dict, Optional
from datetime import datetime, timezone


class SubredditScraper:
    """Scrapes subreddits from Reddit user profiles via the public JSON API."""

    REDDIT_API = "https://www.reddit.com"

    def __init__(self, airtable_token: str = None, airtable_base: str = None,
                 subreddits_table: str = "Subreddits"):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

        self.airtable_token = airtable_token
        self.airtable_base = airtable_base
        self.subreddits_table = subreddits_table
        self.at_headers = {
            "Authorization": f"Bearer {airtable_token}",
            "Content-Type": "application/json",
        } if airtable_token else None

    def scrape_user_subreddits(self, username: str, limit: int = 100) -> Dict[str, Dict]:
        """
        Scrape a Reddit user's post history to find which subreddits they post to.
        Uses the public Reddit page (HTML) and falls back to .json API.

        Args:
            username: Reddit username (without u/ prefix)
            limit: max posts to scan

        Returns:
            dict: {subreddit_name: {post_count, nsfw, sample_title, sample_url, is_link, subreddit_subscribers}}
        """
        print(f"\n[*] Scraping u/{username}'s post history...")

        subreddits = {}

        # Try .json endpoint first (works with some users)
        url = f"{self.REDDIT_API}/user/{username}/submitted.json"
        params = {"limit": min(limit, 100), "sort": "new", "t": "all"}

        try:
            resp = self.session.get(url, params=params, timeout=30)

            if resp.status_code == 200:
                try:
                    data = resp.json()
                    posts = data.get("data", {}).get("children", [])

                    if not posts:
                        print(f"  [!] No posts found for u/{username}")
                        return {}

                    for post in posts:
                        p_data = post.get("data", {})
                        subreddit = p_data.get("subreddit", "")
                        if not subreddit:
                            continue

                        if subreddit not in subreddits:
                            subreddits[subreddit] = {
                                "post_count": 0,
                                "nsfw": p_data.get("over_18", False),
                                "sample_title": p_data.get("title", "")[:100],
                                "sample_url": p_data.get("url", ""),
                                "is_link": not p_data.get("is_self", False),
                                "subreddit_subscribers": 0,
                            }

                        subreddits[subreddit]["post_count"] += 1

                    # Pagination
                    after = data.get("data", {}).get("after")
                    pages = 1
                    while after and pages < 10:
                        time.sleep(2)
                        params["after"] = after
                        resp = self.session.get(url, params=params, timeout=30)
                        if resp.status_code != 200:
                            break
                        data = resp.json()
                        posts = data.get("data", {}).get("children", [])
                        if not posts:
                            break
                        for post in posts:
                            p_data = post.get("data", {})
                            subreddit = p_data.get("subreddit", "")
                            if not subreddit:
                                continue
                            if subreddit not in subreddits:
                                subreddits[subreddit] = {
                                    "post_count": 0,
                                    "nsfw": p_data.get("over_18", False),
                                    "sample_title": p_data.get("title", "")[:100],
                                    "sample_url": p_data.get("url", ""),
                                    "is_link": not p_data.get("is_self", False),
                                    "subreddit_subscribers": 0,
                                }
                            subreddits[subreddit]["post_count"] += 1
                        after = data.get("data", {}).get("after")
                        pages += 1

                except Exception:
                    print(f"  [!] JSON parse failed — trying HTML scrape...")
                    subreddits = self._scrape_html(username, limit)

            elif resp.status_code == 403:
                print(f"  [*] JSON API blocked (403) — trying HTML scrape...")
                subreddits = self._scrape_html(username, limit)

            elif resp.status_code == 404:
                print(f"  [!] User u/{username} not found")
                return {}

            else:
                print(f"  [!] HTTP {resp.status_code}")
                subreddits = self._scrape_html(username, limit)

        except requests.exceptions.RequestException as e:
            print(f"  [!] Request failed: {e}")
            return {}

        if not subreddits:
            print(f"  [!] No subreddits found for u/{username}")
            return {}

        print(f"  [+] Found {len(subreddits)} subreddits from {sum(s['post_count'] for s in subreddits.values())} posts")

        # Get subreddit details
        for sub_name in list(subreddits.keys()):
            sub_info = self.get_subreddit_info(sub_name)
            if sub_info:
                subreddits[sub_name]["nsfw"] = sub_info.get("over18", subreddits[sub_name]["nsfw"])
                subreddits[sub_name]["subreddit_subscribers"] = sub_info.get("subscribers", 0)
            time.sleep(1)

        return subreddits

    def _scrape_html(self, username: str, limit: int = 100) -> Dict[str, Dict]:
        """
        Fallback: scrape subreddit names from the HTML profile page.
        Reddit embeds post data in the HTML as JSON in a <script> tag.
        """
        url = f"{self.REDDIT_API}/user/{username}/submitted/"
        subreddits = {}

        try:
            resp = self.session.get(url, timeout=30, params={"sort": "new", "t": "all", "limit": min(limit, 100)})

            if resp.status_code == 404:
                print(f"  [!] User u/{username} not found")
                return {}

            if resp.status_code != 200:
                print(f"  [!] HTML scrape failed: HTTP {resp.status_code}")
                return {}

            # Reddit embeds data in <script> tags as JSON
            text = resp.text

            # Look for subreddit names in the HTML — they appear as data attributes or in JSON
            import re
            import json as jsonmod

            # Try to find the r/<subreddit> pattern in the HTML
            sub_patterns = re.findall(r'/r/([a-zA-Z0-9_]{3,21})', text)
            for sub in set(sub_patterns):
                if sub.lower() not in ('all', 'popular', 'friends', 'mod', 'announcements'):
                    if sub not in subreddits:
                        subreddits[sub] = {
                            "post_count": 1,
                            "nsfw": True,  # Assume NSFW since these are porn accounts
                            "sample_title": "",
                            "sample_url": "",
                            "is_link": True,
                            "subreddit_subscribers": 0,
                        }
                    else:
                        subreddits[sub]["post_count"] += 1

            # Also try to find the JSON blob in the page
            json_match = re.search(r'<script[^>]*>window\.___r\s*=\s*({.*?})</script>', text, re.DOTALL)
            if json_match:
                try:
                    data = jsonmod.loads(json_match.group(1))
                    posts = data.get("posts", {}).get("models", [])
                    for post in posts:
                        sub = post.get("subredditName", post.get("subreddit", ""))
                        if sub and sub not in subreddits:
                            subreddits[sub] = {
                                "post_count": 1,
                                "nsfw": True,
                                "sample_title": post.get("title", "")[:100],
                                "sample_url": post.get("url", ""),
                                "is_link": not post.get("isSelf", False),
                                "subreddit_subscribers": 0,
                            }
                        elif sub:
                            subreddits[sub]["post_count"] += 1
                except:
                    pass

        except requests.exceptions.RequestException as e:
            print(f"  [!] HTML scrape error: {e}")

        return subreddits

    def get_subreddit_info(self, subreddit: str) -> Optional[Dict]:
        """Fetch subreddit info (subscribers, NSFW status) via public API."""
        url = f"{self.REDDIT_API}/r/{subreddit}/about.json"
        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                return {
                    "subscribers": data.get("subscribers", 0),
                    "over18": data.get("over18", False),
                    "description": data.get("public_description", ""),
                    "subreddit_type": data.get("subreddit_type", ""),
                }
        except:
            pass
        return None

    def guess_niche(self, subreddit: str, description: str = "", tags: str = "") -> str:
        """Guess the niche of a subreddit based on its name and description."""
        text = f"{subreddit} {description} {tags}".lower()

        niche_keywords = {
            "Blowjob": ["blowjob", "blowjobs", "oral", "sucking", "fellatio", "swordswallow", "deepthroat"],
            "Blonde": ["blonde", "blondes"],
            "Boobs": ["boobs", "tits", "breasts", "busty", "boobies", "chest"],
            "Pussy": ["pussy", "vagina", "cameltoe"],
            "Hardcore": ["hardcore", "rough", "pounding"],
            "Threesome": ["threesome", "threesomes", "group", "gangbang"],
            "Amateur": ["amateur", "homemade", "real"],
            "MILF": ["milf", "mature", "mom", "cougar"],
            "Teen": ["teen", "teens", "young", "18"],
            "Anal": ["anal", "assfuck", "buttfuck"],
            "Cumshot": ["cumshot", "cum", "facial", "creampie"],
            "Lesbian": ["lesbian", "lesbians", "girlongirl", "girl on girl"],
            "Ebony": ["ebony", "black", "bbc"],
            "Asian": ["asian", "japanese", "korean", "chinese"],
            "Latina": ["latina", "latin", "mexican", "brazilian"],
            "PAWG": ["pawg", "thick", "curvy", "booty", "ass"],
            "POV": ["pov", "point of view"],
            "Creampie": ["creampie", "creampies"],
            "Deepthroat": ["deepthroat", "deep throat", "swordswallow", "swallow"],
            "General": ["porn", "nsfw", "sex", "fuck", "xxx", "gonewild"],
        }

        for niche, keywords in niche_keywords.items():
            if any(kw in text for kw in keywords):
                return niche

        return "General"

    def add_to_airtable(self, subreddits: Dict[str, Dict], dry_run: bool = False) -> int:
        """Add scraped subreddits to the Airtable Subreddits table."""
        if not self.airtable_token or not self.airtable_base:
            print("[!] No Airtable credentials — skipping upload")
            return 0

        # Get existing subreddits to avoid duplicates
        existing = set()
        url = f"https://api.airtable.com/v0/{self.airtable_base}/{self.subreddits_table}"
        offset = None
        while True:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset
            resp = requests.get(url, headers=self.at_headers, params=params, timeout=30)
            if resp.status_code != 200:
                break
            data = resp.json()
            for r in data.get("records", []):
                existing.add(r.get("fields", {}).get("Subreddit", "").lower())
            offset = data.get("offset")
            if not offset:
                break

        print(f"  [*] {len(existing)} subreddits already in Airtable")

        # Filter out duplicates
        new_subreddits = []
        for sub_name, info in subreddits.items():
            if sub_name.lower() in existing:
                print(f"    [=] r/{sub_name} already exists — skipping")
                continue

            niche = self.guess_niche(sub_name, info.get("sample_title", ""))
            
            new_subreddits.append({
                "fields": {
                    "Subreddit": sub_name,
                    "Full Name": f"r/{sub_name}",
                    "Niche": niche,
                    "NSFW": info.get("nsfw", True),
                    "Active": info.get("post_count", 0) > 0,
                    "Subscribers": info.get("subreddit_subscribers", 0),
                    "Requires Link Post": not info.get("is_link", True),
                    "Allows Text Post": info.get("is_link", True),
                    "Notes": f"Found from user profile | Posts: {info['post_count']} | Sample: {info.get('sample_title', '')[:50]}",
                }
            })

        if not new_subreddits:
            print("  [-] No new subreddits to add")
            return 0

        if dry_run:
            print(f"\n  [DRY RUN] Would add {len(new_subreddits)} subreddits:")
            for s in new_subreddits:
                f = s["fields"]
                print(f"    {f['Full Name']:<25} | Niche: {f['Niche']:<12} | NSFW: {f['NSFW']} | Subs: {f['Subscribers']}")
            return len(new_subreddits)

        # Push to Airtable in batches of 10
        pushed = 0
        for i in range(0, len(new_subreddits), 10):
            batch = new_subreddits[i:i + 10]
            resp = requests.post(url, headers=self.at_headers, json={
                "records": batch,
                "typecast": True,
            }, timeout=30)
            if resp.status_code == 200:
                pushed += len(resp.json().get("records", []))
                print(f"    [+] Pushed batch {i//10 + 1} ({len(resp.json().get('records', []))} subreddits)")
            else:
                print(f"    [!] Batch failed: {resp.status_code} — {resp.text[:200]}")
            time.sleep(0.5)

        print(f"\n  [+] Added {pushed} new subreddits to Airtable")
        return pushed

    def scrape_and_add(self, usernames: List[str], dry_run: bool = False) -> Dict[str, int]:
        """
        Scrape multiple Reddit users and add their subreddits to Airtable.

        Args:
            usernames: list of Reddit usernames
            dry_run: if True, don't write to Airtable

        Returns:
            dict: {username: number_of_subreddits_found}
        """
        all_subreddits = {}
        results = {}

        for username in usernames:
            username = username.strip().replace("u/", "").replace("/u/", "")
            subs = self.scrape_user_subreddits(username)
            results[username] = len(subs)

            for sub_name, info in subs.items():
                if sub_name not in all_subreddits:
                    all_subreddits[sub_name] = info
                else:
                    all_subreddits[sub_name]["post_count"] += info["post_count"]

            time.sleep(3)  # Rate limit between users

        print(f"\n[*] Total unique subreddits found: {len(all_subreddits)}")

        # Sort by post count (most active subreddits first)
        sorted_subs = dict(sorted(all_subreddits.items(), key=lambda x: x[1]["post_count"], reverse=True))

        # Add to Airtable
        added = self.add_to_airtable(sorted_subs, dry_run=dry_run)

        return {"users_scraped": len(usernames), "subreddits_found": len(all_subreddits), "subreddits_added": added}


def main():
    parser = argparse.ArgumentParser(description="Scrape subreddits from Reddit user profiles")
    parser.add_argument("--user", action="append", help="Reddit username (can be used multiple times)")
    parser.add_argument("--users", help="Comma-separated Reddit usernames")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID")
    parser.add_argument("--dry-run", action="store_true", help="Don't write to Airtable")
    parser.add_argument("--limit", type=int, default=100, help="Max posts to scan per user")

    args = parser.parse_args()

    # Collect usernames
    usernames = []
    if args.users:
        usernames.extend(args.users.split(","))
    if args.user:
        usernames.extend(args.user)

    if not usernames:
        print("[!] No usernames provided. Use --user or --users")
        return

    scraper = SubredditScraper(
        airtable_token=args.token,
        airtable_base=args.base_id,
    )

    print(f"\n[*] Subreddit Scraper")
    print(f"    Users: {', '.join(usernames)}")
    print(f"    Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print(f"    Airtable: {'Yes' if args.token and args.base_id else 'No (no upload)'}")

    result = scraper.scrape_and_add(usernames, dry_run=args.dry_run)

    print(f"\n[*] Done!")
    print(f"    Users scraped: {result['users_scraped']}")
    print(f"    Subreddits found: {result['subreddits_found']}")
    print(f"    Subreddits added: {result['subreddits_added']}")


if __name__ == "__main__":
    main()
