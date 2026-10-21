"""The simulator web app: an Echo Show-style front end that is an MCP client of your server.

    alexa-mcp-sim --server http://127.0.0.1:8765/mcp --model examples/careloop.json
    open http://127.0.0.1:8787
"""
from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from importlib import resources
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .planner import Conversation, InteractionModelPlanner


class Simulator:
    def __init__(self, server_url: str, model: InteractionModelPlanner | None = None, token: str | None = None,
                 planner: str = "auto", bedrock: Any = None):
        self.server_url = server_url
        self.model = model
        self.token = token
        self.sessions: dict[str, Conversation] = {}
        self.bedrock = bedrock
        if planner == "bedrock" or (planner == "auto" and bedrock is None and model is None):
            from .bedrock import BedrockPlanner

            self.bedrock = BedrockPlanner()
        self.planner = "bedrock" if self.bedrock is not None and planner != "model" else "interaction-model"

    def client(self) -> Client:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        transport = streamable_http_client(self.server_url, http_client=httpx2.AsyncClient(headers=headers, timeout=30))
        return Client(transport)

    def convo(self, sid: str) -> Conversation:
        return self.sessions.setdefault(sid, Conversation())

    async def info(self) -> dict[str, Any]:
        async with self.client() as c:
            tools = (await c.list_tools()).tools
            return {"server": c.server_info.model_dump() if c.server_info else None, "protocol": c.protocol_version,
                    "planner": self.planner, "instructions": c.instructions,
                    "tools": [{"name": t.name, "title": t.title, "description": t.description,
                               "ui": (t.meta or {}).get("ui", {}).get("resourceUri")} for t in tools]}

    async def tap(self, sid: str, tool: str, args: dict[str, Any], speaker: str) -> dict[str, Any]:
        return await _tap(self, sid, tool, args, speaker)

    async def utter(self, sid: str, text: str, speaker: str) -> dict[str, Any]:
        convo = self.convo(sid)
        convo.speaker = speaker
        started = time.perf_counter()
        trace: list[dict[str, Any]] = []
        ui = None
        async with self.client() as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}

            async def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
                nonlocal ui
                t0 = time.perf_counter()
                res = await c.call_tool(name, args)
                texts = [b.text for b in res.content if getattr(b, "type", "") == "text"]
                entry = {"tool": name, "args": args, "is_error": bool(res.is_error), "text": texts[0] if texts else "",
                         "structured": res.structured_content, "ms": round((time.perf_counter() - t0) * 1000)}
                trace.append(entry)
                if not res.is_error:
                    convo.remember(name, res.structured_content)
                    uri = ((tools[name].meta or {}).get("ui") or {}).get("resourceUri") if name in tools else None
                    if uri:
                        html = (await c.read_resource(uri)).contents[0].text
                        ui = {"uri": uri, "html": html, "result": {"content": [{"type": "text", "text": entry["text"]}],
                                                                    "structuredContent": res.structured_content}}
                return entry

            if self.planner == "bedrock":
                speech, _ = await self.bedrock.run(text, speaker, c.instructions or "", list(tools.values()),
                                                   convo.history, call)
            else:
                plan = self.model.plan(text, convo)
                if plan.say is not None:
                    speech = plan.say
                else:
                    spoken = []
                    for step in plan.calls:
                        out = await call(step.tool, step.args)
                        s = (out["structured"] or {}).get("speech") if isinstance(out["structured"], dict) else None
                        spoken.append(s or out["text"])
                    speech = " ".join(spoken)
        return {"speech": speech, "trace": trace, "ui": ui, "planner": self.planner,
                "ms": round((time.perf_counter() - started) * 1000)}


async def _tap(sim: "Simulator", sid: str, tool: str, args: dict[str, Any], speaker: str) -> dict[str, Any]:
    """A touch on a card: the MCP App asked the host to call a tool (MCP Apps tools/call)."""
    convo = sim.convo(sid)
    started = time.perf_counter()
    async with sim.client() as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        if tool not in tools:
            raise ValueError(f"unknown tool {tool}")
        props = (tools[tool].input_schema or {}).get("properties", {})
        if "speaker" in props and not args.get("speaker") and speaker:
            args = args | {"speaker": speaker}  # the host knows who is in front of the screen (voice ID)
        t0 = time.perf_counter()
        res = await c.call_tool(tool, args)
        texts = [b.text for b in res.content if getattr(b, "type", "") == "text"]
        entry = {"tool": tool, "args": args, "is_error": bool(res.is_error), "text": texts[0] if texts else "",
                 "structured": res.structured_content, "ms": round((time.perf_counter() - t0) * 1000), "via": "touch"}
        ui = None
        if not res.is_error:
            convo.remember(tool, res.structured_content)
            uri = ((tools[tool].meta or {}).get("ui") or {}).get("resourceUri")
            if uri:
                html = (await c.read_resource(uri)).contents[0].text
                ui = {"uri": uri, "html": html, "result": {"content": [{"type": "text", "text": entry["text"]}],
                                                            "structuredContent": res.structured_content}}
    sc = res.structured_content if isinstance(res.structured_content, dict) else {}
    return {"speech": sc.get("speech") or entry["text"], "trace": [entry], "ui": ui, "planner": "touch",
            "result": {"content": [{"type": "text", "text": entry["text"]}], "structuredContent": res.structured_content,
                       "isError": bool(res.is_error)},
            "ms": round((time.perf_counter() - started) * 1000)}


def create_app(sim: Simulator, members: list[str] | None = None) -> Starlette:
    page = resources.files("alexa_mcp_sim").joinpath("static/index.html").read_text(encoding="utf-8")

    async def index(_: Request) -> HTMLResponse:
        return HTMLResponse(page)

    async def info(_: Request) -> JSONResponse:
        try:
            data = await sim.info()
        except Exception as e:  # show connection problems in the UI instead of a stack trace
            return JSONResponse({"error": f"Can't reach the MCP server at {sim.server_url}: {e}"}, status_code=502)
        data["members"] = members or []
        data["samples"] = [s for i in (sim.model.raw["intents"] if sim.model else []) for s in i.get("show", [])]
        return JSONResponse(data)

    async def utter(req: Request) -> JSONResponse:
        body = await req.json()
        text = str(body.get("text", "")).strip()
        if not text:
            return JSONResponse({"error": "empty utterance"}, status_code=400)
        sid = str(body.get("session") or uuid.uuid4())
        try:
            out = await sim.utter(sid, text[:500], str(body.get("speaker", ""))[:64])
        except Exception as e:
            return JSONResponse({"error": str(e), "speech": "Sorry, something went wrong talking to the server."}, status_code=502)
        return JSONResponse(out | {"session": sid})

    async def tap(req: Request) -> JSONResponse:
        body = await req.json()
        tool = str(body.get("tool", ""))
        args = body.get("args") or {}
        if not tool or not isinstance(args, dict):
            return JSONResponse({"error": "tool and args are required"}, status_code=400)
        sid = str(body.get("session") or uuid.uuid4())
        try:
            out = await sim.tap(sid, tool, args, str(body.get("speaker", ""))[:64])
        except Exception as e:
            return JSONResponse({"error": str(e), "speech": "Sorry, that didn't work."}, status_code=502)
        return JSONResponse(out | {"session": sid})

    return Starlette(routes=[Route("/", index), Route("/api/info", info), Route("/api/utter", utter, methods=["POST"]),
                             Route("/api/tap", tap, methods=["POST"])])


def resolve_model(name: str) -> str:
    """A path to an interaction model JSON, or the name of a bundled one (e.g. 'careloop')."""
    if os.path.isfile(name):
        return name
    bundled = resources.files("alexa_mcp_sim").joinpath(f"models/{name}.json")
    if bundled.is_file():
        return str(bundled)
    raise SystemExit(f"No interaction model at {name!r} (bundled: careloop)")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="alexa-mcp-sim", description="Echo Show-style voice simulator for MCP servers")
    p.add_argument("--server", default=os.environ.get("SIM_MCP_URL", "http://127.0.0.1:8765/mcp"))
    p.add_argument("--model", help="interaction model JSON, or a bundled name such as 'careloop' (offline planner)")
    p.add_argument("--planner", choices=["auto", "model", "bedrock"], default=os.environ.get("SIM_PLANNER", "auto"))
    p.add_argument("--token", default=os.environ.get("SIM_MCP_TOKEN"))
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    a = p.parse_args(argv)
    model = InteractionModelPlanner.from_file(resolve_model(a.model)) if a.model else None
    members = model.raw.get("members", []) if model else []
    sim = Simulator(a.server, model, a.token, a.planner)
    import uvicorn

    print(f"alexa-mcp-sim on http://{a.host}:{a.port}  ->  MCP server {a.server}  (planner: {sim.planner})")
    uvicorn.run(create_app(sim, members), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
