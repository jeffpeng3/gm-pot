from xvfbwrapper import Xvfb
from os import getenv
from patchright.sync_api import sync_playwright, Page
from dotenv import load_dotenv
from datetime import timedelta
from time import sleep
load_dotenv()
COMMAND: str = (
    '{name}: {binary} --header "Cookie: {cookies}" --header "User-Agent: {user_agent}" {url}'
)

def CloudflareSolver(page: Page) -> None:
    try:
        click_target = page.wait_for_selector("turnstile-widget")
        pos = click_target.bounding_box()

        page.wait_for_timeout(1000)

        page.wait_for_selector('input[name=cf-turnstile-response]', state='hidden')
        target = page.query_selector('input[name=cf-turnstile-response]')

        v = str(target.get_property("value"))
    except Exception as e:
        print(f"Cloudflare turnstile not found: {e}")
        return

    while len(v) == 0:
        page.mouse.click(pos['x'] + 36, pos['y'] + 36)
        page.wait_for_timeout(1000)
        target = page.query_selector('input[name=cf-turnstile-response]')
        v = str(target.get_property("value"))

def login(page: Page) -> None:
    page.goto('https://app.gomining.com/login')

    CloudflareSolver(page)

    page.fill('input[id="email"]', getenv("EMAIL"))
    page.fill('input[id="password"]', getenv("PASSWORD"))
    page.wait_for_timeout(1000)
    page.click('button[type="submit"]')
    page.wait_for_event("load")

def skip_intro(page: Page) -> None:
    page.goto("https://app.gomining.com/nft-miners")
    page.wait_for_selector("nft-miners")
    
    while page.url == "https://app.gomining.com/nft-miners":
        try:
            if intro := page.wait_for_selector("div[class=tooltip-inner]", timeout=5000, state="visible"):
                btn = intro.query_selector("button")
                btn.click()
            else:
                return
        except:
            return
    skip_intro(page)

def try_service(page: Page) -> float:
    page.goto("https://app.gomining.com/nft-miners")
    btn = page.wait_for_selector('service-button > button')
    page.wait_for_timeout(1000)
    is_disabled = str(btn.get_property('disabled'))
    print(f"Button disabled attribute: {is_disabled}")
    if is_disabled != "true":
        page.screenshot(path="./screenshot/before_service.png")
        btn.click()
        page.screenshot(path="./screenshot/after_service.png")
        return try_service(page)

    page.screenshot(path="./screenshot/service_on_cooldown.png")

    time_str = btn.query_selector("timer")

    text = time_str.inner_text()

    remaining = text.split(':')
    if len(remaining) == 3:
        hours, minutes, seconds = map(int, remaining)
    elif len(remaining) == 2:
        hours = 0
        minutes, seconds = map(int, remaining)
    
    time = timedelta(hours=hours, minutes=minutes, seconds=seconds)

    print(f"Service is on cooldown. Time remaining: {time.total_seconds()} seconds")

    return time.total_seconds()

if __name__ == "__main__":
    xvfb = Xvfb()
    xvfb.start()
    browser = sync_playwright().start().chromium.launch(headless=False)
    page = browser.new_page()

   
    login(page)
    skip_intro(page)

    while True:
        try:
            sleep(try_service(page))
        except Exception as e:
            print(f"Error occurred: {e}")
            page.screenshot(path="./screenshot/error_screenshot.png")
            sleep(5)
            login(page)

