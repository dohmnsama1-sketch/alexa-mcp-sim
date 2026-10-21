"""Amazon Bedrock planner: free-form requests via the Converse API with tool use.

The MCP server's tools are offered to the model as Bedrock tool specs; each `toolUse` block is executed
against the MCP server and returned as a `toolResult`, until the model answers. Needs AWS credentials
(standard boto3 chain) and model access in the chosen Region. Env: SIM_BEDROCK_MODEL (default
amazon.nova-pro-v1:0), AWS_REGION (default us-east-1).
"""
from __future__ import annotations

import json
import os
from typing import Any, Awaitable, Callable

ToolRunner = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

SYSTEM = ("You are Alexa+, a voice assistant on an Echo Show. The current speaker is {speaker}. "
          "Use the tools to answer. Always pass speaker='{speaker}' when a tool takes a speaker. Reply in one to "
          "three short spoken sentences; when a tool result has a 'speech' field, prefer saying it as written. "
          "Never place an order unless the user has just repeated the confirmation code.\n\nServer instructions:\n{instructions}")


def tool_specs(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    return [{"toolSpec": {"name": t.name, "description": (t.description or t.name)[:4000],
                          "inputSchema": {"json": t.input_schema}}} for t in mcp_tools]


def safe_history(messages: list[dict[str, Any]], keep: int = 8) -> list[dict[str, Any]]:
    """Last few messages, starting at a plain user turn: Converse rejects a toolResult whose toolUse was cut off."""
    tail = messages[-keep:]
    while tail and not (tail[0]["role"] == "user" and any("text" in b for b in tail[0]["content"])):
        tail = tail[1:]
    return tail


class BedrockPlanner:
    name = "bedrock"

    def __init__(self, client: Any = None, model_id: str | None = None, max_turns: int = 6):
        if client is None:
            try:
                import boto3  # optional dependency: pip install "alexa-mcp-sim[bedrock]"
            except ImportError as e:
                raise SystemExit("The Bedrock planner needs boto3: pip install 'alexa-mcp-sim[bedrock]', "
                                 "or pass --model <interaction-model.json> for the offline planner.") from e

            client = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self.client = client
        self.model_id = model_id or os.environ.get("SIM_BEDROCK_MODEL", "amazon.nova-pro-v1:0")
        self.max_turns = max_turns

    async def run(self, utterance: str, speaker: str, instructions: str, mcp_tools: list[Any],
                  history: list[dict[str, Any]], call_tool: ToolRunner) -> tuple[str, list[dict[str, Any]]]:
        messages = [*safe_history(history), {"role": "user", "content": [{"text": utterance}]}]
        calls: list[dict[str, Any]] = []
        config = {"tools": tool_specs(mcp_tools)}
        system = [{"text": SYSTEM.format(speaker=speaker, instructions=instructions or "")}]
        for _ in range(self.max_turns):
            resp = self.client.converse(modelId=self.model_id, system=system, messages=messages, toolConfig=config,
                                        inferenceConfig={"maxTokens": 400, "temperature": 0.2})
            msg = resp["output"]["message"]
            messages.append(msg)
            uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
            if resp.get("stopReason") != "tool_use" or not uses:
                text = " ".join(b["text"] for b in msg["content"] if "text" in b).strip()
                history[:] = safe_history(messages)
                return text, calls
            results = []
            for use in uses:
                out = await call_tool(use["name"], use.get("input") or {})
                calls.append(out)
                results.append({"toolResult": {"toolUseId": use["toolUseId"],
                                               "content": [{"text": json.dumps(out["structured"] or out["text"], default=str)[:8000]}],
                                               "status": "error" if out["is_error"] else "success"}})
            messages.append({"role": "user", "content": results})
        return "Sorry, that took too many steps.", calls
