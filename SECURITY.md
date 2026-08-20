# Security Policy

Julie ChenBot is a Discord bot with an optional, narrow-scope Admin
API (see [docs/admin_api.md](docs/admin_api.md)) used only by the
separate Julie ChenBot Admin Dashboard. This document covers how to
report a security issue in this repository.

## Reporting a Vulnerability

Please report suspected vulnerabilities privately, not through a
public GitHub issue.

- Preferred: open a [GitHub Security Advisory](../../security/advisories/new)
  for this repository (repo maintainers only see the report until you
  choose to publish it).
- If private advisories aren't available to you, contact the
  repository owner directly through their GitHub profile.

Please include:

- A description of the issue and its impact.
- Steps to reproduce (a minimal example is ideal).
- Any relevant logs or request/response details, with secrets
  redacted.

Do not include real `ADMIN_API_KEY`, `DISCORD_TOKEN`, or other
credential values in a report, even a private one -- describe the
issue and let the maintainer rotate the affected value.

## Scope

In scope:

- The Discord bot (`bot.py`, `commands/`, `production/`, `services/`).
- The Admin API (`admin_api/`) and its authentication.
- Dependency and CI/CD configuration in this repository.

Out of scope:

- The separate Admin Dashboard frontend/backend repository.
- Discord platform issues (report those to Discord).
- Railway platform issues (report those to Railway).

## Supported Versions

This project does not maintain multiple released versions; security
fixes are applied to the current production branch.
