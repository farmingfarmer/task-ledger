"""Meeting identity, recovered from source_ref.

A window's source_ref looks like 'teams-2026-08-17-project-sync-p2' or
'copilot-2026-08-05-DS_AI_Team_-_AWS_Infrastructure_sync-up-p1': an
ingest prefix, the date, the meeting's slug, and a chunk suffix. The slug
is the recurring MEETING; the date is the occurrence. Nothing downstream
stored this distinction, but it never needed to -- it is recoverable, and
it is what "this standup, last time" means.
"""
import re

_SREF = re.compile(
    r"^(?:teams|copilot|cp)-(\d{4}-\d{2}-\d{2})-(.+?)(?:-[psq]\d+)?$")


def parse(source_ref):
    """-> (series_slug, date) or (source_ref, None) when unrecognized."""
    m = _SREF.match(source_ref or "")
    if not m:
        return (source_ref or "", None)
    return (m.group(2), m.group(1))


def pretty(source_ref):
    """'teams-2026-08-17-project-sync-p2' -> 'project sync (2026-08-17)'."""
    slug, date = parse(source_ref)
    name = re.sub(r"[-_]+", " ", slug).strip()
    return "{0} ({1})".format(name, date) if date else name
