"""One-shot runner: rebuild DB, discover schema, mine patterns, run agent demo.

Usage:
    python run.py                   # full pipeline + scripted demo
    python run.py live              # full pipeline + live Claude agent
    python run.py demo              # just the demo (assume DB exists)
    python run.py inspect           # dump current state of patterns + obs log
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Ensure UTF-8 stdout on Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def step(label: str) -> None:
    print(f"\n{'='*70}\n {label}\n{'='*70}", flush=True)


def run(cmd: list[str]) -> None:
    print(f"[run] {' '.join(cmd)}", flush=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    subprocess.run(cmd, check=True, cwd=ROOT, env=env)


def pipeline_full() -> None:
    step("1/5  Build messy mock ERP (SQLite)")
    run([sys.executable, "mock_erp/seed.py"])

    step("2/5  Introspect + semantically map the schema")
    run([sys.executable, "discovery/introspect.py"])
    run([sys.executable, "-m", "discovery.semantic_map"])

    step("3/5  Render the discovered concept catalog (with structural confidence)")
    run([sys.executable, "-m", "cli.render_concepts"])

    step("4/5  Mine workflow observations into LEARNED_PATTERNS")
    run([sys.executable, "-m", "learner.miner"])

    step("5/5  Run agent demo")
    mode = "live" if (len(sys.argv) > 1 and sys.argv[1] == "live") else "scripted"
    run([sys.executable, "agent/demo.py", mode])


def demo_only() -> None:
    run([sys.executable, "agent/demo.py", "scripted"])


def inspect() -> None:
    import json
    import sqlite3
    conn = sqlite3.connect(ROOT / "data" / "erp.db")
    conn.row_factory = sqlite3.Row

    step("LEARNED_PATTERNS")
    for r in conn.execute(
        "SELECT KIND, KEY, SUPPORT, CONFIDENCE, VALUE_JSON, LEARNED_AT "
        "FROM LEARNED_PATTERNS"
    ):
        print(f"  [{r['KIND']:10s}] {r['KEY']:30s} "
              f"support={r['SUPPORT']:<3d} conf={r['CONFIDENCE']:.2f}")
        print(f"     value={json.loads(r['VALUE_JSON'])}")

    step("WORKFLOW_OBS — most recent 15")
    for r in conn.execute(
        "SELECT OBS_ID, TS, ACTOR, TOOL, OUTCOME, FEEDBACK FROM WORKFLOW_OBS "
        "ORDER BY OBS_ID DESC LIMIT 15"
    ):
        print(f"  #{r['OBS_ID']:<4d} {r['TS']}  {r['ACTOR']:<10s} "
              f"{r['TOOL']:<28s} {r['OUTCOME']}"
              + (f"  feedback={r['FEEDBACK']!r}" if r['FEEDBACK'] else ""))

    conn.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        demo_only()
    elif len(sys.argv) > 1 and sys.argv[1] == "inspect":
        inspect()
    else:
        pipeline_full()


if __name__ == "__main__":
    main()
