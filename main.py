import argparse
import base64
import json
from collections import deque
from datetime import datetime, timedelta, timezone
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

EMAIL_SELECTORS = (
    "#email-or-phone,"
    'input[name="login_email_or_phone"],'
    'input[autocomplete="home email"],'
    'input[id="email"]'
)
PASSWORD_SELECTORS = '#password, input[autocomplete="current-password"]'
SUBMIT_SELECTOR = 'form button[type="submit"]'

TOKEN_COOKIES = ("access_token", "refresh_token")
KEEPALIVE_INTERVAL_SECONDS = 10
VISIBILITY_CHECK_INTERVAL_SECONDS = 60
REFRESH_NAVIGATION_INTERVAL_SECONDS = 900

AUTH_ENDPOINTS = (
    "/api/auth/login",
    "/api/auth/refresh",
    "/api/auth/isAuth-v2",
    "/api/auth/logout",
)
AUTH_RESPONSES: deque = deque()


def auth_log(message: str) -> None:
    line = f"{datetime.now(timezone.utc).isoformat()} {message}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with AUTH_LOG.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
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


def token_signature(ctx: BrowserContext) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (cookie["name"], cookie["value"])
            for cookie in ctx.cookies()
            if cookie["name"] in TOKEN_COOKIES
        )
    )


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


def login(page: Page, ctx: BrowserContext) -> None:
    email = getenv("EMAIL")
    password = getenv("PASSWORD")
    if not email or not password:
        raise RuntimeError("EMAIL and PASSWORD must be set in .env")

    print("Opening login page...")
    auth_log(f"login_start url={page.url}")
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
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
        page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
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
    page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)
    drain_auth_responses()


def skip_intro(page: Page) -> None:
    page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)

    for _ in range(50):
        if "/nft-miners" not in page.url:
            page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
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
        page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
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


def keep_alive(page: Page, ctx: BrowserContext, seconds: float) -> None:
    deadline = time() + max(0.0, seconds)
    last_signature = token_signature(ctx)
    last_visibility_check = 0.0
    last_navigation = time()

    while time() < deadline:
        sleep(min(KEEPALIVE_INTERVAL_SECONDS, max(0.5, deadline - time())))
        drain_auth_responses()

        now = time()
        if now - last_visibility_check >= VISIBILITY_CHECK_INTERVAL_SECONDS:
            last_visibility_check = now
            try:
                if page.evaluate("document.visibilityState") != "visible":
                    page.bring_to_front()
            except Exception:
                pass

        if now - last_navigation >= REFRESH_NAVIGATION_INTERVAL_SECONDS:
            last_navigation = now
            try:
                auth_log("refresh_navigation_start")
                page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_selector(NFT_MINERS_SELECTOR, timeout=60000)
                drain_auth_responses()
            except Exception as e:
                print(f"Refresh navigation failed: {e}")
                auth_log(f"refresh_navigation_error error={type(e).__name__}: {e}")

        try:
            signature = token_signature(ctx)
        except Exception as e:
            print(f"Could not read cookies: {e}")
            continue

        if signature != last_signature:
            last_signature = signature
            print("Token refreshed")
            auth_log(f"token_refreshed {token_state(ctx)}")

        if is_logged_out(page):
            print("Session lost while waiting, logging in again")
            auth_log(f"session_lost url={page.url} {token_state(ctx)}")
            ensure_logged_in(page, ctx)
            page.goto(NFT_MINERS_URL, wait_until="domcontentloaded", timeout=60000)
            drain_auth_responses()
            last_signature = token_signature(ctx)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GoMining service bot")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="login and verify the session, but never click the service button",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    xvfb = Xvfb()
    xvfb.start()

    auth_log("process_start")
    playwright = sync_playwright().start()
    ctx = None

    try:
        ctx = launch_context(playwright)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        attach_auth_logging(page)

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

        while True:
            try:
                wait = try_service(page)
            except Exception as e:
                print(f"Error occurred: {e}")
                auth_log(f"service_error error={type(e).__name__}: {e}")
                take_screenshot(page, "error_screenshot.png")
                try:
                    ensure_logged_in(page, ctx)
                except Exception as login_error:
                    print(f"Could not log in again: {login_error}")
                    auth_log(
                        f"relogin_error error={type(login_error).__name__}: {login_error}"
                    )
                wait = 60

            if wait <= 0:
                wait = 5

            keep_alive(page, ctx, wait)
    except KeyboardInterrupt:
        print("Stopped")
        auth_log("process_stopped")
    finally:
        if ctx is not None:
            try:
                drain_auth_responses()
                auth_log(f"process_end {token_state(ctx)}")
                ctx.close()
            except Exception:
                pass
        playwright.stop()
        xvfb.stop()


if __name__ == "__main__":
    main()
