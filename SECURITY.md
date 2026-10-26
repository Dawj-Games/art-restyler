# Security

Art Restyler is a local development tool for one trusted user. It serves project images and has operations that replace files. It binds to 127.0.0.1 and rejects unexpected browser origins and Host headers, but does not authenticate local processes. Do not expose it to the network, use it with untrusted local users, or open an untrusted catalog/configuration.

All working data and keys are excluded from Git. Keys are stored locally in plaintext, so protect your user account and backups. External generation services receive the art and prompts you submit to them. Inspect agent instructions and manifests before running them.

Report a vulnerability using this repository's GitHub Security page and **Report a vulnerability** if private reporting is available. Otherwise open an issue requesting a private contact without including an exploit, secrets, or private project data. Do not put credentials in public reports.
