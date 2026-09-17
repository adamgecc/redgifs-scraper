"""
Reddit Auto-Poster via AdsPower + Playwright
=============================================
Automates posting RedGifs links to Reddit subreddits using AdsPower browser profiles.

Flow:
  1. Read Airtable Links table for Status="Queued" records
  2. Read Airtable Accounts table for account credentials + AdsPower profile IDs
  3. Start AdsPower browser profile (headless)
  4. Connect Playwright via CDP WebSocket
  5. Log in to Reddit (if not already logged in)
  6. Navigate to subreddit submit page
  7. Fill in URL + title with human-like delays + mouse movement
  8. Submit post
  9. Grab Reddit post URL
  10. Update Airtable: Status="Posted", Posted At, Reddit Post URL
  11. On failure: screenshot, Status="Failed", upload screenshot to Airtable
  12. Stop AdsPower browser
  13. Move to next link

Usage:
    python reddit_poster.py --post
    python reddit_poster.py --post --dry-run
    python reddit_poster.py --post --max 5        # Only post 5 links this run
    python reddit_poster.py --post --account "u_username"  # Only for specific account
"""

import requests
import json
import os
import time
import random
import argparse
import base64
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional, Tuple
from urllib.parse import quote

# Playwright
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeout

# Stealth
try:
    from playwright_stealth import stealth_sync
    HAS_STEALTH = True
except ImportError:
    HAS_STEALTH = False


class AdsPowerController:
    """Controls AdsPower browser profiles via local API."""

    API_BASE = "http://local.adspower.net:50325"

    def __init__(self, headless: bool = True):
        self.headless = headless

    def start_profile(self, user_id: str) -> Optional[Dict]:
        """
        Start an AdsPower browser profile.
        Returns connection info (CDP WebSocket URL, debug port) or None on failure.
        """
        params = {"user_id": user_id, "headless": 1 if self.headless else 0}
        try:
            resp = requests.get(
                f"{self.API_BASE}/api/v1/browser/start",
                params=params,
                timeout=60
            )
            data = resp.json()

            if data.get("code") != 0:
                print(f"    [!] AdsPower start failed: {data.get('msg', 'unknown')}")
                return None

            conn = data.get("data", {})
            ws_url = conn.get("ws", {}).get("puppeteer", "")
            debug_port = conn.get("debug_port", "")

            if not ws_url:
                print("    [!] No CDP WebSocket URL returned")
                return None

            return {
                "ws_url": ws_url,
                "debug_port": debug_port,
                "webdriver": conn.get("webdriver", ""),
            }

        except requests.exceptions.RequestException as e:
            print(f"    [!] AdsPower API error: {e}")
            return None

    def stop_profile(self, user_id: str) -> bool:
        """Stop an AdsPower browser profile."""
        try:
            resp = requests.get(
                f"{self.API_BASE}/api/v1/browser/stop",
                params={"user_id": user_id},
                timeout=10
            )
            return resp.json().get("code") == 0
        except requests.exceptions.RequestException:
            return False

    def check_active(self, user_id: str) -> bool:
        """Check if a profile is already running."""
        try:
            resp = requests.get(
                f"{self.API_BASE}/api/v1/browser/active",
                params={"user_id": user_id},
                timeout=10
            )
            return resp.json().get("code") == 0
        except requests.exceptions.RequestException:
            return False


class HumanBehavior:
    """Simulates human-like browser interaction to avoid detection."""

    @staticmethod
    def random_delay(min_s: float = 1.0, max_s: float = 3.0):
        """Random delay between actions."""
        time.sleep(random.uniform(min_s, max_s))

    @staticmethod
    def type_human(page: Page, selector: str, text: str, min_delay: float = 0.05, max_delay: float = 0.15):
        """Type text character by character with random delays (like a human)."""
        page.click(selector)
        HumanBehavior.random_delay(0.3, 0.8)

        for char in text:
            page.type(selector, char, delay=random.randint(50, 150))
            if random.random() < 0.1:  # 10% chance of a longer pause (thinking)
                time.sleep(random.uniform(0.5, 1.5))

    @staticmethod
    def mouse_move_random(page: Page):
        """Move mouse to a random position on the page."""
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        page.mouse.move(x, y)
        HumanBehavior.random_delay(0.2, 0.5)

    @staticmethod
    def scroll_page(page: Page, direction: str = "down", amount: int = 300):
        """Scroll the page like a human reading."""
        if direction == "down":
            page.mouse.wheel(0, amount)
        else:
            page.mouse.wheel(0, -amount)
        HumanBehavior.random_delay(0.5, 1.5)

    @staticmethod
    def move_and_click(page: Page, selector: str):
        """Move mouse to element then click (not instant click)."""
        element = page.locator(selector)
        if element.count() > 0:
            box = element.first.bounding_box()
            if box:
                # Move to slightly offset position near the element
                target_x = box["x"] + box["width"] / 2 + random.randint(-5, 5)
                target_y = box["y"] + box["height"] / 2 + random.randint(-3, 3)
                page.mouse.move(target_x, target_y, steps=random.randint(5, 15))
                HumanBehavior.random_delay(0.1, 0.3)
                page.mouse.click(target_x, target_y)
                HumanBehavior.random_delay(0.3, 0.8)

    @staticmethod
    def browse_before_posting(page: Page):
        """Simulate browsing behavior before posting (anti-detection)."""
        # Wait for page to load
        HumanBehavior.random_delay(2, 4)

        # Scroll down a bit
        HumanBehavior.scroll_page(page, "down", random.randint(200, 400))
        HumanBehavior.random_delay(1, 2)

        # Move mouse around
        HumanBehavior.mouse_move_random(page)

        # Scroll back up
        HumanBehavior.scroll_page(page, "up", random.randint(100, 200))
        HumanBehavior.random_delay(0.5, 1.5)


class RedditPoster:
    """Automates posting to Reddit via AdsPower + Playwright."""

    REDDIT_LOGIN = "https://www.reddit.com/account/login"
    REDDIT_SUBMIT = "https://www.reddit.com/submit"
    REDDIT_SUBMIT_SUB = "https://www.reddit.com/r/{subreddit}/submit"

    def __init__(
        self,
        airtable_token: str,
        airtable_base: str,
        links_table: str = "Links",
        accounts_table: str = "Accounts",
        screenshot_dir: str = "screenshots",
        headless: bool = True,
    ):
        self.airtable_token = airtable_token
        self.airtable_base = airtable_base
        self.links_table = links_table
        self.accounts_table = accounts_table
        self.screenshot_dir = screenshot_dir
        self.headless = headless

        self.adspower = AdsPowerController(headless=headless)
        self.at_headers = {
            "Authorization": f"Bearer {airtable_token}",
            "Content-Type": "application/json",
        }

        Path(screenshot_dir).mkdir(parents=True, exist_ok=True)

    def _at_url(self, table: str) -> str:
        return f"https://api.airtable.com/v0/{self.airtable_base}/{quote(table)}"

    # === Airtable Methods ===

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

    def _at_update_batch(self, table: str, updates: List[Dict]) -> int:
        """Batch update records in Airtable."""
        updated = 0
        for i in range(0, len(updates), 10):
            batch = updates[i:i + 10]
            payload = {"records": [{"id": u["id"], "fields": u["fields"]} for u in batch], "typecast": True}
            resp = requests.patch(self._at_url(table), headers=self.at_headers, json=payload, timeout=30)
            if resp.status_code == 200:
                updated += len(resp.json().get("records", []))
        return updated

    def _at_update_single(self, table: str, record_id: str, fields: Dict) -> bool:
        """Update a single record."""
        url = f"{self._at_url(table)}/{record_id}"
        payload = {"fields": fields, "typecast": True}
        resp = requests.patch(url, headers=self.at_headers, json=payload, timeout=30)
        return resp.status_code == 200

    def _at_upload_attachment(self, table: str, record_id: str, field_name: str, file_path: str) -> bool:
        """Upload a file as an attachment to an Airtable record."""
        try:
            # Airtable attachment upload requires multipart form
            url = f"{self._at_url(table)}/{record_id}"
            filename = os.path.basename(file_path)

            with open(file_path, "rb") as f:
                file_data = f.read()

            # First upload the file to Airtable's file upload endpoint
            upload_url = f"https://content.airtable.com/v0/{self.airtable_base}/{quote(self.links_table)}/{record_id}/{quote(field_name)}"
            files = {
                field_name: (filename, file_data, "image/png"),
            }
            headers = {"Authorization": f"Bearer {self.airtable_token}"}
            resp = requests.post(upload_url, headers=headers, files=files, timeout=60)

            if resp.status_code == 200:
                print(f"      [+] Screenshot uploaded to Airtable")
                return True
            else:
                print(f"      [!] Screenshot upload failed: HTTP {resp.status_code} — {resp.text[:200]}")
                return False
        except Exception as e:
            print(f"      [!] Screenshot upload error: {e}")
            return False

    def get_queued_links(self, account_name: str = None, max_results: int = None) -> List[Dict]:
        """Get links with Status='Queued' from Airtable."""
        if account_name:
            formula = f'AND({{Status}} = "Queued", {{Assigned Account}} = "{account_name}")'
        else:
            formula = '{{Status}} = "Queued"'

        records = self._at_get(self.links_table, filter_formula=formula)

        if max_results:
            records = records[:max_results]

        print(f"  [*] Found {len(records)} queued links")
        return records

    def get_account_info(self, account_name: str) -> Optional[Dict]:
        """Get account info from Accounts table."""
        formula = f'{{Account Name}} = "{account_name}"'
        records = self._at_get(self.accounts_table, filter_formula=formula)
        if records:
            return records[0]
        return None

    # === Screenshot Methods ===

    def take_screenshot(self, page: Page, context: str = "error") -> Optional[str]:
        """Take a screenshot and save it locally."""
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{self.screenshot_dir}/{context}_{timestamp}.png"
        try:
            page.screenshot(path=filename, full_page=True)
            print(f"      [+] Screenshot saved: {filename}")
            return filename
        except Exception as e:
            print(f"      [!] Screenshot failed: {e}")
            return None

    # === Reddit Login ===

    def login_reddit(self, page: Page, username: str, password: str) -> bool:
        """
        Log in to Reddit.
        Returns True if login successful, False otherwise.
        """
        print("    [*] Navigating to Reddit login...")
        page.goto(self.REDDIT_LOGIN, wait_until="networkidle", timeout=30000)
        HumanBehavior.random_delay(2, 4)

        # Check if already logged in
        if self._is_logged_in(page):
            print("    [+] Already logged in — skipping login")
            return True

        # Fill username
        print("    [*] Entering username...")
        try:
            HumanBehavior.type_human(page, 'input[name="username"]', username)
            HumanBehavior.random_delay(0.5, 1.5)

            # Fill password
            print("    [*] Entering password...")
            HumanBehavior.type_human(page, 'input[name="password"]', password)
            HumanBehavior.random_delay(0.5, 1.5)

            # Click login button
            print("    [*] Clicking login button...")
            HumanBehavior.move_and_click(page, 'button[type="submit"]')

            # Wait for navigation or error
            time.sleep(5)

            if self._is_logged_in(page):
                print("    [+] Login successful!")
                return True
            else:
                print("    [!] Login may have failed — checking for errors...")

                # Screenshot for debugging
                self.take_screenshot(page, "login_failed")

                # Check for 2FA / captcha
                page_text = page.inner_text("body")
                if "two-factor" in page_text.lower() or "2fa" in page_text.lower():
                    print("    [!] 2FA detected — skipping this account")
                    return False
                if "captcha" in page_text.lower():
                    print("    [!] Captcha detected — skipping this account")
                    return False
                if "incorrect" in page_text.lower() or "wrong" in page_text.lower():
                    print("    [!] Wrong credentials — skipping")
                    return False

                return False

        except PlaywrightTimeout:
            print("    [!] Timeout during login")
            self.take_screenshot(page, "login_timeout")
            return False
        except Exception as e:
            print(f"    [!] Login error: {e}")
            self.take_screenshot(page, "login_error")
            return False

    def _is_logged_in(self, page: Page) -> bool:
        """Check if the Reddit session is active."""
        try:
            # Check for user menu / logged in indicators
            page.wait_for_load_state("networkidle", timeout=5000)

            # Check URL — if we're not on login page, we're likely logged in
            if "/account/login" in page.url:
                return False

            # Check for logged-in elements
            logged_in_selectors = [
                '[data-testid="user-drawer-button"]',
                '[aria-label*="profile menu"]',
                'button[aria-label*="Open profile"]',
                'a[href*="/user/"]',
            ]

            for sel in logged_in_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        return True
                except:
                    continue

            # Check if login form is still visible
            if page.locator('input[name="username"]').count() > 0:
                return False

            return True

        except:
            return False

    # === Reddit Posting ===

    def post_link(self, page: Page, subreddit: str, url: str, title: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Post a link to a subreddit.
        Returns (success, reddit_post_url, error_screenshot_path).
        """
        try:
            # Navigate to subreddit submit page
            submit_url = self.REDDIT_SUBMIT_SUB.format(subreddit=subreddit)
            print(f"    [*] Navigating to {submit_url}...")
            page.goto(submit_url, wait_until="networkidle", timeout=30000)
            HumanBehavior.random_delay(2, 5)

            # Check if subreddit allows link posts
            page_text = page.inner_text("body")
            if "doesn't allow" in page_text.lower() or "not allow" in page_text.lower():
                error_msg = f"Subreddit r/{subreddit} doesn't allow link posts"
                print(f"    [!] {error_msg}")
                screenshot = self.take_screenshot(page, f"no_link_posts_{subreddit}")
                return False, None, screenshot

            # Browse before posting (anti-detection)
            print("    [*] Simulating browsing behavior...")
            HumanBehavior.browse_before_posting(page)

            # Select "Link" post type if needed
            link_tab_selectors = [
                'button:has-text("Link")',
                '[data-testid="tab-link"]',
                'a:has-text("Link")',
            ]
            for sel in link_tab_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        HumanBehavior.move_and_click(page, sel)
                        HumanBehavior.random_delay(1, 2)
                        break
                except:
                    continue

            # Fill in URL
            print("    [*] Entering URL...")
            url_selectors = [
                'input[name="url"]',
                'textarea[placeholder*="URL"]',
                'input[placeholder*="url"]',
                'input[placeholder*="Url"]',
            ]
            url_filled = False
            for sel in url_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        HumanBehavior.type_human(page, sel, url)
                        url_filled = True
                        break
                except:
                    continue

            if not url_filled:
                screenshot = self.take_screenshot(page, "url_field_not_found")
                return False, None, screenshot

            HumanBehavior.random_delay(1, 3)

            # Fill in title
            print("    [*] Entering title...")
            title_selectors = [
                'textarea[name="title"]',
                'input[name="title"]',
                'textarea[placeholder*="Title"]',
                'div[role="textbox"]',
            ]
            title_filled = False
            for sel in title_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        HumanBehavior.type_human(page, sel, title)
                        title_filled = True
                        break
                except:
                    continue

            if not title_filled:
                screenshot = self.take_screenshot(page, "title_field_not_found")
                return False, None, screenshot

            HumanBehavior.random_delay(1, 2)

            # Mouse movement before submit
            HumanBehavior.mouse_move_random(page)
            HumanBehavior.random_delay(0.5, 1.5)

            # Click submit
            print("    [*] Clicking submit...")
            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("Post")',
                'button:has-text("Submit")',
                '[data-testid="submit-button"]',
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        HumanBehavior.move_and_click(page, sel)
                        submitted = True
                        break
                except:
                    continue

            if not submitted:
                screenshot = self.take_screenshot(page, "submit_button_not_found")
                return False, None, screenshot

            # Wait for post to go through
            print("    [*] Waiting for post to submit...")
            time.sleep(random.uniform(5, 10))

            # Check for rate limit
            page_text = page.inner_text("body")
            if "doing that too much" in page_text.lower() or "rate limit" in page_text.lower():
                print("    [!] Rate limited by Reddit")
                screenshot = self.take_screenshot(page, "rate_limited")
                return False, None, screenshot

            # Check for captcha
            if "captcha" in page_text.lower():
                print("    [!] Captcha appeared")
                screenshot = self.take_screenshot(page, "captcha")
                return False, None, screenshot

            # Try to get the post URL
            reddit_post_url = None

            # Method 1: Check if URL changed to the new post
            current_url = page.url
            if "/comments/" in current_url:
                reddit_post_url = current_url
                print(f"    [+] Post URL (from redirect): {reddit_post_url}")

            # Method 2: Look for post link on page
            if not reddit_post_url:
                try:
                    post_links = page.locator('a[href*="/comments/"]')
                    if post_links.count() > 0:
                        href = post_links.first.get_attribute("href")
                        if href:
                            if href.startswith("/"):
                                reddit_post_url = f"https://www.reddit.com{href}"
                            elif not href.startswith("http"):
                                reddit_post_url = f"https://www.reddit.com/{href}"
                            else:
                                reddit_post_url = href
                            print(f"    [+] Post URL (from link): {reddit_post_url}")
                except:
                    pass

            if not reddit_post_url:
                # Even if we can't find the URL, the post might have succeeded
                # Check for success indicators
                if "submitted" in page_text.lower() or "your post" in page_text.lower():
                    print("    [+] Post appears successful (URL not found)")
                    return True, None, None
                else:
                    print("    [!] Could not confirm post success")
                    screenshot = self.take_screenshot(page, "post_result_unknown")
                    return False, None, screenshot

            return True, reddit_post_url, None

        except PlaywrightTimeout:
            print("    [!] Timeout during posting")
            screenshot = self.take_screenshot(page, "post_timeout")
            return False, None, screenshot
        except Exception as e:
            print(f"    [!] Posting error: {e}")
            screenshot = self.take_screenshot(page, "post_error")
            return False, None, screenshot

    # === Main Automation Loop ===

    def run(self, max_posts: int = None, account_filter: str = None, dry_run: bool = False):
        """
        Main automation loop.
        Picks up queued links, posts them, updates Airtable.
        """
        print(f"\n[*] Reddit Auto-Poster Starting")
        print(f"    Mode: {'DRY RUN' if dry_run else 'LIVE'}")
        print(f"    Headless: {self.headless}")
        print(f"    Account filter: {account_filter or 'All'}")
        print(f"    Max posts: {max_posts or 'Unlimited'}")

        # Get queued links
        links = self.get_queued_links(account_name=account_filter, max_results=max_posts)

        if not links:
            print("  [-] No queued links found. Run assign.py --auto first.")
            return

        print(f"\n  [*] Processing {len(links)} links...\n")

        success_count = 0
        fail_count = 0

        for i, link_record in enumerate(links, 1):
            fields = link_record.get("fields", {})
            url = fields.get("URL", "")
            title = fields.get("Title", "")
            niche = fields.get("Niche", "")
            subreddit_raw = fields.get("Subreddit", "")
            account_name = fields.get("Assigned Account", "")

            # Clean subreddit name (remove r/ prefix if present)
            subreddit = subreddit_raw.replace("r/", "").replace("/r/", "").strip() if subreddit_raw else niche.lower()

            print(f"\n  [{i}/{len(links)}] Link: {url}")
            print(f"    Account: {account_name}")
            print(f"    Subreddit: r/{subreddit}")
            print(f"    Title: {title}")

            if dry_run:
                print(f"    [DRY RUN] Would post to r/{subreddit}")
                continue

            # Get account info from Accounts table
            account_info = self.get_account_info(account_name)
            if not account_info:
                print(f"    [!] Account '{account_name}' not found in Accounts table — skipping")
                continue

            account_fields = account_info.get("fields", {})

            # Check account status
            account_status = account_fields.get("Account Status", "")
            if account_status in ["Banned", "Cooldown", "Warming Up"]:
                print(f"    [!] Account status is '{account_status}' — skipping")
                continue

            # Get AdsPower profile ID — stored in Notes or a custom field
            adspower_id = account_fields.get("AdsPower ID", "") or account_fields.get("Notes", "")

            # Get credentials
            reddit_username = account_fields.get("Account Name", account_name)
            reddit_password = account_fields.get("Password", "") or account_fields.get("Notes", "")

            if not adspower_id:
                print(f"    [!] No AdsPower profile ID for account '{account_name}' — skipping")
                print(f"        Add the AdsPower user_id to the 'AdsPower ID' or 'Notes' field in Accounts table")
                continue

            # Start AdsPower browser
            print(f"    [*] Starting AdsPower profile: {adspower_id}")
            conn = self.adspower.start_profile(adspower_id)
            if not conn:
                print(f"    [!] Failed to start AdsPower profile — skipping")
                fail_count += 1
                continue

            ws_url = conn["ws_url"]

            try:
                with sync_playwright() as pw:
                    # Connect to AdsPower browser via CDP
                    print(f"    [*] Connecting Playwright via CDP...")
                    browser = pw.chromium.connect_over_cdp(ws_url)

                    # Get or create a context
                    contexts = browser.contexts
                    context = contexts[0] if contexts else browser.new_context()

                    # Create new page
                    page = context.new_page()

                    # Apply stealth
                    if HAS_STEALTH:
                        try:
                            stealth_sync(page)
                        except:
                            pass

                    # Login to Reddit
                    login_success = self.login_reddit(page, reddit_username, reddit_password)

                    if not login_success:
                        print(f"    [!] Login failed — marking link as Failed")
                        screenshot = self.take_screenshot(page, f"login_fail_{reddit_username}")
                        self._at_update_single(self.links_table, link_record["id"], {
                            "Status": "Failed",
                            "Error Screenshot": [{"url": f"file://{screenshot}"}] if screenshot else None,
                        })
                        fail_count += 1
                        page.close()
                        continue

                    # Post the link
                    success, post_url, error_screenshot = self.post_link(
                        page, subreddit, url, title
                    )

                    if success:
                        print(f"    [+] Post successful!")
                        success_count += 1

                        # Update Airtable Links table
                        update_fields = {
                            "Status": "Posted",
                            "Posted At": datetime.now(timezone.utc).isoformat(),
                        }
                        if post_url:
                            update_fields["Reddit Post URL"] = post_url

                        self._at_update_single(self.links_table, link_record["id"], update_fields)

                        # Update Airtable Accounts table
                        posts_today = account_fields.get("Posts Today", 0) or 0
                        total_posts = account_fields.get("Total Posts", 0) or 0
                        self._at_update_single(self.accounts_table, account_info["id"], {
                            "Posts Today": posts_today + 1,
                            "Total Posts": total_posts + 1,
                            "Last Post Date": datetime.now(timezone.utc).isoformat(),
                        })

                    else:
                        print(f"    [!] Post failed — marking as Failed")
                        fail_count += 1

                        update_fields = {
                            "Status": "Failed",
                        }

                        # Upload screenshot to Airtable as attachment
                        if error_screenshot:
                            self._at_upload_attachment(
                                self.links_table, link_record["id"],
                                "Error Screenshot", error_screenshot
                            )

                        self._at_update_single(self.links_table, link_record["id"], update_fields)

                        # Check if account should be marked Banned
                        if error_screenshot and "captcha" in error_screenshot:
                            print(f"    [!] Captcha detected — marking account as Cooldown")
                            self._at_update_single(self.accounts_table, account_info["id"], {
                                "Account Status": "Cooldown",
                            })

                    page.close()

            except Exception as e:
                print(f"    [!] Browser error: {e}")
                fail_count += 1

            finally:
                # Stop AdsPower browser
                print(f"    [*] Stopping AdsPower profile...")
                self.adspower.stop_profile(adspower_id)
                time.sleep(random.uniform(3, 8))  # Cooldown between accounts

        # Summary
        print(f"\n[*] Automation Complete:")
        print(f"    Success: {success_count}")
        print(f"    Failed:  {fail_count}")
        print(f"    Total:   {len(links)}")


def main():
    parser = argparse.ArgumentParser(description="Reddit Auto-Poster via AdsPower + Playwright")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID")
    parser.add_argument("--links-table", default="Links", help="Links table name")
    parser.add_argument("--accounts-table", default="Accounts", help="Accounts table name")
    parser.add_argument("--post", action="store_true", help="Run the auto-poster")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--max", type=int, help="Maximum posts this run")
    parser.add_argument("--account", type=str, help="Only post for this account name")
    parser.add_argument("--no-headless", action="store_true", help="Show browser (debug mode)")
    parser.add_argument("--screenshot-dir", default="screenshots", help="Directory for error screenshots")

    args = parser.parse_args()

    if not args.token or not args.base_id:
        print("[!] Missing credentials. Use --token and --base-id or set AIRTABLE_PAT and AIRTABLE_BASE_ID env vars.")
        return

    if not args.post:
        parser.print_help()
        return

    poster = RedditPoster(
        airtable_token=args.token,
        airtable_base=args.base_id,
        links_table=args.links_table,
        accounts_table=args.accounts_table,
        screenshot_dir=args.screenshot_dir,
        headless=not args.no_headless,
    )

    poster.run(
        max_posts=args.max,
        account_filter=args.account,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
