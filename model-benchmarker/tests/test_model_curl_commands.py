"""The curl recipe renderer on the shared model classes.

Every base function (chat, responses, embeddings, ...) is one HTTP request, so
each class can print the equivalent curl command. The point of these tests is
the TLS half: an in-cluster HTTP endpoint needs no flags, a remote PCAI ingress
needs the pinned CA bundle (REMOTE_CA_BUNDLE) or -k, and a public https API must
stay verified instead of being downgraded to http.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from model_benchmarker.utils import pcai_model_classes as P  # noqa: E402

REMOTE = "https://m.serving.pcai.example.net"
LOCAL = "http://m.svc.cluster.local"


@pytest.fixture(autouse=True)
def _offline_and_quiet(monkeypatch):
    """No CA bundle, no one-time TLS notice, no discovery traffic."""
    monkeypatch.setattr(P, "_REMOTE_CA_BUNDLE", "")
    monkeypatch.setattr(P, "_REMOTE_VERIFY_WARNED", True)


def _chat(**kwargs):
    kwargs.setdefault("model_name", "Qwen/Qwen3-8B")
    kwargs.setdefault("url_remote", REMOTE)
    return P.ChatModel(**kwargs)


def _tls_flags(text):
    """Only the TLS option lines (never the -H / -d ones)."""
    flags = []
    for line in text.splitlines():
        token = line.strip().removesuffix("\\").strip()  # drop the line continuation
        if token in ("-k", "--insecure"):
            flags.append(token)
        elif token.startswith(("--cacert ", "--capath ")):
            flags.extend(token.split())
    return flags


def test_local_chat_command_is_plain_http_without_tls_flags():
    out = _chat().curl_command("chat")
    assert f"{LOCAL}/v1/chat/completions" in out
    assert "# transport: local - in-cluster HTTP: no TLS flags needed" in out
    assert _tls_flags(out) == []
    assert '"model": "Qwen/Qwen3-8B"' in out
    assert '"messages": [{"role": "user", "content": "Hello"}]' in out


def test_remote_chat_command_disables_verification_without_a_bundle():
    out = _chat(model_usage="remote").curl_command("chat")
    assert f"{REMOTE}/v1/chat/completions" in out
    assert _tls_flags(out) == ["-k"]
    assert "verification DISABLED" in out
    assert "--cacert" not in out


def test_remote_command_pins_the_ca_bundle_when_configured(tmp_path, monkeypatch):
    ca = tmp_path / "pcai-ca.pem"
    ca.write_text("cert")
    monkeypatch.setattr(P, "_REMOTE_CA_BUNDLE", str(ca))
    out = _chat(model_usage="remote").curl_command("chat")
    assert _tls_flags(out) == ["--cacert", str(ca)]
    assert "pinned CA bundle" in out


def test_remote_command_uses_capath_for_a_bundle_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_REMOTE_CA_BUNDLE", str(tmp_path))
    out = _chat(model_usage="remote").curl_command("chat")
    assert _tls_flags(out) == ["--capath", str(tmp_path)]


def test_remote_command_falls_back_to_insecure_when_the_bundle_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "_REMOTE_CA_BUNDLE", str(tmp_path / "absent.pem"))
    out = _chat(model_usage="remote").curl_command("chat")
    assert _tls_flags(out) == ["-k"]
    assert "does not exist" in out


def test_public_https_endpoint_keeps_tls_and_system_verification():
    """A non-serving https URL is never rewritten to http, so it is never curled with -k."""
    model = P.ChatModel(model_name="gpt-x", url_remote="https://api.example.com")
    out = model.curl_command("chat")
    assert "https://api.example.com/v1/chat/completions" in out
    assert _tls_flags(out) == []
    assert "system CA store" in out


def test_chat_lists_the_model_probe_plus_both_llm_transports():
    model = _chat()
    assert list(model.base_curl_operations()) == ["models", "chat", "responses"]
    assert f"{LOCAL}/v1/responses" in model.curl_command("responses")


def test_unknown_operation_names_what_is_available():
    with pytest.raises(ValueError, match="available: models, chat, responses"):
        _chat().curl_command("completions")


def test_unknown_usage_is_rejected_instead_of_silently_rendering_remote():
    with pytest.raises(ValueError, match="usage must be 'local' or 'remote'"):
        _chat().curl_command("chat", usage="cluster")


def test_usage_override_renders_the_other_side_without_switching_the_model():
    model = _chat()  # local by default
    remote_out = model.curl_command("chat", usage="remote")
    assert f"{REMOTE}/v1/chat/completions" in remote_out
    assert _tls_flags(remote_out) == ["-k"]
    assert model.model_usage == "local"
    assert model.base_url == f"{LOCAL}/v1"

    local_out = _chat(model_usage="remote").curl_command("chat", usage="local")
    assert f"{LOCAL}/v1/chat/completions" in local_out
    assert _tls_flags(local_out) == []


def test_api_key_is_redacted_by_default_and_inlined_on_request():
    model = _chat(api_key="sk-secret-value")
    redacted = model.curl_command("chat")
    assert "sk-secret-value" not in redacted
    assert "Bearer $PCAI_API_KEY" in redacted
    assert "include_api_key=True" in redacted
    inlined = model.curl_command("chat", include_api_key=True)
    assert "'Authorization: Bearer sk-secret-value'" in inlined


def test_key_less_endpoint_sends_no_authorization_header():
    out = _chat().curl_command("chat")
    assert "Authorization" not in out
    assert "# auth: none" in out


def test_print_returns_exactly_what_it_prints(capsys):
    model = _chat()
    printed = model.print_curl_command("chat")
    assert capsys.readouterr().out == printed + "\n"


def test_embedding_rerank_and_decision_paths():
    embedder = P.EmbeddingModel(model_name="Qwen/Qwen3-Embedding-8B", url_remote=REMOTE)
    assert f"{LOCAL}/v1/embeddings" in embedder.curl_command("embeddings")

    reranker = P.RerankerModel(model_name="Qwen/Qwen3-VL-Reranker-8B", url_remote=REMOTE)
    rerank = reranker.curl_command("rerank")
    assert f"{LOCAL}/rerank" in rerank
    assert "/v1/rerank" not in rerank  # the adapter strips /v1 before posting /rerank

    decision = P.DecisionModel(model_name="vllm-sr/d3-turbo", url_remote=REMOTE)
    assert f"{LOCAL}/v1/systemone" in decision.curl_command("systemone")


def test_voice_model_only_offers_the_functions_its_type_serves():
    asr = P.VoiceModel(model_name="whisper", url_remote=REMOTE, model_type="ASR")
    assert list(asr.base_curl_operations()) == ["models", "asr"]
    transcribe = asr.curl_command("asr")
    assert "-F file=@audio.wav" in transcribe

    tts = P.VoiceModel(model_name="qwen3-tts", url_remote=REMOTE, model_type="TTS", tts_voice="alys")
    assert list(tts.base_curl_operations()) == ["models", "tts", "voices"]
    assert '"voice": "alys"' in tts.curl_command("tts")


def test_speech_flow_delegates_to_the_models_it_composes():
    flow = P.SpeechFlowModel()
    flow.asr_model = P.VoiceModel(model_name="whisper", url_remote=REMOTE, model_type="ASR")
    flow.llm_model = _chat()
    flow.tts_model = P.VoiceModel(model_name="qwen3-tts", url_remote=REMOTE, model_type="TTS", tts_voice="alys")

    assert list(flow.base_curl_operations()) == ["asr", "llm", "tts"]
    assert f"{LOCAL}/v1/audio/transcriptions" in flow.curl_command("asr")
    assert f"{LOCAL}/v1/chat/completions" in flow.curl_command("llm")
    assert f"{LOCAL}/v1/audio/speech" in flow.curl_command("tts")


def test_unresolved_speech_flow_explains_instead_of_printing_a_broken_command():
    out = P.SpeechFlowModel().curl_command()
    assert "sub-models are not resolved" in out
    assert "curl -sS" not in out


def test_missing_endpoint_url_explains_instead_of_printing_a_broken_command():
    model = P.BaseModel(model_name="placeholder", currently_deployed=False)
    assert "no endpoint URL configured" in model.curl_command("models")


def test_every_rendered_command_is_valid_shell():
    """Copy-pasteable means syntactically valid: bash -n parses each one."""
    models = [
        _chat(api_key="sk-1"),
        _chat(model_usage="remote", api_key="sk-1"),
        P.VoiceModel(model_name="whisper", url_remote=REMOTE, model_type="ASR", api_key="sk-1"),
        P.VoiceModel(model_name="tts", url_remote=REMOTE, model_type="TTS", tts_voice="alys"),
        P.EmbeddingModel(model_name="emb", url_remote=REMOTE),
        P.RerankerModel(model_name="rr", url_remote=REMOTE),
        P.DecisionModel(model_name="d3", url_remote=REMOTE),
    ]
    for model in models:
        for operation in model.base_curl_operations():
            text = model.curl_command(operation)
            parsed = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
            assert parsed.returncode == 0, f"{type(model).__name__}.{operation}: {parsed.stderr}\n{text}"
