"""Synthetic equal-answer retrieval probe; no model/API usage or user project writes.

Measures returned payload bytes, NOT tokens, subscription quota or agent quality.
Literal rg is the baseline, not artificially reading every file into model context.
This developer-only probe is intentionally not part of the installed package.
"""
import argparse
import json
from pathlib import Path
import shutil
import statistics
import subprocess
import tempfile
import time

from context_service import Project, TOOLS
from lifecycle import handle


def probe():
    rg = shutil.which("rg")
    if rg is None:
        raise RuntimeError("The comparison requires rg; no substitute baseline")
    measurements = []
    with tempfile.TemporaryDirectory(prefix="foundation-retrieval-") as temporary:
        base = Path(temporary).resolve()
        root = base / "fixture"
        (root / "docs").mkdir(parents=True)
        (root / "pyproject.toml").touch()
        for n in range(10):
            (root / "docs" / f"decision-{n}.md").write_text(
                f"# Decision {n}\n\n" + "Background: this fixture contains unrelated engineering notes.\n" * 60
                + f"\nCASE{n:02}: select OPTION{n:02} because explicit transitions avoid duplicate completion.\n"
                + "\nEvidence: fixture-only assertion, no production conclusion.\n")
        project = Project(root, base / "state")
        start = time.perf_counter()
        first = project.search("CASE00", limit=1)
        cold_ms = (time.perf_counter() - start) * 1000
        assert "OPTION00" in json.dumps(first)
        for n in range(10):
            query, expected = f"CASE{n:02}", f"OPTION{n:02}"
            durations = {"foundation": [], "rg": []}
            for _ in range(3):
                start = time.perf_counter()
                response = project.search(query, limit=1)
                durations["foundation"].append((time.perf_counter() - start) * 1000)
                payload = json.dumps(response, ensure_ascii=False)
                start = time.perf_counter()
                literal = subprocess.run([rg, "--no-heading", "--color", "never", "-n", "--", query, "docs"],
                                         cwd=root, text=True, capture_output=True, check=True, timeout=5).stdout
                durations["rg"].append((time.perf_counter() - start) * 1000)
                assert expected in payload and expected in literal
                assert len(response["documents"]) == 1 and not response["coverage"]["coverage_limited"]
            measurements.append({"case": n, "answer_found_by_both": True,
                "foundation_payload_bytes": len(payload.encode()), "rg_payload_bytes": len(literal.encode()),
                "foundation_median_ms": round(statistics.median(durations["foundation"]), 3),
                "rg_process_median_ms": round(statistics.median(durations["rg"]), 3)})
        started = time.perf_counter()
        startup = handle({"hook_event_name": "SessionStart", "session_id": "fixture-session", "cwd": str(root)},
                         base / "state", root)
        hook_ms = (time.perf_counter() - started) * 1000
        context = startup["hookSpecificOutput"]["additionalContext"]
        return {"scope": "synthetic literal queries, same expected answers; no LLM or user data",
            "cases_passed": len(measurements), "repeats_per_case": 3,
            "foundation_payload_bytes_total": sum(row["foundation_payload_bytes"] for row in measurements),
            "rg_payload_bytes_total": sum(row["rg_payload_bytes"] for row in measurements),
            "catalog_json_bytes": len(json.dumps(TOOLS).encode()),
            "startup_context_bytes": len(context.encode()), "startup_context_characters": len(context),
            "cold_search_ms": round(cold_ms, 3), "hook_in_process_ms": round(hook_ms, 3),
            "timing_limitations": "Foundation in-process excludes MCP transport; rg includes process startup. Hook excludes Python startup. Not end-to-end agent latency.",
            "token_or_quota_measurement": False, "measurements": measurements}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    result = probe()
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "measurements"}, ensure_ascii=False))
