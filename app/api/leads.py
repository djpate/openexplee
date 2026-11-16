from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from typing import Optional, List
from app.database import get_db_connection
from app.engine.verifier import verify_email_smtp, generate_email_permutations, enrich_person_email
from app.engine.copywriter import generate_cold_email
from app.engine.company_pool import DELETED_BY_USER, refresh_pool_counters
from app.engine.drafting import draft_leads, draftable_lead_ids

router = APIRouter(prefix="/api/leads", tags=["leads"])


class VerifyEmailRequest(BaseModel):
    email: str


class EnrichEmailRequest(BaseModel):
    first_name: str
    last_name: str
    company_domain: str


class SuppressionCreateRequest(BaseModel):
    entry_type: str = "email"  # 'email' or 'domain'
    value: str
    reason: Optional[str] = "manual"


class GenerateCopyRequest(BaseModel):
    company_name: str
    company_domain: str
    contact_name: str
    job_title: str
    company_summary: Optional[str] = ""
    value_proposition: str


class UpdateLeadRequest(BaseModel):
    email: Optional[str] = None
    contact_name: Optional[str] = None
    personalized_subject: Optional[str] = None
    personalized_body: Optional[str] = None
    status: Optional[str] = None


@router.get("")
async def list_leads(campaign_id: Optional[int] = None, limit: int = 50):
    conn = await get_db_connection()
    if campaign_id:
        cursor = await conn.execute("SELECT * FROM leads WHERE campaign_id = ? ORDER BY id DESC LIMIT ?", (campaign_id, limit))
    else:
        cursor = await conn.execute("SELECT * FROM leads ORDER BY id DESC LIMIT ?", (limit,))
    rows = await cursor.fetchall()
    await conn.close()
    return [dict(r) for r in rows]


@router.post("/verify")
async def verify_email_endpoint(req: VerifyEmailRequest):
    return await verify_email_smtp(req.email)


@router.post("/enrich-email")
async def enrich_email_endpoint(req: EnrichEmailRequest):
    return await enrich_person_email(req.first_name, req.last_name, req.company_domain)


@router.get("/suppression")
async def list_suppression():
    conn = await get_db_connection()
    cur = await conn.execute("SELECT * FROM suppression_list ORDER BY id DESC")
    rows = [dict(r) for r in await cur.fetchall()]
    await conn.close()
    return rows


@router.post("/suppression")
async def add_suppression(req: SuppressionCreateRequest):
    val = req.value.strip().lower().lstrip("@")
    if not val:
        raise HTTPException(status_code=400, detail="Value cannot be empty")
    conn = await get_db_connection()
    await conn.execute(
        "INSERT OR IGNORE INTO suppression_list (entry_type, value, reason) VALUES (?, ?, ?)",
        (req.entry_type, val, req.reason or "manual"),
    )
    await conn.commit()
    await conn.close()
    return {"ok": True, "entry_type": req.entry_type, "value": val}


@router.delete("/suppression/{entry_id}")
async def remove_suppression(entry_id: int):
    conn = await get_db_connection()
    await conn.execute("DELETE FROM suppression_list WHERE id = ?", (entry_id,))
    await conn.commit()
    await conn.close()
    return {"ok": True}


@router.post("/generate-copy")
async def generate_copy_endpoint(req: GenerateCopyRequest):
    return await generate_cold_email(
        company_name=req.company_name,
        company_domain=req.company_domain,
        contact_name=req.contact_name,
        job_title=req.job_title,
        company_summary=req.company_summary or "",
        value_proposition=req.value_proposition
    )


@router.put("/{lead_id}")
async def update_lead(lead_id: int, req: UpdateLeadRequest):
    conn = await get_db_connection()
    cursor = await conn.execute("SELECT id FROM leads WHERE id = ?", (lead_id,))
    if not await cursor.fetchone():
        await conn.close()
        raise HTTPException(status_code=404, detail="Lead not found")

    updates = []
    params = []
    if req.email is not None:
        updates.append("email = ?")
        params.append(req.email)
    if req.contact_name is not None:
        updates.append("contact_name = ?")
        params.append(req.contact_name)
    if req.personalized_subject is not None:
        updates.append("personalized_subject = ?")
        params.append(req.personalized_subject)
    if req.personalized_body is not None:
        updates.append("personalized_body = ?")
        params.append(req.personalized_body)
    if req.status is not None:
        updates.append("status = ?")
        params.append(req.status)

    if updates:
        params.append(lead_id)
        await conn.execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ?", tuple(params))
        await conn.commit()

    await conn.close()
    return {"ok": True}


class LeadIdsRequest(BaseModel):
    lead_ids: List[int]


async def delete_leads(lead_ids: List[int]) -> int:
    """Deletes leads. Their companies stay in the pool, marked deleted, so no campaign
    picks them up again; campaign counters are recomputed."""
    ids = list(dict.fromkeys(int(i) for i in lead_ids))
    if not ids:
        return 0
    marks = ",".join("?" for _ in ids)
    conn = await get_db_connection()
    try:
        cur = await conn.execute(f"SELECT DISTINCT campaign_id FROM leads WHERE id IN ({marks})", ids)
        cids = [r["campaign_id"] for r in await cur.fetchall() if r["campaign_id"] is not None]
        await conn.execute(
            f"UPDATE campaign_companies SET status = 'skipped', skip_reason = ?, lead_id = NULL "
            f"WHERE lead_id IN ({marks})", (DELETED_BY_USER, *ids))
        cur = await conn.execute(f"DELETE FROM leads WHERE id IN ({marks})", ids)
        deleted = cur.rowcount
        for cid in cids:
            await conn.execute(
                "UPDATE campaigns SET total_leads = (SELECT COUNT(*) FROM leads WHERE campaign_id = ?) "
                "WHERE id = ?", (cid, cid))
        await conn.commit()
    finally:
        await conn.close()
    for cid in cids:
        await refresh_pool_counters(cid)
    return deleted


@router.post("/bulk-delete")
async def bulk_delete_leads(req: LeadIdsRequest):
    return {"deleted": await delete_leads(req.lead_ids)}


@router.post("/bulk-draft")
async def bulk_draft_leads(req: LeadIdsRequest, background_tasks: BackgroundTasks):
    """Drafts emails for the selected leads that are still undrafted, in the background."""
    ids = await draftable_lead_ids(req.lead_ids)
    if ids:
        background_tasks.add_task(draft_leads, ids)
    return {"queued": len(ids)}


@router.delete("/{lead_id}")
async def delete_lead(lead_id: int):
    await delete_leads([lead_id])
    return {"ok": True}

