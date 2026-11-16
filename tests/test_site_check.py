"""Facts about a prospect's website, detected without AI, to ground what the email offers."""
import httpx
import pytest

from app.engine import scraper
from app.engine.site_check import analyze_site, describe_site_facts


def page(url, html, kind="home"):
    return {"url": url, "html": html, "kind": kind}


HOME = """<html><head><meta name="viewport" content="width=device-width">
<link rel="stylesheet" href="/wp-content/themes/x/style.css">
<style>/* Copyright (c) 2015 Daniel Eden */</style></head>
<body><p>Deep sea fishing from Port Richey.</p>
<footer>&copy; 2016 - 2019 Reef Chief Charters</footer></body></html>"""

BOOK_WITH_TOOL = """<html><body><h1>Book</h1>
<iframe src="https://fareharbor.com/embeds/book/reefchief/"></iframe></body></html>"""

BOOK_PHONE_ONLY = """<html><body><h1>Book Your Charter</h1><p>Call us at 727-869-3201 to reserve.</p>
<form action="/contact"><input type="email" name="e"><textarea></textarea></form></body></html>"""


def test_detects_booking_tool_builder_mobile_https_and_real_copyright_year():
    facts = analyze_site({"final_url": "https://reef.com/", "pages": [
        page("https://reef.com/", HOME), page("https://reef.com/book", BOOK_WITH_TOOL, "booking")]})
    assert facts["checked"] is True
    assert facts["booking_tools"] == ["FareHarbor"]
    assert facts["site_builder"] == "WordPress"
    assert facts["mobile_friendly"] is True and facts["https"] is True
    assert facts["copyright_year"] == 2019  # not 2015 from the CSS comment


def test_booking_page_without_a_known_tool_is_reported_hedged():
    facts = analyze_site({"final_url": "http://reef.com/", "pages": [
        page("http://reef.com/", "<html><body><p>Hi</p></body></html>"),
        page("http://reef.com/book-your-trip", BOOK_PHONE_ONLY, "booking")]})
    assert facts["booking_tools"] == [] and facts["booking_pages"] == ["http://reef.com/book-your-trip"]
    assert facts["contact_form"] is True and facts["https"] is False and facts["mobile_friendly"] is False
    lines = describe_site_facts(facts)
    assert any("didn't find" in l and "/book-your-trip" in l for l in lines)
    assert any("HTTPS" in l for l in lines) and any("mobile" in l.lower() for l in lines)


def test_lines_say_they_already_use_a_tool():
    facts = analyze_site({"final_url": "https://x.com/", "pages": [
        page("https://x.com/", '<a href="https://www.opentable.com/r/x">Reserve</a>')]})
    assert facts["booking_tools"] == ["OpenTable"]
    assert any("already" in l and "OpenTable" in l for l in describe_site_facts(facts))


def test_unreachable_site_is_unchecked():
    facts = analyze_site({"error": "HTTP 503", "pages": []})
    assert facts["checked"] is False
    assert describe_site_facts(facts) == ["We couldn't load their website, so nothing about it is verified."]


def test_analysis_carries_its_own_description_lines():
    facts = analyze_site({"final_url": "https://x.com/", "pages": [page("https://x.com/", HOME)]})
    assert facts["lines"] == describe_site_facts(facts)


# ---------- scraper reads booking pages and keeps page HTML ----------

SITE = {
    "https://reef.com/": '<html><head><title>Reef</title></head><body><p>' + "Fishing trips daily. " * 3 + '</p>'
                         '<a href="/about-us">About</a><a href="/book-your-trip">Book Your Trip</a>'
                         '<a href="/faq">FAQ</a></body></html>',
    "https://reef.com/book-your-trip": BOOK_WITH_TOOL,
    "https://reef.com/about-us": "<html><body><p>Captain Mike has fished the Gulf for 30 years.</p></body></html>",
}


@pytest.fixture
def fake_web(monkeypatch):
    def handler(request):
        html = SITE.get(str(request.url))
        return httpx.Response(200, text=html) if html else httpx.Response(404)

    real = httpx.AsyncClient

    def client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(scraper.httpx, "AsyncClient", client)


async def test_scraper_reads_booking_pages_first_and_keeps_html(fake_web):
    intel = await scraper.scrape_company_website("https://reef.com/")
    kinds = [(p["url"], p["kind"]) for p in intel["pages"]]
    assert kinds[0] == ("https://reef.com/", "home")
    assert ("https://reef.com/book-your-trip", "booking") in kinds
    assert ("https://reef.com/about-us", "about") in kinds
    assert intel["final_url"] == "https://reef.com/"
    assert "fareharbor" in next(p["html"] for p in intel["pages"] if p["kind"] == "booking")


# ---------- patterns seen on real lead sites ----------

def _facts(html, kind="booking", url="https://x.com/book"):
    return analyze_site({"final_url": "https://x.com/", "pages": [
        page("https://x.com/", "<html><body><p>Home</p></body></html>"), page(url, html, kind)]})


def test_momence_and_mindbody_widget_tags_are_detected():
    assert _facts('<script src="https://momence.com/plugin/host-schedule/x.js"></script>')["booking_tools"] == ["Momence"]
    assert _facts('<healcode-widget data-type="schedules"></healcode-widget>')["booking_tools"] == ["Mindbody"]


def test_booking_on_another_site_is_recorded():
    facts = _facts('<a href="https://www.iflysales.com/orlando/book">Book Now</a>'
                   '<a href="https://www.instagram.com/x">Instagram</a>')
    assert facts["external_booking_hosts"] == ["iflysales.com"]
    assert any("iflysales.com" in l for l in facts["lines"])
    assert not any("didn't find" in l for l in facts["lines"])


def test_not_found_wording_is_hedged():
    lines = _facts("<p>Call to reserve.</p>")["lines"]
    line = next(l for l in lines if "booking" in l)
    assert "didn't find" in line and "may" in line
    assert "phone, email or a form" not in line


def test_marketing_tools_are_a_separate_fact():
    facts = _facts('<script src="https://widgets.leadconnectorhq.com/loader.js"></script>', kind="home",
                   url="https://x.com/")
    assert facts["marketing_tools"] == ["GoHighLevel (LeadConnector)"]
    assert facts["booking_tools"] == []
    assert any("GoHighLevel" in l for l in facts["lines"])


def test_order_links_to_partner_sites_are_not_booking():
    facts = _facts('<a href="https://hob.beer/shop">Order our beer</a>', kind="booking", url="https://x.com/menus/")
    assert facts["external_booking_hosts"] == []
