import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

from native_youtube_ask_runner import (
    DEFAULT_PROFILE,
    ask_panel_text,
    clean_panel_text,
    click_ask_button,
    save_failure_artifacts,
    submit_prompt,
    wait_for_response,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT_DIR / "data" / "structured_passes"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a multi-step YouTube Ask editorial funnel.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--video-id", default="")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--browser-channel", default="chrome")
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    video_id = args.video_id or video_id_from_url(args.url)
    run_dir = OUT_DIR / f"{video_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    prompts = build_prompts(args.title, args.url)
    (run_dir / "prompts.json").write_text(json.dumps(prompts, indent=2), encoding="utf-8")

    try:
        responses = run_structured_pass(args.url, prompts, args.profile, args.browser_channel, args.timeout, run_dir)
    except Exception as exc:
        (run_dir / "error.txt").write_text(str(exc), encoding="utf-8")
        print(f"Structured pass failed: {exc}")
        print(f"Artifacts: {run_dir}")
        return 1

    for name, text in responses.items():
        (run_dir / f"{name}.txt").write_text(text, encoding="utf-8")
    review = build_local_review(responses.get("final", ""))
    (run_dir / "local_review.json").write_text(json.dumps(review, indent=2), encoding="utf-8")
    (run_dir / "local_review.txt").write_text(format_local_review(review), encoding="utf-8")
    print(run_dir)
    return 0


def build_prompts(title: str, url: str) -> dict[str, str]:
    prefix = f"Video title: {title}\nVideo URL: {url}\n\n" if title else f"Video URL: {url}\n\n"
    return {
        "outline": prefix
        + """Make a timestamped editorial outline of this whole video.

Do not select clips yet.

Return:
- 8-15 major sections with start/end timestamps
- what happens in each section
- strongest visual moments
- strongest spoken/story moments
- low-value or repetitive stretches
- recurring themes

Keep it concise but cover beginning, middle, and end.""",
        "opportunities": """Using the outline you just made, list 10-15 possible short/source opportunities.

Do not rank final winners yet.

For each opportunity include:
- title
- rough timestamp range
- type: short_ready, source_window, needs_trim, or reject
- likely hook
- payoff/end beat
- visual reason it might work
- spoken/story reason it might work
- what could make it fail as a short

Include a mix of process, story, educational, personality, and visual transformation beats when present.""",
        "drilldown": """Now choose the 6-8 strongest opportunities from your previous list and analyze them more strictly.

This is not the final ranking yet.

For each selected opportunity include:
- title
- tightened timestamp range
- window type: short_ready, source_window, needs_trim, or reject
- exact opening beat
- exact payoff/end beat
- complete thought: yes or no
- payoff inside window: yes or no
- exact quote inside window: yes or no
- standalone for a viewer who has not seen the livestream: yes or no
- visual-only candidate: yes or no
- filler/chatter/setup risk: yes or no
- exact caption quote copied from the video without ellipses
- visual proof that the moment is actually visible
- chatter risk: low, medium, or high
- trimming recommendation
- reason it could be rejected in final ranking
- provisional score from 0-100

Be stricter than the previous step. Do not keep a candidate just because the topic is emotionally strong if the window does not contain a complete usable idea.""",
        "final": """Now eliminate weak, repetitive, incomplete, and context-dependent opportunities using your strict drilldown analysis.

Return the final ranked winners plus rejected runners-up.

Use this exact format for winners:

1. Title (MM:SS - MM:SS)
Rank:
Window Type:
Chatter Risk:
Summary:
Why It Beats Alternatives:
Hook:
Payoff:
Complete Thought: yes/no
Payoff Inside Window: yes/no
Exact Quote Inside Window: yes/no
Standalone: yes/no
Visual Only: yes/no
Filler/Chatter/Setup Risk: yes/no
Exact Caption Quote:
Visual Evidence:
Completeness Check:
Editing Notes:
Score: 0-100

Rules:
- Rank by which an editor should cut first.
- Do not over-rank repetitive clips just because they are visually dramatic.
- Prefer complete, standalone short ideas, but do not punish a longer source window if it clearly contains multiple shorts.
- Exact Caption Quote must be copied from the video without ellipses.
- Prefer candidates that survived the drilldown with a clear opening beat, clear payoff, and complete thought.
- If Complete Thought is no, it cannot rank above complete candidates unless Visual Only is yes and the visual payoff is undeniable.
- If Payoff Inside Window, Exact Quote Inside Window, or Standalone is no, mark the candidate as a rejected runner-up unless there is a clear editorial rescue note.
- If a candidate is mostly filler, chatter, transition, cleanup, or setup, reject it unless the personality moment is the point.
- Include 3-5 winners.

Then add:

Rejected Runners-Up:
- title, timestamp, why it lost""",
    }


def run_structured_pass(url: str, prompts: dict[str, str], profile: Path, browser_channel: str, timeout: int, run_dir: Path) -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    failure_dir = run_dir / "failure"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            channel=browser_channel,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_load_state("networkidle", timeout=30_000)
            click_ask_button(page)
            responses = {}
            previous_text = ask_panel_text(page)
            for name, prompt in prompts.items():
                submit_prompt(page, prompt)
                panel_text = wait_for_response(page, previous_text, timeout)
                responses[name] = extract_latest_answer(panel_text, prompt)
                previous_text = ask_panel_text(page)
                time.sleep(1)
            return responses
        except Exception:
            save_failure_artifacts(page, failure_dir)
            raise
        finally:
            context.close()


def extract_latest_answer(panel_text: str, prompt: str) -> str:
    cleaned = clean_panel_text(panel_text)
    index = cleaned.rfind(prompt.strip()[:120])
    if index >= 0:
        return cleaned[index + len(prompt.strip()[:120]) :].strip()
    return cleaned


def build_local_review(final_text: str) -> dict:
    candidates = parse_final_candidates(final_text)
    for candidate in candidates:
        candidate["local_flags"] = local_flags(candidate)
        candidate["local_recommendation"] = local_recommendation(candidate)
    return {
        "candidate_count": len(candidates),
        "candidates": candidates,
        "summary": summarize_local_review(candidates),
    }


def parse_final_candidates(final_text: str) -> list[dict]:
    pattern = re.compile(
        r"(?ms)^\s*(?:\d+\.\s*)?(?P<title>[^\r\n]+?)\s*\((?P<range>\d{1,2}:\d{2}(?::\d{2})?\s*-\s*\d{1,2}:\d{2}(?::\d{2})?)\)\s*$"
        r"(?P<body>.*?)(?=^\s*(?:\d+\.\s*)?[^\r\n]+?\s*\(\d{1,2}:\d{2}(?::\d{2})?\s*-\s*\d{1,2}:\d{2}(?::\d{2})?\)\s*$|\Z)"
    )
    candidates = []
    for match in pattern.finditer(final_text):
        body = match.group("body")
        fields = {}
        for label in [
            "Rank",
            "Window Type",
            "Chatter Risk",
            "Summary",
            "Why It Beats Alternatives",
            "Hook",
            "Payoff",
            "Complete Thought",
            "Payoff Inside Window",
            "Exact Quote Inside Window",
            "Standalone",
            "Visual Only",
            "Filler/Chatter/Setup Risk",
            "Exact Caption Quote",
            "Visual Evidence",
            "Completeness Check",
            "Editing Notes",
            "Score",
        ]:
            value = extract_field(body, label)
            if value:
                fields[to_key(label)] = value
        if "rank" not in fields or "score" not in fields:
            continue
        candidates.append({"title": match.group("title").strip(), "timestamp_range": match.group("range").strip(), **fields})
    return candidates


def extract_field(body: str, label: str) -> str:
    labels = [
        "Rank",
        "Window Type",
        "Chatter Risk",
        "Summary",
        "Why It Beats Alternatives",
        "Hook",
        "Payoff",
        "Complete Thought",
        "Payoff Inside Window",
        "Exact Quote Inside Window",
        "Standalone",
        "Visual Only",
        "Filler/Chatter/Setup Risk",
        "Exact Caption Quote",
        "Visual Evidence",
        "Completeness Check",
        "Editing Notes",
        "Score",
    ]
    next_labels = "|".join(re.escape(item) for item in labels if item != label)
    match = re.search(rf"(?ms)^\s*{re.escape(label)}:\s*(.*?)(?=^\s*(?:{next_labels}):|\Z)", body)
    return " ".join(match.group(1).split()) if match else ""


def to_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def yes(value: str) -> bool:
    return value.strip().lower().startswith("yes")


def no(value: str) -> bool:
    return value.strip().lower().startswith("no")


def local_flags(candidate: dict) -> list[str]:
    flags = []
    if no(candidate.get("complete_thought", "")):
        flags.append("incomplete_thought")
    if no(candidate.get("payoff_inside_window", "")):
        flags.append("payoff_outside_window")
    if no(candidate.get("exact_quote_inside_window", "")):
        flags.append("quote_outside_window")
    if no(candidate.get("standalone", "")):
        flags.append("not_standalone")
    if yes(candidate.get("visual_only", "")):
        flags.append("visual_only")
    if yes(candidate.get("filler_chatter_setup_risk", "")):
        flags.append("filler_chatter_setup_risk")
    chatter = candidate.get("chatter_risk", "").lower()
    if "high" in chatter:
        flags.append("high_chatter")
    elif "medium" in chatter:
        flags.append("medium_chatter")
    return flags


def local_recommendation(candidate: dict) -> str:
    flags = set(candidate.get("local_flags", []))
    hard_failures = {"payoff_outside_window", "quote_outside_window", "not_standalone"}
    if flags & hard_failures:
        return "reject_or_manual_rescue"
    if "incomplete_thought" in flags and "visual_only" not in flags:
        return "reject"
    if "incomplete_thought" in flags and "visual_only" in flags:
        return "keep_as_visual_short_only"
    if "filler_chatter_setup_risk" in flags or "high_chatter" in flags:
        return "manual_review_before_cutting"
    return "candidate_ok"


def summarize_local_review(candidates: list[dict]) -> dict:
    counts = {}
    for candidate in candidates:
        recommendation = candidate.get("local_recommendation", "unknown")
        counts[recommendation] = counts.get(recommendation, 0) + 1
    return counts


def format_local_review(review: dict) -> str:
    lines = [f"Candidates parsed: {review['candidate_count']}"]
    lines.append(f"Summary: {review['summary']}")
    for candidate in review["candidates"]:
        flags = ", ".join(candidate["local_flags"]) or "none"
        lines.append(
            f"- {candidate['title']} ({candidate['timestamp_range']}): "
            f"{candidate['local_recommendation']} [{flags}]"
        )
    return "\n".join(lines) + "\n"


def video_id_from_url(url: str) -> str:
    match = re.search(r"[?&]v=([^&]+)", url)
    return match.group(1) if match else "youtube_video"


if __name__ == "__main__":
    raise SystemExit(main())
