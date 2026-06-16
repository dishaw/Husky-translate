"""Isolated document extraction worker for llm_translate.

This script intentionally avoids importing Odoo. The Odoo model writes the
uploaded file to a temporary path, launches this script as a child process, and
reads the pickled extraction result back from another temporary path.
"""

import argparse
import os
import pickle
import sys
import traceback


THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if THIS_DIR not in sys.path:
    sys.path.insert(0, THIS_DIR)

import docx_handler  # noqa: E402
import pdf_handler  # noqa: E402
import pptx_handler  # noqa: E402


def _apply_memory_limit(memory_limit_mb):
    if not memory_limit_mb or os.name != "posix":
        return
    try:
        import resource

        limit = int(memory_limit_mb) * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        # Best-effort guard: unsupported platforms should still extract normally.
        return


def _extract(
    kind,
    file_content,
    pdf_mode="text",
    pdf_dpi=144,
    pdf_max_pages=200,
    office_image_mode="none",
    office_max_image_bytes=2 * 1024 * 1024,
    office_max_total_image_bytes=16 * 1024 * 1024,
    office_max_images=80,
):
    if kind == "pdf":
        if pdf_mode == "page_images":
            return pdf_handler.extract_pdf_as_page_images(
                file_content,
                dpi=pdf_dpi,
                max_pages=pdf_max_pages,
            )
        return pdf_handler.extract_paragraphs_from_pdf(
            file_content,
            max_pages=pdf_max_pages,
        )
        if pdf_mode == "page_images":
            return pdf_handler.extract_pdf_as_page_images(
                file_content,
                dpi=pdf_dpi,
                max_pages=pdf_max_pages,
            )
        return pdf_handler.extract_paragraphs_from_pdf(
            file_content,
            max_pages=pdf_max_pages,
        )
    if kind == "pptx":
        return pptx_handler.extract_paragraphs_from_pptx(file_content)
    if kind == "ppt":
        return pptx_handler.extract_paragraphs_from_ppt(file_content)
    if kind == "docx":
        return docx_handler.extract_paragraphs_from_docx(
            file_content,
            image_mode=office_image_mode,
            max_image_bytes=office_max_image_bytes,
            max_total_image_bytes=office_max_total_image_bytes,
            max_images=office_max_images,
        )
        return docx_handler.extract_paragraphs_from_docx(
            file_content,
            image_mode=office_image_mode,
            max_image_bytes=office_max_image_bytes,
            max_total_image_bytes=office_max_total_image_bytes,
            max_images=office_max_images,
        )
    if kind == "doc":
        return docx_handler.extract_paragraphs_from_doc(
            file_content,
            image_mode=office_image_mode,
            max_image_bytes=office_max_image_bytes,
            max_total_image_bytes=office_max_total_image_bytes,
            max_images=office_max_images,
        )
        return docx_handler.extract_paragraphs_from_doc(
            file_content,
            image_mode=office_image_mode,
            max_image_bytes=office_max_image_bytes,
            max_total_image_bytes=office_max_total_image_bytes,
            max_images=office_max_images,
        )
    raise ValueError(f"Unsupported extraction kind: {kind}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=["pdf", "pptx", "ppt", "docx", "doc"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--pdf-mode", choices=["text", "page_images"], default="text")
    parser.add_argument("--pdf-dpi", type=int, default=144)
    parser.add_argument("--pdf-max-pages", type=int, default=200)
    parser.add_argument("--office-image-mode", choices=["none", "limited", "full"], default="none")
    parser.add_argument("--office-max-image-bytes", type=int, default=2 * 1024 * 1024)
    parser.add_argument("--office-max-total-image-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--office-max-images", type=int, default=80)
    parser.add_argument("--memory-limit-mb", type=int, default=1024)
    args = parser.parse_args(argv)

    try:
        _apply_memory_limit(args.memory_limit_mb)
        _apply_memory_limit(args.memory_limit_mb)
        with open(args.input, "rb") as f:
            file_content = f.read()
        payload = {"ok": True, "result": _extract(
            args.kind,
            file_content,
            pdf_mode=args.pdf_mode,
            pdf_dpi=args.pdf_dpi,
            pdf_max_pages=args.pdf_max_pages,
            office_image_mode=args.office_image_mode,
            office_max_image_bytes=args.office_max_image_bytes,
            office_max_total_image_bytes=args.office_max_total_image_bytes,
            office_max_images=args.office_max_images,
        )}
        payload = {"ok": True, "result": _extract(
            args.kind,
            file_content,
            pdf_mode=args.pdf_mode,
            pdf_dpi=args.pdf_dpi,
            pdf_max_pages=args.pdf_max_pages,
            office_image_mode=args.office_image_mode,
            office_max_image_bytes=args.office_max_image_bytes,
            office_max_total_image_bytes=args.office_max_total_image_bytes,
            office_max_images=args.office_max_images,
        )}
    except Exception as exc:
        payload = {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }

    with open(args.output, "wb") as f:
        pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)

    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
