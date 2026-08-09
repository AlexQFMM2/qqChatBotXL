from __future__ import annotations

from dataclasses import dataclass


PAGE_WIDTH = 595.28
PAGE_HEIGHT = 841.89
LEFT_MARGIN = 48.0
RIGHT_MARGIN = 48.0
TOP_MARGIN = 52.0
BOTTOM_MARGIN = 48.0
BODY_FONT_SIZE = 12.0
BODY_LINE_HEIGHT = 20.0


@dataclass(frozen=True, slots=True)
class PdfLine:
    text: str
    size: float = BODY_FONT_SIZE
    centered: bool = False


def _safe_text(value: str) -> str:
    return "".join(
        character if character in "\t\n" or ord(character) >= 32 else " "
        for character in value
    )


def _pdf_hex(value: str) -> str:
    safe = "".join(character if ord(character) <= 0xFFFF else "□" for character in value)
    return safe.encode("utf-16-be").hex().upper()


def _character_width(character: str, font_size: float) -> float:
    if character == "\t":
        return font_size * 2.2
    if ord(character) < 128:
        return font_size * (0.33 if character in " .,;:!|'`il" else 0.58)
    return font_size


def _text_width(value: str, font_size: float) -> float:
    return sum(_character_width(character, font_size) for character in value)


def _wrap_line(value: str, max_width: float) -> list[str]:
    if not value:
        return [""]
    lines: list[str] = []
    current = ""
    current_width = 0.0
    for character in value.expandtabs(4):
        width = _character_width(character, BODY_FONT_SIZE)
        if current and current_width + width > max_width:
            lines.append(current.rstrip())
            current = character.lstrip() if character == " " else character
            current_width = _text_width(current, BODY_FONT_SIZE)
        else:
            current += character
            current_width += width
    lines.append(current.rstrip())
    return lines


def _layout(title: str, content: str) -> list[list[PdfLine]]:
    available_width = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN
    body_lines: list[PdfLine] = []
    normalized = _safe_text(content).replace("\r\n", "\n").replace("\r", "\n")
    for source_line in normalized.split("\n"):
        body_lines.extend(PdfLine(line) for line in _wrap_line(source_line, available_width))

    pages: list[list[PdfLine]] = []
    current: list[PdfLine] = []
    remaining_height = PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN
    clean_title = _safe_text(title).strip()[:120]
    if clean_title:
        current.append(PdfLine(clean_title, size=18.0, centered=True))
        remaining_height -= 38.0
    for line in body_lines:
        if remaining_height < BODY_LINE_HEIGHT:
            pages.append(current)
            current = []
            remaining_height = PAGE_HEIGHT - TOP_MARGIN - BOTTOM_MARGIN
        current.append(line)
        remaining_height -= BODY_LINE_HEIGHT
    pages.append(current)
    return pages


def _page_stream(lines: list[PdfLine]) -> bytes:
    commands: list[str] = []
    y = PAGE_HEIGHT - TOP_MARGIN
    for index, line in enumerate(lines):
        if index == 1 and lines[0].centered:
            y -= 18.0
        if line.centered:
            x = max(LEFT_MARGIN, (PAGE_WIDTH - _text_width(line.text, line.size)) / 2)
        else:
            x = LEFT_MARGIN
        if line.text:
            commands.extend(
                [
                    "BT",
                    f"/F1 {line.size:g} Tf",
                    f"1 0 0 1 {x:.2f} {y:.2f} Tm",
                    f"<{_pdf_hex(line.text)}> Tj",
                    "ET",
                ]
            )
        y -= BODY_LINE_HEIGHT
    return ("\n".join(commands) + "\n").encode("ascii")


def create_text_pdf(title: str, content: str) -> bytes:
    """Create a small, dependency-free UTF-8 text PDF using a standard CJK font."""
    pages = _layout(title, content)
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: (
            b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light "
            b"/Encoding /UniGB-UCS2-H /DescendantFonts [4 0 R] >>"
        ),
        4: (
            b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
            b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> "
            b"/DW 1000 /W [0 127 500] >>"
        ),
    }
    page_ids: list[int] = []
    for index, lines in enumerate(pages):
        page_id = 5 + index * 2
        content_id = page_id + 1
        page_ids.append(page_id)
        stream = _page_stream(lines)
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH:.2f} {PAGE_HEIGHT:.2f}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"endstream"
        )
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode(
        "ascii"
    )

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {0: 0}
    for object_id in sorted(objects):
        offsets[object_id] = len(output)
        output.extend(f"{object_id} 0 obj\n".encode("ascii"))
        output.extend(objects[object_id])
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    size = max(objects) + 1
    output.extend(f"xref\n0 {size}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for object_id in range(1, size):
        output.extend(f"{offsets[object_id]:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode(
            "ascii"
        )
    )
    return bytes(output)
