"""A minimal, dependency-free vector PDF writer for test charts.

Only filled rectangles, filled polygons (device grey) and Helvetica text. The
output is deterministic (no creation date), uncompressed, one page.
"""

from __future__ import annotations

from camera_lab.two_d.charts.draw import Poly, Rect, Shape

PT_PER_MM = 72.0 / 25.4


def _num(v: float) -> str:
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _escape(text: str) -> bytes:
    raw = text.encode("latin-1", errors="replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _content(shapes: list[Shape], page_h_mm: float) -> bytes:
    def x(mm: float) -> str:
        return _num(mm * PT_PER_MM)

    def y(mm: float) -> str:  # PDF origin is bottom-left
        return _num((page_h_mm - mm) * PT_PER_MM)

    ops: list[bytes] = []
    for s in shapes:
        if isinstance(s, Rect):
            ops.append(
                f"{_num(s.grey)} g {x(s.x)} {y(s.y + s.h)} {_num(s.w * PT_PER_MM)} "
                f"{_num(s.h * PT_PER_MM)} re f".encode()
            )
        elif isinstance(s, Poly):
            (x0, y0), *rest = s.points
            path = [f"{_num(s.grey)} g {x(x0)} {y(y0)} m"]
            path += [f"{x(px)} {y(py)} l" for px, py in rest]
            ops.append((" ".join(path) + " h f").encode())
        else:
            ops.append(
                f"0 g BT /F1 {_num(s.size_pt)} Tf {x(s.x)} {y(s.y)} Td (".encode()
                + _escape(s.text)
                + b") Tj ET"
            )
    return b"\n".join(ops) + b"\n"


def render_pdf(shapes: list[Shape], page_mm: tuple[float, float], title: str) -> bytes:
    """Return a one-page PDF of ``shapes`` on a page of ``page_mm`` (w, h)."""
    return render_pdf_pages([(shapes, page_mm)], title)


def render_pdf_pages(pages: list[tuple[list[Shape], tuple[float, float]]], title: str) -> bytes:
    """Return a PDF with one page per ``(shapes, page_mm)``."""
    n = len(pages)
    font = 3 + 2 * n  # objects: catalog, pages, (page, content) per page, font, info
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode(),
    ]
    for i, (shapes, (w_mm, h_mm)) in enumerate(pages):
        content = _content(shapes, h_mm)
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(w_mm * PT_PER_MM)} "
                f"{_num(h_mm * PT_PER_MM)}] /Resources << /Font << /F1 {font} 0 R >> >> "
                f"/Contents {4 + 2 * i} 0 R >>"
            ).encode()
        )
        objects.append(
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream"
        )
    objects += [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Title (" + _escape(title) + b") /Producer (camera_lab) >>",
    ]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)
