"""
Extraction of the page's real text layer, with per-word boxes.
"""

import pypdfium2 as pdfium

from coordinates import normalize_rect

# PDFium sometimes reports a character's box as flat — no width or no height.
MIN_CHARBOX_SIZE = 1e-6


def load(pdf_bytes):
    """Open a PDF with the form environment initialised."""
    pdf = pdfium.PdfDocument(pdf_bytes)
    pdf.init_forms()
    return pdf


def extract_text_layer(pdf, page_number):
    """Return the text of a page and its words as normalized boxes.

    Returns `(text, [{"text", "x1", "x2", "y1", "y2", "start", "end"}, ...])`,
    coordinates in DocumentCloud's convention.
    """
    characters, charboxes, mediabox, rotation = read_page(pdf, page_number)

    words = []
    for start, end in locate_words(characters):
        box = measure_word(charboxes[start:end])
        normalized = normalize_rect(box, mediabox, rotation)
        if normalized is None:
            continue
        word = "".join(characters[start:end])
        words.append({"text": word, **normalized, "start": start, "end": end})

    return "".join(characters), words


def read_page(pdf, page_number):
    """Read one page's characters, their boxes and its geometry, then close it.

    Returns `(characters, charboxes, mediabox, rotation)`, one character and
    one box per position on the page.
    """
    page = pdf[page_number]
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        characters = [textpage.get_text_range(index, 1) for index in range(count)]
        charboxes = [textpage.get_charbox(index) for index in range(count)]
        return characters, charboxes, page.get_mediabox(), read_rotation(page)
    finally:
        textpage.close()
        page.close()


def read_rotation(page):
    """Return a page's rotation in degrees, 0 if it reports none usable."""
    try:
        return int(page.get_rotation() or 0) % 360
    except (TypeError, ValueError):
        return 0


def locate_words(characters):
    """Yield the `[start, end)` character range of each word."""
    start = None
    for index, char in enumerate(characters):
        # Whitespace separates words, and so does \x00, a stray byte in the
        # CERFA templates.
        if not char or char.isspace() or char == "\x00":
            if start is not None:
                yield start, index
            start = None
        elif start is None:
            start = index

    if start is not None:
        yield start, len(characters)


def measure_word(charboxes):
    """Return the box around one word's characters, in PDF space."""
    # A word whose characters all report a flat box still has to produce one:
    # DocumentCloud pairs the nth word with the nth position, so dropping a
    # word here would shift every word after it.
    solid = [charbox for charbox in charboxes if has_area(charbox)]
    lefts, bottoms, rights, tops = zip(*(solid or charboxes))
    return [min(lefts), min(bottoms), max(rights), max(tops)]


def has_area(charbox):
    """Whether a character's box has both a width and a height."""
    left, bottom, right, top = charbox
    return right - left >= MIN_CHARBOX_SIZE and top - bottom >= MIN_CHARBOX_SIZE
