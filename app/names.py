import re
from collections.abc import Iterable

_OWNER = r"[A-Za-z0-9][A-Za-z0-9-]{0,38}"
_REPO = r"[A-Za-z0-9._-]{1,100}"
_PATTERNS = [
    # A browser URL may point inside the repo (/tree/main/..., /blob/..., #readme, ?tab=...).
    re.compile(rf"^(?:https?://)?(?:www\.)?github\.com/({_OWNER})/({_REPO})(?:[/?#].*)?$"),
    re.compile(rf"^git@github\.com:({_OWNER})/({_REPO})$"),
    re.compile(rf"^({_OWNER})/({_REPO})$"),
]

MAX_TAG_LENGTH = 40
MAX_TAGS = 20
MAX_DESCRIPTION_LENGTH = 500


def parse_repo_name(text: str) -> str:
    """Normalize user input (owner/repo, HTTPS or SSH URL) to lowercase 'owner/repo'."""
    value = text.strip()
    for pattern in _PATTERNS:
        match = pattern.match(value)
        if not match:
            continue
        owner, repo = match.groups()
        repo = repo.removesuffix(".git")
        if repo in ("", ".", ".."):
            break
        return f"{owner}/{repo}".lower()
    raise ValueError(f"nome de repositório inválido: {text!r}")


def parse_tags(value: str | Iterable[str]) -> list[str]:
    """Comma-separated text or a list -> tidy tags; keeps the first spelling of dupes."""
    parts = value.split(",") if isinstance(value, str) else value
    tags: list[str] = []
    seen = set()
    for part in parts:
        tag = " ".join(part.split())
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"tag com mais de {MAX_TAG_LENGTH} caracteres: {tag!r}")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValueError(f"no máximo {MAX_TAGS} tags por repositório")
    return tags


def parse_description(value: str | None) -> str | None:
    """Trimmed description, None when empty."""
    if value is None:
        return None
    text = value.strip()
    if len(text) > MAX_DESCRIPTION_LENGTH:
        raise ValueError(f"descrição com mais de {MAX_DESCRIPTION_LENGTH} caracteres")
    return text or None
