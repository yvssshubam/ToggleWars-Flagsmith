"""Every call the bot makes to GitHub's REST API goes through this module.

Auth is a fine-grained personal access token scoped to one repo, with
Contents, Issues and Pull requests set to read and write.

Every comment the bot posts starts with BOT_MARKER. Because the token belongs
to your own account, the bot's comments arrive as webhooks from *you*; the
marker is how the bot recognises (and ignores) its own comments.
"""

import base64
import os

import httpx
from dotenv import load_dotenv

API_URL = "https://api.github.com"
BOT_MARKER = "<!-- release-autopilot -->"
TIMEOUT_SECONDS = 20


class GitHubError(Exception):
    def __init__(self, action, status_code, body):
        self.action, self.status_code, self.body = action, status_code, body
        status = f"HTTP {status_code}" if status_code is not None else "no response"
        super().__init__(f"{action} failed: {status}: {body[:300]}")


class GitHubClient:
    def __init__(self, token, repo, transport=None):
        if "/" not in repo:
            raise ValueError(f"GITHUB_REPO must look like owner/name, got {repo!r}")
        self.repo = repo
        self._http = httpx.Client(
            base_url=API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "release-autopilot",
            },
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        )

    @classmethod
    def from_env(cls):
        load_dotenv()
        missing = [n for n in ("GITHUB_TOKEN", "GITHUB_REPO") if not os.environ.get(n)]
        if missing:
            raise RuntimeError(f"Missing {', '.join(missing)} in .env")
        return cls(os.environ["GITHUB_TOKEN"], os.environ["GITHUB_REPO"])

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _request(self, method, path, action, ok=(200,), **kwargs):
        try:
            r = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise GitHubError(action, None, f"{type(e).__name__}: {e}") from e
        if r.status_code not in ok:
            raise GitHubError(action, r.status_code, r.text)
        return r

    # --- reads ---------------------------------------------------------------

    def get_repo(self):
        return self._request("GET", f"/repos/{self.repo}", "read repo").json()

    def list_root(self, ref=None):
        """Names of files and folders at the top of the repo."""
        params = {"ref": ref} if ref else None
        items = self._request("GET", f"/repos/{self.repo}/contents/", "list repo root", params=params).json()
        return [item["name"] for item in items]

    def get_pull(self, number):
        return self._request("GET", f"/repos/{self.repo}/pulls/{number}", f"read PR #{number}").json()

    def list_pull_files(self, number, max_pages=30):
        """Files changed in a PR: dicts with filename, status, additions, deletions, patch."""
        files = []
        for page in range(1, max_pages + 1):
            batch = self._request(
                "GET", f"/repos/{self.repo}/pulls/{number}/files", f"list files of PR #{number}",
                params={"per_page": 100, "page": page},
            ).json()
            files.extend(batch)
            if len(batch) < 100:
                break
        return files

    def get_file(self, path, ref):
        """(text, sha) of a file at a branch or commit, or None if it doesn't exist there."""
        try:
            data = self._request("GET", f"/repos/{self.repo}/contents/{path}", f"read {path}@{ref}",
                                 params={"ref": ref}).json()
        except GitHubError as e:
            if e.status_code == 404:
                return None
            raise
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    # --- writes --------------------------------------------------------------

    def update_file(self, path, text, blob_sha, branch, message):
        """Commit a new version of one file to a branch. `blob_sha` is the file's
        current blob sha (from get_file), so a concurrent change fails with 409
        instead of being overwritten. Returns the new commit's sha."""
        r = self._request(
            "PUT", f"/repos/{self.repo}/contents/{path}", f"commit {path} to {branch}",
            json={"message": message, "branch": branch, "sha": blob_sha,
                  "content": base64.b64encode(text.encode("utf-8")).decode("ascii")},
        )
        return r.json()["commit"]["sha"]

    def branch_sha(self, branch):
        """The commit a branch points at."""
        r = self._request("GET", f"/repos/{self.repo}/git/ref/heads/{branch}", f"read branch {branch}")
        return r.json()["object"]["sha"]

    def create_branch(self, branch, sha):
        """Create `branch` at commit `sha`. Returns False if it already exists."""
        try:
            self._request("POST", f"/repos/{self.repo}/git/refs", f"create branch {branch}", ok=(201,),
                          json={"ref": f"refs/heads/{branch}", "sha": sha})
        except GitHubError as e:
            if e.status_code == 422 and "already exists" in e.body:
                return False
            raise
        return True

    def find_open_pull(self, branch):
        """Number of the open PR from `branch` in this repo, or None."""
        owner = self.repo.split("/")[0]
        pulls = self._request("GET", f"/repos/{self.repo}/pulls", f"find PR from {branch}",
                              params={"head": f"{owner}:{branch}", "state": "open"}).json()
        return pulls[0]["number"] if pulls else None

    def create_pull(self, title, branch, base, body):
        """Open a PR from `branch` into `base`; if one is already open, return its number."""
        try:
            r = self._request("POST", f"/repos/{self.repo}/pulls", f"open PR from {branch}", ok=(201,),
                              json={"title": title, "head": branch, "base": base,
                                    "body": f"{BOT_MARKER}\n{body}"})
        except GitHubError as e:
            existing = self.find_open_pull(branch) if e.status_code == 422 else None
            if existing:
                return existing
            raise
        return r.json()["number"]

    # --- comments ------------------------------------------------------------

    def post_comment(self, number, body):
        """Comment on a PR (PRs are issues for comments). Returns the comment id."""
        r = self._request("POST", f"/repos/{self.repo}/issues/{number}/comments",
                          f"comment on PR #{number}", ok=(201,), json={"body": f"{BOT_MARKER}\n{body}"})
        return r.json()["id"]

    def edit_comment(self, comment_id, body):
        self._request("PATCH", f"/repos/{self.repo}/issues/comments/{comment_id}",
                      f"edit comment {comment_id}", json={"body": f"{BOT_MARKER}\n{body}"})