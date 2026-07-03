"""
public.py — unified local-worker entrypoint for PAF-Model.

Selects the backend worker based on --backend and delegates to it. Each backend
keeps its own proven worker loop / browser pool / scraper unchanged:

    --backend deepseek  → public_deepseek.py  (account-name + email/password auth)
    --backend qwen      → public_qwen.py      (cookie-file auth)

The worker registers with the VPS including its "backend" field, so the unified
VPS gateway routes tasks to the right pool (see PublicForward/ForVPS/vps_server.py).

Run two processes (one per backend), exactly as before:

    python public.py --backend deepseek --vps ws://VPS_IP:PORT/ws/worker --workers 2
    python public.py --backend qwen     --vps ws://VPS_IP:PORT/ws/worker --workers 2

All flags after --backend are passed through unchanged to the selected worker.
Run `python public.py --backend deepseek --help` to see backend-specific flags.
"""
from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="public.py",
        description="PAF-Model unified local worker (backend dispatcher).",
        add_help=False,
    )
    parser.add_argument(
        "--backend",
        choices=["deepseek", "qwen"],
        help="Which backend this worker serves: 'deepseek' or 'qwen'.",
    )
    parser.add_argument(
        "-h", "--help", action="store_true", dest="_show_help",
        help="Show this message (or backend help when --backend is given).",
    )
    args, rest = parser.parse_known_args()

    if not args.backend:
        if args._show_help:
            parser.print_help()
            raise SystemExit(0)
        parser.error("--backend is required (choose 'deepseek' or 'qwen').")

    # Re-assemble argv for the delegated worker (drop --backend; keep --help).
    forwarded = list(rest)
    if args._show_help:
        forwarded.append("--help")
    sys.argv = [f"public_{args.backend}.py"] + forwarded

    if args.backend == "deepseek":
        from public_deepseek import main as worker_main
    else:
        from public_qwen import main as worker_main

    worker_main()


if __name__ == "__main__":
    main()
