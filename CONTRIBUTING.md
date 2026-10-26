# Contributing

Bug reports, portability fixes, provider adapters, and documentation improvements are welcome. Open an issue with your platform, Python and Unity versions, minimal reproduction steps, expected behavior, and redacted logs. Do not upload private game artwork, keys, catalogs, or backups.

For development, create a virtual environment and install `requirements.txt`. Run:

```sh
python -m unittest discover -s tests -v
python -m compileall -q scanner.py engine.py server.py init_project.py batch_folder.py
```

Tests create synthetic Unity files in temporary directories; they never need an API key or the original game. CI runs on Windows, macOS, and Linux. Add meaningful regression coverage for changes to importing, application, rollback, GUID preservation, or project isolation.

Keep the default workflow local and provider-independent. Preserve user review, original backups, and Unity metadata. Keep game-specific conventions in local configuration. Document any integration changes in `docs/` and the changelog. Submit pull requests against `main` with the problem, resulting behavior, and validation performed.

By contributing, you agree to license your contribution under this repository's MIT License. Only contribute code and examples you are permitted to share.
