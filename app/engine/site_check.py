"""Facts about a prospect's website, found by pattern checks (no AI).

The draft prompt offers only what these facts support, so an email never pitches online
booking to a business that already uses FareHarbor, or claims a site is outdated on a hunch.
"""
import re
from datetime import date
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

# Online booking, scheduling, reservation and ordering tools, by the hosts their widgets
# and links use. Checked in the page HTML (iframes, scripts, links).
BOOKING_TOOLS = [
    ("FareHarbor", ("fareharbor.com",)),
    ("Checkfront", ("checkfront.com",)),
    ("Peek", ("book.peek.com", "peekpro.com")),
    ("Xola", ("xola.com",)),
    ("Rezdy", ("rezdy.com",)),
    ("Bookeo", ("bookeo.com",)),
    ("Calendly", ("calendly.com/",)),
    ("Acuity Scheduling", ("acuityscheduling.com", ".as.me/")),
    ("Square Appointments", ("squareup.com/appointments", "book.squareup.com", "square.site/book")),
    ("Mindbody", ("mindbodyonline.com", "mindbody.io", "healcode")),
    ("Momence", ("momence.com",)),
    ("Mariana Tek", ("marianatek.com", "marianaiframes.com")),
    ("Zen Planner", ("zenplanner.com",)),
    ("Wodify", ("wodify.com",)),
    ("Pike13", ("pike13.com",)),
    ("ClubReady", ("clubready.com",)),
    ("Glofox", ("glofox.com",)),
    ("Walla", ("hellowalla.com",)),
    ("Jackrabbit", ("jackrabbitclass.com",)),
    ("iClassPro", ("iclasspro.com",)),
    ("Amilia", ("amilia.com",)),
    ("TeamUp", ("goteamup.com",)),
    ("PunchPass", ("punchpass.com",)),
    ("Bookwhen", ("bookwhen.com",)),
    ("Vagaro", ("vagaro.com",)),
    ("Booksy", ("booksy.com",)),
    ("Fresha", ("fresha.com",)),
    ("GlossGenius", ("glossgenius.com",)),
    ("Boulevard", ("joinblvd.com", "blvd.co/")),
    ("Schedulicity", ("schedulicity.com",)),
    ("Setmore", ("setmore.com",)),
    ("SimplyBook.me", ("simplybook.me", "simplybook.it")),
    ("PocketSuite", ("pocketsuite.io",)),
    ("Jane", ("janeapp.com", ".jane.app")),
    ("Zocdoc", ("zocdoc.com",)),
    ("NexHealth", ("nexhealth.com",)),
    ("LocalMed", ("localmed.com",)),
    ("OpenTable", ("opentable.com",)),
    ("Resy", ("resy.com",)),
    ("SevenRooms", ("sevenrooms.com",)),
    ("Tock", ("exploretock.com",)),
    ("Toast", ("toasttab.com",)),
    ("ChowNow", ("chownow.com",)),
    ("Housecall Pro", ("housecallpro.com",)),
    ("Jobber", ("getjobber.com",)),
    ("ServiceTitan", ("servicetitan.com",)),
    ("Wix Bookings", ("wix-bookings", "bookings.wixapps")),
    ("Squarespace Scheduling", ("squarespacescheduling.com",)),
    ("Eventbrite", ("eventbrite.com",)),
    ("Tripleseat", ("tripleseat.com",)),
    ("HoneyBook", ("honeybook.com",)),
    ("Dubsado", ("dubsado.com",)),
    ("Perfect Venue", ("perfectvenue.com",)),
    ("ROLLER", ("roller.app", "rollerdigital.com")),
    ("CenterEdge", ("centeredgeonline.com",)),
    ("Ticket Tailor", ("tickettailor.com",)),
]

# Marketing / CRM platforms: worth knowing (they already have tooling), but not booking.
MARKETING_TOOLS = [
    ("GoHighLevel (LeadConnector)", ("leadconnectorhq.com", "msgsndr.com")),
    ("HubSpot", ("hs-scripts.com", "hsforms.net", "hubspot.com")),
    ("Podium", ("podium.com",)),
    ("Birdeye", ("birdeye.com",)),
    ("Mailchimp", ("list-manage.com", "chimpstatic.com")),
]

# Booking/ticket links that go to another site count as online booking, even when we don't
# recognise the system. Social, maps and review hosts don't.
_BOOKING_LINK_WORDS = ("book", "reserv", "schedul", "ticket", "appointment")
_NOT_BOOKING_HOSTS = ("facebook.", "instagram.", "twitter.", "x.com", "tiktok.", "youtube.", "linkedin.",
                      "pinterest.", "google.", "goo.gl", "apple.com", "yelp.", "tripadvisor.", "wa.me",
                      "whatsapp.", "gmpg.org", "w.org", "wordpress.org")
# A page of their own whose path names booking (/book-now, /reservations, /register-online...). Its
# system often loads with scripts we can't see, so it counts as taking bookings online. Pages the
# scraper also reads for "order", "menu", "rates" or a class "schedule" don't count.
_BOOKING_PATH = re.compile(r"(?i)book|reserv|ticket|appointment|regist|enrol|sign[-_]?up")
_LINK = re.compile(r"""(?is)<(a|form|iframe)\b[^>]*?(?:href|action|src)\s*=\s*["'](https?://[^"']+)["'][^>]*>(.*?)(?:</\1>|$)""")

SITE_BUILDERS = [
    ("Wix", ("wixstatic.com", "static.parastorage.com")),
    ("Squarespace", ("squarespace.com", "squarespace-cdn.com")),
    ("GoDaddy Website Builder", ("wsimg.com",)),
    ("Weebly", ("weebly.com",)),
    ("Shopify", ("cdn.shopify.com",)),
    ("Webflow", ("webflow.com", "website-files.com")),
    ("Duda", ("multiscreensite.com", "dudamobile.com")),
    ("WordPress", ("wp-content/", "wp-includes/")),
]

FORM_SERVICES = ("wpforms", "gform_", "wpcf7", "jotform", "typeform", "hsforms", "formstack", "ninja-forms")

_INVISIBLE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>|<!--.*?-->")
_COPYRIGHT = re.compile(r"(?i)(?:©|&copy;|&#169;|copyright)\s*(?:\(c\)\s*)?((?:19|20)\d{2})(?:\s*[-–]\s*((?:19|20)\d{2}))?")


def _found(html: str, needles) -> bool:
    return any(n in html for n in needles)


def _copyright_year(pages: List[Dict[str, Any]]) -> Optional[int]:
    """Latest year in a visible copyright notice. Scripts, styles and comments are ignored:
    bundled libraries carry their own (unrelated) copyright lines."""
    years = []
    for p in pages:
        visible = _INVISIBLE.sub(" ", p.get("html") or "")
        for a, b in _COPYRIGHT.findall(visible):
            years += [int(y) for y in (a, b) if y]
    years = [y for y in years if 1995 <= y <= date.today().year]
    return max(years) if years else None


def _external_booking_hosts(pages: List[Dict[str, Any]], own_host: str) -> List[str]:
    hosts: List[str] = []
    for p in pages:
        for _tag, url, inner in _LINK.findall(p.get("html") or ""):
            host = (urlparse(url).hostname or "").lower()
            host = host[4:] if host.startswith("www.") else host
            if not host or host == own_host or host.endswith("." + own_host) or any(h in host for h in _NOT_BOOKING_HOSTS):
                continue
            target = (url + " " + re.sub(r"<[^>]+>", " ", inner)).lower()
            if any(w in target for w in _BOOKING_LINK_WORDS) and host not in hosts:
                hosts.append(host)
    return hosts


def analyze_site(intel: Dict[str, Any]) -> Dict[str, Any]:
    """Facts from the pages the scraper read (intel["pages"]: {url, html, kind}).
    Always includes "lines": the facts in plain English, for the prompt and the UI."""
    pages = [p for p in (intel.get("pages") or []) if p.get("html")]
    if not pages:
        facts: Dict[str, Any] = {"checked": False}
        facts["lines"] = describe_site_facts(facts)
        return facts
    html = "\n".join(p["html"] for p in pages).lower()
    final_url = intel.get("final_url") or pages[0].get("url") or ""
    own_host = (urlparse(final_url).hostname or "").lower()
    own_host = own_host[4:] if own_host.startswith("www.") else own_host
    booking_tools = [name for name, needles in BOOKING_TOOLS if _found(html, needles)]
    contact_form = (("<form" in html and ("<textarea" in html or 'type="email"' in html or "type='email'" in html))
                    or _found(html, FORM_SERVICES))
    facts = {
        "checked": True,
        "booking_tools": booking_tools,
        # Unrecognised systems: booking links that leave the site (only when no known tool matched).
        "external_booking_hosts": [] if booking_tools else _external_booking_hosts(pages, own_host),
        "marketing_tools": [name for name, needles in MARKETING_TOOLS if _found(html, needles)],
        "booking_pages": [p["url"] for p in pages if p.get("kind") == "booking"],
        "contact_form": bool(contact_form),
        "site_builder": next((name for name, needles in SITE_BUILDERS if _found(html, needles)), None),
        "mobile_friendly": 'name="viewport"' in html or "name='viewport'" in html,
        "https": final_url.lower().startswith("https://"),
        "copyright_year": _copyright_year(pages),
        "pages_checked": [p["url"] for p in pages],
    }
    facts["lines"] = describe_site_facts(facts)
    return facts


def describe_site_facts(facts: Dict[str, Any]) -> List[str]:
    """Plain-English, hedged statements: only what we actually saw on the pages we checked."""
    if not facts.get("checked"):
        return ["We couldn't load their website, so nothing about it is verified."]
    lines = []
    tools = facts.get("booking_tools") or []
    external = facts.get("external_booking_hosts") or []
    booking_pages = facts.get("booking_pages") or []
    own_booking = [u for u in booking_pages if _BOOKING_PATH.search(urlparse(u).path or "")]
    if tools:
        lines.append(f"They already use {', '.join(tools)} for online booking, scheduling or ordering.")
    elif external:
        lines.append(f"Their booking or ticket links go to another site ({', '.join(external)}), so they already "
                     "take bookings online there.")
    elif own_booking:
        lines.append(f"They have their own booking page ({urlparse(own_booking[0]).path}), so they already take "
                     "bookings or booking requests online. We couldn't see which system it uses.")
    else:
        where = f", including their {urlparse(booking_pages[0]).path} page" if booking_pages else ""
        lines.append(f"We didn't find an online booking or scheduling tool on the pages we checked{where}, "
                     "but one may load in a way we can't see.")
    if facts.get("marketing_tools"):
        lines.append(f"They also use {', '.join(facts['marketing_tools'])} (marketing / CRM).")
    lines.append("They have a contact form." if facts.get("contact_form") else "No contact form found on the pages we checked.")
    if facts.get("site_builder"):
        lines.append(f"The site is built with {facts['site_builder']}.")
    lines.append("The site is set up for mobile." if facts.get("mobile_friendly")
                 else "The site isn't set up for mobile (no mobile viewport).")
    if not facts.get("https"):
        lines.append("The site isn't served over HTTPS.")
    if facts.get("copyright_year"):
        lines.append(f"The footer copyright year is {facts['copyright_year']}.")
    return lines
