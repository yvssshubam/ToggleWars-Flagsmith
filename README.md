# Release Autopilot for Flagsmith

An open-source release bot for AI-written code. For every pull request it
proposes a release plan, and after a human approves it, hides the change
behind a Flagsmith flag, rolls it out to internal users, then 5%, then 25%,
switches it off by a human-approved rule if the new code path is worse, asks
before releasing to everyone, and finally opens a PR that deletes the flag.

**AI writes and explains; rules decide. Nothing changes in production that a
human didn't approve.**

Entry for Track 02 (AI Agents and Feature Flags) of Toggle Wars, the
WeMakeDevs × Flagsmith hackathon.

> Status: work in progress (26 Nov 2026). Setup steps, how Flagsmith is used,
> and the demo video link will be filled in before submission.

## Layout

| Path | What it is |
|---|---|
| `autopilot/` | The bot. `flagsmith_client.py` holds every Flagsmith Admin API call. |
| `autopilot_sdk/` | Small library an app imports to evaluate flags per user and report metrics. |
| `simulator/` | Traffic simulator for the demo shop. |
| `scripts/` | One-off checks against the real Flagsmith account. |
| `tests/` | Offline tests. |

## Setup (Windows, PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # then fill in FS_TOKEN
python -m pytest
python -m scripts.smoke_flagsmith
```
