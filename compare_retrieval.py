"""Equal-fixture retrieval comparison in isolated interpreters; bytes, not quota.

No model calls, user-project writes or timing claims. Baseline must be supplied
explicitly. First-answer and exhaustive workloads are reported separately.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def worker(source):
    sys.path.insert(0, str(source.resolve()))
    from context_service import Project, TOOLS, VERSION
    from lifecycle import handle
    with tempfile.TemporaryDirectory(prefix="retrieval-comparison-") as td:
        base = Path(td).resolve()
        root = base / "fixture"
        (root / "docs").mkdir(parents=True)
        (root / "pyproject.toml").touch()
        for i in range(9):
            (root / "docs" / f"decision-{i:02}.md").write_text(
                f"# Choice {i:02}\n\n" + "Background engineering evidence. " * 80
                + f"\nSHAREDKEY ANSWER{i:02}: preserve ownership and verify source revision.\n"
                + "Supporting constraints remain in the full original document. " * 80,
                encoding="utf-8")
        project = Project(root, base / "state")
        initial = project.search("SHAREDKEY")
        assert "ANSWER" in json.dumps(initial), "Initial page lost all answers"
        assert not initial["coverage"]["coverage_limited"], initial
        pages = [initial]
        if "has_more" in initial:
            while pages[-1]["has_more"]:
                pages.append(project.search("SHAREDKEY", cursor=pages[-1]["cursor"]))
                assert len(pages) <= 12, "Pagination stalled"
        else:
            # The baseline has no continuation; include the original call's cost
            # plus the expanded call, rather than silently assuming completeness.
            pages.append(project.search("SHAREDKEY", limit=10))
        paths = {doc["path"] for page in pages for doc in page["documents"]}
        assert len(paths) == 9, paths
        combined = json.dumps(pages)
        assert all(f"ANSWER{i:02}" in combined for i in range(9))
        context = handle({"hook_event_name": "SessionStart", "session_id": "benchmark",
                          "cwd": str(root)}, base / "state", root)["hookSpecificOutput"]["additionalContext"]
        return {"version": VERSION, "first_answer_bytes": size(initial),
                "first_page_documents": len(initial["documents"]),
                "exhaustive_bytes": sum(size(page) for page in pages), "exhaustive_calls": len(pages),
                "unique_documents": len(paths), "answers_found": 9,
                "catalog_bytes": size(TOOLS), "startup_bytes": len(context.encode())}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.source:
        print(json.dumps(worker(args.source)))
        return
    if not args.baseline or not args.evidence:
        parser.error("--baseline and --evidence are required")
    results = {}
    for label, source in (("baseline", args.baseline), ("candidate", Path(__file__).parent)):
        process = subprocess.run([sys.executable, __file__, "--source", str(source)],
                                 capture_output=True, text=True, encoding="utf-8", timeout=60, check=True)
        results[label] = json.loads(process.stdout)
    results["limitations"] = ["Synthetic fixed answers, not model task accuracy",
        "UTF-8 compact payload bytes, not tokens or subscription quota",
        "Catalog/startup reported separately; compact JSON framing alone does not prove model savings",
        "Exhaustive retrieval can cost more calls/bytes than an explicitly larger page",
        "No cached-input, model-output or full agent-turn measurement"]
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results))


if __name__ == "__main__":
    main()
