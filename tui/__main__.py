"""CLI entry point for the STAMMTISCH TUI."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="stammtisch-tui",
        description="STAMMTISCH Quantitative Workstation",
    )
    parser.add_argument("--binary", help="Path to stammtisch-core binary")
    parser.add_argument("--state-root", help="STAMMTISCH_HOME state root")
    parser.add_argument("--pipeline-dir", help="Pipeline spec directory")
    parser.add_argument("--skip-boot", action="store_true", help="Skip boot animation")
    parser.add_argument("--deepseek-key", help="DeepSeek API key (or set DEEPSEEK_API_KEY env)")
    args = parser.parse_args()

    from .app import run_tui

    key = args.deepseek_key or os.environ.get("DEEPSEEK_API_KEY")
    run_tui(
        binary=args.binary,
        state_root=args.state_root,
        pipeline_dir=args.pipeline_dir,
        skip_boot=args.skip_boot,
        deepseek_key=key,
    )


if __name__ == "__main__":
    main()
