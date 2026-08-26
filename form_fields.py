"""
Extraction of filled PDF form field (AcroForm) values.
"""

import io

from pypdf import PdfReader

import text_positions
from coordinates import normalize_rect
from pdf_objects import follow_ref

# Field nodes form a tree via /Parent
# This guards against a malformed cycle.
MAX_PARENT_DEPTH = 32


def find_inherited(node, key):
    """Find the value of `key` on this field node or its nearest ancestor.

    /FT and /V are inheritable: a widget commonly carries neither, because one
    field drawn in several places defines both once on the shared parent.
    Returns None if no ancestor within MAX_PARENT_DEPTH supplies the key.
    """
    for _ in range(MAX_PARENT_DEPTH):
        if node is None:
            break
        if key in node:
            return follow_ref(node[key])
        parent = node.get("/Parent")
        node = follow_ref(parent) if parent is not None else None
    return None


def build_field_name(annot):
    """Build the fully qualified field name from /T up the parent chain."""
    parts, node = [], annot
    for _ in range(MAX_PARENT_DEPTH):
        if node is None:
            break
        title = node.get("/T")
        if title is not None:
            parts.append(str(follow_ref(title)))
        parent = node.get("/Parent")
        node = follow_ref(parent) if parent is not None else None
    return ".".join(reversed(parts))


def spread_across_rect(value, rect):
    """Lay a value out across a field's rectangle, one box per word.

    The last resort, for a widget whose appearance stream cannot be trusted to
    say where its text was drawn.  Words share the width in proportion to their
    length, separated by a gap one character wide.
    """
    words = value.split()
    if not words:
        return []
    left, right = min(rect[0], rect[2]), max(rect[0], rect[2])
    bottom, top = min(rect[1], rect[3]), max(rect[1], rect[3])

    lengths = [max(len(word), 1) for word in words]
    scale = (right - left) / (sum(lengths) + len(words) - 1)

    boxes = []
    cursor = left
    for word, length in zip(words, lengths):
        span = length * scale
        boxes.append((word, cursor, cursor + span, bottom, top))
        cursor += span + scale
    return boxes


def position_field_words(annot, value, rect, page_box, rotation):
    """Position each word of a field's value, in DocumentCloud coordinates.

    Returns `(words, label)`, where `label` records where the placement came
    from: the widget's appearance stream when it can be trusted, the
    field's rectangle divided up when it cannot.

    Exactly one box per word, in order, as DocumentCloud pairs the nth word of a
    page's text with the nth position.
    """
    positioned = text_positions.position_words(annot, value)
    if positioned is None:
        boxes, label = spread_across_rect(value, rect), text_positions.SPREAD
    else:
        boxes, label = positioned.boxes, positioned.label

    words = []
    for word, left, right, bottom, top in boxes:
        box = normalize_rect([left, bottom, right, top], page_box, rotation)
        if box is None:
            continue
        words.append({"text": word, **box})
    return words, label


def extract_form_text_by_page(pdf_bytes):
    """Extract texts typed into PDF forms, grouped by page.

    Pages count from zero, and only pages with something typed on them appear.
    A field is skipped if it is empty, if it is a checkbox, or if it cannot be
    placed on the page.
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    by_page = {}

    for page_index, page in enumerate(reader.pages):
        annots = page.get("/Annots")
        if annots is None:
            continue
        try:
            rotation = int(page.get("/Rotate", 0) or 0) % 360
        except (TypeError, ValueError):
            rotation = 0

        fields = []
        for ref in follow_ref(annots):
            annot = follow_ref(ref)
            if not hasattr(annot, "get") or annot.get("/Subtype") != "/Widget":
                continue

            # Only field types holding text, not buttons (/Btn).
            if str(find_inherited(annot, "/FT") or "") not in {"/Tx", "/Ch"}:
                continue

            value = find_inherited(annot, "/V")
            if value is None:
                continue

            if isinstance(value, list):
                value = " ".join(str(follow_ref(v)) for v in value)

            # Values are stored as UTF-16BE; strip() would leave the byte order mark.
            text = str(value).replace("\ufeff", "").strip()
            if not text:
                continue

            rect = annot.get("/Rect")
            if rect is None:
                continue
            try:
                raw_rect = [float(follow_ref(v)) for v in follow_ref(rect)]
                box = normalize_rect(raw_rect, page.mediabox, rotation)
            except (TypeError, ValueError):
                continue
            if box is None:
                continue

            words, method = position_field_words(
                annot, text, raw_rect, page.mediabox, rotation
            )
            fields.append(
                {
                    "name": build_field_name(annot),
                    "text": text,
                    **box,
                    "words": words,
                    "position_method": method,
                }
            )

        if fields:
            by_page[page_index] = fields

    return by_page
