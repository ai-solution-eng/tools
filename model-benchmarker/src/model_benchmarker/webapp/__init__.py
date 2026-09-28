"""webapp -- the ModelBenchmarker PCAI app: benchmarks from the browser.

A small FastAPI server serving the LLM memory estimator (the default page),
API-key-gated benchmark launcher pages (/benchmark/chat, /benchmark/endpoint)
and a subprocess run registry that fires the existing bench CLIs
(benchmark_chat / endpoint_benchmarker) and tracks their artifacts.
Targets are restricted to the operator-configured endpoint allowlist, one
benchmark per endpoint at a time. Deployed by helm/ (chart
model-benchmarker); see documentation/webapp.md.
"""

from __future__ import annotations
