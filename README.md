# Codex Context Foundation

[Download the pilot package](./codex-context-foundation-0.1.0-pre3.public1.zip) · [SHA-256 checksum](./SHA256SUMS)

The ZIP contains the Python source, installer, tests, README, license and integrity manifest.

Experimental, local-first context and project-memory tools for Codex. This is an independent community project, not an official OpenAI product.

**Pilot / pre-release, not a finished autonomous context optimizer.** Token or subscription-quota savings have not been demonstrated. Exact literal searches may be more compact with `rg`.

## Included

- Local STDIO MCP service: bounded document/code retrieval, evidence-linked Markdown records, checkpoints, coordinated handoff, and an operation journal.
- Per-working-copy state isolation. Existing documents remain authoritative; indexes are derived data.
- Advisory session lifecycle hooks and a reversible, versioned installer.
- Unit tests and a SHA-256 file manifest.

No user projects, conversations, runtime state, credentials, or personal configuration are distributed. There are no model API calls or dependency downloads during installation. Serena and Graphify are not installed. A legacy experimental Serena supervisor remains in `foundation.py` but is not used by the baseline service.

## Requirements

- Python 3.11+ (not bundled), including SQLite with FTS5.
- macOS or Linux; tests were run on macOS. Windows is not qualified.
- Codex supporting STDIO MCP and lifecycle hooks.
- Git for Git project identity checks.

## Install the pilot

Download and extract the ZIP, open a terminal in its `codex-context-foundation` directory, then:

```sh
python3 install.py verify
python3 install.py install --activate --pilot
```

Review the generated hook commands in Codex settings and explicitly approve only those you trust. Hooks can execute outside the sandbox. The installer does not bypass trust review, copy authentication, or force-enable hooks you disabled. Existing settings are preserved outside the managed integration. If an open task still uses the old MCP connection, reconnect this server and verify the live version.

The public packaging revision has a distinct version to avoid overwriting an existing immutable pilot release. It changes the version, public documentation and a diagnostic label; it does not upgrade any local installation automatically.

## Verify and disconnect

```sh
python3 -m unittest -q test_foundation test_install test_context_service test_lifecycle
python3 install.py doctor
python3 install.py disconnect
```

`verify` checks package integrity, not publisher authenticity. `doctor` checks local configuration, not live MCP connectivity or hook trust. `disconnect` removes this integration without deleting project files or saved state. Review `python3 install.py --help` for custom install locations.

## Limits and safety

- Hooks store bounded event metadata, not full transcripts, tool output or commands. Meaningful notes and checkpoints still require agent actions. Shell edits are not fully tracked.
- Handoff coordinates ownership but does not create Codex tasks, authenticate callers, or guarantee automatic resumption. Host tools and sometimes user approval are needed.
- Existing canonical operation journals are not replaced. Some worktree/journal configurations reject a second journal until routing is configured; this is not a universal journal adapter.
- Source reads reject symlinks, hard links, hidden/private paths, large files and unsupported types within the service. This is not an OS sandbox or protection against a malicious process under the same user account.
- Evidence references are not automatically verified. No guarantee of zero information loss or regression prevention is made.
- Compatibility with future Codex versions and every platform is not guaranteed. No online update monitor is included.
- The predecessor pilot passed local tests and live MCP/session-hook checks. Those results do not qualify every subsequent build or all lifecycle events. Test this package before relying on it.

## License

MIT. See LICENSE.
