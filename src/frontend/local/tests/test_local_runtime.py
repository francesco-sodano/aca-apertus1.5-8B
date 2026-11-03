import asyncio
import base64
import io
import json
import wave

import httpx
import pytest
from PIL import Image

from apertus_local.media import MAX_AUDIO_SAMPLES, LocalAttachment, prepare_attachment
from apertus_local.runtime import (
    CONTEXT_TOKENS,
    MODEL_ID,
    LocalCompletionService,
    LocalProfile,
    LocalSettings,
)
from apertus_frontend.tools import ToolValidationError


def stream_response(*deltas, done=True, finish="stop"):
    packets = [{"choices": [{"delta": delta}]} for delta in deltas]
    packets.append({"choices": [{"delta": {}, "finish_reason": finish}]})
    body = "".join("data: " + json.dumps(packet) + "\n\n" for packet in packets)
    if done:
        body += "data: [DONE]\n\n"
    return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})


def wav_bytes(samples, rate=24_000, channels=1):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(channels)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(b"\0" * (samples * channels * 2))
    return output.getvalue()


@pytest.mark.parametrize("endpoint", [
    "https://example.com", "http://example.com", "http://inference:8000/v1",
    "http://user:password@inference:8000", "http://inference:8000?key=value",
])
def test_settings_reject_nonlocal_or_credential_endpoints(endpoint):
    with pytest.raises(ValueError, match="local inference"):
        LocalSettings(endpoint=endpoint)


def test_settings_ignore_ambient_cloud_values(monkeypatch):
    for key in ("CONTENT_SAFETY_ENDPOINT", "FOUNDRY_PROJECT_ENDPOINT", "MODEL_ENDPOINT",
                "VLLM_API_KEY", "APPLICATIONINSIGHTS_CONNECTION_STRING", "AZURE_CLIENT_SECRET"):
        monkeypatch.setenv(key, "CLOUD_SENTINEL")
    assert LocalSettings.from_env() == LocalSettings()


def test_image_is_resized_without_changing_aspect_ratio():
    source = io.BytesIO()
    Image.new("RGB", (1024, 512), "red").save(source, format="PNG")
    result = prepare_attachment("sample.png", "image/png", source.getvalue())
    with Image.open(io.BytesIO(base64.b64decode(result.data_base64))) as image:
        assert image.size == (512, 256)
        assert image.getpixel((0, 0)) == (255, 0, 0)
    assert result.content_part()["type"] == "image_url"


def test_audio_is_local_mono_24khz_pcm():
    result = prepare_attachment("sample.wav", "audio/wav", wav_bytes(48_000, 48_000, 2))
    with wave.open(io.BytesIO(base64.b64decode(result.data_base64))) as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 24_000)
        assert audio.getnframes() == 24_000
    assert result.content_part()["type"] == "audio_url"


def test_exact_audio_duration_boundary():
    prepare_attachment("ten.wav", "audio/wav", wav_bytes(MAX_AUDIO_SAMPLES))
    with pytest.raises(ValueError, match="10 seconds"):
        prepare_attachment("long.wav", "audio/wav", wav_bytes(MAX_AUDIO_SAMPLES + 1))


@pytest.mark.parametrize(("mime", "data"), [
    ("text/plain", b"text"), ("image/png", b"broken"), ("audio/wav", b"broken"),
    ("audio/wav", b""),
])
def test_invalid_media_is_rejected(mime, data):
    with pytest.raises(ValueError):
        prepare_attachment("invalid", mime, data)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", [LocalProfile.CHAT, LocalProfile.THINKING])
async def test_stream_keeps_media_native_and_reasoning_out_of_answer(profile):
    requests = []
    tokens = []

    def handle(request):
        assert request.url.host == "inference"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        requests.append((request.url.path, payload))
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 100})
        assert request.url.path == "/v1/chat/completions"
        assert payload["chat_template_kwargs"]["enable_thinking"] is (profile is LocalProfile.THINKING)
        return stream_response(
            {"reasoning": "PRIVATE_REASONING"},
            {"reasoning_content": "OLDER_PRIVATE_REASONING"},
            {"content": "Red, "}, {"content": "Blue, 7314"},
        )

    async def on_token(token):
        tokens.append(token)

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        result = await service.complete(
            text="Read both media.", history=[], profile=profile, on_token=on_token,
            attachments=(
                LocalAttachment("image", "image/png", "aW1hZ2U="),
                LocalAttachment("audio", "audio/wav", "YXVkaW8="),
            ),
        )
    finally:
        await service.aclose()
    assert result.answer == "".join(tokens) == "Red, Blue, 7314"
    parts = requests[-1][1]["messages"][-1]["content"]
    assert [part["type"] for part in parts] == ["text", "image_url", "audio_url"]
    assert parts[2]["audio_url"]["url"].startswith("data:audio/wav;base64,")


@pytest.mark.asyncio
async def test_native_tool_executes_only_local_allowlist_and_feeds_result_back():
    tokens = []

    def handle(request):
        payload = json.loads(request.content)
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 120})
        if payload.get("tools"):
            assert {item["function"]["name"] for item in payload["tools"]} == {"calculator", "get_current_time"}
            assert payload["tool_choice"] == "auto"
            return httpx.Response(200, json={"choices": [{
                "finish_reason": "tool_calls",
                "message": {"content": None, "tool_calls": [{
                    "id": "call-local", "type": "function",
                    "function": {"name": "calculator", "arguments": '{"expression":"23*17"}'},
                }]},
            }]})
        assert json.loads(payload["messages"][-1]["content"])["result"] == 391
        return stream_response({"content": "391"})

    async def on_token(token):
        tokens.append(token)

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        result = await service.complete(
            text="Use calculator for 23*17", history=[], profile=LocalProfile.TOOLS, on_token=on_token,
        )
    finally:
        await service.aclose()
    assert result.answer == "391"
    assert result.selected_tools == ("calculator",)
    assert tokens == ["391"]


@pytest.mark.asyncio
async def test_unknown_web_tool_is_rejected():
    def handle(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 100})
        return httpx.Response(200, json={"choices": [{
            "message": {"tool_calls": [{
                "id": "bad", "function": {"name": "search_web", "arguments": '{"query":"news"}'},
            }]},
        }]})

    async def on_token(token):
        pytest.fail("No answer should be emitted for an unregistered tool.")

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(ToolValidationError, match="unknown tool"):
            await service.complete(text="news", history=[], profile=LocalProfile.TOOLS, on_token=on_token)
    finally:
        await service.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("trim_history", [False, True])
async def test_context_limit_is_enforced_and_only_complete_history_pairs_are_trimmed(trim_history):
    history = [{"role": "user", "content": "old"}, {"role": "assistant", "content": "old answer"}]

    def handle(request):
        payload = json.loads(request.content)
        if request.url.path == "/tokenize":
            count = 100 if trim_history and len(payload["messages"]) == 2 else CONTEXT_TOKENS
            return httpx.Response(200, json={"count": count})
        return stream_response({"content": "answer"})

    async def on_token(token):
        pass

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        if trim_history:
            result = await service.complete(
                text="latest", history=history, profile=LocalProfile.CHAT, on_token=on_token,
            )
            assert result.trimmed_history_messages == 2
            assert len(history) == 2
        else:
            with pytest.raises(ValueError, match="16K context"):
                await service.complete(text="latest", history=[], profile=LocalProfile.CHAT, on_token=on_token)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_incomplete_stream_is_not_a_successful_completion():
    def handle(request):
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 100})
        return stream_response({"content": "partial"}, done=False)

    async def on_token(token):
        pass

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(RuntimeError, match="complete visible answer"):
            await service.complete(text="hello", history=[], profile=LocalProfile.CHAT, on_token=on_token)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_only_one_generation_runs_at_a_time():
    active = 0
    peak = 0

    async def handle(request):
        nonlocal active, peak
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"count": 100})
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return stream_response({"content": "answer"})

    async def on_token(token):
        pass

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        await asyncio.gather(*[
            service.complete(text="hello", history=[], profile=LocalProfile.CHAT, on_token=on_token)
            for _ in range(2)
        ])
    finally:
        await service.aclose()
    assert peak == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [False, True])
async def test_readiness_checks_real_model_identity_and_context(matches):
    def handle(request):
        if request.url.path == "/health":
            return httpx.Response(200)
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{
            "id": MODEL_ID if matches else "other-model", "max_model_len": CONTEXT_TOKENS,
        }]})

    service = LocalCompletionService(LocalSettings(), transport=httpx.MockTransport(handle))
    try:
        if matches:
            await service.ready()
        else:
            with pytest.raises(RuntimeError, match="tested profile"):
                await service.ready()
    finally:
        await service.aclose()
