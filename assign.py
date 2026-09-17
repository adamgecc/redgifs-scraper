"""
Auto-Assignment Module
=====================
Auto-assigns scraped RedGifs links to Reddit accounts based on niche matching.

Flow:
  1. Find all links with Status = "Scraped" (not yet assigned)
  2. Find all accounts with matching niche + Account Status = "Ready" or "Active"
  3. Distribute links round-robin across accounts (1 per account per day)
  4. Set Status = "Queued", fill Assigned Account, Assigned Employee, Subreddit
  5. Respect Max Posts Per Day limit per account

Usage:
    python assign.py --auto
    python assign.py --auto --dry-run    # Preview without writing
    python assign.py --auto --niche blowjob  # Assign only specific niche
"""

import requests
import os
import argparse
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional


class AutoAssigner:
    """Auto-assigns scraped links to Reddit accounts via Airtable."""

    API_BASE = "https://api.airtable.com/v0"

    def __init__(self, token: str, base_id: str, links_table: str = "Links",
                 accounts_table: str = "Accounts"):
        self.token = token
        self.base_id = base_id
        self.links_table = links_table
        self.accounts_table = accounts_table
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _url(self, table: str) -> str:
        from urllib.parse import quote
        return f"{self.API_BASE}/{self.base_id}/{quote(table)}"

    def _get_all_records(self, table: str, filter_formula: str = None) -> List[Dict]:
        """Fetch all records from a table with pagination."""
        records = []
        offset = None

        while True:
            params = {"pageSize": 100}
            if filter_formula:
                params["filterByFormula"] = filter_formula
            if offset:
                params["offset"] = offset

            resp = requests.get(self._url(table), headers=self.headers, params=params, timeout=30)
            if resp.status_code != 200:
                print(f"  [!] Fetch failed for '{table}': HTTP {resp.status_code} — {resp.text[:200]}")
                break

            data = resp.json()
            records.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        return records

    def _update_record(self, table: str, record_id: str, fields: Dict) -> bool:
        """Update a single record."""
        url = f"{self._url(table)}/{record_id}"
        payload = {"fields": fields, "typecast": True}
        resp = requests.patch(url, headers=self.headers, json=payload, timeout=30)
        return resp.status_code == 200

    def _batch_update(self, table: str, updates: List[Dict]) -> int:
        """Batch update up to 10 records at once."""
        url = self._url(table)
        updated = 0

        for i in range(0, len(updates), 10):
            batch = updates[i:i + 10]
            payload = {
                "records": [
                    {"id": u["id"], "fields": u["fields"]}
                    for u in batch
                ],
                "typecast": True,
            }
            resp = requests.patch(url, headers=self.headers, json=payload, timeout=30)
            if resp.status_code == 200:
                updated += len(resp.json().get("records", []))
            else:
                print(f"  [!] Batch update failed: HTTP {resp.status_code} — {resp.text[:200]}")

        return updated

    def get_unassigned_links(self, niche: str = None) -> List[Dict]:
        """Get all links with Status = 'Scraped' (not yet assigned)."""
        if niche:
            # Filter by Status = Scraped AND Niche = specified
            formula = f'AND({{Status}} = "Scraped", {{Niche}} = "{niche}")'
        else:
            formula = '{{Status}} = "Scraped"'

        records = self._get_all_records(self.links_table, filter_formula=formula)
        print(f"  [*] Found {len(records)} unassigned links (Status=Scraped)")
        return records

    def get_active_accounts(self, niche: str = None) -> List[Dict]:
        """Get all accounts that are Ready or Active, optionally filtered by niche."""
        if niche:
            formula = f'AND(OR({{Account Status}} = "Ready", {{Account Status}} = "Active"), {{Niche}} = "{niche}")'
        else:
            formula = 'OR({{Account Status}} = "Ready", {{Account Status}} = "Active")'

        records = self._get_all_records(self.accounts_table, filter_formula=formula)
        print(f"  [*] Found {len(records)} active/ready accounts")
        return records

    def _get_account_field(self, account_record: Dict, field: str, default=None):
        """Safely get a field from an account record."""
        return account_record.get("fields", {}).get(field, default)

    def _get_today_str(self) -> str:
        """Get today's date as ISO string for comparison."""
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _account_can_post_today(self, account_record: Dict) -> bool:
        """Check if account hasn't exceeded daily post limit."""
        fields = account_record.get("fields", {})
        posts_today = fields.get("Posts Today", 0) or 0
        max_posts = fields.get("Max Posts Per Day", 1) or 1
        return posts_today < max_posts

    def _parse_subreddits(self, subreddits_field) -> List[str]:
        """Parse subreddits field into a list."""
        if not subreddits_field:
            return []
        if isinstance(subreddits_field, list):
            return [s.strip() for s in subreddits_field if s.strip()]
        return [s.strip() for s in str(subreddits_field).replace(",", "\n").split("\n") if s.strip()]

    def assign_links(self, niche: str = None, dry_run: bool = False) -> Dict:
        """
        Auto-assign unassigned links to accounts.

        Distribution logic:
        - Round-robin across accounts matching the niche
        - 1 link per account per day (respect Max Posts Per Day)
        - Fill: Assigned Account, Assigned Employee, Subreddit, Status = Queued

        Args:
            niche: only assign links for this niche (None = all niches)
            dry_run: if True, only print what would be assigned without writing

        Returns:
            dict with assignment stats
        """
        print(f"\n[*] Auto-assignment starting...")

        # Get unassigned links
        links = self.get_unassigned_links(niche=niche)
        if not links:
            print("  [-] No unassigned links found")
            return {"assigned": 0, "skipped": 0, "total": 0}

        # Group links by niche
        links_by_niche = {}
        for link in links:
            link_niche = link.get("fields", {}).get("Niche", "Unknown")
            if link_niche not in links_by_niche:
                links_by_niche[link_niche] = []
            links_by_niche[link_niche].append(link)

        total_assigned = 0
        total_skipped = 0
        updates = []

        for niche_name, niche_links in links_by_niche.items():
            print(f"\n  [*] Niche: {niche_name} ({len(niche_links)} links to assign)")

            # Get accounts for this niche
            accounts = self.get_active_accounts(niche=niche_name)
            if not accounts:
                print(f"      [-] No active accounts for '{niche_name}' — skipping {len(niche_links)} links")
                total_skipped += len(niche_links)
                continue

            # Filter accounts that can post today
            available_accounts = [a for a in accounts if self._account_can_post_today(a)]
            if not available_accounts:
                print(f"      [-] All accounts for '{niche_name}' have hit daily limit — skipping")
                total_skipped += len(niche_links)
                continue

            print(f"      [*] {len(available_accounts)} accounts available for assignment")

            # Round-robin assignment
            account_idx = 0
            for link in niche_links:
                # Find next available account (round-robin)
                assigned = False
                attempts = 0

                while attempts < len(available_accounts):
                    account = available_accounts[account_idx % len(available_accounts)]
                    account_idx += 1
                    attempts += 1

                    if not self._account_can_post_today(account):
                        continue

                    # Get account details
                    account_name = self._get_account_field(account, "Account Name", "Unknown")
                    employee = self._get_account_field(account, "Assigned Employee", None)
                    subreddits = self._parse_subreddits(self._get_account_field(account, "Subreddits", ""))

                    # Pick a subreddit (round-robin through account's subs)
                    subreddit = subreddits[0] if subreddits else f"r/{niche_name.lower()}"

                    # Get link's Gif ID for repost check
                    gif_id = link.get("fields", {}).get("Gif ID", "")

                    if dry_run:
                        print(f"      [DRY] Link '{gif_id}' → Account: {account_name}, Employee: {employee}, Sub: {subreddit}")
                    else:
                        updates.append({
                            "id": link["id"],
                            "fields": {
                                "Status": "Queued",
                                "Assigned Account": account_name,
                                "Assigned Employee": employee,
                                "Subreddit": subreddit,
                            }
                        })

                    total_assigned += 1
                    assigned = True
                    break

                if not assigned:
                    total_skipped += 1

        # Batch write assignments
        if updates and not dry_run:
            print(f"\n  [*] Writing {len(updates)} assignments to Airtable...")
            written = self._batch_update(self.links_table, updates)
            print(f"      [+] {written} records updated")
        elif dry_run:
            print(f"\n  [*] Dry run — no changes written")

        stats = {
            "assigned": total_assigned,
            "skipped": total_skipped,
            "total": len(links),
        }

        print(f"\n[*] Assignment complete:")
        print(f"    Assigned: {total_assigned}")
        print(f"    Skipped:  {total_skipped}")
        print(f"    Total:    {len(links)}")

        return stats

    def reset_daily_counts(self):
        """
        Reset 'Posts Today' counter for all accounts.
        Run this via cron at midnight UTC.
        """
        print(f"\n[*] Resetting daily post counts...")

        accounts = self._get_all_records(self.accounts_table)
        updates = []

        for account in accounts:
            posts_today = account.get("fields", {}).get("Posts Today", 0)
            if posts_today and posts_today > 0:
                updates.append({
                    "id": account["id"],
                    "fields": {"Posts Today": 0},
                })

        if updates:
            written = self._batch_update(self.accounts_table, updates)
            print(f"  [+] Reset {written} accounts' daily counts")
        else:
            print(f"  [-] No accounts needed reset")


def main():
    parser = argparse.ArgumentParser(description="Auto-assign RedGifs links to Reddit accounts")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID")
    parser.add_argument("--links-table", default="Links", help="Links table name")
    parser.add_argument("--accounts-table", default="Accounts", help="Accounts table name")
    parser.add_argument("--auto", action="store_true", help="Run auto-assignment")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--niche", type=str, help="Only assign links for this niche")
    parser.add_argument("--reset-daily", action="store_true", help="Reset daily post counts (cron at midnight)")

    args = parser.parse_args()

    if not args.token or not args.base_id:
        print("[!] Missing credentials. Use --token and --base-id or set AIRTABLE_PAT and AIRTABLE_BASE_ID env vars.")
        return

    assigner = AutoAssigner(
        token=args.token,
        base_id=args.base_id,
        links_table=args.links_table,
        accounts_table=args.accounts_table,
    )

    if args.reset_daily:
        assigner.reset_daily_counts()
        return

    if args.auto:
        assigner.assign_links(niche=args.niche, dry_run=args.dry_run)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
