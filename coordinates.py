
def normalize_rect(rect, page_box, rotation):
    """Convert a PDF rectangle to DocumentCloud's coordinate convention."""
    left = float(min(page_box[0], page_box[2]))
    bottom = float(min(page_box[1], page_box[3]))
    width = abs(float(page_box[2]) - float(page_box[0]))
    height = abs(float(page_box[3]) - float(page_box[1]))
    if not width or not height:
        return None

    x_lo = (min(rect[0], rect[2]) - left) / width
    x_hi = (max(rect[0], rect[2]) - left) / width
    # Flip the y axis: PDF measures up from the bottom, DocumentCloud down
    # from the top, so the rect's top edge is its larger y value.
    y_lo = 1.0 - (max(rect[1], rect[3]) - bottom) / height
    y_hi = 1.0 - (min(rect[1], rect[3]) - bottom) / height

    corners = [(x_lo, y_lo), (x_hi, y_hi)]
    if rotation:
        rotated = []
        for x, y in corners:
            if rotation == 90:
                rotated.append((1.0 - y, x))
            elif rotation == 180:
                rotated.append((1.0 - x, 1.0 - y))
            elif rotation == 270:
                rotated.append((y, 1.0 - x))
            else:
                rotated.append((x, y))
        corners = rotated

    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    clamp = lambda v: max(0.0, min(1.0, v))
    return {
        "x1": clamp(min(xs)),
        "x2": clamp(max(xs)),
        "y1": clamp(min(ys)),
        "y2": clamp(max(ys)),
    }
