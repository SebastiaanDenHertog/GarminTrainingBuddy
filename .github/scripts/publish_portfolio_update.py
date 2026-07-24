from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_FILE = Path(".portfolio/project.json")
MESSAGE_PATTERN = re.compile(
    r"^portfolio(?:\((?P<type>[A-Za-z0-9_-]+)\))?:\s*(?P<title>.+)$",
    re.IGNORECASE,
)


def required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment value {name} is missing.")
    return value


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def parse_portfolio_message(message: str) -> tuple[str, str, str] | None:
    lines = message.strip().splitlines()
    if not lines:
        return None

    match = MESSAGE_PATTERN.match(lines[0].strip())
    if match is None:
        return None

    update_type = (match.group("type") or "development").lower()
    title = match.group("title").strip()
    description = "\n".join(lines[1:]).strip()

    if not description:
        description = title

    return update_type, title, description


def post_update(api_url: str, api_token: str, payload: dict) -> None:
    request = Request(
        api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            "User-Agent": "sebastiaan-portfolio-github-action/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8")
            print(f"Published {payload['event_id']}: {body}")
    except HTTPError as error:
        response_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Portfolio API returned HTTP {error.code}: {response_body}"
        ) from error
    except URLError as error:
        raise RuntimeError(f"Could not reach portfolio API: {error}") from error


def build_push_payloads(event: dict, project: dict) -> list[dict]:
    repository = required_environment("GITHUB_REPOSITORY")
    server_url = os.getenv("GITHUB_SERVER_URL", "https://github.com").rstrip("/")

    payloads: list[dict] = []

    for commit in event.get("commits", []):
        parsed_message = parse_portfolio_message(commit.get("message", ""))
        if parsed_message is None:
            continue

        update_type, title, description = parsed_message
        commit_sha = commit["id"]

        payloads.append(
            {
                "event_id": f"{repository}:{commit_sha}",
                "project": project,
                "title": title,
                "description": description,
                "update_type": update_type,
                "happened_at": commit.get("timestamp")
                or datetime.now(timezone.utc).isoformat(),
                "commit_sha": commit_sha,
                "commit_url": (
                    f"{server_url}/{repository}/commit/{commit_sha}"
                    if project.get("repository_url")
                    else None
                ),
            }
        )

    return payloads


def build_manual_payload(project: dict) -> dict:
    repository = required_environment("GITHUB_REPOSITORY")
    run_id = required_environment("GITHUB_RUN_ID")
    run_attempt = os.getenv("GITHUB_RUN_ATTEMPT", "1")

    title = required_environment("MANUAL_TITLE")
    description = required_environment("MANUAL_DESCRIPTION")
    update_type = os.getenv("MANUAL_UPDATE_TYPE", "development").strip()

    return {
        "event_id": f"{repository}:manual:{run_id}:{run_attempt}",
        "project": project,
        "title": title,
        "description": description,
        "update_type": update_type,
        "happened_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": os.getenv("GITHUB_SHA"),
        "commit_url": None,
    }


def main() -> int:
    if not PROJECT_FILE.exists():
        print(f"Missing project file: {PROJECT_FILE}", file=sys.stderr)
        return 1

    api_base_url = required_environment("PORTFOLIO_API_BASE_URL").rstrip("/")
    api_token = required_environment("PORTFOLIO_API_TOKEN")
    api_url = f"{api_base_url}/api/v1/updates"

    project = load_json(PROJECT_FILE)
    event = load_json(Path(required_environment("GITHUB_EVENT_PATH")))
    event_name = required_environment("GITHUB_EVENT_NAME")

    if event_name == "workflow_dispatch":
        payloads = [build_manual_payload(project)]
    elif event_name == "push":
        payloads = build_push_payloads(event, project)
    else:
        print(f"Event {event_name!r} is not supported; nothing to publish.")
        return 0

    if not payloads:
        print(
            "No public portfolio updates found. "
            "Use a commit starting with 'portfolio:' to publish one."
        )
        return 0

    for payload in payloads:
        post_update(api_url, api_token, payload)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
