import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE = ROOT_DIR / "data" / "browser_profile"
DEFAULT_RESPONSES = ROOT_DIR / "data" / "native_youtube_responses"
DEFAULT_FAILURES = ROOT_DIR / "data" / "automation_failures"


def main() -> int:
    args = parse_args()
    prompt, url, stream_id = load_job(args)
    out_path = args.out or default_response_path(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        response_text = ask_youtube(
            url=url,
            prompt=prompt,
            profile_dir=args.profile,
            headless=args.headless,
            timeout_seconds=args.timeout,
            browser_channel=args.browser_channel,
        )
    except Exception as exc:
        print(f"Native YouTube Ask automation failed: {exc}", file=sys.stderr)
        return 1

    out_path.write_text(response_text, encoding="utf-8")
    print(f"Wrote native YouTube response to {out_path}")

    if args.app_url and stream_id:
        result = import_response(args.app_url, stream_id, response_text)
        print(json.dumps(result, indent=2))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ask YouTube's native Gemini panel and save/import the response.")
    parser.add_argument("--url", help="YouTube video URL. Optional when --app-url and --stream-id are provided.")
    parser.add_argument("--prompt", help="Prompt text. Optional when --prompt-file or --app-url/--stream-id are provided.")
    parser.add_argument("--prompt-file", type=Path, help="File containing the prompt to send to YouTube Ask.")
    parser.add_argument("--app-url", help="Local app URL, for example http://127.0.0.1:8001.")
    parser.add_argument("--stream-id", help="App stream_id. Used to fetch the prompt and import the response.")
    parser.add_argument("--out", type=Path, help="Where to save the raw native YouTube response.")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="Persistent Chromium profile directory.")
    parser.add_argument("--browser-channel", default="chrome", help="Playwright browser channel. Use chrome for Google sign-in compatibility.")
    parser.add_argument("--timeout", type=int, default=180, help="Seconds to wait for a completed Ask response.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless. Headed is better for login/session setup.")
    return parser.parse_args()


def load_job(args: argparse.Namespace) -> tuple[str, str, str | None]:
    stream_id = args.stream_id
    prompt = args.prompt or ""
    url = args.url or ""

    if args.prompt_file:
        prompt = args.prompt_file.read_text(encoding="utf-8")

    if args.app_url and args.stream_id and (not prompt or not url):
        job = fetch_native_prompt(args.app_url, args.stream_id)
        prompt = prompt or job["prompt"]
        url = url or job["url"]

    if not prompt:
        raise SystemExit("Provide --prompt, --prompt-file, or --app-url with --stream-id.")
    if not url:
        raise SystemExit("Provide --url, or --app-url with --stream-id.")
    return prompt, url, stream_id


def fetch_native_prompt(app_url: str, stream_id: str) -> dict:
    with urllib.request.urlopen(f"{app_url.rstrip('/')}/api/streams/{stream_id}/native-prompt", timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def ask_youtube(url: str, prompt: str, profile_dir: Path, headless: bool, timeout_seconds: int, browser_channel: str) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError('Install automation dependencies with: pip install -e ".[automation]" && playwright install chromium') from exc

    profile_dir.mkdir(parents=True, exist_ok=True)
    failure_dir = DEFAULT_FAILURES / datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as playwright:
        launch_options = {
            "user_data_dir": str(profile_dir),
            "headless": headless,
            "viewport": {"width": 1440, "height": 1000},
        }
        if browser_channel:
            launch_options["channel"] = browser_channel
        context = playwright.chromium.launch_persistent_context(**launch_options)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            click_ask_button(page)
            initial_text = ask_panel_text(page)
            submit_prompt(page, prompt)
            response_text = wait_for_response(page, initial_text, timeout_seconds)
            return response_text
        except PlaywrightTimeoutError as exc:
            save_failure_artifacts(page, failure_dir)
            raise RuntimeError(f"Timed out while driving YouTube Ask. Failure artifacts: {failure_dir}") from exc
        except Exception:
            save_failure_artifacts(page, failure_dir)
            raise
        finally:
            context.close()


def click_ask_button(page) -> None:
    button = visible_ask_button(page)
    if not button:
        if page.get_by_role("link", name=re.compile(r"Sign in", re.I)).count() or page.get_by_role("button", name=re.compile(r"Sign in", re.I)).count():
            raise RuntimeError(
                "The automation browser profile is signed out, so YouTube Ask is not available. "
                "Open the YouTube profile setup from the app, sign in, then run Native Ask again."
            )
        raise RuntimeError("Could not find a visible YouTube Ask button on this video page.")
    button.click(timeout=30_000)
    page.get_by_text(re.compile(r"Ask about this video", re.I)).wait_for(timeout=30_000)


def visible_ask_button(page):
    role_buttons = page.get_by_role("button", name=re.compile(r"^Ask$", re.I))
    for index in range(role_buttons.count()):
        candidate = role_buttons.nth(index)
        try:
            if candidate.is_visible(timeout=1000):
                return candidate
        except Exception:
            pass

    text_buttons = page.locator("button:has-text('Ask')")
    for index in range(text_buttons.count()):
        candidate = text_buttons.nth(index)
        try:
            if candidate.is_visible(timeout=1000):
                return candidate
        except Exception:
            pass
    return None


def submit_prompt(page, prompt: str) -> None:
    textbox = page.get_by_role("textbox", name=re.compile(r"Ask a question", re.I))
    textbox.click(timeout=30_000)
    page.keyboard.insert_text(prompt)
    page.keyboard.press("Enter")


def ask_panel_text(page) -> str:
    return page.evaluate(
        """() => {
          const heading = Array.from(document.querySelectorAll('h1,h2,h3'))
            .find((el) => /Ask about this video/i.test(el.textContent || ''));
          const root = heading?.closest('ytd-engagement-panel-section-list-renderer, ytd-watch-flexy, body') || document.body;
          return root.innerText || '';
        }"""
    )


def extract_appended_panel_text(initial_text: str, current_text: str) -> str:
    """Return only text appended during this Ask turn.

    YouTube can preserve prior Ask conversation text in a persistent browser profile.
    Importing the whole panel would incorrectly stamp that prior content onto the
    current Stream. Fail closed if the current panel is not an append-only extension
    of the baseline captured immediately before submitting this prompt.
    """

    initial = initial_text.replace("\r\n", "\n").rstrip()
    current = current_text.replace("\r\n", "\n").rstrip()
    if not initial:
        return current.strip()
    if current == initial:
        return ""
    if not current.startswith(initial):
        raise RuntimeError(
            "YouTube Ask panel changed non-append-only; refusing to import ambiguous or stale conversation context."
        )
    return current[len(initial) :].strip()


def wait_for_response(page, initial_text: str, timeout_seconds: int) -> str:
    deadline = time.time() + timeout_seconds
    last_delta = ""
    stable_count = 0
    while time.time() < deadline:
        text = ask_panel_text(page)
        delta = extract_appended_panel_text(initial_text, text)
        if len(delta) > 400 and delta == last_delta:
            stable_count += 1
            if stable_count >= 3:
                return clean_panel_text(delta)
        else:
            stable_count = 0
            last_delta = delta
        time.sleep(2)
    raise TimeoutError("YouTube Ask did not produce a stable response before timeout.")


def clean_panel_text(text: str) -> str:
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line and line.lower() not in {"ask about this video", "ask a question..."}]
    return "\n".join(lines).strip()


def save_failure_artifacts(page, failure_dir: Path) -> None:
    failure_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(failure_dir / "screenshot.png"), full_page=True)
    except Exception:
        pass
    try:
        (failure_dir / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass


def default_response_path(url: str) -> Path:
    match = re.search(r"[?&]v=([^&]+)", url)
    video_id = match.group(1) if match else "youtube_video"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return DEFAULT_RESPONSES / f"{video_id}_{stamp}.txt"


def import_response(app_url: str, stream_id: str, response_text: str) -> dict:
    payload = urllib.parse.urlencode(
        {
            "stream_id": stream_id,
            "source": "native-youtube-gemini-sidebar-automated",
            "response_text": response_text,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{app_url.rstrip('/')}/api/native/import",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    raise SystemExit(main())