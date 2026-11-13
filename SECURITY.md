# Security

This repository interacts with trading infrastructure and may be configured with wallet credentials.

## Never disclose secrets

- Never commit private keys, seed phrases, CLOB API secrets, passphrases, or real environment files.
- Keep real credentials in local files excluded by `.gitignore`.
- Use a dedicated wallet with limited funds for testing.
- If a credential is accidentally committed, revoke or rotate it immediately. Deleting the file in a later commit does not make the exposed secret safe.

## Reporting a vulnerability

For security issues that could expose credentials, funds, or account access, do not publish exploit details in a public issue. Contact the repository maintainer privately through the GitHub profile and include a concise description and reproduction steps where safe.

Please do not include live private keys, API secrets, or unredacted account information in any report.
