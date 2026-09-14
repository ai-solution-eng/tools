"""Target specifications: *what* we benchmark.

Two target kinds, both constructible from CLI flags:

* :class:`RestTarget` — any REST endpoint: method, path template (may embed
  ``{dataset}``), query-string params, JSON body template with a ``{query}``
  placeholder, arbitrary headers.
* :class:`McpTarget` — any MCP server: endpoint URL, transport, tool name,
  tool arguments.  Unlike the original Multimodal RAG benchmark script, an
  MCP target needs **only one URL**: connectivity is checked by opening the
  MCP session itself and tool discovery happens via ``list_tools`` — no
  companion REST URL, no hardcoded ``:8000`` derivation.

MCP tool arguments are resolved at connect time from the tool's
``inputSchema``: the sampled reference query goes into the query-ish string
argument (auto-detected, or ``--query-arg``), ``--dataset`` fills a
dataset-ish argument, ``--arg k=v`` pins everything else.  Required schema
fields that cannot be filled this way are reported loudly instead of being
silently sent as empty strings.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Property names (lowercased) that the ``--dataset`` value may fill on an MCP tool.
DATASET_ARG_NAMES = ("dataset_name", "dataset", "dataset_id", "collection", "collection_name", "index")

# Property names (lowercased) considered the "sampled query" slot on an MCP tool,
# in priority order.
QUERY_ARG_NAMES = ("query", "q", "prompt", "text", "search", "question", "input", "message")

_SENSITIVE_KEY = re.compile(r"password|token|secret|authorization|api[-_]?key|credential", re.IGNORECASE)


class TargetError(ValueError):
    """Raised for an unusable target configuration (message is user-facing)."""


def parse_kv(pairs: list[str] | tuple[str, ...], flag: str) -> dict:
    """Parse repeatable ``KEY=VALUE`` flags into a dict.

    Values are JSON-parsed when they parse cleanly (``top_k=10`` -> int 10,
    ``use_reranker=true`` -> bool True), else kept as strings.
    """
    out: dict = {}
    for item in pairs:
        if "=" not in item:
            raise TargetError(f"{flag} expects KEY=VALUE, got: {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise TargetError(f"{flag} expects KEY=VALUE, got: {item!r}")
        try:
            out[key] = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            out[key] = value
    return out


def redact_secrets(data: dict) -> dict:
    """Return a copy of *data* with sensitive values masked (for JSON artifacts)."""
    out = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[k] = redact_secrets(v)
        elif isinstance(v, str) and _SENSITIVE_KEY.search(k):
            out[k] = "REDACTED" if v else v
        else:
            out[k] = v
    return out


def _schema_properties(tool) -> tuple[dict, list[str]]:
    """Extract (properties, required) from a Tool's inputSchema, defensively."""
    schema = getattr(tool, "input_schema", None) or {}
    props = schema.get("properties") or {}
    required = schema.get("required") or []
    return props, list(required)


def pick_query_arg(tool) -> str:
    """Choose which tool argument receives the sampled reference query."""
    props, required = _schema_properties(tool)
    if not props:
        raise TargetError(f"tool '{tool.name}' has no input properties; pass --query-arg explicitly")
    lowered = {name.lower(): name for name in props}
    for cand in QUERY_ARG_NAMES:
        if cand in lowered:
            name = lowered[cand]
            if name in required or not required:
                return name
    # fall back: first required string property, then any string property
    for name, spec in props.items():
        if name in required and isinstance(spec, dict) and spec.get("type") == "string":
            return name
    for name, spec in props.items():
        if isinstance(spec, dict) and spec.get("type") == "string":
            return name
    raise TargetError(
        f"could not infer which argument of tool '{tool.name}' receives the query "
        f"(properties: {', '.join(props)}); pass --query-arg explicitly"
    )


def pick_tool(tools, requested: str | None):
    """Pick the MCP tool to benchmark.

    ``requested`` given -> exact (case-insensitive) match, else error listing
    available tools.  Otherwise: if several tools exist, prefer one whose name
    matches the usual query-ish verbs; a single tool is used as-is.
    """
    if not tools:
        raise TargetError("server exposes no tools")
    if requested:
        by_lower = {t.name.lower(): t for t in tools}
        tool = by_lower.get(requested.lower())
        if tool is None:
            names = ", ".join(t.name for t in tools)
            raise TargetError(f"tool '{requested}' not found on server. Available tools: {names}")
        return tool
    if len(tools) == 1:
        return tools[0]
    verbs = ("search", "query", "ask", "retrieve", "find", "lookup")
    for t in tools:
        if any(v in t.name.lower() for v in verbs):
            return t
    names = ", ".join(t.name for t in tools)
    raise TargetError(
        f"server exposes {len(tools)} tools ({names}); pass --tool to choose one "
        f"(or --list-tools to inspect them)"
    )


# ---------------------------------------------------------------------------
# REST target
# ---------------------------------------------------------------------------


@dataclass
class RestTarget:
    base_url: str
    path_template: str = "/api/datasets/{dataset}/search"  # mm-script parity default
    method: str = "GET"
    query_param: str = "q"  # "" disables the sampled-query query param (POST body only)
    params: dict = field(default_factory=dict)
    body_template: str | None = None
    headers: dict = field(default_factory=dict)
    dataset: str | None = None
    health_path: str = "/healthz"
    http2: bool = False
    insecure: bool = False

    def validate(self) -> None:
        if "{dataset}" in self.path_template and not self.dataset:
            raise TargetError(
                f"path template {self.path_template!r} contains {{dataset}} but no --dataset was given"
            )
        if self.body_template is not None:
            # Fail fast on a body template that cannot produce valid JSON.
            try:
                self._render_body('probe "quoted" \\ and newline\n')
            except TargetError as exc:
                raise TargetError(f"invalid --body template: {exc}") from exc
            if self.method == "GET":
                raise TargetError("--body was given but --method is GET; use --method POST/PUT/PATCH (or drop --body)")
        if self.dataset and "{dataset}" not in self.path_template:
            # Allowed (headers/params may use it) but worth saying.
            pass

    def _render_body(self, query: str) -> bytes:
        assert self.body_template is not None
        # json.dumps(query)[1:-1] splices an escaped string value; the
        # template must place "{query}" inside quotes.
        escaped = json.dumps(query)[1:-1]
        rendered = self.body_template.replace("{query}", escaped)
        try:
            json.loads(rendered)
        except json.JSONDecodeError as exc:
            raise TargetError(f"rendered body is not valid JSON ({exc}): {rendered[:200]}") from exc
        return rendered.encode("utf-8")

    def build_request(self, query: str) -> tuple[str, dict, bytes | None]:
        """Return (url, query_params, body_bytes)."""
        path = self.path_template.replace("{dataset}", self.dataset or "")
        url = self.base_url.rstrip("/") + path
        params = dict(self.params)
        if self.query_param:
            params[self.query_param] = query
        body = self._render_body(query) if self.body_template is not None else None
        return url, params, body

    def describe(self) -> dict:
        return {
            "kind": "rest",
            "method": self.method,
            "url": self.base_url.rstrip("/") + self.path_template.replace("{dataset}", self.dataset or "{dataset}"),
            "query_param": self.query_param,
            "params": self.params,
            "body_template": self.body_template,
            "headers": self.headers,
            "dataset": self.dataset,
        }


# ---------------------------------------------------------------------------
# MCP target
# ---------------------------------------------------------------------------


@dataclass
class McpTarget:
    url: str
    transport: str = "streamable-http"  # or "sse"
    tool_name: str | None = None  # None -> auto-pick
    args: dict = field(default_factory=dict)  # from --arg (JSON-parsed values)
    query_arg: str | None = None  # None -> auto-detect from inputSchema
    dataset: str | None = None  # fills a dataset-ish required arg if present
    headers: dict = field(default_factory=dict)
    insecure: bool = False

    def describe(self) -> dict:
        return {
            "kind": "mcp",
            "url": self.url,
            "transport": self.transport,
            "tool": self.tool_name,
            "query_arg": self.query_arg,
            "args": self.args,
            "headers": self.headers,
            "dataset": self.dataset,
        }
