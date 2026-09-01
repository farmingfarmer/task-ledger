#!/usr/bin/env python3
"""Print the Copilot prompt, and nothing else, ready to paste.

    tl.bat prompt                 print it
    tl.bat prompt | clip          straight onto the Windows clipboard
    tl.bat prompt --with <file>   the prompt followed by a transcript

The prompt lives inside docs/prompts/copilot-claims-prompt.md, which
also contains instructions FOR YOU -- how to run the ingester, how to
score the comparison, what the confidence cap means. Hand-selecting the
right half of that file is a step that can go wrong quietly: paste too
much and Copilot starts answering the documentation instead of the
meeting, which reads as Copilot being bad at extraction.

--with matters more than it looks. Anchoring can only match a quote
against a transcript THIS system holds, so the surest way to earn
receipts is to hand Copilot the same text: prompt, then the transcript,
in one message. Asking it to work from a meeting it has open instead
means it quotes a transcript we cannot see, and a quote we cannot find
is a claim we must cap.
"""
import argparse
import os
import sys

ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."))
DOC = os.path.join(ROOT, "docs", "prompts", "copilot-claims-prompt.md")
START = "## The prompt"


def prompt_text(path=DOC):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if START not in text:
        raise SystemExit("{0} has no '{1}' section".format(path, START))
    body = text.split(START, 1)[1]
    # Ends at the next horizontal rule on its own line -- everything after
    # it addresses the operator, not the assistant.
    for end in ("\n---\n", "\n---\r\n"):
        if end in body:
            body = body.split(end, 1)[0]
            break
    return body.strip("\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--with", dest="transcript", default=None,
                    help="append a transcript file, so the quotes come "
                         "from text this system can search")
    args = ap.parse_args()

    out = [prompt_text()]
    if args.transcript:
        sys.path.insert(0, os.path.join(ROOT, "extraction"))
        from textio import read_text
        body = read_text(args.transcript, verbose=False)
        out.append("\nHere is the transcript.\n")
        out.append(body)
        sys.stderr.write(
            "prompt + {0} ({1:,} characters).\n".format(
                os.path.basename(args.transcript), len(body)))
        if len(body) > 60000:
            sys.stderr.write(
                "That is long. Chat windows truncate silently, and a "
                "truncated transcript looks exactly like a meeting where "
                "less was said -- split it and do one meeting at a "
                "time.\n")
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
