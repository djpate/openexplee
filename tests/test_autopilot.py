"""Autopilot: a campaign with automatic sending on also finds its own new leads, so it keeps
sending every weekday instead of stopping when the leads found so far run out."""
import asyncio

import aiosqlite
import httpx
import pytest

from app.api import campaigns
from app.config import settings
from app.engine import scheduler


@pytest.fixture
def discovered(monkeypatch):
    """Records discovery runs instead of searching; each run 'finishes' immediately."""
    runs = []

    async def run(campaign_id):
        runs.append(campaign_id)

    monkeypatch.setattr(campaigns, "run_discovery_task", run)
    monkeypatch.setattr(scheduler, "_today", lambda: "2026-11-16")
    return runs


async def _inbox(db, quota=5, status="active"):
    async with aiosqlite.connect(db) as c:
        await c.execute(
            "INSERT INTO inboxes (email, provider, smtp_host, smtp_user, smtp_password, daily_quota, status) "
            "VALUES (?, 'google', 'smtp.gmail.com', 'u', 'p', ?, ?)", (f"me{quota}{status}@x.com", quota, status))
        await c.commit()


async def _campaign(db, queued, auto_send=1, status="ready", pending=100, release="2026-10-21.0"):
    """A campaign with `queued` qualified leads not emailed yet and `pending` businesses left to check."""
    async with aiosqlite.connect(db) as c:
        cid = (await c.execute(
            "INSERT INTO campaigns (name, icp_prompt, auto_send, status, pool_pending, overture_release) "
            "VALUES ('C', 'i', ?, ?, ?, ?)", (auto_send, status, pending, release))).lastrowid
        for i in range(queued):
            await c.execute(
                "INSERT INTO leads (campaign_id, company_name, company_domain, email, verification_status, status) "
                "VALUES (?, ?, ?, ?, 'verified', 'enriched')", (cid, f"c{i}", f"c{i}.com", f"info@c{i}.com"))
        await c.commit()
        return cid


async def _campaign_row(db, cid):
    async with aiosqlite.connect(db) as c:
        c.row_factory = aiosqlite.Row
        return dict(await (await c.execute("SELECT * FROM campaigns WHERE id = ?", (cid,))).fetchone())


async def _finish(db, cid):
    async with aiosqlite.connect(db) as c:
        await c.execute("UPDATE campaigns SET status = 'ready' WHERE id = ?", (cid,))
        await c.commit()


async def test_finds_more_leads_when_fewer_than_two_days_are_queued(db, discovered):
    await _inbox(db, quota=5)
    cid = await _campaign(db, queued=9)  # 5 a day: 10 is two days' worth
    assert await scheduler.autopilot_topup_tick() == [cid]
    await asyncio.sleep(0)
    assert discovered == [cid]
    row = await _campaign_row(db, cid)
    assert row["status"] == "discovering" and row["autopilot_batches"] == 1
    assert await scheduler.autopilot_topup_tick() == []  # already discovering


async def test_no_search_when_two_days_are_queued(db, discovered):
    await _inbox(db, quota=5)
    await _campaign(db, queued=10)
    assert await scheduler.autopilot_topup_tick() == [] and discovered == []


async def test_daily_capacity_counts_every_active_inbox(db, discovered):
    await _inbox(db, quota=5)
    await _inbox(db, quota=10)
    await _inbox(db, quota=50, status="paused")
    cid = await _campaign(db, queued=20)  # 15 a day: needs 30
    assert await scheduler.autopilot_topup_tick() == [cid]


async def test_nothing_is_searched_without_a_sending_inbox(db, discovered):
    await _campaign(db, queued=0)
    assert await scheduler.autopilot_topup_tick() == [] and discovered == []


@pytest.mark.parametrize("auto_send,status", [(0, "ready"), (1, "sending"), (1, "discovering")])
async def test_only_idle_autopilot_campaigns_are_searched(db, discovered, auto_send, status):
    await _inbox(db)
    await _campaign(db, queued=0, auto_send=auto_send, status=status)
    assert await scheduler.autopilot_topup_tick() == [] and discovered == []


async def test_at_most_three_batches_a_day(db, discovered, monkeypatch):
    await _inbox(db)
    cid = await _campaign(db, queued=0)  # every batch comes back empty
    for _ in range(3):
        assert await scheduler.autopilot_topup_tick() == [cid]
        await asyncio.sleep(0)
        await _finish(db, cid)
    assert await scheduler.autopilot_topup_tick() == []
    monkeypatch.setattr(scheduler, "_today", lambda: "2026-11-17")
    assert await scheduler.autopilot_topup_tick() == [cid]
    assert (await _campaign_row(db, cid))["autopilot_batches"] == 1


async def test_a_failed_search_can_be_retried_the_same_day(db, discovered):
    await _inbox(db)
    cid = await _campaign(db, queued=0, status="error")
    assert await scheduler.autopilot_topup_tick() == [cid]


async def test_says_so_when_every_business_has_been_checked(db, discovered):
    await _inbox(db)
    cid = await _campaign(db, queued=0, pending=0)
    assert await scheduler.autopilot_topup_tick() == [] and discovered == []
    assert "every business" in (await _campaign_row(db, cid))["discovery_message"]


async def test_a_search_that_never_finished_is_resumed(db, discovered):
    await _inbox(db)
    cid = await _campaign(db, queued=0, pending=0, release=None)  # run_discovery_task fetches the pool first
    assert await scheduler.autopilot_topup_tick() == [cid]


async def test_scheduler_sends_then_tops_up(monkeypatch):
    order = []
    names = ("reset_daily_inbox_quotas", "poll_inboxes", "process_due_followups", "auto_send_tick",
             "autopilot_topup_tick")
    for name in names:
        async def fn(*a, _n=name, **k):
            order.append(_n)
        monkeypatch.setattr(scheduler, name, fn)
    await scheduler.scheduler_tick()
    assert order == list(names)


# ---------- the switch ----------

@pytest.fixture
async def client(db):
    from app.main import app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_turning_autopilot_on_says_what_it_still_needs(db, client, monkeypatch):
    cid = await _campaign(db, queued=0)
    monkeypatch.setattr(settings, "postal_address", "")
    r = (await client.put(f"/api/campaigns/{cid}/auto-send", json={"enabled": True})).json()
    assert r["auto_send"] is True
    assert any("inbox" in w for w in r["waiting_for"]) and any("postal address" in w for w in r["waiting_for"])
    await _inbox(db)
    monkeypatch.setattr(settings, "postal_address", "1 Main St")
    r = (await client.put(f"/api/campaigns/{cid}/auto-send", json={"enabled": True})).json()
    assert r["waiting_for"] == []


async def test_creating_a_campaign_with_auto_send_turns_autopilot_on(db, client, discovered):
    r = await client.post("/api/campaigns", json={
        "name": "C", "icp_prompt": "dentists in Tampa", "value_proposition": "vp", "auto_send": True})
    assert (await _campaign_row(db, r.json()["id"]))["auto_send"] == 1
