"""Query-granular tool search on the pcai_tools meta-tool (2026-11-23, B5).

WHY: `list` is keyed by SERVER, so "which tool deletes a dataset?" cost a whole
server catalogue in context -- the exact cost progressive disclosure exists to
avoid. `search` ranks the in-memory catalogue with BM25 over server name + tool
name + description and returns the same compact line shape `list` returns, with
the server named so the model can `describe` it next.

WHAT IS PINNED HERE:
  * ranking sanity -- "delete dataset" puts the dataset-delete tool FIRST, and a
    verb-only match (delete_pod) still appears, lower;
  * the additive contract -- `list`/`describe` output is BYTE-identical to the
    pre-change strings (pinned literally below) and `search` never promotes;
  * the three non-answer cases each with their own explicit text: empty query,
    no matches, and search DISABLED by `tool_search_max_results <= 0`. A
    disabled action must say it is disabled: an empty result reads as "no such
    tool" and sends the model hunting for a capability it has.
"""

from __future__ import annotations

import pytest

from pcai_llm.config import settings
from pcai_llm.core import tool_surface as ts


def _catalog() -> list[dict]:
    """A miniature fleet catalogue: five servers with overlapping VERBS.

    Deliberately realistic -- the trap this feature must survive is a query whose
    verb ("delete") exists on several servers, where only the NOUN says which one
    the model meant.
    """
    return [
        {"server": "mm-rag", "tool": "search_datasets", "description": "Search RAG datasets by name"},
        {"server": "mm-rag", "tool": "delete_dataset", "description": "Delete a dataset and every file in it"},
        {"server": "mm-rag", "tool": "list_dataset_files", "description": "List the files stored in one dataset"},
        {"server": "mm-rag", "tool": "ingest_document", "description": "Upload a document into a dataset"},
        {"server": "k8s-ops", "tool": "delete_pod", "description": "Delete a pod so its controller recreates it"},
        {"server": "k8s-ops", "tool": "list_pods", "description": "List pods in a namespace"},
        {"server": "k8s-ops", "tool": "describe_resource", "description": "Describe one Kubernetes resource"},
        {"server": "prometheus", "tool": "prom_query", "description": "Run a PromQL query"},
        {"server": "prometheus", "tool": "prom_alerts", "description": "List firing alerts"},
        {"server": "logsearch", "tool": "search_logs", "description": "Regex search logs across every pod"},
        {"server": "logsearch", "tool": "count_matches", "description": "Count regex matches over a window"},
        {
            "server": "applygate",
            "tool": "plan_apply",
            "description": "Dry-run a manifest apply and return the plan",
        },
        {
            "server": "applygate",
            "tool": "delete_manifest",
            "description": "Delete the resources a manifest created",
        },
    ]


def _search(query: str, **kwargs: object) -> tuple[str, set[str]]:
    args: dict = {"action": "search", "query": query}
    args.update(kwargs)
    return ts.handle_meta_call(args, _catalog(), max_results=5)


def _hit_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.startswith("- ")]


# ---------------------------------------------------------------------------
# ranking sanity
# ---------------------------------------------------------------------------


def test_delete_dataset_query_ranks_the_dataset_delete_tool_first() -> None:
    """The motivating query: a VERB on several servers, a NOUN on exactly one."""
    hits = ts.rank_tools(_catalog(), "delete dataset", limit=5)
    assert hits[0]["server"] == "mm-rag"
    assert hits[0]["tool"] == "delete_dataset"
    # the verb-only matches are still offered, BELOW the one that matches both
    names = [h["tool"] for h in hits]
    assert "delete_pod" in names and "delete_manifest" in names
    assert names.index("delete_dataset") < names.index("delete_pod")


def test_search_ranks_by_relevance_not_catalogue_order() -> None:
    """`prom_alerts` sits late in the catalogue; a query for alerts finds it."""
    hits = ts.rank_tools(_catalog(), "firing alerts", limit=3)
    assert hits[0]["tool"] == "prom_alerts"


def test_plural_fold_matches_singular_and_plural_queries_alike() -> None:
    """A query for "dataset" must reach `search_datasets`: one trailing-s fold,
    applied on BOTH sides, is the whole mechanism (no stemmer dependency)."""
    singular = ts.rank_tools(_catalog(), "dataset", limit=10)
    plural = ts.rank_tools(_catalog(), "datasets", limit=10)
    assert [h["tool"] for h in singular] == [h["tool"] for h in plural]
    assert "search_datasets" in [h["tool"] for h in singular]


def test_ranking_is_deterministic_and_ties_break_on_name() -> None:
    """Same catalog, different iteration order -> identical ranking.

    A tie that depended on dict order would make the answer unreproducible
    across pods, which is exactly what a release gate cannot tolerate.
    """

    def key(hit: dict) -> tuple[str, str]:
        return (hit["server"], hit["tool"])

    forward = ts.rank_tools(_catalog(), "list", limit=10)
    backward = ts.rank_tools(list(reversed(_catalog())), "list", limit=10)
    assert [key(h) for h in forward] == [key(h) for h in backward]
    assert forward, "the fixture must actually match the query"


def test_rank_tools_returns_nothing_for_an_empty_or_stopword_only_query() -> None:
    assert ts.rank_tools(_catalog(), "", limit=5) == []
    assert ts.rank_tools(_catalog(), "   ", limit=5) == []
    assert ts.rank_tools([], "delete", limit=5) == []


# ---------------------------------------------------------------------------
# output shape (the same compact shape `list` returns, with the server named)
# ---------------------------------------------------------------------------


def test_search_returns_compact_lines_with_the_server_named() -> None:
    text, _ = _search("delete dataset")
    lines = text.splitlines()
    assert lines[0] == 'search "delete dataset": 5 of 13 tool(s), most relevant first'
    assert lines[1] == "- mm-rag/delete_dataset: Delete a dataset and every file in it"
    # the next step is spelled out, and it names a server `describe` accepts
    assert lines[-1] == "next: describe mm-rag for full parameter schemas"


def test_search_descriptions_are_capped_like_list_lines() -> None:
    """The line shape is `list`-compact: a 400-char description is truncated."""
    catalog = [
        {"server": "verbose", "tool": "delete_thing", "description": "Delete a thing. " + "x" * 400},
    ]
    text, _ = ts.handle_meta_call({"action": "search", "query": "delete"}, catalog, max_results=5)
    line = _hit_lines(text)[0]
    assert len(line) < ts.LIST_DESCRIPTION_CHARS + 40
    assert "\u2026" in line


def test_search_honours_the_result_cap() -> None:
    text, _ = _search("delete")
    assert len(_hit_lines(text)) == 3  # delete_dataset, delete_pod, delete_manifest
    capped, _ = ts.handle_meta_call({"action": "search", "query": "delete"}, _catalog(), max_results=2)
    assert len(_hit_lines(capped)) == 2
    assert capped.splitlines()[0].startswith('search "delete": 2 of 13')


def test_search_never_promotes() -> None:
    """`describe` is the promotion path; search is read-only discovery."""
    _text, promoted = _search("delete dataset")
    assert promoted == set()
    _text2, kept = ts.handle_meta_call({"action": "search", "query": "pod"}, _catalog(), {"prometheus"}, max_results=5)
    assert kept == {"prometheus"}


def test_search_can_be_scoped_to_one_server() -> None:
    text, _ = _search("delete", server="k8s-ops")
    assert [line.split(":")[0] for line in _hit_lines(text)] == ["- k8s-ops/delete_pod"]
    assert text.splitlines()[0] == 'search "delete": 1 of 3 tool(s), most relevant first'


def test_search_unknown_server_names_the_real_ones() -> None:
    text, _ = _search("pod", server="k8z-ops")
    assert text.startswith("error:")
    assert "k8z-ops" in text
    assert "applygate" in text and "mm-rag" in text


# ---------------------------------------------------------------------------
# the three non-answer cases, each explicit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_an_explicit_error(query: str) -> None:
    text, _ = _search(query)
    assert text.startswith("error:")
    assert "query" in text


def test_missing_query_is_an_explicit_error() -> None:
    text, _ = ts.handle_meta_call({"action": "search"}, _catalog(), max_results=5)
    assert text.startswith("error:")


def test_non_string_query_is_rejected_not_stringified() -> None:
    """Searching for the repr of a list answers a question nobody asked."""
    text, _ = ts.handle_meta_call({"action": "search", "query": ["delete", "dataset"]}, _catalog(), max_results=5)
    assert text == "error: query must be a string"


def test_no_matches_says_so_and_points_at_list() -> None:
    """A genuine no-match is INFORMATION (not an error) and must be unambiguous."""
    text, _ = _search("quantum flux capacitor")
    assert text.startswith("no tools matched")
    assert "13 tool(s) searched" in text
    assert "'list'" in text


@pytest.mark.parametrize("cap", [0, -3])
def test_disabled_search_is_an_explicit_error_not_an_empty_result(cap: int) -> None:
    """`tool_search_max_results <= 0` disables the action and SAYS so.

    Returning an empty hit list here would read as "no such tool" and send the
    model hunting for a capability it actually has.
    """
    text, _ = ts.handle_meta_call({"action": "search", "query": "delete dataset"}, _catalog(), max_results=cap)
    assert text.startswith("error:")
    assert "disabled" in text
    assert f"tool_search_max_results={cap}" in text
    assert "no tools matched" not in text


def test_disabled_search_does_not_silently_fall_through_to_a_listing() -> None:
    """A disabled action must not answer a different question than it was asked."""
    text, _ = ts.handle_meta_call({"action": "search", "query": "pod"}, _catalog(), max_results=0)
    assert "mm-rag:" not in text  # that would be the `list` shape
    assert "list_pods:" not in text


# ---------------------------------------------------------------------------
# the production wiring: the cap comes from settings
# ---------------------------------------------------------------------------


def test_the_setting_exists_and_is_an_int() -> None:
    assert isinstance(settings.tool_search_max_results, int)


def test_search_cap_comes_from_settings_when_the_call_site_passes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """toolrelay calls handle_meta_call WITHOUT a cap; the setting must apply."""
    monkeypatch.setattr(settings, "tool_search_max_results", 2)
    text, _ = ts.handle_meta_call({"action": "search", "query": "delete dataset"}, _catalog())
    assert len(_hit_lines(text)) == 2


def test_the_setting_can_disable_search_for_the_wired_call_site(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_search_max_results", 0)
    text, _ = ts.handle_meta_call({"action": "search", "query": "pod"}, _catalog())
    assert text.startswith("error:")
    assert "disabled" in text


# ---------------------------------------------------------------------------
# additivity: list / describe are BYTE-identical, and the spec advertises search
# ---------------------------------------------------------------------------


def _cat3() -> list[dict]:
    return [
        {
            "server": "prometheus",
            "tool": "prom_query",
            "description": "Run a PromQL query",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
        {
            "server": "prometheus",
            "tool": "prom_alerts",
            "description": "List firing alerts",
            "inputSchema": {"type": "object"},
        },
        {"server": "k8s-ops", "tool": "list_pods", "description": "List pods", "inputSchema": {}},
    ]


LIST_ALL = (
    "k8s-ops:\n  list_pods: List pods\nprometheus:\n  prom_alerts: List firing alerts\n  prom_query: Run a PromQL query"
)
LIST_ONE = "prometheus (2 tools):\nprom_alerts: List firing alerts\nprom_query: Run a PromQL query"


def test_list_output_is_byte_identical_to_the_pre_search_surface() -> None:
    text, promoted = ts.handle_meta_call({"action": "list"}, _cat3(), max_results=5)
    assert text == LIST_ALL
    assert promoted == set()
    text_one, _ = ts.handle_meta_call({"action": "list", "server": "prometheus"}, _cat3(), max_results=5)
    assert text_one == LIST_ONE


def test_describe_output_is_byte_identical_to_the_pre_search_surface() -> None:
    text, promoted = ts.handle_meta_call({"action": "describe", "server": "prometheus"}, _cat3(), max_results=5)
    assert promoted == {"prometheus"}
    expected = (
        '[{"tool": "prom_alerts", "description": "List firing alerts",'
        ' "parameters": {"type": "object"}}, {'
        '"tool": "prom_query", "description": "Run a PromQL query", "parameters":'
        ' {"type": "object", "properties": {"query": {"type": "string"}}}}]'
    )
    assert text == expected


def test_describe_narrowing_and_error_strings_are_unchanged() -> None:
    text, _ = ts.handle_meta_call(
        {"action": "describe", "server": "prometheus", "tools": ["prom_alerts"]}, _cat3(), max_results=5
    )
    assert '"tool": "prom_alerts"' in text and "prom_query" not in text
    assert ts.handle_meta_call({"action": "nope"}, _cat3(), max_results=5)[0] == (
        "error: unknown action 'nope' (expected 'list', 'search' or 'describe')"
    )
    assert ts.handle_meta_call({"action": "describe"}, _cat3(), max_results=5)[0] == (
        "error: describe requires a server"
    )
    assert "unknown server" in ts.handle_meta_call({"action": "list", "server": "x"}, _cat3(), max_results=5)[0]
    assert ts.handle_meta_call("not json", _cat3(), max_results=5)[0] == (
        "error: pcai_tools arguments must be a JSON object"
    )


def test_meta_tool_spec_advertises_search_and_still_advertises_the_rest() -> None:
    spec = ts.meta_tool_spec(_cat3())
    params = spec["function"]["parameters"]
    assert params["properties"]["action"]["enum"] == ["list", "search", "describe"]
    assert params["properties"]["query"]["type"] == "string"
    desc = spec["function"]["description"]
    for line in ("list              ->", "list <server>     ->", "search <query>    ->", "describe <server> ->"):
        assert line in desc
    # the server directory is unchanged by the new action
    assert "- k8s-ops (1 tools)" in desc
