from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from typing import Optional, List
from app.database import get_db_connection
import json
from app.config import settings
from app.engine.discovery import refresh_campaign_pool
from app.engine.drafting import draft_leads, drafting_count, sendable_lead_ids, undrafted_lead_ids
from app.engine.fit import campaign_lead_ids, qualified_sql, rate_leads, rating_count
from app.engine.lead_builder import process_next_batch
from app.engine.overture import OvertureError
from app.engine.search_plan import PlanError
from app.engine import sequencer as _sequencer
from app.engine.sequencer import (
    POSTAL_ADDRESS_MISSING, compliance_ready, dispatch_campaign_batch, get_next_available_inbox,
    run_campaign_send, send_window_text,
)
from app.engine.analyzer import analyze_my_product
from app.engine.copywriter import default_draft_instructions

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


class ProductAnalyzeRequest(BaseModel):
    url: str


class AutopilotRequest(BaseModel):
    url: str
    auto_send: bool = False
    max_campaigns: int = 3
    leads_per_campaign: int = 5


@router.post("/analyze-product")
async def analyze_product_endpoint(req: ProductAnalyzeRequest):
    return await analyze_my_product(req.url)


@router.post("/autopilot")
async def launch_autopilot(req: AutopilotRequest, background_tasks: BackgroundTasks):
    """
    Explee-style 1-click autopilot:
    1. Crawls the submitted website & extracts product + ICP segments
    2. Creates campaigns for each ICP segment
    3. Discovers leads, verifies mailboxes, drafts AI copy, and optionally auto-sends
    """
    analysis = await analyze_my_product(req.url)
    value_prop = analysis.get("value_proposition") or f"Solutions by {req.url}"
    product_name = analysis.get("product_name", req.url)
    icps = (analysis.get("target_icps") or [])[: max(1, min(req.max_campaigns, 6))]

    created = []
    conn = await get_db_connection()
    try:
        for icp in icps:
            camp_name = icp.get("campaign_name") or "Target ICP"
            icp_query = icp.get("icp_query") or value_prop
            cursor = await conn.execute(
                """
                INSERT INTO campaigns (name, icp_prompt, target_industry, value_proposition, status, batch_size)
                VALUES (?, ?, ?, ?, 'discovering', ?)
                """,
                (camp_name, icp_query, product_name[:80], value_prop,
                 max(1, min(req.leads_per_campaign, 1000))),
            )
            cid = cursor.lastrowid
            created.append({"id": cid, "name": camp_name, "icp_query": icp_query})
            background_tasks.add_task(run_discovery_task, cid, req.auto_send)
        await conn.commit()
    finally:
        await conn.close()

    return {
        "product_name": product_name,
        "value_proposition": value_prop,
        "key_features": analysis.get("key_features", []),
        "crawled_subpages": analysis.get("crawled_subpages", []),
        "auto_send": req.auto_send,
        "campaigns_created": created,
    }


class CampaignCreate(BaseModel):
    name: str
    icp_prompt: str
    target_industry: Optional[str] = "B2B SaaS"
    value_proposition: str
    auto_send: bool = False
    batch_size: Optional[int] = None
    draft_instructions: Optional[str] = None  # blank or the default -> use the live default


class DraftInstructionsUpdate(BaseModel):
    instructions: Optional[str] = None  # None, blank or the default -> back to the live default


def _custom_instructions(text: Optional[str]) -> Optional[str]:
    """What to store: None means "use the current default", so a campaign that never changed
    its instructions keeps getting improvements to the default."""
    text = (text or "").strip()
    return None if not text or text == default_draft_instructions().strip() else text


_KEEP = object()  # _set_campaign: leave discovery_message as it is


async def _set_campaign(campaign_id: int, status: str, message=_KEEP):
    """Set the status, and discovery_message unless message is _KEEP (None clears it)."""
    conn = await get_db_connection()
    try:
        if message is _KEEP:
            await conn.execute("UPDATE campaigns SET status = ? WHERE id = ?", (status, campaign_id))
        else:
            await conn.execute(
                "UPDATE campaigns SET status = ?, discovery_message = ? WHERE id = ?",
                (status, message, campaign_id),
            )
        await conn.commit()
    finally:
        await conn.close()


def _joined(refresh_result) -> Optional[str]:
    return "\n".join((refresh_result or {}).get("messages") or []) or None


def _failure_message(e: Exception) -> str:
    if isinstance(e, (PlanError, OvertureError)):
        return str(e)
    return f"Discovery failed: {type(e).__name__}: {e}"


async def run_discovery_task(campaign_id: int, auto_send: bool = False) -> None:
    await _set_campaign(campaign_id, "discovering")
    try:
        conn = await get_db_connection()
        try:
            cur = await conn.execute(
                "SELECT search_plan, overture_release, batch_size, value_proposition FROM campaigns WHERE id = ?",
                (campaign_id,))
            camp = await cur.fetchone()
        finally:
            await conn.close()
        if not camp:
            return
        # A success replaces any stale warning or error with this run's search messages.
        message = None
        # No completed search yet: no plan, or the plan was stored but its first fetch
        # failed or was interrupted (the release is set only when a fetch completes).
        if not camp["search_plan"] or not camp["overture_release"]:
            message = _joined(await refresh_campaign_pool(campaign_id))
        res = await process_next_batch(
            campaign_id, camp["batch_size"] or settings.discovery_batch_size, camp["value_proposition"] or "")
        await _set_campaign(campaign_id, "ready", message)
        if auto_send:
            await dispatch_campaign_batch(campaign_id, limit=res.get("leads", 0))
    except Exception as e:
        print(f"[Campaign Task Error]: {e}")
        await _set_campaign(campaign_id, "error", _failure_message(e))


async def run_refresh_task(campaign_id: int) -> None:
    await _set_campaign(campaign_id, "discovering")
    try:
        res = await refresh_campaign_pool(campaign_id)
        await _set_campaign(campaign_id, "ready", _joined(res))
    except Exception as e:
        print(f"[Campaign Refresh Error]: {e}")
        await _set_campaign(campaign_id, "error", _failure_message(e))


@router.get("")
async def list_campaigns():
    conn = await get_db_connection()
    q = qualified_sql("l")
    cursor = await conn.execute(f"""
        SELECT c.*,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.verification_status IN ('verified', 'deliverable')) as verified_count,
            (SELECT COUNT(*) FROM campaign_companies cc WHERE cc.campaign_id = c.id AND cc.status = 'done') as pool_done,
            (SELECT COUNT(*) FROM campaign_companies cc WHERE cc.campaign_id = c.id AND cc.status = 'skipped') as pool_skipped,
            (SELECT COUNT(*) FROM campaign_companies cc WHERE cc.campaign_id = c.id AND cc.status = 'processing') as pool_processing,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.status = 'sent') as sent_leads_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.status IN ('enriched', 'queued', 'discovered') AND l.verification_status != 'invalid' AND l.email IS NOT NULL AND l.email != '' AND {q}) as ready_leads_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.status IN ('enriched', 'queued', 'discovered') AND l.verification_status != 'invalid' AND l.email IS NOT NULL AND l.email != '' AND (l.personalized_body IS NULL OR l.personalized_body = '') AND {q}) as undrafted_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.status IN ('enriched', 'queued', 'discovered') AND l.verification_status != 'invalid' AND l.email IS NOT NULL AND l.email != '' AND l.personalized_body IS NOT NULL AND l.personalized_body != '' AND {q}) as drafted_count,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.fit_score IS NOT NULL AND {q}) as fit_qualified,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND NOT {q}) as fit_disqualified,
            (SELECT COUNT(*) FROM leads l WHERE l.campaign_id = c.id AND l.fit_score IS NULL AND l.fit_override IS NULL) as fit_unrated
        FROM campaigns c
        ORDER BY c.created_at DESC
    """)
    rows = await cursor.fetchall()
    await conn.close()

    result = []
    for r in rows:
        d = dict(r)
        try:
            d["search_plan"] = json.loads(d["search_plan"]) if d.get("search_plan") else None
        except (TypeError, ValueError):
            d["search_plan"] = None
        total = d.get("total_leads") or 0
        sent = d.get("sent_count") or d.get("sent_leads_count") or 0
        raw_status = d.get("status", "draft")

        # Determine real-time display status
        if raw_status in ("discovering", "sending"):
            d["display_status"] = raw_status
        elif total == 0:
            d["display_status"] = "no_leads"
        elif total > 0 and sent >= total:
            d["display_status"] = "completed"
        elif total > 0 and sent < total:
            d["display_status"] = "ready"
        else:
            d["display_status"] = raw_status

        d["drafting_count"] = drafting_count(d["id"])
        d["rating_count"] = rating_count(d["id"])
        result.append(d)
    return result


@router.post("")
async def create_campaign(req: CampaignCreate, background_tasks: BackgroundTasks):
    conn = await get_db_connection()
    batch_size = max(1, min(req.batch_size or settings.discovery_batch_size, 1000))
    cursor = await conn.execute("""
        INSERT INTO campaigns (name, icp_prompt, target_industry, value_proposition, status, batch_size,
                               draft_instructions)
        VALUES (?, ?, ?, ?, 'draft', ?, ?)
    """, (req.name, req.icp_prompt, req.target_industry, req.value_proposition, batch_size,
          _custom_instructions(req.draft_instructions)))
    campaign_id = cursor.lastrowid
    await conn.commit()
    await conn.close()

    # Automatically trigger discovery in the background
    background_tasks.add_task(run_discovery_task, campaign_id, req.auto_send)

    return {"id": campaign_id, "name": req.name, "status": "discovering"}


@router.get("/{campaign_id}")
async def get_campaign(campaign_id: int):
    conn = await get_db_connection()
    cursor = await conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    camp = await cursor.fetchone()
    if not camp:
        await conn.close()
        raise HTTPException(status_code=404, detail="Campaign not found")

    leads_cur = await conn.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC", (campaign_id,))
    leads = [dict(r) for r in await leads_cur.fetchall()]
    await conn.close()

    result = dict(camp)
    result["leads"] = leads
    return result


async def _claim_discovery(campaign_id: int):
    """Atomically move the campaign to 'discovering', or raise 404 / 409
    (already discovering, or sending)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute(
            "UPDATE campaigns SET status = 'discovering' WHERE id = ? "
            "AND (status IS NULL OR status NOT IN ('discovering', 'sending'))",
            (campaign_id,))
        await conn.commit()
        if cur.rowcount:
            return
        cur = await conn.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,))
        row = await cur.fetchone()
    finally:
        await conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Campaign not found")
    if row["status"] == "sending":
        raise HTTPException(status_code=409, detail="Campaign is sending; try again when it finishes.")
    raise HTTPException(status_code=409, detail="Discovery is already running for this campaign.")


@router.post("/{campaign_id}/discover")
async def trigger_campaign_discovery(campaign_id: int, background_tasks: BackgroundTasks):
    await _claim_discovery(campaign_id)
    background_tasks.add_task(run_discovery_task, campaign_id, False)
    return {"id": campaign_id, "status": "discovering"}


class AutoSendUpdate(BaseModel):
    enabled: bool


@router.put("/{campaign_id}/auto-send")
async def update_auto_send(campaign_id: int, req: AutoSendUpdate):
    """When on, the scheduler sends this campaign on weekdays during sending hours, up to each inbox's limit."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("UPDATE campaigns SET auto_send = ? WHERE id = ?", (1 if req.enabled else 0, campaign_id))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Campaign not found")
    finally:
        await conn.close()
    return {"ok": True, "auto_send": req.enabled}


class MinFitUpdate(BaseModel):
    min_fit_score: int = Field(ge=1, le=5)


@router.post("/{campaign_id}/rate")
async def rate_campaign_leads(campaign_id: int, background_tasks: BackgroundTasks, rerate: bool = False):
    """Rates fit for the campaign's unrated leads (or all of them with rerate=true), in the background."""
    conn = await get_db_connection()
    try:
        found = await (await conn.execute("SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,))).fetchone()
    finally:
        await conn.close()
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    ids = await campaign_lead_ids(campaign_id, overwrite=rerate)
    if ids:
        background_tasks.add_task(rate_leads, ids, rerate)
    return {"queued": len(ids)}


@router.put("/{campaign_id}/min-fit")
async def update_min_fit(campaign_id: int, req: MinFitUpdate):
    """Leads rated below this are disqualified (skipped by drafting and sending)."""
    conn = await get_db_connection()
    try:
        cur = await conn.execute("UPDATE campaigns SET min_fit_score = ? WHERE id = ?", (req.min_fit_score, campaign_id))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Campaign not found")
    finally:
        await conn.close()
    return {"ok": True}


@router.get("/draft-instructions/default")
async def get_default_draft_instructions():
    return {"instructions": default_draft_instructions()}


@router.put("/{campaign_id}/draft-instructions")
async def update_draft_instructions(campaign_id: int, req: DraftInstructionsUpdate):
    conn = await get_db_connection()
    try:
        cur = await conn.execute("UPDATE campaigns SET draft_instructions = ? WHERE id = ?",
                                 (_custom_instructions(req.instructions), campaign_id))
        await conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Campaign not found")
    finally:
        await conn.close()
    return {"ok": True, "uses_default": _custom_instructions(req.instructions) is None}


@router.post("/{campaign_id}/draft")
async def draft_campaign_emails(campaign_id: int, background_tasks: BackgroundTasks, redraft: bool = False):
    """Drafts emails for every undrafted lead in the campaign, in the background. With
    redraft=true, redrafts every sendable lead, replacing existing drafts."""
    conn = await get_db_connection()
    try:
        found = await (await conn.execute("SELECT 1 FROM campaigns WHERE id = ?", (campaign_id,))).fetchone()
    finally:
        await conn.close()
    if not found:
        raise HTTPException(status_code=404, detail="Campaign not found")
    ids = await (sendable_lead_ids(campaign_id) if redraft else undrafted_lead_ids(campaign_id))
    if ids:
        background_tasks.add_task(draft_leads, ids, redraft)
    return {"queued": len(ids)}


@router.post("/{campaign_id}/refresh-search")
async def refresh_campaign_search(campaign_id: int, background_tasks: BackgroundTasks):
    await _claim_discovery(campaign_id)
    background_tasks.add_task(run_refresh_task, campaign_id)
    return {"id": campaign_id, "status": "discovering"}


@router.post("/{campaign_id}/launch")
async def launch_campaign(campaign_id: int, background_tasks: BackgroundTasks):
    conn = await get_db_connection()
    try:
        unsent = ("status IN ('enriched', 'queued', 'discovered') AND verification_status != 'invalid' "
                  "AND email IS NOT NULL AND email != ''")
        cur = await conn.execute(f"""
            SELECT
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND {unsent} AND {qualified_sql()}) as sendable,
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND {unsent} AND NOT {qualified_sql()}) as disqualified,
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ?) as total,
                (SELECT COUNT(*) FROM leads WHERE campaign_id = ? AND status = 'sent') as sent,
                (SELECT COALESCE(min_fit_score, 3) FROM campaigns WHERE id = ?) as min_fit
        """, (campaign_id, campaign_id, campaign_id, campaign_id, campaign_id))
        row = await cur.fetchone()
        sendable = row["sendable"] if row else 0
        disqualified = row["disqualified"] if row else 0
        total = row["total"] if row else 0
        sent = row["sent"] if row else 0
        min_fit = row["min_fit"] if row and row["min_fit"] is not None else 3

        camp_cur = await conn.execute("SELECT status FROM campaigns WHERE id = ?", (campaign_id,))
        camp_row = await camp_cur.fetchone()
        current_status = camp_row["status"] if camp_row else "draft"
    finally:
        await conn.close()

    if current_status == "sending":
        return {"status": "sending", "message": "Campaign is already dispatching emails in the background!"}
    if current_status == "discovering":
        return {"status": "discovering", "message": "Campaign is currently discovering leads. Please wait until discovery finishes."}

    if sendable == 0:
        if disqualified:
            return {
                "dispatched": 0,
                "status": "all_disqualified",
                "message": f"All {disqualified} remaining leads are disqualified (fit below {min_fit}). "
                           "Lower 'Disqualify below' on the campaign card, or requalify leads, to send to them."
            }
        if total > 0 and sent >= total:
            return {
                "dispatched": 0,
                "status": "completed",
                "message": f"All {total} leads in this campaign have already been dispatched!"
            }
        return {
            "dispatched": 0,
            "status": "no_leads",
            "message": "No leads in this campaign yet. Click 'Find Leads' first to discover prospects."
        }

    if not compliance_ready():
        return {"dispatched": 0, "status": "no_postal_address", "message": POSTAL_ADDRESS_MISSING}

    if not _sequencer.in_send_window():
        return {
            "dispatched": 0,
            "status": "outside_window",
            "message": f"Outside sending hours ({send_window_text()}). Launch again during sending hours."
        }

    inbox = await get_next_available_inbox()
    if not inbox:
        conn = await get_db_connection()
        try:
            inbox_cur = await conn.execute("SELECT email, daily_quota, sent_today FROM inboxes WHERE status = 'active' LIMIT 1")
            ib_row = await inbox_cur.fetchone()
        finally:
            await conn.close()

        if ib_row:
            return {
                "status": "quota_exceeded",
                "dispatched": 0,
                "message": f"Daily sending quota reached ({ib_row['sent_today']}/{ib_row['daily_quota']} sent today for {ib_row['email']}). Increase daily quota in Inboxes & Domains or reset quota to send more."
            }
        return {
            "status": "no_inbox",
            "dispatched": 0,
            "message": "No active sending inboxes found. Please connect an inbox in Inboxes & Domains."
        }

    conn = await get_db_connection()
    try:
        await conn.execute("UPDATE campaigns SET status = 'sending' WHERE id = ?", (campaign_id,))
        await conn.commit()
    finally:
        await conn.close()

    background_tasks.add_task(run_campaign_send, campaign_id, 20)
    return {
        "status": "sending",
        "message": (f"Sending up to {sendable} emails in the background, a few minutes apart. "
                    "Each inbox stops at its daily limit."),
        "queue_count": sendable
    }


@router.delete("/{campaign_id}")
async def delete_campaign(campaign_id: int):
    conn = await get_db_connection()
    await conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
    await conn.commit()
    await conn.close()
    return {"ok": True}

