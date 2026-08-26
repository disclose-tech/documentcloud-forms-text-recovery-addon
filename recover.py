import re
from collections import Counter

import form_fields
import text_layer

# Vertical positions are rounded to this fraction of the page height before
# sorting, so words on one line sort together.
LINE_ROUNDING = 0.005


def normalize(text):
    """Normalize text for comparison."""
    return re.sub(r"\s+", "", text).casefold()


def words_inside(field, words):
    """The text-layer words lying inside a field, in reading order."""

    inside = []
    for word in words:
        cx = (word["x1"] + word["x2"]) / 2
        cy = (word["y1"] + word["y2"]) / 2
        if field["x1"] <= cx <= field["x2"] and field["y1"] <= cy <= field["y2"]:
            inside.append(word)
    return inside


def value_is_present(field, words):
    """Whether the field's value already appears where the field is."""
    inside = words_inside(field, words)
    return normalize("".join(word["text"] for word in inside)) == normalize(
        field["text"]
    )


def reading_position(box):
    """Where a box sits in reading order, top line first.

    The vertical position is rounded first, because `y1` is a word's top
    edge and tall letters push it up.
    """
    return (round(box["y1"] / LINE_ROUNDING), box["x1"])


def insertion_offset(field, words):
    """Where in the page text a field's value belongs: after the words that
    read before the field, usually its own label.  Found on the page, not from
    the text's own offsets (PDFium returns characters in content-stream order,
    footer first on these forms).
    """
    field_position = reading_position(field)
    preceding = [word for word in words if reading_position(word) < field_position]
    if not preceding:
        return 0
    return max(word["end"] for word in preceding)


def merge(text, words, fields):
    """Insert filled field values into a page's text and positions."""
    dropped = set()
    inserts = {}

    for field in sorted(fields, key=reading_position):
        inside = words_inside(field, words)
        for word in inside:
            dropped.add(id(word))

        # A field that sits on top of placeholder glyphs takes their place in
        # the text; one that sits on blank paper goes after its label.
        offset = (
            min(word["start"] for word in inside)
            if inside
            else insertion_offset(field, words)
        )
        inserts.setdefault(offset, []).append(field)

    # Which characters survive: every one except those of a dropped word.
    keep = bytearray(b"\x01") * len(text)
    for word in words:
        if id(word) in dropped:
            for index in range(word["start"], min(word["end"], len(text))):
                keep[index] = 0

    # Where each surviving word begins, so its box is emitted with its characters.
    starts = {}
    for word in words:
        if id(word) not in dropped:
            starts.setdefault(word["start"], []).append(word)

    pieces = []
    positions = []
    previous = "\n"

    # Text and positions are built in the same pass: DocumentCloud pairs them by index.
    # The loop runs one index past the end, so values placed after the last
    # character are emitted too.
    for index in range(len(text) + 1):
        # The values placed at this offset, on their own lines.
        if index in inserts:
            prefix = "" if previous.isspace() else "\n"
            pieces.append(
                prefix + "\n".join(field["text"] for field in inserts[index]) + "\n"
            )
            for field in inserts[index]:
                positions.extend(field["words"])
            previous = "\n"

        if index == len(text):
            break

        char = text[index]
        for word in starts.get(index, ()):
            positions.append(
                {key: word[key] for key in ("text", "x1", "x2", "y1", "y2")}
            )
        if keep[index]:
            pieces.append(char)
            previous = char
    merged_text = "".join(pieces)

    return merged_text, positions


def analyze_page(pdf, page_number, fields, keep_diff=False):
    """Decide what to do with one page."""
    if not fields:
        return None, {"action": "skip", "reason": "no filled fields"}

    text, words = text_layer.extract_text_layer(pdf, page_number)
    missing = [field for field in fields if not value_is_present(field, words)]
    if not missing:
        return None, {"action": "skip", "reason": "field values already present"}

    merged_text, positions = merge(text, words, missing)

    entry = {
        "action": "patch",
        "fields_recovered": len(missing),
        "field_names": [field["name"] for field in missing],
        # How each field's word positions were determined, best first:
        # measured, per line or spread.
        "position_methods": dict(
            Counter(field["position_method"] for field in missing)
        ),
    }
    if keep_diff:
        entry["text_before"] = text
        entry["text_after"] = merged_text

    return {"text": merged_text, "positions": positions}, entry


def analyze(pdf_bytes, keep_diff=False):
    """Figure out which pages need fixing and build their text and positions.

    Returns `(pages, report)`, where `pages` is the payload for the API and
    `report` records a decision for every page, skipped ones included.
    """
    by_page = form_fields.extract_form_text_by_page(pdf_bytes)

    pages = []
    report = []

    with text_layer.load(pdf_bytes) as pdf:
        for page_number in range(len(pdf)):
            payload, entry = analyze_page(
                pdf, page_number, by_page.get(page_number, []), keep_diff
            )
            if payload is not None:
                pages.append({"page_number": page_number, **payload})
            report.append({"page_number": page_number, **entry})

    return pages, report
