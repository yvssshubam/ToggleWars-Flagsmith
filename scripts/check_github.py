"""Read-only check of the GitHub setup in .env. Posts and changes nothing.

    python -m scripts.check_github
"""

import os
import sys

from dotenv import load_dotenv

from autopilot.github_client import GitHubClient, GitHubError


def main():
    load_dotenv()
    problems = []
    for name in ("GITHUB_TOKEN", "GITHUB_REPO", "GITHUB_WEBHOOK_SECRET", "ALLOWED_APPROVERS"):
        value = os.environ.get(name, "").strip()
        shown = "set" if name in ("GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET") and value else (value or "MISSING")
        print(f"{name}: {shown}")
        if not value:
            problems.append(f"{name} is missing from .env")
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and not token.startswith("github_pat_"):
        print("   note: this doesn't look like a fine-grained token (they start with github_pat_)")
    secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if secret and len(secret) < 16:
        problems.append("GITHUB_WEBHOOK_SECRET is short; use at least 16 random characters")
    if not os.environ.get("GITHUB_TOKEN", "").strip() or not os.environ.get("GITHUB_REPO", "").strip():
        print("\nFAILED:", "; ".join(problems))
        sys.exit(1)

    try:
        with GitHubClient.from_env() as gh:
            repo = gh.get_repo()
            print(f"\nrepo: {repo['full_name']} | {'PRIVATE' if repo['private'] else 'public'} | "
                  f"default branch: {repo['default_branch']}")
            perms = repo.get("permissions") or {}
            print(f"token can write to it: {perms.get('push')}")
            if repo["private"]:
                problems.append("the repo is private; the hackathon requires a public repo")
            if not perms.get("push"):
                problems.append("the token can't write to this repo (check Contents / Pull requests / Issues = Read and write)")
            root = gh.list_root()
            print(f"files at the top of the repo: {', '.join(sorted(root))}")
            leaked = [n for n in root if n == ".env" or (n.startswith(".env") and n != ".env.example")]
            if leaked:
                problems.append(f"SECRET FILE ON GITHUB: {', '.join(leaked)} (remove it and rotate the keys)")
            if "demo_shop" not in root:
                problems.append("demo_shop/ isn't on GitHub yet; run git push")
            demo = gh.get_file("demo_shop/.env", repo["default_branch"])
            if demo is not None:
                problems.append("SECRET FILE ON GITHUB: demo_shop/.env (remove it and rotate the key)")
    except GitHubError as e:
        print(f"\nFAILED: {e}")
        if e.status_code in (401, 403):
            print("The token was rejected: check it's copied fully into GITHUB_TOKEN and hasn't expired.")
        elif e.status_code == 404:
            print("Repo not found: check GITHUB_REPO and that the token's repository access includes it.")
        sys.exit(1)

    if problems:
        print("\nPROBLEMS:")
        for p in problems:
            print(" -", p)
        sys.exit(1)
    print("\nPASS: GitHub is set up for the bot.")


if __name__ == "__main__":
    main()