# /// script
# dependencies = ["requests>=2.32,<3"]
# ///
"""Refresh public repository, package, and Google Scholar totals."""

import html
import json
import os
import random
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data/profile.json"
USER_AGENT = "fcakyon-profile/1.0 (+https://github.com/fcakyon/fcakyon)"


def scholar_session() -> requests.Session:
    """Create a Scholar session routed through a sticky DataImpulse proxy."""
    username = f"{os.environ['PROXY_USER']};sessid-{random.randrange(16**8):08x}"
    proxy = (
        f"http://{username}:{os.environ['PROXY_PASS']}@"
        f"{os.environ['PROXY_HOST']}:{os.environ['PROXY_PORT']}"
    )
    session = requests.Session()
    session.headers.update(
        {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}
    )
    session.proxies = {"http": proxy, "https": proxy}
    return session


def fetch_scholar_counts(profile_id: str) -> dict[str, int]:
    """Fetch exact citation totals from one Google Scholar profile page.

    Args:
        profile_id (str): Google Scholar profile identifier.

    Returns:
        (dict[str, int]): Citation totals keyed by Scholar publication identifier.
    """
    response = scholar_session().get(
        "https://scholar.google.com/citations",
        params={"user": profile_id, "hl": "en", "pagesize": 100},
        timeout=60,
    )
    response.raise_for_status()
    if len(response.text) < 5000 or "not a robot" in response.text.lower():
        raise RuntimeError("Google Scholar returned a block page")

    counts = {}
    for block in response.text.split('<tr class="gsc_a_tr"')[1:]:
        publication = re.search(r'citation_for_view=[^:&"]+:([^&"]+)', block)
        citations = re.search(r'class="gsc_a_ac[^"]*"[^>]*>(\d*)</a>', block)
        if publication:
            counts[html.unescape(publication.group(1))] = (
                int(citations.group(1) or 0) if citations else 0
            )
    return counts


def fetch_repo_metrics(repo: str, token: str) -> dict[str, int]:
    """Fetch public GitHub repository totals.

    Args:
        repo (str): Repository in owner/name form.
        token (str): GitHub API token.

    Returns:
        (dict[str, int]): Star and fork totals.
    """
    response = requests.get(
        f"https://api.github.com/repos/{repo}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    return {"stars": payload["stargazers_count"], "forks": payload["forks_count"]}


def fetch_package_downloads(package: str) -> str:
    """Fetch the latest monthly PyPI download total.

    Args:
        package (str): PyPI package name.

    Returns:
        (str): Compact downloads reported for the last month.
    """
    response = requests.get(
        f"https://static.pepy.tech/badge/{package}/month",
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    values = re.findall(r"<text[^>]*>([^<]+)</text>", response.text)
    if not values or not re.fullmatch(r"[\d.]+[kMB]?", values[-1]):
        raise RuntimeError(f"PePy returned an invalid badge for {package}")
    return values[-1]


def fetch_traffic_downloads(repo: str, branch: str) -> int:
    """Fetch the cumulative clone count stored by a repository traffic branch.

    Args:
        repo (str): Repository in owner/name form.
        branch (str): Branch containing history.json.

    Returns:
        (int): Sum of the recorded daily clone counts.
    """
    response = requests.get(
        f"https://raw.githubusercontent.com/{repo}/{branch}/history.json",
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    return sum(response.json()["clones"].values())


def fetch_project_metrics(project: dict, token: str) -> dict[str, int | str]:
    """Fetch every configured metric for one repository.

    Args:
        project (dict): Repository and optional download sources.
        token (str): GitHub API token.

    Returns:
        (dict[str, int | str]): Fresh metrics for the repository.
    """
    metrics = fetch_repo_metrics(project["repo"], token)
    if project.get("traffic_branch"):
        metrics["downloads_total"] = fetch_traffic_downloads(
            project["repo"], project["traffic_branch"]
        )
    if project.get("pypi_package"):
        metrics["downloads_month"] = fetch_package_downloads(project["pypi_package"])
    return metrics


def main() -> None:
    """Update the canonical profile data when a public total changes."""
    data = json.loads(DATA_PATH.read_text())
    before = json.dumps(data, sort_keys=True)
    projects = [item for item in data["open_source"] if item.get("repo")]
    token = os.environ["GITHUB_TOKEN"]
    with ThreadPoolExecutor(max_workers=len(projects) + 1) as executor:
        citation_future = executor.submit(fetch_scholar_counts, data["scholar_profile"])
        project_futures = [
            executor.submit(fetch_project_metrics, project, token)
            for project in projects
        ]
        citations = citation_future.result()
        for project, future in zip(projects, project_futures, strict=True):
            project["metrics"].update(future.result())

    for paper in (item for item in data["papers"] if item.get("scholar_id")):
        paper["metrics"]["citations"] = citations[paper["scholar_id"]]

    if json.dumps(data, sort_keys=True) != before:
        data["updated_at"] = datetime.now(UTC).date().isoformat()
        DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        print(f"Updated {DATA_PATH.relative_to(ROOT)}")
    else:
        print("Profile totals are unchanged")


if __name__ == "__main__":
    main()
