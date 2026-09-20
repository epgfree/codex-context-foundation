"""Build an allowlisted source distribution; never package state or project data."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

from install import VERSION

FILES = ("context_service.py", "platform_fs.py", "foundation.py", "lifecycle.py", "install.py", "README.md", "LICENSE", "test_context_service.py", "test_foundation.py", "test_install.py", "test_lifecycle.py", "test_platform_fs.py", "test_portability.py")


def build(source: Path, output: Path) -> dict:
    content = {name: (source / name).read_bytes() for name in FILES}
    manifest = {"version": VERSION, "qualified_for_activation": False,
                "qualified_for_pilot": True,
                "supported_platforms": ["darwin", "linux", "win32"], "requires_python": ">=3.11",
                "imports_project_data": False,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in content.items()}}
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Choose a new artifact path; existing bundle is preserved")
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        def add(name, data):
            entry = zipfile.ZipInfo(f"codex-context-foundation/{name}", date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
        for name, data in content.items():
            add(name, data)
        add("package-manifest.json", json.dumps(manifest, indent=2) + "\n")
    return {"path": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "files": len(content) + 1,
            "qualified_for_activation": False, "qualified_for_pilot": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(build(Path(__file__).resolve().parent, args.output)))
