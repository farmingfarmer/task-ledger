#!/usr/bin/env python3
"""Lint the batch files, because cmd.exe cannot be run on the machine
that edits them.

Three shipped kits died to three different batch-parser landmines in
one week: a goto to a label that did not exist, a trailing backslash
eating a closing quote, and bare parentheses inside an echo detonating
the enclosing block -- twice, once via variable expansion and once
typed directly. Every one parsed fine to a human eye and failed only
on the recipient's machine. cmd's grammar is not going to be learned
by being careful; it gets a linter in the suite, where the mistake
costs a red test instead of a teammate's afternoon.

Checks:
  1. every `goto :label` has a matching label line (:eof is built in)
  2. echo lines must escape every ( and ) as ^( and ^)
  3. `-C "%VAR%"` style: no quoted argument ending in \\" -- the
     backslash escapes the quote
"""
import glob
import re
import sys

FILES = (glob.glob("*.bat") + glob.glob("deploy/windows/*.bat"))
bad = 0
for path in FILES:
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    labels = {ln.strip().lstrip(":").lower()
              for ln in lines if ln.strip().startswith(":")}
    labels.add("eof")
    for i, ln in enumerate(lines, 1):
        s = ln.strip()
        for m in re.finditer(r"goto\s+:?(\w+)", s, re.I):
            if m.group(1).lower() not in labels:
                print("{0}:{1}: goto to missing label :{2}".format(
                    path, i, m.group(1)))
                bad += 1
        if re.match(r"(@?echo)\b", s, re.I) and not s.lower() == "echo.":
            body = re.sub(r"\^.", "", s)      # drop escaped pairs
            body = re.sub(r'"[^"]*"', "", body)   # quoted args are safe
            # an echo's text ends where the shell operators begin
            body = re.split(r"\||&&|>", body)[0]
            if "(" in body or ")" in body:
                print("{0}:{1}: unescaped paren in echo -- detonates "
                      "any enclosing block: {2}".format(
                          path, i, s[:70]))
                bad += 1
        # variables that END in a backslash (%~dp0 and anything derived
        # from it) must not sit flush against a closing quote: \" is an
        # escaped quote and the argument swallows its neighbour. The
        # nested-quote schtasks idiom ("\"...\"") is deliberate; skip it.
        # cd is a builtin with forgiving parsing -- the \\" trap bites
        # argv handed to EXTERNAL programs. Comments are prose.
        if ('"\\"' not in s
                and not re.match(r"(rem\b|cd\b|cd /d)", s, re.I)
                and re.search(r'"%(KITROOT|~dp0)%?"(?!")', s)):
            print("{0}:{1}: %KITROOT%/%~dp0 end in a backslash; "
                  'use "...%." not "...%": {2}'.format(path, i, s[:70]))
            bad += 1
if bad:
    print("{0} batch landmine(s). cmd will not warn you; this did."
          .format(bad))
    sys.exit(1)
print("bat lint: {0} file(s) clean".format(len(FILES)))
