"""
Reddit API Poster (PRAW)
========================
Posts RedGifs links to Reddit subreddits using the Reddit API directly.
No browser, no captcha, no UI elements to break.

Requires: Reddit app credentials (client_id + client_secret) per account.
Create at: https://www.reddit.com/prefs/apps (create "script" type app)

Usage:
    python reddit_api_poster.py --post
    python reddit_api_poster.py --post --max 5
    python reddit_api_poster.py --post --dry-run
"""

import praw
import requests
import os
import argparse
import json
import time
from typing import List, Dict, Optional
from datetime import datetime, timezone
from urllib.parse import quote


class RedditAPIPoster:
    """Posts links to Reddit via PRAW (Reddit API)."""

    def __init__(
        self,
        airtable_token: str,
        airtable_base: str,
        links_table: str = "Links",
        accounts_table: str = "Accounts",
    ):
        self.airtable_token = airtable_token
        self.airtable_base = airtable_base
        self.links_table = links_table
        self.accounts_table = accounts_table
        self.at_headers = {
            "Authorization": f"Bearer {airtable_token}",
            "Content-Type": "application/json",
        }

    def _at_url(self, table: str) -> str:
        return f"https://api.airtable.com/v0/{self.airtable_base}/{quote(table)}"

    def _at_get(self, table: str, filter_formula: str = None) -> List[Dict]:
        """Fetch records from Airtable with pagination."""
        records = []
        offset = None
        while True:
            params = {"pageSize": 100}
            if filter_formula:
                params["filterByFormula"] = filter_formula
            if offset:
                params["offset"] = offset
            resp = requests.get(self._at_url(table), headers=self.at_headers, params=params, timeout=30)
            if resp.status_code != 200:
                break
            data = resp.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break
        return records

    def _at_update_single(self, table: str, record_id: str, fields: Dict) -> bool:
        url = f"{self._at_url(table)}/{record_id}"
        payload = {"fields": fields, "typecast": True}
        resp = requests.patch(url, headers=self.at_headers, json=payload, timeout=30)
        return resp.status_code == 200

    def get_queued_links(self, max_results: int = None) -> List[Dict]:
        """Get links with Status='Queued'."""
        formula = '{Status} = "Queued"'
        records = self._at_get(self.links_table, filter_formula=formula)
        if max_results:
            records = records[:max_results]
        print(f"  [*] Found {len(records)} queued links")
        return records

    def get_account_info(self, username: str) -> Optional[Dict]:
        """Get account info from Accounts table."""
        formula = f'{{Username}} = "{username}"'
        records = self._at_get(self.accounts_table, filter_formula=formula)
        if records:
            return records[0]
        return None

    def create_reddit_client(self, username: str, password: str,
                             client_id: str, client_secret: str,
                             user_agent: str = None) -> praw.Reddit:
        """Create an authenticated Reddit API client."""
        if not user_agent:
            user_agent = f"python:reddit_poster:v1.0 (by /u/{username})"

        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            username=username,
            password=password,
            user_agent=user_agent,
        )
        return reddit

    def post_link(self, reddit: praw.Reddit, subreddit: str, url: str, title: str) -> tuple:
        """
        Submit a link post to a subreddit.

        Returns (success, reddit_post_url, error_message).
        """
        try:
            subreddit_obj = reddit.subreddit(subreddit)
            submission = subreddit_obj.submit(
                title=title,
                url=url,
            )
            post_url = f"https://www.reddit.com{submission.permalink}"
            return True, post_url, None
        except praw.exceptions.RedditAPIException as e:
            error_msg = str(e)
            return False, None, error_msg
        except Exception as e:
            return False, None, str(e)

    def run(self, max_posts: int = None, dry_run: bool = False):
        """Main posting loop."""
        print(f"\n[*] Reddit API Poster Starting")
        print(f"    Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        print(f"    Max posts: {max_posts or 'Unlimited'}")

        links = self.get_queued_links(max_results=max_posts)
        if not links:
            print("  [-] No queued links found.")
            return

        print(f"\n  [*] Processing {len(links)} links...\n")

        success_count = 0
        fail_count = 0

        for i, link_record in enumerate(links, 1):
            fields = link_record.get("fields", {})
            url = fields.get("URL", "")
            title = fields.get("Title", "")
            account_name = fields.get("Assigned Account", "")
            subreddit_raw = fields.get("Subreddit", "")

            subreddit = subreddit_raw.replace("r/", "").replace("/r/", "").strip() if subreddit_raw else ""

            print(f"\n  [{i}/{len(links)}] Link: {url}")
            print(f"    Account: {account_name}")
            print(f"    Subreddit: r/{subreddit}")
            print(f"    Title: {title}")

            if dry_run:
                print(f"    [DRY RUN] Would post to r/{subreddit}")
                continue

            # Get account info
            account_info = self.get_account_info(account_name)
            if not account_info:
                print(f"    [!] Account '{account_name}' not found in Accounts table — skipping")
                fail_count += 1
                continue

            account_fields = account_info.get("fields", {})

            # Check account status
            if account_fields.get("Account Status") == "Banned":
                print(f"    [!] Account is Banned — skipping")
                fail_count += 1
                continue

            # Get credentials
            password = account_fields.get("Password", "")
            client_id = account_fields.get("Client ID", "")
            client_secret = account_fields.get("Client Secret", "")

            if not password or not client_id or not client_secret:
                print(f"    [!] Missing credentials for '{account_name}'")
                print(f"        Need: Password, Client ID, Client Secret in Airtable")
                print(f"        Create app at: https://www.reddit.com/prefs/apps (type: script)")
                fail_count += 1
                continue

            # Create Reddit client
            try:
                print(f"    [*] Authenticating as u/{account_name}...")
                reddit = self.create_reddit_client(account_name, password, client_id, client_secret)
                print(f"    [+] Authenticated!")
            except Exception as e:
                print(f"    [!] Authentication failed: {e}")
                fail_count += 1
                self._at_update_single(self.links_table, link_record["id"], {"Status": "Failed"})
                continue

            # Post the link
            print(f"    [*] Posting to r/{subreddit}...")
            success, post_url, error = self.post_link(reddit, subreddit, url, title)

            if success:
                print(f"    [+] Post successful!")
                print(f"    [+] URL: {post_url}")
                success_count += 1

                # Update Airtable
                self._at_update_single(self.links_table, link_record["id"], {
                    "Status": "Posted",
                    "Posted At": datetime.now(timezone.utc).isoformat(),
                    "Reddit Post URL": post_url,
                })

                posts_today = account_fields.get("Posts Today", 0) or 0
                total_posts = account_fields.get("Total Posts", 0) or 0
                self._at_update_single(self.accounts_table, account_info["id"], {
                    "Posts Today": posts_today + 1,
                    "Total Posts": total_posts + 1,
                    "Last Post Date": datetime.now(timezone.utc).isoformat(),
                })
            else:
                print(f"    [!] Post failed: {error}")
                fail_count += 1
                self._at_update_single(self.links_table, link_record["id"], {
                    "Status": "Failed",
                })

                # Check if banned
                if "banned" in error.lower() or "suspended" in error.lower():
                    print(f"    [!] Account appears banned — marking as Banned")
                    self._at_update_single(self.accounts_table, account_info["id"], {
                        "Account Status": "Banned",
                    })

            time.sleep(2)  # Small delay between posts

        print(f"\n[*] Complete:")
        print(f"    Success: {success_count}")
        print(f"    Failed:  {fail_count}")
        print(f"    Total:   {len(links)}")


def main():
    parser = argparse.ArgumentParser(description="Reddit API Poster (PRAW)")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID")
    parser.add_argument("--links-table", default="Links", help="Links table name")
    parser.add_argument("--accounts-table", default="Accounts", help="Accounts table name")
    parser.add_argument("--post", action="store_true", help="Run the poster")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--max", type=int, help="Maximum posts this run")

    args = parser.parse_args()

    if not args.token or not args.base_id:
        print("[!] Missing credentials. Use --token and --base-id or set env vars.")
        return

    if not args.post:
        parser.print_help()
        return

    poster = RedditAPIPoster(
        airtable_token=args.token,
        airtable_base=args.base_id,
        links_table=args.links_table,
        accounts_table=args.accounts_table,
    )

    poster.run(max_posts=args.max, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
