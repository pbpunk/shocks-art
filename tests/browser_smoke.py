import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def cleanup_fixture() -> None:
    from sqlalchemy import select

    from app.core.database import SessionLocal
    from app.models import AnalysisRun, CandidateWindow, Stream

    with SessionLocal() as db:
        fixture_streams = db.scalars(
            select(Stream).where(
                (Stream.source_video_id == "fixture_stream_001") |
                (Stream.channel_id == "fixture_channel")
            )
        ).all()
        for stream in fixture_streams:
            for candidate in db.scalars(select(CandidateWindow).where(CandidateWindow.stream_id == stream.stream_id)).all():
                db.delete(candidate)
            for run in db.scalars(select(AnalysisRun).where(AnalysisRun.stream_id == stream.stream_id)).all():
                db.delete(run)
            db.delete(stream)
        db.commit()


try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 900})
        page.goto("http://127.0.0.1:8000")
        page.wait_for_load_state("networkidle")
        assert "Shocks Art Clips" in page.text_content("body")
        page.goto("http://127.0.0.1:8000/pipeline")
        page.wait_for_load_state("networkidle")
        assert "Livestream Pipeline" in page.text_content("body")
        page.request.post("http://127.0.0.1:8000/api/fixtures/load-candidate")
        page.goto("http://127.0.0.1:8000/candidates")
        page.wait_for_load_state("networkidle")
        assert "Turning a mistake into a clean recovery" in page.text_content("body")
        page.goto("http://127.0.0.1:8000/debug")
        page.wait_for_load_state("networkidle")
        assert "gemini-audit-console" in page.text_content("body")
        page.screenshot(path="data/browser_smoke_candidates.png", full_page=True)
        browser.close()
finally:
    cleanup_fixture()
