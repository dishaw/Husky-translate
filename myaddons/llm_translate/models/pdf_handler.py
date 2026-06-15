"""PDF document handler for llm_translate.

Uses PyMuPDF (fitz) to:
- Extract paragraphs and tables from PDF files
- Rebuild translated PDF by overlaying translated text
"""

import io
import json
import logging
import re

_logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
    _logger.warning("PyMuPDF (fitz) not installed. PDF support unavailable.")


# ---------------------------------------------------------------------------
# Token estimation (same heuristic as docx_handler)
# ---------------------------------------------------------------------------

def estimate_tokens(text):
    """Rough token estimate: ~1.3 tokens per CJK char, ~0.25 per latin word."""
    if not text:
        return 0
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))
    latin_words = len(re.findall(r'[a-zA-Z]+', text))
    return int(cjk * 1.3 + latin_words * 1.3 + len(re.findall(r'\d+', text)))


# ---------------------------------------------------------------------------
# Extract paragraphs from PDF
# ---------------------------------------------------------------------------

def extract_pdf_as_page_images(file_content, dpi=200):
    """Extract PDF pages as images with text for translation.

    Each page becomes one paragraph entry containing:
    - An image (base64 PNG) of the rendered page
    - The raw extracted text as source_text for translation reference

    Args:
        file_content: Binary content of the PDF file.
        dpi: Resolution for rendering (default 200 for good quality).

    Returns:
        dict with "paragraphs" list, each entry representing one page.
    """
    if fitz is None:
        raise ImportError(
            "PyMuPDF is required for PDF support. "
            "Install with: pip install PyMuPDF"
        )

    doc = fitz.open(stream=file_content, filetype="pdf")
    paragraphs = []

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    for page_num in range(len(doc)):
        page = doc[page_num]

        # Render page to PNG image
        pix = page.get_pixmap(matrix=matrix)
        img_bytes = pix.tobytes("png")
        import base64
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        img_data_uri = f"data:image/png;base64,{img_b64}"

        # Also extract text for reference / searchability
        page_text = page.get_text("text").strip()

        paragraphs.append({
            "text": "",  # No text display - image only
            "style": "Normal",
            "alignment": "LEFT",
            "bold": False,
            "font_size": 11,
            "runs": [],
            "images": [{"data_uri": img_data_uri, "width": pix.width, "height": pix.height}],
            "textboxes": [],
            "is_empty": False,
            "para_index": page_num,
            "numbering_prefix": None,
            "numbering_level": None,
            "pdf_page": page_num,
            "is_pdf_page_image": True,
        })

    doc.close()

    return {
        "paragraphs": paragraphs,
        "header_text": "",
        "footer_text": "",
        "header_images": [],
        "footer_images": [],
        "is_pdf": True,
        "page_count": len(paragraphs),
    }


def extract_paragraphs_from_pdf(file_content):
    """Extract paragraphs and tables from a PDF file.

    Uses a line-by-line approach: each visual line in the PDF becomes one
    paragraph entry, preserving reading order (top→bottom, left→right).
    Font, size, color, bold/italic info are captured per span (run).

    Args:
        file_content: Binary content of the PDF file.

    Returns:
        dict with "paragraphs", "is_pdf", "page_count", etc.
    """
    if fitz is None:
        raise ImportError(
            "PyMuPDF is required for PDF support. "
            "Install with: pip install PyMuPDF"
        )

    doc = fitz.open(stream=file_content, filetype="pdf")
    paragraphs = []
    pdf_pages_meta = []

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_rect = page.rect
        pdf_pages_meta.append({
            "page_num": page_num,
            "width": page_rect.width,
            "height": page_rect.height,
        })

        # ── 1. Table extraction ────────────────────────────────────
        table_rects = []
        try:
            tab_finder = page.find_tables()
            if tab_finder and tab_finder.tables:
                for tbl_idx, table in enumerate(tab_finder.tables):
                    tbl_rect = fitz.Rect(table.bbox)
                    table_rects.append(tbl_rect)
                    rows = table.extract()
                    col_count = table.col_count if hasattr(table, 'col_count') else (
                        len(rows[0]) if rows else 0
                    )
                    for row_idx, row_data in enumerate(rows):
                        cells = []
                        cell_texts = []
                        for col_idx, cell_text in enumerate(row_data):
                            text = (cell_text or "").strip()
                            cells.append({
                                "col_index": col_idx,
                                "grid_span": 1,
                                "row_span": 1,
                                "text": text,
                                "bold": row_idx == 0,
                                "font_size": None,
                                "runs": [{"text": text, "bold": row_idx == 0,
                                          "italic": False, "underline": False}],
                            })
                            cell_texts.append(text)

                        if not any(c.strip() for c in cell_texts):
                            continue

                        paragraphs.append({
                            "text": " [CELL] ".join(cell_texts),
                            "style": "Table Row",
                            "alignment": None,
                            "bold": row_idx == 0,
                            "font_size": None,
                            "runs": [],
                            "images": [],
                            "textboxes": [],
                            "is_empty": False,
                            "para_index": None,
                            "is_table_row": True,
                            "table_index": tbl_idx,
                            "row_index": row_idx,
                            "cells": cells,
                            "table_row_count": len(rows),
                            "table_col_count": col_count,
                            "numbering_prefix": None,
                            "numbering_level": None,
                            "pdf_page": page_num,
                            "pdf_bbox": list(table.bbox),
                            # y-position for sorting among text lines
                            "_sort_y": tbl_rect.y0 + row_idx * 0.01,
                        })
        except Exception as e:
            _logger.debug("Table extraction failed on page %d: %s", page_num, e)

        # ── 2. Extract every text line individually ────────────────
        blocks = page.get_text("dict",
                               flags=fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_DEHYPHENATE
                               )["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue

            block_rect = fitz.Rect(block["bbox"])

            # Skip if block overlaps a detected table
            in_table = False
            for tr in table_rects:
                if block_rect.intersects(tr):
                    overlap = block_rect & tr
                    if overlap.get_area() > block_rect.get_area() * 0.3:
                        in_table = True
                        break
            if in_table:
                continue

            # Each *line* in the block becomes a separate paragraph
            for line in block.get("lines", []):
                line_bbox = line["bbox"]
                spans = line.get("spans", [])
                if not spans:
                    continue

                runs_data = []
                line_texts = []
                first_bold = None
                first_font_size = None

                for span in spans:
                    text = span.get("text", "")
                    if not text:
                        continue
                    line_texts.append(text)

                    flags = span.get("flags", 0)
                    is_bold = bool(flags & (1 << 4))
                    is_italic = bool(flags & (1 << 1))
                    font_size = round(span.get("size", 0), 1)
                    font_name = span.get("font", "")
                    color_int = span.get("color", 0)

                    run = {
                        "text": text,
                        "bold": is_bold,
                        "italic": is_italic,
                        "underline": False,
                    }
                    if font_size:
                        run["font_size"] = font_size
                    if font_name:
                        run["font_name"] = font_name
                    if color_int and color_int != 0:
                        run["color"] = f"{color_int:06X}"
                    runs_data.append(run)

                    if first_font_size is None:
                        first_bold = is_bold
                        first_font_size = font_size

                full_text = "".join(line_texts).strip()
                if not full_text:
                    continue

                # Detect alignment from line position
                line_cx = (line_bbox[0] + line_bbox[2]) / 2
                page_cx = page_rect.width / 2
                line_left = line_bbox[0]
                line_right = line_bbox[2]
                line_width = line_right - line_left

                if line_width < page_rect.width * 0.4 and abs(line_cx - page_cx) < 30:
                    alignment = "CENTER"
                elif line_left > page_rect.width * 0.55:
                    alignment = "RIGHT"
                else:
                    alignment = "LEFT"

                # Detect style
                style = "Normal"
                if first_font_size:
                    if first_font_size >= 18:
                        style = "Heading 1"
                    elif first_font_size >= 14:
                        style = "Heading 2"
                    elif first_font_size >= 12 and first_bold:
                        style = "Heading 3"

                paragraphs.append({
                    "text": full_text,
                    "style": style,
                    "alignment": alignment,
                    "bold": first_bold or False,
                    "font_size": first_font_size,
                    "runs": runs_data,
                    "images": [],
                    "textboxes": [],
                    "is_empty": False,
                    "para_index": None,
                    "numbering_prefix": None,
                    "numbering_level": None,
                    "pdf_page": page_num,
                    "pdf_bbox": list(line_bbox),
                    "_sort_y": line_bbox[1],  # top-y for sorting
                })

    # ── 3. Sort all entries by page, then Y position, then X ───────
    for idx, p in enumerate(paragraphs):
        p["_sort_page"] = p.get("pdf_page", 0)
        if "_sort_y" not in p:
            p["_sort_y"] = 0
        p["_sort_x"] = p.get("pdf_bbox", [0])[0] if p.get("pdf_bbox") else 0

    paragraphs.sort(key=lambda p: (p["_sort_page"], p["_sort_y"], p["_sort_x"]))

    # Clean up temp sort keys
    for p in paragraphs:
        p.pop("_sort_y", None)
        p.pop("_sort_x", None)
        p.pop("_sort_page", None)

    doc.close()

    return {
        "paragraphs": paragraphs,
        "header_text": "",
        "footer_text": "",
        "header_images": [],
        "footer_images": [],
        "is_pdf": True,
        "page_count": len(pdf_pages_meta),
        "pdf_pages_meta": pdf_pages_meta,
    }


# ---------------------------------------------------------------------------
# Rebuild translated PDF
# ---------------------------------------------------------------------------

def rebuild_pdf_with_ocr(original_content, ocr_pages):
    """Build translated PDF from OCR results using page images as background.

    This method rasterizes each source page first, then draws translated text
    blocks on top. It avoids coordinate / rotation inconsistencies for pages
    with landscape orientation or PDF rotation flags.

    Args:
        original_content: bytes of the original PDF.
        ocr_pages: dict mapping page_num -> list of text_block dicts.
            Each text_block: {original, translated, x_pct, y_pct, w_pct, h_pct}

    Returns:
        bytes: The translated PDF.
    """
    if fitz is None:
        raise ImportError("PyMuPDF is required for PDF rebuild.")

    src_doc = fitz.open(stream=original_content, filetype="pdf")
    out_doc = fitz.open()

    # Keep moderate resolution to balance quality and file size
    render_matrix = fitz.Matrix(2.0, 2.0)

    for page_num in range(len(src_doc)):
        src_page = src_doc[page_num]
        page_rect = src_page.rect
        pw = page_rect.width
        ph = page_rect.height

        # 1) Render source page as image background (rotation already reflected)
        pix = src_page.get_pixmap(matrix=render_matrix, alpha=False)
        bg_png = pix.tobytes("png")

        out_page = out_doc.new_page(width=pw, height=ph)
        out_page.insert_image(out_page.rect, stream=bg_png)

        # 2) Overlay translated text blocks (same coordinate basis as preview)
        blocks = ocr_pages.get(page_num, []) or []
        for block in blocks:
            translated = (block.get("translated") or "").strip()
            if not translated:
                continue

            x_pct = float(block.get("x_pct", 0) or 0)
            y_pct = float(block.get("y_pct", 0) or 0)
            w_pct = float(block.get("w_pct", 10) or 10)
            h_pct = float(block.get("h_pct", 5) or 5)

            x0 = pw * x_pct / 100.0
            y0 = ph * y_pct / 100.0
            box_w = pw * w_pct / 100.0
            box_h = ph * h_pct / 100.0

            if box_w < 4 or box_h < 3:
                continue

            fontname = "china-ss" if _has_cjk(translated) else "helv"

            # --- Smart font size: estimate from box area & text length ---
            # Average char width ≈ 0.55 * fontsize for CJK, 0.5 for latin
            char_w_ratio = 0.6 if _has_cjk(translated) else 0.5
            text_len = max(1, len(translated))
            # Area-based estimation: box area / text length = area per char
            # area per char = char_w * line_h ≈ (ratio*fs) * (1.2*fs)
            area = max(1, (box_w - 2) * (box_h - 2))
            area_per_char = area / text_len
            # fs^2 * ratio * 1.2 = area_per_char → fs = sqrt(area_per_char/(ratio*1.2))
            import math
            estimated_fs = math.sqrt(area_per_char / (char_w_ratio * 1.2)) * 1.5
            # Also limit by box height (single-line text shouldn't exceed box)
            max_by_height = box_h * 0.9
            start_font = min(estimated_fs, max_by_height)
            start_font = max(3, min(start_font, 72))  # sane range 3-72pt

            _logger.debug(
                "PDF block: text=%r  box=%.0fx%.0f  estimated_fs=%.1f  start=%.1f",
                translated[:30], box_w, box_h, estimated_fs, start_font,
            )

            # --- Find a font that fits, expanding box height if necessary ---
            final_font = start_font
            final_rect_h = box_h
            fontsize = start_font

            # Phase 1: try shrinking font to fit original box
            fit_ok = False
            for _ in range(20):
                if fontsize < 3:
                    break
                insert_rect = fitz.Rect(x0 + 1, y0 + 1, x0 + box_w - 1, y0 + final_rect_h - 1)
                if insert_rect.width < 4 or insert_rect.height < 4:
                    break
                test_doc = fitz.open()
                test_page = test_doc.new_page(
                    width=max(20, insert_rect.width + 4),
                    height=max(20, insert_rect.height + 4),
                )
                test_r = fitz.Rect(2, 2, 2 + insert_rect.width, 2 + insert_rect.height)
                test_rc = test_page.insert_textbox(
                    test_r, translated,
                    fontsize=fontsize, fontname=fontname, color=(0, 0, 0),
                )
                test_doc.close()
                if test_rc >= 0:
                    final_font = fontsize
                    fit_ok = True
                    break
                fontsize *= 0.85

            # Phase 2: if still doesn't fit at 3pt, expand box downward
            if not fit_ok:
                fontsize = max(4, start_font * 0.5)  # use a readable size
                for expand in range(1, 8):
                    trial_h = box_h * (1 + expand * 0.5)
                    # Don't expand beyond page bottom
                    if y0 + trial_h > ph:
                        trial_h = ph - y0
                    insert_rect = fitz.Rect(x0 + 1, y0 + 1, x0 + box_w - 1, y0 + trial_h - 1)
                    if insert_rect.height < 4:
                        break
                    test_doc = fitz.open()
                    test_page = test_doc.new_page(
                        width=max(20, insert_rect.width + 4),
                        height=max(20, insert_rect.height + 4),
                    )
                    test_r = fitz.Rect(2, 2, 2 + insert_rect.width, 2 + insert_rect.height)
                    test_rc = test_page.insert_textbox(
                        test_r, translated,
                        fontsize=fontsize, fontname=fontname, color=(0, 0, 0),
                    )
                    test_doc.close()
                    if test_rc >= 0:
                        final_font = fontsize
                        final_rect_h = trial_h
                        fit_ok = True
                        break

            # Phase 3: absolute fallback - just use 3pt, never skip
            if not fit_ok:
                final_font = 3
                final_rect_h = max(box_h, min(ph - y0, box_h * 4))

            draw_rect = fitz.Rect(x0, y0, x0 + box_w, y0 + final_rect_h)
            insert_rect = draw_rect + (1, 1, -1, -1)

            out_page.draw_rect(draw_rect, color=None, fill=(1, 1, 1), width=0)
            rc = out_page.insert_textbox(
                insert_rect, translated,
                fontsize=final_font, fontname=fontname, color=(0, 0, 0),
            )
            if rc < 0:
                _logger.warning(
                    "PDF text overflow even after expansion: text=%r fs=%.1f box=%.0fx%.0f",
                    translated[:40], final_font, insert_rect.width, insert_rect.height,
                )

    output = io.BytesIO()
    out_doc.save(output, garbage=4, deflate=True)
    out_doc.close()
    src_doc.close()
    return output.getvalue()


def rebuild_pdf_from_original(original_content, rebuild_data):
    """Create a translated PDF by overlaying translated text on the original.

    Strategy: For each text block in the original, find the corresponding
    translation and redact the original text, then insert translated text.

    Args:
        original_content: Binary content of the original PDF.
        rebuild_data: dict with "paragraphs" list from _prepare_rebuild_data().

    Returns:
        bytes: The translated PDF content.
    """
    if fitz is None:
        raise ImportError("PyMuPDF is required for PDF rebuild.")

    doc = fitz.open(stream=original_content, filetype="pdf")

    # Build a map: page_num -> list of (bbox, translated_text, style_metadata)
    page_translations = {}
    table_translations = {}  # (page, table_index, row_index) -> [cell_texts]

    for para in rebuild_data.get("paragraphs", []):
        meta = para.get("style_metadata", {})
        translated = para.get("translated_text", "")
        if not translated:
            continue

        pdf_page = meta.get("pdf_page")
        if pdf_page is None:
            continue

        if meta.get("is_table_row"):
            # Table row: split by [CELL]
            tbl_key = (pdf_page, meta.get("table_index", 0), meta.get("row_index", 0))
            cells = [c.strip() for c in translated.split("[CELL]")]
            table_translations[tbl_key] = cells
        else:
            bbox = meta.get("pdf_bbox")
            if bbox:
                page_translations.setdefault(pdf_page, []).append({
                    "bbox": bbox,
                    "text": translated,
                    "font_size": meta.get("font_size", 11),
                    "bold": meta.get("bold", False),
                    "alignment": meta.get("alignment", "LEFT"),
                })

    # ── Process table translations ──────────────────────────────────
    for page_num in range(len(doc)):
        page = doc[page_num]

        # Replace table cells
        try:
            tab_finder = page.find_tables()
            if tab_finder and tab_finder.tables:
                for tbl_idx, table in enumerate(tab_finder.tables):
                    rows = table.extract()
                    for row_idx, row_data in enumerate(rows):
                        tbl_key = (page_num, tbl_idx, row_idx)
                        if tbl_key not in table_translations:
                            continue
                        translated_cells = table_translations[tbl_key]
                        # Get cell bounding boxes from table
                        for col_idx, cell_text in enumerate(row_data):
                            if col_idx >= len(translated_cells):
                                break
                            new_text = translated_cells[col_idx]
                            if not new_text or new_text == (cell_text or "").strip():
                                continue
                            # Find cell bbox
                            try:
                                cell_bbox = table.cells[row_idx * table.col_count + col_idx]
                                if cell_bbox:
                                    rect = fitz.Rect(cell_bbox)
                                    _replace_text_in_rect(
                                        page, rect, new_text,
                                        fontsize=8, bold=(row_idx == 0),
                                    )
                            except (IndexError, AttributeError):
                                pass
        except Exception as e:
            _logger.debug("Table rebuild on page %d: %s", page_num, e)

    # ── Process paragraph translations ──────────────────────────────
    for page_num, translations in page_translations.items():
        if page_num >= len(doc):
            continue
        page = doc[page_num]
        for t in translations:
            bbox = t["bbox"]
            rect = fitz.Rect(bbox)
            fontsize = t.get("font_size", 11) or 11
            # Clamp font size
            if fontsize > 30:
                fontsize = 30
            elif fontsize < 6:
                fontsize = 6
            _replace_text_in_rect(
                page, rect, t["text"],
                fontsize=fontsize, bold=t.get("bold", False),
                alignment=t.get("alignment", "LEFT"),
            )

    # Write output
    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue()


def _replace_text_in_rect(page, rect, new_text, fontsize=11, bold=False,
                          alignment="LEFT"):
    """Replace text in a rectangular area of a PDF page.

    Redacts original content and inserts new text.
    """
    # Add some padding to ensure we cover the text
    redact_rect = rect + (-1, -1, 1, 1)

    # Add redaction annotation (white fill to cover original)
    page.add_redact_annot(redact_rect, text="", fill=(1, 1, 1))
    page.apply_redactions()

    # Choose font
    fontname = "china-ss" if _has_cjk(new_text) else "helv"
    if bold and not _has_cjk(new_text):
        fontname = "hebo"

    # Calculate text alignment
    text_align = fitz.TEXT_ALIGN_LEFT
    if alignment == "CENTER":
        text_align = fitz.TEXT_ALIGN_CENTER
    elif alignment == "RIGHT":
        text_align = fitz.TEXT_ALIGN_RIGHT
    elif alignment == "JUSTIFY":
        text_align = fitz.TEXT_ALIGN_JUSTIFY

    # Insert the translated text within the original rect (no padding change)
    # Keep original font size; text will wrap automatically within the rect.
    insert_rect = rect + (2, 1, -2, -1)

    page.insert_textbox(
        insert_rect,
        new_text,
        fontsize=fontsize,
        fontname=fontname,
        align=text_align,
        color=(0, 0, 0),
    )


def _has_cjk(text):
    """Check if text contains CJK characters."""
    return bool(re.search(r'[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]', text))


# ---------------------------------------------------------------------------
# Simple rebuild (no original PDF) - creates new PDF from scratch
# ---------------------------------------------------------------------------

def rebuild_pdf(rebuild_data):
    """Create a new PDF from translated paragraphs.

    Used when original PDF is not available.

    Args:
        rebuild_data: dict with "paragraphs" list.

    Returns:
        bytes: PDF file content.
    """
    if fitz is None:
        raise ImportError("PyMuPDF is required for PDF rebuild.")

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    y_pos = 50  # Starting Y position
    margin_left = 50
    margin_right = 545
    page_bottom = 792

    fontname = "china-ss"

    for para in rebuild_data.get("paragraphs", []):
        meta = para.get("style_metadata", {})
        text = para.get("translated_text", "")
        if not text:
            y_pos += 12
            continue

        fontsize = meta.get("font_size", 11) or 11
        if fontsize > 24:
            fontsize = 24

        # Check if we need a new page
        estimated_height = fontsize * 1.5 * (1 + len(text) // 60)
        if y_pos + estimated_height > page_bottom:
            page = doc.new_page(width=595, height=842)
            y_pos = 50

        rect = fitz.Rect(margin_left, y_pos, margin_right, page_bottom)
        rc = page.insert_textbox(
            rect,
            text,
            fontsize=fontsize,
            fontname=fontname,
            color=(0, 0, 0),
        )
        if rc < 0:
            # Text overflowed; try smaller font
            rc = page.insert_textbox(
                rect, text,
                fontsize=fontsize * 0.8,
                fontname=fontname,
                color=(0, 0, 0),
            )

        # Estimate used height and advance y_pos
        lines_used = max(1, len(text) // 60 + text.count('\n') + 1)
        y_pos += lines_used * fontsize * 1.4 + 6

    output = io.BytesIO()
    doc.save(output, garbage=4, deflate=True)
    doc.close()
    return output.getvalue()
