# Notes for coding agents working on claude-account-switch

claude-switch swaps the login Claude Code uses. It handles real OAuth tokens, and Anthropic's refresh tokens work
once, so one careless run can log a person out.

If `private/AGENTS.md` exists, read it first. It holds the owner's local setup and is never committed.

## Safety

- Never run `claude_switch.py` without `CLAUDE_SWITCH_TEST_HOME` set to a fresh temp folder. Without it the tool
  reads and writes the real `~/.claude`, `~/.claude.json`, `~/.claude-switch/` and the macOS Keychain.
- Never call Anthropic or claude.ai addresses. Under a test home, `CLAUDE_SWITCH_TOKEN_URL` and
  `CLAUDE_SWITCH_USAGE_URL` point at the tests' fake server.
- Never read, print or copy a credentials file, and never put a token in output, a log, an exception or a commit. The
  tests check every output for the fake token prefix `tok-`.

## Layout

| Path | What |
|------|------|
| `claude_switch.py` | The whole tool. Keep it one file, standard library only, Python 3.9+ |
| `tests/test_claude_switch.py` | Tests: `python -m unittest discover -s tests -v` |
| `tests/fake_security.py` | A stand-in for macOS `/usr/bin/security` |

## Rules

- Every change ships with a test.
- Match Claude Code's lock names, order and stale times (see `claude_locks()` and `storage_lock()`). When Claude Code
  changes them, record the Claude Code version you checked in the code comment.
- Bump `__version__` and `version` in `pyproject.toml` together, and add the change to `CHANGELOG.md`.
- Write docs and messages in short, plain sentences in the active voice.
