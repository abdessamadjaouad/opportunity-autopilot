import io
import re
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from playwright.sync_api import expect, sync_playwright
from pypdf import PdfWriter

from app.api.main import create_app
from app.models import Opportunity, ProfileFact
from app.services import add_opportunity, snapshot_profile


@pytest.fixture
def dashboard_server(db, settings):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    settings.public_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    lead = add_opportunity(db, {"url": "https://employer.example/jobs/fixture", "title": "Fixture · Junior Data Engineer",
        "employer": "Fixture employer", "country": "FR", "description": "Fixture only. Python, SQL, PostgreSQL. Visa sponsorship unknown."})
    with db.write() as session:
        item = session.get(Opportunity, lead["id"])
        item.data = {**item.data, "is_fixture": True}
        session.add(ProfileFact(id="fixture-name", key="identity.name", status="unconfirmed",
                               data={"value": {"full": "Fixture Owner"}, "sources": []}))
        session.flush()
        snapshot_profile(session, "Fixture setup")
    server = uvicorn.Server(uvicorn.Config(create_app(settings, db), log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(.02)
    assert server.started
    yield settings.public_url
    server.should_exit = True
    thread.join(timeout=10)
    listener.close()


@pytest.mark.browser
@pytest.mark.parametrize("width,height", [(1440, 1000), (390, 844)])
def test_private_dashboard_workflows_and_layout(dashboard_server, width, height):
    shots = Path("/tmp/opportunity-autopilot-ui")
    shots.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True)
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(dashboard_server)
        expect(page.get_by_role("heading", name="Welcome back")).to_be_visible()
        page.screenshot(path=str(shots / f"login-{width}.png"), full_page=True)
        page.get_by_label("Owner password").fill("fixture-owner-password")
        page.get_by_role("button", name="Open workspace").click()
        expect(page.get_by_role("heading", name="New opportunities", exact=True)).to_be_visible()
        expect(page.get_by_text("Simulator fixture", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), page.evaluate(
            "Array.from(document.querySelectorAll('*')).filter(e=>e.getBoundingClientRect().right>innerWidth+1).slice(0,10).map(e=>e.className)")
        page.screenshot(path=str(shots / f"opportunities-{width}.png"), full_page=True)
        page.get_by_role("button", name="Add opportunity", exact=True).click()
        page.get_by_label("Official listing URL").fill("https://university.example/phd/browser-test")
        page.get_by_label("Opportunity title").fill("Fixture · Funded research engineer")
        page.get_by_label("Employer or laboratory").fill("Fixture university")
        page.get_by_label("Country code").fill("DE")
        page.get_by_role("button", name="Save opportunity").click()
        expect(page.get_by_role("heading", name="Fixture · Funded research engineer")).to_be_visible()
        page.reload()
        expect(page.get_by_role("heading", name="Fixture · Funded research engineer")).to_be_visible()
        page.get_by_role("button", name="Assess eligibility").click()
        expect(page.get_by_text("Assessment updated using available evidence.", exact=True)).to_be_visible()
        for route, heading in [("applications", "Applications"), ("human-tasks", "Needs human intervention"),
                               ("documents", "Documents & profile"), ("automation", "Sources & automation")]:
            page.goto(dashboard_server + "/#" + route)
            expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
            page.wait_for_timeout(150)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), route
            page.screenshot(path=str(shots / f"{route}-{width}.png"), full_page=True)
        page.get_by_role("button", name="Register source", exact=True).click()
        page.get_by_label("Source name", exact=True).fill("Fixture registered board")
        page.get_by_label("Employer board identifier").fill("fixture-board")
        page.get_by_role("dialog").get_by_role("button", name="Register source", exact=True).click()
        expect(page.get_by_role("heading", name="Fixture registered board")).to_be_visible()
        page.get_by_role("tab", name="Operating limits").click()
        page.get_by_label("Daily submission cap").fill("1")
        page.get_by_role("button", name="Save operating limits").click()
        expect(page.get_by_text("Operating limits saved. Limits alone do not authorize spending or submissions.")).to_be_visible()
        page.get_by_role("button", name="Pause new submissions").click()
        expect(page.get_by_role("heading", name="Submission pause is active")).to_be_visible()
        page.get_by_role("tab", name="Connections", exact=True).click()
        expect(page.get_by_role("heading", name="Gmail", exact=True)).to_be_visible()
        page.screenshot(path=str(shots / f"connections-{width}.png"), full_page=True)
        page.goto(dashboard_server + "/#documents")
        page.get_by_role("button", name="Add private document").click()
        writer = PdfWriter()
        writer.add_blank_page(width=595, height=842)
        content = io.BytesIO()
        writer.write(content)
        page.get_by_label("Original PDF", exact=True).set_input_files({"name": "fixture-transcript.pdf",
            "mimeType": "application/pdf", "buffer": content.getvalue()})
        page.get_by_role("button", name="Store private PDF").click()
        expect(page.get_by_text("Private original PDF stored unchanged.")).to_be_visible()
        page.get_by_role("tab", name="Private originals").click()
        with page.expect_download() as pending:
            page.get_by_role("button", name=re.compile(r"transcript-[a-z0-9]+\.pdf")).click()
        download = pending.value
        assert re.fullmatch(r"transcript-[a-z0-9]+\.pdf", download.suggested_filename)
        page.get_by_role("contentinfo").get_by_role("button", name="Sign out", exact=True).click()
        expect(page.get_by_role("heading", name="Welcome back")).to_be_visible()
        page.reload()
        expect(page.get_by_role("heading", name="Welcome back")).to_be_visible()
        assert not errors, errors
        context.close()
        browser.close()
