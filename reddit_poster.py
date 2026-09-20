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
    """Controls AdsPower browser profiles via local or remote API."""

    API_BASE = "http://local.adspower.net:50325"

    def __init__(self, headless: bool = True, api_key: str = None, ssh_host: str = None,
                 rotation_url: str = None):
        """
        Args:
            headless: run browser headless
            api_key: AdsPower API key (required for Mac Mini, optional for local)
            ssh_host: if set, route API calls through SSH tunnel to remote machine
            rotation_url: proxy rotation link to hit between accounts (fresh IP)
        """
        self.headless = headless
        self.api_key = api_key
        self.ssh_host = ssh_host
        self.rotation_url = rotation_url
        self.headers = {}
        if api_key:
            self.headers["Authorization"] = f"Bearer {api_key}"

    def rotate_ip(self):
        """Hit the proxy rotation link to get a fresh IP."""
        if not self.rotation_url:
            return

        print("    [*] Rotating proxy IP...")
        try:
            if self.ssh_host:
                import subprocess
                result = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=10", self.ssh_host, f'curl -s -L "{self.rotation_url}"'],
                    capture_output=True, text=True, timeout=30
                )
                print(f"        [+] Rotation response: {result.stdout[:100]}")
            else:
                resp = requests.get(self.rotation_url, timeout=30, allow_redirects=True)
                print(f"        [+] Rotation response: {resp.text[:100]}")
        except Exception as e:
            print(f"        [!] Rotation failed: {e}")

    def _api_get(self, path: str, params: dict = None) -> dict:
        """Make a GET request to AdsPower API, optionally via SSH tunnel."""
        if self.ssh_host:
            # Build curl command to run on remote machine via SSH
            url = f"{self.API_BASE}{path}"
            curl_cmd = f'curl -s -H "Authorization: Bearer {self.api_key}" "{url}'
            if params:
                query = "&".join(f"{k}={v}" for k, v in params.items())
                curl_cmd += f"?{query}"
            curl_cmd += '"'
            import subprocess
            result = subprocess.run(
                ["ssh", "-o", "ConnectTimeout=10", self.ssh_host, curl_cmd],
                capture_output=True, text=True, timeout=60
            )
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {"code": -1, "msg": f"SSH parse error: {result.stdout[:200]}"}
        else:
            resp = requests.get(f"{self.API_BASE}{path}", headers=self.headers, params=params, timeout=60)
            return resp.json()

    def start_profile(self, user_id: str) -> Optional[Dict]:
        """
        Start an AdsPower browser profile.
        Returns connection info (CDP WebSocket URL, debug port) or None on failure.
        When using SSH, sets up an SSH tunnel so Playwright on the local machine
        can connect to the remote CDP endpoint.
        """
        params = {"user_id": user_id, "headless": 1 if self.headless else 0}
        data = self._api_get("/api/v1/browser/start", params)

        if data.get("code") != 0:
            print(f"    [!] AdsPower start failed: {data.get('msg', 'unknown')}")
            return None

        conn = data.get("data", {})
        ws_url = conn.get("ws", {}).get("puppeteer", "")
        debug_port = conn.get("debug_port", "")

        if not ws_url:
            print("    [!] No CDP WebSocket URL returned")
            return None

        # If using SSH, we need to tunnel the CDP port to local
        if self.ssh_host:
            import subprocess
            import re

            # Extract the port from the WebSocket URL (e.g. ws://127.0.0.1:59019/...)
            port_match = re.search(r':(\d+)', ws_url)
            if not port_match:
                print(f"    [!] Could not extract port from WebSocket URL: {ws_url}")
                return None

            remote_port = int(port_match.group(1))
            local_port = remote_port  # Use same port locally

            # Kill any existing tunnel on this port
            subprocess.run(["lsof", "-ti", f":{local_port}"], capture_output=True)
            subprocess.run(["bash", "-c", f"lsof -ti :{local_port} | xargs kill -9 2>/dev/null"], capture_output=True)

            # Start SSH tunnel: local_port -> remote 127.0.0.1:remote_port
            print(f"    [*] Starting SSH tunnel: localhost:{local_port} → {self.ssh_host}:{remote_port}")
            tunnel = subprocess.Popen(
                ["ssh", "-o", "ConnectTimeout=10", "-L", f"{local_port}:127.0.0.1:{remote_port}", "-N", self.ssh_host],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(2)  # Wait for tunnel to establish

            # Replace the WebSocket URL to use localhost
            ws_url_local = ws_url  # Already uses 127.0.0.1, which now tunnels to remote
            print(f"    [+] Tunnel established. CDP: {ws_url_local}")

            return {
                "ws_url": ws_url_local,
                "debug_port": str(local_port),
                "webdriver": conn.get("webdriver", ""),
                "_tunnel_pid": tunnel.pid,
            }

        return {
            "ws_url": ws_url,
            "debug_port": debug_port,
            "webdriver": conn.get("webdriver", ""),
        }

    def stop_profile(self, user_id: str, tunnel_pid: int = None) -> bool:
        """Stop an AdsPower browser profile and kill SSH tunnel if exists."""
        data = self._api_get("/api/v1/browser/stop", {"user_id": user_id})
        success = data.get("code") == 0

        # Kill SSH tunnel if we started one
        if tunnel_pid:
            import subprocess
            try:
                subprocess.run(["kill", str(tunnel_pid)], capture_output=True)
                print(f"    [+] SSH tunnel closed (PID {tunnel_pid})")
            except:
                pass

        return success

    def check_active(self, user_id: str) -> bool:
        """Check if a profile is already running."""
        data = self._api_get("/api/v1/browser/active", {"user_id": user_id})
        return data.get("code") == 0

    def list_profiles(self) -> List[Dict]:
        """List all AdsPower profiles."""
        all_profiles = []
        page = 1
        while True:
            data = self._api_get("/api/v1/user/list", {"page": page, "page_size": 100})
            if data.get("code") != 0:
                break
            profiles = data.get("data", {}).get("list", [])
            if not profiles:
                break
            all_profiles.extend(profiles)
            if len(profiles) < 100:
                break
            page += 1
        return all_profiles


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
    REDDIT_SUBMIT_LINK = "https://www.reddit.com/r/{subreddit}/submit"

    def __init__(
        self,
        airtable_token: str,
        airtable_base: str,
        links_table: str = "Posts",
        accounts_table: str = "Accounts",
        screenshot_dir: str = "screenshots",
        headless: bool = True,
        adspower_api_key: str = None,
        adspower_ssh_host: str = None,
        adspower_rotation_url: str = None,
    ):
        self.airtable_token = airtable_token
        self.airtable_base = airtable_base
        self.posts_table = links_table  # Renamed to posts_table for clarity
        self.accounts_table = accounts_table
        self.screenshot_dir = screenshot_dir
        self.headless = headless

        self.adspower = AdsPowerController(
            headless=headless,
            api_key=adspower_api_key,
            ssh_host=adspower_ssh_host,
            rotation_url=adspower_rotation_url,
        )
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
            upload_url = f"https://content.airtable.com/v0/{self.airtable_base}/{quote(self.posts_table)}/{record_id}/{quote(field_name)}"
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
        """Get posts with Status='Queued' from Posts table."""
        if account_name:
            formula = f'AND({{Status}} = "Queued", {{Account}} = "{account_name}")'
        else:
            formula = '{Status} = "Queued"'

        records = self._at_get(self.posts_table, filter_formula=formula)

        if max_results:
            records = records[:max_results]

        print(f"  [*] Found {len(records)} queued posts")
        return records

    def get_account_info(self, account_name: str) -> Optional[Dict]:
        """Get account info from Accounts table."""
        formula = f'{{Username}} = "{account_name}"'
        records = self._at_get(self.accounts_table, filter_formula=formula)
        if records:
            return records[0]
        return None

    def _get_subreddit_niche(self, subreddit: str) -> Optional[str]:
        """Look up the niche for a subreddit from the Subreddits table in Airtable."""
        subreddit_clean = subreddit.replace("r/", "").replace("/r/", "").strip()
        formula = f'{{Subreddit}} = "{subreddit_clean}"'
        records = self._at_get("Subreddits", filter_formula=formula)
        if records:
            return records[0].get("fields", {}).get("Niche", "")
        return None

    def _get_subreddit_banned_words(self, subreddit: str) -> list:
        """Look up banned words for a subreddit from the Subreddits table."""
        subreddit_clean = subreddit.replace("r/", "").replace("/r/", "").strip()
        formula = f'{{Subreddit}} = "{subreddit_clean}"'
        records = self._at_get("Subreddits", filter_formula=formula)
        if records:
            banned = records[0].get("fields", {}).get("Banned Words", "")
            if banned:
                return [w.strip() for w in banned.split(",") if w.strip()]
        return []

    # === Screenshot Methods ===

    def take_screenshot(self, page: Page, context: str = "error") -> Optional[str]:
        """Take a screenshot and save it to the redgifs_screenshots folder on the Desktop."""
        import os as os2
        screenshot_dir = os2.path.expanduser("~/Desktop/redgifs_screenshots")
        os2.makedirs(screenshot_dir, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        filename = f"{screenshot_dir}/{context}_{timestamp}.png"
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
        page.goto(self.REDDIT_LOGIN, wait_until="domcontentloaded", timeout=60000)
        HumanBehavior.random_delay(3, 6)

        # Check if already logged in
        if self._is_logged_in(page):
            print("    [+] Already logged in — skipping login")
            return True

        # Fill username
        print("    [*] Entering username...")
        username_selectors = [
            'input[name="username"]',
            'input[name="email"]',
            'input[placeholder*="username"]',
            'input[placeholder*="email"]',
            'input[placeholder*="Email or username"]',
        ]
        username_filled = False
        for sel in username_selectors:
            try:
                if page.locator(sel).count() > 0:
                    HumanBehavior.type_human(page, sel, username)
                    username_filled = True
                    break
            except:
                continue

        if not username_filled:
            screenshot = self.take_screenshot(page, "username_field_not_found")
            return False

        HumanBehavior.random_delay(0.5, 1.5)

        # Fill password
        print("    [*] Entering password...")
        password_selectors = [
            'input[name="password"]',
            'input[type="password"]',
            'input[placeholder*="password"]',
        ]
        password_filled = False
        for sel in password_selectors:
            try:
                if page.locator(sel).count() > 0:
                    HumanBehavior.type_human(page, sel, password)
                    password_filled = True
                    break
            except:
                continue

        if not password_filled:
            screenshot = self.take_screenshot(page, "password_field_not_found")
            return False

        HumanBehavior.random_delay(0.5, 1.5)

        # Click login button
        print("    [*] Clicking login button...")
        login_selectors = [
            'button[type="submit"]',
            'button:has-text("Log In")',
            'button:has-text("Login")',
            'button:has-text("log in")',
            '[data-testid="login-button"]',
            'button[data-testid="login-button"]',
        ]
        clicked = False
        for sel in login_selectors:
            try:
                if page.locator(sel).count() > 0:
                    HumanBehavior.move_and_click(page, sel)
                    clicked = True
                    break
            except:
                continue

        if not clicked:
            # Fallback: press Enter on the password field
            print("    [*] Button not found — pressing Enter on password field...")
            page.press('input[type="password"]', "Enter")

        # Wait for navigation or error
        time.sleep(8)

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
            self.take_screenshot(page, "login_error")
            return False

    def _is_logged_in(self, page: Page) -> bool:
        """Check if the Reddit session is active."""
        try:
            # Check for user menu / logged in indicators
            page.wait_for_load_state("domcontentloaded", timeout=10000)

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

    def post_link(self, page: Page, subreddit: str, url: str, title: str, reddit_username: str = "") -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Post a link to a subreddit.
        Reddit's new submit page has Title + Body (rich text editor).
        We paste the RedGifs URL into the body and submit.

        Returns (success, reddit_post_url, error_screenshot_path).
        """
        try:
            # Navigate to subreddit submit page — use LINK type to force link post
            submit_url = self.REDDIT_SUBMIT_LINK.format(subreddit=subreddit)
            print(f"    [*] Navigating to {submit_url}...")
            # Force the new Reddit UI with post type tabs
            submit_url = submit_url + ("&" if "?" in submit_url else "?") + "newreddit=true&self=false"
            
            # Set desktop viewport size — Reddit serves simplified UI for narrow viewports
            try:
                page.set_viewport_size({"width": 1920, "height": 1080})
                print("    [*] Set viewport to 1920x1080")
            except:
                pass
            
            page.goto(submit_url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for the page to fully render — the post type tabs (Text/Images/Link/Poll/AMA)
            # load via JavaScript after the initial DOM load
            print("    [*] Waiting for page to fully render...")
            time.sleep(15)
            
            # Try to find and click the Link tab using JavaScript
            # The tabs are rendered as faceplate elements — let's find the clickable one
            print("    [*] Looking for Link tab via JavaScript...")
            try:
                # Execute JS to find and click the Link tab
                link_clicked = page.evaluate("""() => {
                    // Find all clickable elements containing "Link" text
                    const allElements = document.querySelectorAll('a, button, div[role="tab"], div[role="button"], span, faceplate-tab');
                    for (const el of allElements) {
                        const text = (el.textContent || '').trim();
                        if (text === 'Link' || text === 'link') {
                            // Check if it's visible
                            const rect = el.getBoundingClientRect();
                            if (rect.width > 0 && rect.height > 0) {
                                // Check if parent context has Text/Images (post type tabs row)
                                let parent = el;
                                for (let i = 0; i < 5; i++) {
                                    parent = parent.parentElement;
                                    if (!parent) break;
                                    const pText = parent.textContent || '';
                                    if (pText.includes('Text') && pText.includes('Images')) {
                                        el.click();
                                        return 'clicked: found in tab row with Text/Images';
                                    }
                                }
                                // Even if not in tab row, try clicking if it looks like a tab
                                if (el.tagName === 'FACEPLATE-TAB' || el.getAttribute('role') === 'tab' || el.getAttribute('role') === 'button') {
                                    el.click();
                                    return 'clicked: tag=' + el.tagName + ' role=' + el.getAttribute('role');
                                }
                            }
                        }
                    }
                    return 'not found';
                }""")
                print(f"    [*] JS result: {link_clicked}")
            except Exception as e:
                print(f"    [!] JS error: {e}")
            
            # Wait for URL field to appear after clicking Link tab
            time.sleep(3)
            
            url_field_found = False
            for attempt in range(6):
                try:
                    url_check = page.locator('input[name="url"], input[type="url"], input[placeholder*="URL"], input[placeholder*="url"], input[placeholder*="Url"], textarea[placeholder*="URL"]')
                    if url_check.count() > 0 and url_check.first.is_visible():
                        print(f"    [+] URL field found after {(attempt+1)*5}s!")
                        url_field_found = True
                        break
                except:
                    pass
                print(f"    [*] Waiting for URL field... ({(attempt+1)*5}s)")
                time.sleep(5)
            
            if not url_field_found:
                print("    [!] URL field never appeared")
            
            HumanBehavior.random_delay(2, 4)

            # Handle mature content popup if it appears on the submit page
            mature_selectors_pre = [
                'button:has-text("Yes")',
                'button:has-text("I\'m Over 18")',
                'button:has-text("I am 18")',
                'button:has-text("over 18")',
            ]
            for sel in mature_selectors_pre:
                try:
                    if page.locator(sel).count() > 0:
                        print(f"    [*] Mature content popup on submit page — clicking: {sel}")
                        HumanBehavior.move_and_click(page, sel)
                        HumanBehavior.random_delay(2, 4)
                        break
                except:
                    continue

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

            # Take a screenshot BEFORE trying to click Link tab — this shows the fully loaded page
            self.take_screenshot(page, "submit_page_loaded")

            # Try to find and click "Link" post type tab
            # New Reddit UI has tabs at the top: Text | Images | Link | Poll | AMA
            # We need to click the "Link" tab that's in the same row as "Text" and "Images"
            # Click the Link Embed button inside Shadow DOM of <post-composer-standalone-toolbar>
            # This button opens the "Add Link" section with a "Link URL *" input field
            # Shadow DOM elements can't be found by regular CSS selectors — we need JS
            print("    [*] Finding Link Embed button in Shadow DOM...")
            
            link_embed_clicked = False
            url_field_found = False  # Track if URL was filled via Link URL field
            
            # Try multiple times — the toolbar takes time to render
            for attempt in range(5):
                try:
                    toolbar_pos = page.evaluate("""() => {
                        // Find the toolbar element
                        const toolbar = document.querySelector('post-composer-standalone-toolbar');
                        if (!toolbar) return {error: 'toolbar element not found'};
                        
                        // Wait for shadow root
                        if (!toolbar.shadowRoot) return {error: 'no shadow root'};
                        
                        // Find the button inside shadow DOM
                        const btn = toolbar.shadowRoot.querySelector('button');
                        if (!btn) return {error: 'no button in shadow root'};
                        
                        // Get the button's bounding rect
                        const r = btn.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return {error: 'button not visible', w: r.width, h: r.height};
                        
                        return {
                            x: r.x + r.width / 2,
                            y: r.y + r.height / 2,
                            w: r.width,
                            h: r.height,
                            text: btn.textContent.trim()
                        };
                    }""")
                    
                    if toolbar_pos.get('error'):
                        print(f"    [*] Attempt {attempt+1}: {toolbar_pos['error']}")
                        time.sleep(3)
                        continue
                    
                    click_x = int(toolbar_pos['x'])
                    click_y = int(toolbar_pos['y'])
                    
                    if click_x <= 0 or click_y <= 0:
                        print(f"    [*] Attempt {attempt+1}: invalid position ({click_x}, {click_y})")
                        time.sleep(3)
                        continue
                    
                    print(f"    [*] Clicking Link Embed at ({click_x}, {click_y}) — button text: '{toolbar_pos.get('text', '')}'")
                    page.mouse.move(click_x, click_y, steps=10)
                    time.sleep(0.5)
                    page.mouse.click(click_x, click_y)
                    time.sleep(3)
                    print("    [+] Link Embed button clicked!")
                    link_embed_clicked = True
                    break
                    
                except Exception as e:
                    print(f"    [*] Attempt {attempt+1}: error — {e}")
                    time.sleep(3)
            
            # If Shadow DOM button not found, try clicking the chain link icon in the formatting toolbar
            if not link_embed_clicked:
                print("    [*] Trying chain link icon in formatting toolbar...")
                try:
                    # The formatting toolbar has a link icon (chain link) button
                    # Find it by looking for buttons/icons in the toolbar area below the body text
                    link_icon_pos = page.evaluate("""() => {
                        // Look for buttons that contain a link/chain icon
                        // These are typically in the formatting toolbar
                        const buttons = document.querySelectorAll('button, [role="button"], a');
                        for (const btn of buttons) {
                            const rect = btn.getBoundingClientRect();
                            // The formatting toolbar is typically below the body text area (y > 300)
                            if (rect.y > 250 && rect.y < 500 && rect.x < 400 && rect.width > 10 && rect.width < 50) {
                                // Check if it contains an SVG (icon)
                                const svg = btn.querySelector('svg');
                                if (svg) {
                                    const svgRect = svg.getBoundingClientRect();
                                    if (svgRect.width > 0 && svgRect.height > 0) {
                                        // Check aria-label or title for link
                                        const aria = btn.getAttribute('aria-label') || '';
                                        const title = btn.getAttribute('title') || '';
                                        const text = btn.textContent || '';
                                        if (aria.toLowerCase().includes('link') || 
                                            title.toLowerCase().includes('link') ||
                                            text.toLowerCase().includes('link')) {
                                            return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, source: 'aria/title match'};
                                        }
                                        // If no aria-label, try the first button in the toolbar (usually link)
                                        // Check if siblings contain bold/italic (formatting toolbar)
                                        const parent = btn.parentElement;
                                        if (parent) {
                                            const pText = parent.textContent || '';
                                            if (pText.includes('Bold') || pText.includes('bold') || pText.includes('B')) {
                                                // This is likely the formatting toolbar — first button is usually link
                                                return {x: rect.x + rect.width/2, y: rect.y + rect.height/2, source: 'formatting toolbar first button'};
                                            }
                                        }
                                    }
                                }
                            }
                        }
                        
                        // Fallback: find all small buttons in the toolbar area and return the first one
                        const allBtns = document.querySelectorAll('button');
                        const candidates = [];
                        for (const btn of allBtns) {
                            const rect = btn.getBoundingClientRect();
                            if (rect.y > 250 && rect.y < 500 && rect.x < 400 && rect.width > 10 && rect.width < 50) {
                                candidates.push({x: rect.x + rect.width/2, y: rect.y + rect.height/2, w: rect.width});
                            }
                        }
                        if (candidates.length > 0) {
                            return {x: candidates[0].x, y: candidates[0].y, source: 'first small button in toolbar area'};
                        }
                        
                        return null;
                    }""")
                    
                    if link_icon_pos:
                        click_x = int(link_icon_pos['x'])
                        click_y = int(link_icon_pos['y'])
                        print(f"    [*] Clicking link icon at ({click_x}, {click_y}) — source: {link_icon_pos.get('source', '')}")
                        page.mouse.move(click_x, click_y, steps=10)
                        time.sleep(0.5)
                        page.mouse.click(click_x, click_y)
                        time.sleep(3)
                        print("    [+] Link icon clicked!")
                        link_embed_clicked = True
                    else:
                        print("    [!] No link icon found in toolbar")
                except Exception as e:
                    print(f"    [!] Error finding link icon: {e}")
            
            # Now find the "Link URL" input field that appeared
            # The field label says "Link URL *" — the input is right below it
            if link_embed_clicked:
                print("    [*] Looking for Link URL input field...")
                
                url_field_found = False
                for attempt in range(5):
                    try:
                        # Find the "Link URL" label element and click below it
                        label_pos = page.evaluate("""() => {
                            const all = document.querySelectorAll('*');
                            for (const el of all) {
                                // Get only direct text nodes (not child element text)
                                const ownText = Array.from(el.childNodes)
                                    .filter(n => n.nodeType === 3)
                                    .map(n => n.textContent)
                                    .join('');
                                if (ownText.includes('Link URL')) {
                                    const rect = el.getBoundingClientRect();
                                    if (rect.width > 0 && rect.height > 0) {
                                        // The input field should be below or right after the label
                                        return {
                                            x: rect.x + 50,
                                            y: rect.bottom + 25,
                                            labelX: rect.x,
                                            labelY: rect.y,
                                            labelBottom: rect.bottom
                                        };
                                    }
                                }
                            }
                            return null;
                        }""")
                        
                        if label_pos:
                            input_x = int(label_pos['x'])
                            input_y = int(label_pos['y'])
                            print(f"    [*] Clicking URL input at ({input_x}, {input_y})...")
                            page.mouse.click(input_x, input_y)
                            time.sleep(1)
                            
                            # Type the URL
                            page.keyboard.type(url, delay=30)
                            time.sleep(2)
                            url_field_found = True
                            print("    [+] URL typed into Link URL field!")
                            break
                        else:
                            print(f"    [*] Link URL label not found (attempt {attempt+1})")
                            time.sleep(2)
                    except Exception as e:
                        print(f"    [*] Error finding URL field (attempt {attempt+1}): {e}")
                        time.sleep(2)
                
                if not url_field_found:
                    print("    [!] Could not find Link URL field — falling back to body paste")
            
            # Take a screenshot after Link Embed click
            self.take_screenshot(page, "after_link_tab")
            
            # Wait for link preview to load if URL was entered via Link URL field
            if url_field_found:
                print("    [*] Waiting for link preview to load...")
                time.sleep(8)

            # Fill in URL — if already filled via Link URL field, skip this
            if not url_field_found if link_embed_clicked else True:
                print("    [*] Entering URL...")
                url_selectors = [
                    'input[name="url"]',
                    'input[type="url"]',
                    'textarea[placeholder*="URL"]',
                    'input[placeholder*="URL"]',
                    'input[placeholder*="url"]',
                    'input[placeholder*="Url"]',
                    'input[placeholder*="link"]',
                    'input[placeholder*="Link"]',
                    'input[placeholder*="Link URL"]',
                    '#post-url',
                    '[data-testid="post-url"]',
                ]
                url_filled = False
                for sel in url_selectors:
                    try:
                        if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                            HumanBehavior.type_human(page, sel, url)
                            url_filled = True
                            print(f"    [+] URL filled via selector: {sel}")
                            break
                    except:
                        continue

                if not url_filled:
                    # No URL field found — paste into body text area instead
                    # IMPORTANT: The body text area is DIFFERENT from the title field
                    # On Reddit's new UI, both are contenteditable divs with role="textbox"
                    # We need to skip the title (which was already filled) and find the BODY area
                    print("    [*] No URL field found — pasting URL into body text area...")
                    # First, find how many contenteditable divs exist
                    body_count = page.locator('div[contenteditable="true"][role="textbox"]').count()
                    print(f"    [*] Found {body_count} contenteditable textboxes on page")
                    
                    body_selectors = [
                        'div[contenteditable="true"][role="textbox"] >> nth=1',  # Second one (first is title)
                        'textarea[placeholder*="Body"]',
                        'textarea[placeholder*="body"]',
                        'div[role="textbox"]:not([placeholder*="Title"])',
                    ]
                    
                    for sel in body_selectors:
                        try:
                            if 'nth=1' in sel:
                                # Use Playwright's nth() to get the second contenteditable div
                                loc = page.locator('div[contenteditable="true"][role="textbox"]')
                                if loc.count() > 1:
                                    loc = loc.nth(1)
                                    HumanBehavior.move_and_click(page, sel.split(" >> ")[0])
                                    # Re-select the correct element
                                    loc.click()
                                else:
                                    continue
                            else:
                                if page.locator(sel).count() > 0:
                                    HumanBehavior.move_and_click(page, sel)
                            
                            HumanBehavior.random_delay(0.3, 0.8)
                            # Clear any existing content first
                            page.keyboard.press("Control+a")
                            time.sleep(0.1)
                            page.keyboard.press("Backspace")
                            time.sleep(0.2)
                            # Type ONLY the URL — nothing else
                            page.keyboard.type(url, delay=random.randint(30, 80))
                            url_filled = True
                            print(f"    [+] URL entered into body via: {sel}")
                            break
                        except:
                            continue

                if not url_filled:
                    screenshot = self.take_screenshot(page, "url_and_body_not_found")
                    return False, None, screenshot
            else:
                print("    [+] URL already filled via Link URL field — skipping")
                url_filled = True

            # Fill in title FIRST (before URL) to prevent mixing
            print("    [*] Entering title...")
            title_selectors = [
                'textarea[placeholder*="Title"]',
                'textarea[name="title"]',
                'input[name="title"]',
                'input[placeholder*="Title"]',
                '#post-title',
                '[data-testid="post-title"]',
            ]
            title_filled = False
            for sel in title_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        page.locator(sel).first.click()
                        time.sleep(0.2)
                        page.keyboard.press("Control+a")
                        time.sleep(0.1)
                        page.keyboard.press("Backspace")
                        time.sleep(0.2)
                        HumanBehavior.type_human(page, sel, title)
                        title_filled = True
                        print(f"    [+] Title filled via selector: {sel}")
                        break
                except:
                    continue

            if not title_filled:
                screenshot = self.take_screenshot(page, "title_field_not_found")
                return False, None, screenshot

            HumanBehavior.random_delay(1, 3)

            # Now fill URL — either via Link URL field or body text area
            # IMPORTANT: Use selectors that DON'T match the title field
            print("    [*] Entering URL...")

            HumanBehavior.random_delay(1, 2)

            # Mouse movement before submit
            HumanBehavior.mouse_move_random(page)
            HumanBehavior.random_delay(0.5, 1.5)

            # Click submit — Reddit's new UI uses "Post" button
            print("    [*] Clicking Post button...")
            submit_selectors = [
                'button:has-text("Post")',
                'button:has-text("Submit")',
                'button[type="submit"]',
                '[data-testid="submit-button"]',
                'button[data-testid="submit-post-button"]',
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        HumanBehavior.move_and_click(page, sel)
                        submitted = True
                        print(f"    [+] Clicked: {sel}")
                        break
                except:
                    continue

            if not submitted:
                screenshot = self.take_screenshot(page, "submit_button_not_found")
                return False, None, screenshot

            # Wait for post to go through
            print("    [*] Waiting for post to submit...")
            time.sleep(random.uniform(5, 10))

            # Handle mature content popup if it appears
            mature_selectors = [
                'button:has-text("Yes")',
                'button:has-text("I\'m Over 18")',
                'button:has-text("I am 18")',
                'button:has-text("over 18")',
            ]
            for sel in mature_selectors:
                try:
                    if page.locator(sel).count() > 0:
                        print(f"    [*] Mature content popup — clicking: {sel}")
                        HumanBehavior.move_and_click(page, sel)
                        HumanBehavior.random_delay(2, 4)
                        break
                except:
                    continue

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

            # Method 1: Check if URL changed to the new post (most reliable)
            # After posting, Reddit redirects to the subreddit page with ?created=... params
            current_url = page.url
            if "created=" in current_url or "createdPostType" in current_url:
                # Extract the post ID from the URL params
                import re as re_mod
                created_match = re_mod.search(r'created=(t3_\w+)', current_url)
                if created_match:
                    post_id = created_match.group(1)
                    # Construct the post URL — we need the subreddit and title slug
                    reddit_post_url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id.replace('t3_', '')}/"
                    print(f"    [+] Post URL (from created param): {reddit_post_url}")

            # Method 2: If no created param, check if URL redirected to the post directly
            if not reddit_post_url and "/comments/" in current_url:
                # Make sure it's not a random post — check it's recent
                reddit_post_url = current_url.split("?")[0]  # Remove query params
                print(f"    [+] Post URL (from redirect): {reddit_post_url}")

            # Method 3: Check the user's profile for their newest post
            if not reddit_post_url:
                try:
                    print(f"    [*] Checking user profile for latest post...")
                    page.goto(f"https://www.reddit.com/user/{reddit_username}/submitted/", wait_until="domcontentloaded", timeout=30000)
                    time.sleep(8)
                    
                    # Find the first post link on the profile page
                    # The profile shows posts sorted by "new" — first one is our newest post
                    # Look for links that contain /comments/ AND the subreddit name
                    post_links = page.locator(f'a[href*="/r/{subreddit}/comments/"]')
                    if post_links.count() > 0:
                        href = post_links.first.get_attribute("href") or ""
                        if href:
                            if href.startswith("/"):
                                reddit_post_url = f"https://www.reddit.com{href}"
                            elif not href.startswith("http"):
                                reddit_post_url = f"https://www.reddit.com/{href}"
                            else:
                                reddit_post_url = href
                            reddit_post_url = reddit_post_url.split("?")[0]  # Remove query params
                            print(f"    [+] Post URL (from profile - subreddit match): {reddit_post_url}")
                    else:
                        # Fallback: any /comments/ link that's not a user profile
                        all_links = page.locator('a[href*="/comments/"]')
                        for i in range(all_links.count()):
                            href = all_links.nth(i).get_attribute("href") or ""
                            # Skip user profile links and game promos
                            if "/user/" in href:
                                continue
                            if "FarmMergeValley" in href or "games_drawer" in href or "entry_point" in href:
                                continue
                            if href.startswith("/"):
                                reddit_post_url = f"https://www.reddit.com{href}"
                            elif not href.startswith("http"):
                                reddit_post_url = f"https://www.reddit.com/{href}"
                            else:
                                reddit_post_url = href
                            reddit_post_url = reddit_post_url.split("?")[0]
                            print(f"    [+] Post URL (from profile - first match): {reddit_post_url}")
                            break
                except Exception as e:
                    print(f"    [*] Profile check error: {e}")

            if not reddit_post_url:
                # Even if we can't find the URL, the post might have succeeded
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
            url = fields.get("RedGifs Link", "")
            title = fields.get("Title", "")
            niche = fields.get("Niche", "")
            subreddit_raw = fields.get("Subreddit", "")
            account_name = fields.get("Account", "")

            # Clean subreddit name (remove r/ prefix if present)
            subreddit = subreddit_raw.replace("r/", "").replace("/r/", "").strip() if subreddit_raw else niche.lower()

            print(f"\n  [{i}/{len(links)}] Link: {url}")
            print(f"    Account: {account_name}")
            print(f"    Subreddit: r/{subreddit}")
            print(f"    Title: {title}")
            print(f"    Niche: {niche}")

            # Check niche/subreddit match
            if niche and subreddit:
                subreddit_niche = self._get_subreddit_niche(subreddit)
                if subreddit_niche and subreddit_niche != "General" and niche != "General":
                    if subreddit_niche != niche:
                        print(f"    [!] Niche mismatch: link niche '{niche}' does not match subreddit niche '{subreddit_niche}' — skipping")
                        # Update post status to Failed
                        self._at_update_single(self.posts_table, link_record["id"], {
                            "Status": "Failed",
                            "Error": f"Niche mismatch: {niche} vs {subreddit_niche}",
                        })
                        fail_count += 1
                        continue
                    else:
                        print(f"    [+] Niche match: {niche} == {subreddit_niche}")

            # Check banned words in title
            if subreddit:
                banned_words = self._get_subreddit_banned_words(subreddit)
                if banned_words:
                    title_lower = title.lower()
                    found_banned = [w for w in banned_words if w.lower() in title_lower]
                    if found_banned:
                        print(f"    [!] Title contains banned words: {found_banned} — skipping (r/{subreddit} rules)")
                        self._at_update_single(self.posts_table, link_record["id"], {
                            "Status": "Failed",
                            "Error": f"Banned words in title: {', '.join(found_banned)} (r/{subreddit})",
                        })
                        fail_count += 1
                        continue

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
            if account_status == "Banned":
                print(f"    [!] Account is Banned — skipping")
                continue
            # Only post from Active accounts
            if account_status != "Active":
                print(f"    [!] Account status is '{account_status}' (not Active) — skipping")
                continue

            # Get AdsPower profile ID — stored in Notes or a custom field
            adspower_id = account_fields.get("AdsPower ID", "") or account_fields.get("Notes", "")

            # Get credentials
            reddit_username = account_fields.get("Username", account_name)
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
            tunnel_pid = conn.get("_tunnel_pid")

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

                    # Login to Reddit — always try, even if session might be alive
                    # The login function checks if already logged in first
                    login_success = self.login_reddit(page, reddit_username, reddit_password)

                    if not login_success:
                        # Retry login once more
                        print(f"    [*] Retrying login...")
                        time.sleep(3)
                        login_success = self.login_reddit(page, reddit_username, reddit_password)

                    if not login_success:
                        print(f"    [!] Login failed — marking link as Failed")
                        screenshot = self.take_screenshot(page, f"login_fail_{reddit_username}")
                        self._at_update_single(self.posts_table, link_record["id"], {
                            "Status": "Failed",
                            "Error Screenshot": [{"url": f"file://{screenshot}"}] if screenshot else None,
                        })
                        fail_count += 1
                        page.close()
                        continue

                    # Post the link
                    success, post_url, error_screenshot = self.post_link(
                        page, subreddit, url, title, reddit_username
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

                        self._at_update_single(self.posts_table, link_record["id"], update_fields)

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
                                self.posts_table, link_record["id"],
                                "Error Screenshot", error_screenshot
                            )

                        self._at_update_single(self.posts_table, link_record["id"], update_fields)

                        # Check if account should be marked Banned
                        if error_screenshot and "captcha" in error_screenshot:
                            print(f"    [!] Captcha detected — marking account as Cooldown")
                            self._at_update_single(self.accounts_table, account_info["id"], {
                                "Account Status": "Cooldown",
                            })

                    page.close()

            except Exception as e:
                print(f"    [!] Browser error: {e}")
                # If proxy error, wait and retry once
                if "SOCKS" in str(e) or "ERR_PROXY" in str(e) or "ERR_SOCKS" in str(e):
                    print(f"    [*] Proxy connection failed — waiting 60s for IP rotation...")
                    self.adspower.rotate_ip()
                    time.sleep(60)
                fail_count += 1

            finally:
                # Stop AdsPower browser
                print(f"    [*] Stopping AdsPower profile...")
                self.adspower.stop_profile(adspower_id, tunnel_pid=tunnel_pid)

                # Rotate proxy IP for next account
                self.adspower.rotate_ip()

                time.sleep(random.uniform(30, 60))  # Cooldown between accounts (longer for proxy rotation)

        # Summary
        print(f"\n[*] Automation Complete:")
        print(f"    Success: {success_count}")
        print(f"    Failed:  {fail_count}")
        print(f"    Total:   {len(links)}")


def main():
    parser = argparse.ArgumentParser(description="Reddit Auto-Poster via AdsPower + Playwright")
    parser.add_argument("--token", default=os.getenv("AIRTABLE_PAT"), help="Airtable PAT")
    parser.add_argument("--base-id", default=os.getenv("AIRTABLE_BASE_ID"), help="Airtable base ID")
    parser.add_argument("--links-table", default="Posts", help="Posts table name")
    parser.add_argument("--accounts-table", default="Accounts", help="Accounts table name")
    parser.add_argument("--post", action="store_true", help="Run the auto-poster")
    parser.add_argument("--dry-run", action="store_true", help="Preview without posting")
    parser.add_argument("--max", type=int, help="Maximum posts this run")
    parser.add_argument("--account", type=str, help="Only post for this account name")
    parser.add_argument("--no-headless", action="store_true", help="Show browser (debug mode)")
    parser.add_argument("--screenshot-dir", default="screenshots", help="Directory for error screenshots")
    parser.add_argument("--adspower-key", default=os.getenv("ADSPOWER_API_KEY"), help="AdsPower API key (or set ADSPOWER_API_KEY env var)")
    parser.add_argument("--adspower-ssh", default=os.getenv("ADSPOWER_SSH_HOST"), help="SSH host for remote AdsPower (e.g. macmini) or set ADSPOWER_SSH_HOST env var")
    parser.add_argument("--rotation-url", default=os.getenv("ADSPOWER_ROTATION_URL", "https://i.fxdx.in/actionlinks/do/changeip/SSeRX4OdQPaOy6WhBgqzag"), help="Proxy IP rotation link (or set ADSPOWER_ROTATION_URL env var)")
    parser.add_argument("--list-profiles", action="store_true", help="List all AdsPower profiles and exit")

    args = parser.parse_args()

    if not args.token or not args.base_id:
        print("[!] Missing credentials. Use --token and --base-id or set AIRTABLE_PAT and AIRTABLE_BASE_ID env vars.")
        print(f"    token={'set' if args.token else 'MISSING'}, base_id={'set' if args.base_id else 'MISSING'}")
        return

    # Handle --list-profiles
    if args.list_profiles:
        controller = AdsPowerController(
            api_key=args.adspower_key,
            ssh_host=args.adspower_ssh,
        )
        profiles = controller.list_profiles()
        print(f"\n[*] {len(profiles)} AdsPower Profiles on {'remote (' + args.adspower_ssh + ')' if args.adspower_ssh else 'local'}:\n")
        for i, p in enumerate(profiles, 1):
            proxy = p.get("user_proxy_config", {})
            proxy_type = proxy.get("proxy_soft", "?")
            if proxy_type == "no_proxy":
                proxy_str = "no proxy"
            else:
                proxy_str = f"{proxy.get('proxy_type','?')} {proxy.get('proxy_host','?')}:{proxy.get('proxy_port','?')}"
            print(f"  {i:>3}. {p.get('user_id','?'):<12} | {p.get('name','?'):<25} | {proxy_str}")
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
        adspower_api_key=args.adspower_key,
        adspower_ssh_host=args.adspower_ssh,
        adspower_rotation_url=args.rotation_url,
    )

    poster.run(
        max_posts=args.max,
        account_filter=args.account,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
