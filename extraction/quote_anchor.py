"""Match a quoted sentence back to the utterance somebody actually said.

WHY. Copilot comprehends a meeting better than a local model does -- that
was measured, side by side, and it was not close. What it does not give
is a checkable citation: its exports name a MEETING, not a sentence, so
its claims enter the ledger marked 'assistant_claim', confidence-capped
below the floors for closing work, and read-only in the browser. The
assistant says a transcript supports it and nobody downstream can check.

But the transcript is right here. Ask Copilot for the quote it worked
from and that gap closes: find the sentence in the windows already on
disk and the claim earns the same evidence ref the local extractor would
have produced -- same resource id, same content hash, computed the same
way -- plus the real speaker, from the transport rather than the model.

THE RISK IS A FABRICATED RECEIPT, and it is worse than no receipt at
all. A provenance rail that sometimes points at the wrong sentence is
not a weaker rail, it is a lie with a hash on it. So matching is
tiered, every tier is recorded on the match, and the loosest one is
deliberately strict enough to refuse a near-miss. A claim that does not
match stays exactly as it is today: unverified, capped, honest about it.
"""
import difflib
import glob
import hashlib
import json
import os
import re

# Anything above this is the same sentence with the edges rubbed off --
# a dropped filler, a smoothed contraction, "gonna" written out. Below
# it, two sentences about the same topic score frighteningly well, and
# attaching the wrong utterance would put a false receipt in the one
# place this system asks to be trusted. Chosen to refuse rather than
# guess: an unmatched claim is a known unknown, a wrong match is not.
FUZZY_FLOOR = 0.86


def _sha(text):
    """Byte-identical to extraction_worker._sha for a single part.

    That function joins its parts with \x1f, so hashing the text alone
    would produce a receipt that looks right and matches nothing -- a
    provenance rail that silently disagrees with itself depending on
    which path wrote the row. Verified against evidence already in the
    database rather than read off the source.
    """
    h = hashlib.sha256()
    h.update(str(text).encode("utf-8"))
    h.update(b"\x1f")
    return h.hexdigest()


def _norm(text):
    """Case, punctuation and spacing are transcription noise, not content."""
    return re.sub(r"[^a-z0-9 ]+", " ",
                  (text or "").lower()).strip()


def _squash(text):
    return " ".join(_norm(text).split())


class Transcripts(object):
    """Every utterance the ledger has on disk, indexed for lookup."""

    def __init__(self, inbox):
        self.utterances = []          # (squashed, record)
        self.exact = {}
        roots = [inbox, os.path.join(inbox, "processed")]
        for root in roots:
            for path in sorted(glob.glob(os.path.join(root, "*.json"))):
                try:
                    with open(path, encoding="utf-8") as f:
                        doc = json.load(f)
                except Exception:                       # noqa: BLE001
                    continue
                job = doc.get("job") or {}
                for item in doc.get("items") or []:
                    text = item.get("text") or ""
                    if not text.strip():
                        continue
                    rec = {
                        "resource_id": item.get("resource_id"),
                        # Same hash the extraction worker writes, over the
                        # same text, so a receipt from either path points
                        # at the same thing and verifies identically.
                        "content_hash": _sha(text),
                        "author_ref": item.get("author_ref"),
                        "source_ref": job.get("source_ref"),
                        "text": text,
                    }
                    key = _squash(text)
                    if key:
                        self.exact.setdefault(key, rec)
                        self.utterances.append((key, rec))

    def __len__(self):
        return len(self.utterances)

    def find(self, quote):
        """-> (record, how) or (None, why-not).

        Tiers, strictest first. 'how' travels with the match so a run can
        report what its receipts actually rest on rather than implying
        they were all exact.
        """
        q = _squash(quote)
        if not q or len(q) < 12:
            # Too short to identify anything. "yes", "agreed", "will do"
            # match dozens of utterances and mean nothing as a citation.
            return None, "quote too short to identify a sentence"

        hit = self.exact.get(q)
        if hit:
            return hit, "exact"

        # A quote that is a clean substring of one utterance, or contains
        # one: Copilot trims a preamble, or quotes two sentences where the
        # transcript split them.
        contained = [rec for key, rec in self.utterances
                     if (q in key or key in q) and len(key) >= 12]
        if len(contained) == 1:
            return contained[0], "contained"
        if len(contained) > 1:
            return None, "ambiguous: matches {0} utterances".format(
                len(contained))

        best, score = None, 0.0
        matcher = difflib.SequenceMatcher()
        matcher.set_seq2(q)
        for key, rec in self.utterances:
            # Cheap length gate first: difflib over every utterance in a
            # corpus is the slowest thing here by an order of magnitude.
            if abs(len(key) - len(q)) > max(40, len(q) * 0.5):
                continue
            matcher.set_seq1(key)
            if matcher.real_quick_ratio() < FUZZY_FLOOR:
                continue
            if matcher.quick_ratio() < FUZZY_FLOOR:
                continue
            r = matcher.ratio()
            if r > score:
                best, score = rec, r
        if best is not None and score >= FUZZY_FLOOR:
            return best, "fuzzy {0:.2f}".format(score)
        return None, "no utterance matches (best {0:.2f})".format(score)
