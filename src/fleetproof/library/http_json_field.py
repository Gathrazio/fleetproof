#!/usr/bin/env python
"""http_json_field — a JSON field exists at an endpoint, with a type and range.

ASSERTS
    GET --url returns a JSON document in which the dotted path --field
    resolves to a value that is present, is of --type (number | string |
    bool) when given, and lies within [--min, --max] when given (numbers
    only). Nothing else about the document is asserted.

SOURCE OF TRUTH
    The emitter's source, NOT this check and NOT the dispatch prompt. Before
    pinning a field name here, open the file that writes the JSON and quote
    the key from it — a grader demanded `days_until_expiry` when the daemon
    had always written `days_remaining`, because a function name in the same
    file was misread as the field name (observed in a field deployment on
    Windows). Put the file:line you read it from in the check's description.

EXIT CODES
    0  field present, type and range satisfied
    2  field missing (path does not resolve)
    3  wrong type
    4  out of range
    5  fetch or parse failure (unreachable, non-JSON, sample unreadable)
    6  usage

CONTROL SAMPLE (FLEETPROOF_CONTROL_SAMPLE)
    When set, the file it names is parsed INSTEAD of fetching --url, so a
    captured real emission can be a grader control (`fleetproof check
    control`). Capture it from the running target, never write it by hand:
        python -c "import urllib.request as u;print(u.urlopen('http://host:port/health').read().decode())" > pass.json
    Register it:
        fleetproof check control http-json-field --pass-sample pass.json --provenance captured
    The shipped fixtures/http_json_field.sample.json is an EXAMPLE of the
    shape only; replace it with your own captured emission.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request

_TYPES = {
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "bool": lambda v: isinstance(v, bool),
}

_MISSING = object()


def resolve(doc, dotted: str):
    """Walk ``dotted`` (a.b.c; integer segments index lists) or return _MISSING."""
    cursor = doc
    for part in dotted.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        elif isinstance(cursor, list) and part.isdigit() and int(part) < len(cursor):
            cursor = cursor[int(part)]
        else:
            return _MISSING
    return cursor


def load_document(url: str, timeout: float):
    sample = os.environ.get("FLEETPROOF_CONTROL_SAMPLE")
    if sample:
        with open(sample, encoding="utf-8-sig") as fh:
            return json.load(fh), f"control sample {sample}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace")), url


def grade(doc, field: str, kind: str | None, lo: float | None, hi: float | None) -> tuple[int, str]:
    value = resolve(doc, field)
    if value is _MISSING:
        return 2, f"FAIL: {field} missing"
    if kind and not _TYPES[kind](value):
        return 3, f"FAIL: {field} is {type(value).__name__}, expected {kind}"
    if lo is not None or hi is not None:
        if not _TYPES["number"](value):
            return 3, f"FAIL: {field} is {type(value).__name__}, a range needs a number"
        if lo is not None and value < lo:
            return 4, f"FAIL: {field}={value} < min {lo}"
        if hi is not None and value > hi:
            return 4, f"FAIL: {field}={value} > max {hi}"
    return 0, f"PASS: {field}={value!r}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--field", required=True, help="Dotted path, e.g. sati_cert.days_remaining")
    parser.add_argument("--type", choices=sorted(_TYPES), default=None)
    parser.add_argument("--min", type=float, default=None)
    parser.add_argument("--max", type=float, default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 6
    try:
        doc, source = load_document(args.url, args.timeout)
    except Exception as e:  # unreachable host, bad JSON, unreadable sample
        print(f"FAIL: could not load JSON: {e}")
        return 5
    code, line = grade(doc, args.field, args.type, args.min, args.max)
    print(f"{line} [{source}]")
    return code


if __name__ == "__main__":
    sys.exit(main())
