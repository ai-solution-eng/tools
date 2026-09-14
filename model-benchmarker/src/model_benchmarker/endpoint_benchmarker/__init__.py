"""endpoint_benchmarker — the universal load benchmarker of ModelBenchmarker.

Vendored from the standalone ``EndpointBenchmarker`` project (itself a
generalization of ``MultimodalRAG/tests/benchmark.py``): N simulated users
looping over a reference query pool against

* **any REST endpoint** — arbitrary method / path template / query params /
  JSON body template / headers;
* **any MCP server** — tool discovery via ``list_tools``, auto argument
  building from the tool's input schema, streamable-http or SSE transport,
  and *no second REST URL required*;

plus a concurrency-sweep mode (``--sweep 1,4,32,64``) that records a
wall-clock window per level and — when a Prometheus URL is given — joins
GPU telemetry (DCGM) averaged over each level's window, producing the
"scaling curve" used for hosted-trial capacity discussions.

Artifacts: JSON run artifact, per-level CSV, self-contained HTML report, and
an easy-to-read Markdown report in the ModelBenchmarker results-tree
convention (``--md``), which ``results_to_html.py`` ingests unchanged.
This is the roadmap home for the planned agent-benchmarking fusion: chat
streaming (TTFT/tokens-per-second) as a target mode, then tool-attached
agent runs measured as span trees with per-phase time attribution.
"""

from __future__ import annotations

__version__ = "0.2.0"
