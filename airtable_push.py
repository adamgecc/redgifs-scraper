"""
Airtable Push Module
====================
Pushes scraped RedGifs results into an Airtable base.
Uses the Airtable REST API directly (no SDK dependency — just requests).

Schema (matches your table):
  - URL    → Single line text
  - Niche  → Single line text
  - Title  → Single line text

Auth: Airtable Personal Access Token (PAT)
  Create at: https://airtable.com/create/tokens
  Scopes: data.records:write, data.records:read

Usage:
    from airtable_push import AirtablePush

    pusher = AirtablePush(token="pat...", base_id="app...", table_name="Table 1")
    pusher.push_records(results_list)

    # Or via CLI:
    python airtable_push.py --token pat... --base-id app... --table-name "Table 1" --file data/blowjob_results.json
"""

import requests
import json
import os
import argparse
import time
from typing import List, Dict, Optional


class AirtablePush:
    """
    Pushes records to Airtable via the REST API.
    Batches up to 10 records per request (Airtable limit).
    """

    API_BASE = "https://api.airtable.com/v0"
    BATCH_SIZE = 10  # Airtable max records per create request

    def __init__(self, token: str, base_id: str, table_name: str):
        """
        Args:
            token: Airtable Personal Access Token (starts with 'pat...')
            base_id: Airtable base ID (starts with 'app...')
            table_name: Name of the table to push to (e.g. "Table 1")
        """
        self.token = token
        self.base_id = base_id
        self.table_name = table_name

        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        })

    def _url(self) -> str:
        """Build the Airtable records endpoint URL."""
        # URL-encode the table name in case it has spaces
        from urllib.parse import quote
        return f"{self.API_BASE}/{self.base_id}/{quote(self.table_name)}"

    def _transform_record(self, item: Dict) -> Dict:
        """
        Transform a scraper result into Airtable field format.
        Matches your schema: URL, Niche, Title
        """
        return {
            "fields": {
                "URL": item.get("url", ""),
                "Niche": item.get("niche", ""),
                "Title": item.get("title", ""),
            }
        }

    def push_records(self, records: List[Dict], dedup: bool = True) -> Dict:
        """
        Push a list of scraper results to Airtable in batches.

        Args:
            records: list of scraper result dicts (from scraper.py output)
            dedup: if True, skip records whose URL already exists in the table

        Returns:
            dict with push stats: {pushed, skipped, errors, total}
        """
        if not records:
            print("[!] No records to push")
            return {"pushed": 0, "skipped": 0, "errors": 0, "total": 0}

        existing_urls = set()
        if dedup:
            print("[*] Checking existing records for deduplication...")
            existing_urls = self._get_existing_urls()
            print(f"    Found {len(existing_urls)} existing URLs in Airtable")

        # Filter out dupes
        to_push = []
        skipped = 0
        for record in records:
            url = record.get("url", "")
            if dedup and url in existing_urls:
                skipped += 1
                continue
            to_push.append(record)

        if not to_push:
            print("[*] All records already exist in Airtable — nothing to push")
            return {"pushed": 0, "skipped": skipped, "errors": 0, "total": len(records)}

        print(f"[*] Pushing {len(to_push)} records to Airtable (skipped {skipped} dupes)...")

        pushed = 0
        errors = 0

        # Batch push (10 records per Airtable API limit)
        for i in range(0, len(to_push), self.BATCH_SIZE):
            batch = to_push[i:i + self.BATCH_SIZE]
            batch_num = (i // self.BATCH_SIZE) + 1
            total_batches = (len(to_push) + self.BATCH_SIZE - 1) // self.BATCH_SIZE

            print(f"  [*] Batch {batch_num}/{total_batches} ({len(batch)} records)...")

            payload = {
                "records": [self._transform_record(item) for item in batch],
                "typecast": True,
            }

            try:
                resp = self.session.post(self._url(), json=payload, timeout=30)

                if resp.status_code == 200:
                    result = resp.json()
                    pushed += len(result.get("records", []))
                    print(f"      [+] Pushed {len(result.get('records', []))} records")
                else:
                    errors += len(batch)
                    print(f"      [!] HTTP {resp.status_code}: {resp.text[:200]}")

            except requests.exceptions.RequestException as e:
                errors += len(batch)
                print(f"      [!] Request failed: {e}")

            # Small delay between batches to be nice to the API
            if i + self.BATCH_SIZE < len(to_push):
                time.sleep(0.5)

        print(f"\n[*] Airtable push complete:")
        print(f"    Pushed:  {pushed}")
        print(f"    Skipped: {skipped} (duplicates)")
        print(f"    Errors:  {errors}")
        print(f"    Total:   {len(records)}")

        return {
            "pushed": pushed,
            "skipped": skipped,
            "errors": errors,
            "total": len(records),
        }

    def _get_existing_urls(self) -> set:
        """
        Fetch all existing URLs from the Airtable table for deduplication.
        Handles pagination (100 records per page).
        """
        existing = set()
        offset = None

        while True:
            params = {"pageSize": 100}
            if offset:
                params["offset"] = offset

            try:
                resp = self.session.get(self._url(), params=params, timeout=30)
                if resp.status_code != 200:
                    print(f"  [!] Dedup fetch failed (HTTP {resp.status_code}): {resp.text[:200]}")
                    break

                data = resp.json()
                for record in data.get("records", []):
                    url = record.get("fields", {}).get("URL")
                    if url:
                        existing.add(url)

                offset = data.get("offset")
                if not offset:
                    break

            except requests.exceptions.RequestException as e:
                print(f"  [!] Dedup fetch error: {e}")
                break

        return existing

    def test_connection(self) -> bool:
        """Test that the token, base ID, and table name are valid."""
        try:
            resp = self.session.get(self._url(), params={"pageSize": 1}, timeout=10)
            if resp.status_code == 200:
                print("[+] Airtable connection OK!")
                return True
            else:
                print(f"[!] Airtable connection failed (HTTP {resp.status_code}): {resp.text[:200]}")
                return False
        except requests.exceptions.RequestException as e:
            print(f"[!] Airtable connection error: {e}")
            return False


def main():
    parser = argparse.ArgumentParser(description="Push scraped RedGifs results to Airtable")
    parser.add_argument("--token", type=str,
                        default=os.getenv("AIRTABLE_PAT"),
                        help="Airtable PAT (or set AIRTABLE_PAT env var)")
    parser.add_argument("--base-id", type=str,
                        default=os.getenv("AIRTABLE_BASE_ID"),
                        help="Airtable base ID (or set AIRTABLE_BASE_ID env var)")
    parser.add_argument("--table-name", type=str,
                        default=os.getenv("AIRTABLE_TABLE_NAME", "Table 1"),
                        help='Table name (default: "Table 1", or set AIRTABLE_TABLE_NAME env var)')
    parser.add_argument("--file", type=str, required=True,
                        help="Path to scraped JSON file (e.g. data/blowjob_results.json)")
    parser.add_argument("--no-dedup", action="store_true",
                        help="Skip deduplication (push all records regardless)")

    args = parser.parse_args()

    # Validate creds
    if not args.token:
        print("[!] Missing Airtable token. Use --token or set AIRTABLE_PAT env var.")
        return
    if not args.base_id:
        print("[!] Missing Airtable base ID. Use --base-id or set AIRTABLE_BASE_ID env var.")
        return

    # Load scraped data
    if not os.path.exists(args.file):
        print(f"[!] File not found: {args.file}")
        return

    with open(args.file, "r", encoding="utf-8") as f:
        records = json.load(f)

    if not isinstance(records, list):
        print("[!] Invalid JSON format — expected a list of records")
        return

    print(f"[*] Loaded {len(records)} records from {args.file}")

    # Push
    pusher = AirtablePush(
        token=args.token,
        base_id=args.base_id,
        table_name=args.table_name,
    )

    if not pusher.test_connection():
        print("[!] Connection test failed. Check your token, base ID, and table name.")
        return

    pusher.push_records(records, dedup=not args.no_dedup)


if __name__ == "__main__":
    main()
