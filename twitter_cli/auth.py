"""Cookie authentication for Twitter/X.

Supports:
1. Environment variables: TWITTER_AUTH_TOKEN + TWITTER_CT0
2. Auto-extract from browser via browser-cookie3
   Extracts ALL Twitter cookies for full browser-like fingerprint.
   Prefers in-process extraction (required on macOS for Keychain access),
   falls back to subprocess if in-process fails (e.g. SQLite lock).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from .constants import BEARER_TOKEN, get_user_agent
from .exceptions import AuthenticationError

logger = logging.getLogger(__name__)

# Domains to match for Twitter cookies
_TWITTER_DOMAINS = {"x.com", "twitter.com", ".x.com", ".twitter.com"}
_COOKIE_CACHE_TTL = max(int(os.environ.get("TWITTER_COOKIE_CACHE_TTL", "900")), 0)


def _is_twitter_domain(domain: str) -> bool:
    return domain in _TWITTER_DOMAINS or domain.endswith(".x.com") or domain.endswith(".twitter.com")


# ---------------------------------------------------------------------------
# Keychain / environment diagnostics
# ---------------------------------------------------------------------------

_KEYCHAIN_ERROR_KEYWORDS = (
    "key for cookie decryption",
    "safe storage",
    "keychain",
    "secretstorage",
)


def _diagnose_keychain_issues(diagnostics: List[str]) -> Optional[str]:
    """Analyse extraction diagnostics for Keychain permission issues.

    Returns a user-friendly hint string, or None.
    """
    lowered = " ".join(diagnostics).lower()
    if not any(kw in lowered for kw in _KEYCHAIN_ERROR_KEYWORDS):
        return None

    is_ssh = bool(os.environ.get("SSH_CLIENT") or os.environ.get("SSH_TTY") or os.environ.get("SSH_CONNECTION"))

    if sys.platform == "darwin":
        if is_ssh:
            return (
                "macOS Keychain is locked (SSH session detected).\n"
                "  Fix: security unlock-keychain ~/Library/Keychains/login.keychain-db\n"
                "  Then retry the command."
            )
        return (
            "macOS Keychain permission denied — your terminal is not authorized to read browser cookie encryption keys.\n"
            "  Fix: Open Keychain Access → search for \"<Browser> Safe Storage\" → Access Control → add your Terminal app.\n"
            "  Or click \"Always Allow\" when the Keychain authorization popup appears."
        )
    if sys.platform == "win32":
        return (
            "Windows DPAPI cookie decryption failed.\n"
            "  Possible causes:\n"
            "  1. Chrome is running (locks the cookie database). Try closing Chrome first.\n"
            "  2. browser_cookie3 may not support the latest Chrome cookie encryption format.\n"
            "  3. If Chrome was running, admin privileges may be required for VSS shadowcopy.\n"
            "  Workaround: Set TWITTER_AUTH_TOKEN and TWITTER_CT0 environment variables manually."
        )
    # Linux: gnome-keyring / SecretStorage issues
    return (
        "System keyring access failed — the cookie encryption key could not be retrieved.\n"
        "  If running headless or via SSH, ensure your keyring daemon is unlocked."
    )


def load_from_env() -> Optional[Dict[str, str]]:
    """Load cookies from environment variables."""
    auth_token = os.environ.get("TWITTER_AUTH_TOKEN", "")
    ct0 = os.environ.get("TWITTER_CT0", "")
    if auth_token and ct0:
        return {"auth_token": auth_token, "ct0": ct0}
    if auth_token or ct0:
        logger.debug(
            "Environment cookies incomplete: auth_token=%s ct0=%s",
            bool(auth_token),
            bool(ct0),
        )
    return None


def verify_cookies(auth_token: str, ct0: str, cookie_string: Optional[str] = None) -> Dict[str, Any]:
    """Verify cookies by calling a Twitter API endpoint.

    Uses curl_cffi for proper TLS fingerprint.
    Tries multiple endpoints. Only raises on clear auth failures (401/403).
    For other errors (404, network), returns empty dict (proceed without verification).
    """
    from .client import _get_cffi_session

    urls = [
        "https://api.x.com/1.1/account/verify_credentials.json",
        "https://x.com/i/api/1.1/account/settings.json",
    ]

    # Use full cookie string if available, otherwise minimal
    cookie_header = cookie_string or "auth_token=%s; ct0=%s" % (auth_token, ct0)

    headers = {
        "Authorization": "Bearer %s" % BEARER_TOKEN,
        "Cookie": cookie_header,
        "X-Csrf-Token": ct0,
        "X-Twitter-Active-User": "yes",
        "X-Twitter-Auth-Type": "OAuth2Session",
        "User-Agent": get_user_agent(),
    }

    # Reuse the shared curl_cffi session for consistent TLS fingerprint
    session = _get_cffi_session()
    attempts = []

    logger.debug(
        "Verifying Twitter cookies with %s cookie header",
        "full forwarded" if cookie_string else "minimal",
    )

    for url in urls:
        endpoint = url.split("/")[-1]
        try:
            resp = session.get(url, headers=headers, timeout=5)
            if resp.status_code in (401, 403):
                raise AuthenticationError(
                    "Cookie expired or invalid (HTTP %d). Please re-login to x.com in your browser." % resp.status_code
                )
            if resp.status_code == 200:
                data = resp.json()
                attempts.append("%s=200" % endpoint)
                logger.debug("Cookie verification succeeded via %s", endpoint)
                return {"screen_name": data.get("screen_name", "")}
            attempts.append("%s=%d" % (endpoint, resp.status_code))
            logger.debug("Verification endpoint %s returned HTTP %d, trying next...", url, resp.status_code)
            continue
        except RuntimeError:
            raise
        except Exception as e:
            attempts.append("%s=%s" % (endpoint, type(e).__name__))
            logger.debug("Verification endpoint %s failed: %s", url, e)
            continue

    # All endpoints failed with non-auth errors — proceed without verification
    logger.info(
        "Cookie verification skipped (attempts: %s), will verify on first API call",
        ", ".join(attempts) if attempts else "none",
    )
    return {}


def _cookie_cache_path() -> str:
    """Return path for the short-lived browser cookie cache."""
    home = os.path.expanduser("~")
    return os.path.join(home, ".twitter-cli", "cookie_cache.json")


def _load_cookie_cache() -> Optional[Dict[str, str]]:
    """Load a fresh browser cookie cache entry, or nil when absent/stale."""
    if _COOKIE_CACHE_TTL <= 0:
        return None
    try:
        cache_path = _cookie_cache_path()
        if not os.path.exists(cache_path):
            return None
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)
        created_at = float(cache.get("created_at", 0))
        if time.time() - created_at > _COOKIE_CACHE_TTL:
            return None
        auth_token = cache.get("auth_token")
        ct0 = cache.get("ct0")
        if not auth_token or not ct0:
            return None
        cookies = {"auth_token": auth_token, "ct0": ct0}
        cookie_string = cache.get("cookie_string")
        if cookie_string:
            cookies["cookie_string"] = cookie_string
        logger.info("Loaded Twitter cookies from cache")
        return cookies
    except Exception as exc:
        logger.debug("Failed to load cookie cache: %s", exc)
        return None


def _save_cookie_cache(cookies: Dict[str, str]) -> None:
    """Save browser-derived cookies to the short-lived cache."""
    if _COOKIE_CACHE_TTL <= 0:
        return
    try:
        cache_path = _cookie_cache_path()
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        payload = {
            "auth_token": cookies["auth_token"],
            "ct0": cookies["ct0"],
            "created_at": time.time(),
        }
        if cookies.get("cookie_string"):
            payload["cookie_string"] = cookies["cookie_string"]
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception as exc:
        logger.debug("Failed to save cookie cache: %s", exc)


def _extract_cookies_from_jar(jar: Any, source: str = "unknown") -> Optional[Dict[str, str]]:
    """Extract Twitter cookies from a cookie jar."""
    result: Dict[str, str] = {}
    all_cookies: Dict[str, str] = {}
    twitter_cookie_count = 0
    for cookie in jar:
        domain = cookie.domain or ""
        if _is_twitter_domain(domain):
            twitter_cookie_count += 1
            if cookie.name == "auth_token":
                result["auth_token"] = cookie.value
            elif cookie.name == "ct0":
                result["ct0"] = cookie.value
            if cookie.name and cookie.value:
                all_cookies[cookie.name] = cookie.value
    if "auth_token" in result and "ct0" in result:
        cookies = {"auth_token": result["auth_token"], "ct0": result["ct0"]}
        if all_cookies:
            cookies["cookie_string"] = "; ".join("%s=%s" % (k, v) for k, v in all_cookies.items())
            logger.info("Extracted %d total cookies for full browser fingerprint", len(all_cookies))
        return cookies
    logger.debug(
        "Cookie jar %s did not contain usable Twitter auth cookies (twitter_cookies=%d, auth_token=%s, ct0=%s)",
        source,
        twitter_cookie_count,
        "auth_token" in result,
        "ct0" in result,
    )
    return None


# Base directories for Chromium-based browsers, keyed by browser name.
# Each entry maps to the directory under the platform-specific app data root.
_CHROMIUM_BASE_DIRS: Dict[str, str] = {
    "chrome": os.path.join("Google", "Chrome"),
    "arc": os.path.join("Arc", "User Data"),
    "dia": os.path.join("Dia", "User Data"),
    "edge": "Microsoft Edge",
    "brave": os.path.join("BraveSoftware", "Brave-Browser"),
    "chromium": "Chromium",
}

_CUSTOM_CHROMIUM_BROWSER = "custom-chromium"
_DISCOVERED_ONLY_CHROMIUM_BROWSERS = {"dia", _CUSTOM_CHROMIUM_BROWSER}

# Default browser order for cookie extraction
_DEFAULT_BROWSER_ORDER = ["arc", "dia", "chrome", "edge", "firefox", "brave", "chromium"]
_SUPPORTED_BROWSERS = set(_DEFAULT_BROWSER_ORDER) | {_CUSTOM_CHROMIUM_BROWSER}


def _custom_chromium_user_data_dir() -> Optional[str]:
    """Return custom Chromium user data/profile directory from env, if set."""
    root = os.environ.get("TWITTER_CHROMIUM_USER_DATA_DIR", "").strip()
    if not root:
        return None
    return os.path.abspath(os.path.expanduser(root))


def _get_browser_order() -> List[str]:
    """Return browser extraction order, respecting TWITTER_BROWSER env var."""
    default_order = list(_DEFAULT_BROWSER_ORDER)
    if _custom_chromium_user_data_dir():
        default_order.insert(0, _CUSTOM_CHROMIUM_BROWSER)

    env = os.environ.get("TWITTER_BROWSER", "").strip().lower()
    if not env:
        return default_order
    if env not in _SUPPORTED_BROWSERS:
        logger.warning("TWITTER_BROWSER='%s' is invalid, using default order", env)
        return default_order
    return [env] + [b for b in default_order if b != env]


def _chromium_root_for_browser(browser_name: str) -> Optional[str]:
    """Return the profile root directory for a Chromium-based browser."""
    if browser_name == _CUSTOM_CHROMIUM_BROWSER:
        return _custom_chromium_user_data_dir()

    base_dir = _CHROMIUM_BASE_DIRS.get(browser_name)
    if base_dir is None:
        return None

    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
    if sys.platform == "win32":
        if browser_name == "edge":
            return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), base_dir)
    if browser_name == "edge":
        return os.path.join(os.path.expanduser("~"), ".config", "microsoft-edge")
    return os.path.join(os.path.expanduser("~"), ".config", base_dir)


def _profile_cookie_paths(profile_dir: str) -> List[str]:
    """Return possible Chromium cookie database paths for one profile."""
    paths = []
    for rel_path in ("Cookies", os.path.join("Network", "Cookies")):
        cookie_path = os.path.join(profile_dir, rel_path)
        if os.path.exists(cookie_path):
            paths.append(cookie_path)
    return paths


def _profile_name_from_cookie_file(cookie_file: str) -> str:
    """Return profile name for a Chromium cookie database path."""
    parent = os.path.basename(os.path.dirname(cookie_file))
    if parent == "Network":
        return os.path.basename(os.path.dirname(os.path.dirname(cookie_file)))
    return parent


def _chromium_key_file_for_cookie(browser_name: str, cookie_file: str) -> Optional[str]:
    """Return Chromium Local State path associated with a cookie database."""
    root = _chromium_root_for_browser(browser_name)
    candidates: List[str] = []
    if root:
        candidates.append(os.path.join(root, "Local State"))

    profile_dir = os.path.dirname(cookie_file)
    if os.path.basename(profile_dir) == "Network":
        profile_dir = os.path.dirname(profile_dir)
    candidates.append(os.path.join(os.path.dirname(profile_dir), "Local State"))

    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate):
            return candidate
    return None


def _iter_chrome_cookie_files(browser_name: str) -> List[str]:
    """Return cookie file paths for all Chromium profiles.

    If TWITTER_CHROME_PROFILE is set, only that profile is returned.
    Otherwise yields Default first, then Profile 1, Profile 2, ... sorted.
    """
    root = _chromium_root_for_browser(browser_name)
    if not root or not os.path.isdir(root):
        return []

    # If user explicitly specifies a profile, only use that one
    env_profile = os.environ.get("TWITTER_CHROME_PROFILE", "").strip()
    if env_profile:
        profile_dir = env_profile if os.path.isabs(env_profile) else os.path.join(root, env_profile)
        profile_paths = _profile_cookie_paths(profile_dir)
        if profile_paths:
            logger.debug("Using specified Chromium profile: %s", env_profile)
            return profile_paths
        logger.warning("TWITTER_CHROME_PROFILE='%s' not found under %s", env_profile, root)
        return []

    # Auto-discover: Default first, then Profile N sorted
    paths: List[str] = []
    seen = set()

    def append_profile(profile_dir: str) -> None:
        for cookie_path in _profile_cookie_paths(profile_dir):
            if cookie_path not in seen:
                seen.add(cookie_path)
                paths.append(cookie_path)

    # Accept either a Chromium User Data directory or a direct profile directory.
    append_profile(root)
    append_profile(os.path.join(root, "Default"))

    profile_dirs = sorted(glob.glob(os.path.join(root, "Profile *")))
    for profile_dir in profile_dirs:
        append_profile(profile_dir)

    return paths


def _load_custom_chromium_cookie_jar(
    browser_cookie3: Any,
    browser_name: str,
    cookie_file: Optional[str] = None,
) -> Any:
    """Load a Chromium cookie jar for browsers not built into browser-cookie3."""
    if not hasattr(browser_cookie3, "ChromiumBased"):
        raise RuntimeError("browser-cookie3 does not expose ChromiumBased")

    if browser_name == "dia":
        display_name = "Dia"
        osx_key_service = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_SERVICE", "").strip() or "Dia Safe Storage"
        osx_key_user = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_USER", "").strip() or "Dia"
        os_crypt_name = os.environ.get("TWITTER_CHROMIUM_OS_CRYPT_NAME", "").strip() or "chrome"
    else:
        display_name = "Custom Chromium"
        osx_key_service = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_SERVICE", "").strip() or "Chrome Safe Storage"
        osx_key_user = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_USER", "").strip() or "Chrome"
        os_crypt_name = os.environ.get("TWITTER_CHROMIUM_OS_CRYPT_NAME", "").strip() or "chrome"

    key_file = _chromium_key_file_for_cookie(browser_name, cookie_file) if cookie_file else None
    windows_keys = [key_file] if key_file else []
    loader = browser_cookie3.ChromiumBased(
        browser=display_name,
        cookie_file=cookie_file,
        domain_name="",
        key_file=key_file,
        linux_cookies=[],
        windows_cookies=[],
        osx_cookies=[],
        windows_keys=windows_keys,
        os_crypt_name=os_crypt_name,
        osx_key_service=osx_key_service,
        osx_key_user=osx_key_user,
    )
    return loader.load()


def _get_browser_cookie_fn(browser_cookie3: Any, browser_name: str) -> Any:
    """Return a browser_cookie3 loader for a browser name."""
    if browser_name in {"dia", _CUSTOM_CHROMIUM_BROWSER}:
        return lambda cookie_file=None: _load_custom_chromium_cookie_jar(
            browser_cookie3,
            browser_name,
            cookie_file=cookie_file,
        )
    return getattr(browser_cookie3, browser_name)


def _extract_in_process() -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Extract cookies in the main process (required on macOS for Keychain access).

    On macOS, Chrome encrypts cookies using a key stored in the system Keychain.
    Child processes do NOT inherit the parent's Keychain authorization, so
    browser_cookie3 must run in the main process to decrypt cookies.

    For Chromium-based browsers, iterates all profiles to find Twitter cookies.

    Returns (cookies_dict | None, diagnostics_list).
    """
    try:
        import browser_cookie3
    except ImportError:
        logger.debug("browser_cookie3 not installed, skipping in-process extraction")
        return None, ["browser-cookie3 not installed"]

    attempts: List[str] = []
    diagnostics: List[str] = []

    for name in _get_browser_order():
        try:
            fn = _get_browser_cookie_fn(browser_cookie3, name)
        except AttributeError as e:
            logger.debug("%s browser_cookie3 loader missing: %s", name, e)
            attempts.append("%s=missing-loader" % name)
            diagnostics.append("%s: %s" % (name, e))
            continue

        if name in _CHROMIUM_BASE_DIRS or name == _CUSTOM_CHROMIUM_BROWSER:
            # Chromium-based: iterate all profiles
            cookie_files = _iter_chrome_cookie_files(name)
            if not cookie_files:
                if name in _DISCOVERED_ONLY_CHROMIUM_BROWSERS:
                    attempts.append("%s=not-found" % name)
                    continue
                # No profile dirs found — try the default (no cookie_file arg)
                try:
                    jar = fn()
                except Exception as e:
                    logger.debug("%s in-process extraction failed: %s", name, e)
                    attempts.append("%s=%s" % (name, type(e).__name__))
                    diagnostics.append("%s: %s" % (name, e))
                    continue
                cookies = _extract_cookies_from_jar(jar, source="%s(in-process)" % name)
                if cookies:
                    logger.info("Found cookies in %s (in-process, default)", name)
                    return cookies, diagnostics
                attempts.append("%s=no-cookies" % name)
                continue

            for cookie_file in cookie_files:
                profile_name = _profile_name_from_cookie_file(cookie_file)
                try:
                    jar = fn(cookie_file=cookie_file)
                except Exception as e:
                    logger.debug("%s[%s] in-process extraction failed: %s", name, profile_name, e)
                    attempts.append("%s[%s]=%s" % (name, profile_name, type(e).__name__))
                    diagnostics.append("%s[%s]: %s" % (name, profile_name, e))
                    continue
                cookies = _extract_cookies_from_jar(jar, source="%s[%s](in-process)" % (name, profile_name))
                if cookies:
                    logger.info("Found cookies in %s profile '%s' (in-process)", name, profile_name)
                    return cookies, diagnostics
                attempts.append("%s[%s]=no-cookies" % (name, profile_name))
        else:
            # Non-Chromium (Firefox): use default behavior
            try:
                jar = fn()
            except Exception as e:
                logger.debug("%s in-process extraction failed: %s", name, e)
                attempts.append("%s=%s" % (name, type(e).__name__))
                diagnostics.append("%s: %s" % (name, e))
                continue
            cookies = _extract_cookies_from_jar(jar, source="%s(in-process)" % name)
            if cookies:
                logger.info("Found cookies in %s (in-process)", name)
                return cookies, diagnostics
            attempts.append("%s=no-cookies" % name)

    if attempts:
        logger.debug("In-process extraction attempts: %s", ", ".join(attempts))
    return None, diagnostics


def _extract_via_subprocess() -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Extract cookies via subprocess (fallback if in-process fails, e.g. SQLite lock).

    Returns (cookies_dict | None, diagnostics_list).
    """
    extract_script = '''
import glob, json, os, sys
try:
    import browser_cookie3
except ImportError:
    print(json.dumps({"error": "browser-cookie3 not installed"}))
    sys.exit(1)

CHROMIUM_BASE_DIRS = {
    "chrome": os.path.join("Google", "Chrome"),
    "arc": os.path.join("Arc", "User Data"),
    "dia": os.path.join("Dia", "User Data"),
    "edge": os.path.join("Microsoft Edge"),
    "brave": os.path.join("BraveSoftware", "Brave-Browser"),
    "chromium": "Chromium",
}
CUSTOM_CHROMIUM_BROWSER = "custom-chromium"
DISCOVERED_ONLY_CHROMIUM_BROWSERS = {"dia", CUSTOM_CHROMIUM_BROWSER}
DEFAULT_ORDER = ["arc", "dia", "chrome", "edge", "firefox", "brave", "chromium"]
SUPPORTED_BROWSERS = set(DEFAULT_ORDER) | {CUSTOM_CHROMIUM_BROWSER}

def custom_chromium_user_data_dir():
    root = os.environ.get("TWITTER_CHROMIUM_USER_DATA_DIR", "").strip()
    if not root:
        return None
    return os.path.abspath(os.path.expanduser(root))

def chromium_root_for_browser(browser_name):
    if browser_name == CUSTOM_CHROMIUM_BROWSER:
        return custom_chromium_user_data_dir()
    base_dir = CHROMIUM_BASE_DIRS.get(browser_name)
    if base_dir is None:
        return None
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", base_dir)
    if sys.platform == "win32":
        if browser_name == "edge":
            return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Edge", "User Data")
        return os.path.join(os.environ.get("LOCALAPPDATA", ""), base_dir)
    if browser_name == "edge":
        return os.path.join(os.path.expanduser("~"), ".config", "microsoft-edge")
    return os.path.join(os.path.expanduser("~"), ".config", base_dir)

def profile_cookie_paths(profile_dir):
    paths = []
    for rel_path in ("Cookies", os.path.join("Network", "Cookies")):
        cookie_path = os.path.join(profile_dir, rel_path)
        if os.path.exists(cookie_path):
            paths.append(cookie_path)
    return paths

def profile_name_from_cookie_file(cookie_file):
    parent = os.path.basename(os.path.dirname(cookie_file))
    if parent == "Network":
        return os.path.basename(os.path.dirname(os.path.dirname(cookie_file)))
    return parent

def chromium_key_file_for_cookie(browser_name, cookie_file):
    root = chromium_root_for_browser(browser_name)
    candidates = []
    if root:
        candidates.append(os.path.join(root, "Local State"))
    profile_dir = os.path.dirname(cookie_file)
    if os.path.basename(profile_dir) == "Network":
        profile_dir = os.path.dirname(profile_dir)
    candidates.append(os.path.join(os.path.dirname(profile_dir), "Local State"))
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if os.path.exists(candidate):
            return candidate
    return None

def iter_cookie_files(browser_name):
    root = chromium_root_for_browser(browser_name)
    if not root or not os.path.isdir(root):
        return []
    env_profile = os.environ.get("TWITTER_CHROME_PROFILE", "").strip()
    if env_profile:
        profile_dir = env_profile if os.path.isabs(env_profile) else os.path.join(root, env_profile)
        return profile_cookie_paths(profile_dir)
    paths = []
    seen = set()
    def append_profile(profile_dir):
        for cookie_path in profile_cookie_paths(profile_dir):
            if cookie_path not in seen:
                seen.add(cookie_path)
                paths.append(cookie_path)
    append_profile(root)
    append_profile(os.path.join(root, "Default"))
    for pd in sorted(glob.glob(os.path.join(root, "Profile *"))):
        append_profile(pd)
    return paths

def load_custom_chromium_cookie_jar(browser_name, cookie_file=None):
    if not hasattr(browser_cookie3, "ChromiumBased"):
        raise RuntimeError("browser-cookie3 does not expose ChromiumBased")
    if browser_name == "dia":
        display_name = "Dia"
        osx_key_service = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_SERVICE", "").strip() or "Dia Safe Storage"
        osx_key_user = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_USER", "").strip() or "Dia"
        os_crypt_name = os.environ.get("TWITTER_CHROMIUM_OS_CRYPT_NAME", "").strip() or "chrome"
    else:
        display_name = "Custom Chromium"
        osx_key_service = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_SERVICE", "").strip() or "Chrome Safe Storage"
        osx_key_user = os.environ.get("TWITTER_CHROMIUM_KEYCHAIN_USER", "").strip() or "Chrome"
        os_crypt_name = os.environ.get("TWITTER_CHROMIUM_OS_CRYPT_NAME", "").strip() or "chrome"
    key_file = chromium_key_file_for_cookie(browser_name, cookie_file) if cookie_file else None
    windows_keys = [key_file] if key_file else []
    loader = browser_cookie3.ChromiumBased(
        browser=display_name,
        cookie_file=cookie_file,
        domain_name="",
        key_file=key_file,
        linux_cookies=[],
        windows_cookies=[],
        osx_cookies=[],
        windows_keys=windows_keys,
        os_crypt_name=os_crypt_name,
        osx_key_service=osx_key_service,
        osx_key_user=osx_key_user,
    )
    return loader.load()

def get_browser_cookie_fn(browser_name):
    if browser_name in {"dia", CUSTOM_CHROMIUM_BROWSER}:
        return lambda cookie_file=None: load_custom_chromium_cookie_jar(browser_name, cookie_file=cookie_file)
    return getattr(browser_cookie3, browser_name)

def extract_from_jar(jar, name, profile=""):
    result = {}
    all_cookies = {}
    for cookie in jar:
        domain = cookie.domain or ""
        if domain.endswith(".x.com") or domain.endswith(".twitter.com") or domain in ("x.com", "twitter.com", ".x.com", ".twitter.com"):
            if cookie.name == "auth_token":
                result["auth_token"] = cookie.value
            elif cookie.name == "ct0":
                result["ct0"] = cookie.value
            if cookie.name and cookie.value:
                all_cookies[cookie.name] = cookie.value
    if "auth_token" in result and "ct0" in result:
        result["browser"] = name
        if profile:
            result["profile"] = profile
        result["all_cookies"] = all_cookies
        return result
    return None

browser_order = list(DEFAULT_ORDER)
if custom_chromium_user_data_dir():
    browser_order.insert(0, CUSTOM_CHROMIUM_BROWSER)
env_browser = os.environ.get("TWITTER_BROWSER", "").strip().lower()
if env_browser in SUPPORTED_BROWSERS:
    browser_order = [env_browser] + [b for b in browser_order if b != env_browser]
attempts = []

for name in browser_order:
    try:
        fn = get_browser_cookie_fn(name)
    except AttributeError as exc:
        attempts.append(f"{name}=missing-loader: {exc}")
        continue
    if name in CHROMIUM_BASE_DIRS or name == CUSTOM_CHROMIUM_BROWSER:
        cookie_files = iter_cookie_files(name)
        if not cookie_files:
            if name in DISCOVERED_ONLY_CHROMIUM_BROWSERS:
                attempts.append(f"{name}=not-found")
                continue
            try:
                jar = fn()
            except Exception as exc:
                attempts.append(f"{name}={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}=no-cookies")
            continue
        for cf in cookie_files:
            pname = profile_name_from_cookie_file(cf)
            try:
                jar = fn(cookie_file=cf)
            except Exception as exc:
                attempts.append(f"{name}[{pname}]={type(exc).__name__}: {exc}")
                continue
            r = extract_from_jar(jar, name, pname)
            if r:
                print(json.dumps(r))
                sys.exit(0)
            attempts.append(f"{name}[{pname}]=no-cookies")
    else:
        try:
            jar = fn()
        except Exception as exc:
            attempts.append(f"{name}={type(exc).__name__}: {exc}")
            continue
        r = extract_from_jar(jar, name)
        if r:
            print(json.dumps(r))
            sys.exit(0)
        attempts.append(f"{name}=no-cookies")

print(json.dumps({
    "error": "No Twitter cookies found in any browser. Make sure you are logged into x.com.",
    "attempts": attempts,
}))
sys.exit(1)
'''

    diagnostics: List[str] = []

    def _run_extract_command(
        cmd: list[str],
        timeout: int,
        label: str,
    ) -> Tuple[Optional[Dict[str, Any]], bool]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.debug("Cookie extraction %s timed out", label)
            return None, False
        except FileNotFoundError as exc:
            logger.debug("Cookie extraction %s launcher missing: %s", label, exc)
            return None, False

        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if stderr:
            logger.debug("Cookie extraction stderr from %s: %s", label, stderr[:300])
        if not output:
            logger.debug("Cookie extraction from %s produced no stdout", label)
            return None, True

        try:
            data = json.loads(output)
        except json.JSONDecodeError as exc:
            logger.debug("Cookie extraction %s returned invalid JSON: %s", label, exc)
            return None, True

        if "error" in data:
            attempts = data.get("attempts") or []
            if attempts:
                logger.debug("Subprocess extraction attempts (%s): %s", label, ", ".join(str(item) for item in attempts))
                diagnostics.extend(str(item) for item in attempts)
            retryable = data.get("error") == "browser-cookie3 not installed"
            return None, retryable

        return data, False

    try:
        data, retry_with_uv = _run_extract_command(
            [sys.executable, "-c", extract_script],
            timeout=15,
            label="current env",
        )
        if data is None and retry_with_uv:
            data, _ = _run_extract_command(
                ["uv", "run", "--with", "browser-cookie3", "python", "-c", extract_script],
                timeout=30,
                label="uv fallback",
            )

        if data is None:
            return None, diagnostics
        logger.info("Found cookies in %s (subprocess)", data.get("browser", "unknown"))

        # Build full cookie string from all extracted cookies
        cookies: Dict[str, str] = {"auth_token": data["auth_token"], "ct0": data["ct0"]}
        all_cookies = data.get("all_cookies", {})
        if all_cookies:
            cookie_str = "; ".join("%s=%s" % (k, v) for k, v in all_cookies.items())
            cookies["cookie_string"] = cookie_str
            logger.info("Extracted %d total cookies for full browser fingerprint", len(all_cookies))
        return cookies, diagnostics
    except KeyError as exc:
        logger.debug("Cookie extraction subprocess returned incomplete payload: %s", exc)
        return None, diagnostics


def extract_from_browser() -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Auto-extract ALL Twitter cookies from local browser using browser-cookie3.

    Strategy:
    1. Try in-process first (required on macOS for Keychain access)
    2. Fall back to subprocess (handles SQLite lock when browser is running)

    Returns (cookies_dict | None, diagnostics_list).
    """
    all_diagnostics: List[str] = []

    # 1. In-process (works on macOS, may fail with SQLite lock)
    cookies, diag = _extract_in_process()
    all_diagnostics.extend(diag)
    if cookies:
        return cookies, all_diagnostics

    # 2. Subprocess fallback (handles SQLite lock, but fails on macOS Keychain)
    logger.debug("In-process extraction failed, trying subprocess fallback")
    cookies, diag = _extract_via_subprocess()
    all_diagnostics.extend(diag)
    if not cookies:
        logger.warning("Twitter cookie extraction failed in both in-process and subprocess modes")
    return cookies, all_diagnostics


def get_cookies() -> Dict[str, str]:
    """Get Twitter cookies. Priority: env vars -> browser extraction.

    Raises RuntimeError if no cookies found.
    """
    cookies: Optional[Dict[str, str]] = None
    diagnostics: List[str] = []
    from_env = False
    from_cache = False

    # 1. Try environment variables
    cookies = load_from_env()
    if cookies:
        from_env = True
        logger.info("Loaded cookies from environment variables")

    # 2. Reuse the short-lived browser cache when available.
    if not cookies:
        cookies = _load_cookie_cache()
        from_cache = cookies is not None

    # 3. Try browser extraction (auto-detect)
    if not cookies:
        logger.debug("Attempting browser cookie extraction")
        cookies, diagnostics = extract_from_browser()

    if not cookies:
        lines = ["No Twitter cookies found."]
        # Add actionable Keychain hint when relevant
        hint = _diagnose_keychain_issues(diagnostics)
        if hint:
            lines.append("")
            lines.append("Likely cause:")
            lines.extend("  " + line for line in hint.splitlines())
            lines.append("")
        lines.append("Option 1: Set TWITTER_AUTH_TOKEN and TWITTER_CT0 environment variables")
        lines.append(
            "Option 2: Make sure you are logged into x.com in your browser "
            "(Arc/Dia/Chrome/Edge/Firefox/Brave/Chromium)"
        )
        lines.append(
            "Option 3: For other Chromium-based browsers, set TWITTER_CHROMIUM_USER_DATA_DIR "
            "to the browser's User Data directory"
        )
        lines.append("")
        lines.append("Run 'twitter -v <command>' for debug diagnostics.")
        raise AuthenticationError("\n".join(lines))

    if from_cache:
        return cookies

    # Verify only for explicit auth failures; transient endpoint issues are tolerated.
    try:
        verify_cookies(cookies["auth_token"], cookies["ct0"], cookies.get("cookie_string"))
    except RuntimeError:
        # Auth failure — re-extract from browser and retry verification
        logger.info("Cookie verification failed, re-extracting from browser")
        fresh_cookies, _ = extract_from_browser()
        if fresh_cookies:
            # Verify fresh cookies — if this also fails, let it raise
            verify_cookies(fresh_cookies["auth_token"], fresh_cookies["ct0"], fresh_cookies.get("cookie_string"))
            _save_cookie_cache(fresh_cookies)
            return fresh_cookies
        raise
    if not from_env:
        _save_cookie_cache(cookies)
    return cookies
