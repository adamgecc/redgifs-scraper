"""
AdsPower Proxy Integration
==========================
Pulls proxy config from an AdsPower browser profile so the scraper
can route requests through the same IP the profile uses.

AdsPower exposes a local API at port 50325 (default) that returns
the proxy configuration for a given profile ID.

Usage:
    from adspower_proxy import get_adspower_proxy
    proxy = get_adspower_proxy("profile_id_here")
    # proxy = {"http": "http://user:pass@ip:port", "https": "http://user:pass@ip:port"}
"""

import requests
import json
from urllib.parse import quote


# AdsPower local API defaults
ADSPOWER_API = "http://local.adspower.net:50325"
ADSPOWER_STATUS_API = "http://local.adspower.net:50325/status"


def check_adspower_running():
    """Check if AdsPower local API is reachable."""
    try:
        resp = requests.get(ADSPOWER_STATUS_API, timeout=5)
        return resp.status_code == 200
    except requests.exceptions.RequestException:
        return False


def get_adspower_proxy(profile_id, api_base=ADSPOWER_API):
    """
    Fetch proxy configuration from an AdsPower profile.

    AdsPower stores proxy info in the profile config. We query the
    local API to get the profile's proxy details and format them
    into a requests-compatible proxy dict.

    Args:
        profile_id: AdsPower profile ID (the long hash string)
        api_base: AdsPower local API base URL

    Returns:
        dict: {"http": "...", "https": "..."} for requests, or None on failure
    """
    if not check_adspower_running():
        print("[!] AdsPower local API not reachable. Is AdsPower running?")
        return None

    # Query profile info via AdsPower API
    endpoint = f"{api_base}/api/v1/browser/list"
    params = {"user_id": profile_id}

    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[!] AdsPower API request failed: {e}")
        return None

    data = resp.json()
    if data.get("code") != 0:
        print(f"[!] AdsPower API error: {data.get('msg', 'unknown error')}")
        return None

    profiles = data.get("data", {}).get("list", [])
    if not profiles:
        print(f"[!] No profile found with ID {profile_id}")
        return None

    profile = profiles[0]
    proxy_config = profile.get("user_proxy_config", {})

    proxy_type = proxy_config.get("type", "http").lower()  # http, socks5, etc.

    # AdsPower proxy fields
    host = proxy_config.get("host", "")
    port = proxy_config.get("port", "")
    user = proxy_config.get("user", "")
    password = proxy_config.get("password", "")

    if not host or not port:
        print("[!] No proxy configured in this AdsPower profile (direct connection)")
        return None

    # Build proxy URL
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    else:
        auth = f"{host}:{port}"

    proxy_url = f"{proxy_type}://{auth}"

    print(f"[*] AdsPower proxy: {proxy_type}://{host}:{port} (user={user or 'none'})")

    return {"http": proxy_url, "https": proxy_url}


def start_adspower_browser(profile_id, api_base=ADSPOWER_API, headless=0):
    """
    Start an AdsPower browser profile via local API.
    Useful if we later switch to Playwright-through-AdsPower approach.

    Args:
        profile_id: AdsPower profile ID
        headless: 0 = visible, 1 = headless

    Returns:
        dict: connection info (ws endpoint, debug port) or None
    """
    if not check_adspower_running():
        print("[!] AdsPower local API not reachable.")
        return None

    endpoint = f"{api_base}/api/v1/browser/start"
    params = {"user_id": profile_id, "headless": headless}

    try:
        resp = requests.get(endpoint, params=params, timeout=60)
        resp.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"[!] Failed to start AdsPower browser: {e}")
        return None

    data = resp.json()
    if data.get("code") != 0:
        print(f"[!] AdsPower start error: {data.get('msg')}")
        return None

    conn = data.get("data", {})
    ws_endpoint = conn.get("ws", {}).get("selenium", "")
    debug_port = conn.get("debug_port", "")

    print(f"[*] AdsPower browser started | debug port: {debug_port}")
    print(f"    WebSocket: {ws_endpoint}")

    return conn


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python adspower_proxy.py <profile_id>")
        sys.exit(1)

    proxy = get_adspower_proxy(sys.argv[1])
    if proxy:
        print(f"Proxy dict: {proxy}")
    else:
        print("No proxy retrieved.")
