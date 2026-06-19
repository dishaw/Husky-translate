import base64
import html
import json
import logging
import os
import pickle
import re
import signal
import subprocess
import sys
import tempfile

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from . import docx_handler
from . import pdf_handler
from . import pptx_handler

_logger = logging.getLogger(__name__)

# Supported MIME types for Word documents
WORD_MIMETYPES = (
    "application/msword",  # .doc
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
)

# Supported MIME types for PowerPoint presentations
PPTX_MIMETYPES = (
    "application/vnd.ms-powerpoint",  # .ppt
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",  # .pptx
)

# Supported MIME types for PDF documents
PDF_MIMETYPES = (
    "application/pdf",
)

# Supported MIME types for image files
IMAGE_MIMETYPES = (
    "image/jpeg",
    "image/png",
    "image/bmp",
    "image/gif",
    "image/webp",
    "image/tiff",
    "image/svg+xml",
)

# All supported MIME types
ALL_SUPPORTED_MIMETYPES = WORD_MIMETYPES + PPTX_MIMETYPES + PDF_MIMETYPES + IMAGE_MIMETYPES

# Supported file extensions (for fallback check when MIME type is wrong)
SUPPORTED_EXTENSIONS = (".doc", ".docx", ".pdf", ".ppt", ".pptx", ".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".svg")

DEFAULT_MAX_SOURCE_FILE_BYTES = 100 * 1024 * 1024
DEFAULT_EXTRACTION_MEMORY_LIMIT_MB = 1024
DEFAULT_PDF_EXTRACTION_MODE = "text"
DEFAULT_PDF_EXTRACTION_DPI = 144
DEFAULT_PDF_MAX_PAGES = 200
DEFAULT_OFFICE_IMAGE_MODE = "none"
DEFAULT_OFFICE_MAX_IMAGE_BYTES = 2 * 1024 * 1024
DEFAULT_OFFICE_MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
DEFAULT_OFFICE_MAX_IMAGES = 80
DEFAULT_EXTRACTION_MEMORY_LIMIT_MB = 1024
DEFAULT_PDF_EXTRACTION_MODE = "text"
DEFAULT_PDF_EXTRACTION_DPI = 144
DEFAULT_PDF_MAX_PAGES = 200
DEFAULT_OFFICE_IMAGE_MODE = "limited"
DEFAULT_OFFICE_MAX_IMAGE_BYTES = 2 * 1024 * 1024
DEFAULT_OFFICE_MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024
DEFAULT_OFFICE_MAX_IMAGES = 80
FRONTEND_LINE_PAYLOAD_BYTES = 1 * 1024 * 1024
EXTRACTION_TIMEOUT_SECONDS = 600
EXTRACTION_PARAGRAPH_BATCH_SIZE = 50
EXTRACTION_LINE_CREATE_BATCH_SIZE = 100

# Language choices
LANGUAGE_SELECTION = [
    ("zh", "Simplified Chinese"),
    ("zh_tw", "Traditional Chinese"),
    ("en", "English"),
    ("ja", "日本語 (Japanese)"),
    ("ko", "한국어 (Korean)"),
    ("fr", "Français (French)"),
    ("de", "Deutsch (German)"),
    ("es", "Español (Spanish)"),
    ("pt", "Português (Portuguese)"),
    ("ru", "Русский (Russian)"),
    ("ar", "العربية (Arabic)"),
    ("it", "Italiano (Italian)"),
    ("nl", "Nederlands (Dutch)"),
    ("th", "ไทย (Thai)"),
    ("vi", "Tiếng Việt (Vietnamese)"),
    ("other", "Other"),
]

# Map language code to display name for prompts
LANGUAGE_NAMES = {code: name for code, name in LANGUAGE_SELECTION}
LANGUAGE_PROMPT_NAMES = {
    "zh": "Simplified Chinese",
    "zh_tw": "Traditional Chinese",
}


class LLMTranslation(models.Model):
    _name = "llm.translation"
    _description = "LLM Document Translation"
    _inherit = ["mail.thread"]
    _order = "create_date DESC"

    name = fields.Char(
        string="Name",
        required=True,
        default="New Translation",
        tracking=True,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("extracting", "Extracting Text"),
            ("translating", "Translating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Status",
        default="draft",
        required=True,
        tracking=True,
    )

    # LLM Configuration
    provider_id = fields.Many2one(
        "llm.provider",
        string="Provider",
        required=True,
        ondelete="restrict",
        tracking=True,
    )
    model_id = fields.Many2one(
        "llm.model",
        string="Model",
        required=True,
        domain="[('provider_id', '=', provider_id), ('model_use', 'in', ['chat', 'multimodal'])]",
        ondelete="restrict",
        tracking=True,
    )

    # Language settings
    source_lang = fields.Selection(
        LANGUAGE_SELECTION,
        string="Source Language",
        required=True,
        default="en",
        tracking=True,
    )
    source_lang_custom = fields.Char(
        string="Custom Source Language",
        help="Specify source language if 'Other' is selected",
    )
    target_lang = fields.Selection(
        LANGUAGE_SELECTION,
        string="Target Language",
        required=True,
        default="zh",
        tracking=True,
    )
    target_lang_custom = fields.Char(
        string="Custom Target Language",
        help="Specify target language if 'Other' is selected",
    )

    # Knowledge base for glossary
    knowledge_collection_id = fields.Many2one(
        "llm.knowledge.collection",
        string="Glossary Knowledge Base",
        ondelete="set null",
        tracking=True,
        help="Knowledge collection containing terminology translations. "
             "Will be searched before translating each paragraph to ensure "
             "consistent terminology usage.",
    )
    custom_system_prompt = fields.Text(
        string="Custom Translation Prompt",
        help="Optional task-specific system prompt. Supports {source_lang} and "
             "{target_lang} placeholders. Dynamic glossary and batch rules are "
             "appended automatically during translation.",
    )

    # Project for saving files
    project_id = fields.Many2one(
        "project.project",
        string="Project",
        required=True,
        ondelete="restrict",
        tracking=True,
        help="Project to save source and translated documents to.",
    )

    # Source file
    source_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Source Document",
        ondelete="set null",
        help="Original document (.doc/.docx/.ppt/.pptx/.pdf) to translate.",
    )
    source_filename = fields.Char(
        string="Source Filename",
        related="source_attachment_id.name",
        readonly=True,
    )
    source_file_size_display = fields.Char(
        string="Size",
        compute="_compute_source_file_size_display",
        readonly=True,
    )

    # Result file
    result_attachment_id = fields.Many2one(
        "ir.attachment",
        string="Translated Document",
        ondelete="set null",
        readonly=True,
        help="Generated translated Word document.",
    )
    result_filename = fields.Char(
        string="Result Filename",
        related="result_attachment_id.name",
        readonly=True,
    )

    @api.depends("source_attachment_id", "source_attachment_id.file_size")
    def _compute_source_file_size_display(self):
        for rec in self:
            size = rec.source_attachment_id.file_size if rec.source_attachment_id else 0
            rec.source_file_size_display = self._format_file_size(size)

    @staticmethod
    def _format_file_size(size):
        try:
            size = int(size or 0)
        except (TypeError, ValueError):
            size = 0
        units = ["B", "KB", "MB", "GB"]
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                if unit == "B":
                    return f"{int(value)} {unit}"
                return f"{value:.1f} {unit}"
            value /= 1024

    @staticmethod
    def _estimate_base64_size(file_data):
        if not file_data:
            return 0
        if isinstance(file_data, bytes):
            file_data = file_data.decode("ascii", errors="ignore")
        payload = file_data.split(",", 1)[-1] if "," in file_data else file_data
        payload = "".join(str(payload).split())
        padding = payload.count("=")
        return max(0, (len(payload) * 3 // 4) - padding)

    def _get_max_source_file_bytes(self):
        """Read source document guardrail from system parameters."""
        try:
            value = self.env["ir.config_parameter"].sudo().get_param(
                "llm_translate.max_source_file_bytes",
                str(DEFAULT_MAX_SOURCE_FILE_BYTES),
            )
            max_bytes = int(value or DEFAULT_MAX_SOURCE_FILE_BYTES)
        except (TypeError, ValueError):
            max_bytes = DEFAULT_MAX_SOURCE_FILE_BYTES
        return max(1 * 1024 * 1024, min(max_bytes, 512 * 1024 * 1024))

    # Translation lines
    line_ids = fields.One2many(
        "llm.translation.line",
        "translation_id",
        string="Translation Lines",
    )

    # Progress tracking
    total_lines = fields.Integer(
        string="Total Paragraphs",
        compute="_compute_progress",
        store=True,
    )
    translated_lines = fields.Integer(
        string="Translated Paragraphs",
        compute="_compute_progress",
        store=True,
    )
    progress = fields.Float(
        string="Progress (%)",
        compute="_compute_progress",
        store=True,
    )
    error_message = fields.Text(
        string="Error Message",
        readonly=True,
    )
    header_text = fields.Text(
        string="Document Header",
        help="Header text extracted from the Word document.",
    )
    footer_text = fields.Text(
        string="Document Footer",
        help="Footer text extracted from the Word document.",
    )

    # User
    user_id = fields.Many2one(
        "res.users",
        string="Created By",
        default=lambda self: self.env.user,
        required=True,
        ondelete="restrict",
    )

    @api.depends("line_ids", "line_ids.state")
    def _compute_progress(self):
        for rec in self:
            lines = rec.line_ids.filtered(lambda l: not l.is_empty)
            rec.total_lines = len(lines)
            rec.translated_lines = len(lines.filtered(lambda l: l.state == "done"))
            rec.progress = (
                (rec.translated_lines / rec.total_lines * 100)
                if rec.total_lines
                else 0.0
            )

    @api.onchange("provider_id")
    def _onchange_provider_id(self):
        """Clear model when provider changes."""
        if self.provider_id and self.model_id and self.model_id.provider_id != self.provider_id:
            self.model_id = False

    def unlink(self):
        """Override unlink to clean up all related attachments.

        Collects attachments from three sources to ensure nothing is orphaned:
        1. Attachments whose res_model points to llm.translation (OCR images)
        2. source_attachment_id (may have res_model = project.project after translation)
        3. result_attachment_id (always res_model = project.project)
        """
        all_attachments = self.env["ir.attachment"].sudo()
        for rec in self:
            # 1. OCR images and any other attachments linked via res_model/res_id
            all_attachments |= self.env["ir.attachment"].sudo().search([
                ("res_model", "=", self._name),
                ("res_id", "=", rec.id),
            ])
            # 2-3. Explicitly linked source/result (may have different res_model)
            if rec.source_attachment_id:
                all_attachments |= rec.source_attachment_id
            if rec.result_attachment_id:
                all_attachments |= rec.result_attachment_id
        # Detach first to avoid recursive ORM cleanup issues
        self.write({
            "source_attachment_id": False,
            "result_attachment_id": False,
        })
        if all_attachments:
            all_attachments.exists().sudo().unlink()
        return super().unlink()

    def _get_source_lang_name(self):
        """Get the display name of the source language."""
        self.ensure_one()
        if self.source_lang == "other":
            return self.source_lang_custom or "Unknown"
        return LANGUAGE_PROMPT_NAMES.get(
            self.source_lang,
            LANGUAGE_NAMES.get(self.source_lang, self.source_lang),
        )

    def _get_target_lang_name(self):
        """Get the display name of the target language."""
        self.ensure_one()
        if self.target_lang == "other":
            return self.target_lang_custom or "Unknown"
        return LANGUAGE_PROMPT_NAMES.get(
            self.target_lang,
            LANGUAGE_NAMES.get(self.target_lang, self.target_lang),
        )

    # =========================================================================
    # FILE UPLOAD & EXTRACTION
    # =========================================================================

    def action_upload_file(self, attachment_id):
        """Process an uploaded file: validate and extract paragraphs.

        Args:
            attachment_id: ID of the ir.attachment record.
        """
        self.ensure_one()
        attachment = self.env["ir.attachment"].browse(attachment_id)
        if not attachment.exists():
            raise UserError(_("Attachment not found."))

        mimetype = attachment.mimetype or ""
        filename = (attachment.name or "").lower()
        ext = os.path.splitext(filename)[1]

        if mimetype not in ALL_SUPPORTED_MIMETYPES and ext not in SUPPORTED_EXTENSIONS:
            raise UserError(
                _("Unsupported file format. Supported: %s") % ", ".join(SUPPORTED_EXTENSIONS)
            )

        file_size = attachment.file_size or self._estimate_base64_size(attachment.datas)
        max_source_file_bytes = self._get_max_source_file_bytes()
        if file_size > max_source_file_bytes:
            raise UserError(_(
                "文件过大：%(filename)s 为 %(size)s。当前单文件上传/抽取上限是 %(limit)s。"
                "请先拆分/压缩文件，或分批上传后再翻译。"
            ) % {
                "filename": attachment.name or "uploaded file",
                "size": self._format_file_size(file_size),
                "limit": self._format_file_size(max_source_file_bytes),
            })

        # Clean up old source attachment to prevent orphans
        old_source = self.source_attachment_id
        if old_source and old_source.id != attachment.id:
            self.write({"source_attachment_id": False})
            old_source.sudo().unlink()

        self.write({
            "source_attachment_id": attachment.id,
            "name": f"Translation - {attachment.name}",
            "state": "extracting",
            "error_message": False,
        })

        try:
            self._extract_paragraphs()
            self.write({"state": "draft"})
        except Exception as e:
            _logger.exception("Failed to extract paragraphs from %s", attachment.name)
            error_msg = str(e)
            # Provide user-friendly hints for common issues
            if "python-docx" in error_msg or "No module named 'docx'" in error_msg:
                error_msg = _(
                    "python-docx library is not installed. "
                    "Please install it with: pip install python-docx"
                )
            elif "python-pptx" in error_msg or "No module named 'pptx'" in error_msg:
                error_msg = _(
                    "python-pptx library is not installed. "
                    "Please install it with: pip install python-pptx"
                )
            self.write({
                "state": "error",
                "error_message": error_msg,
            })

    def _create_image_attachment(self, data_uri, para_index, img_index):
        """Store a data-URI image as an ir.attachment.

        Args:
            data_uri: 'data:<mime>;base64,<b64data>' string.
            para_index: Paragraph index (for naming).
            img_index: Image index within the paragraph.

        Returns:
            ir.attachment record.
        """
        # Parse data URI
        import re as _re
        m = _re.match(r'data:([^;]+);base64,(.*)', data_uri, _re.DOTALL)
        if not m:
            raise ValueError("Invalid data URI for image")
        mimetype = m.group(1)
        b64_data = m.group(2)
        ext = mimetype.split("/")[-1].replace("jpeg", "jpg")
        name = f"ocr_img_p{para_index}_i{img_index}.{ext}"

        return self.env["ir.attachment"].create({
            "name": name,
            "datas": b64_data,
            "mimetype": mimetype,
            "res_model": self._name,
            "res_id": self.id,
        })

    def _extract_image_for_translation(self, file_content, filename, mimetype):
        """Extract an uploaded image file for OCR-based translation.

        Treats the image as a single "page" with one image paragraph.
        The image is stored as an attachment, and an image_ocr line is created
        for the LLM to perform OCR + translation on.

        Args:
            file_content: Raw bytes of the image file.
            filename: Original filename.
            mimetype: MIME type of the image.

        Returns:
            dict: Result structure compatible with _extract_paragraphs processing.
        """
        # Encode image as data URI for display
        b64_data = base64.b64encode(file_content).decode("ascii")
        data_uri = f"data:{mimetype};base64,{b64_data}"

        # Store the image as an attachment
        img_attachment = self.env["ir.attachment"].create({
            "name": filename,
            "datas": b64_data,
            "mimetype": mimetype,
            "res_model": self._name,
            "res_id": self.id,
        })

        # Create a single paragraph with the image
        return {
            "paragraphs": [{
                "text": "",
                "is_empty": False,
                "para_index": 0,
                "images": [{
                    "data_uri": data_uri,
                    "width": None,
                    "height": None,
                    "placement": "inline",
                    "offset_h": None,
                    "offset_v": None,
                    "attachment_id": img_attachment.id,
                    "_skip_ocr_attach": True,  # Already stored above, don't re-store
                }],
                "textboxes": [],
            }],
            "header_text": "",
            "footer_text": "",
            "header_images": [],
            "footer_images": [],
        }

    def _get_extraction_kind(self, filename, mimetype):
        """Return a normalized extraction kind for a source document."""
        filename = (filename or "").lower()
        mimetype = mimetype or ""

        if (
            mimetype in IMAGE_MIMETYPES
            or any(filename.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".svg"))
        ):
            return "image"
        if mimetype in PDF_MIMETYPES or filename.endswith(".pdf"):
            return "pdf"
        if (
            mimetype == "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            or filename.endswith(".pptx")
        ):
            return "pptx"
        if mimetype == "application/vnd.ms-powerpoint" or filename.endswith(".ppt"):
            return "ppt"
        if (
            mimetype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            or filename.endswith(".docx")
        ):
            return "docx"
        return "doc"

    def _get_extraction_timeout_seconds(self):
        """Read extraction timeout from system parameters with a safe default."""
        try:
            value = self.env["ir.config_parameter"].sudo().get_param(
                "llm_translate.extraction_timeout_seconds",
                str(EXTRACTION_TIMEOUT_SECONDS),
            )
            timeout = int(value or EXTRACTION_TIMEOUT_SECONDS)
        except (TypeError, ValueError):
            timeout = EXTRACTION_TIMEOUT_SECONDS
        return max(30, min(timeout, 1800))

    def _get_extraction_memory_limit_mb(self):
        """Return per-worker memory guardrail in MB."""
        try:
            value = self.env["ir.config_parameter"].sudo().get_param(
                "llm_translate.extraction_memory_limit_mb",
                str(DEFAULT_EXTRACTION_MEMORY_LIMIT_MB),
            )
            limit_mb = int(value or DEFAULT_EXTRACTION_MEMORY_LIMIT_MB)
        except (TypeError, ValueError):
            limit_mb = DEFAULT_EXTRACTION_MEMORY_LIMIT_MB
        return max(128, min(limit_mb, 4096))

    def _get_pdf_extraction_options(self):
        """Return PDF extraction guardrails for the isolated worker."""
        params = self.env["ir.config_parameter"].sudo()
        mode = params.get_param(
            "llm_translate.pdf_extraction_mode",
            DEFAULT_PDF_EXTRACTION_MODE,
        )
        if mode not in ("text", "page_images"):
            mode = DEFAULT_PDF_EXTRACTION_MODE
        try:
            dpi = int(params.get_param(
                "llm_translate.pdf_extraction_dpi",
                str(DEFAULT_PDF_EXTRACTION_DPI),
            ) or DEFAULT_PDF_EXTRACTION_DPI)
        except (TypeError, ValueError):
            dpi = DEFAULT_PDF_EXTRACTION_DPI
        try:
            max_pages = int(params.get_param(
                "llm_translate.pdf_max_pages",
                str(DEFAULT_PDF_MAX_PAGES),
            ) or DEFAULT_PDF_MAX_PAGES)
        except (TypeError, ValueError):
            max_pages = DEFAULT_PDF_MAX_PAGES
        return {
            "mode": mode,
            "dpi": max(72, min(dpi, 200)),
            "max_pages": max(1, min(max_pages, 1000)),
        }

    def _get_office_extraction_options(self):
        """Return Office image extraction guardrails for the isolated worker."""
        params = self.env["ir.config_parameter"].sudo()
        image_mode = params.get_param(
            "llm_translate.office_image_mode",
            DEFAULT_OFFICE_IMAGE_MODE,
        )
        if image_mode not in ("none", "limited", "full"):
            image_mode = DEFAULT_OFFICE_IMAGE_MODE

        def _get_int_param(name, default, minimum, maximum):
            try:
                value = int(params.get_param(name, str(default)) or default)
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        return {
            "image_mode": image_mode,
            "max_image_bytes": _get_int_param(
                "llm_translate.office_max_image_bytes",
                DEFAULT_OFFICE_MAX_IMAGE_BYTES,
                0,
                50 * 1024 * 1024,
            ),
            "max_total_image_bytes": _get_int_param(
                "llm_translate.office_max_total_image_bytes",
                DEFAULT_OFFICE_MAX_TOTAL_IMAGE_BYTES,
                0,
                200 * 1024 * 1024,
            ),
            "max_images": _get_int_param(
                "llm_translate.office_max_images",
                DEFAULT_OFFICE_MAX_IMAGES,
                0,
                1000,
            ),
        }

    def _get_extraction_memory_limit_mb(self):
        """Return per-worker memory guardrail in MB."""
        try:
            value = self.env["ir.config_parameter"].sudo().get_param(
                "llm_translate.extraction_memory_limit_mb",
                str(DEFAULT_EXTRACTION_MEMORY_LIMIT_MB),
            )
            limit_mb = int(value or DEFAULT_EXTRACTION_MEMORY_LIMIT_MB)
        except (TypeError, ValueError):
            limit_mb = DEFAULT_EXTRACTION_MEMORY_LIMIT_MB
        return max(128, min(limit_mb, 4096))

    def _get_pdf_extraction_options(self):
        """Return PDF extraction guardrails for the isolated worker."""
        params = self.env["ir.config_parameter"].sudo()
        mode = params.get_param(
            "llm_translate.pdf_extraction_mode",
            DEFAULT_PDF_EXTRACTION_MODE,
        )
        if mode not in ("text", "page_images"):
            mode = DEFAULT_PDF_EXTRACTION_MODE
        try:
            dpi = int(params.get_param(
                "llm_translate.pdf_extraction_dpi",
                str(DEFAULT_PDF_EXTRACTION_DPI),
            ) or DEFAULT_PDF_EXTRACTION_DPI)
        except (TypeError, ValueError):
            dpi = DEFAULT_PDF_EXTRACTION_DPI
        try:
            max_pages = int(params.get_param(
                "llm_translate.pdf_max_pages",
                str(DEFAULT_PDF_MAX_PAGES),
            ) or DEFAULT_PDF_MAX_PAGES)
        except (TypeError, ValueError):
            max_pages = DEFAULT_PDF_MAX_PAGES
        return {
            "mode": mode,
            "dpi": max(72, min(dpi, 200)),
            "max_pages": max(1, min(max_pages, 1000)),
        }

    def _get_office_extraction_options(self):
        """Return Office image extraction guardrails for the isolated worker."""
        params = self.env["ir.config_parameter"].sudo()
        image_mode = params.get_param(
            "llm_translate.office_image_mode",
            DEFAULT_OFFICE_IMAGE_MODE,
        )
        if image_mode not in ("none", "limited", "full"):
            image_mode = DEFAULT_OFFICE_IMAGE_MODE

        def _get_int_param(name, default, minimum, maximum):
            try:
                value = int(params.get_param(name, str(default)) or default)
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        return {
            "image_mode": image_mode,
            "max_image_bytes": _get_int_param(
                "llm_translate.office_max_image_bytes",
                DEFAULT_OFFICE_MAX_IMAGE_BYTES,
                0,
                50 * 1024 * 1024,
            ),
            "max_total_image_bytes": _get_int_param(
                "llm_translate.office_max_total_image_bytes",
                DEFAULT_OFFICE_MAX_TOTAL_IMAGE_BYTES,
                0,
                200 * 1024 * 1024,
            ),
            "max_images": _get_int_param(
                "llm_translate.office_max_images",
                DEFAULT_OFFICE_MAX_IMAGES,
                0,
                1000,
            ),
        }

    @staticmethod
    def _kill_extraction_process(proc):
        """Terminate a stuck extraction worker and its child process group."""
        if proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass

    def _run_extraction_worker(self, kind, file_content, display_name):
        """Run non-image document extraction in an isolated Python process."""
        timeout = self._get_extraction_timeout_seconds()
        memory_limit_mb = self._get_extraction_memory_limit_mb()
        pdf_options = self._get_pdf_extraction_options()
        office_options = self._get_office_extraction_options()
        worker_path = os.path.join(os.path.dirname(__file__), "extraction_worker.py")
        if not os.path.exists(worker_path):
            raise UserError(_("Document extraction worker is missing."))

        with tempfile.TemporaryDirectory(prefix="llm_extract_") as tmpdir:
            input_path = os.path.join(tmpdir, "source.bin")
            output_path = os.path.join(tmpdir, "result.pkl")
            with open(input_path, "wb") as f:
                f.write(file_content)

            cmd = [
                sys.executable or "python3",
                worker_path,
                "--kind",
                kind,
                "--input",
                input_path,
                "--output",
                output_path,
                "--memory-limit-mb",
                str(memory_limit_mb),
            ]
            if kind == "pdf":
                cmd.extend([
                    "--pdf-mode",
                    pdf_options["mode"],
                    "--pdf-dpi",
                    str(pdf_options["dpi"]),
                    "--pdf-max-pages",
                    str(pdf_options["max_pages"]),
                ])
            elif kind in ("doc", "docx"):
                cmd.extend([
                    "--office-image-mode",
                    office_options["image_mode"],
                    "--office-max-image-bytes",
                    str(office_options["max_image_bytes"]),
                    "--office-max-total-image-bytes",
                    str(office_options["max_total_image_bytes"]),
                    "--office-max-images",
                    str(office_options["max_images"]),
                ])
            popen_kwargs = {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif os.name == "nt" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            _logger.info(
                "Starting isolated %s extraction for %s with timeout=%ss",
                kind,
                display_name,
                timeout,
            )
            proc = subprocess.Popen(cmd, **popen_kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._kill_extraction_process(proc)
                _logger.warning(
                    "Extraction timed out for %s after %s seconds",
                    display_name,
                    timeout,
                )
                raise UserError(_(
                    "Document extraction timed out after %(seconds)s seconds. "
                    "Please split or compress the file, then try again."
                ) % {"seconds": timeout})

            if not os.path.exists(output_path):
                detail = (stderr or stdout or "").strip()
                if not detail:
                    detail = f"worker exited with status {proc.returncode}"
                raise UserError(_("Document extraction failed: %s") % detail)

            with open(output_path, "rb") as f:
                payload = pickle.load(f)

        if not payload.get("ok"):
            if payload.get("traceback"):
                _logger.warning("Extraction worker traceback:\n%s", payload["traceback"])
            raise UserError(_("Document extraction failed: %s") % (payload.get("error") or "unknown error"))

        if proc.returncode not in (0, None):
            _logger.warning(
                "Extraction worker returned %s despite payload for %s: stdout=%s stderr=%s",
                proc.returncode,
                display_name,
                stdout,
                stderr,
            )

        return payload.get("result")

    def _extract_source_document(self, file_content, filename, mimetype):
        """Extract a source document, isolating heavy parsers from Odoo."""
        kind = self._get_extraction_kind(filename, mimetype)
        if kind == "image":
            _logger.info("Processing image file in Odoo process: %s", filename)
            return self._extract_image_for_translation(file_content, filename, mimetype)

        return self._run_extraction_worker(kind, file_content, filename)

    def _extract_paragraphs(self):
        """Extract paragraphs from the source document and create line records."""
        self.ensure_one()
        if not self.source_attachment_id:
            raise UserError(_("No source document attached."))

        # Delete existing lines
        self.line_ids.unlink()

        # Delete old OCR image attachments linked to this translation
        old_ocr_attachments = self.env["ir.attachment"].search([
            ("res_model", "=", self._name),
            ("res_id", "=", self.id),
            ("name", "like", "ocr_img_"),
        ])
        if old_ocr_attachments:
            old_ocr_attachments.unlink()

        attachment = self.source_attachment_id
        file_content = base64.b64decode(attachment.datas)
        filename = (attachment.name or "").lower()
        mimetype = attachment.mimetype or ""
        result = self._extract_source_document(file_content, filename, mimetype)

        # Handle both old list format and new dict format
        if isinstance(result, dict):
            raw_paragraphs = result.get("paragraphs", [])
            header_text = result.get("header_text", "")
            footer_text = result.get("footer_text", "")
            header_images = result.get("header_images", [])
            footer_images = result.get("footer_images", [])
            # Store for backward compat
            self.header_text = header_text
            self.footer_text = footer_text
        else:
            raw_paragraphs = result
            header_text = ""
            footer_text = ""
            header_images = []
            footer_images = []
        raw_paragraphs = raw_paragraphs or []

        # Create translation lines
        sequence = 0
        created_line_count = 0
        line_vals_batch = []

        def flush_lines():
            nonlocal created_line_count
            if not line_vals_batch:
                return
            self.env["llm.translation.line"].create(line_vals_batch)
            created_line_count += len(line_vals_batch)
            line_vals_batch.clear()

        def add_line(vals):
            line_vals_batch.append(vals)
            if len(line_vals_batch) >= EXTRACTION_LINE_CREATE_BATCH_SIZE:
                flush_lines()

        # Header line (translatable, appears on every page; may contain images)
        if (header_text and header_text.strip()) or header_images:
            sequence += 1
            has_text = bool(header_text and header_text.strip())
            add_line({
                "translation_id": self.id,
                "sequence": sequence,
                "source_text": header_text.strip() if has_text else "",
                "line_type": "header",
                "is_empty": False,
                "style_metadata": json.dumps({
                    "style": "Header",
                    "alignment": "CENTER",
                    "font_size": 9,
                    "bold": False,
                    "runs": [],
                    "images": header_images,
                    "is_empty": False,
                }),
                "estimated_tokens": docx_handler.estimate_tokens(header_text) if has_text else 0,
                "state": "pending" if has_text else "done",
            })

        # Body paragraphs and table rows (splitting long ones)
        for para_index, para in enumerate(raw_paragraphs):
            text = para.get("text", "")
            is_empty = para.get("is_empty", False)
            has_images = bool(para.get("images", []))
            has_textboxes = bool(para.get("textboxes", []))
            is_table_row = para.get("is_table_row", False)
            style_metadata = {
                k: v for k, v in para.items() if k != "text"
            }

            # Determine line_type
            para_line_type = "table_cell" if is_table_row else "body"

            # Combine paragraph text with textbox text for translation
            # Textbox text is appended with separator for the LLM
            textboxes = para.get("textboxes", [])
            combined_text = text
            if textboxes:
                tb_texts = [tb.get("full_text", "") for tb in textboxes if tb.get("full_text", "").strip()]
                if tb_texts:
                    # Always use [TEXTBOX] separator after paragraph text
                    # Even if paragraph text is empty, keep it as first part
                    combined_text = combined_text + "\n[TEXTBOX]\n" + "\n[TEXTBOX]\n".join(tb_texts)

            if is_empty or (not combined_text.strip() and not has_images):
                sequence += 10
                add_line({
                    "translation_id": self.id,
                    "sequence": sequence,
                    "source_text": "",
                    "line_type": para_line_type,
                    "is_empty": True,
                    "style_metadata": json.dumps(style_metadata),
                    "state": "done",  # Nothing to translate
                })
                if (para_index + 1) % EXTRACTION_PARAGRAPH_BATCH_SIZE == 0:
                    flush_lines()
                continue

            # Image-only paragraphs: mark structure line as done, create OCR lines
            if not combined_text.strip() and has_images:
                sequence += 10
                add_line({
                    "translation_id": self.id,
                    "sequence": sequence,
                    "source_text": "",
                    "line_type": para_line_type,
                    "is_empty": False,  # Not empty - has images
                    "style_metadata": json.dumps(style_metadata),
                    "state": "done",  # Nothing to translate
                })
                # Create image_ocr lines for each image with potential text
                for img_idx, img_data in enumerate(para.get("images", [])):
                    if not img_data.get("data_uri"):
                        continue
                    # Store image as ir.attachment to avoid huge JSON fields
                    # (skip if already stored by _extract_image_for_translation)
                    if img_data.get("_skip_ocr_attach") and img_data.get("attachment_id"):
                        img_att_id = img_data["attachment_id"]
                    else:
                        img_att = self._create_image_attachment(
                            img_data["data_uri"], para.get("para_index", 0), img_idx
                        )
                        img_att_id = img_att.id
                    sequence += 1
                    img_meta = {
                        "para_index": para.get("para_index"),
                        "image_index": img_idx,
                        "image_attachment_id": img_att_id,
                        "image_width_px": img_data.get("width"),
                        "image_height_px": img_data.get("height"),
                        "image_placement": img_data.get("placement", "inline"),
                        "image_offset_h_px": img_data.get("offset_h"),
                        "image_offset_v_px": img_data.get("offset_v"),
                    }
                    add_line({
                        "translation_id": self.id,
                        "sequence": sequence,
                        "source_text": f"[Image OCR] Para {para.get('para_index', '?')}, Image {img_idx + 1}",
                        "line_type": "image_ocr",
                        "is_empty": False,
                        "style_metadata": json.dumps(img_meta),
                        "estimated_tokens": 0,
                        "state": "pending",
                    })
                if (para_index + 1) % EXTRACTION_PARAGRAPH_BATCH_SIZE == 0:
                    flush_lines()
                continue

            chunks = docx_handler.split_long_paragraph(combined_text, max_tokens=2000)
            for i, chunk in enumerate(chunks):
                sequence += 10
                chunk_meta = dict(style_metadata)
                if len(chunks) > 1:
                    chunk_meta["is_split"] = True
                    chunk_meta["split_index"] = i
                    chunk_meta["split_total"] = len(chunks)

                add_line({
                    "translation_id": self.id,
                    "sequence": sequence,
                    "source_text": chunk,
                    "line_type": para_line_type,
                    "is_empty": False,
                    "style_metadata": json.dumps(chunk_meta),
                    "estimated_tokens": docx_handler.estimate_tokens(chunk),
                    "state": "pending",
                })

            # Create image_ocr lines for images in this paragraph
            if has_images:
                for img_idx, img_data in enumerate(para.get("images", [])):
                    if not img_data.get("data_uri"):
                        continue
                    # Store image as ir.attachment to avoid huge JSON fields
                    # (skip if already stored by _extract_image_for_translation)
                    if img_data.get("_skip_ocr_attach") and img_data.get("attachment_id"):
                        img_att_id = img_data["attachment_id"]
                    else:
                        img_att = self._create_image_attachment(
                            img_data["data_uri"], para.get("para_index", 0), img_idx
                        )
                        img_att_id = img_att.id
                    sequence += 1
                    img_meta = {
                        "para_index": para.get("para_index"),
                        "image_index": img_idx,
                        "image_attachment_id": img_att_id,
                        "image_width_px": img_data.get("width"),
                        "image_height_px": img_data.get("height"),
                        "image_placement": img_data.get("placement", "inline"),
                        "image_offset_h_px": img_data.get("offset_h"),
                        "image_offset_v_px": img_data.get("offset_v"),
                    }
                    add_line({
                        "translation_id": self.id,
                        "sequence": sequence,
                        "source_text": f"[Image OCR] Para {para.get('para_index', '?')}, Image {img_idx + 1}",
                        "line_type": "image_ocr",
                        "is_empty": False,
                        "style_metadata": json.dumps(img_meta),
                        "estimated_tokens": 0,
                        "state": "pending",
                    })

            if (para_index + 1) % EXTRACTION_PARAGRAPH_BATCH_SIZE == 0:
                flush_lines()

        # Footer line (translatable, appears on every page; may contain images)
        if (footer_text and footer_text.strip()) or footer_images:
            sequence += 10
            has_text = bool(footer_text and footer_text.strip())
            add_line({
                "translation_id": self.id,
                "sequence": sequence,
                "source_text": footer_text.strip() if has_text else "",
                "line_type": "footer",
                "is_empty": False,
                "style_metadata": json.dumps({
                    "style": "Footer",
                    "alignment": "CENTER",
                    "font_size": 9,
                    "bold": False,
                    "runs": [],
                    "images": footer_images,
                    "is_empty": False,
                }),
                "estimated_tokens": docx_handler.estimate_tokens(footer_text) if has_text else 0,
                "state": "pending" if has_text else "done",
            })

        flush_lines()

        _logger.info(
            "Extracted %d lines from %s (including splits)",
            created_line_count,
            attachment.name,
        )

    # =========================================================================
    # KNOWLEDGE BASE GLOSSARY
    # =========================================================================

    def _prepare_glossary(self, text):
        """Search the knowledge base for relevant terminology.

        Queries the knowledge collection using semantic search to find
        relevant glossary terms for the given text segment.

        Args:
            text: Source text to find glossary terms for.

        Returns:
            str: Glossary instructions string for the LLM prompt, or empty string.
        """
        self.ensure_one()
        if not self.knowledge_collection_id:
            return ""

        try:
            collection = self.knowledge_collection_id
            chunk_model = self.env["llm.knowledge.chunk"]

            # Semantic search in the knowledge base
            chunks = chunk_model.search(
                args=[("embedding", "=", text)],
                limit=10,
                collection_id=collection.id,
                query_min_similarity=0.3,
            )

            if not chunks:
                return ""

            glossary_entries = []
            for chunk in chunks:
                content = chunk.content or ""
                if content.strip():
                    glossary_entries.append(content.strip())

            if not glossary_entries:
                return ""

            glossary_text = "\n".join(f"- {entry}" for entry in glossary_entries[:8])

            return (
                f"\n\n[GLOSSARY/TERMINOLOGY REFERENCE]\n"
                f"The following terminology entries from the knowledge base may be "
                f"relevant to this paragraph. Use them to ensure consistent and accurate "
                f"translation of names, technical terms, and domain-specific vocabulary:\n"
                f"{glossary_text}\n"
                f"[END GLOSSARY]\n"
            )

        except Exception as e:
            _logger.warning("Glossary lookup failed: %s", e)
            return ""

    def _prepare_translation_memory(self, text):
        """Look up user-corrected translation memory entries for the source text.

        Queries the llm.translation.glossary model for entries whose source_text
        appears in the given text. Returns a formatted prompt section.

        Args:
            text: Source text to find matching glossary entries for.

        Returns:
            str: Translation memory instructions string, or empty string.
        """
        self.ensure_one()
        if not text or not text.strip():
            return ""

        try:
            GlossaryModel = self.env["llm.translation.glossary"]
            source_lang = self.source_lang or "en"
            target_lang = self.target_lang or "zh"
            # Check if current user is guest/temp user
            is_guest = bool(getattr(self.env.user, "is_temp_user", False))

            matches = GlossaryModel.find_matches(
                text, source_lang=source_lang, target_lang=target_lang, is_guest=is_guest
            )
            if not matches:
                return ""

            return GlossaryModel.format_for_prompt(matches)

        except Exception as e:
            _logger.warning("Translation memory lookup failed: %s", e)
            return ""

    def _parse_terminology_json(self, text):
        """Parse a terminology JSON response from the LLM."""
        if not text:
            return []

        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        block = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if block:
            text = block.group(1).strip()
        else:
            obj = re.search(r"\{.*\}", text, re.DOTALL)
            arr = re.search(r"\[.*\]", text, re.DOTALL)
            if obj:
                text = obj.group(0)
            elif arr:
                text = arr.group(0)

        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError) as e:
            _logger.warning("Failed to parse terminology JSON: %s — raw: %s", e, text[:300])
            return []

        if isinstance(data, dict):
            items = data.get("terms") or data.get("candidates") or data.get("terminology") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []

        return items if isinstance(items, list) else []

    def _collect_terminology_source_sample(self, max_chars=12000):
        """Collect representative source text for terminology extraction."""
        self.ensure_one()
        parts = []
        total = 0
        for line in self.line_ids.sorted("sequence"):
            if line.is_empty or line.line_type == "image_ocr":
                continue
            text = (line.source_text or "").strip()
            if not text:
                continue
            text = re.sub(r"\s*\[CELL\]\s*", " | ", text)
            if total + len(text) > max_chars:
                remaining = max_chars - total
                if remaining > 80:
                    parts.append(text[:remaining])
                break
            parts.append(text)
            total += len(text) + 1
        return "\n".join(parts)

    def extract_terminology_candidates(self, limit=40):
        """Extract document-level terminology candidates without saving them."""
        self.ensure_one()
        limit = max(1, min(int(limit or 40), 80))
        source_text = self._collect_terminology_source_sample()
        if not source_text:
            return {"candidates": [], "error": _("No source text available.")}
        if not self.provider_id or not self.model_id:
            return {"candidates": [], "error": _("Please select a provider and model first.")}

        source_lang = self._get_source_lang_name()
        target_lang = self._get_target_lang_name()
        existing_entries = self.env["llm.translation.glossary"].sudo().search([
            ("active", "=", True),
            ("source_lang", "=", self.source_lang or "en"),
            ("target_lang", "=", self.target_lang or "zh"),
        ])
        existing_terms = {
            (entry.source_phrase or entry.source_text or "").strip()
            for entry in existing_entries
            if (entry.source_phrase or entry.source_text)
        }

        prompt = (
            "Extract important document terminology for a translation memory.\n"
            f"Source language: {source_lang}\n"
            f"Target language: {target_lang}\n\n"
            "Focus on domain-specific equipment names, part names, technical "
            "parameters, process terms, material names, measurement labels, and "
            "short noun phrases that must be translated consistently.\n"
            "Avoid ordinary sentences, pure numbers, units alone, dates, and very "
            "generic words.\n"
            "Return only terminology that appears verbatim in SOURCE TEXT.\n"
            "Suggest the best target-language translation for each term.\n\n"
            f"SOURCE TEXT:\n{source_text}\n\n"
            "Respond with valid JSON only, in this exact shape:\n"
            "{\n"
            '  "terms": [\n'
            '    {"source_phrase": "源文术语", "translated_phrase": "Target term", '
            '"reason": "brief reason", "confidence": 0.0}\n'
            "  ]\n"
            "}\n"
            f"Return at most {limit} terms. Prefer longer and more specific terms "
            "over their shorter sub-terms."
        )

        try:
            response = self.provider_id.chat(
                self.env["mail.message"],
                model=self.model_id,
                stream=False,
                tools=None,
                prepend_messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are a senior technical translator and terminology "
                            "manager. Always respond with valid JSON only."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
            raw_text = self._extract_response_text(response)
        except Exception as e:
            _logger.warning("Terminology extraction failed: %s", e)
            return {"candidates": [], "error": str(e)}

        candidates = []
        seen = set()
        source_no_space = re.sub(r"\s+", "", source_text)
        for item in self._parse_terminology_json(raw_text):
            if not isinstance(item, dict):
                continue
            source_phrase = (item.get("source_phrase") or item.get("source") or "").strip()
            translated_phrase = (
                item.get("translated_phrase")
                or item.get("translation")
                or item.get("target_phrase")
                or item.get("target")
                or ""
            ).strip()
            if not source_phrase or not translated_phrase:
                continue
            if source_phrase in existing_terms or source_phrase in seen:
                continue
            if source_phrase not in source_text and source_phrase not in source_no_space:
                continue
            if re.fullmatch(r"[\d\s.,:;/%°℃\-+]+", source_phrase):
                continue
            seen.add(source_phrase)
            candidates.append({
                "source_phrase": source_phrase,
                "translated_phrase": translated_phrase,
                "reason": (item.get("reason") or item.get("analysis") or "").strip(),
                "confidence": item.get("confidence"),
            })
            if len(candidates) >= limit:
                break

        return {"candidates": candidates}

    def import_terminology_candidates(self, candidates):
        """Save approved terminology candidates into translation memory."""
        self.ensure_one()
        GlossaryModel = self.env["llm.translation.glossary"].sudo()
        imported = []
        errors = []
        source_lang = self.source_lang or "en"
        target_lang = self.target_lang or "zh"

        for item in candidates or []:
            source_phrase = (item.get("source_phrase") or item.get("source") or "").strip()
            translated_phrase = (
                item.get("translated_phrase")
                or item.get("translation")
                or item.get("target_phrase")
                or item.get("target")
                or ""
            ).strip()
            if not source_phrase or not translated_phrase:
                continue
            entry = GlossaryModel._upsert_entry(
                source_phrase,
                translated_phrase,
                source_lang,
                target_lang,
            )
            if entry:
                values = {"origin": "manual"}
                reason = (item.get("reason") or "").strip()
                if reason and not entry.ai_analysis:
                    values["ai_analysis"] = reason
                entry.write(values)
                imported.append(entry.id)
            else:
                errors.append(source_phrase)

        return {
            "success": True,
            "imported_count": len(imported),
            "imported_ids": imported,
            "errors": errors,
        }

    def _term_text_contains(self, haystack, needle):
        haystack = html.unescape(re.sub(r"<[^>]+>", "", haystack or "")).lower()
        needle = html.unescape(re.sub(r"<[^>]+>", "", needle or "")).lower()
        haystack_spaced = re.sub(r"\s+", " ", haystack).strip()
        needle_spaced = re.sub(r"\s+", " ", needle).strip()
        if not needle_spaced:
            return False
        return (
            needle_spaced in haystack_spaced
            or re.sub(r"\s+", "", needle_spaced) in re.sub(r"\s+", "", haystack_spaced)
        )

    def validate_terminology_usage(self, limit=200):
        """Check translated lines against mandatory terminology memory."""
        self.ensure_one()
        limit = max(1, min(int(limit or 200), 1000))
        GlossaryModel = self.env["llm.translation.glossary"].sudo()
        entries = GlossaryModel.search([
            ("active", "=", True),
            ("source_lang", "=", self.source_lang or "en"),
            ("target_lang", "=", self.target_lang or "zh"),
            ("source_phrase", "!=", False),
            ("new_phrase", "!=", False),
        ], order="frequency desc")

        issues = []
        for line in self.line_ids.sorted("sequence"):
            if line.is_empty or line.line_type == "image_ocr":
                continue
            source_text = line.source_text or ""
            translated_text = line.translated_text or ""
            missing_terms = []
            for entry in entries:
                source_phrase = (entry.source_phrase or entry.source_text or "").strip()
                expected = (entry.new_phrase or entry.translated_text or "").strip()
                if not source_phrase or not expected:
                    continue
                if not self._term_text_contains(source_text, source_phrase):
                    continue
                if self._term_text_contains(translated_text, expected):
                    continue
                missing_terms.append({
                    "source_phrase": source_phrase,
                    "expected_translation": expected,
                    "entry_id": entry.id,
                })
            if missing_terms:
                issues.append({
                    "line_id": line.id,
                    "sequence": line.sequence,
                    "source_text": source_text,
                    "translated_text": translated_text,
                    "missing_terms": missing_terms,
                })
                if len(issues) >= limit:
                    break

        return {
            "issue_count": len(issues),
            "issues": issues,
        }

    def retranslate_terminology_violations(self, line_ids=None, limit=20):
        """Re-translate lines that failed terminology validation."""
        self.ensure_one()
        validation = self.validate_terminology_usage(limit=1000)
        issue_ids = [issue["line_id"] for issue in validation["issues"]]
        if line_ids:
            requested = {int(line_id) for line_id in line_ids}
            issue_ids = [line_id for line_id in issue_ids if line_id in requested]
        issue_ids = issue_ids[:max(1, min(int(limit or 20), 100))]
        lines = self.env["llm.translation.line"].browse(issue_ids).exists()

        updated = []
        errors = []
        for line in lines.sorted("sequence"):
            if line.is_empty or line.line_type == "image_ocr":
                continue
            try:
                line.write({"state": "pending", "translated_text": False, "reasoning": False})
                self._translate_single_line(line)
                updated.append(line.id)
                self.env.cr.commit()
            except Exception as e:
                _logger.warning("Terminology retranslation failed for line %s: %s", line.id, e)
                line.write({
                    "state": "error",
                    "translated_text": f"[TERMINOLOGY RETRANSLATION ERROR: {e}]",
                })
                errors.append({"line_id": line.id, "error": str(e)})
                self.env.cr.commit()

        if updated:
            self._finalize_translation(force_rebuild=True)
            self.env.cr.commit()

        return {
            "success": True,
            "updated_line_ids": updated,
            "errors": errors,
            "remaining": self.validate_terminology_usage(limit=1000),
        }

    # =========================================================================
    # TRANSLATION
    # =========================================================================

    def _build_default_system_prompt(self):
        """Build the default base system prompt shown in the settings dialog."""
        self.ensure_one()
        source_lang = self._get_source_lang_name()
        target_lang = self._get_target_lang_name()

        prompt = (
            f"You are a professional document translator. "
            f"Translate the following text from {source_lang} to {target_lang}. "
            f"Rules:\n"
            f"1. Preserve the original meaning, tone, and style as closely as possible.\n"
            f"2. Do NOT add explanations, notes, or commentary.\n"
            f"3. Do NOT wrap the translation in quotes or any markers.\n"
            f"4. Output ONLY the translated text, nothing else.\n"
            f"5. Maintain paragraph structure and formatting cues.\n"
            f"6. For proper nouns, technical terms, or brand names, use the "
            f"glossary below if available. Otherwise keep the original or use "
            f"the standard translation in the target language.\n"
            f"7. If the text contains code snippets, URLs, or file paths, keep them unchanged.\n"
            f"8. Do NOT include any reasoning, thinking, or <think> tags in your response.\n"
            f"9. If the text contains [TEXTBOX] markers, they separate different text areas. "
            f"Translate each section independently but keep [TEXTBOX] markers exactly as-is "
            f"on their own line to maintain the section structure.\n"
            f"10. Some text segments contain inline formatting markers: "
            f"<b>text</b> for bold, <i>text</i> for italic, <u>text</u> for underline. "
            f"PRESERVE these tags in the translation at the same relative positions. "
            f"Translate only the content inside the tags, never translate the tag names themselves.\n"
        )
        if self.target_lang == "zh":
            prompt += (
                "11. The target language is Simplified Chinese. Use Simplified Chinese "
                "characters only; do not output Traditional Chinese characters.\n"
            )
        elif self.target_lang == "zh_tw":
            prompt += (
                "11. The target language is Traditional Chinese. Use Traditional Chinese "
                "characters and wording.\n"
            )

        return prompt

    def _render_custom_system_prompt(self):
        self.ensure_one()
        prompt = self.custom_system_prompt or self._build_default_system_prompt()
        replacements = {
            "{source_lang}": self._get_source_lang_name(),
            "{target_lang}": self._get_target_lang_name(),
        }
        for placeholder, value in replacements.items():
            prompt = prompt.replace(placeholder, value)
        return prompt.rstrip() + "\n"

    def _build_system_prompt(self, glossary_text="", batch_mode=False):
        """Build the system prompt for the translation task.

        Args:
            glossary_text: Optional glossary section from knowledge base.
            batch_mode: If True, add rule about [SEP] markers for batch translation.

        Returns:
            str: System prompt for the LLM.
        """
        self.ensure_one()
        prompt = self._render_custom_system_prompt()

        if batch_mode:
            prompt += (
                f"12. The input contains MULTIPLE paragraphs separated by [SEP] markers. "
                f"Translate each paragraph independently. Your output MUST contain "
                f"the EXACT SAME number of [SEP] markers as the input to separate "
                f"the translated paragraphs. Keep [SEP] markers on their own line. "
                f"Do NOT merge, split, or reorder paragraphs. Treat all paragraphs "
                f"in this batch as related context: identify repeated proper nouns, "
                f"project names, product names, technical terms, and organization "
                f"names, then translate them consistently across the whole batch.\n"
            )

        if glossary_text:
            prompt += glossary_text

        return prompt

    def get_prompt_settings(self):
        self.ensure_one()
        default_prompt = self._build_default_system_prompt()
        return {
            "default_prompt": default_prompt,
            "custom_prompt": self.custom_system_prompt or "",
            "current_prompt": self.custom_system_prompt or default_prompt,
            "is_custom": bool(self.custom_system_prompt),
        }

    def update_prompt_settings(self, prompt_text=None):
        self.ensure_one()
        prompt_text = (prompt_text or "").strip()
        self.write({"custom_system_prompt": prompt_text or False})
        return self.get_prompt_settings()

    def action_start_translation(self):
        """Start the translation process. Can be resumed if interrupted."""
        self.ensure_one()

        if not self.source_attachment_id:
            raise UserError(_("Please upload a source document first."))
        if not self.line_ids:
            raise UserError(_("No paragraphs extracted. Please re-upload the document."))
        if not self.provider_id or not self.model_id:
            raise UserError(_("Please select a provider and model."))
        if self.state not in ("draft", "error", "translating"):
            raise UserError(_("Translation can only be started from draft or error state."))

        self.write({
            "state": "translating",
            "error_message": False,
        })

        # Get pending lines (skip already translated and empty ones)
        pending_lines = self.line_ids.filtered(
            lambda l: l.state == "pending" and not l.is_empty
        )

        if not pending_lines:
            self._finalize_translation()
            return

        try:
            self._translate_lines(pending_lines)
        except Exception as e:
            _logger.exception("Translation failed for %s", self.name)
            self.write({
                "state": "error",
                "error_message": str(e),
            })
            raise

    # Prompt budget for batch translation.
    # Regular paragraphs are packed until this token ceiling is reached.
    BATCH_MAX_TOKENS = 6000
    BATCH_CANDIDATE_WINDOW = 500
    TABLE_ROW_MARKER = "[ROW]"
    TABLE_CELL_MARKER = "[CELL]"

    @staticmethod
    def _has_mixed_format(runs):
        """Return True if runs have differing bold/italic/underline formatting."""
        if not runs or len(runs) <= 1:
            return False
        ref = runs[0]
        return any(
            r.get('bold') != ref.get('bold')
            or r.get('italic') != ref.get('italic')
            or r.get('underline') != ref.get('underline')
            for r in runs
        )

    @staticmethod
    def _build_marked_source(source_text, runs):
        """Wrap runs with <b>/<i>/<u> tags so the LLM can preserve formatting.

        Only called when the paragraph has mixed inline formatting.
        Falls back to plain source_text when runs produce an empty result.
        """
        if not runs:
            return source_text
        parts = []
        for run in runs:
            text = run.get('text', '')
            if not text:
                continue
            if run.get('underline'):
                text = f'<u>{text}</u>'
            if run.get('italic'):
                text = f'<i>{text}</i>'
            if run.get('bold'):
                text = f'<b>{text}</b>'
            parts.append(text)
        marked = ''.join(parts)
        return marked if marked.strip() else source_text

    # ── Smart batching helpers ────────────────────────────────────

    @staticmethod
    def _parse_line_meta(line):
        """Parse style_metadata JSON from a translation line.

        Returns:
            dict: Parsed metadata or empty dict on failure.
        """
        if not line.style_metadata:
            return {}
        try:
            return json.loads(line.style_metadata)
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def _is_pure_numeric(text):
        """Check if text is non-translatable short label or pure number.
        
        These patterns should be copied as-is without LLM processing:
        - Pure numbers: '123', '45.67'
        - Numbered labels: '1)', '2.', '10)', 'A)', 'a.'
        - Numbering with brackets: '(1)', '(a)', '(10)'
        - Roman numerals: 'i)', 'ii)', 'III.'
        - Currency/units: '$100', '50%', '100kg'
        - Standalone units: 'mm', 'cm', 'kg', 'MPa'
        - Single punctuation or symbols
        
        Args:
            text: Text to check.
        
        Returns:
            bool: True if text should be copied as-is, False otherwise.
        """
        if not text or not isinstance(text, str):
            return False
        text = text.strip()
        if not text:
            return False
        unit_tokens = {
            "mm", "cm", "dm", "m", "km", "in", "ft",
            "mg", "g", "kg", "t",
            "ml", "l", "s", "min", "h",
            "pa", "kpa", "mpa", "bar", "psi",
            "v", "kv", "a", "ma", "w", "kw", "hz",
            "rpm", "n", "nm",
        }
        if text.lower() in unit_tokens:
            return True
        # Match common non-translatable patterns:
        # - Pure integers/decimals: 123, 45.67
        # - Numbered labels: 1), 2., 10), A), a., I.
        # - Parenthesized numbers: (1), (a), (10)
        # - Numbers with units/currency: $100, 50%, 100kg, 100m²
        # - Single letters/symbols
        return re.match(
            r'^('
            r'\d+([.,]\d+)*'            # integers or decimals: 123, 45.67, 1,000
            r'|[A-Za-z]'                # single standalone letter: A, b
            r'|[A-Za-z\d]+[).]'         # numbered labels: 1), 2., A), a., 10)
            r'|\([A-Za-z\d]+\)'         # parenthesized: (1), (a), (10)
            r'|[ivxIVX]+[).]'           # roman numerals: i), ii., III)
            r'|[$€£¥]?\d+([.,]\d+)*[%°²³]?'  # currency/units: $100, 50%, 100°
            r'|[^\w\s]'                  # single punctuation/symbol
            r')$',
            text
        ) is not None

    @staticmethod
    def _is_section_break(prev_line, prev_meta, cur_line, cur_meta):
        """Decide whether *cur_line* starts a new semantic section.

        Design principles:
        - A heading paragraph starts a new section, BUT the body
          paragraphs that follow belong to the SAME section.
        - Sequence gaps (empty lines) break a section only when
          the PREVIOUS line is NOT a heading / numbered title.
          (Headings in Word often have empty lines after them —
          that is NOT a semantic break.)
        - Top-level numbering pattern changes start a new section.
        - Numbered ↔ non-numbered transitions start a new section.

        Returns:
            bool
        """
        cur_style = (cur_meta.get("style") or "").lower()
        prev_style = (prev_meta.get("style") or "").lower()
        cur_is_heading = any(k in cur_style for k in ("heading", "title", "toc", "目录"))
        prev_is_heading = any(k in prev_style for k in ("heading", "title", "toc", "目录"))

        prev_prefix = prev_meta.get("numbering_prefix") or ""
        prev_has_num = bool(prev_prefix.strip())
        # Even non-heading numbered lines at level 0 that served as
        # "the only line in a batch" should pull in body text.
        # We relax the gap rule when the previous was a heading or
        # a level-0 numbered title (those are often section-level items
        # with empty lines after them in Word).
        prev_level = prev_meta.get("numbering_level")
        prev_is_numbered_title = prev_has_num and prev_level == 0

        # ── 1. Current line is a heading → new section ──
        #    (unless previous is ALSO a heading — keep consecutive headings together)
        if cur_is_heading and not prev_is_heading:
            return True

        # ── 2. Sequence gap (empty lines) ──
        #    Skip this check when the previous line is a heading or
        #    a level-0 numbered title (Word often puts empty lines
        #    after these — that is not a semantic break).
        if not prev_is_heading and not prev_is_numbered_title:
            seq_gap = (cur_line.sequence or 0) - (prev_line.sequence or 0)
            if seq_gap > 10:
                return True

        # ── 3. Top-level numbering pattern change ──
        cur_level = cur_meta.get("numbering_level")
        cur_prefix = cur_meta.get("numbering_prefix") or ""

        if cur_level is not None and prev_level is not None:
            if cur_level == 0 and prev_level == 0:
                cur_clean = re.sub(r'[\d\u4e00-\u9fff]+', '#', cur_prefix.strip())
                prev_clean = re.sub(r'[\d\u4e00-\u9fff]+', '#', prev_prefix.strip())
                if cur_clean != prev_clean:
                    return True

        # ── 4. Numbered ↔ non-numbered transition ──
        has_cur_num = bool(cur_prefix.strip())
        if has_cur_num != prev_has_num:
            return True

        # ── 5. Table row ↔ non-table row transition ──
        cur_is_table = bool(cur_meta.get("is_table_row"))
        prev_is_table = bool(prev_meta.get("is_table_row"))
        if cur_is_table != prev_is_table:
            return True
        # Different tables → break
        if cur_is_table and prev_is_table:
            if cur_meta.get("table_index") != prev_meta.get("table_index"):
                return True

        return False

    def _collect_smart_batch(self, pending_lines):
        """Select lines for the next batch using structural heuristics.

        Groups consecutive paragraphs that belong to the same logical
        section — for example a heading together with the body text
        below it, or all items of a numbered list, or a block of
        normal paragraphs separated by empty lines from the next block.

        Respects the prompt token budget.

        Args:
            pending_lines: Recordset of pending ``llm.translation.line``
                records, **already sorted** by sequence.

        Returns:
            recordset: Subset of *pending_lines* to translate in one call.
        """
        if not pending_lines:
            return pending_lines.browse()

        first_line = pending_lines[0]
        first_meta = self._parse_line_meta(first_line)
        first_is_table = (
            first_line.line_type == "table_cell"
            and self.TABLE_CELL_MARKER in (first_line.source_text or "")
        )

        if first_line.line_type == "image_ocr":
            return self.env["llm.translation.line"].browse([first_line.id])

        if first_is_table:
            table_index = first_meta.get("table_index")
            batch_ids = []
            total_tokens = 0
            for line in pending_lines:
                meta = self._parse_line_meta(line)
                is_same_table_row = (
                    line.line_type == "table_cell"
                    and self.TABLE_CELL_MARKER in (line.source_text or "")
                    and meta.get("table_index") == table_index
                )
                if not is_same_table_row:
                    break

                tokens = line.estimated_tokens or docx_handler.estimate_tokens(line.source_text or "")
                if batch_ids and total_tokens + tokens > self.BATCH_MAX_TOKENS:
                    break
                batch_ids.append(line.id)
                total_tokens += tokens

            return self.env["llm.translation.line"].browse(batch_ids)

        batch_ids = []
        total_tokens = 0
        prev_meta = {}
        prev_line = None

        for line in pending_lines:
            meta = self._parse_line_meta(line)
            tokens = line.estimated_tokens or docx_handler.estimate_tokens(line.source_text or "")
            is_boundary_line = (
                line.line_type == "image_ocr"
                or (
                    line.line_type == "table_cell"
                    and self.TABLE_CELL_MARKER in (line.source_text or "")
                )
            )

            # Image OCR and table blocks have their own translation path.
            # Keep them out of regular paragraph batches.
            if is_boundary_line:
                break

            if batch_ids:
                if total_tokens + tokens > self.BATCH_MAX_TOKENS and total_tokens > 0:
                    break

            batch_ids.append(line.id)
            total_tokens += tokens
            prev_meta = meta
            prev_line = line

        return self.env["llm.translation.line"].browse(batch_ids)

    def action_translate_next(self, single_line=False):
        """Translate the next *smart batch* of pending lines.

        Uses structural heuristics (headings, numbering, empty lines,
        style changes) to group related paragraphs, then sends them
        in a single LLM call separated by ``[SEP]`` markers.

        Returns:
            dict: Status dict with keys:
                - 'finished': bool, True if all lines are done
                - 'translated_line_ids': list[int]
                - 'progress': float 0-100
                - 'total_lines': int
                - 'translated_lines': int
                - 'error': str or False
        """
        self.ensure_one()

        if not self.provider_id or not self.model_id:
            return {"error": "No provider/model configured", "finished": True}

        if self.state not in ("draft", "error", "translating", "done"):
            return {"error": f"Cannot translate in state '{self.state}'", "finished": True}

        # Set state to translating if needed
        if self.state != "translating":
            self.write({"state": "translating", "error_message": False})

        pending_domain = [
            ("translation_id", "=", self.id),
            ("state", "=", "pending"),
            ("is_empty", "=", False),
        ]
        first_candidate = self.env["llm.translation.line"].search(
            pending_domain,
            order="sequence asc",
            limit=1,
        )

        if not first_candidate:
            # All done - finalize
            self._finalize_translation()
            self.env.cr.commit()
            return {
                "finished": True,
                "translated_line_ids": [],
                "progress": self.progress,
                "total_lines": self.total_lines,
                "translated_lines": self.translated_lines,
                "error": False,
            }

        candidate_limit = self.BATCH_CANDIDATE_WINDOW
        first_meta = self._parse_line_meta(first_candidate)
        if first_candidate.line_type == "image_ocr":
            candidate_limit = 1
        elif (
            first_candidate.line_type == "table_cell"
            and self.TABLE_CELL_MARKER in (first_candidate.source_text or "")
        ):
            candidate_limit = max(
                int(first_meta.get("table_row_count") or 0) + 5,
                self.BATCH_CANDIDATE_WINDOW,
            )

        # Fetch enough pending lines so table batches can include the whole
        # table when it fits within the token ceiling.
        candidate_lines = self.env["llm.translation.line"].search(
            pending_domain,
            order="sequence asc",
            limit=candidate_limit,
        )

        if not candidate_lines:
            # All done - finalize
            self._finalize_translation()
            self.env.cr.commit()
            return {
                "finished": True,
                "translated_line_ids": [],
                "progress": self.progress,
                "total_lines": self.total_lines,
                "translated_lines": self.translated_lines,
                "error": False,
            }

        # --- Smart batch selection ---
        if single_line:
            pending_lines = candidate_lines[:1]
        else:
            pending_lines = self._collect_smart_batch(candidate_lines)
        _logger.info(
            "%s batch: selected %d/%d pending lines (tokens ~%d)",
            "Single-line" if single_line else "Smart",
            len(pending_lines),
            len(candidate_lines),
            sum(l.estimated_tokens or 0 for l in pending_lines),
        )

        # --- Collect glossary/TM for all lines in the batch ---
        all_source_texts = [l.source_text for l in pending_lines]
        combined_source = "\n".join(all_source_texts)
        glossary_text = self._prepare_glossary(combined_source)
        tm_text = self._prepare_translation_memory(combined_source)
        combined_glossary = glossary_text + tm_text

        # ── Separate image_ocr, table rows from regular lines ──
        image_ocr_lines = [l for l in pending_lines if l.line_type == "image_ocr"]
        # Table rows are translated as a whole table block when possible.
        table_row_lines = [l for l in pending_lines
                           if l.line_type == "table_cell"
                           and self.TABLE_CELL_MARKER in (l.source_text or "")]
        regular_lines = [l for l in pending_lines
                         if l not in table_row_lines and l not in image_ocr_lines]

        use_batch = len(regular_lines) > 1
        system_prompt = self._build_system_prompt(
            combined_glossary, batch_mode=use_batch
        )

        # Build user message (with [SEP] markers only when batching)
        # For lines with mixed run formatting, wrap with <b>/<i>/<u> tags
        sep_marker = "[SEP]"

        def _get_user_text(line_rec):
            meta = self._parse_line_meta(line_rec)
            runs = meta.get('runs', [])
            if self._has_mixed_format(runs):
                return self._build_marked_source(line_rec.source_text, runs)
            return line_rec.source_text

        line_errors = []
        translated_ids = []

        # ── 0. Handle image OCR lines: multimodal translation ──
        for ocr_line in image_ocr_lines:
            try:
                self._translate_image_ocr_line(ocr_line)
                translated_ids.append(ocr_line.id)
            except Exception as e:
                _logger.error(
                    "Image OCR translation failed seq=%s: %s",
                    ocr_line.sequence, e,
                )
                ocr_line.write({
                    "state": "error",
                    "translated_text": f"[IMAGE OCR ERROR: {e}]",
                })
                line_errors.append(str(e))

        # ── 1. Handle table rows: per-cell translation ──
        if table_row_lines:
            try:
                table_lines = self.env["llm.translation.line"].browse(
                    [line.id for line in table_row_lines]
                )
                translated_ids.extend(
                    self._translate_table_rows(table_lines, combined_glossary)
                )
            except Exception as e:
                _logger.error(
                    "Whole-table translate failed for translation %s: %s",
                    self.id, e,
                )
                for tr_line in table_row_lines:
                    tr_line.write({
                        "state": "error",
                        "translated_text": f"[TRANSLATION ERROR: {e}]",
                    })
                line_errors.append(str(e))

        # ── 2. Separate pure numeric lines (no LLM needed) ──
        pure_numeric_lines = [l for l in regular_lines
                              if self._is_pure_numeric(l.source_text)]
        translatable_lines = [l for l in regular_lines if l not in pure_numeric_lines]

        # Handle pure numeric lines: just copy text
        for num_line in pure_numeric_lines:
            num_line.write({
                "translated_text": (num_line.source_text or "").strip(),
                "reasoning": False,
                "state": "done",
            })
            translated_ids.append(num_line.id)

        # ── 3. Handle translatable lines via batch or single LLM call ──
        if translatable_lines:
            use_batch = len(translatable_lines) > 1
            system_prompt = self._build_system_prompt(
                combined_glossary, batch_mode=use_batch
            )

            user_texts = [_get_user_text(l) for l in translatable_lines]
            if use_batch:
                user_content = f"\n{sep_marker}\n".join(user_texts)
            else:
                user_content = user_texts[0]

            messages_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]

            try:
                response = self.provider_id.chat(
                    self.env["mail.message"],
                    model=self.model_id,
                    stream=False,
                    tools=None,
                    prepend_messages=messages_list,
                )

                raw_text = self._extract_response_text(response)
                translated_text, reasoning = self._strip_think_tags(raw_text)

                if not use_batch:
                    # Single line — assign directly
                    translatable_lines[0].write({
                        "translated_text": translated_text,
                        "reasoning": reasoning or False,
                        "state": "done",
                    })
                    translated_ids.append(translatable_lines[0].id)
                else:
                    # Split response by [SEP] marker
                    segments = [s.strip() for s in translated_text.split(sep_marker)]

                    if len(segments) == len(translatable_lines):
                        for line_rec, seg in zip(translatable_lines, segments):
                            line_rec.write({
                                "translated_text": seg,
                                "reasoning": reasoning or False,
                                "state": "done",
                            })
                            translated_ids.append(line_rec.id)
                    else:
                        _logger.warning(
                            "Batch SEP count mismatch: expected %d, got %d. "
                            "Falling back to one line only; remaining lines stay pending.",
                            len(translatable_lines), len(segments),
                        )
                        line_rec = translatable_lines[0]
                        try:
                            self._translate_single_line(line_rec, combined_glossary)
                            translated_ids.append(line_rec.id)
                        except Exception as e:
                            _logger.error(
                                "Fallback translate failed for seq=%s: %s",
                                line_rec.sequence, e,
                            )
                            line_rec.write({
                                "state": "error",
                                "translated_text": f"[TRANSLATION ERROR: {e}]",
                            })
                            translated_ids.append(line_rec.id)
                            line_errors.append(str(e))

            except Exception as e:
                _logger.error(
                    "Batch translation failed, falling back to one line only: %s", e,
                )
                line_rec = translatable_lines[0]
                try:
                    self._translate_single_line(line_rec)
                    translated_ids.append(line_rec.id)
                except Exception as e2:
                    _logger.error(
                        "Individual translate failed for seq=%s: %s",
                        line_rec.sequence, e2,
                    )
                    line_rec.write({
                        "state": "error",
                        "translated_text": f"[TRANSLATION ERROR: {e2}]",
                    })
                    translated_ids.append(line_rec.id)
                    line_errors.append(str(e2))

        self.env.cr.commit()

        # Recompute progress
        self.invalidate_recordset(["progress", "total_lines", "translated_lines"])

        # Check if more lines remain
        remaining = self.env["llm.translation.line"].search_count([
            ("translation_id", "=", self.id),
            ("state", "=", "pending"),
            ("is_empty", "=", False),
        ])

        # Check if there are error lines
        error_lines = self.env["llm.translation.line"].search_count([
            ("translation_id", "=", self.id),
            ("state", "=", "error"),
            ("is_empty", "=", False),
        ])

        finished = remaining == 0
        if finished:
            self._finalize_translation()
            self.env.cr.commit()

        # Debug logging
        _logger.info(
            "action_translate_next: remaining_pending=%d, error_lines=%d, total=%d, translated=%d, finished=%s",
            remaining, error_lines, self.total_lines, self.translated_lines, finished,
        )

        result = {
            "finished": finished,
            "translated_line_ids": translated_ids,
            "error_line_ids": [
                line.id for line in pending_lines if line.state == "error"
            ],
            "progress": self.progress,
            "total_lines": self.total_lines,
            "translated_lines": self.translated_lines,
            "error": "; ".join(line_errors) if line_errors else False,
            "debug_info": {
                "remaining_pending": remaining,
                "error_lines_count": error_lines,
            },
        }
        return result

    def _translate_table_rows(self, table_lines, combined_glossary=None):
        """Translate consecutive rows from one table in a single LLM call.

        Rows are separated with [ROW] and cells with [CELL]. The response must
        preserve the same row and cell counts; otherwise we fall back to the
        existing row/cell translator so the document structure stays valid.
        """
        self.ensure_one()
        table_lines = table_lines.sorted("sequence")
        if not table_lines:
            return []

        combined_source = "\n".join(line.source_text or "" for line in table_lines)
        if combined_glossary is None:
            glossary_text = self._prepare_glossary(combined_source)
            tm_text = self._prepare_translation_memory(combined_source)
            combined_glossary = glossary_text + tm_text

        system_prompt = self._build_system_prompt(combined_glossary)
        table_system_prompt = (
            system_prompt
            + "\nYou are translating one complete table. "
            + "Rows are separated by [ROW]. Cells are separated by [CELL]. "
            + "Return only the translated table text. Preserve exactly the same "
            + "number of rows, cells, [ROW] markers, and [CELL] markers. "
            + "Do not add markdown, explanations, bullets, numbering, or code fences. "
            + "Keep empty cells and pure numeric cells unchanged."
        )

        expected_cell_counts = []
        user_rows = []
        for line in table_lines:
            source_cells = [
                cell.strip()
                for cell in (line.source_text or "").split(self.TABLE_CELL_MARKER)
            ]
            expected_cell_counts.append(len(source_cells))

            meta = self._parse_line_meta(line)
            cells_meta = meta.get("cells", [])
            user_cells = []
            for ci, cell_text in enumerate(source_cells):
                cm = cells_meta[ci] if ci < len(cells_meta) else {}
                cell_runs = cm.get("runs", [])
                if self._has_mixed_format(cell_runs):
                    user_cells.append(self._build_marked_source(cell_text, cell_runs))
                else:
                    user_cells.append(cell_text)
            user_rows.append(f" {self.TABLE_CELL_MARKER} ".join(user_cells))

        user_content = f"\n{self.TABLE_ROW_MARKER}\n".join(user_rows)

        try:
            response = self.provider_id.chat(
                self.env["mail.message"],
                model=self.model_id,
                stream=False,
                tools=None,
                prepend_messages=[
                    {"role": "system", "content": table_system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )
            raw_text = self._extract_response_text(response)
            translated_table, reasoning = self._strip_think_tags(raw_text)
            translated_table = translated_table.strip()
            translated_table = re.sub(
                rf"^\s*{re.escape(self.TABLE_ROW_MARKER)}\s*",
                "",
                translated_table,
            )
            translated_table = re.sub(
                rf"\s*{re.escape(self.TABLE_ROW_MARKER)}\s*$",
                "",
                translated_table,
            )
            translated_rows = [
                row.strip()
                for row in re.split(
                    rf"\s*{re.escape(self.TABLE_ROW_MARKER)}\s*",
                    translated_table,
                )
            ]

            if len(translated_rows) != len(table_lines):
                raise ValueError(
                    "table row count mismatch: expected %d, got %d"
                    % (len(table_lines), len(translated_rows))
                )

            parsed_rows = []
            for row_text, expected_count in zip(translated_rows, expected_cell_counts):
                cells = [
                    cell.strip()
                    for cell in re.split(
                        rf"\s*{re.escape(self.TABLE_CELL_MARKER)}\s*",
                        row_text,
                    )
                ]
                if len(cells) != expected_count:
                    raise ValueError(
                        "table cell count mismatch: expected %d, got %d"
                        % (expected_count, len(cells))
                    )
                parsed_rows.append(cells)

            translated_ids = []
            for line, cells in zip(table_lines, parsed_rows):
                line.write({
                    "translated_text": f" {self.TABLE_CELL_MARKER} ".join(cells),
                    "reasoning": reasoning or False,
                    "state": "done",
                })
                translated_ids.append(line.id)

            _logger.info(
                "Translated table block: table_index=%s rows=%d",
                self._parse_line_meta(table_lines[0]).get("table_index"),
                len(table_lines),
            )
            return translated_ids
        except Exception as e:
            _logger.warning(
                "Whole-table translation failed; falling back row-by-row: %s",
                e,
            )
            translated_ids = []
            for line in table_lines:
                self._translate_table_row_cells(line, combined_glossary)
                translated_ids.append(line.id)
            return translated_ids

    def _translate_table_row_cells(self, line_rec, combined_glossary=None):
        """Translate a table row by translating each cell individually.

        Splits source_text by [CELL], translates each cell via a separate
        LLM call, then reassembles with [CELL] separators.  This guarantees
        the column structure is always preserved, regardless of the LLM's
        adherence to separator instructions.

        Args:
            line_rec: llm.translation.line record (table_cell type).
            combined_glossary: Pre-computed glossary text (optional).
        """
        self.ensure_one()
        if combined_glossary is None:
            glossary_text = self._prepare_glossary(line_rec.source_text)
            tm_text = self._prepare_translation_memory(line_rec.source_text)
            combined_glossary = glossary_text + tm_text

        system_prompt = self._build_system_prompt(combined_glossary)

        source_cells = [c.strip() for c in line_rec.source_text.split("[CELL]")]

        # Also grab per-cell runs from metadata for format tagging
        meta = self._parse_line_meta(line_rec)
        cells_meta = meta.get("cells", [])

        translated_cells = []
        pending_cells = []
        all_reasoning = []

        for ci, cell_text in enumerate(source_cells):
            if not cell_text.strip():
                # Empty cell — keep empty
                translated_cells.append("")
                continue

            # Pure numeric cells don't need translation
            if self._is_pure_numeric(cell_text):
                translated_cells.append(cell_text.strip())
                continue

            # Check if this cell has mixed formatting (runs)
            cm = cells_meta[ci] if ci < len(cells_meta) else {}
            cell_runs = cm.get("runs", [])
            if self._has_mixed_format(cell_runs):
                user_text = self._build_marked_source(cell_text, cell_runs)
            else:
                user_text = cell_text

            translated_cells.append(None)
            pending_cells.append((ci, user_text))

        if len(pending_cells) > 1:
            batch_system_prompt = (
                system_prompt
                + "\nTranslate each table cell segment independently. "
                + "Return exactly the same number of segments, separated only by [CELL]."
            )
            batch_user_text = "\n[CELL]\n".join(text for _idx, text in pending_cells)
            try:
                response = self.provider_id.chat(
                    self.env["mail.message"],
                    model=self.model_id,
                    stream=False,
                    tools=None,
                    prepend_messages=[
                        {"role": "system", "content": batch_system_prompt},
                        {"role": "user", "content": batch_user_text},
                    ],
                )
                raw_text = self._extract_response_text(response)
                batch_text, reasoning = self._strip_think_tags(raw_text)
                segments = [s.strip() for s in batch_text.split("[CELL]")]
                if len(segments) == len(pending_cells):
                    for (ci, _user_text), segment in zip(pending_cells, segments):
                        translated_cells[ci] = segment
                    if reasoning:
                        all_reasoning.append(reasoning)
                    pending_cells = []
                else:
                    _logger.warning(
                        "Table row batch cell count mismatch: expected %d, got %d; falling back per cell",
                        len(pending_cells),
                        len(segments),
                    )
            except Exception as e:
                _logger.warning("Table row batch translation failed, falling back per cell: %s", e)

        for ci, user_text in pending_cells:
            messages_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ]

            response = self.provider_id.chat(
                self.env["mail.message"],
                model=self.model_id,
                stream=False,
                tools=None,
                prepend_messages=messages_list,
            )

            raw_text = self._extract_response_text(response)
            cell_trans, reasoning = self._strip_think_tags(raw_text)
            translated_cells[ci] = cell_trans.strip()
            if reasoning:
                all_reasoning.append(reasoning)

        # Reassemble with [CELL] separators
        final_text = " [CELL] ".join(translated_cells)
        final_reasoning = "\n".join(all_reasoning) if all_reasoning else False

        line_rec.write({
            "translated_text": final_text,
            "reasoning": final_reasoning,
            "state": "done",
        })

    # =========================================================================
    # IMAGE OCR TRANSLATION (multimodal LLM)
    # =========================================================================

    def _translate_image_ocr_line(self, line_rec):
        """Translate an image_ocr line using multimodal LLM vision.

        Sends the image to the LLM asking it to:
        1. Identify all text blocks and their bounding boxes (percentage coords)
        2. Translate each text block to the target language

        Stores structured JSON result in image_ocr_result field.
        The result is used by rebuild_docx_from_original to create overlay textboxes.

        Supports multiple provider backends:
        - Ollama: uses ``images`` field with raw base64 strings
        - OpenAI/Anthropic: uses ``image_url`` content blocks

        Args:
            line_rec: llm.translation.line record with line_type='image_ocr'.
        """
        self.ensure_one()

        # Check if the selected model supports multimodal/vision capability
        if not self.model_id:
            line_rec.write({
                "translated_text": "[No model selected for OCR]",
                "image_ocr_result": json.dumps({"text_blocks": []}),
                "state": "error",
            })
            return

        meta = self._parse_line_meta(line_rec)

        # Load image from ir.attachment (preferred) or legacy inline data_uri
        image_b64 = ""
        image_mimetype = "image/png"
        att_id = meta.get("image_attachment_id")
        if att_id:
            att = self.env["ir.attachment"].browse(att_id)
            if att.exists() and att.datas:
                image_mimetype = att.mimetype or "image/png"
                # att.datas is already base64-encoded by Odoo
                image_b64 = att.datas.decode() if isinstance(att.datas, bytes) else att.datas
        if not image_b64:
            # Try legacy inline data_uri
            legacy_uri = meta.get("image_data_uri", "")
            if legacy_uri:
                m = re.match(r'data:([^;]+);base64,(.*)', legacy_uri, re.DOTALL)
                if m:
                    image_mimetype = m.group(1)
                    image_b64 = m.group(2)

        if not image_b64:
            line_rec.write({
                "translated_text": "[No image data available]",
                "image_ocr_result": json.dumps({"text_blocks": []}),
                "state": "done",
            })
            return

        source_lang = self._get_source_lang_name()
        target_lang = self._get_target_lang_name()

        system_prompt = (
            "You are an OCR and translation system.  Analyze images and extract text blocks.\n\n"
            "CRITICAL COORDINATE RULES:\n"
            "- The FULL image is 100% wide and 100% tall.\n"
            "- x_pct, y_pct = top-left corner as % of image size (0 to 100).\n"
            "- w_pct, h_pct = width/height of box as % of image size.\n"
            "- EVERY value MUST be between 0 and 100.  Never exceed 100.\n"
            "- Top of image = y_pct 0.  Bottom of image = y_pct ~95-100.\n"
            "- Left edge = x_pct 0.  Right edge = x_pct ~95-100.\n\n"
            "TASK:\n"
            "1. Find the main text blocks (group nearby words into one block).\n"
            "2. For each block: estimate bounding box in image percentages.\n"
            "3. Translate from " + source_lang + " to " + target_lang + ".\n"
            "4. Return ONLY valid JSON, no markdown, no explanation.\n"
            "5. If no text found, return {\"text_blocks\": []}.\n"
            "6. Skip tiny decorative text, watermarks, or unreadable text.\n"
            "7. Limit output to max 40 most important text blocks.\n\n"
            "Example (a box in the top-left quarter of the image):\n"
            "{\"text_blocks\": [{\"original\": \"Hello\", \"translated\": \"你好\", "
            "\"x_pct\": 5, \"y_pct\": 3, \"w_pct\": 20, \"h_pct\": 6}]}\n"
        )

        user_text = (
            f"Analyze this image from a document. "
            f"Identify all text blocks, determine their precise bounding box positions "
            f"(as percentages of image width/height), and translate them "
            f"from {source_lang} to {target_lang}. "
            f"Return ONLY the JSON result."
        )

        # Build provider-specific prepend_messages with image data.
        # Different providers require different formats for multimodal messages.
        provider_service = self.provider_id.service
        _logger.info(
            "Image OCR: using provider=%s (service=%s), model=%s",
            self.provider_id.name, provider_service, self.model_id.name,
        )

        if provider_service == "ollama":
            # Ollama format: images are passed as a separate `images` list
            # containing raw base64 strings (no data URI prefix).
            messages_list = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text, "images": [image_b64]},
            ]
        else:
            # OpenAI / Anthropic / generic format: image_url content blocks
            image_data_uri = f"data:{image_mimetype};base64,{image_b64}"
            messages_list = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": image_data_uri}},
                    ],
                },
            ]

        try:
            if provider_service == "ollama":
                # For Ollama: call the client directly to bypass
                # normalize_prepend_messages which strips image data.
                client = self.provider_id.client
                params = {
                    "model": self.model_id.name,
                    "stream": False,
                    "messages": messages_list,
                }
                _logger.info(
                    "Image OCR: calling Ollama directly, model=%s, image_size=%d bytes",
                    self.model_id.name, len(image_b64) * 3 // 4,
                )
                raw_response = client.chat(**params)
                raw_text = raw_response.get("message", {}).get("content", "")
                # Handle thinking content from models like Qwen
                thinking = raw_response.get("message", {}).get("thinking", "")
                if thinking:
                    raw_text_with_think = f"<think>{thinking}</think>\n\n{raw_text}"
                else:
                    raw_text_with_think = raw_text
            else:
                # For OpenAI/Anthropic: use the standard provider.chat()
                response = self.provider_id.chat(
                    self.env["mail.message"],
                    model=self.model_id,
                    stream=False,
                    tools=None,
                    prepend_messages=messages_list,
                )
                raw_text_with_think = self._extract_response_text(response)

            raw_text, reasoning = self._strip_think_tags(raw_text_with_think)

            # Parse JSON from response and normalize coordinates
            ocr_result = self._parse_ocr_json(raw_text)
            if ocr_result and ocr_result.get("text_blocks"):
                ocr_result = self._normalize_ocr_coordinates(ocr_result)

            if not ocr_result or not ocr_result.get("text_blocks"):
                line_rec.write({
                    "translated_text": "[No text detected in image]",
                    "image_ocr_result": json.dumps({"text_blocks": []}),
                    "reasoning": reasoning or False,
                    "state": "done",
                })
                return

            # Build human-readable summary for the UI
            summary_parts = []
            for block in ocr_result["text_blocks"]:
                orig = block.get("original", "")
                trans = block.get("translated", "")
                summary_parts.append(f"{orig} → {trans}")

            translated_text = "\n".join(summary_parts)

            line_rec.write({
                "translated_text": translated_text,
                "image_ocr_result": json.dumps(ocr_result, ensure_ascii=False),
                "reasoning": reasoning or False,
                "state": "done",
            })

        except Exception as e:
            _logger.error("Image OCR translation failed: %s", e, exc_info=True)
            line_rec.write({
                "state": "error",
                "translated_text": f"[IMAGE OCR ERROR: {e}]",
            })

    @staticmethod
    def _parse_ocr_json(text):
        """Parse OCR JSON from LLM response, handling common formatting issues.

        The LLM might return JSON directly, in a code block, or with extra text.
        This method tries multiple strategies to extract valid JSON.

        Args:
            text: Raw LLM response text.

        Returns:
            dict or None: Parsed JSON with text_blocks, or None on failure.
        """
        if not text:
            return None

        # Strategy 1: Direct JSON parse
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            pass

        # Strategy 2: Extract from markdown code blocks
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, re.DOTALL)
        if json_match:
            try:
                return json.loads(json_match.group(1))
            except (json.JSONDecodeError, ValueError):
                pass

        # Strategy 3: Find JSON object braces in the text
        brace_start = text.find('{')
        brace_end = text.rfind('}')
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except (json.JSONDecodeError, ValueError):
                pass

        _logger.warning("Could not parse OCR JSON from LLM response: %s", text[:200])
        return None

    @staticmethod
    def _normalize_ocr_coordinates(ocr_result):
        """Normalize OCR bounding box coordinates to valid 0-100 percentage range.

        Some LLMs (especially smaller models) return coordinates exceeding 100%
        for tall/wide images.  They treat Y as a running row counter rather than
        a true percentage of image height.  This method detects and rescales such
        coordinates so that everything fits within 0-100.

        Also deduplicates blocks with identical text at nearly identical positions
        and enforces minimum dimensions for readability.

        Args:
            ocr_result: dict with ``text_blocks`` list from ``_parse_ocr_json``.

        Returns:
            dict: Cleaned ``{"text_blocks": [...]}`` with valid coordinates.
        """
        if not ocr_result or not ocr_result.get("text_blocks"):
            return ocr_result

        blocks = ocr_result["text_blocks"]
        if not blocks:
            return ocr_result

        # ---- 1. Determine actual extent of coordinates ----
        max_x_right = 0   # max(x + w)
        max_y_bottom = 0   # max(y + h)
        for b in blocks:
            x = float(b.get("x_pct", 0))
            y = float(b.get("y_pct", 0))
            w = float(b.get("w_pct", 0))
            h = float(b.get("h_pct", 0))
            max_x_right = max(max_x_right, x + w)
            max_y_bottom = max(max_y_bottom, y + h)

        # ---- 2. Compute scale factors (only if coordinates exceed 100%) ----
        x_scale = (97.0 / max_x_right) if max_x_right > 100 else 1.0
        y_scale = (97.0 / max_y_bottom) if max_y_bottom > 100 else 1.0

        _logger.info(
            "OCR coordinate normalization: max_x_right=%.1f  max_y_bottom=%.1f  "
            "x_scale=%.4f  y_scale=%.4f  blocks_in=%d",
            max_x_right, max_y_bottom, x_scale, y_scale, len(blocks),
        )

        # ---- 3. Normalize, clamp, enforce minimums ----
        normalized = []
        for b in blocks:
            orig = (b.get("original") or "").strip()
            trans = (b.get("translated") or "").strip()
            if not orig and not trans:
                continue

            x = float(b.get("x_pct", 0)) * x_scale
            y = float(b.get("y_pct", 0)) * y_scale
            w = float(b.get("w_pct", 5)) * x_scale
            h = float(b.get("h_pct", 2)) * y_scale

            # Enforce minimum box dimensions (percentage of image)
            w = max(w, 1.5)
            h = max(h, 0.8)

            # Clamp so boxes stay inside image
            x = max(0, min(x, 99))
            y = max(0, min(y, 99))
            w = min(w, 100 - x)
            h = min(h, 100 - y)

            normalized.append({
                "original": orig,
                "translated": trans,
                "x_pct": round(x, 1),
                "y_pct": round(y, 1),
                "w_pct": round(w, 1),
                "h_pct": round(h, 1),
            })

        # ---- 4. Deduplicate (same original text at similar positions) ----
        seen = set()
        deduped = []
        for b in normalized:
            key = (b["original"], round(b["y_pct"], 0), round(b["x_pct"], 0))
            if key not in seen:
                seen.add(key)
                deduped.append(b)

        _logger.info(
            "OCR coordinate normalization: blocks_out=%d (deduped from %d)",
            len(deduped), len(normalized),
        )
        return {"text_blocks": deduped}

    def _translate_single_line(self, line_rec, combined_glossary=None):
        """Translate a single line using the configured LLM provider.

        For table rows, delegates to per-cell translation.
        Skips LLM call for pure numeric text.
        Used as fallback when batch [SEP] parsing fails.

        Args:
            line_rec: llm.translation.line record to translate.
            combined_glossary: Pre-computed glossary text (optional).
        """
        self.ensure_one()

        # Image OCR: multimodal translation
        if line_rec.line_type == "image_ocr":
            return self._translate_image_ocr_line(line_rec)

        # Table rows: translate per-cell to guarantee [CELL] structure
        if line_rec.line_type == "table_cell" and "[CELL]" in (line_rec.source_text or ""):
            return self._translate_table_row_cells(line_rec, combined_glossary)

        source_text = line_rec.source_text or ""

        # Pure numeric text doesn't need translation
        if self._is_pure_numeric(source_text):
            line_rec.write({
                "translated_text": source_text.strip(),
                "reasoning": False,
                "state": "done",
            })
            return

        if combined_glossary is None:
            glossary_text = self._prepare_glossary(source_text)
            tm_text = self._prepare_translation_memory(source_text)
            combined_glossary = glossary_text + tm_text

        system_prompt = self._build_system_prompt(combined_glossary)

        # Use marked source for mixed-format paragraphs
        meta = self._parse_line_meta(line_rec)
        runs = meta.get('runs', [])
        if self._has_mixed_format(runs):
            user_text = self._build_marked_source(source_text, runs)
        else:
            user_text = source_text

        messages_list = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        response = self.provider_id.chat(
            self.env["mail.message"],
            model=self.model_id,
            stream=False,
            tools=None,
            prepend_messages=messages_list,
        )

        raw_text = self._extract_response_text(response)
        translated_text, reasoning = self._strip_think_tags(raw_text)
        line_rec.write({
            "translated_text": translated_text,
            "reasoning": reasoning or False,
            "state": "done",
        })

    def _translate_lines(self, lines):
        """Translate a set of lines using the configured LLM provider.

        Processes each line individually to avoid token limits.
        Commits each translation to DB immediately for resume support.

        Args:
            lines: llm.translation.line recordset to translate.
        """
        self.ensure_one()
        provider = self.provider_id
        model = self.model_id

        total = len(lines)
        for idx, line in enumerate(lines):
            _logger.info(
                "Translating line %d/%d (seq=%s) for %s",
                idx + 1, total, line.sequence, self.name,
            )

            try:
                # Image OCR: multimodal translation
                if line.line_type == "image_ocr":
                    self._translate_image_ocr_line(line)
                    self.env.cr.commit()
                    continue

                # Table rows: per-cell translation
                if line.line_type == "table_cell" and "[CELL]" in (line.source_text or ""):
                    self._translate_table_row_cells(line)
                    self.env.cr.commit()
                    continue

                # Pure numeric text: just copy
                if self._is_pure_numeric(line.source_text):
                    line.write({
                        "translated_text": (line.source_text or "").strip(),
                        "reasoning": False,
                        "state": "done",
                    })
                    self.env.cr.commit()
                    continue

                # Prepare glossary context from knowledge base
                glossary_text = self._prepare_glossary(line.source_text)

                # Also check translation memory (user-corrected glossary)
                tm_text = self._prepare_translation_memory(line.source_text)

                combined_glossary = glossary_text + tm_text

                # Build messages
                system_prompt = self._build_system_prompt(combined_glossary)

                # Use marked source for mixed-format paragraphs
                meta = self._parse_line_meta(line)
                runs = meta.get('runs', [])
                if self._has_mixed_format(runs):
                    user_text = self._build_marked_source(line.source_text, runs)
                else:
                    user_text = line.source_text

                messages_list = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_text},
                ]

                # Call LLM via provider's chat method (non-streaming)
                response = provider.chat(
                    self.env["mail.message"],  # empty recordset
                    model=model,
                    stream=False,
                    tools=None,
                    prepend_messages=messages_list,
                )
                # (No verbose debug logging here)

                # Extract translated text from response
                raw_text = self._extract_response_text(response)
                translated_text, reasoning = self._strip_think_tags(raw_text)

                line.write({
                    "translated_text": translated_text,
                    "reasoning": reasoning or False,
                    "state": "done",
                })

                # Commit after each line for resume support
                self.env.cr.commit()

            except Exception as e:
                _logger.error(
                    "Failed to translate line seq=%s: %s", line.sequence, e
                )
                line.write({
                    "state": "error",
                    "translated_text": f"[TRANSLATION ERROR: {e}]",
                })
                self.env.cr.commit()
                # Continue with next line instead of stopping
                continue

        # Check if all lines are done
        self._finalize_translation()

    def _extract_response_text(self, response):
        """Extract plain text from LLM provider response.

        Handles different response formats from various providers.

        Args:
            response: Raw response from provider chat method.

        Returns:
            str: Extracted text content.
        """
        if isinstance(response, str):
            return response.strip()

        # OpenAI-style response object
        if hasattr(response, "choices"):
            choices = response.choices
            if choices and len(choices) > 0:
                message = choices[0].message
                if hasattr(message, "content"):
                    return (message.content or "").strip()

        # Dict response
        if isinstance(response, dict):
            if "content" in response:
                return response["content"].strip()
            if "choices" in response:
                choices = response["choices"]
                if choices:
                    msg = choices[0].get("message", {})
                    return msg.get("content", "").strip()
            if "text" in response:
                return response["text"].strip()

        # Generator/iterator (streaming or Ollama non-streaming returns generator)
        if hasattr(response, "__iter__") and not isinstance(response, (str, dict, list)):
            parts = []
            for chunk in response:
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif isinstance(chunk, dict):
                    # Ollama format: {"message": {"content": "..."}}
                    msg = chunk.get("message", {})
                    if isinstance(msg, dict) and msg.get("content"):
                        parts.append(msg["content"])
                    elif chunk.get("content"):
                        parts.append(chunk["content"])
                    elif chunk.get("text"):
                        parts.append(chunk["text"])
                elif hasattr(chunk, "choices"):
                    for choice in chunk.choices:
                        if hasattr(choice, "delta") and hasattr(choice.delta, "content"):
                            if choice.delta.content:
                                parts.append(choice.delta.content)
                        elif hasattr(choice, "message") and hasattr(choice.message, "content"):
                            if choice.message.content:
                                parts.append(choice.message.content)
            if parts:
                return "".join(parts).strip()

        # Fallback: convert to string
        _logger.warning("Unknown LLM response type %s, converting to string", type(response).__name__)
        return str(response).strip()

    @staticmethod
    def _strip_think_tags(text):
        """Strip <think>...</think> tags from LLM response.

        Some models (e.g. DeepSeek) emit reasoning within <think> blocks.
        We separate the reasoning from the actual translation.

        Args:
            text: Raw translated text possibly containing <think> tags.

        Returns:
            tuple: (clean_text, reasoning) where reasoning is the
                   content inside <think> tags (may be empty string).
        """
        if not text:
            return ("", "")
        # Extract all <think>...</think> blocks
        pattern = r'<think>(.*?)</think>'
        reasoning_parts = re.findall(pattern, text, re.DOTALL)
        reasoning = "\n".join(part.strip() for part in reasoning_parts).strip()
        # Remove the <think> tags from the text
        clean = re.sub(pattern, '', text, flags=re.DOTALL).strip()
        return (clean, reasoning)

    def _finalize_translation(self, force_rebuild=False):
        """Build the translated document and save it.

        If there are error lines, the document is still built with partial
        results (untranslated paragraphs use source text as fallback).
        The state will be set to 'error' instead of 'done'.
        """
        self.ensure_one()

        # Check for errors
        error_lines = self.line_ids.filtered(lambda l: l.state == "error")
        has_errors = bool(error_lines)
        if has_errors:
            self.write({
                "state": "error",
                "error_message": _(
                    "%d paragraphs failed to translate. You can retry the translation."
                ) % len(error_lines),
            })

        # Build the translated document
        try:
            paragraphs_data = self._prepare_rebuild_data()
            source_name = self.source_attachment_id.name or "document"
            base_name, ext = os.path.splitext(source_name) if "." in source_name else (source_name, "")
            target_lang = self._get_target_lang_name()
            is_pdf_source = (source_name.lower().endswith(".pdf"))

            is_pptx_source = (
                source_name.lower().endswith(".pptx")
                or source_name.lower().endswith(".ppt")
            )

            if is_pptx_source:
                # ── PPTX rebuild: replace text in-place ──
                original_content = base64.b64decode(self.source_attachment_id.datas)
                if source_name.lower().endswith(".ppt") and not source_name.lower().endswith(".pptx"):
                    try:
                        original_content = pptx_handler._convert_ppt_to_pptx_via_libreoffice(original_content)
                    except Exception:
                        original_content = None
                if original_content:
                    result_bytes = pptx_handler.rebuild_pptx_from_original(
                        original_content, paragraphs_data
                    )
                else:
                    raise UserError(_("Failed to convert .ppt file for rebuild."))
                result_name = f"{base_name}_{target_lang}.pptx"
                result_mimetype = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            elif is_pdf_source:
                # ── PDF rebuild: overlay OCR translations onto original ──
                original_content = base64.b64decode(self.source_attachment_id.datas)
                # Collect OCR results from image_ocr lines
                has_pdf_ocr_lines = bool(self.line_ids.filtered(
                    lambda l: l.line_type == "image_ocr"
                ))
                has_pdf_ocr_lines = bool(self.line_ids.filtered(
                    lambda l: l.line_type == "image_ocr"
                ))
                ocr_pages = {}  # page_num -> list of text_blocks
                if has_pdf_ocr_lines:
                    for line in self.line_ids.sorted("sequence"):
                        if line.line_type != "image_ocr":
                            continue
                        meta = json.loads(line.style_metadata) if line.style_metadata else {}
                        page_num = meta.get("para_index", 0)  # para_index = page_num for PDF
                        if line.image_ocr_result:
                            try:
                                ocr_data = json.loads(line.image_ocr_result)
                                blocks = ocr_data.get("text_blocks", [])
                                if blocks:
                                    ocr_pages.setdefault(page_num, []).extend(blocks)
                            except (json.JSONDecodeError, TypeError):
                                pass
                    result_bytes = pdf_handler.rebuild_pdf_with_ocr(
                        original_content, ocr_pages
                    )
                else:
                    result_bytes = pdf_handler.rebuild_pdf_from_original(
                        original_content,
                        paragraphs_data,
                    )
                if has_pdf_ocr_lines:
                    for line in self.line_ids.sorted("sequence"):
                        if line.line_type != "image_ocr":
                            continue
                        meta = json.loads(line.style_metadata) if line.style_metadata else {}
                        page_num = meta.get("para_index", 0)  # para_index = page_num for PDF
                        if line.image_ocr_result:
                            try:
                                ocr_data = json.loads(line.image_ocr_result)
                                blocks = ocr_data.get("text_blocks", [])
                                if blocks:
                                    ocr_pages.setdefault(page_num, []).extend(blocks)
                            except (json.JSONDecodeError, TypeError):
                                pass
                    result_bytes = pdf_handler.rebuild_pdf_with_ocr(
                        original_content, ocr_pages
                    )
                else:
                    result_bytes = pdf_handler.rebuild_pdf_from_original(
                        original_content,
                        paragraphs_data,
                    )
                result_name = f"{base_name}_{target_lang}.pdf"
                result_mimetype = "application/pdf"
            else:
                # ── DOCX rebuild ─────────────────────────────────
                original_content = None
                if self.source_attachment_id and self.source_attachment_id.datas:
                    original_content = base64.b64decode(self.source_attachment_id.datas)
                    if source_name.lower().endswith(".doc") and not source_name.lower().endswith(".docx"):
                        try:
                            original_content = docx_handler._convert_doc_to_docx_via_libreoffice(original_content)
                        except Exception:
                            original_content = None

                if original_content:
                    try:
                        result_bytes = docx_handler.rebuild_docx_from_original(
                            original_content, paragraphs_data
                        )
                    except Exception as e:
                        original_media_count = docx_handler.count_docx_media_files(
                            original_content
                        )
                        if original_media_count:
                            _logger.exception(
                                "rebuild_docx_from_original failed for a document "
                                "with %d media files; refusing image-dropping fallback",
                                original_media_count,
                            )
                            raise UserError(_(
                                "Failed to rebuild the translated Word document "
                                "while preserving images: %s"
                            ) % str(e))
                        _logger.warning(
                            "rebuild_docx_from_original failed for media-free "
                            "document, falling back: %s",
                            e,
                        )
                        result_bytes = docx_handler.rebuild_docx(paragraphs_data)

                    original_media_count = docx_handler.count_docx_media_files(
                        original_content
                    )
                    result_media_count = docx_handler.count_docx_media_files(
                        result_bytes
                    )
                    if original_media_count and not result_media_count:
                        raise UserError(_(
                            "Translated Word document was rebuilt without images. "
                            "Please retry; the image-preserving rebuild failed."
                        ))
                else:
                    result_bytes = docx_handler.rebuild_docx(paragraphs_data)
                result_name = f"{base_name}_{target_lang}.docx"
                result_mimetype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

            # Save as attachment linked to project
            attachment_vals = {
                "name": result_name,
                "datas": base64.b64encode(result_bytes),
                "res_model": "project.project",
                "res_id": self.project_id.id,
                "mimetype": result_mimetype,
            }
            # Delete old result attachment BEFORE creating new one to prevent orphans
            old_result = self.result_attachment_id
            if old_result:
                self.write({"result_attachment_id": False})
                old_result.unlink()

            result_attachment = self.env["ir.attachment"].create(attachment_vals)

            # Also link source to project if not already
            if self.source_attachment_id:
                self.source_attachment_id.write({
                    "res_model": "project.project",
                    "res_id": self.project_id.id,
                })

            vals = {
                "result_attachment_id": result_attachment.id,
            }
            if not has_errors:
                vals["state"] = "done"
            self.write(vals)
            _logger.info("Translation completed: %s → %s (errors: %s)", source_name, result_name, has_errors)

        except Exception as e:
            _logger.exception("Failed to build translated document")
            error_message = _("Failed to build translated document: %s") % str(e)
            self.write({
                "state": "error",
                "error_message": error_message,
            })
            if force_rebuild:
                raise UserError(error_message)

    def _prepare_rebuild_data(self):
        """Prepare data for rebuilding the translated document.

        Handles merging split paragraphs back together.
        Separates header/footer lines from body paragraphs.

        Returns:
            dict: {paragraphs: list[dict], header_text: str, footer_text: str}
        """
        self.ensure_one()
        paragraphs_data = []
        header_translated = ""
        footer_translated = ""
        image_ocr_results = []
        merge_buffer = {"text": "", "meta": None}

        for line in self.line_ids.sorted("sequence"):
            meta = json.loads(line.style_metadata) if line.style_metadata else {}

            # Defensively strip <think> tags for rebuild
            line_translated = line.translated_text or ""
            if "<think>" in line_translated:
                line_translated, _ = self._strip_think_tags(line_translated)

            # Handle header/footer lines separately
            line_type = line.line_type or "body"
            if line_type == "header":
                header_translated = line_translated or line.source_text or ""
                continue
            elif line_type == "footer":
                footer_translated = line_translated or line.source_text or ""
                continue
            elif line_type == "image_ocr":
                # Collect image OCR results for text box creation during rebuild
                if line.image_ocr_result:
                    try:
                        ocr_data = json.loads(line.image_ocr_result)
                        # Normalize coordinates before DOCX rebuild
                        ocr_data = self._normalize_ocr_coordinates(ocr_data)
                        image_ocr_results.append({
                            "para_index": meta.get("para_index"),
                            "image_index": meta.get("image_index", 0),
                            "image_width_px": meta.get("image_width_px"),
                            "image_height_px": meta.get("image_height_px"),
                            "image_placement": meta.get("image_placement", "inline"),
                            "image_offset_h_px": meta.get("image_offset_h_px"),
                            "image_offset_v_px": meta.get("image_offset_v_px"),
                            "text_blocks": ocr_data.get("text_blocks", []),
                        })
                    except (json.JSONDecodeError, TypeError):
                        pass
                continue

            # Body and textbox lines
            if meta.get("is_split"):
                # Accumulate split paragraph parts
                merge_buffer["text"] += line_translated
                if merge_buffer["meta"] is None:
                    merge_buffer["meta"] = meta

                # If this is the last split part, flush
                if meta.get("split_index", 0) == meta.get("split_total", 1) - 1:
                    paragraphs_data.append({
                        "translated_text": merge_buffer["text"],
                        "style_metadata": merge_buffer["meta"],
                    })
                    merge_buffer = {"text": "", "meta": None}
            else:
                # Flush any pending merge buffer
                if merge_buffer["text"]:
                    paragraphs_data.append({
                        "translated_text": merge_buffer["text"],
                        "style_metadata": merge_buffer["meta"],
                    })
                    merge_buffer = {"text": "", "meta": None}

                paragraphs_data.append({
                    "translated_text": line_translated or line.source_text or "",
                    "style_metadata": meta,
                })

        # Flush remaining
        if merge_buffer["text"]:
            paragraphs_data.append({
                "translated_text": merge_buffer["text"],
                "style_metadata": merge_buffer["meta"],
            })

        return {
            "paragraphs": paragraphs_data,
            "header_text": header_translated,
            "footer_text": footer_translated,
            "image_ocr_results": image_ocr_results,
        }

    def _strip_bilingual_export_markers(self, text, keep_formatting=False):
        """Prepare stored line text for WYSIWYG-style bilingual export."""
        text = text or ""
        if "<think>" in text:
            text, _reasoning = self._strip_think_tags(text)
        text = re.split(r"\s*\[TEXTBOX\]\s*", text)[0].strip()
        if not keep_formatting:
            text = re.sub(r"</?(?:b|i|u)>", "", text)
        text = re.sub(r"<(?!/?(?:b|i|u)\b)[^>]+>", "", text)
        text = html.unescape(text)
        return text.strip()

    def _get_bilingual_line_meta(self, line):
        if not line.style_metadata:
            return {}
        if isinstance(line.style_metadata, dict):
            return line.style_metadata
        try:
            return json.loads(line.style_metadata)
        except (json.JSONDecodeError, TypeError):
            return {}

    def _set_bilingual_paragraph_format(self, para, meta):
        alignment = (meta or {}).get("alignment")
        if alignment:
            alignment_map = {
                "LEFT": docx_handler.WD_ALIGN_PARAGRAPH.LEFT,
                "left": docx_handler.WD_ALIGN_PARAGRAPH.LEFT,
                "CENTER": docx_handler.WD_ALIGN_PARAGRAPH.CENTER,
                "center": docx_handler.WD_ALIGN_PARAGRAPH.CENTER,
                "RIGHT": docx_handler.WD_ALIGN_PARAGRAPH.RIGHT,
                "right": docx_handler.WD_ALIGN_PARAGRAPH.RIGHT,
                "JUSTIFY": docx_handler.WD_ALIGN_PARAGRAPH.JUSTIFY,
                "both": docx_handler.WD_ALIGN_PARAGRAPH.JUSTIFY,
                "justify": docx_handler.WD_ALIGN_PARAGRAPH.JUSTIFY,
            }
            if alignment in alignment_map:
                para.alignment = alignment_map[alignment]
        # Keep paragraph spacing inherited from the generated document style.
        # The split export preserves original spacing from the source DOCX; the
        # bilingual export should not introduce an extra hard-coded gap.

    def _apply_bilingual_run_format(self, run, run_meta=None, para_meta=None, muted=False):
        run_meta = run_meta or {}
        para_meta = para_meta or {}
        run.bold = bool(run_meta.get("bold", para_meta.get("bold", False)))
        run.italic = bool(run_meta.get("italic", False))
        run.underline = bool(run_meta.get("underline", False))
        if run_meta.get("font_name"):
            run.font.name = run_meta["font_name"]
        font_size = run_meta.get("font_size") or para_meta.get("font_size")
        if font_size:
            run.font.size = docx_handler.Pt(font_size)
        if run_meta.get("color"):
            try:
                run.font.color.rgb = docx_handler.RGBColor.from_string(run_meta["color"])
            except Exception:
                pass

    def _add_bilingual_source_paragraph(self, doc, line):
        meta = self._get_bilingual_line_meta(line)
        text = self._strip_bilingual_export_markers(line.source_text)
        if meta.get("is_empty") or line.is_empty:
            return
        if not text and not meta.get("images"):
            return

        para = doc.add_paragraph()
        self._set_bilingual_paragraph_format(para, meta)
        runs = meta.get("runs") or []
        if runs:
            for run_meta in runs:
                run_text = self._strip_bilingual_export_markers(run_meta.get("text") or "")
                if not run_text:
                    continue
                run = para.add_run(run_text)
                self._apply_bilingual_run_format(run, run_meta, meta, muted=True)
        else:
            run = para.add_run(text)
            self._apply_bilingual_run_format(run, {}, meta, muted=True)
        self._add_bilingual_images(doc, meta)

    def _parse_bilingual_format_segments(self, text):
        segments = []
        flags = {"bold": False, "italic": False, "underline": False}
        tag_re = re.compile(r"<(/?)(b|i|u)>", re.I)
        pos = 0
        for match in tag_re.finditer(text or ""):
            if match.start() > pos:
                segments.append({"text": text[pos:match.start()], **flags})
            enabled = match.group(1) != "/"
            tag = match.group(2).lower()
            if tag == "b":
                flags["bold"] = enabled
            elif tag == "i":
                flags["italic"] = enabled
            elif tag == "u":
                flags["underline"] = enabled
            pos = match.end()
        if pos < len(text or ""):
            segments.append({"text": text[pos:], **flags})
        return [seg for seg in segments if seg["text"]]

    def _add_bilingual_text_runs(self, para, text, meta, template_run=None, keep_formatting=True, muted=False):
        template_run = template_run or {}
        if keep_formatting:
            segments = self._parse_bilingual_format_segments(text)
        else:
            segments = []
        if not segments:
            segments = [{"text": re.sub(r"</?(?:b|i|u)>", "", text or "")}]
        for segment in segments:
            if not segment.get("text"):
                continue
            run = para.add_run(segment["text"])
            segment_meta = {
                **template_run,
                "bold": segment.get("bold", template_run.get("bold", meta.get("bold", False))),
                "italic": segment.get("italic", template_run.get("italic", False)),
                "underline": segment.get("underline", template_run.get("underline", False)),
            }
            self._apply_bilingual_run_format(run, segment_meta, meta, muted=muted)

    def _add_bilingual_translated_paragraph(self, doc, line):
        meta = self._get_bilingual_line_meta(line)
        text = self._strip_bilingual_export_markers(
            line.translated_text or line.source_text,
            keep_formatting=True,
        )
        if meta.get("is_empty") or line.is_empty:
            return
        if not text and not meta.get("images"):
            return

        para = doc.add_paragraph()
        self._set_bilingual_paragraph_format(para, meta)
        template_run = (meta.get("runs") or [{}])[0] if (meta.get("runs") or []) else {}
        self._add_bilingual_text_runs(para, text, meta, template_run, keep_formatting=True)
        self._add_bilingual_images(doc, meta)

    def _add_bilingual_images(self, doc, meta):
        for image in (meta or {}).get("images") or []:
            data_uri = image.get("data_uri") or ""
            if "," not in data_uri:
                continue
            try:
                image_bytes = base64.b64decode(data_uri.split(",", 1)[1])
                width_px = min(float(image.get("width") or 500), 500)
                doc.add_picture(
                    docx_handler.io.BytesIO(image_bytes),
                    width=docx_handler.Pt(width_px * 0.75),
                )
            except Exception:
                _logger.debug("Skipping image in bilingual export", exc_info=True)

    def _add_bilingual_table_pair(self, doc, line, translation_first=False):
        meta = self._get_bilingual_line_meta(line)
        cells_meta = meta.get("cells") or []
        source_cells = [
            self._strip_bilingual_export_markers((cell or {}).get("text") or "")
            for cell in cells_meta
        ] or [
            self._strip_bilingual_export_markers(cell)
            for cell in re.split(r"\s*\[CELL\]\s*", line.source_text or "")
        ]
        translated_cells = [
            self._strip_bilingual_export_markers(cell, keep_formatting=True)
            for cell in re.split(r"\s*\[CELL\]\s*", line.translated_text or line.source_text or "")
        ]
        cell_count = max(len(source_cells), len(translated_cells), 1)

        table_pairs = ((translated_cells, False), (source_cells, True)) if translation_first else (
            (source_cells, True),
            (translated_cells, False),
        )

        for cells, muted in table_pairs:
            table = doc.add_table(rows=1, cols=cell_count)
            table.style = "Table Grid"
            for idx in range(cell_count):
                cell_text = cells[idx] if idx < len(cells) else ""
                para = table.rows[0].cells[idx].paragraphs[0]
                cell_meta = cells_meta[idx] if idx < len(cells_meta) else {}
                if muted and cell_meta.get("runs"):
                    for run_meta in cell_meta.get("runs") or []:
                        run_text = self._strip_bilingual_export_markers(run_meta.get("text") or "")
                        if not run_text:
                            continue
                        run = para.add_run(run_text)
                        self._apply_bilingual_run_format(run, run_meta, meta, muted=False)
                else:
                    self._add_bilingual_text_runs(
                        para,
                        cell_text,
                        meta,
                        cell_meta,
                        keep_formatting=not muted,
                        muted=False,
                    )

    def _prepare_bilingual_rebuild_data(
        self,
        translation_first=False,
        table_translation_newline=True,
    ):
        """Prepare paragraph/table translations for source-template bilingual export."""
        self.ensure_one()
        paragraph_translations = {}
        split_buffer = {}
        table_translations = {}

        def _line_translation(line):
            return self._strip_bilingual_export_markers(
                line.translated_text or line.source_text or "",
                keep_formatting=True,
            )

        for line in self.line_ids.sorted("sequence"):
            if line.line_type == "image_ocr":
                continue
            meta = self._get_bilingual_line_meta(line)

            if meta.get("is_table_row") or line.line_type == "table_cell":
                table_idx = meta.get("table_index")
                row_idx = meta.get("row_index")
                if table_idx is None or row_idx is None:
                    continue
                translated_cells = [
                    self._strip_bilingual_export_markers(cell, keep_formatting=True)
                    for cell in re.split(r"\s*\[CELL\]\s*", line.translated_text or line.source_text or "")
                ]
                table_translations.setdefault(str(table_idx), {})[str(row_idx)] = {
                    "cells": translated_cells,
                }
                continue

            para_idx = meta.get("para_index")
            if para_idx is None:
                continue
            if meta.get("is_empty") or line.is_empty:
                continue

            translated = _line_translation(line)
            if not re.sub(r"</?(?:b|i|u)>", "", translated or "").strip():
                continue
            if meta.get("numbering_prefix"):
                translated = docx_handler.strip_numbering_prefix(
                    translated,
                    meta["numbering_prefix"],
                )

            if meta.get("is_split"):
                buf = split_buffer.setdefault(
                    para_idx,
                    {"parts": [], "total": meta.get("split_total", 1)},
                )
                buf["parts"].append((meta.get("split_index", len(buf["parts"])), translated))
                if len(buf["parts"]) >= buf["total"]:
                    ordered = [part for _idx, part in sorted(buf["parts"], key=lambda item: item[0])]
                    paragraph_translations[para_idx] = "".join(ordered)
                    split_buffer.pop(para_idx, None)
            else:
                paragraph_translations[para_idx] = translated

        for para_idx, buf in split_buffer.items():
            ordered = [part for _idx, part in sorted(buf["parts"], key=lambda item: item[0])]
            paragraph_translations[para_idx] = "".join(ordered)

        return {
            "paragraphs": paragraph_translations,
            "tables": table_translations,
            "translation_first": bool(translation_first),
            "table_translation_newline": bool(table_translation_newline),
        }

    def _finalize_bilingual_translation(
        self,
        translation_first=False,
        table_translation_newline=True,
    ):
        """Build a WYSIWYG-style bilingual DOCX matching the single-pane view."""
        self.ensure_one()
        if not docx_handler.Document:
            raise UserError(_("python-docx is required. Install with: pip install python-docx"))

        source_name = self.source_attachment_id.name or self.name or "document"
        base_name, _ext = os.path.splitext(source_name) if "." in source_name else (source_name, "")
        target_lang = self._get_target_lang_name()
        result_bytes = None

        if self.source_attachment_id and self.source_attachment_id.datas:
            original_content = base64.b64decode(self.source_attachment_id.datas)
            if source_name.lower().endswith(".doc") and not source_name.lower().endswith(".docx"):
                try:
                    original_content = docx_handler._convert_doc_to_docx_via_libreoffice(original_content)
                except Exception:
                    original_content = None

            if original_content:
                try:
                    result_bytes = docx_handler.rebuild_bilingual_docx_from_original(
                        original_content,
                        self._prepare_bilingual_rebuild_data(
                            translation_first=translation_first,
                            table_translation_newline=table_translation_newline,
                        ),
                    )
                except Exception as e:
                    _logger.warning(
                        "rebuild_bilingual_docx_from_original failed, falling back: %s",
                        e,
                    )

        if result_bytes is None:
            doc = docx_handler.Document()

            for line in self.line_ids.sorted("sequence"):
                if line.line_type == "image_ocr":
                    continue
                meta = self._get_bilingual_line_meta(line)
                if meta.get("is_table_row") or line.line_type == "table_cell":
                    self._add_bilingual_table_pair(
                        doc,
                        line,
                        translation_first=translation_first,
                    )
                else:
                    if translation_first:
                        self._add_bilingual_translated_paragraph(doc, line)
                        self._add_bilingual_source_paragraph(doc, line)
                    else:
                        self._add_bilingual_source_paragraph(doc, line)
                        self._add_bilingual_translated_paragraph(doc, line)

            buffer = docx_handler.io.BytesIO()
            doc.save(buffer)
            result_bytes = buffer.getvalue()
            docx_handler.validate_docx_package(result_bytes)

        attachment_vals = {
            "name": f"{base_name}_{target_lang}_bilingual.docx",
            "datas": base64.b64encode(result_bytes),
            "res_model": "project.project",
            "res_id": self.project_id.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }

        old_result = self.result_attachment_id
        if old_result:
            self.write({"result_attachment_id": False})
            old_result.unlink()

        result_attachment = self.env["ir.attachment"].create(attachment_vals)
        vals = {"result_attachment_id": result_attachment.id}
        if not self.line_ids.filtered(lambda l: l.state == "error"):
            vals["state"] = "done"
        self.write(vals)

    def action_retry_errors(self):
        """Retry translation for lines that previously failed."""
        self.ensure_one()
        error_lines = self.line_ids.filtered(lambda l: l.state == "error")
        if not error_lines:
            raise UserError(_("No failed paragraphs to retry."))

        error_lines.write({"state": "pending", "translated_text": False})
        self.action_start_translation()

    def action_reset_to_draft(self):
        """Reset the translation to draft state.

        If the translation has a source attachment but no lines (e.g. extraction
        failed previously), re-extract paragraphs automatically.
        """
        self.ensure_one()
        self.write({
            "state": "draft",
            "error_message": False,
        })
        # If there's a source file but no lines, re-extract
        if self.source_attachment_id and not self.line_ids:
            try:
                self.write({"state": "extracting"})
                self._extract_paragraphs()
                self.write({"state": "draft"})
            except Exception as e:
                _logger.exception(
                    "Re-extraction failed for %s",
                    self.source_attachment_id.name,
                )
                error_msg = str(e)
                if "python-docx" in error_msg or "No module named 'docx'" in error_msg:
                    error_msg = _(
                        "python-docx library is not installed. "
                        "Please install it with: pip install python-docx"
                    )
                self.write({
                    "state": "error",
                    "error_message": error_msg,
                })

    def action_download_result(self):
        """Return an action to download the translated document."""
        self.ensure_one()
        if not self.result_attachment_id:
            # Try to build the document (even partial)
            self._finalize_translation(force_rebuild=True)
        if not self.result_attachment_id:
            raise UserError(_("No translated document available."))

        return {
            "type": "ir.actions.act_url",
            "url": f"/web/content/{self.result_attachment_id.id}?download=true",
            "target": "new",
        }

    # =========================================================================
    # RPC methods for frontend
    # =========================================================================

    def _line_to_frontend_dict(self, line):
        style_meta = {}
        if line.style_metadata:
            try:
                style_meta = json.loads(line.style_metadata)
            except (json.JSONDecodeError, TypeError):
                pass

        translated = line.translated_text or ""
        reasoning = line.reasoning or ""
        if "<think>" in translated:
            translated, extra_reasoning = self._strip_think_tags(translated)
            if extra_reasoning and not reasoning:
                reasoning = extra_reasoning

        line_meta = dict(style_meta)
        ocr_result_parsed = None
        if line.line_type == "image_ocr":
            att_id = line_meta.get("image_attachment_id")
            if att_id:
                line_meta["image_preview_url"] = f"/web/image/{att_id}"
            line_meta.pop("image_data_uri", None)
            line_meta.pop("image_attachment_id", None)
            if line.image_ocr_result:
                try:
                    ocr_result_parsed = json.loads(line.image_ocr_result)
                    ocr_result_parsed = self._normalize_ocr_coordinates(
                        ocr_result_parsed
                    )
                except (json.JSONDecodeError, TypeError):
                    pass

        line_dict = {
            "id": line.id,
            "sequence": line.sequence,
            "source_text": line.source_text or "",
            "translated_text": translated,
            "state": line.state,
            "is_empty": line.is_empty,
            "estimated_tokens": line.estimated_tokens,
            "style_metadata": line_meta,
            "reasoning": reasoning,
            "line_type": line.line_type or "body",
        }
        if ocr_result_parsed:
            line_dict["image_ocr_result"] = ocr_result_parsed
        return line_dict

    def _get_frontend_lines_window(
        self,
        translation,
        line_offset=0,
        max_payload_bytes=FRONTEND_LINE_PAYLOAD_BYTES,
    ):
        try:
            line_offset = max(0, int(line_offset or 0))
        except (TypeError, ValueError):
            line_offset = 0
        try:
            max_payload_bytes = int(max_payload_bytes or FRONTEND_LINE_PAYLOAD_BYTES)
        except (TypeError, ValueError):
            max_payload_bytes = FRONTEND_LINE_PAYLOAD_BYTES
        max_payload_bytes = max(64 * 1024, min(max_payload_bytes, FRONTEND_LINE_PAYLOAD_BYTES))

        all_lines = translation.line_ids.sorted("sequence")
        total_count = len(all_lines)
        lines_data = []
        loaded_bytes = 0

        for line in all_lines[line_offset:]:
            line_dict = self._line_to_frontend_dict(line)
            line_bytes = len(
                json.dumps(line_dict, ensure_ascii=False, default=str).encode("utf-8")
            )
            if lines_data and loaded_bytes + line_bytes > max_payload_bytes:
                break
            lines_data.append(line_dict)
            loaded_bytes += line_bytes
            if loaded_bytes >= max_payload_bytes:
                break

        next_offset = line_offset + len(lines_data)
        return {
            "lines": lines_data,
            "line_window": {
                "offset": line_offset,
                "next_offset": next_offset,
                "loaded_count": next_offset,
                "returned_count": len(lines_data),
                "total_count": total_count,
                "loaded_bytes": loaded_bytes,
                "max_bytes": max_payload_bytes,
                "has_more": next_offset < total_count,
            },
        }

    @api.model
    def get_translation_lines(self, translation_id, line_offset=0, max_payload_bytes=None):
        translation = self.browse(translation_id)
        if not translation.exists():
            return {"error": "Translation not found"}
        return self._get_frontend_lines_window(
            translation,
            line_offset=line_offset,
            max_payload_bytes=max_payload_bytes or FRONTEND_LINE_PAYLOAD_BYTES,
        )

    @api.model
    def get_translation_data(self, translation_id, line_offset=0, max_payload_bytes=None):
        """Get translation data for the frontend view.

        Lines are returned in a byte-limited window so large documents do not
        freeze the browser by rendering the entire document at once.
        """
        translation = self.browse(translation_id)
        if not translation.exists():
            return {"error": "Translation not found"}

        line_window = self._get_frontend_lines_window(
            translation,
            line_offset=line_offset,
            max_payload_bytes=max_payload_bytes or FRONTEND_LINE_PAYLOAD_BYTES,
        )

        return {
            "id": translation.id,
            "name": translation.name,
            "state": translation.state,
            "source_lang": translation.source_lang,
            "target_lang": translation.target_lang,
            "provider_id": translation.provider_id.id,
            "provider_name": translation.provider_id.name,
            "model_id": translation.model_id.id,
            "model_name": translation.model_id.name,
            "project_id": translation.project_id.id,
            "project_name": translation.project_id.name,
            "knowledge_collection_id": translation.knowledge_collection_id.id if translation.knowledge_collection_id else False,
            "knowledge_collection_name": translation.knowledge_collection_id.name if translation.knowledge_collection_id else "",
            "source_filename": translation.source_filename,
            "result_filename": translation.result_filename,
            "is_image": bool(
                translation.source_attachment_id.mimetype in IMAGE_MIMETYPES
                or (translation.source_filename or "").lower().endswith(
                    (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp", ".tiff", ".tif", ".svg")
                )
            ) if translation.source_attachment_id else False,
            "progress": translation.progress,
            "total_lines": translation.total_lines,
            "translated_lines": translation.translated_lines,
            "error_message": translation.error_message,
            "header_text": translation.header_text or "",
            "footer_text": translation.footer_text or "",
            "lines": line_window["lines"],
            "line_window": line_window["line_window"],
        }

    @api.model
    def get_providers_and_models(self):
        """Get available providers and their chat models for the frontend.

        Returns:
            list[dict]: Provider data with nested models.
        """
        providers = self.env["llm.provider"].search([("active", "=", True)])
        result = []
        for provider in providers:
            models_data = []
            chat_models = self.env["llm.model"].search([
                ("provider_id", "=", provider.id),
                ("model_use", "in", ["chat", "multimodal"]),
            ])
            for model in chat_models:
                models_data.append({
                    "id": model.id,
                    "name": model.name,
                })
            result.append({
                "id": provider.id,
                "name": provider.name,
                "models": models_data,
            })
        return result

    @api.model
    def get_knowledge_collections(self):
        """Get available knowledge collections for glossary selection.

        Returns:
            list[dict]: Collection data.
        """
        collections = self.env["llm.knowledge.collection"].search([
            ("active", "=", True),
        ])
        return [
            {"id": c.id, "name": c.name}
            for c in collections
        ]

    @api.model
    def get_projects(self):
        """Get available projects, respecting privacy_visibility settings.

        Returns:
            list[dict]: Project data visible to the current user.
        """
        user = self.env.user
        projects = self.env["project.project"].search([
            ("active", "=", True),
        ])

        visible = []
        for p in projects:
            pv = p.privacy_visibility
            if pv in ('employees', 'portal') or not pv:
                # All internal users can see these
                visible.append(p)
            elif pv == 'followers':
                # Only followers / project members can see this project
                if user.partner_id.id in p.message_partner_ids.ids:
                    visible.append(p)
            else:
                visible.append(p)

        return [
            {
                "id": p.id,
                "name": p.name,
                "partner_id": p.partner_id.id if p.partner_id else False,
                "partner_name": p.partner_id.name if p.partner_id else "No Company"
            }
            for p in visible
        ]
