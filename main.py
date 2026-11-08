import argparse
import base64
import json
import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from os import getenv
from pathlib import Path
from time import sleep, time
from urllib.parse import urlparse

from dotenv import load_dotenv
from patchright.sync_api import BrowserContext, Page, Playwright, sync_playwright
from xvfbwrapper import Xvfb

load_dotenv()

APP_URL = "https://app.gomining.com"
LOGIN_URL = f"{APP_URL}/login"
NFT_MINERS_URL = f"{APP_URL}/nft-miners"
NFT_MINERS_SELECTOR = "nft-miners"
SERVICE_BUTTON_SELECTOR = "service-button > button"
SERVICE_TIMER_SELECTOR = "service-button timer"

PROFILE_DIR = Path(getenv("PROFILE_DIR", "./profile"))
SCREENSHOT_DIR = Path(getenv("SCREENSHOT_DIR", "./screenshot"))
LOG_DIR = Path(getenv("LOG_DIR", "./logs"))
AUTH_LOG = LOG_DIR / "auth.log"
AUTH_LOG_MAX_BYTES = int(getenv("AUTH_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
AUTH_LOG_BACKUPS = int(getenv("AUTH_LOG_BACKUPS", "3"))
RUNTIME_FILE = Path(getenv("RUNTIME_FILE", "./state/runtime.json"))

MAX_SLEEP_CHUNK_SECONDS = int(getenv("MAX_SLEEP_CHUNK_SECONDS", str(24 * 3600)))
BROWSER_KEEPALIVE_MAX_SECONDS = int(
    getenv("BROWSER_KEEPALIVE_MAX_SECONDS", str(10 * 60))
)
ERROR_RETRY_SECONDS = int(getenv("ERROR_RETRY_SECONDS", "300"))
SERVICE_RETRY_SECONDS = int(getenv("SERVICE_RETRY_SECONDS", "300"))

EMAIL_SELECTORS = (
    "#email-or-phone,"
    'input[name="login_email_or_phone"],'
    'input[autocomplete="home email"],'
    'input[id="email"]'
)
PASSWORD_SELECTORS = '#password, input[autocomplete="current-password"]'
SUBMIT_SELECTOR = 'form button[type="submit"]'

TOKEN_COOKIES = ("access_token", "refresh_token")

TRANSIENT_NETWORK_ERRORS = (
    "ERR_NETWORK_CHANGED",
    "ERR_INTERNET_DISCONNECTED",
    "ERR_NAME_NOT_RESOLVED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_CONNECTION_REFUSED",
    "ERR_TIMED_OUT",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_NETWORK_ACCESS_DENIED",
)
GOTO_MAX_ATTEMPTS = int(getenv("GOTO_MAX_ATTEMPTS", "4"))

AUTH_ENDPOINTS = (
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/isAuth-v2",
    "/api/auth/logout",
)
AUTH_RESPONSES: deque = deque()
_auth_logger: logging.Logger | None = None


def get_auth_logger() -> logging.Logger:
    global _auth_logger
    if _auth_logger is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("gm-pot-auth")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = RotatingFileHandler(
            AUTH_LOG,
            maxBytes=AUTH_LOG_MAX_BYTES,
            backupCount=AUTH_LOG_BACKUPS,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        _auth_logger = logger
    return _auth_logger


def auth_log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line)
    try:
        get_auth_logger().info(line)
    except OSError as e:
        print(f"Could not write auth log: {e}")


def jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:
        return None


def token_state(ctx: BrowserContext) -> dict:
    state = {}
    for cookie in ctx.cookies():
        if cookie["name"] == "access_token":
            exp = jwt_exp(cookie["value"])
            if exp is not None:
                state["access_exp_in"] = exp - int(time())
        elif cookie["name"] == "refresh_token":
            state["has_refresh_token"] = bool(cookie["value"])
    return state


def on_auth_response(response) -> None:
    if any(endpoint in response.url for endpoint in AUTH_ENDPOINTS):
        AUTH_RESPONSES.append(response)


def on_frame_navigated(frame) -> None:
    if frame.parent_frame is None:
        auth_log(f"navigate url={frame.url}")


def drain_auth_responses() -> None:
    while AUTH_RESPONSES:
        response = AUTH_RESPONSES.popleft()
        detail = ""
        should_read_body = response.status != 204 and any(
            endpoint in response.url
            for endpoint in (
                "/api/auth/login",
                "/api/auth/refresh",
                "/api/auth/logout",
            )
        )
        if should_read_body:
            try:
                payload = json.loads(response.text())
                data = payload.get("data") if isinstance(payload, dict) else None
                fields = {
                    "statusMessage": payload.get("statusMessage")
                    if isinstance(payload, dict)
                    else None,
                }
                if isinstance(data, dict):
                    fields["statusMessage"] = (
                        fields["statusMessage"] or data.get("statusMessage")
                    )
                    refresh_exp = data.get("refreshExp")
                    fields["refreshExp"] = refresh_exp
                    if isinstance(refresh_exp, (int, float)):
                        fields["refresh_exp_in_days"] = round(
                            (refresh_exp - time()) / 86400, 2
                        )
                    fields["deviceId"] = data.get("deviceId")
                detail = json.dumps({k: v for k, v in fields.items() if v is not None})
            except Exception as e:
                detail = f"body_unavailable={type(e).__name__}"
        auth_log(
            f"auth_response status={response.status} url={response.url} {detail}"
        )


def attach_auth_logging(page: Page) -> None:
    page.on("response", on_auth_response)
    page.on("framenavigated", on_frame_navigated)


def take_screenshot(page: Page, name: str) -> None:
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SCREENSHOT_DIR / name))
    except Exception as e:
        print(f"Screenshot failed: {e}")


def has_tokens(ctx: BrowserContext) -> bool:
    return any(
        cookie["name"] in TOKEN_COOKIES and cookie["value"]
        for cookie in ctx.cookies()
    )


def has_access_token(ctx: BrowserContext) -> bool:
    return any(
        cookie["name"] == "access_token" and cookie["value"]
        for cookie in ctx.cookies()
    )


def is_logged_out(page: Page) -> bool:
    path = urlparse(page.url).path
    return path == "/auth" or path.startswith("/login")


def launch_context(playwright: Playwright) -> BrowserContext:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        ctx = playwright.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            headless=False,
        )
        print(f"Using browser profile at {PROFILE_DIR.resolve()}")
        return ctx
    except Exception as e:
        print(f"Could not open browser profile: {e}")
        raise


def solve_cloudflare(page: Page) -> None:
    try:
        widget = page.wait_for_selector("turnstile-widget", timeout=15000)
        if widget is None:
            return
        pos = widget.bounding_box()
    except Exception as e:
        print(f"Cloudflare turnstile widget not found: {e}")
        return

    for _ in range(10):
        response = page.locator('input[name="cf-turnstile-response"]')
        if response.count() == 0:
            return
        token = response.first.input_value()
        if token:
            print("Cloudflare turnstile solved")
            return
        if pos:
            page.mouse.click(pos["x"] + 36, pos["y"] + 36)
        page.wait_for_timeout(1000)

    print("Cloudflare turnstile not solved, continuing anyway")


def goto(page: Page, url: str, timeout: int = 60000) -> None:
    last_error = None
    for attempt in range(1, GOTO_MAX_ATTEMPTS + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:
            last_error = e
            message = str(e)
            if not any(code in message for code in TRANSIENT_NETWORK_ERRORS):
                raise
            auth_log(
                f"goto_retry attempt={attempt} url={url} "
                f"error={message.splitlines()[0][:200]}"
            )
            if attempt < GOTO_MAX_ATTEMPTS:
                page.wait_for_timeout(2000 * attempt)
    if last_error is not None:
        raise last_error


def login(page: Page, ctx: BrowserContext) -> None:
    email = getenv("EMAIL")
    password = getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError("EMAIL and PASSWORD must be set in .env")

    print("Opening login page...")
    auth_log(f"login_start url={page.url}")
    try:
        goto(page, LOGIN_URL)
        solve_cloudflare(page)

        email_input = page.locator(EMAIL_SELECTORS).first
        email_input.wait_for(state="visible", timeout=60000)
        email_input.fill(email)

        page.locator(PASSWORD_SELECTORS).first.fill(password)
        page.locator(SUBMIT_SELECTOR).first.click()

        deadline = time() + 90
        while time() < deadline:
            drain_auth_responses()
            if has_access_token(ctx) and not is_logged_out(page):
                print("Login succeeded")
                auth_log(f"login_success url={page.url} {token_state(ctx)}")
                return
            page.wait_for_timeout(1000)
    except Exception as e:
        auth_log(f"login_error error={type(e).__name__}: {e}")
        take_screenshot(page, "login_error.png")
        raise

    take_screenshot(page, "login_failed.png")
    auth_log(f"login_failed url={page.url} {token_state(ctx)}")
    raise RuntimeError("Login did not produce a valid session")


def ensure_logged_in(page: Page, ctx: BrowserContext) -> None:
    if has_tokens(ctx):
        print("Trying saved session...")
        auth_log(f"session_check_start url={page.url} {token_state(ctx)}")
        goto(page, NFT_MINERS_URL)
        drain_auth_responses()
        if not is_logged_out(page):
            try:
                page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)
                print("Saved session is valid")
                auth_log(f"session_valid url={page.url} {token_state(ctx)}")
                return
            except Exception:
                print("Saved session did not reach nft-miners")
                auth_log(f"session_not_ready url={page.url} {token_state(ctx)}")
        else:
            print("Saved session was rejected")
            auth_log(f"session_rejected url={page.url} {token_state(ctx)}")

    login(page, ctx)
    goto(page, NFT_MINERS_URL)
    page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)
    drain_auth_responses()


def skip_intro(page: Page) -> None:
    if "/nft-miners" not in page.url:
        goto(page, NFT_MINERS_URL)
    page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)

    for _ in range(50):
        if "/nft-miners" not in page.url:
            goto(page, NFT_MINERS_URL)
            page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)

        try:
            tooltip = page.wait_for_selector(
                "div.tooltip-inner", timeout=5000, state="visible"
            )
        except Exception:
            return

        if tooltip is None:
            return

        btn = tooltip.query_selector("button")
        if btn is None:
            return
        btn.click()
        page.wait_for_timeout(500)


def read_cooldown(page: Page) -> float:
    timer = page.query_selector(SERVICE_TIMER_SELECTOR)
    if timer is None:
        print("Cooldown timer not found")
        return 300

    text = timer.inner_text().strip()
    print(f"Cooldown timer: {text}")

    try:
        parts = [int(part) for part in text.split(":")]
    except ValueError:
        return 300

    if len(parts) == 3:
        hours, minutes, seconds = parts
    elif len(parts) == 2:
        hours, minutes, seconds = 0, parts[0], parts[1]
    elif len(parts) == 1:
        hours, minutes, seconds = 0, 0, parts[0]
    else:
        return 300

    remaining = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    print(f"Service is on cooldown. Time remaining: {remaining.total_seconds()} seconds")
    return remaining.total_seconds()


def try_service(page: Page, allow_click: bool = True) -> float:
    for _ in range(3):
        goto(page, NFT_MINERS_URL)
        page.wait_for_selector(SERVICE_BUTTON_SELECTOR, timeout=60000)
        page.wait_for_timeout(1000)
        drain_auth_responses()

        btn = page.locator(SERVICE_BUTTON_SELECTOR).first
        disabled = btn.get_attribute("disabled") is not None or btn.is_disabled()
        print(f"Service button disabled: {disabled}")

        if disabled:
            return read_cooldown(page)

        if not allow_click:
            print("Service is available. Skipping click (dry run).")
            return 0

        take_screenshot(page, "before_service.png")
        btn.click()
        page.wait_for_timeout(2000)
        take_screenshot(page, "after_service.png")

    print("Service button did not become disabled after clicking")
    return 300


def load_next_run() -> float | None:
    try:
        data = json.loads(RUNTIME_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = data.get("next_run_at") if isinstance(data, dict) else None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def save_next_run(next_run_at: float, cooldown: float) -> None:
    RUNTIME_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = RUNTIME_FILE.with_name(f"{RUNTIME_FILE.name}.tmp")
    tmp.write_text(
        json.dumps(
            {
                "next_run_at": next_run_at,
                "cooldown_seconds": cooldown,
                "updated_at": time(),
            }
        ),
        encoding="utf-8",
    )
    tmp.replace(RUNTIME_FILE)


def clear_next_run() -> None:
    try:
        RUNTIME_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"Could not remove runtime file: {e}")


def sleep_until(timestamp: float) -> None:
    while True:
        remaining = timestamp - time()
        if remaining <= 0:
            return
        sleep(min(remaining, MAX_SLEEP_CHUNK_SECONDS))


class BrowserSession:
    def __init__(self) -> None:
        self.xvfb: Xvfb | None = None
        self.playwright: Playwright | None = None
        self.ctx: BrowserContext | None = None
        self.page: Page | None = None

    def open(self) -> None:
        self.xvfb = Xvfb()
        self.xvfb.start()
        self.playwright = sync_playwright().start()
        self.ctx = launch_context(self.playwright)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        attach_auth_logging(self.page)
        auth_log("session_start")

    def close(self) -> None:
        if self.ctx is not None:
            try:
                drain_auth_responses()
                auth_log(f"session_end {token_state(self.ctx)}")
                self.ctx.close()
            except Exception as e:
                print(f"Could not close browser session: {e}")
            self.ctx = None
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
            self.playwright = None
        if self.xvfb is not None:
            try:
                self.xvfb.stop()
            except Exception:
                pass
            self.xvfb = None
        self.page = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GoMining service bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="login and verify the session, but never click the service button",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="ignore the saved next run time and work immediately",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    auth_log("process_start")

    if args.force:
        clear_next_run()

    if not args.dry_run and not args.force:
        next_run_at = load_next_run()
        if next_run_at is not None and next_run_at > time():
            wait = next_run_at - time()
            auth_log(
                f"resume_wait seconds={int(wait)} next_run_at={int(next_run_at)}"
            )
            sleep_until(next_run_at)
            auth_log("resume_wait_end")

    session: BrowserSession | None = None
    try:
        while True:
            fresh_session = session is None
            wait = ERROR_RETRY_SECONDS

            try:
                if fresh_session:
                    session = BrowserSession()
                    session.open()

                ctx = session.ctx
                page = session.page
                if ctx is None or page is None:
                    raise RuntimeError("Browser session is not initialized")

                if fresh_session:
                    ensure_logged_in(page, ctx)

                    if args.dry_run:
                        print("Dry run: service button will not be clicked")
                        remaining = try_service(page, allow_click=False)
                        drain_auth_responses()
                        if remaining > 0:
                            print(f"Service cooldown: {remaining:.0f} seconds")
                        else:
                            print("Service is available")
                        take_screenshot(page, "dry-run.png")
                        print(f"Browser profile: {PROFILE_DIR.resolve()}")
                        return

                    skip_intro(page)

                wait = try_service(page)
                if wait <= 0:
                    wait = SERVICE_RETRY_SECONDS
            except Exception as e:
                auth_log(f"cycle_error error={type(e).__name__}: {e}")
                if session is not None:
                    session.close()
                    session = None
                if args.dry_run:
                    raise
                wait = ERROR_RETRY_SECONDS

            next_run_at = time() + wait
            save_next_run(next_run_at, wait)

            if session is not None and wait <= BROWSER_KEEPALIVE_MAX_SECONDS:
                auth_log(
                    f"browser_wait seconds={int(wait)} next_run_at={int(next_run_at)}"
                )
                sleep_until(next_run_at)
                auth_log("browser_wait_end")
                continue

            if session is not None:
                session.close()
                session = None

            auth_log(
                f"sleep_start seconds={int(wait)} next_run_at={int(next_run_at)}"
            )
            sleep_until(next_run_at)
            auth_log("sleep_end")
    except KeyboardInterrupt:
        auth_log("process_stopped")
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    main()
