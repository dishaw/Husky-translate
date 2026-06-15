import logging

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class LLMTranslationLine(models.Model):
    _name = "llm.translation.line"
    _description = "LLM Translation Line (Paragraph)"
    _order = "sequence, id"

    translation_id = fields.Many2one(
        "llm.translation",
        string="Translation",
        required=True,
        ondelete="cascade",
        index=True,
    )
    sequence = fields.Integer(
        string="Sequence",
        default=10,
        index=True,
    )
    source_text = fields.Text(
        string="Source Text",
        readonly=True,
    )
    translated_text = fields.Text(
        string="Translated Text",
    )
    state = fields.Selection(
        [
            ("pending", "Pending"),
            ("translating", "Translating"),
            ("done", "Done"),
            ("error", "Error"),
        ],
        string="Status",
        default="pending",
        required=True,
        index=True,
    )
    is_empty = fields.Boolean(
        string="Empty Paragraph",
        default=False,
        help="Empty paragraphs are preserved for document structure.",
    )
    estimated_tokens = fields.Integer(
        string="Estimated Tokens",
        default=0,
        help="Estimated token count for this paragraph.",
    )
    style_metadata = fields.Text(
        string="Style Metadata (JSON)",
        help="JSON-encoded paragraph style information for document reconstruction.",
    )
    reasoning = fields.Text(
        string="LLM Reasoning",
        help="Internal reasoning/thinking from LLM (stripped from translated text).",
    )
    image_ocr_result = fields.Text(
        string="Image OCR Result (JSON)",
        help="JSON-encoded OCR text blocks with bounding box positions for image translation.",
    )
    line_type = fields.Selection(
        [
            ("header", "Header"),
            ("footer", "Footer"),
            ("body", "Body"),
            ("textbox", "Textbox"),
            ("table_cell", "Table Cell"),
            ("image_ocr", "Image OCR"),
        ],
        string="Line Type",
        default="body",
        required=True,
        index=True,
        help="Type of line: header/footer for page headers/footers, body for regular paragraphs, textbox for floating text boxes, table_cell for table cell content.",
    )

    @api.model
    def _register_hook(self):
        """Ensure new columns exist even without module upgrade (-u)."""
        super()._register_hook()
        cr = self.env.cr
        # Auto-create columns for llm_translation_line
        cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'llm_translation_line' AND column_name = 'reasoning'"
        )
        if not cr.fetchone():
            cr.execute("ALTER TABLE llm_translation_line ADD COLUMN reasoning text")
            _logger.info("Auto-created 'reasoning' column in llm_translation_line")
        # Auto-create line_type column
        cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'llm_translation_line' AND column_name = 'line_type'"
        )
        if not cr.fetchone():
            cr.execute(
                "ALTER TABLE llm_translation_line "
                "ADD COLUMN line_type varchar NOT NULL DEFAULT 'body'"
            )
            _logger.info("Auto-created 'line_type' column in llm_translation_line")
        # Auto-create image_ocr_result column
        cr.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'llm_translation_line' AND column_name = 'image_ocr_result'"
        )
        if not cr.fetchone():
            cr.execute("ALTER TABLE llm_translation_line ADD COLUMN image_ocr_result text")
            _logger.info("Auto-created 'image_ocr_result' column in llm_translation_line")
        # Auto-create columns for llm_translation
        for col in ('header_text', 'footer_text'):
            cr.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'llm_translation' AND column_name = %s", (col,)
            )
            if not cr.fetchone():
                cr.execute(f"ALTER TABLE llm_translation ADD COLUMN {col} text")
                _logger.info("Auto-created '%s' column in llm_translation", col)

    @api.model_create_multi
    def create(self, vals_list):
        """Override to set done state for empty paragraphs."""
        for vals in vals_list:
            if vals.get("is_empty"):
                vals["state"] = "done"
        return super().create(vals_list)
