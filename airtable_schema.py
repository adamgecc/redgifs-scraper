"""
Airtable Schema Builder
=======================
Creates the full Airtable schema for the RedGifs → Reddit posting operation.

Tables:
  1. Links     — scraped RedGifs links, assignment status, posting tracking
  2. Accounts  — Reddit accounts, niche/subreddit assignments, warmup status
  3. Employees — VA employees, assigned accounts

Usage:
    python airtable_schema.py --token patXXX --base-id appXXX

    Or with env vars:
    export AIRTABLE_PAT="patXXX"
    export AIRTABLE_BASE_ID="appXXX"
    python airtable_schema.py
"""

import requests
import os
import argparse
import json
import time


class AirtableSchemaBuilder:
    """Creates the full Airtable schema via the Meta API."""

    META_API = "https://api.airtable.com/v0/meta/bases"

    def __init__(self, token: str, base_id: str):
        self.token = token
        self.base_id = base_id
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def _url(self) -> str:
        return f"{self.META_API}/{self.base_id}/tables"

    def create_table(self, name: str, description: str, fields: list) -> dict:
        """
        Create a table with specified fields.

        Args:
            name: table name
            description: table description
            fields: list of field definition dicts

        Returns:
            API response dict
        """
        payload = {
            "name": name,
            "description": description,
            "fields": fields,
        }

        resp = requests.post(self._url(), headers=self.headers, json=payload, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            table_id = data.get("id")
            print(f"  [+] Created table '{name}' (ID: {table_id})")
            return data
        else:
            print(f"  [!] Failed to create '{name}': HTTP {resp.status_code}")
            print(f"      {resp.text[:300]}")
            return None

    def list_tables(self) -> list:
        """List all existing tables in the base."""
        resp = requests.get(self._url(), headers=self.headers, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("tables", [])
        return []

    def delete_table(self, table_id: str) -> bool:
        """Delete a table by ID."""
        url = f"{self._url()}/{table_id}"
        resp = requests.delete(url, headers=self.headers, timeout=10)
        return resp.status_code == 200

    def build_links_table(self) -> dict:
        """
        Links table — all scraped RedGifs links with assignment + posting status.
        This is where the scraper writes to and where employees pick up their queue.
        """
        # Valid Airtable colors: blueLight, blueDark, cyanLight, cyanDark, tealLight, tealDark,
        # purpleLight, purpleDark, pinkLight, pinkDark, redLight, redDark, yellowLight, yellowDark,
        # greenLight, greenDark, grayLight, grayDark, orangeLight, orangeDark
        fields = [
            {"name": "URL", "type": "url", "description": "RedGifs share link"},
            {"name": "Niche", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "Blowjob", "color": "pinkDark"},
                    {"name": "Blonde", "color": "yellowDark"},
                    {"name": "Boobs", "color": "blueDark"},
                    {"name": "Pussy", "color": "purpleDark"},
                    {"name": "Hardcore", "color": "redDark"},
                    {"name": "Threesome", "color": "greenDark"},
                    {"name": "Amateur", "color": "cyanDark"},
                    {"name": "MILF", "color": "orangeDark"},
                    {"name": "Teen", "color": "tealDark"},
                    {"name": "Anal", "color": "redLight"},
                    {"name": "Cumshot", "color": "yellowLight"},
                    {"name": "Lesbian", "color": "pinkLight"},
                    {"name": "Ebony", "color": "grayDark"},
                    {"name": "Asian", "color": "yellowLight"},
                    {"name": "Latina", "color": "orangeLight"},
                    {"name": "PAWG", "color": "blueLight"},
                    {"name": "POV", "color": "grayLight"},
                    {"name": "Creampie", "color": "redDark"},
                ]
            }, "description": "Niche category"},
            {"name": "Title", "type": "singleLineText", "description": "RedGifs GIF title"},
            {"name": "Gif ID", "type": "singleLineText", "description": "RedGifs internal ID"},
            {"name": "Tags", "type": "multilineText", "description": "RedGifs tags (comma-separated)"},
            {"name": "Scraped At", "type": "dateTime", "options": {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}}, "description": "When this link was scraped"},
            {"name": "Status", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "Scraped", "color": "grayLight"},
                    {"name": "Queued", "color": "yellowLight"},
                    {"name": "Posted", "color": "greenLight"},
                    {"name": "Failed", "color": "redLight"},
                    {"name": "Reposted", "color": "blueLight"},
                ]
            }, "description": "Current status: Scraped -> Queued -> Posted/Failed"},
            {"name": "Assigned Account", "type": "singleLineText", "description": "Reddit account name assigned to post this link"},
            {"name": "Assigned Employee", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "VA 1", "color": "blueDark"},
                    {"name": "VA 2", "color": "greenDark"},
                    {"name": "VA 3", "color": "orangeDark"},
                    {"name": "VA 4", "color": "purpleDark"},
                ]
            }, "description": "Employee responsible for posting this link"},
            {"name": "Subreddit", "type": "singleLineText", "description": "Subreddit to post to (e.g. r/blowjobs)"},
            {"name": "Posted At", "type": "dateTime", "options": {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}}, "description": "When the link was posted on Reddit"},
            {"name": "Reddit Post URL", "type": "url", "description": "URL of the Reddit post (filled after posting)"},
            {"name": "Repost Count", "type": "number", "options": {"precision": 0}, "description": "How many times this link has been reposted across accounts"},
            {"name": "Batch ID", "type": "singleLineText", "description": "Scrape batch identifier (auto-generated)"},
        ]

        return self.create_table(
            name="Links",
            description="Scraped RedGifs links — assignment queue for Reddit posting",
            fields=fields,
        )

    def build_accounts_table(self) -> dict:
        """
        Accounts table — Reddit accounts with niche/subreddit assignments.
        You paste your account list here.
        """
        fields = [
            {"name": "Account Name", "type": "singleLineText", "description": "Reddit username"},
            {"name": "Account Status", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "Warming Up", "color": "yellowLight"},
                    {"name": "Ready", "color": "greenLight"},
                    {"name": "Active", "color": "blueLight"},
                    {"name": "Banned", "color": "redDark"},
                    {"name": "Cooldown", "color": "orangeLight"},
                ]
            }, "description": "Account status: Warming Up -> Ready -> Active / Banned / Cooldown"},
            {"name": "Niche", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "Blowjob", "color": "pinkDark"},
                    {"name": "Blonde", "color": "yellowDark"},
                    {"name": "Boobs", "color": "blueDark"},
                    {"name": "Pussy", "color": "purpleDark"},
                    {"name": "Hardcore", "color": "redDark"},
                    {"name": "Threesome", "color": "greenDark"},
                    {"name": "Amateur", "color": "cyanDark"},
                    {"name": "MILF", "color": "orangeDark"},
                    {"name": "Teen", "color": "tealDark"},
                    {"name": "Anal", "color": "redLight"},
                    {"name": "Cumshot", "color": "yellowLight"},
                    {"name": "Lesbian", "color": "pinkLight"},
                    {"name": "Ebony", "color": "grayDark"},
                    {"name": "Asian", "color": "yellowLight"},
                    {"name": "Latina", "color": "orangeLight"},
                    {"name": "PAWG", "color": "blueLight"},
                    {"name": "POV", "color": "grayLight"},
                    {"name": "Creampie", "color": "redDark"},
                ]
            }, "description": "Niche this account is assigned to post"},
            {"name": "Subreddits", "type": "multilineText", "description": "Subreddits this account posts to (one per line or comma-separated)"},
            {"name": "Assigned Employee", "type": "singleSelect", "options": {
                "choices": [
                    {"name": "VA 1", "color": "blueDark"},
                    {"name": "VA 2", "color": "greenDark"},
                    {"name": "VA 3", "color": "orangeDark"},
                    {"name": "VA 4", "color": "purpleDark"},
                ]
            }, "description": "Employee managing this account"},
            {"name": "Posts Today", "type": "number", "options": {"precision": 0}, "description": "Posts made today (reset daily)"},
            {"name": "Total Posts", "type": "number", "options": {"precision": 0}, "description": "Total posts made by this account"},
            {"name": "Max Posts Per Day", "type": "number", "options": {"precision": 0}, "description": "Maximum posts allowed per day (default: 1)"},
            {"name": "Warmup Days", "type": "number", "options": {"precision": 0}, "description": "Days since account started warmup"},
            {"name": "Account Age", "type": "singleLineText", "description": "Account age or creation date"},
            {"name": "Notes", "type": "multilineText", "description": "Notes about this account"},
            {"name": "Last Post Date", "type": "dateTime", "options": {"dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}}, "description": "Date of last successful post"},
            {"name": "Karma", "type": "number", "options": {"precision": 0}, "description": "Current Reddit karma"},
        ]

        return self.create_table(
            name="Accounts",
            description="Reddit accounts — niche/subreddit assignments, warmup status, posting stats",
            fields=fields,
        )

    def build_employees_table(self) -> dict:
        """
        Employees table — VAs managing the accounts.
        """
        fields = [
            {"name": "Name", "type": "singleLineText", "description": "Employee name (VA 1, VA 2, etc.)"},
            {"name": "Accounts Assigned", "type": "number", "options": {"precision": 0}, "description": "Number of accounts assigned to this employee"},
            {"name": "Total Posts", "type": "number", "options": {"precision": 0}, "description": "Total posts made by this employee's accounts"},
            {"name": "Posts Today", "type": "number", "options": {"precision": 0}, "description": "Posts made today by this employee's accounts"},
            {"name": "Shift Hours", "type": "singleLineText", "description": "Shift schedule (e.g. 08:00-16:00 UTC)"},
            {"name": "Active", "type": "checkbox", "options": {"color": "greenBright", "icon": "check"}, "description": "Is this employee currently active?"},
            {"name": "Notes", "type": "multilineText", "description": "Notes about this employee"},
        ]

        return self.create_table(
            name="Employees",
            description="VA employees — account assignments, posting stats, shift info",
            fields=fields,
        )

    def build_all(self, skip_existing=True):
        """Build all three tables. Skips tables that already exist unless skip_existing=False."""
        print(f"\n[*] Building Airtable schema in base {self.base_id}")
        print(f"    Existing tables:")

        existing = self.list_tables()
        existing_names = set()
        for t in existing:
            print(f"      - {t['name']} (ID: {t['id']})")
            existing_names.add(t["name"])

        # Delete existing tables with same names if not skipping
        tables_to_build = [
            ("Links", self.build_links_table),
            ("Accounts", self.build_accounts_table),
            ("Employees", self.build_employees_table),
        ]

        for table_name, builder in tables_to_build:
            if table_name in existing_names and skip_existing:
                print(f"\n  [=] Table '{table_name}' already exists — skipping (use --overwrite to rebuild)")
                continue

            if table_name in existing_names and not skip_existing:
                # Find and delete the existing table
                for t in existing:
                    if t["name"] == table_name:
                        print(f"\n  [-] Deleting existing '{table_name}' (ID: {t['id']})...")
                        self.delete_table(t["id"])
                        time.sleep(0.5)
                        break

            print(f"\n  [*] Creating '{table_name}'...")
            builder()

        print(f"\n[+] Schema build complete!")
        print(f"    Tables: Links, Accounts, Employees")
        print(f"\n[*] Next steps:")
        print(f"    1. Paste your Reddit accounts into the 'Accounts' table")
        print(f"    2. Assign niches + subreddits + employees to each account")
        print(f"    3. Run the scraper: python scraper.py --niche blowjob --count 50 --airtable")
        print(f"    4. Run auto-assignment: python assign.py --auto")


def main():
    parser = argparse.ArgumentParser(description="Build Airtable schema for RedGifs → Reddit operation")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT (or set AIRTABLE_PAT)")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID (or set AIRTABLE_BASE_ID)")
    parser.add_argument("--overwrite", action="store_true", help="Delete and rebuild existing tables")

    args = parser.parse_args()

    if not args.token:
        print("[!] Missing Airtable token. Use --token or set AIRTABLE_PAT env var.")
        return
    if not args.base_id:
        print("[!] Missing Airtable base ID. Use --base-id or set AIRTABLE_BASE_ID env var.")
        return

    builder = AirtableSchemaBuilder(token=args.token, base_id=args.base_id)
    builder.build_all(skip_existing=not args.overwrite)


# unichr helper for Python 3
try:
    unichr
except NameError:
    unichr = chr

if __name__ == "__main__":
    main()
