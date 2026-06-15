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


def _extract(kind, file_content):
    if kind == "pdf":
        return pdf_handler.extract_pdf_as_page_images(file_content)
    if kind == "pptx":
        return pptx_handler.extract_paragraphs_from_pptx(file_content)
    if kind == "ppt":
        return pptx_handler.extract_paragraphs_from_ppt(file_content)
    if kind == "docx":
        return docx_handler.extract_paragraphs_from_docx(file_content)
    if kind == "doc":
        return docx_handler.extract_paragraphs_from_doc(file_content)
    raise ValueError(f"Unsupported extraction kind: {kind}")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", required=True, choices=["pdf", "pptx", "ppt", "docx", "doc"])
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    try:
        with open(args.input, "rb") as f:
            file_content = f.read()
        payload = {"ok": True, "result": _extract(args.kind, file_content)}
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
