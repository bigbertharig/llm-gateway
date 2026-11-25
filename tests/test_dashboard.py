"""The dashboard (docs/DASHBOARD.md): page, rig API and overrides, model/benchmark summaries."""

from __future__ import annotations

import json

from llm_gateway.dashboard import sources
import pytest
from fastapi.testclient import TestClient
from llm_gateway.app import create_app
from llm_gateway.settings import Settings
from runtime_fixture import fake_owner

@pytest.fixture
def menv():
    owner = fake_owner()
    with TestClient(create_app(Settings.from_env(), orchestration=owner)) as client:
        yield {'client': client, 'owner': owner}



def test_page_and_rig_status(menv):  # noqa: F811
    c = menv["client"]
    page = c.get("/dashboard/")
    assert page.status_code == 200 and "Rig dashboard" in page.text and "/dashboard/api/" in page.text
    assert 'id="dashboard-refresh"' in page.text
    assert 'data-tab="cloud"' in page.text and 'id="tab-cloud"' in page.text
    assert 'id="cloud-openrouter-form"' in page.text and 'id="cloud-claude-form"' in page.text
    assert 'id="cloud-openrouter-select-visible"' in page.text
    assert 'id="cloud-openrouter-clear-visible"' in page.text
    assert 'id="cloud-openrouter-selection-count"' in page.text
    assert 'class="cloud-card cloud-card-full"' in page.text
    assert 'className = "cloud-model-table"' in page.text
    assert '"Input $ / 1M"' in page.text and '"Output $ / 1M"' in page.text
    assert '"release_year", "Released"' in page.text
    assert 'id="cloud-codex-api-form"' in page.text
    assert 'id="cloud-claude-code-login"' in page.text
    assert 'id="cloud-claude-code-complete"' in page.text
    assert 'Codex (Subscription)' in page.text and 'Codex (API)' in page.text
    assert 'Claude (Subscription)' in page.text and 'Claude (API)' in page.text
    assert 'type="password"' in page.text and 'Leave blank to keep the stored key' in page.text
    assert '$("#dashboard-refresh").onclick = () => refresh(true)' in page.text
    assert '" · last updated " + localClock(last[refreshTab])' in page.text
    assert "if (refreshInFlight)" in page.text and "refreshQueuedNow = refreshQueuedNow || now" in page.text
    assert page.text.count('id="chat-context-meter"') == 1
    assert "Updating through the current tool/model step." in page.text
    assert 'api("chat/options")' in page.text
    assert "new EventSource" in page.text
    assert 'assistant · progress' in page.text
    assert '.chat-message.assistant { max-width: 100%; margin-left: 0; color: #fff; }' in page.text
    assert 'id="chat-toggle-tools"' in page.text and "desktop-show-tools" in page.text
    assert "chatToolDisclosure" in page.text and 'document.createElement("details")' in page.text
    assert "chat-tool-disclosure" in page.text and "Show earlier activity (" in page.text
    assert "chat-compaction-marker" in page.text and 'started.textContent = "Compaction started"' in page.text
    assert 'finished.textContent = "Compaction finished"' in page.text and 'className = "compaction-rule"' in page.text
    assert "color: var(--warm)" in page.text and "background: var(--warm)" in page.text
    assert "flushTranscriptHistory" in page.text and "historicalAt <= eventAt" in page.text
    assert 'showChatCommandOutput("Codex context compaction finished.")' in page.text
    assert 'event.method === "thread/compacted"' in page.text and "pendingCodexCompaction" in page.text
    assert "chatVisibleToolRows += 80" in page.text
    assert "deliverCodexAtNextBoundary" in page.text and "error.status = res.status" in page.text
    assert 'error.status !== 409' in page.text and 'steer = !steer' in page.text
    assert 'aria-valuetext", "Context usage pending"' in page.text
    assert 'windowDurationMins === 300' in page.text
    assert 'remaining <= 10 ? "bad" : remaining <= 25 ? "warn"' in page.text
    assert 'id="chat-limit-alert"' in page.text
    assert "renderChatMarkdown" in page.text and "Attach images" in page.text and "Enter sends" in page.text
    assert "historical_turns" in page.text and "renderHistoricalTurn" in page.text
    assert 'name === "coding" ? "Start new coding conversation" : "Start new chat"' in page.text
    # Transcript/capacity polling must not replace an in-progress composer draft.
    assert "const refreshingDisplayedChat = sameChat && displayedChatId === id" in page.text
    assert "clearChatView({preserveComposer})" in page.text
    assert "chatListOpening" in page.text
    assert "showChatModeLoading" in page.text and '$("#chat-tabs").replaceChildren()' in page.text
    assert 'expectedMode !== (tab === "coding" ? "coding" : "chat")' in page.text
    assert '(c.mode || "chat") !== expectedMode' in page.text
    assert "refreshQueued" in page.text and "Promise.resolve().then(() => refresh(rerunNow))" in page.text
    assert "due(refreshTab)" in page.text and "last[refreshTab] = Date.now()" in page.text
    assert '#tab-chat { --panel: #211631' in page.text
    assert 'nav button[data-tab="chat"][aria-selected="true"]' in page.text
    assert "stopActiveCodexTurn" in page.text
    assert 'event.key !== "Escape"' in page.text and 'tab === "chat" || tab === "coding"' in page.text
    assert c.get("/dashboard", follow_redirects=False).status_code in (301, 302, 307)
    rig = c.get("/dashboard/api/rig").json()
    assert rig["gpus"][0]["name"] == "fixture"


def test_overrides_are_requests_to_orchestration(menv):
    c = menv['client']
    response = c.post('/dashboard/api/rig/kill', json={'gpu_ids': '2', 'reason': 'test'})
    assert response.json() == {'killed': [2]}
    request = next(call for call in menv['owner'].calls if call['operation'] == 'manager.kill')
    assert request['arguments'] == {'args': [[2], 'test'], 'kwargs': {}}
    assert c.post('/dashboard/api/rig/kill', json={'gpu_ids': []}).status_code == 400


def test_model_evidence_is_projected_from_owner(menv):
    c = menv['client']
    assert c.get('/dashboard/api/models').json()['models'][0]['model'] == 'fixture'
    assert c.get('/dashboard/api/benchmarks').json()['source'] == 'orchestration_model_catalog'


def test_retired_queue_routes_are_absent(menv):  # noqa: F811
    client = menv["client"]
    page = client.get("/dashboard/").text
    assert 'data-tab="orchestration"' not in page
    assert 'api("orchestration' not in page
    assert client.get("/dashboard/api/rig").status_code == 200
    assert client.get("/dashboard/api/orchestration").status_code == 404
    assert client.post("/dashboard/api/orchestration/kill_plan", json={}).status_code == 404


def test_saved_activity_uses_surviving_event_time_on_desktop_and_mobile():
    """A completed tool belongs after progress emitted while it was running."""
    from pathlib import Path
    import subprocess
    static = Path(__file__).resolve().parents[1] / "src/llm_gateway/dashboard/static"
    desktop = (static / "index.html").read_text()
    mobile = (static / "mobile.html").read_text()
    activity = desktop[desktop.index("function renderChatActivity("):desktop.index("function codexActivityLine(")]
    timeline = mobile[mobile.index("const timeline=(events,"):mobile.index("  const historical=", mobile.index("const timeline=(events,"))]
    events = [
        {"method": "item/started", "received_at": "2026-11-24T12:00:01Z",
         "item": {"id": "tool-1", "type": "commandExecution", "command": "read file"}},
        {"method": "item/completed", "received_at": "2026-11-24T12:00:02Z",
         "item": {"id": "progress-1", "type": "agentMessage", "phase": "commentary", "text": "Checking"}},
        {"method": "item/completed", "received_at": "2026-11-24T12:00:03Z",
         "item": {"id": "tool-1", "type": "commandExecution", "command": "read file"}},
    ]
    script = """
const element = () => ({dataset: {}, append() {}, replaceChildren() {}, setAttribute() {}, classList: {add() {}}});
const document = {createElement: element};
const renderChatMarkdown = () => {};
const chatToolDisclosure = () => element();
const loadDuration = String;
const target = element();
""" + activity + "\nconst events = " + json.dumps(events) + ";\n" + """
renderChatActivity(target, events);
const desktopOrder = JSON.parse(target.dataset.signature).map(event => event.item.id);
const mobileOrder = [];
const activityText = event => event.item.id;
const add = text => mobileOrder.push(text);
const addTool = text => mobileOrder.push(text);
const addCompaction = () => mobileOrder.push('compacted');
""" + timeline + "\ntimeline(events); console.log(JSON.stringify({desktopOrder, mobileOrder}));"
    result = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
    assert json.loads(result.stdout) == {
        "desktopOrder": ["progress-1", "tool-1"],
        "mobileOrder": ["progress-1", "tool-1"],
    }
