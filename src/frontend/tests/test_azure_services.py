from types import SimpleNamespace
import json
import logging

import pytest

from apertus_frontend.azure_services import (
    AzureContentSafetyGateway,
    FoundryWebSearchGateway,
    SafetyResponseError,
    MAX_GROUNDING_SUMMARY_CHARACTERS,
    _blocked_category,
    _extract_foundry_result,
)
from apertus_frontend.pipeline import (
    Attachment,
    Citation,
    GroundingUnavailableError,
    SafetyAssessment,
    SafetyBlockedError,
)
from apertus_frontend.tracing import trace_scope


def category_results(**severities):
    return [
        {"category": category, "severity": severities.get(category, 0)}
        for category in ("Hate", "SelfHarm", "Sexual", "Violence")
    ]


class FakeCredential:
    async def get_token(self, *scopes, **kwargs):
        return SimpleNamespace(token="token")


def foundry_payload(text="Evidence"):
    return {
        "status": "completed",
        "output_text": text,
        "output": [{
            "type": "web_search_call",
            "status": "completed",
            "action": {
                "type": "search",
                "sources": [{"type": "url", "url": "https://example.com/source"}],
            },
        }],
    }


class RecordingClient:
    def __init__(self, payload=None, headers=None, payloads=None):
        self.body = None
        self.bodies = []
        self.headers = headers or {}
        self.payload = payload if payload is not None else foundry_payload()
        self.payloads = list(payloads or [])

    async def post(self, url, *, headers, json, params=None):
        self.body = json
        self.bodies.append(json)
        self.request_headers = headers
        payload = self.payloads.pop(0) if self.payloads else self.payload
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: payload,
            headers=self.headers,
        )


def test_extracts_foundry_text_and_unique_citations():
    payload = {
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Grounded summary",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Primary source",
                                "url": "https://example.com/source",
                            },
                            {
                                "type": "url_citation",
                                "title": "Duplicate",
                                "url": "https://example.com/source",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Grounded summary"
    assert len(citations) == 1
    assert citations[0].url == "https://example.com/source"


def test_extracts_included_web_search_sources_without_inline_annotations():
    payload = {
        "output_text": "Evidence without inline annotations",
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "type": "search",
                    "sources": [
                        {
                            "type": "url",
                            "title": "Primary source",
                            "url": "https://example.com/primary",
                        }
                    ],
                },
            }
        ],
    }

    summary, citations = _extract_foundry_result(payload)

    assert summary == "Evidence without inline annotations"
    assert citations == (
        Citation(title="Primary source", url="https://example.com/primary"),
    )


def test_prefers_inline_citations_and_limits_display_sources():
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "sources": [
                        {
                            "type": "url",
                            "title": f"Source {index}",
                            "url": f"https://example.com/{index}",
                        }
                        for index in range(10)
                    ]
                },
            },
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Evidence",
                        "annotations": [
                            {
                                "type": "url_citation",
                                "title": "Cited source",
                                "url": "https://cited.example.com",
                            }
                        ],
                    }
                ],
            },
        ]
    }

    _, citations = _extract_foundry_result(payload)

    assert len(citations) == 5
    assert citations[0].url == "https://cited.example.com"


def test_content_safety_threshold_is_inclusive():
    payload = {
        "categoriesAnalysis": [
            {"category": "Hate", "severity": 2},
            {"category": "Violence", "severity": 4},
        ]
    }

    assert _blocked_category(payload, 4) == "Violence severity 4"
    assert _blocked_category(payload, 6) is None


@pytest.mark.asyncio
async def test_content_safety_block_preserves_rule_and_severity_for_ui():
    client = RecordingClient(
        payload={
            "userPromptAnalysis": {"attackDetected": False},
            "categoriesAnalysis": category_results(Violence=4),
        }
    )
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example",
        credential=FakeCredential(),
        threshold=4,
        client=client,
    )

    with pytest.raises(SafetyBlockedError) as blocked:
        await gateway.screen_text("unsafe content", purpose="user-input")

    assert blocked.value.rule == "Violence"
    assert blocked.value.severity == 4
    assert blocked.value.threshold == 4
    assert blocked.value.user_message == (
        "Your message was blocked by Azure AI Content Safety. Rule: Violence."
    )


@pytest.mark.asyncio
async def test_content_safety_returns_non_blocking_category_metadata(traced_spans):
    client = RecordingClient(
        payload={
            "userPromptAnalysis": {"attackDetected": False},
            "categoriesAnalysis": category_results(Violence=2),
        }
    )
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example",
        credential=FakeCredential(),
        threshold=4,
        client=client,
    )

    assessment = await gateway.screen_text(
        "potentially violent request", purpose="user-input"
    )

    assert assessment == SafetyAssessment(
        category="Violence", severity=2, threshold=4
    )
    decision = next(
        span
        for span in traced_spans.get_finished_spans()
        if span.name == "content_safety.text"
    )
    assert decision.attributes["apertus.outcome"] == "allowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "expects_reasoning"),
    [
        ("gpt-4.1-nano-grounding", False),
        ("gpt-5-nano-grounding", True),
    ],
)
async def test_web_search_uses_model_compatible_latency_controls(
    model, expects_reasoning
):
    client = RecordingClient()
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model=model,
        credential=FakeCredential(),
        client=client,
    )

    await gateway.search("Current information")

    assert ("reasoning" in client.body) is expects_reasoning
    assert ("text" in client.body) is expects_reasoning
    assert "English evidence brief" in client.body["instructions"]
    assert "separate model renders the final answer" in client.body["instructions"]
    assert "Missing results are not evidence" in client.body["instructions"]
    assert "strict safe-search policy" in client.body["instructions"]
    assert "under 150 words" in client.body["instructions"]
    assert client.body["max_output_tokens"] == 1200
    assert client.body["tools"][0]["search_context_size"] == "medium"
    assert "user_location" not in client.body["tools"][0]
    assert client.request_headers["x-ms-client-request-id"]


@pytest.mark.asyncio
async def test_answer_validation_preserves_claims_without_search_tools():
    client = RecordingClient(payload={
        "status": "completed",
        "output_text": json.dumps({
            "english_answer": "Odysseus returns home after the Trojan War.",
            "supported": True,
        }),
        "output": [],
    })
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    validation = await gateway.validate_answer(
        answer="Odisseus returna a chasa suenter la guerra da Troia.",
        evidence="Odysseus returns home after the Trojan War.",
    )

    assert validation.english_answer == "Odysseus returns home after the Trojan War."
    assert validation.supported is True
    assert "<EVIDENCE>" in client.body["input"]
    assert "<ANSWER>" in client.body["input"]
    assert "Set supported to true" in client.body["instructions"]
    assert "Do not use model memory, correct facts" in client.body["instructions"]
    assert "tools" not in client.body
    assert client.body["temperature"] == 0
    assert client.body["max_output_tokens"] == 1200
    assert client.body["text"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["incomplete", "empty", "refusal"])
async def test_answer_validation_fails_closed(failure):
    payload = {
        "status": "completed",
        "output_text": json.dumps({
            "english_answer": "English rendering",
            "supported": True,
        }),
        "output": [],
    }
    if failure == "incomplete":
        payload["status"] = "incomplete"
    elif failure == "empty":
        payload["output_text"] = "{}"
    else:
        payload["output"].append({"type": "refusal", "refusal": "Declined"})
    client = RecordingClient(payload=payload)
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    with pytest.raises(GroundingUnavailableError) as rejected:
        await gateway.validate_answer(answer="Answer", evidence="Evidence")

    assert rejected.value.source == "Microsoft Foundry"
    assert rejected.value.rule == "Structured evidence validation required"
    assert len(client.bodies) == 1


@pytest.mark.asyncio
async def test_web_search_caps_grounding_summary_size():
    client = RecordingClient(
        payload=foundry_payload("x" * (MAX_GROUNDING_SUMMARY_CHARACTERS + 1))
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    packet = await gateway.search("Current information")

    assert len(packet.summary) == MAX_GROUNDING_SUMMARY_CHARACTERS


@pytest.mark.asyncio
async def test_web_search_logs_only_support_metadata(caplog):
    client = RecordingClient(
        payload={
            **foundry_payload(),
            "id": "resp_support_123",
        },
        headers={
            "apim-request-id": "apim_support_123",
            "x-ms-request-id": "ms_support_123",
            "x-request-id": "request_support_123",
        },
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="gpt-4.1-nano-grounding",
        credential=FakeCredential(),
        client=client,
    )

    with caplog.at_level(logging.INFO, logger="apertus.frontend.azure"):
        await gateway.search("private user query")

    record = next(item for item in caplog.records if item.msg == "web_search_completed")
    assert record.custom_dimensions["client_request_id"]
    assert record.custom_dimensions["response_id"] == "resp_support_123"
    assert record.custom_dimensions["apim_request_id"] == "apim_support_123"
    assert record.custom_dimensions["x_ms_request_id"] == "ms_support_123"
    assert record.custom_dimensions["request_id"] == "request_support_123"
    assert record.custom_dimensions["web_search_call_count"] == 1
    assert record.custom_dimensions["search_action_count"] == 1
    assert "private user query" not in caplog.text
    assert "Evidence" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("capture_content", [False, True])
async def test_foundry_span_records_usage_and_optional_evidence(traced_spans, monkeypatch, capture_content):
    monkeypatch.setenv("TRACE_CONTENT", str(capture_content).lower())
    client = RecordingClient(payload={
        **foundry_payload("Evidence text"),
        "id": "foundry-response",
        "model": "gpt-4.1-nano",
        "usage": {"input_tokens": 90, "output_tokens": 40},
    }, headers={"apim-request-id": "support-id"})
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )
    with trace_scope("tool.execute"):
        await gateway.search("User query person@example.com")

    span, parent = traced_spans.get_finished_spans()
    assert span.parent.span_id == parent.context.span_id
    assert span.attributes["gen_ai.usage.input_tokens"] == 90
    assert span.attributes["gen_ai.usage.output_tokens"] == 40
    assert span.attributes["gen_ai.response.id"] == "foundry-response"
    assert span.attributes["apertus.response_status"] == "completed"
    assert span.attributes["apertus.search_action_count"] == 1
    assert span.attributes["apertus.evidence_status"] == "accepted"
    assert span.attributes["apertus.evidence_attempt"] == 1
    assert span.attributes["apertus.foundry.apim_request_id"] == "support-id"
    assert ("gen_ai.output.messages" in span.attributes) is capture_content
    if capture_content:
        assert "person@example.com" in span.attributes["gen_ai.input.messages"]
        assert "Evidence text" in span.attributes["gen_ai.output.messages"]
    assert "strict safe-search policy" not in span.to_json()
    assert "Bearer token" not in span.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["uncited", "incomplete", "failed_search", "missing_search", "empty_text", "invalid_url"])
async def test_invalid_search_evidence_is_retried_twice_then_rejected(failure, traced_spans):
    payload = foundry_payload()
    if failure == "uncited":
        payload["output"][0]["action"]["sources"] = []
    elif failure == "incomplete":
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
    elif failure == "failed_search":
        payload["output"][0]["status"] = "failed"
    elif failure == "missing_search":
        payload["output"] = []
    elif failure == "empty_text":
        payload["output_text"] = " "
    else:
        payload["output"][0]["action"]["sources"][0]["url"] = "not-a-url"
    client = RecordingClient(payload=payload)
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )

    with pytest.raises(GroundingUnavailableError) as rejected:
        await gateway.search("Public fact question")

    assert rejected.value.source == "Microsoft Foundry Web Search"
    assert len(client.bodies) == 3
    spans = traced_spans.get_finished_spans()
    assert [span.attributes["apertus.evidence_attempt"] for span in spans] == [1, 2, 3]
    assert all(span.attributes["apertus.evidence_status"] != "accepted" for span in spans)


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_status", ["content_filtered", "refused"])
async def test_provider_policy_rejection_is_never_retried(evidence_status, traced_spans):
    payload = foundry_payload("Withheld provider response")
    if evidence_status == "content_filtered":
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "content_filter"}
    else:
        payload["output"].append({
            "type": "message",
            "content": [{"type": "refusal", "refusal": "Provider declined the request."}],
        })
    client = RecordingClient(payloads=[payload, foundry_payload("Must not be requested")])
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )

    with pytest.raises(GroundingUnavailableError) as rejected:
        await gateway.search("Public fact question")

    assert rejected.value.retryable is False
    assert rejected.value.source == "Microsoft Foundry Web Search"
    assert rejected.value.rule == (
        "Content filter" if evidence_status == "content_filtered" else "Model refusal"
    )
    assert len(client.bodies) == 1
    spans = traced_spans.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["apertus.evidence_status"] == evidence_status
    assert spans[0].attributes["apertus.evidence_attempt"] == 1
    assert spans[0].attributes["apertus.outcome"] == "grounding_rejected"


@pytest.mark.asyncio
async def test_missing_sources_recover_without_reusing_false_evidence():
    missing = foundry_payload("The film does not exist.")
    missing["output"][0]["action"]["sources"] = []
    client = RecordingClient(payloads=[missing, foundry_payload("Cited film synopsis")])
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )

    packet = await gateway.search("Find the film synopsis")

    assert packet.summary == "Cited film synopsis"
    assert packet.valid_citations
    assert len(client.bodies) == 2
    assert "fresh search" in client.bodies[1]["instructions"]
    assert client.bodies[0]["input"] == "Find the film synopsis"
    assert "Broaden the factual search" in client.bodies[1]["input"]
    assert "Factual lookup:\nFind the film synopsis" in client.bodies[1]["input"]
    assert client.bodies[0]["tools"][0]["search_context_size"] == "medium"
    assert client.bodies[1]["tools"][0]["search_context_size"] == "high"
    assert all("The film does not exist." not in str(body) for body in client.bodies)


@pytest.mark.asyncio
async def test_third_attempt_uses_alternate_entity_search():
    missing = foundry_payload("No evidence")
    missing["output"][0]["action"]["sources"] = []
    client = RecordingClient(
        payloads=[missing, missing, foundry_payload("Cited evidence")]
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )

    packet = await gateway.search("Christopher Nolan The Odyssey plot")

    assert packet.valid_citations
    assert len(client.bodies) == 3
    assert "alternate concise search formulations" in client.bodies[2]["input"]
    assert client.bodies[2]["tools"][0]["search_context_size"] == "high"
    assert "wikipedia.org" in client.bodies[2]["tools"][0]["filters"]["allowed_domains"]


@pytest.mark.asyncio
async def test_third_attempt_does_not_filter_general_current_facts():
    missing = foundry_payload("No evidence")
    missing["output"][0]["action"]["sources"] = []
    client = RecordingClient(
        payloads=[missing, missing, foundry_payload("Cited evidence")]
    )
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )

    await gateway.search("current Swiss Federal Council members")

    assert "filters" not in client.bodies[2]["tools"][0]


@pytest.mark.asyncio
async def test_recovery_uses_only_selector_objective_not_response_requirements():
    missing = foundry_payload("No evidence")
    missing["output"][0]["action"]["sources"] = []
    client = RecordingClient(payloads=[missing, foundry_payload("Cited evidence")])
    gateway = FoundryWebSearchGateway(
        project_endpoint="https://foundry.example/api/projects/test",
        model="grounding-model", credential=FakeCredential(), client=client,
    )
    query = (
        "Search objective selected by Apertus: Christopher Nolan The Odyssey plot\n"
        "LATEST USER REQUEST: Answer in Rumantsch Grischun\n"
        "Conversation context for reference resolution only: private history"
    )

    await gateway.search(query)

    recovery_input = client.bodies[1]["input"]
    assert "Christopher Nolan The Odyssey plot" in recovery_input
    assert all("Rumantsch" not in body["input"] for body in client.bodies)
    assert all("private history" not in body["input"] for body in client.bodies)


@pytest.mark.asyncio
async def test_safety_trace_contains_decision_not_submitted_text(traced_spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "true")
    client = RecordingClient(payload={
        "userPromptAnalysis": {"attackDetected": False},
        "categoriesAnalysis": category_results(Violence=5),
    })
    gateway = AzureContentSafetyGateway(endpoint="https://safety.example", credential=FakeCredential(), client=client)

    with pytest.raises(SafetyBlockedError):
        await gateway.screen_text("CONTENT_SENTINEL", purpose="user-input")

    spans = traced_spans.get_finished_spans()
    decision = next(span for span in spans if span.name == "content_safety.text")
    assert decision.attributes["apertus.outcome"] == "blocked"
    assert decision.attributes["apertus.safety.severity"] == 5
    assert decision.attributes["apertus.safety.rule"] == "Violence"
    assert any(span.name == "content_safety.shieldPrompt" for span in spans)
    assert all("CONTENT_SENTINEL" not in span.to_json() for span in spans)


@pytest.mark.asyncio
async def test_image_trace_never_captures_attachment_data(traced_spans, monkeypatch):
    monkeypatch.setenv("TRACE_CONTENT", "true")
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example", credential=FakeCredential(),
        client=RecordingClient(payload={"categoriesAnalysis": category_results()}),
    )

    await gateway.screen_image(Attachment("NAME_SENTINEL", "image/png", "IMAGE_SENTINEL"))

    for span in traced_spans.get_finished_spans():
        assert "SENTINEL" not in span.to_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("detected, expected", [(False, "grounded"), (True, "ungrounded"), (None, "indeterminate")])
async def test_groundedness_span_records_tristate_result(traced_spans, detected, expected):
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example", credential=FakeCredential(),
        client=RecordingClient(payload={} if detected is None else {"ungroundedDetected": detected}),
    )

    await gateway.is_grounded(query="Query", answer="Answer", sources=("Evidence",))

    decision = next(span for span in traced_spans.get_finished_spans() if span.name == "groundedness.detect")
    assert decision.attributes["apertus.groundedness_result"] == expected
    assert gateway._client.body["task"] == "Summarization"
    assert "qna" not in gateway._client.body


@pytest.mark.asyncio
@pytest.mark.parametrize("analysis", [None, {}, {"attackDetected": "false"}, {"attackDetected": 0}])
async def test_malformed_prompt_shield_response_fails_closed(analysis):
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example", credential=FakeCredential(),
        client=RecordingClient(payload={"userPromptAnalysis": analysis}),
    )

    with pytest.raises(SafetyResponseError):
        await gateway.screen_text("Test message", purpose="user-input")


@pytest.mark.asyncio
@pytest.mark.parametrize("categories", [
    None, [], [{"category": "Violence", "severity": 0}],
    category_results(Violence="0"), category_results(Violence=True),
    category_results(Violence=8),
])
async def test_malformed_category_response_fails_closed(categories):
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example", credential=FakeCredential(),
        client=RecordingClient(payload={"categoriesAnalysis": categories}),
    )

    with pytest.raises(SafetyResponseError):
        await gateway.screen_text("Test output", purpose="model-output")


@pytest.mark.asyncio
@pytest.mark.parametrize("detected", [None, "false", [], 0])
async def test_malformed_groundedness_is_not_an_approval(detected):
    gateway = AzureContentSafetyGateway(
        endpoint="https://safety.example", credential=FakeCredential(),
        client=RecordingClient(payload={"ungroundedDetected": detected}),
    )

    assert await gateway.is_grounded(query="Query", answer="Answer", sources=("Evidence",)) is None
