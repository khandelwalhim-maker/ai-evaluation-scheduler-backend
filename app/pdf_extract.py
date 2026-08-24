from __future__ import annotations

import fitz
import pdfplumber


def extract_text_generic(path: str) -> str:
    doc = fitz.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _cluster_words_by_row(words: list[tuple], tolerance: float = 3.0) -> list[list[tuple]]:
    rows: list[list[tuple]] = []
    for word in sorted(words, key=lambda w: w[1]):
        for row in rows:
            if abs(row[0][1] - word[1]) <= tolerance:
                row.append(word)
                break
        else:
            rows.append([word])
    return rows


def _extract_grid_via_words(path: str) -> str:
    doc = fitz.open(path)
    try:
        out_lines: list[str] = []
        for page in doc:
            words = page.get_text("words")
            rows = _cluster_words_by_row(words, tolerance=3.0)
            rows.sort(key=lambda row: min(w[1] for w in row))
            for row in rows:
                row_sorted = sorted(row, key=lambda w: w[0])
                y_band = round(sum(w[1] for w in row_sorted) / len(row_sorted))
                text = " ".join(w[4] for w in row_sorted)
                out_lines.append(f"[y~{y_band}] {text}")
        return "\n".join(out_lines)
    finally:
        doc.close()


def _pdfplumber_rows(path: str) -> list[list[str]]:
    rows: list[list[str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for row in table:
                    rows.append([(c or "").strip() for c in row])
    return rows


def extract_grid(path: str) -> str:
    rows = _pdfplumber_rows(path)
    lines = [" | ".join(row) for row in rows if any(row)]

    if lines:
        return "\n".join(lines)

    return _extract_grid_via_words(path)


def extract_grid_chunks(path: str) -> list[str]:
    """Split a spatial grid into row-bands separated by blank rows (for example,
    one weekday per band), each serialized with the header row for column
    context. Falls back to a single chunk when the table has no blank-row
    structure or pdfplumber finds no table at all.
    """
    rows = _pdfplumber_rows(path)
    if not rows:
        text = _extract_grid_via_words(path)
        return [text] if text.strip() else []

    header, body = rows[0], rows[1:]
    header_line = " | ".join(header)

    bands: list[list[list[str]]] = []
    current: list[list[str]] = []
    for row in body:
        if any(row):
            current.append(row)
        elif current:
            bands.append(current)
            current = []
    if current:
        bands.append(current)

    if not bands:
        return [extract_grid(path)]

    return [
        "\n".join([header_line] + [" | ".join(row) for row in band])
        for band in bands
    ]


def ocr_fallback(path: str, dpi: int = 200) -> str:
    import pytesseract
    from PIL import Image

    doc = fitz.open(path)
    try:
        texts = []
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        for page in doc:
            pix = page.get_pixmap(matrix=matrix)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            texts.append(pytesseract.image_to_string(image))
        return "\n".join(texts)
    finally:
        doc.close()
