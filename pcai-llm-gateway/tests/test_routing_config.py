"""Model-routing runtime config + the decision-model prompt contract.

WHY THIS FILE EXISTS (2026-10-23): core.model_routing has been heuristic-only
and env-tuned, so there was nowhere to say "the router may route to these
models, not those", nowhere to keep the prompt a DECISION model (vllm-sr /
d3-turbo) would receive, and no way to feed live GPU demand into it. This
wave adds core.routing_config (mirrors core.distiller_config: TTL cache,
degrade-not-fail, one transaction per write) plus the admin routes over it.

The contract these tests pin:

  * render_prompt is a TEMPLATE, not a format string. Only the five named
    {{placeholders}} are substituted; an unknown {{...}} stays VERBATIM so a
    typo is visible in the preview instead of being silently eaten. No eval,
    no str.format — the template is operator prose that may contain braces.
  * _encode/_decode is TOTAL and round-trips exactly, including the LIST tag
    ('j:') for allowed_models and junk passthrough (a hand-edited row must
    degrade, never raise). The bool-through-TEXT inversion the distiller hit
    (bool("false") is True) is asserted here too.
  * the PUT validates against the LIVE registry: an unregistered
    decision_model or allowed_models entry is a 400, and an unknown key is a
    400 rather than a silent drop. A field sent as null CLEARS its row.
  * prompt-preview WRITES NOTHING — no execute, no insert — so an operator
    can iterate on wording without littering the audit trail.

FakeDB style per repo convention (test_admin_ui.py / test_distiller.py): the
routes mount on a bare FastAPI app with the admin key stubbed, and the kv
store models the real TEXT round-trip by storing ENCODED strings, so a
decode defect cannot hide behind a fake that keeps Python objects.
"""

from __future__ import annotations

import uuid
from typing import Any, Self

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pcai_llm.api import routes_admin
from pcai_llm.config import settings
from pcai_llm.core import routing_config as rc
from pcai_llm.routing.router import Endpoint, Router

ADMIN_H = {"Authorization": "Bearer admin-secret"}


def _reset_config_cache() -> None:
    """The module TTL cache is process-global; every test starts cold."""
    rc._cache = {}
    rc._cache_at = 0.0


# ---------------------------------------------------------------------------
# _encode / _decode — TOTAL, exact, junk-tolerant
# ---------------------------------------------------------------------------


def test_encode_decode_roundtrips_every_type() -> None:
    for value in (True, False, 0, 8000, "", "a prompt", ["m1", "m2"], []):
        assert rc._decode(rc._encode(value)) == value


def test_bool_does_not_invert_through_the_text_column() -> None:
    """THE P0 defect class from distiller_config: asyncpg returns "false" for a
    stored False, and bool("false") is True — an admin's inject_gpu_load=false
    would read back ON. The type tags are what stop it."""
    assert rc._encode(False) == "b:false"
    assert rc._decode("b:false") is False
    assert rc._decode("b:true") is True


def test_list_uses_the_json_tag_and_survives_junk() -> None:
    assert rc._encode(["Qwen3.8-27B"]).startswith("j:")
    assert rc._decode('j:["a", "b"]') == ["a", "b"]
    # junk must never raise: the router's fail-safe direction is "route
    # normally", and an unreadable list means allow-all, not a broken config.
    assert rc._decode("j:not-json") == []
    assert rc._decode('j:{"not": "a list"}') == []


def test_untagged_legacy_rows_pass_through_as_strings() -> None:
    assert rc._decode("offpeak") == "offpeak"
    assert rc._decode("i:not-an-int") == "i:not-an-int"  # undecodable int stays raw


def test_defaults_returns_a_copy() -> None:
    """allowed_models is a LIST: a caller mutating the returned dict must not
    edit the module's DEFAULTS in place (that would poison every later read)."""
    got = rc.defaults()
    got["allowed_models"].append("poison")
    assert rc.DEFAULTS["allowed_models"] == []


# ---------------------------------------------------------------------------
# render_prompt — PURE template substitution
# ---------------------------------------------------------------------------

_GPU = {
    "available": True,
    "source": "offpeak",
    "inject": True,
    "models": {
        "Qwen3.8-27B": {"value_pct": 42.14, "active": True, "fraction": 0.5},
        "GLM-5.3-Flash": {"value_pct": None, "active": False, "fraction": 1.0},
    },
}


def test_render_substitutes_every_placeholder() -> None:
    out = rc.render_prompt(
        "models={{models}}|list={{model_list}}|gpu={{gpu_load}}|json={{gpu_load_json}}|dm={{decision_model}}",
        gpu=_GPU,
        models=["Qwen3.8-27B", "GLM-5.3-Flash"],
        decision_model="vllm-sr",
    )
    assert "models=Qwen3.8-27B, GLM-5.3-Flash" in out
    assert "- Qwen3.8-27B\n- GLM-5.3-Flash" in out
    # the low-demand model renders value + demand word + fraction; the
    # unsampled one renders 'no data' rather than a fabricated 0%.
    assert "- Qwen3.8-27B: 42.1% util (low demand, 0.5x)" in out
    assert "- GLM-5.3-Flash: no data" in out
    assert '"Qwen3.8-27B"' in out  # the JSON form
    assert "dm=vllm-sr" in out


def test_render_leaves_unknown_placeholders_verbatim() -> None:
    """A typo must be VISIBLE in the preview, not silently eaten."""
    out = rc.render_prompt("{{models}} and {{modles}} and {{gpu_load_x}}", gpu=_GPU, models=["m1"])
    assert "m1 and {{modles}} and {{gpu_load_x}}" == out


def test_render_does_not_format_or_eval() -> None:
    """Braces that are not a known placeholder are literal text — a JSON
    example in the prompt must survive untouched, and nothing is executed."""
    out = rc.render_prompt('{"a": 1, "b": {nested}} {0} {{ models }}', gpu=_GPU, models=["m1"])
    assert out == '{"a": 1, "b": {nested}} {0} {{ models }}'


def test_render_empty_models() -> None:
    out = rc.render_prompt("[{{models}}][{{model_list}}]", gpu=_GPU, models=[])
    assert out == "[][]"


def test_render_gpu_unavailable_says_no_data_per_model() -> None:
    gpu = {"available": False, "source": "offpeak", "inject": True, "models": {}}
    out = rc.render_prompt("{{gpu_load}}", gpu=gpu, models=["m1", "m2"])
    assert out == "- m1: no data\n- m2: no data"


def test_render_injection_off_marks_both_placeholders() -> None:
    """inject_gpu_load false collapses BOTH forms to the same marker, so a
    prompt referencing only the JSON form behaves consistently with one
    referencing the prose form."""
    gpu = {**_GPU, "inject": False}
    out = rc.render_prompt("{{gpu_load}} / {{gpu_load_json}}", gpu=gpu, models=["m1"])
    assert out == '(gpu load injection is off) / "(gpu load injection is off)"'


def test_render_tolerates_a_junk_gpu_payload() -> None:
    """The renderer is fed a snapshot dict; a non-dict or a models value of the
    wrong type must degrade, never raise (this runs on a request path)."""
    assert rc.render_prompt("{{gpu_load}}", gpu={}, models=["m1"]) == "(gpu load injection is off)"
    assert rc.render_prompt("{{gpu_load}}", gpu={"inject": True, "models": "junk"}, models=["m1"]) == "- m1: no data"


# ---------------------------------------------------------------------------
# gpu_load_snapshot — the off-peak signal, never a second Prometheus client
# ---------------------------------------------------------------------------


async def test_gpu_snapshot_unavailable_when_offpeak_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "offpeak_enabled", False)
    snap = await rc.gpu_load_snapshot()
    assert snap["available"] is False and snap["models"] == {}


async def test_gpu_snapshot_shapes_the_offpeak_state(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.core import offpeak

    monkeypatch.setattr(settings, "offpeak_enabled", True)
    monkeypatch.setattr(
        offpeak,
        "models_snapshot",
        lambda: {
            "Qwen3.8-27B": {
                "active": True,
                "fraction": 0.5,
                "last_value_pct": 12.5,
                "last_check": "2026-10-23T00:00:00+00:00",
                "last_error": None,
                "promql": "q",
            }
        },
    )
    snap = await rc.gpu_load_snapshot()
    assert snap["available"] is True and snap["source"] == "offpeak"
    entry = snap["models"]["Qwen3.8-27B"]
    assert entry["value_pct"] == 12.5 and entry["active"] is True and entry["fraction"] == 0.5
    assert entry["promql"] == "q"


async def test_gpu_snapshot_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from pcai_llm.core import offpeak

    monkeypatch.setattr(settings, "offpeak_enabled", True)

    def boom() -> dict:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(offpeak, "models_snapshot", boom)
    snap = await rc.gpu_load_snapshot()
    assert snap["available"] is False


# ---------------------------------------------------------------------------
# routing_allowed_models — the router's own ceiling
# ---------------------------------------------------------------------------


async def test_allowed_models_empty_config_allows_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_config_cache()
    monkeypatch.setattr(rc, "db", _KVFakeDB())
    assert await rc.routing_allowed_models(["a", "b"]) == ["a", "b"]


async def test_allowed_models_intersects_in_registry_order(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _KVFakeDB()
    fake.rows = {"allowed_models": rc._encode(["b", "c"])}
    _reset_config_cache()
    monkeypatch.setattr(rc, "db", fake)
    # registry order wins, not config order: the ladder and the prompt agree
    assert await rc.routing_allowed_models(["a", "b", "c"]) == ["b", "c"]


async def test_allowed_models_all_stale_falls_back_to_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A config naming only vanished models must not silently disable routing
    (the graceful-degradation direction delete_model documents)."""
    fake = _KVFakeDB()
    fake.rows = {"allowed_models": rc._encode(["ghost"])}
    _reset_config_cache()
    monkeypatch.setattr(rc, "db", fake)
    assert await rc.routing_allowed_models(["a"]) == ["a"]


# ---------------------------------------------------------------------------
# the kv fake (models the real TEXT round-trip) + the route fixture
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, fake: _KVFakeDB) -> None:
        self.fake = fake

    async def execute(self, sql: str, *args: Any) -> str:
        return await self.fake.execute(sql, *args)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def transaction(self) -> _FakeConn:
        return self


class _KVFakeDB:
    """SELECT key/value + upsert/delete for the routing_config kv table.

    Stores the ENCODED strings (as asyncpg would return them), so a decode
    defect cannot hide behind a fake that keeps Python objects. Every execute
    is recorded so the preview test can assert NOTHING was written.
    """

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    def acquire_bounded(self) -> _FakeConn:
        return _FakeConn(self)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"key": k, "value": v} for k, v in self.rows.items()]

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return None

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if "INSERT INTO routing_config" in sql:
            self.rows[str(args[0])] = args[1]
        elif "DELETE FROM routing_config" in sql:
            self.rows.pop(str(args[0]), None)
        return "OK"


class _AdminFakeDB(_KVFakeDB):
    """The kv store, the override-list SELECT, and a profile-upsert echo.

    The echo exists because create_profile asserts a RETURNING row: without it
    a POST that VALIDATED correctly still 500s on the fake, which would mask a
    real refusal behind an unrelated crash (the test would pass on the wrong
    status).
    """

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [{"key": k, "value": v} for k, v in self.rows.items()]

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO profiles" in sql:
            return {
                "id": uuid.uuid4(),
                "name": args[0],
                "model_order": args[1],
                "defaults_json": args[2],
                "offpeak_fraction": args[3],
                "routing_policy": args[4],
                "routing_min_model": args[5],
            }
        return None


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch):
    """Mount the admin router with a kv fake and a registered chat/embedding
    pair, so validation has a real registry to validate against.

    core.audit owns its OWN pool seam (audit.db) by design — faking the
    operation seams is not enough for the audit row, and in a unit test the
    real pool is unconnected (it logs 'audit row lost' and carries on, which
    would make an audit assertion vacuous). The audit ACTION is recorded here
    instead, the same seam test_deny_rows uses."""
    monkeypatch.setattr(settings, "admin_api_keys", "admin-secret")
    fake = _AdminFakeDB()
    monkeypatch.setattr(routes_admin, "db", fake)
    _reset_config_cache()
    monkeypatch.setattr(rc, "db", fake)

    audited: list[dict[str, Any]] = []

    async def _fake_audit(request: Any, **kwargs: Any) -> None:
        audited.append(kwargs)

    monkeypatch.setattr(routes_admin, "_audit", _fake_audit)

    r = Router()
    r.set_endpoints("llm-chat", [Endpoint(id="e1", model_name="llm-chat", base_url="http://a/v1", model_type="chat")])
    r.set_endpoints("llm-big", [Endpoint(id="e2", model_name="llm-big", base_url="http://b/v1", model_type="chat")])
    r.set_endpoints("emb", [Endpoint(id="e3", model_name="emb", base_url="http://c/v1", model_type="embedding")])
    monkeypatch.setattr(routes_admin, "model_router", r)

    app = FastAPI()
    app.include_router(routes_admin.router)
    return TestClient(app, raise_server_exceptions=False), fake, r, audited


# ---------------------------------------------------------------------------
# GET /admin/routing/config
# ---------------------------------------------------------------------------


def test_get_config_reports_the_whole_panel_surface(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.get("/admin/routing/config", headers=ADMIN_H)
    assert r.status_code == 200, r.text
    d = r.json()
    # every key the console reads (the panel is already shipped)
    for key in ("config", "defaults", "overridden", "registered", "chat_models", "placeholders", "gpu"):
        assert key in d, key
    assert d["config"]["enabled"] is True
    assert d["config"]["decision_model"] == ""
    assert d["config"]["allowed_models"] == []
    assert d["overridden"] == []
    # chat_models excludes the embedding pool: a router ladder is a chat concept
    assert d["chat_models"] == ["llm-big", "llm-chat"]
    assert "emb" in d["registered"]
    assert set(d["placeholders"]) == {"models", "model_list", "gpu_load", "gpu_load_json", "decision_model"}
    # effective_enabled is the AND the rest of the gateway honors
    assert d["env_enabled"] is True and d["effective_enabled"] is True


def test_effective_enabled_is_the_env_and(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    tc, fake, _r, _audited = admin_env
    fake.rows = {"enabled": rc._encode(True)}
    _reset_config_cache()
    monkeypatch.setattr(settings, "routing_enabled", False)
    d = tc.get("/admin/routing/config", headers=ADMIN_H).json()
    assert d["config"]["enabled"] is True and d["env_enabled"] is False
    assert d["effective_enabled"] is False  # the env flag still wins


# ---------------------------------------------------------------------------
# PUT /admin/routing/config
# ---------------------------------------------------------------------------


def test_put_persists_and_reports_overrides(admin_env) -> None:
    tc, fake, _r, audited = admin_env
    r = tc.put(
        "/admin/routing/config",
        json={"prompt": "pick one of {{models}}", "decision_model": "llm-big", "inject_gpu_load": True},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["decision_model"] == "llm-big"
    assert r.json()["config"]["inject_gpu_load"] is True
    # the mutation is audited under the contract's action/resource triple
    assert audited and audited[-1]["action"] == "routing.config"
    assert audited[-1]["resource_type"] == "system" and audited[-1]["resource_id"] == "routing"
    assert sorted(audited[-1]["details"]["keys"]) == ["decision_model", "inject_gpu_load", "prompt"]
    # the rows landed ENCODED (real TEXT round-trip)
    assert fake.rows["decision_model"] == "s:llm-big"
    assert fake.rows["inject_gpu_load"] == "b:true"
    overridden = tc.get("/admin/routing/config", headers=ADMIN_H).json()["overridden"]
    assert overridden == ["decision_model", "inject_gpu_load", "prompt"]


def test_put_null_clears_the_row(admin_env) -> None:
    tc, fake, _r, _audited = admin_env
    tc.put("/admin/routing/config", json={"prompt": "x", "max_prompt_chars": 900}, headers=ADMIN_H)
    assert "prompt" in fake.rows
    r = tc.put("/admin/routing/config", json={"prompt": None}, headers=ADMIN_H)
    assert r.status_code == 200
    assert "prompt" not in fake.rows  # cleared, not tombstoned
    assert r.json()["config"]["prompt"] == ""  # back to the default


def test_put_unknown_key_is_400(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"bogus_key": 1}, headers=ADMIN_H)
    assert r.status_code == 400 and "bogus_key" in r.json()["detail"]


def test_put_unregistered_decision_model_is_400(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"decision_model": "ghost"}, headers=ADMIN_H)
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"] and "not a registered decision" in r.json()["detail"]


def test_put_unregistered_allowed_models_is_400(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"allowed_models": ["llm-chat", "ghost"]}, headers=ADMIN_H)
    assert r.status_code == 400
    assert "ghost" in r.json()["detail"]
    # the VALID chat model is not blamed — only the bad entry is named in the
    # offending list (the detail separately lists the chat pools for reference).
    offending = r.json()["detail"].split("chat pools:")[0]
    assert "llm-chat" not in offending


def test_put_bad_gpu_load_source_is_400(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"gpu_load_source": "prometheus"}, headers=ADMIN_H)
    assert r.status_code == 400 and "offpeak" in r.json()["detail"]


def test_put_max_prompt_chars_bounds(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    assert tc.put("/admin/routing/config", json={"max_prompt_chars": 199}, headers=ADMIN_H).status_code == 422
    assert tc.put("/admin/routing/config", json={"max_prompt_chars": 60001}, headers=ADMIN_H).status_code == 422
    assert tc.put("/admin/routing/config", json={"max_prompt_chars": 200}, headers=ADMIN_H).status_code == 200


def test_put_allowed_models_roundtrips_as_a_list(admin_env) -> None:
    """The list must come back as a LIST, not a JSON string — the panel renders
    it into checkboxes."""
    tc, fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"allowed_models": ["llm-chat"]}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert r.json()["config"]["allowed_models"] == ["llm-chat"]
    assert fake.rows["allowed_models"].startswith("j:")


# ---------------------------------------------------------------------------
# POST /admin/routing/prompt-preview — WRITES NOTHING
# ---------------------------------------------------------------------------


def test_preview_renders_with_saved_config_and_writes_nothing(admin_env) -> None:
    tc, fake, _r, _audited = admin_env
    tc.put("/admin/routing/config", json={"prompt": "saved {{models}}"}, headers=ADMIN_H)
    before = list(fake.executed)

    r = tc.post("/admin/routing/prompt-preview", json={}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rendered"] == "saved llm-big, llm-chat"
    assert d["truncated"] is False and d["chars"] == len(d["rendered"])
    assert d["models"] == ["llm-big", "llm-chat"]
    assert d["used_placeholders"] == ["models"]
    # THE contract: a preview is a read. No execute, no insert, no audit.
    assert fake.executed == before, "prompt-preview must not write"


def test_preview_overrides_do_not_persist(admin_env) -> None:
    tc, fake, _r, _audited = admin_env
    tc.put("/admin/routing/config", json={"prompt": "saved"}, headers=ADMIN_H)
    r = tc.post(
        "/admin/routing/prompt-preview",
        json={"prompt": "override {{decision_model}}", "decision_model": "llm-big"},
        headers=ADMIN_H,
    )
    assert r.json()["rendered"] == "override llm-big"
    # the SAVED config is untouched
    assert fake.rows["prompt"] == "s:saved"


def test_preview_truncates_at_max_prompt_chars(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    tc.put("/admin/routing/config", json={"max_prompt_chars": 200}, headers=ADMIN_H)
    r = tc.post("/admin/routing/prompt-preview", json={"prompt": "x" * 500}, headers=ADMIN_H)
    d = r.json()
    assert d["truncated"] is True and d["chars"] == 200


def test_preview_inject_off_marks_the_gpu_placeholders(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.post(
        "/admin/routing/prompt-preview",
        json={"prompt": "{{gpu_load}}", "inject_gpu_load": False},
        headers=ADMIN_H,
    )
    assert r.json()["rendered"] == "(gpu load injection is off)"


def test_preview_used_placeholders_is_computed_on_the_template(admin_env) -> None:
    """Empty means the prompt references no live data at all; a name missing
    from the list means that placeholder is misspelled in the text."""
    tc, _fake, _r, _audited = admin_env
    d = tc.post(
        "/admin/routing/prompt-preview",
        json={"prompt": "{{models}} {{modles}}"},
        headers=ADMIN_H,
    ).json()
    assert d["used_placeholders"] == ["models"]
    assert "{{modles}}" in d["rendered"]  # typo stays visible


# ---------------------------------------------------------------------------
# section 7 — a profile ordering that doubles as an ACL
# ---------------------------------------------------------------------------


def test_stale_model_order_entry_is_refused_naming_it(admin_env) -> None:
    """THE regression guard for the missing-Qwen defect.

    routes_enroll derives an enrolled key's allowed_models as
    [m for m in model_order if m in registered], so a renamed/never-registered
    id silently deleted that model from every key minted from the profile —
    no error, no warning, just a shorter dropdown. The live shape: an ordering
    naming "Qwen/Qwen3.8-27B-FP8" while the registry served "Qwen3.8-27B".
    """
    tc, fake, _r, _audited = admin_env
    r = tc.post(
        "/admin/profiles",
        json={"name": "team", "model_order": ["llm-chat", "Qwen/Qwen3.8-27B-FP8"]},
        headers=ADMIN_H,
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "Qwen/Qwen3.8-27B-FP8" in detail  # the stale entry is NAMED
    assert "not registered" in detail
    assert "allowed_models" in detail  # says what the consequence would be
    # refused BEFORE any write
    assert not [a for sql, a in fake.executed if "INSERT INTO profiles" in sql]


def test_registered_model_order_entries_are_accepted(admin_env) -> None:
    """Registered-but-unusual entries stay allowed: this checks registry
    membership, not good taste."""
    tc, _fake, _r, _audited = admin_env
    r = tc.post(
        "/admin/profiles",
        json={"name": "team", "model_order": ["llm-big", "llm-chat"]},
        headers=ADMIN_H,
    )
    assert r.status_code == 201, r.text
    assert r.json()["model_order"] == ["llm-big", "llm-chat"]


def test_empty_registry_does_not_block_a_profile_save(admin_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """SCOPE of the guard: with no models registered the derived ACL is []
    whatever the ordering says, and an empty ACL means allow-ALL — nothing can
    be hidden, so there is no silent truncation to prevent. Refusing there
    would block the bootstrap/teardown ordering (a fresh install seeds
    profiles before endpoints; a draining site empties the registry)."""
    tc, _fake, _r, _audited = admin_env
    monkeypatch.setattr(routes_admin, "model_router", Router())  # empty registry
    r = tc.post("/admin/profiles", json={"name": "seed", "model_order": ["not-there-yet"]}, headers=ADMIN_H)
    assert r.status_code == 201, r.text


def test_list_profiles_reports_stale_models_and_acl_effect(admin_env) -> None:
    """The console can only show the double duty if GET reports it: a drifted
    ordering read as "no models configured" while it was actively hiding
    models from every enrolled key."""
    tc, _fake, _r, _audited = admin_env

    class _ProfilesDB(_AdminFakeDB):
        async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
            if "FROM profiles" in sql:
                return [
                    {
                        "id": uuid.uuid4(),
                        "name": "team",
                        "model_order": ["llm-chat", "Qwen/Qwen3.8-27B-FP8"],
                        "defaults_json": "{}",
                        "offpeak_fraction": None,
                        "routing_policy": "off",
                        "routing_min_model": "",
                    }
                ]
            return await super().fetch(sql, *args)

    import pcai_llm.api.routes_admin as ra

    original = ra.db
    ra.db = _ProfilesDB()
    try:
        rows = tc.get("/admin/profiles", headers=ADMIN_H).json()
    finally:
        ra.db = original
    row = rows[0]
    assert row["stale_models"] == ["Qwen/Qwen3.8-27B-FP8"]
    assert row["acl_effect"] == {"mode": "derived", "models": ["llm-chat"]}
    # existing fields untouched (additive reporting only)
    assert row["name"] == "team" and row["model_order"] == ["llm-chat", "Qwen/Qwen3.8-27B-FP8"]


# ---------------------------------------------------------------------------
# Cross-validation regression tests (2026-10-23 review wave)
#
# Each case is a defect a reviewer FOUND in the first cut of this wave, pinned
# so it cannot come back. They share one theme: a value an operator could set
# that the surfaces would then MISREPORT.
# ---------------------------------------------------------------------------


def test_put_refuses_a_non_chat_decision_model(admin_env) -> None:
    """An EMBEDDING pool as decision model is not merely useless — it was
    actively harmful. allowed_models was validated against EVERY pool, so an
    embedding-only allowlist saved cleanly, intersected to EMPTY against the
    chat candidates, tripped routing_allowed_models' all-stale fallback, and
    then reported EVERY chat model eligible: the exact opposite of the
    restriction just saved. Both name fields are chat-only now, which keeps
    that fallback reserved for ids that VANISHED from the registry."""
    tc, _fake, _r, _audited = admin_env
    # NOTE (2026-10-23): the DECISION MODEL field was widened from chat-only
    # to decision-or-chat when the vLLM-SR decision type landed, because the
    # decision model CHOOSES a ladder candidate rather than being one. An
    # embedding pool is still refused by both — it can answer neither a chat
    # request nor a choice question.
    r = tc.put("/admin/routing/config", json={"decision_model": "emb"}, headers=ADMIN_H)
    assert r.status_code == 400, r.text
    assert "decision" in r.json()["detail"]


def test_put_refuses_non_chat_allowed_models(admin_env) -> None:
    tc, _fake, _r, _audited = admin_env
    r = tc.put("/admin/routing/config", json={"allowed_models": ["emb"]}, headers=ADMIN_H)
    assert r.status_code == 400, r.text
    assert "emb" in r.json()["detail"]


def test_put_accepts_chat_models(admin_env) -> None:
    """The chat-only rule must not become a blanket refusal."""
    tc, _fake, _r, _audited = admin_env
    r = tc.put(
        "/admin/routing/config",
        json={"decision_model": "llm-chat", "allowed_models": ["llm-big"]},
        headers=ADMIN_H,
    )
    assert r.status_code == 200, r.text


def test_put_audit_records_prompt_length_not_its_body(admin_env) -> None:
    """The audit trail is append-only and never pruned. Auditing a 20 000-char
    prompt verbatim would put a full prompt body in every row; the trail
    records its LENGTH instead."""
    tc, _fake, _r, audited = admin_env
    big = "p" * 18000
    r = tc.put("/admin/routing/config", json={"prompt": big}, headers=ADMIN_H)
    assert r.status_code == 200, r.text
    assert audited
    details = audited[-1]["details"]
    assert details["values"]["prompt"] == "<18000 chars>"
    assert big not in str(details)


# ---------------------------------------------------------------------------
# R3 (audit 2026-10-29): the 2026-10-28 global-arming path added a ladder but
# no routing_min_model knob, so a fleet-armed heuristic router was pinned to
# the ceiling and could NEVER downgrade -- the feature the panel just enabled
# was inert for every floorless profile. The knob rides the DEFAULTS panel
# surface; model_routing.global_min_model reads it.
# ---------------------------------------------------------------------------


def test_r3_routing_min_model_in_defaults_and_copy() -> None:
    import pcai_llm.core.routing_config as rc

    assert rc.DEFAULTS.get("routing_min_model") == ""
    d = rc.defaults()
    assert d["routing_min_model"] == ""
    # mutating the copy must not touch the module default (existing contract)
    d["routing_min_model"] = "X"
    assert rc.DEFAULTS["routing_min_model"] == ""


async def test_r3_set_values_roundtrips_the_global_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The panel write path must accept and store the new knob."""
    import pcai_llm.core.routing_config as rc

    class _Conn:
        def __init__(self) -> None:
            self.queries: list[Any] = []

        async def execute(self, *a: Any, **k: Any) -> None:
            self.queries.append(a)

        def transaction(self) -> Self:
            return self

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    conn = _Conn()

    class _Acquire:
        async def __aenter__(self) -> _Conn:
            return conn

        async def __aexit__(self, *exc: object) -> None:
            return None

    def _acquire_bounded() -> _Acquire:  # async-CM factory, matching db.acquire_bounded
        return _Acquire()

    async def _fake_fetch(*a: Any, **k: Any) -> list[Any]:
        return [{"key": "routing_min_model", "value": rc._encode("GLM-5.3-Flash")}]

    monkeypatch.setattr(rc.db, "acquire_bounded", _acquire_bounded)
    monkeypatch.setattr(rc.db, "fetch", _fake_fetch)
    await rc.invalidate()
    try:
        merged = await rc.set_values({"routing_min_model": "GLM-5.3-Flash"})
        assert merged["routing_min_model"] == "GLM-5.3-Flash"
        # the key rides as $1 parameter: (sql, key, encoded_value)
        assert any(len(q) >= 2 and q[1] == "routing_min_model" for q in conn.queries)
    finally:
        await rc.invalidate()
