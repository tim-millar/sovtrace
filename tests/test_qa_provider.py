"""Official SDK adapter tests use an in-memory HTTP transport, never the network."""

import json
from collections.abc import Callable

import httpx
import pytest
from openai import OpenAI

from sovtrace.qa.contracts import QAError
from sovtrace.qa.provider import DEFAULT_MODEL, OpenAIGenerator


def response_body(
    *, text: str | None = None, refusal: bool = False, status: str = "completed"
) -> dict[str, object]:
    content = (
        [{"type": "refusal", "refusal": "declined"}]
        if refusal
        else [
            {
                "type": "output_text",
                "text": text
                or '{"status":"query","operation":"count","sql":"SELECT COUNT(*) FROM assets","reason":null}',
                "annotations": [],
            }
        ]
    )
    return {
        "id": "resp_synthetic",
        "object": "response",
        "created_at": 1,
        "model": "gpt-6-luna",
        "status": status,
        "output": [
            {
                "type": "message",
                "id": "msg_synthetic",
                "status": "completed",
                "role": "assistant",
                "content": content,
            }
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
    }


def client_for(handler: Callable[[httpx.Request], httpx.Response]) -> OpenAI:
    return OpenAI(
        api_key="synthetic-test-key",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_responses_structured_output_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "requested-model")
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    with client_for(handler) as client:
        generator = OpenAIGenerator(client=client)
        generation = generator.generate("Synthetic question", "Table: assets\nasset_id: TEXT")
        assert generation.proposal.operation == "count"
        assert generation.model == "gpt-6-luna"
        assert not client.is_closed()
    assert len(requests) == 1
    body = requests[0]
    assert body["model"] == "requested-model"
    assert body["store"] is False
    assert body["input"] == [{"role": "user", "content": "Synthetic question"}]
    assert "Table: assets" in str(body["instructions"])
    assert "10000000000000" in str(body["instructions"])
    assert "tools" not in body and "previous_response_id" not in body
    schema = body["text"]  # Schema is sent through the actual SDK helper.
    assert isinstance(schema, dict)
    assert schema["format"]["type"] == "json_schema"
    assert schema["format"]["strict"] is True
    assert schema["format"]["schema"]["additionalProperties"] is False


def test_missing_credentials_and_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    generator = OpenAIGenerator()
    assert generator.model == DEFAULT_MODEL == "gpt-6-luna"
    with pytest.raises(QAError) as failure:
        generator.generate("Question", "Schema")
    assert failure.value.failure.category == "configuration_error"
    assert failure.value.failure.code == "missing_credentials"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "authentication"),
        (403, "authentication"),
        (429, "rate_limit"),
        (500, "service_unavailable"),
        (503, "service_unavailable"),
        (400, "request_rejected"),
    ],
)
def test_provider_http_failures(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {"message": "sensitive synthetic provider detail", "type": "test_error"}
            },
        )

    with client_for(handler) as client:
        with pytest.raises(QAError) as failure:
            OpenAIGenerator(client=client).generate("Question", "Schema")
    assert failure.value.failure.category == "provider_error"
    assert failure.value.failure.code == code
    assert "sensitive" not in str(failure.value)


@pytest.mark.parametrize(
    ("exception", "code"), [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "transport")]
)
def test_provider_transport_failures(exception: type[httpx.RequestError], code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception("synthetic error", request=request)

    with client_for(handler) as client:
        with pytest.raises(QAError) as failure:
            OpenAIGenerator(client=client).generate("Question", "Schema")
    assert failure.value.failure.code == code


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (response_body(refusal=True), "model_refusal"),
        (response_body(status="incomplete"), "incomplete_output"),
        (response_body(text="not JSON"), "malformed_output"),
        (
            response_body(text='{"status":"query","operation":"count","sql":null,"reason":null}'),
            "malformed_output",
        ),
        ({**response_body(), "output": []}, "malformed_output"),
    ],
)
def test_refusal_incomplete_and_malformed(body: dict[str, object], code: str) -> None:
    with client_for(lambda request: httpx.Response(200, json=body)) as client:
        with pytest.raises(QAError) as failure:
            OpenAIGenerator(client=client).generate("Question", "Schema")
    assert failure.value.failure.category == "invalid_model_response"
    assert failure.value.failure.code == code


def test_model_unsupported() -> None:
    body = response_body(
        text='{"status":"unsupported","operation":null,"sql":null,"reason":"Business concept unavailable"}'
    )
    with client_for(lambda request: httpx.Response(200, json=body)) as client:
        proposal = OpenAIGenerator(client=client).generate("Question", "Schema").proposal
    assert proposal.status == "unsupported" and proposal.sql is None


def test_multiple_structured_proposals_are_rejected() -> None:
    body = response_body()
    body["output"] = list(body["output"]) * 2  # type: ignore[arg-type]
    with client_for(lambda request: httpx.Response(200, json=body)) as client:
        with pytest.raises(QAError) as failure:
            OpenAIGenerator(client=client).generate("Question", "Schema")
    assert failure.value.failure.code == "malformed_output"


@pytest.mark.parametrize("status", [200, 500])
def test_owned_sdk_client_is_closed(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    client = client_for(
        lambda request: httpx.Response(
            status, json=response_body() if status == 200 else {"error": {"message": "synthetic"}}
        )
    )
    monkeypatch.setattr("sovtrace.qa.provider.OpenAI", lambda **kwargs: client)
    if status == 200:
        OpenAIGenerator().generate("Question", "Schema")
    else:
        with pytest.raises(QAError):
            OpenAIGenerator().generate("Question", "Schema")
    assert client.is_closed()


def test_response_usage_metadata_is_numeric_only() -> None:
    body = response_body()
    body["usage"] = {
        "input_tokens": 1200,
        "input_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 128},
        "output_tokens": 160,
        "output_tokens_details": {"reasoning_tokens": 64},
        "total_tokens": 1360,
    }
    body["service_tier"] = "default"
    with client_for(lambda request: httpx.Response(200, json=body)) as client:
        generation = OpenAIGenerator(client=client).generate("Question", "Schema")
    assert generation.usage is not None
    assert generation.usage.input_tokens == 1200
    assert generation.usage.cached_input_tokens == 800
    assert generation.usage.cache_write_tokens == 128
    assert generation.usage.output_tokens == 160
    assert generation.usage.reasoning_tokens == 64
    assert generation.service_tier == "default"


def test_explicit_models_preserve_identical_request_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "conflicting-env-model")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    with client_for(handler) as client:
        for model in ("gpt-6-luna", "gpt-5.4-mini"):
            OpenAIGenerator(model=model, client=client).generate("Same question", "Same schema")
    assert [r.pop("model") for r in requests] == ["gpt-6-luna", "gpt-5.4-mini"]
    assert requests[0] == requests[1]
    assert requests[0]["reasoning"] == {"effort": "low"}
    assert requests[0]["max_output_tokens"] == 3000


def test_question_is_verbatim_with_schema_but_without_source_records():
    question = "Artificial Mira Test: does $4200 at /artificial/path exceed a threshold?"
    schema = "Table: assets\nasset_id: TEXT\nstate: TEXT"
    requests = []

    def transport(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_body())

    with client_for(transport) as client:
        OpenAIGenerator(client=client).generate(question, schema)
    assert len(requests) == 1
    body = requests[0]
    assert body["input"] == [{"role": "user", "content": question}]
    assert schema in body["instructions"]
    assert body["store"] is False
    assert set(body) >= {"model", "instructions", "input", "text", "store"}
    assert all(key not in body for key in ("rows", "source_cells", "workbook", "evidence"))
