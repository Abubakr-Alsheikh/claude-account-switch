# Contributing

- Open an issue before a new command or a change in behavior, so we agree on the shape first.
- Run the tests: `python -m unittest discover -s tests -v`. They use a fake home, a fake token server and a fake
  `security` tool. Every change ships with a test.
- Never run claude-switch against your real profile while you develop. Set `CLAUDE_SWITCH_TEST_HOME` to a temp
  folder, as the tests do.
- Never commit a login, a token or a file from `~/.claude-switch/`.
- Standard library only, one file.
