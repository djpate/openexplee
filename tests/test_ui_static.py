import json
import pathlib
import re
import shutil
import subprocess

import pytest

PAGE = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")


def test_footer_attribution():
    assert "Business data: Overture Maps Foundation (CDLA-Permissive-2.0 / Apache-2.0 / CC0)" in HTML


def test_refresh_and_batch_controls():
    assert "refreshSearch(" in HTML
    assert 'id="camp-batch"' in HTML
    assert "refresh-search" in HTML


def test_plan_block_uses_live_pool_counts():
    assert "pool_done" in HTML and "pool_skipped" in HTML and "pool_pending" in HTML
    assert "escapeHtml(" in HTML


def test_inline_js_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", HTML, re.S)
    js = tmp_path / "page.js"
    js.write_text("\n;\n".join(blocks), encoding="utf-8")
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def js_function(name):
    """Source of a named function in the page script (brace-matched)."""
    m = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", HTML)
    assert m, f"function {name} not found"
    depth = 0
    for j in range(HTML.index("{", m.end()), len(HTML)):
        if HTML[j] == "{":
            depth += 1
        elif HTML[j] == "}":
            depth -= 1
            if depth == 0:
                return HTML[m.start():j + 1]
    raise AssertionError(f"unbalanced function {name}")


def run_js(tmp_path, names, expr):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    js = tmp_path / "run.js"
    js.write_text("\n".join(js_function(n) for n in names)
                  + f"\nprocess.stdout.write(JSON.stringify({expr}));", encoding="utf-8")
    r = subprocess.run([node, str(js)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# I1: errors and the searching line show on every card, plan or not.

def test_plan_block_status_lines_not_gated_on_plan():
    body = js_function("renderPlanBlock")
    assert "if (!plan) return ''" not in body
    gate = body.index("if (!plan)")
    assert body.index("escapeHtml(c.discovery_message)") < gate
    assert body.index("Searching Overture") < gate


def test_planless_card_renders_message_and_searching_line(tmp_path):
    out = run_js(tmp_path, ["escapeHtml", "renderPlanBlock"], """[
      renderPlanBlock({id: 1, status: 'discovering', search_plan: null, discovery_message: '<b>S3 down</b>'}),
      renderPlanBlock({id: 2, status: 'error', search_plan: null, discovery_message: 'No categories matched'}),
      renderPlanBlock({id: 3, status: 'draft', search_plan: null, discovery_message: null}),
    ]""")
    assert "&lt;b&gt;S3 down&lt;/b&gt;" in out[0] and "<b>" not in out[0]
    assert "Searching Overture" in out[0]
    assert "No categories matched" in out[1] and "Searching Overture" not in out[1]
    assert out[2].strip() == ""


# M2: plan-block buttons are disabled while sending.

def test_plan_block_buttons_disabled_while_sending(tmp_path):
    assert "'sending'" in js_function("renderPlanBlock")
    plan = "{categories: ['dental_clinic'], areas: [{name: 'Tampa', tier: 1}]}"
    out = run_js(tmp_path, ["escapeHtml", "renderPlanBlock"], f"""[
      renderPlanBlock({{id: 1, status: 'sending', display_status: 'sending', search_plan: {plan}}}),
      renderPlanBlock({{id: 2, status: 'ready', display_status: 'ready', search_plan: {plan}}}),
    ]""")
    assert out[0].count("disabled class=") == 2 and "Searching Overture" not in out[0]
    assert out[1].count("disabled class=") == 0


# I5: third-party strings are escaped before they reach innerHTML.

def test_campaign_card_and_table_escape_name_and_icp():
    assert "${c.name}" not in HTML and "${c.icp_prompt}" not in HTML
    for fn in ("loadCampaigns", "loadDashboard"):
        body = js_function(fn)
        assert "escapeHtml(c.name)" in body and "escapeHtml(c.icp_prompt)" in body


def test_leads_table_escapes_every_lead_field():
    assert "renderLeadRow(l, " in js_function("loadLeads")
    body = js_function("renderLeadRow")
    assert not re.search(r"\$\{l\.", body), "raw lead field interpolation"
    for field in ("company_name", "company_domain", "contact_name", "job_title", "email"):
        assert f"escapeHtml(l.{field}" in body
    assert "safeHttpUrl(l.website_url)" in body


def test_thread_list_escapes_every_thread_field():
    assert "renderThreadItem(" in js_function("renderMailboxThreads")
    body = js_function("renderThreadItem")
    assert not re.search(r"\$\{t\.", body), "raw thread field interpolation"
    for field in ("contact_name", "job_title", "company_name", "campaign_name"):
        assert f"escapeHtml(t.{field}" in body


def test_lead_row_renders_hostile_values_inert(tmp_path):
    out = run_js(tmp_path, ["escapeHtml", "safeHttpUrl", "renderLeadRow"], """[
      renderLeadRow({id: 7, company_name: '<img src=x onerror=alert(1)>', company_domain: 'evil.com',
                     website_url: 'javascript:alert(1)', contact_name: null, job_title: null,
                     email: "o'brien@x.com", verification_status: 'verified', status: 'enriched'}),
      renderLeadRow({id: 8, company_name: 'Tampa Dental', company_domain: 'tampadental.com',
                     website_url: 'https://tampadental.com/?a=1&b="2"', contact_name: 'Ann Lee',
                     job_title: 'Owner', email: 'ann@tampadental.com', status: 'sent'}),
    ]""")
    hostile, ok = out
    assert "<img" not in hostile and "&lt;img src=x onerror=alert(1)&gt;" in hostile
    assert "href=" not in hostile and "javascript:" not in hostile and "evil.com" in hostile
    assert "null" not in hostile and "—" in hostile
    assert "o&#39;brien@x.com" in hostile and "handleVerifySingleLead(7, &quot;o&#39;brien@x.com&quot;)" in hostile
    assert 'href="https://tampadental.com/?a=1&amp;b=&quot;2&quot;"' in ok
    assert "Ann Lee" in ok and "Owner" in ok


def test_thread_item_renders_hostile_values_inert(tmp_path):
    out = run_js(tmp_path, ["escapeHtml", "renderThreadItem"], """
      renderThreadItem({id: 3, contact_name: '<script>x</script>', job_title: null,
                        company_name: '<b>Co</b>', subject: 's', snippet: '<i>hi</i>',
                        campaign_name: '<u>C</u>', followup_step: 2}, false)""")
    assert "<script>" not in out and "&lt;script&gt;" in out
    assert "<b>" not in out and "<i>" not in out and "<u>" not in out
    assert "null" not in out and "selectThread(3)" in out


def test_toast_escapes_message():
    assert "escapeHtml(message)" in js_function("showToast")


# Lazy drafting: selection, bulk actions, campaign draft button.
def test_leads_table_has_selection_and_bulk_actions():
    for s in ('id="leads-select-all"', 'id="leads-bulk-bar"', "bulkDraftLeads(", "bulkDeleteLeads(",
              "/api/leads/bulk-draft", "/api/leads/bulk-delete"):
        assert s in HTML, s


def test_lead_row_checkbox_reflects_selection_and_shows_undrafted(tmp_path):
    on, off = run_js(tmp_path, ["escapeHtml", "safeHttpUrl", "renderLeadRow"], """[
      renderLeadRow({id: 5, company_domain: 'a.com', status: 'enriched'}, true),
      renderLeadRow({id: 6, company_domain: 'b.com', status: 'enriched'}, false)]""")
    assert 'class="lead-select' in on and 'data-id="5"' in on and '" checked>' in on
    assert 'data-id="6"' in off and '" checked>' not in off
    assert "Not drafted yet" in off


def test_campaign_draft_button_states(tmp_path):
    idle, busy, none = run_js(tmp_path, ["escapeHtml", "renderDraftButton"], """[
      renderDraftButton({id: 3, undrafted_count: 12, drafting_count: 0}),
      renderDraftButton({id: 3, undrafted_count: 12, drafting_count: 4}),
      renderDraftButton({id: 3, undrafted_count: 0, drafting_count: 0})]""")
    assert "draftCampaignEmails(3, 12)" in idle and "Draft 12 emails" in idle
    assert "Drafting 4" in busy and "draftCampaignEmails" not in busy
    assert none == ""


def test_auto_poll_keeps_refreshing_while_drafting():
    assert "drafting_count" in js_function("startAutoPoll")


def test_settings_page_lets_user_pick_both_claude_models():
    assert 'id="settings-anthropic-model"' in HTML and 'id="settings-extraction-model"' in HTML
    load, save = js_function("loadSettings"), js_function("handleSaveSettings")
    for field, el in (("anthropic_model", "settings-anthropic-model"),
                      ("anthropic_extraction_model", "settings-extraction-model")):
        assert f"data.{field}" in load and el in load
        assert f"{field}:" in save and el in save


# Draft viewer: read and edit the full draft.
def test_draft_viewer_controls_exist_and_use_values_not_html():
    for s in ('id="draft-modal"', 'id="draft-subject"', 'id="draft-body"', 'id="draft-meta"',
              "saveDraft(", "closeDraft(", "draftNow("):
        assert s in HTML, s
    opener = js_function("openDraft")
    assert ".value" in opener and "innerHTML" not in opener  # draft text is LLM output: never HTML
    save = js_function("saveDraft")
    assert "/api/leads/" in save and "'PUT'" in save
    assert "personalized_subject" in save and "personalized_body" in save


def test_lead_row_opens_the_full_draft(tmp_path):
    out = run_js(tmp_path, ["escapeHtml", "safeHttpUrl", "renderLeadRow"],
                 "renderLeadRow({id: 9, company_domain: 'a.com', status: 'enriched', "
                 "personalized_subject: 's', personalized_body: 'b'})")
    assert "openDraft(9)" in out
