"""GitHub REST API client used as the message bus between Railway and the
Opus routine running in the Anthropic cloud sandbox.

The cloud sandbox's egress allowlist blocks `*.up.railway.app`, `discord.com`,
and most arbitrary domains, but allows `github.com` + `*.githubusercontent.com`.
So GitHub becomes the bus:

  Railway worker  --commit pulse-context/latest.json--->  github.com/repo/branch
                                                              |
                                                              | (raw.githubusercontent.com,
                                                              |  allowed by egress)
                                                              v
                                                        Opus routine reads,
                                                        synthesizes,
                                                        commits result via MCP
                                                              |
                                                              v
  Railway worker  <--poll pulse-output/pending/----------  github.com/repo/branch
                  --post markdown to Discord-->

Plain urllib used (no new dependency). Rate limit: 5000 req/hour authenticated,
plenty for our cadence.
"""

import base64
import json
import logging
import urllib.parse
import urllib.request
from typing import Any

from config import settings

log = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """Wraps non-2xx responses from the GitHub API."""

    def __init__(self, status: int, body: str):
        super().__init__(f"GitHub API {status}: {body[:300]}")
        self.status = status
        self.body = body


def _auth_headers() -> dict[str, str]:
    token = settings.github_token
    if not token:
        raise RuntimeError("GITHUB_TOKEN not configured — bridge disabled")
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "InstitutionalReportBot/1.0",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(method: str, path: str, body: dict | None = None) -> dict:
    url = f"{API_BASE}{path}"
    headers = _auth_headers()
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read()
            if not payload:
                return {}
            return json.loads(payload)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        raise GitHubError(e.code, body_text) from None


def _repo_path(path: str) -> str:
    """Return owner/repo URL fragment for github_repo setting like 'owner/repo'."""
    repo = settings.github_repo.strip().strip("/")
    if not repo or "/" not in repo:
        raise RuntimeError(f"Invalid GITHUB_REPO setting: {repo!r}")
    return f"/repos/{repo}/contents/{path.lstrip('/')}"


def get_file(path: str, ref: str | None = None) -> dict | None:
    """Get a single file's metadata + base64 content. Returns None on 404."""
    branch = ref or settings.github_bridge_branch
    qs = urllib.parse.urlencode({"ref": branch})
    try:
        return _request("GET", f"{_repo_path(path)}?{qs}")
    except GitHubError as e:
        if e.status == 404:
            return None
        raise


def get_file_text(path: str, ref: str | None = None) -> str | None:
    """Convenience: fetch a file and decode its content as utf-8 text."""
    meta = get_file(path, ref=ref)
    if not meta:
        return None
    encoding = meta.get("encoding")
    content = meta.get("content") or ""
    if encoding == "base64":
        return base64.b64decode(content).decode("utf-8", errors="replace")
    return content


def list_dir(path: str, ref: str | None = None) -> list[dict]:
    """List a directory's children. Returns [] on 404."""
    branch = ref or settings.github_bridge_branch
    qs = urllib.parse.urlencode({"ref": branch})
    try:
        result = _request("GET", f"{_repo_path(path)}?{qs}")
    except GitHubError as e:
        if e.status == 404:
            return []
        raise
    if isinstance(result, list):
        return result
    return []


def put_file(path: str, content: str, message: str, ref: str | None = None) -> dict:
    """Create or update a file. Encodes content as base64 internally."""
    branch = ref or settings.github_bridge_branch
    body: dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    # If the file exists, GitHub requires the current sha for updates.
    existing = get_file(path, ref=branch)
    if existing and "sha" in existing:
        body["sha"] = existing["sha"]
    return _request("PUT", _repo_path(path), body=body)


def delete_file(path: str, message: str, ref: str | None = None) -> dict | None:
    """Delete a file. Returns None if the file doesn't exist."""
    branch = ref or settings.github_bridge_branch
    existing = get_file(path, ref=branch)
    if not existing or "sha" not in existing:
        return None
    body = {"message": message, "sha": existing["sha"], "branch": branch}
    return _request("DELETE", _repo_path(path), body=body)


def ensure_branch_exists() -> None:
    """Create the bridge branch from main if it doesn't exist yet."""
    repo = settings.github_repo.strip().strip("/")
    branch = settings.github_bridge_branch
    try:
        _request("GET", f"/repos/{repo}/git/refs/heads/{branch}")
        log.info(f"GitHub bridge branch '{branch}' already exists")
        return
    except GitHubError as e:
        if e.status != 404:
            raise

    # Branch doesn't exist — create it from default branch
    repo_meta = _request("GET", f"/repos/{repo}")
    default = repo_meta.get("default_branch", "main")
    default_ref = _request("GET", f"/repos/{repo}/git/refs/heads/{default}")
    sha = default_ref["object"]["sha"]
    _request(
        "POST",
        f"/repos/{repo}/git/refs",
        body={"ref": f"refs/heads/{branch}", "sha": sha},
    )
    log.info(f"Created GitHub bridge branch '{branch}' from '{default}' at {sha[:8]}")
