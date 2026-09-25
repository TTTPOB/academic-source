"""Extract cookies via CloakBrowser for publisher access.

Opens a visible CloakBrowser window, lets user log in to their institution,
then captures and saves publisher cookies for automated downloads.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .log import get_logger

log = get_logger()

# Publisher domains that benefit from institutional cookies
PUBLISHER_DOMAINS = [
    "sciencedirect.com",
    "elsevier.com",
    "springer.com",
    "springerlink.com",
    "link.springer.com",
    "onlinelibrary.wiley.com",
    "wiley.com",
    "ieeexplore.ieee.org",
    "ieee.org",
    "nature.com",
    "science.org",
    "sciencemag.org",
    "tandfonline.com",
    "pnas.org",
    "jstor.org",
    "acs.org",
    "rsc.org",
    "cambridge.org",
    "oup.com",
    "academic.oup.com",
    "sagepub.com",
    "sage.cnpereading.com",
    "mdpi.com",
    "frontiersin.org",
]

# Publisher login URLs for quick access
PUBLISHER_LOGIN_URLS = {
    "elsevier": "https://www.sciencedirect.com/",
    "springer": "https://link.springer.com/",
    "wiley": "https://onlinelibrary.wiley.com/",
    "nature": "https://www.nature.com/",
    "science": "https://www.science.org/",
    "ieee": "https://ieeexplore.ieee.org/",
    "tandfonline": "https://www.tandfonline.com/",
    "pnas": "https://www.pnas.org/",
    "acs": "https://pubs.acs.org/",
    "rsc": "https://pubs.rsc.org/",
    "aip": "https://pubs.aip.org/",
    "aps": "https://journals.aps.org/",
    "iop": "https://iopscience.iop.org/",
    "oxford": "https://academic.oup.com/",
    "acm": "https://dl.acm.org/",
    "sage-cn": "https://sage.cnpereading.com/login",
}


def _save_cookies_json(cookies: list[dict[str, Any]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(cookies, indent=2, ensure_ascii=False), encoding="utf-8")


def cookies_to_netscape(cookies: list[dict[str, Any]]) -> str:
    """Convert a list of cookie dicts to Netscape HTTP Cookie File format."""
    lines = ["# Netscape HTTP Cookie File\n"]
    for c in cookies:
        domain = c.get("domain", "")
        flag = "TRUE" if domain.startswith(".") else "FALSE"
        path = c.get("path", "/")
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = str(int(c.get("expires", 0)) or 0)
        name = c.get("name", "")
        value = c.get("value", "")
        lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{name}\t{value}\n")
    return "".join(lines)


def _save_cookies_netscape(cookies: list[dict[str, Any]], output_path: Path) -> None:
    output_path.write_text(cookies_to_netscape(cookies), encoding="utf-8")


def _is_publisher_cookie(cookie: dict[str, Any]) -> bool:
    """Check if a cookie belongs to a known publisher domain."""
    domain = cookie.get("domain", "").lstrip(".")
    return any(domain.endswith(d) for d in PUBLISHER_DOMAINS)


def extract_via_browser(
    config: dict[str, Any],
    *,
    url: str = "https://www.sciencedirect.com/",
    max_wait: int = 300,
) -> dict[str, Any]:
    """Open visible browser for institutional login, then capture publisher cookies.

    Args:
        config: scansci-pdf config dict.
        url: URL to open (default: ScienceDirect).
        max_wait: Max seconds to wait for login.

    Returns:
        Result dict with success, cookies_count, domains, etc.
    """
    try:
        from .browser_backend import launch
        from .browser_backend import is_available as _browser_backend_available
        if not _browser_backend_available():
            return {
                "success": False,
                "error": "no browser backend available",
                "fix": "pip install patchright (or pip install cloakbrowser)",
            }
    except ImportError:
        return {
            "success": False,
            "error": "no browser backend available",
            "fix": "pip install patchright (or pip install cloakbrowser)",
        }

    from .config import DATA_DIR
    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / "publisher_cookies.json"
    netscape_file = cache_dir / "publisher_cookies.txt"

    log.info(f"   [cookies] Opening browser: {url}")
    print(f"\n  请在浏览器中登录你的机构账号（如学校图书馆）")
    print(f"  打开页面: {url}")
    print(f"  登录完成后关闭浏览器窗口即可\n")

    try:
        browser = launch(headless=False, humanize=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            log.info(f"   [cookies] Page load warning: {exc}")

        # Auto-dismiss cookie consent popups (common on Elsevier/Cell/Wiley/etc.)
        time.sleep(2)
        for js in [
            # Elsevier/Cell cookie banner
            "document.querySelector('#onetrust-accept-btn-handler')?.click()",
            # OneTrust general
            "document.querySelector('.onetrust-close-btn-handler')?.click()",
            # Generic consent banners
            "document.querySelector('[class*=\"cookie-accept\"], [class*=\"consent-accept\"]')?.click()",
            "document.querySelector('button[id*=\"accept\"], button[class*=\"accept\"]')?.click()",
        ]:
            try:
                page.evaluate(js)
            except Exception:
                pass

        # Wait for user to close browser
        try:
            page.wait_for_event("close", timeout=max_wait * 1000)
        except Exception:
            pass

        # Capture all cookies
        all_cookies = context.cookies()

        if not all_cookies:
            browser.close()
            return {
                "success": False,
                "message": "未捕获到 cookies。请确保已登录机构账号。",
            }

        # Filter publisher cookies + keep all for completeness
        publisher_cookies = [c for c in all_cookies if _is_publisher_cookie(c)]
        save_cookies = publisher_cookies if publisher_cookies else all_cookies

        # Save
        _save_cookies_json(save_cookies, cookie_file)
        _save_cookies_netscape(save_cookies, netscape_file)

        # Import into CloakBrowser server if running
        browser_imported = 0
        try:
            from .browser_engine import import_cookies, is_available
            if is_available(config):
                browser_imported = import_cookies(netscape_file, config)
        except Exception:
            pass

        browser.close()

        domains_found = list({c.get("domain", "").lstrip(".") for c in save_cookies})[:10]
        return {
            "success": True,
            "cookies_count": len(save_cookies),
            "total_captured": len(all_cookies),
            "cookie_file": str(cookie_file),
            "netscape_file": str(netscape_file),
            "domains": domains_found,
            "browser_imported": browser_imported,
            "message": f"捕获 {len(all_cookies)} 个 cookies，其中 {len(publisher_cookies)} 个属于出版社。"
                       f"已保存，后续下载自动使用。",
        }

    except Exception as exc:
        log.info(f"   [cookies] Error: {exc}")
        return {"success": False, "error": str(exc)}


def load_saved_cookies(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Load previously saved publisher cookies, filtering out expired ones."""
    from .config import DATA_DIR
    cookie_file = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / "publisher_cookies.json"
    if not cookie_file.exists():
        return []
    try:
        cookies = json.loads(cookie_file.read_text(encoding="utf-8"))
    except Exception:
        return []
    now = time.time()
    return [c for c in cookies if _is_cookie_valid(c, now)]


SCIENCE_HTTP_ORIGIN = "https://www.science.org/"
SCIENCE_HTTP_STATE_FILE = "science_http_state.json"


def _science_cookie_applies(cookie: dict[str, Any]) -> bool:
    """Match the Science origin with cookie domain and path boundaries."""
    domain = cookie.get("domain")
    path = cookie.get("path", "/")
    if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
        return False
    if not isinstance(domain, str) or not isinstance(path, str):
        return False
    host = "www.science.org"
    bare = domain.lstrip(".").lower()
    if not bare or not (host == bare or host.endswith("." + bare)):
        return False
    return path.startswith("/") and all(
        target == path or target.startswith(path.rstrip("/") + "/")
        for target in ("/doi/epdf/10.1126/example", "/doi/pdfdirect/10.1126/example")
    )


def save_science_http_state(
    user_agent: str, cookies: list[dict[str, Any]], config: dict[str, Any]
) -> None:
    """Save the browser context's matched Science identity in one file."""
    from .config import DATA_DIR

    matched = [c for c in cookies if isinstance(c, dict) and _science_cookie_applies(c) and _is_cookie_valid(c)]
    if not isinstance(user_agent, str) or not user_agent.strip() or not any(
        c.get("name") == "cf_clearance" and c.get("value") for c in matched
    ):
        return
    path = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / SCIENCE_HTTP_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"origin": SCIENCE_HTTP_ORIGIN, "user_agent": user_agent, "cookies": matched}, ensure_ascii=False), encoding="utf-8")


def load_science_http_state(config: dict[str, Any]) -> dict[str, Any] | None:
    """Load only a paired, currently usable Science clearance identity."""
    from .config import DATA_DIR

    path = Path(config.get("cache_dir", str(DATA_DIR / "cache"))) / SCIENCE_HTTP_STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        cookies = state["cookies"]
        if state["origin"] != SCIENCE_HTTP_ORIGIN or not isinstance(state["user_agent"], str) or not state["user_agent"].strip() or not isinstance(cookies, list):
            return None
        valid = [c for c in cookies if isinstance(c, dict) and _science_cookie_applies(c) and _is_cookie_valid(c)]
        if not any(c.get("name") == "cf_clearance" and c.get("value") for c in valid):
            return None
        return {"origin": SCIENCE_HTTP_ORIGIN, "user_agent": state["user_agent"], "cookies": valid}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _is_cookie_valid(cookie: dict[str, Any], now: float | None = None) -> bool:
    """Check expiry; Playwright uses -1 for session cookies."""
    expires = cookie.get("expires", 0)
    if expires in (None, 0, -1):
        return True
    if now is None:
        now = time.time()
    return expires > now


def inject_cookies(session: Any, config: dict[str, Any]) -> None:
    """Inject saved publisher cookies into a requests.Session."""
    cookies = load_saved_cookies(config)
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))


def merge_cookies(new_cookies: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge new cookies into saved publisher cookies, dedup by (name, domain, path).

    Filters to publisher domains only. Updates existing cookies in-place
    (new value overwrites old) and appends genuinely new ones. Saves both
    JSON and Netscape formats.

    Returns the merged cookie list.
    """
    from .config import DATA_DIR

    cache_dir = Path(config.get("cache_dir", str(DATA_DIR / "cache")))
    cookie_file = cache_dir / "publisher_cookies.json"
    netscape_file = cache_dir / "publisher_cookies.txt"

    existing = load_saved_cookies(config)

    # Build index of existing cookies by (name, domain, path)
    key = lambda c: (c.get("name", ""), c.get("domain", ""), c.get("path", "/"))
    merged: dict[tuple, dict[str, Any]] = {key(c): c for c in existing}

    for c in new_cookies:
        if not _is_publisher_cookie(c):
            continue
        merged[key(c)] = c

    result = list(merged.values())

    if result:
        _save_cookies_json(result, cookie_file)
        _save_cookies_netscape(result, netscape_file)

    return result


def _is_doi(text: str) -> bool:
    return text.startswith("10.") and "/" in text


def publisher_login(
    identifier: str,
    config: dict[str, Any],
    *,
    max_wait: int = 300,
) -> dict[str, Any]:
    """Open browser for institutional login on a publisher article page.

    Takes a DOI or publisher name, resolves to the article page, and opens
    a visible stealth browser. User clicks "Access through your institution",
    completes SSO login, then closes the browser. Cookies are captured and
    imported into CloakBrowser for automated downloads.

    Args:
        identifier: DOI (e.g. 10.1126/science.aec6396) or publisher name
                    (e.g. "elsevier", "wiley", "nature", "springer", "ieee",
                    "science", "tandfonline", "pnas", "acs", "rsc", "aip",
                    "aps", "iop", "oxford", "acm")
        config: scansci-pdf config dict
        max_wait: Max seconds to wait for login (default 300)

    Returns:
        Result dict with success, cookies_count, domains, message.
    """
    import re

    # Resolve identifier to a URL
    if _is_doi(identifier):
        # DOI → resolve to article URL
        url = f"https://doi.org/{identifier}"
        log.info(f"   [login] DOI detected, opening article: {url}")
    elif identifier.lower() in PUBLISHER_LOGIN_URLS:
        url = PUBLISHER_LOGIN_URLS[identifier.lower()]
        log.info(f"   [login] Publisher '{identifier}', opening: {url}")
    elif re.match(r"https?://", identifier):
        url = identifier
        log.info(f"   [login] URL detected: {url}")
    else:
        # Try fuzzy publisher name match
        lower = identifier.lower()
        match = None
        for key in PUBLISHER_LOGIN_URLS:
            if key in lower or lower in key:
                match = key
                break
        if match:
            url = PUBLISHER_LOGIN_URLS[match]
            log.info(f"   [login] Matched publisher '{match}', opening: {url}")
        else:
            available = ", ".join(sorted(PUBLISHER_LOGIN_URLS.keys()))
            return {
                "success": False,
                "error": f"Unknown publisher: '{identifier}'. Available: {available}",
            }

    return extract_via_browser(config, url=url, max_wait=max_wait)
