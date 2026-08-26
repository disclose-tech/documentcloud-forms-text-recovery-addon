"""
Position a recovered field value sits inside its widget.
"""

from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, ContentStream

from pdf_objects import follow_ref

try:
    from pypdf._codecs.core_font_metrics import CORE_FONT_METRICS
except ImportError:
    # A private module, pinned in requirements but not promised.
    CORE_FONT_METRICS = {}

# How a field's boxes were placed.  Ordered best first.
MEASURED = "measured"
PER_LINE = "per line"
SPREAD = "spread"

NO_SCALE_OR_ROTATION = (1.0, 0.0, 0.0, 1.0)
NO_TRANSFORM = NO_SCALE_OR_ROTATION + (0.0, 0.0)


class PositionedWords:
    """Word boxes for the text of one field, and how they were arrived at."""

    def __init__(self, boxes, method, note=None):
        self.boxes = boxes
        self.method = method
        self.note = note

    @property
    def label(self):
        return f"{self.method} ({self.note})" if self.note else self.method


class TextSpan:
    """A stretch of text drawn from one starting point on one baseline."""

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.text = ""


class Font:
    """A font reduced to what a word box needs: widths, ascent, descent."""

    def __init__(self, widths, default_width, ascent, descent, substituted_font):
        self.widths = widths
        self.default_width = default_width
        self.ascent = ascent
        self.descent = descent
        self.substituted_font = substituted_font


def read_spans(appearance):
    """Read what an appearance draws, as `(font_name, size, spans)`, or None."""

    # The text operators the loop below knows how to follow
    followed = {b"Tf", b"Tm", b"Td", b"Tj", b"TJ"}

    font_name = None
    size = None
    spans = []
    x = y = 0.0
    moved = True

    try:
        operations = ContentStream(appearance, None).operations
    except (PdfReadError, ValueError, TypeError, KeyError, IndexError):
        # One unparseable widget costs its own field's precision; letting it
        # raise would cost the document.
        return None

    for operands, operator in operations:
        if operator == b"Tf":
            if len(operands) != 2:
                return None
            font_name = str(operands[0])
            size = float(operands[1])
        elif operator == b"Tm":
            if len(operands) != 6:
                return None
            matrix = tuple(float(value) for value in operands)
            if matrix[:4] != NO_SCALE_OR_ROTATION:
                return None
            x, y = matrix[4], matrix[5]
            moved = True
        elif operator == b"Td":
            if len(operands) != 2:
                return None
            x += float(operands[0])
            y += float(operands[1])
            moved = True
        elif operator == b"BT":
            x = y = 0.0
            moved = True
        elif operator in (b"Tj", b"TJ"):
            text = read_drawn_text(operands, operator)
            if text is None:
                return None
            if moved or not spans:
                spans.append(TextSpan(x, y))
                moved = False
            spans[-1].text += text
        elif operator == b"cm":
            # `cm` shifts and resizes everything drawn after it, and the
            # positions recorded above ignore it.  Only one that changes
            # nothing is safe.
            if len(operands) != 6 or tuple(float(v) for v in operands) != NO_TRANSFORM:
                return None
        elif operator.startswith(b"T") and operator not in followed:
            return None

    if not size or font_name is None or not spans:
        return None
    return font_name, size, spans


def read_drawn_text(operands, operator):
    """Read the string a text operator draws, or None if we cannot follow it."""
    if not operands:
        return None
    if operator == b"Tj":
        return str(operands[0])
    if not isinstance(operands[0], ArrayObject):
        return None
    parts = []
    for element in operands[0]:
        # A number sets spacing by hand, and widths are all we can measure.
        if isinstance(element, (int, float)):
            return None
        parts.append(str(element))
    return "".join(parts)


def lookup_core_metrics(name):
    """Find a standard font's metrics, looking past any prefix on the name."""
    if not name:
        return None
    name = name.lstrip("/")
    if len(name) > 7 and name[6] == "+":
        name = name[7:]
    return CORE_FONT_METRICS.get(name)


def choose_substitute_font(flags):
    """Choose a stand-in font from the flags describing the one we lack."""
    if flags & 1:
        return "Courier"
    if flags & 2:
        return "Times-Roman"
    return "Helvetica"


def find_font(resources, font_name):
    """Find the font a stream draws with, standing one in when widths are missing."""
    fonts = follow_ref(follow_ref(resources or {}).get("/Font") or {})
    entry = follow_ref(fonts.get(font_name)) if font_name else None
    entry = entry if hasattr(entry, "get") else {}
    descriptor = follow_ref(entry.get("/FontDescriptor")) or {}

    ascent = descent = None
    if descriptor:
        try:
            ascent = float(follow_ref(descriptor.get("/Ascent")))
            descent = float(follow_ref(descriptor.get("/Descent")))
        except (TypeError, ValueError):
            ascent = descent = None

    metrics = lookup_core_metrics(str(entry.get("/BaseFont") or font_name or ""))
    substituted_font = None
    if metrics is None:
        try:
            flags = int(follow_ref(descriptor.get("/Flags")) or 0)
        except (TypeError, ValueError):
            flags = 0
        substituted_font = choose_substitute_font(flags)
        metrics = CORE_FONT_METRICS.get(substituted_font)

    # Widths and box height are looked up apart, because a font commonly
    # carries one without the other.
    widths = read_embedded_widths(entry, descriptor)
    if widths is None:
        if metrics is None:
            return None
        table = dict(metrics.character_widths)
        widths = (table, table.pop("default", 500))
    else:
        substituted_font = None

    if ascent is None and metrics is not None:
        ascent = float(metrics.font_descriptor.ascent)
        descent = float(metrics.font_descriptor.descent)
    if ascent is None or descent is None:
        return None
    return Font(widths[0], widths[1], ascent, descent, substituted_font)


def read_embedded_widths(font, descriptor):
    """Read a font's own character widths, as `(widths, default_width)` or None."""
    array = follow_ref(font.get("/Widths"))
    first = follow_ref(font.get("/FirstChar"))
    if array is None or first is None:
        return None
    try:
        first = int(first)
        values = [float(follow_ref(value)) for value in array]
    except (TypeError, ValueError):
        return None
    if not values:
        return None

    widths = {}
    for offset, value in enumerate(values):
        try:
            widths[bytes([first + offset]).decode("cp1252")] = value
        except (ValueError, UnicodeDecodeError):
            continue
    try:
        default_width = float(follow_ref(descriptor.get("/MissingWidth")) or 0)
    except (TypeError, ValueError):
        default_width = 0.0
    return widths, default_width


def measure_span_words(span, font, size):
    """Measure where each word of a span starts and ends, as `(word, x0, x1)`."""
    boxes = []
    cursor = span.x
    start = None
    word = []

    for char in span.text:
        width = font.widths.get(char, font.default_width) * size / 1000.0
        if char.isspace():
            if word:
                boxes.append(("".join(word), start, cursor))
                word = []
                start = None
        else:
            if not word:
                start = cursor
            word.append(char)
        cursor += width

    if word:
        boxes.append(("".join(word), start, cursor))
    return boxes


def scale_to_fit(spans, placed, width):
    """Shrink the words until the widest line fits, as `(boxes, clamped)`."""
    # The text was wrapped to fit this box, so a line that comes out wider than
    # the box means our widths are too big.  Allow 2% for rounding.
    slack = 1.02

    widest = 0.0
    for span, boxes in zip(spans, placed):
        if boxes:
            widest = max(widest, boxes[-1][2] - span.x)
    if widest <= width * slack or not widest:
        return placed, False

    scale = width / widest
    return [
        [
            (word, span.x + (x0 - span.x) * scale, span.x + (x1 - span.x) * scale)
            for word, x0, x1 in boxes
        ]
        for span, boxes in zip(spans, placed)
    ], True


def position_words(annot, text):
    """One box per word of `text`, `(word, x0, x1, y0, y1)` in page points, or None."""
    appearance = follow_ref(follow_ref(annot.get("/AP") or {}).get("/N"))
    if appearance is None or not hasattr(appearance, "get_data"):
        return None

    try:
        drawing_box = [
            float(follow_ref(v)) for v in follow_ref(appearance.get("/BBox"))
        ]
        field_box = [float(follow_ref(v)) for v in follow_ref(annot.get("/Rect"))]
    except (TypeError, ValueError):
        return None

    matrix = follow_ref(appearance.get("/Matrix"))
    if matrix is not None:
        try:
            if tuple(float(follow_ref(v)) for v in matrix) != NO_TRANSFORM:
                return None
        except (TypeError, ValueError):
            return None

    drawing_width = abs(drawing_box[2] - drawing_box[0])
    drawing_height = abs(drawing_box[3] - drawing_box[1])
    field_width = abs(field_box[2] - field_box[0])
    field_height = abs(field_box[3] - field_box[1])
    # `dx` and `dy` below shift the drawing onto the page without resizing it.
    rounding = 0.01
    if (
        abs(drawing_width - field_width) > rounding
        or abs(drawing_height - field_height) > rounding
    ):
        return None

    parsed = read_spans(appearance)
    if parsed is None:
        return None
    font_name, size, spans = parsed

    words = text.split()
    if " ".join(span.text for span in spans).split() != words:
        return None

    font = find_font(follow_ref(appearance.get("/Resources")), font_name)
    dx = min(field_box[0], field_box[2]) - min(drawing_box[0], drawing_box[2])
    dy = min(field_box[1], field_box[3]) - min(drawing_box[1], drawing_box[3])

    if font is None:
        return spread_words_per_line(
            spans, words, drawing_width, drawing_box, dx, dy, size
        )

    placed, clamped = scale_to_fit(
        spans, [measure_span_words(span, font, size) for span in spans], drawing_width
    )
    top = font.ascent * size / 1000.0
    bottom = font.descent * size / 1000.0

    boxes = []
    overflowed = False
    for span, span_boxes in zip(spans, placed):
        baseline = keep_baseline_inside(span.y + dy, top, bottom, field_box)
        # A line moved by less than its own height still sits on the words it
        # was drawn for; more than that means it came from outside the field.
        overflowed = overflowed or abs(baseline - (span.y + dy)) > top - bottom
        for word, x0, x1 in span_boxes:
            boxes.append(
                clip_to_widget(
                    (word, x0 + dx, x1 + dx, baseline + bottom, baseline + top),
                    field_box,
                )
            )

    if len(boxes) != len(words):
        return None

    notes = [
        note for note in (font.substituted_font, "clamped" if clamped else None) if note
    ]
    if overflowed:
        notes.append("overflowing")
    return PositionedWords(boxes, MEASURED, ", ".join(notes) or None)


def keep_baseline_inside(baseline, top, bottom, field_box):
    """Slide a baseline until a whole line box fits inside the widget."""
    # If the text is longer that the box, the viewer clips the overflow away. Those
    # words are still in the text we send, so they need a box a reader can see.
    low = min(field_box[1], field_box[3]) - bottom
    high = max(field_box[1], field_box[3]) - top
    if low > high:
        # The line is taller than the field, so we centre it: the overhang splits
        # evenly and gets clipped away.
        return (low + high) / 2.0
    return min(max(baseline, low), high)


def spread_words_per_line(spans, words, width, drawing_box, dx, dy, size):
    """Spread each line's words across the widget when no widths can be found."""
    # Without widths the spacing across the line is guesswork, but each word
    # still sits on the line the stream drew it on.
    left = min(drawing_box[0], drawing_box[2])
    boxes = []
    for span in spans:
        span_words = span.text.split()
        if not span_words:
            continue
        lengths = [max(len(word), 1) for word in span_words]
        scale = width / (sum(lengths) + len(span_words) - 1)
        cursor = left
        for word, length in zip(span_words, lengths):
            word_width = length * scale
            boxes.append(
                (
                    word,
                    cursor + dx,
                    cursor + word_width + dx,
                    span.y + dy,
                    span.y + size + dy,
                )
            )
            cursor += word_width + scale

    if len(boxes) != len(words):
        return None
    return PositionedWords(boxes, PER_LINE)


def clip_to_widget(box, field_box):
    """Hold a box inside the widget, where the letters themselves are cut off."""
    word, x0, x1, y0, y1 = box
    left, right = min(field_box[0], field_box[2]), max(field_box[0], field_box[2])
    bottom, top = min(field_box[1], field_box[3]), max(field_box[1], field_box[3])
    return (
        word,
        min(max(x0, left), right),
        min(max(x1, left), right),
        min(max(y0, bottom), top),
        min(max(y1, bottom), top),
    )
