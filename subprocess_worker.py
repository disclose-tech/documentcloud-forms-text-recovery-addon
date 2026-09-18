"""Run `recover.analyze` on one PDF in an isolated subprocess.

Reads the PDF bytes from stdin and writes ``[pages, report]`` as JSON to stdout.
Lives in its own process so a malformed PDF can't take the whole run down.
"""

import json
import sys

import recover


def main():
    pdf_bytes = sys.stdin.buffer.read()
    keep_diff = "--keep-diff" in sys.argv
    pages, report = recover.analyze(pdf_bytes, keep_diff=keep_diff)
    json.dump([pages, report], sys.stdout)


if __name__ == "__main__":
    main()
