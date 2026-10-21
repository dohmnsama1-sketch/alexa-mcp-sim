"""Simulator <-> CareLoop over real Streamable HTTP: a whole morning, as in the demo video."""
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from starlette.testclient import TestClient

careloop_seed = pytest.importorskip("careloop.seed")

from alexa_mcp_sim.app import Simulator, create_app  # noqa: E402
from alexa_mcp_sim.planner import InteractionModelPlanner  # noqa: E402

MODEL = Path(__file__).resolve().parents[1] / "examples" / "careloop.json"


def _port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def careloop_url(tmp_path):
    data = tmp_path / "h.json"
    data.write_text(json.dumps(careloop_seed.build(datetime(2026, 10, 23, 8, 5, tzinfo=ZoneInfo(careloop_seed.TZ)))))
    port = _port()
    env = {**os.environ, "CARELOOP_NOW": "2026-10-23T08:05"}
    env.pop("CARELOOP_TOKEN", None)
    proc = subprocess.Popen([sys.executable, "-m", "careloop.server", "--port", str(port), "--data", str(data)],
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5)
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    yield f"http://127.0.0.1:{port}/mcp"
    proc.terminate()
    proc.wait(5)


def test_demo_morning(careloop_url):
    model = InteractionModelPlanner.from_file(MODEL)
    app = create_app(Simulator(careloop_url, model, planner="model"), model.raw["members"])
    web = TestClient(app)
    info = web.get("/api/info").json()
    assert info["server"]["name"] == "careloop" and info["protocol"] == "2026-07-28" and len(info["tools"]) == 17
    assert "Alexa, good morning" in info["samples"]
    assert "alexa-mcp-sim" in web.get("/").text

    def say(text, speaker, sid="s1"):
        r = web.post("/api/utter", json={"text": text, "speaker": speaker, "session": sid})
        assert r.status_code == 200, r.text
        return r.json()

    b = say("Alexa, good morning", "Sara")
    assert b["speech"].startswith("Good morning, Sara.") and b["ui"]["uri"] == "ui://careloop/briefing.html"
    assert b["ui"]["result"]["structuredContent"]["recipient"] == "Nora"
    assert "Taken" not in b["speech"]
    assert "Got it" in say("Mom took her blood pressure pill", "Sara")["speech"]
    assert "Hold on" in say("Mom took her blood pressure pill", "Omar", sid="s2")["speech"]
    q = say("Order more of her blood pressure pills", "Sara")
    code = q["trace"][0]["structured"]["quote"]["code"]
    assert q["ui"]["uri"] == "ui://careloop/order.html"
    assert "code" in say("yes", "Sara")["speech"]
    o = say("Confirm order " + " ".join(code), "Sara")
    assert o["speech"].startswith("Ordered.") and o["ui"]["result"]["structuredContent"]["order"]["simulated"]
    assert "Saved" in say("Tell Omar Mom wants to see the wedding photos tonight", "Sara")["speech"]
    assert "emergency" in say("I feel a little dizzy", "Nora", sid="s3")["speech"]
    al = say("Any alerts?", "Omar", sid="s2")
    assert "dizzy" in al["speech"]
    assert "Marked as handled by Omar" in say("I'm on it", "Omar", sid="s2")["speech"]
    assert "wedding photos" in say("What did I miss?", "Omar", sid="s2")["speech"]
    a = say("How has Mom done with her meds this week?", "Sara")
    assert a["ui"]["uri"] == "ui://careloop/adherence.html" and "percent" in a["speech"]
    r = say("How did we do as a family this week?", "Sara")
    assert r["ui"]["uri"] == "ui://careloop/report.html"
    m = r["ui"]["result"]["structuredContent"]["metrics"]
    assert m["double_doses_prevented"] == 1 and m["refills_before_running_out"] == 1 and m["alerts_acknowledged"] == 1
    # touch on the Echo Show card: the MCP App asks the host to call a tool, the host fills in the speaker
    t = web.post("/api/tap", json={"tool": "meds_due", "args": {}, "speaker": "Leila", "session": "s4"}).json()
    assert t["planner"] == "touch" and t["trace"][0]["via"] == "touch" and t["ui"]["uri"] == "ui://careloop/doses.html"
    t = web.post("/api/tap", json={"tool": "dose_log", "args": {"medication": "Vitamin D3", "status": "taken"},
                                   "speaker": "Leila", "session": "s4"}).json()
    assert t["trace"][0]["args"]["speaker"] == "Leila" and "Got it" in t["speech"] and not t["result"]["isError"]
    assert web.post("/api/tap", json={"tool": "nope", "args": {}}).status_code == 502
    assert web.post("/api/tap", json={"args": {}}).status_code == 400
    err = say("Bob took his lisinopril", "Bob", sid="s9")
    assert err["trace"][0]["is_error"] and "don't know Bob" in err["speech"]


def test_unreachable_server_is_reported():
    model = InteractionModelPlanner.from_file(MODEL)
    web = TestClient(create_app(Simulator(f"http://127.0.0.1:{_port()}/mcp", model, planner="model")))
    r = web.get("/api/info")
    assert r.status_code == 502 and "Can't reach" in r.json()["error"]
