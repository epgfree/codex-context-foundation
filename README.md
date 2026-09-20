# Codex Context Foundation

Independent, local-first project memory and bounded retrieval for Codex. Not an official OpenAI product.

**Pre5 is a locally validated retrieval pilot.** macOS/Python 3.14 completed 130 discovered tests (110 executed, 20 Windows-specific skips) and the packaged integration scenario, including paged search/read across process restarts. The previous pre4 release passed the [six-job acceptance matrix](https://github.com/epgfree/codex-context-foundation/actions/runs/35506131849) on Windows, Linux and macOS with Python 3.11 and 3.14. That result does not qualify pre5 on those platforms or prove full Codex desktop integration. No measured subscription-quota savings are promised.

## What it does

- Bounded project-local document/code retrieval; Markdown records with evidence references.
- Persistent checkpoints, explicit ownership handoff and a journal that blocks uncertain repeats.
- Per-working-copy isolation and advisory session hooks.
- Reversible installer preserving unrelated Codex settings.

Existing wiki/source documents remain authoritative. No projects, conversations, credentials, personal configuration or runtime databases are distributed. Installation does not download dependencies or call a model API. Serena and Graphify are not installed. The legacy experimental Serena supervisor is not the baseline entrypoint.

## Requirements

- Python 3.11+ with SQLite FTS5, installed separately.
- Codex supporting STDIO MCP and lifecycle hooks.
- Git for Git project identity checks.
- Windows native installation uses a local NTFS volume and Windows PowerShell. Reparse points/junctions and unsafe state permissions are refused. WSL is a separate Linux installation, not the native Windows path.
- macOS/Linux use POSIX file-descriptor-based protections.
- Network shares and Windows device/alternate-stream paths are not supported.

## Install

Download [the latest pre5 installation ZIP](https://github.com/epgfree/codex-context-foundation/raw/refs/heads/main/codex-context-foundation-0.1.0-pre5.zip) and verify [SHA256SUMS-pre5.txt](https://github.com/epgfree/codex-context-foundation/blob/main/SHA256SUMS-pre5.txt). Extract it and run the commands below inside its `codex-context-foundation` folder. The ZIP is the exact locally tested package; its bundled README preserves the documentation snapshot at build time. This repository README carries publication and CI updates. The [pre4 archive](https://github.com/epgfree/codex-context-foundation/raw/refs/heads/main/codex-context-foundation-0.1.0-pre4.zip) remains available for rollback. For a source checkout, first run `python bundle.py package.zip` and extract the generated package.

Windows (PowerShell):

```powershell
py -3 install.py verify
py -3 install.py install --activate --pilot
```

macOS / Linux:

```sh
python3 install.py verify
python3 install.py install --activate --pilot
```

Python is not bundled; this is not a self-contained executable. The installer chooses the current user's directories and the running Python interpreter. It never requests administrator access or changes PowerShell execution policy. Hook approval remains an explicit Codex user action.

Review the generated hooks in Codex settings before granting trust. Hooks can execute outside the sandbox. If a running task keeps an older MCP connection, reconnect this server and verify its version. Do not disable protection or trust unrelated hooks.

## Diagnostics and rollback

Use `py -3` instead of `python3` on Windows:

```sh
python3 install.py doctor
python3 install.py disconnect
```

`doctor` is read-only and does not prove live MCP connectivity or hook trust. `disconnect` removes the managed integration, not state or projects. Old versioned releases are preserved. For a previous release, run its installer with explicit pilot activation and review changed hooks again.

Default paths can be overridden with `--destination` and `--codex-dir`; `CODEX_HOME` is honored. Use separate installations for Windows and WSL. Do not manually share their SQLite state.

## Progressive retrieval (pre5)

- Prefer bounded local file reads or literal search for a known path, name or string. Use memory retrieval for project decisions and unfinished work; choosing local search does not disable saving decisions/checkpoints.
- `context_search` defaults to three previews. When `has_more` is true, repeat the same query with the returned `cursor`. For an exhaustive request, a larger explicit `limit` (up to 10) can reduce round trips. A preview is not a full document.
- `context_read` accepts a source `path` or a legacy database `note_id`. It returns `text`, a source `revision`, `next` and `end`. Concatenate text pages without trimming. Repeat with the same source and `cursor=next` until `end=true`; the default page is 2,000 Unicode characters, at most 8,000. Legacy-note pages concatenate to JSON containing the persistent note fields.
- Continuations are bound to the project/request and source content. Edits, deletion or unavailable sources cause an explicit restart requirement, not a silent mixture of old and new pages. Restart retrieval and recheck decisions based on the old evidence.
- `has_more=false` only exhausts matching indexed records. `coverage.coverage_limited`, skipped files and source-change warnings still apply; absence of a result is not proof of absence. Files outside the approved scope or size budget remain unavailable through this tool.
- Full canonical documents stay on disk. Existing notes, ownership, checkpoints and uncertain-operation protections are retained. There is no automatic summary replacement or imported conversation history.
- Index freshness still verifies source contents. No mtime-only cache was added; filesystem protections take precedence over faster repeated scans.

Developer comparisons: `python3 compare_retrieval.py --baseline /path/to/extracted/pre4 --evidence /path/to/report.json` and `python3 check_retrieval.py --evidence /path/to/literal-report.json`. Fixtures are synthetic and isolated. Report first-page and exhaustive payloads, round trips, startup context and tool-catalog overhead separately. Bytes are not model tokens, cached-input charges, subscription limits or end-to-end answer-quality evidence.

## Validation

Run these from a source checkout (the installation ZIP does not include the CI harness):

```sh
python3 -m unittest discover -v
python3 ci_smoke.py
```

CI runs Python 3.11 and 3.14 on Windows, Linux and macOS. Tests cover packaged installation into a temporary profile, unchanged unrelated configuration, UTF-8 paths and memory, real STDIO restarts, handoff and disconnect. Windows-specific tests cover filesystem and native hook execution. OS-specific skips are reported, not counted as proof of that platform.

The pre4 matrix runs 107 unit tests per job plus the packaged integration scenario. Platform-specific skips remain explicit (for example, Windows API tests are skipped on POSIX). CI does not run the full Codex desktop app or bypass its approval system. Windows runners use an elevated CI account; the installer itself never requests elevation. A user's actual desktop installation still requires normal hook review and a live connection check.

## Limits

- Hooks keep bounded metadata, not transcripts, tool output or commands. Agents must still write meaningful decisions and checkpoints. Shell edits are not fully tracked.
- Handoff does not create new Codex tasks or start model turns. Owner strings coordinate tasks, not authenticate users. Fully automatic task rotation is not implemented.
- Evidence references are not independently verified. No guarantee of zero information loss or automatic regression prevention is made.
- Existing operation journals are not replaced; ambiguous worktree routing fails closed.
- File-scope and ACL checks are not an OS sandbox against administrators or malicious processes running as the same user.
- Exact literal search may be smaller with `rg`; this service should not replace every search.
- Future Codex compatibility is not guaranteed. App-version discovery outside macOS may be unavailable; no online update monitor is included.

## Sources and license

[OpenAI hook contract](https://learn.chatgpt.com/docs/hooks) describes Windows command overrides and user trust. [GitHub Python CI](https://docs.github.com/en/actions/tutorials/build-and-test-code/python) describes the platform matrix.

MIT — see LICENSE.
