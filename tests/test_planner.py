from pathlib import Path

import pytest

from alexa_mcp_sim.planner import Conversation, InteractionModelPlanner, normalize

MODEL = Path(__file__).resolve().parents[1] / "examples" / "careloop.json"


@pytest.fixture
def planner():
    return InteractionModelPlanner.from_file(MODEL)


def plan(p, text, convo=None):
    return p.plan(text, convo or Conversation(speaker="Sara"))


def test_normalize_strips_wake_word_and_punctuation():
    assert normalize("Alexa, good morning!") == "good morning"
    assert normalize("Hey Alexa  What's due?") == "What's due"


@pytest.mark.parametrize("text,tool", [
    ("Alexa, good morning", "care_briefing"), ("What's due for Mom?", "meds_due"),
    ("Mom took her blood pressure pill", "dose_log"), ("How has Mom done with her meds this week?", "adherence_report"),
    ("Order more of her blood pressure pills", "refill_quote"), ("Confirm order T 7 7 D", "refill_confirm"),
    ("What did I miss?", "whats_new"), ("Any alerts?", "alerts_list"), ("Who's driving Mom tomorrow", "agenda"),
])
def test_routes_utterances_to_tools(planner, text, tool):
    assert plan(planner, text).calls[0].tool == tool


def test_slots_keep_original_case(planner):
    call = plan(planner, "Tell Omar Mom wants to see the wedding photos tonight").calls[0]
    assert call.args == {"speaker": "Sara", "text": "For Omar: Mom wants to see the wedding photos tonight"}


def test_specific_sample_beats_generic(planner):
    assert plan(planner, "Mom skipped her metformin").calls[0].args["status"] == "skipped"
    assert plan(planner, "Mom took her metformin").calls[0].args["medication"] == "metformin"


def test_yes_never_buys_without_the_code(planner):
    p = plan(planner, "yes order it")
    assert p.calls == [] and "code" in p.say


def test_context_from_previous_results(planner):
    convo = Conversation(speaker="Omar")
    assert plan(planner, "I'm on it", convo).say.startswith("Ask me about alerts")
    convo.remember("alerts_list", {"alerts": [{"id": "alert_7"}]})
    assert plan(planner, "I'm on it", convo).calls[0].args == {"speaker": "Omar", "alert_id": "alert_7"}
    convo.remember("dose_log", {"logged": False, "previous": {"med_id": "med_1"}})
    call = plan(planner, "confirm extra dose", convo).calls[0]
    assert call.args["medication"] == "med_1" and call.args["confirm_extra_dose"] is True


def test_fallback(planner):
    p = plan(planner, "sing me a song")
    assert p.calls == [] and p.say.startswith("Sorry")


def test_bundled_model_matches_example_and_resolves():
    import json
    from pathlib import Path
    from alexa_mcp_sim.app import resolve_model
    ex = Path(__file__).resolve().parents[1] / "examples" / "careloop.json"
    assert json.loads(Path(resolve_model("careloop")).read_text()) == json.loads(ex.read_text())
    assert resolve_model(str(ex)) == str(ex)
    import pytest
    with pytest.raises(SystemExit):
        resolve_model("no-such-model")
