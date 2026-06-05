# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability in the Kalshi Trading Bot, please
report it **privately** — do not open a public issue.

- Preferred: [GitHub Security Advisories](https://github.com/Viprasol-Tech/kalshi-trading-bot/security/advisories/new)
- Email: [support@viprasol.com](mailto:support@viprasol.com)

We aim to acknowledge reports within **72 hours** and to provide a fix or
mitigation timeline after triage. Please give us a reasonable window to address
the issue before any public disclosure.

## Supported versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅        |

## Handling secrets

This project authenticates to Kalshi with an **API key ID** and an **RSA private
key file**. To keep your credentials safe:

- **Never commit** `.env`, `*.pem`, `*.key`, or anything under `secrets/` — these
  are excluded by `.gitignore` and a `detect-private-key` pre-commit hook.
- Store the RSA private key as a file on disk with restrictive permissions; pass
  its path via `KALSHI_PRIVATE_KEY_PATH`.
- Rotate keys immediately if you suspect exposure (Kalshi dashboard → API Keys).
- Keys are never logged; telemetry must not print full credentials.
- Start in `KALSHI_ENVIRONMENT=demo` with `KALSHI_DRY_RUN=true`.

Maintained by [Viprasol Tech Private Limited](https://viprasol.com).
