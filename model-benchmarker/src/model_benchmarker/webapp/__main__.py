"""python -m model_benchmarker.webapp -> uvicorn serving the PCAI app."""

from __future__ import annotations

import argparse
import os


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="model-benchmarker-web",
        description="Serve the ModelBenchmarker web app (memory estimator + benchmark launchers).",
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--work-dir", default=None, help="run-artifact root (env BENCH_WORK_DIR, default /data)")
    args = parser.parse_args()

    if args.work_dir:
        os.environ["BENCH_WORK_DIR"] = args.work_dir

    import uvicorn

    from .app import create_app  # imported AFTER the env is set: create_app() reads it

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
