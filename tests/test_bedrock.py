"""The Bedrock planner, driven by a stub Converse client (no AWS account needed for the test)."""
import asyncio
import copy
from types import SimpleNamespace

from alexa_mcp_sim.bedrock import BedrockPlanner, tool_specs


class StubBedrock:
    def __init__(self):
        self.calls = []

    def converse(self, **kw):
        self.calls.append(copy.deepcopy(kw))  # boto3 serializes at call time
        if len(self.calls) == 1:
            return {"stopReason": "tool_use", "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "t1", "name": "care_briefing", "input": {"speaker": "Sara"}}}]}}}
        return {"stopReason": "end_turn", "output": {"message": {"role": "assistant", "content": [{"text": "Good morning, Sara."}]}}}


def test_tool_specs_shape():
    t = SimpleNamespace(name="x", description="does x", input_schema={"type": "object", "properties": {}})
    assert tool_specs([t]) == [{"toolSpec": {"name": "x", "description": "does x",
                                             "inputSchema": {"json": {"type": "object", "properties": {}}}}}]


def test_converse_tool_loop():
    stub = StubBedrock()
    planner = BedrockPlanner(client=stub, model_id="amazon.nova-pro-v1:0")
    seen = []

    async def call_tool(name, args):
        seen.append((name, args))
        return {"tool": name, "args": args, "is_error": False, "text": "ok", "structured": {"speech": "Good morning, Sara."}}

    tools = [SimpleNamespace(name="care_briefing", description="briefing", input_schema={"type": "object"})]
    history = []
    speech, calls = asyncio.run(planner.run("good morning", "Sara", "instr", tools, history, call_tool))
    assert speech == "Good morning, Sara." and seen == [("care_briefing", {"speaker": "Sara"})]
    second = stub.calls[1]["messages"]
    assert second[-1]["content"][0]["toolResult"]["toolUseId"] == "t1"
    assert "speaker is Sara" in stub.calls[0]["system"][0]["text"]
    assert stub.calls[0]["toolConfig"]["tools"][0]["toolSpec"]["name"] == "care_briefing"
    assert history  # conversation memory kept for the next turn


def test_safe_history_never_starts_with_orphan_tool_result():
    from alexa_mcp_sim.bedrock import safe_history
    msgs = [{"role": "user", "content": [{"text": "hi"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "a", "name": "x", "input": {}}}]},
            {"role": "user", "content": [{"toolResult": {"toolUseId": "a", "content": []}}]},
            {"role": "assistant", "content": [{"text": "done"}]}]
    assert safe_history(msgs, keep=3) == []
    assert safe_history(msgs, keep=4)[0]["content"][0]["text"] == "hi"
