# GlassOrchestrator Setup Guide (Validated)

This guide captures the installation path validated on a fresh Windows environment.

## 1) Prerequisites

Open a terminal in the repository root and verify Python launcher availability:

```powershell
py --version
```

If `py` is missing:

```powershell
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
```

Close and reopen the terminal, then verify `py --version` again.

## 2) Config Layering (How Fallback Works)

Runtime config is loaded in this order (later files override earlier ones):

1. `orchestrator_config.json`
2. `orchestrator_project.json`
3. `orchestrator_project.local.json` (gitignored)
4. `orchestrator_config.local.json` (legacy, gitignored)
5. `config/config.local.json` (shared local, gitignored)

Credentials resolve as:

1. Environment variable value (if set)
2. Local JSON fallback value

## 3) Recommended Credential Pattern

Use `orchestrator_project.local.json` in repo root (gitignored).

Create or edit with values for your machine:

```json
{
	"email_account": "your_gmail_address",
	"email_password": "your_google_app_password",
	"service_account_json": "C:\\path\\to\\Service_account.json",
	"spreadsheet_id": "your_google_sheet_id",
	"location": "APO",
	"sender_address": "",
	"notify_recipients": []
}
```

Notes:

- `email_account` and `email_password` are required for IMAP/SMTP auth paths.
- `service_account_json` must point to a real local file path.
- `orchestrator_project.local.json` is ignored by git.

## 4) Optional Environment Variable Pattern

You can set these instead (or use them as overrides):

- `GLASS_EMAIL_ACCOUNT`
- `GLASS_EMAIL_PASSWORD`
- `GLASS_SENDER`
- `GLASS_NOTIFY_RECIPIENTS`
- `GLASS_LOGIN_USERNAME`
- `GLASS_LOGIN_PASSWORD`
- `GLASS_LOGIN_ID`

Helper scripts:

- `Run-Setup-GlassEnv.cmd`
- `Run-Set-GlassPassword.cmd`

## 5) Validate Setup

Run full bootstrap + tests:

```powershell
.\Run-Tests.cmd
```

This script will:

- create `.venv` if missing,
- install `requirements.txt`,
- run the full test suite.

## 6) Run Orchestrator

```powershell
.\Run-GlassOrchestrator.cmd
```

## Troubleshooting

### `py` not recognized

Install Python with `winget` and open a new terminal.

### Credential-related test failures

If tests fail for missing account/password, confirm either:

1. `GLASS_EMAIL_ACCOUNT` and `GLASS_EMAIL_PASSWORD` are set, or
2. `orchestrator_project.local.json` contains `email_account` and `email_password`.

### Service account file missing

Update `service_account_json` to a valid path on your machine.

## Security

- Never commit credentials.
- If credentials were exposed during migration, rotate the Google App Password.
