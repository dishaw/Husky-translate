"""Controllers for the LLM Translation module.

Provides JSON-RPC endpoints for the frontend translation view:
- Upload file and extract paragraphs
- Start/resume translation (with per-line progress updates via polling)
- Get translation status and data
"""

import base64
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Centralized extension -> MIME type mapping. Add new formats here only.
_EXT_MIMETYPE = {
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".svg": "image/svg+xml",
}

DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _get_mimetype(filename):
    """Return MIME type for a supported filename, or None."""
    ext = (filename or "").rsplit(".", 1)[-1].lower()
    return _EXT_MIMETYPE.get(f".{ext}")


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


def _estimate_base64_size(file_data):
    """Estimate original byte length without decoding the whole payload."""
    if not file_data:
        return 0
    payload = file_data.split(",", 1)[-1] if "," in file_data else file_data
    payload = "".join(str(payload).split())
    padding = payload.count("=")
    return max(0, (len(payload) * 3 // 4) - padding)


def _get_max_upload_bytes():
    """Return the configured upload guardrail in bytes."""
    try:
        value = request.env["ir.config_parameter"].sudo().get_param(
            "llm_translate.max_upload_bytes",
            str(DEFAULT_MAX_UPLOAD_BYTES),
        )
        max_bytes = int(value or DEFAULT_MAX_UPLOAD_BYTES)
    except (TypeError, ValueError):
        max_bytes = DEFAULT_MAX_UPLOAD_BYTES
    return max(1 * 1024 * 1024, min(max_bytes, 512 * 1024 * 1024))


def _oversize_error(filename, size, max_bytes=None):
    max_bytes = max_bytes or _get_max_upload_bytes()
    return (
        f"文件过大：{filename or 'uploaded file'} 为 {_format_file_size(size)}。"
        f"当前单文件上传/抽取上限是 {_format_file_size(max_bytes)}。"
        "请先拆分/压缩文件，或分批上传后再翻译。"
    )


class LLMTranslateController(http.Controller):

    def _prepare_create_vals(self, vals):
        create_vals = {
            "name": vals.get("name", "New Translation"),
            "provider_id": int(vals["provider_id"]),
            "model_id": int(vals["model_id"]),
            "source_lang": vals.get("source_lang", "en"),
            "target_lang": vals.get("target_lang", "zh"),
            "project_id": int(vals["project_id"]),
            "user_id": request.env.uid,
        }

        if vals.get("source_lang_custom"):
            create_vals["source_lang_custom"] = vals["source_lang_custom"]
        if vals.get("target_lang_custom"):
            create_vals["target_lang_custom"] = vals["target_lang_custom"]
        if vals.get("knowledge_collection_id"):
            create_vals["knowledge_collection_id"] = int(vals["knowledge_collection_id"])
        else:
            # Auto-link glossary knowledge collection if one exists
            GlossaryModel = request.env["llm.translation.glossary"].sudo()
            glossary_collection = GlossaryModel._get_or_create_knowledge_collection(
                create_vals.get("source_lang", "en"),
                create_vals.get("target_lang", "zh"),
            )
            if glossary_collection:
                create_vals["knowledge_collection_id"] = glossary_collection.id
        return create_vals

    def _create_attachment_from_upload(self, translation, upload_file, filename=None):
        filename = filename or getattr(upload_file, "filename", None) or "uploaded_file"
        mimetype = _get_mimetype(filename)
        if not mimetype:
            return None, {"error": "Unsupported file format."}

        file_content = upload_file.read()
        file_size = len(file_content or b"")
        max_upload_bytes = _get_max_upload_bytes()
        if file_size > max_upload_bytes:
            return None, {"error": _oversize_error(filename, file_size, max_upload_bytes)}

        attachment = request.env["ir.attachment"].sudo().create({
            "name": filename,
            "datas": base64.b64encode(file_content).decode("ascii"),
            "mimetype": mimetype,
            "res_model": "llm.translation",
            "res_id": translation.id,
        })
        return attachment, None

    @http.route("/llm_translate/upload", type="json", auth="public", methods=["POST"])
    def upload_file(self, translation_id, file_data, filename):
        """Upload a Word document and extract paragraphs.

        Args:
            translation_id: ID of the llm.translation record.
            file_data: Base64-encoded file content.
            filename: Original filename.

        Returns:
            dict: Updated translation data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        # Determine mimetype from extension
        mimetype = _get_mimetype(filename)
        if not mimetype:
            return {"error": "Unsupported file format."}
        file_size = _estimate_base64_size(file_data)
        max_upload_bytes = _get_max_upload_bytes()
        if file_size > max_upload_bytes:
            return {"error": _oversize_error(filename, file_size, max_upload_bytes)}

        # Create attachment (old source cleanup is handled by action_upload_file)
        attachment = request.env["ir.attachment"].sudo().create({
            "name": filename,
            "datas": file_data,
            "mimetype": mimetype,
            "res_model": "llm.translation",
            "res_id": translation.id,
        })

        # Process the file
        try:
            translation.action_upload_file(attachment.id)
            return request.env["llm.translation"].sudo().get_translation_data(translation.id)
        except Exception as e:
            _logger.exception("Upload failed")
            return {"error": str(e)}

    @http.route("/llm_translate/upload_binary", type="http", auth="public", methods=["POST"], csrf=False)
    def upload_binary(self, **post):
        """Upload a file through multipart/form-data with browser progress events."""
        translation = request.env["llm.translation"].sudo().browse(int(post.get("translation_id") or 0))
        if not translation.exists():
            return request.make_json_response({"error": "Translation not found"}, status=404)

        upload_file = request.httprequest.files.get("file")
        if not upload_file:
            return request.make_json_response({"error": "No file uploaded."}, status=400)

        try:
            attachment, error = self._create_attachment_from_upload(
                translation,
                upload_file,
                upload_file.filename,
            )
            if error:
                return request.make_json_response(error, status=400)
            translation.action_upload_file(attachment.id)
            return request.make_json_response(
                request.env["llm.translation"].sudo().get_translation_data(translation.id)
            )
        except Exception as e:
            _logger.exception("Binary upload failed")
            return request.make_json_response({"error": str(e)}, status=500)

    @http.route("/llm_translate/create", type="json", auth="public", methods=["POST"])
    def create_translation(self, vals, file_data=None, filename=None):
        """Create a new translation record, optionally uploading a file.

        Args:
            vals: Dict with field values (provider_id, model_id, etc.)
            file_data: Optional base64-encoded file content.
            filename: Original filename (required if file_data is provided).

        Returns:
            dict: Created translation data.
        """
        try:
            create_vals = self._prepare_create_vals(vals)

            upload_mimetype = None
            if file_data and filename:
                upload_mimetype = _get_mimetype(filename)
                if not upload_mimetype:
                    return {"error": "Unsupported file format."}
                file_size = _estimate_base64_size(file_data)
                max_upload_bytes = _get_max_upload_bytes()
                if file_size > max_upload_bytes:
                    return {"error": _oversize_error(filename, file_size, max_upload_bytes)}

            translation = request.env["llm.translation"].sudo().create(create_vals)

            # If a file was provided, upload and parse it immediately
            if file_data and filename:
                attachment = request.env["ir.attachment"].sudo().create({
                    "name": filename,
                    "datas": file_data,
                    "mimetype": upload_mimetype,
                    "res_model": "llm.translation",
                    "res_id": translation.id,
                })
                translation.action_upload_file(attachment.id)

            return request.env["llm.translation"].sudo().get_translation_data(translation.id)

        except Exception as e:
            _logger.exception("Failed to create translation")
            return {"error": str(e)}

    @http.route("/llm_translate/create_binary", type="http", auth="public", methods=["POST"], csrf=False)
    def create_translation_binary(self, **post):
        """Create a translation and upload a file through multipart/form-data."""
        try:
            vals = json.loads(post.get("vals") or "{}")
            upload_file = request.httprequest.files.get("file")
            if not upload_file:
                return request.make_json_response({"error": "No file uploaded."}, status=400)

            create_vals = self._prepare_create_vals(vals)
            translation = request.env["llm.translation"].sudo().create(create_vals)
            attachment, error = self._create_attachment_from_upload(
                translation,
                upload_file,
                upload_file.filename,
            )
            if error:
                translation.sudo().unlink()
                return request.make_json_response(error, status=400)

            translation.action_upload_file(attachment.id)
            return request.make_json_response(
                request.env["llm.translation"].sudo().get_translation_data(translation.id)
            )
        except Exception as e:
            _logger.exception("Failed to create binary translation")
            return request.make_json_response({"error": str(e)}, status=500)

    @http.route("/llm_translate/translate", type="json", auth="public", methods=["POST"])
    def start_translation(self, translation_id):
        """Start or resume translation (legacy - translates ALL at once).

        WARNING: This may timeout for large documents. Prefer /translate_next.

        Args:
            translation_id: ID of the llm.translation record.

        Returns:
            dict: Updated translation data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        try:
            translation.action_start_translation()
            return request.env["llm.translation"].sudo().get_translation_data(translation.id)
        except Exception as e:
            _logger.exception("Translation failed")
            return {"error": str(e)}

    @http.route("/llm_translate/translate_next", type="json", auth="public", methods=["POST"])
    def translate_next(self, translation_id):
        """Translate the next batch of pending paragraphs.

        The frontend should call this in a loop until 'finished' is True.
        Each call translates a batch of paragraphs (default 8) using a
        single LLM call with [SEP] markers, then returns immediately.

        Args:
            translation_id: ID of the llm.translation record.

        Returns:
            dict: {finished, translated_line_ids, progress, total_lines,
                   translated_lines, error, lines_data}
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found", "finished": True}

        try:
            result = translation.action_translate_next()
            # Attach updated line data for ALL translated lines in this batch
            lines_data = []
            line_ids = []
            seen_line_ids = set()
            for line_id in (
                (result.get("translated_line_ids") or [])
                + (result.get("error_line_ids") or [])
            ):
                if line_id in seen_line_ids:
                    continue
                seen_line_ids.add(line_id)
                line_ids.append(line_id)
            for line_id in line_ids:
                line = request.env["llm.translation.line"].sudo().browse(line_id)
                if not line.exists():
                    continue
                import json as _json
                style_meta = {}
                if line.style_metadata:
                    try:
                        style_meta = _json.loads(line.style_metadata)
                    except (ValueError, TypeError):
                        pass
                # Defensively strip <think> tags
                translated = line.translated_text or ""
                reasoning = line.reasoning or ""
                if "<think>" in translated:
                    import re
                    think_pattern = r'<think>(.*?)</think>'
                    reasoning_parts = re.findall(think_pattern, translated, re.DOTALL)
                    if reasoning_parts and not reasoning:
                        reasoning = "\n".join(p.strip() for p in reasoning_parts).strip()
                    translated = re.sub(think_pattern, '', translated, flags=re.DOTALL).strip()
                line_dict = {
                    "id": line.id,
                    "sequence": line.sequence,
                    "source_text": line.source_text or "",
                    "translated_text": translated,
                    "state": line.state,
                    "is_empty": line.is_empty,
                    "estimated_tokens": line.estimated_tokens,
                    "style_metadata": style_meta,
                    "reasoning": reasoning,
                    "line_type": line.line_type or "body",
                }
                # Include OCR result for image_ocr lines (normalised coords)
                if line.line_type == "image_ocr" and line.image_ocr_result:
                    try:
                        _ocr = _json.loads(line.image_ocr_result)
                        _ocr = translation._normalize_ocr_coordinates(_ocr)
                        line_dict["image_ocr_result"] = _ocr
                    except (ValueError, TypeError):
                        pass
                lines_data.append(line_dict)
            result["lines_data"] = lines_data
            # Backward compat: also set line_data to first item if any
            if lines_data:
                result["line_data"] = lines_data[0]
            return result
        except Exception as e:
            _logger.exception("translate_next failed")
            return {"error": str(e), "finished": False}

    @http.route("/llm_translate/reset", type="json", auth="public", methods=["POST"])
    def reset_translation(self, translation_id):
        """Reset a stuck translation back to draft state.

        Args:
            translation_id: ID of the llm.translation record.

        Returns:
            dict: Updated translation data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        try:
            translation.action_reset_to_draft()
            return request.env["llm.translation"].sudo().get_translation_data(translation.id)
        except Exception as e:
            _logger.exception("Reset failed")
            return {"error": str(e)}

    @http.route("/llm_translate/status", type="json", auth="public", methods=["POST"])
    def get_status(self, translation_id, line_offset=0, max_payload_bytes=None):
        """Get current translation status and data.

        Args:
            translation_id: ID of the llm.translation record.

        Returns:
            dict: Translation data.
        """
        return request.env["llm.translation"].sudo().get_translation_data(
            int(translation_id),
            line_offset=line_offset,
            max_payload_bytes=max_payload_bytes,
        )

    @http.route("/llm_translate/lines", type="json", auth="public", methods=["POST"])
    def get_translation_lines(self, translation_id, line_offset=0, max_payload_bytes=None):
        """Load the next byte-limited window of translation lines."""
        return request.env["llm.translation"].sudo().get_translation_lines(
            int(translation_id),
            line_offset=line_offset,
            max_payload_bytes=max_payload_bytes,
        )

    @http.route("/llm_translate/update_line", type="json", auth="public", methods=["POST"])
    def update_line(self, line_id, translated_text=None, source_text=None):
        """Manually update a line's source or translated text.

        Args:
            line_id: ID of the llm.translation.line record.
            translated_text: New translated text (optional).
            source_text: New source text (optional).

        Returns:
            dict: Success status with auto-learned entries.
        """
        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        old_translated = line.translated_text or ""
        vals = {}
        if translated_text is not None:
            vals["translated_text"] = translated_text
            vals["state"] = "done"
        if source_text is not None:
            vals["source_text"] = source_text
        if vals:
            line.write(vals)

        # Auto-learn glossary from manual translation edits
        learned = []
        if translated_text is not None and old_translated and old_translated != translated_text:
            try:
                translation = line.translation_id
                source_lang = translation.source_lang or "en"
                target_lang = translation.target_lang or "zh"
                # Pass the provider/model so the glossary can call AI analysis
                provider = translation.provider_id or False
                model = translation.model_id or False
                GlossaryModel = request.env["llm.translation.glossary"]
                learned = GlossaryModel.learn_from_edit(
                    line.source_text or "",
                    old_translated,
                    translated_text,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    provider=provider,
                    model=model,
                    is_guest=bool(getattr(request.env.user, "is_temp_user", False)),
                )
                if learned:
                    _logger.info(
                        "Auto-learned %d glossary entries from line %s",
                        len(learned), line_id,
                    )
            except Exception as e:
                _logger.warning("Glossary auto-learn failed: %s", e)

        return {
            "success": True,
            "learned": learned,
        }

    @http.route("/llm_translate/retry_errors", type="json", auth="public", methods=["POST"])
    def retry_errors(self, translation_id):
        """Reset all error lines to pending so they can be re-translated.

        Args:
            translation_id: ID of the translation record.

        Returns:
            dict: Success status.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        error_lines = translation.line_ids.filtered(lambda l: l.state == "error")
        if error_lines:
            error_lines.write({"state": "pending", "translated_text": False})
            translation.write({"state": "translating", "error_message": False})

        return {"success": True, "reset_count": len(error_lines)}

    @http.route("/llm_translate/retry_line", type="json", auth="public", methods=["POST"])
    def retry_line(self, translation_id, line_id):
        """Retry translation for a single line.

        Args:
            translation_id: ID of the translation record.
            line_id: ID of the line to retry.

        Returns:
            dict: Updated translation data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        line.write({"state": "pending", "translated_text": False, "reasoning": False})

        try:
            translation._translate_lines(line)
            return request.env["llm.translation"].sudo().get_translation_data(translation.id)
        except Exception as e:
            return {"error": str(e)}

    @http.route("/llm_translate/rebuild", type="json", auth="public", methods=["POST"])
    def rebuild_document(self, translation_id, export_mode="translated"):
        """Rebuild the translated document (e.g., after manual edits).

        Args:
            translation_id: ID of the translation record.

        Returns:
            dict: Updated translation data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        # Check if user is a temp user - block download/rebuild
        user = request.env.user
        is_temp = getattr(user, 'is_temp_user', False)
        if is_temp:
            return {"error": "请登录后下载翻译文件"}

        try:
            if export_mode == "bilingual":
                translation._finalize_bilingual_translation()
            else:
                translation._finalize_translation(force_rebuild=True)
            return request.env["llm.translation"].sudo().get_translation_data(translation.id)
        except Exception as e:
            return {"error": str(e)}

    @http.route("/llm_translate/delete", type="json", auth="public", methods=["POST"])
    def delete_translation(self, translation_id):
        """Delete a translation record along with its attachments and lines.

        Args:
            translation_id: ID of the llm.translation record.

        Returns:
            dict: Success status.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        try:
            # unlink() handles all attachment cleanup (source, result, OCR images)
            # and line_ids are deleted via cascade
            translation.unlink()

            return {"success": True}
        except Exception as e:
            _logger.exception("Delete failed")
            return {"error": str(e)}

    @http.route("/llm_translate/retranslate_line", type="json", auth="public", methods=["POST"])
    def retranslate_line(self, translation_id, line_id):
        """Re-translate a single line (reset + translate immediately).

        Args:
            translation_id: ID of the translation record.
            line_id: ID of the line to re-translate.

        Returns:
            dict: Updated line data.
        """
        translation = request.env["llm.translation"].sudo().browse(int(translation_id))
        if not translation.exists():
            return {"error": "Translation not found"}

        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        try:
            import json as _json
            import re

            # Parse the line's style_metadata
            style_meta = {}
            if line.style_metadata:
                try:
                    style_meta = _json.loads(line.style_metadata)
                except (ValueError, TypeError):
                    pass

            # ── Image-only paragraph: re-translate all image_ocr sibling lines ──
            is_image_para = not line.source_text and not line.is_empty
            if is_image_para:
                para_index = style_meta.get("para_index")
                # Find all image_ocr lines for this translation & para_index
                all_ocr_lines = request.env["llm.translation.line"].sudo().search([
                    ("translation_id", "=", translation.id),
                    ("line_type", "=", "image_ocr"),
                ])
                matching_ocr_lines = request.env["llm.translation.line"].sudo()
                for ocr_line in all_ocr_lines:
                    try:
                        ocr_meta = _json.loads(ocr_line.style_metadata or "{}")
                    except (ValueError, TypeError):
                        ocr_meta = {}
                    if ocr_meta.get("para_index") == para_index:
                        matching_ocr_lines |= ocr_line

                if matching_ocr_lines:
                    # Reset all OCR lines to pending
                    matching_ocr_lines.write({
                        "state": "pending",
                        "image_ocr_result": False,
                    })
                    # Translate each OCR line
                    for ocr_line in matching_ocr_lines:
                        translation._translate_lines(ocr_line)
                    request.env.cr.commit()

                # Build response with updated OCR line data
                ocr_lines_data = []
                for ocr_line in matching_ocr_lines:
                    ocr_meta = {}
                    if ocr_line.style_metadata:
                        try:
                            ocr_meta = _json.loads(ocr_line.style_metadata)
                        except (ValueError, TypeError):
                            pass
                    ocr_result_parsed = None
                    if ocr_line.image_ocr_result:
                        try:
                            ocr_result_parsed = _json.loads(ocr_line.image_ocr_result)
                            ocr_result_parsed = translation._normalize_ocr_coordinates(
                                ocr_result_parsed
                            )
                        except Exception:
                            pass
                    ocr_lines_data.append({
                        "id": ocr_line.id,
                        "state": ocr_line.state,
                        "line_type": "image_ocr",
                        "style_metadata": ocr_meta,
                        "image_ocr_result": ocr_result_parsed,
                    })
                return {
                    "success": True,
                    "is_image_para": True,
                    "ocr_lines_data": ocr_lines_data,
                }

            # ── Normal text line ──
            line.write({"state": "pending", "translated_text": False, "reasoning": False})
            translation._translate_lines(line)
            request.env.cr.commit()

            translated = line.translated_text or ""
            reasoning = line.reasoning or ""
            if "<think>" in translated:
                think_pattern = r'<think>(.*?)</think>'
                rp = re.findall(think_pattern, translated, re.DOTALL)
                if rp and not reasoning:
                    reasoning = "\n".join(p.strip() for p in rp).strip()
                translated = re.sub(think_pattern, '', translated, flags=re.DOTALL).strip()
            return {
                "success": True,
                "line_data": {
                    "id": line.id,
                    "sequence": line.sequence,
                    "source_text": line.source_text or "",
                    "translated_text": translated,
                    "state": line.state,
                    "is_empty": line.is_empty,
                    "style_metadata": style_meta,
                    "reasoning": reasoning,
                    "line_type": line.line_type or "body",
                },
            }
        except Exception as e:
            _logger.exception("Retranslate failed")
            return {"error": str(e)}

    @http.route("/llm_translate/providers", type="json", auth="public", methods=["POST"])
    def get_providers(self):
        """Get available providers and models."""
        return request.env["llm.translation"].sudo().get_providers_and_models()

    @http.route("/llm_translate/collections", type="json", auth="public", methods=["POST"])
    def get_collections(self):
        """Get available knowledge collections."""
        return request.env["llm.translation"].sudo().get_knowledge_collections()

    @http.route("/llm_translate/projects", type="json", auth="public", methods=["POST"])
    def get_projects(self):
        """Get available projects."""
        return request.env["llm.translation"].sudo().get_projects()

    @http.route("/llm_translate/list", type="json", auth="public", methods=["POST"])
    def list_translations(self, project_id=None):
        """List translation records for the current user.

        Portal/temp users see only their own records (via RPC, bypassing ORM ACL).
        """
        domain = [("user_id", "=", request.env.uid)]
        if project_id:
            domain.append(("project_id", "=", int(project_id)))

        translations = request.env["llm.translation"].sudo().search(
            domain, order="create_date desc", limit=50
        )
        result = []
        for t in translations:
            result.append({
                "id": t.id,
                "name": t.name,
                "state": t.state,
                "source_lang": t.source_lang,
                "target_lang": t.target_lang,
                "provider_id": t.provider_id.id if t.provider_id else False,
                "model_id": t.model_id.id if t.model_id else False,
                "project_id": t.project_id.id if t.project_id else False,
                "progress": t.progress,
                "source_filename": t.source_filename,
                "source_file_size": t.source_attachment_id.file_size if t.source_attachment_id else 0,
                "source_file_size_display": t.source_file_size_display,
                "create_date": t.create_date.isoformat() if t.create_date else "",
            })
        return result

    # =========================================================================
    # GLOSSARY / TRANSLATION MEMORY
    # =========================================================================

    @http.route("/llm_translate/glossary/count", type="json", auth="public", methods=["POST"])
    def glossary_count(self, source_lang=None, target_lang=None):
        """Get the count of glossary entries for a language pair.

        Returns:
            dict: {count: int}
        """
        domain = [("active", "=", True)]
        if source_lang:
            domain.append(("source_lang", "=", source_lang))
        if target_lang:
            domain.append(("target_lang", "=", target_lang))
        count = request.env["llm.translation.glossary"].sudo().search_count(domain)
        return {"count": count}

    @http.route("/llm_translate/glossary/list", type="json", auth="public", methods=["POST"])
    def glossary_list(self, source_lang=None, target_lang=None):
        """Get glossary entries filtered by language pair and creator (user vs guest).

        Returns:
            list[dict]: Glossary entries visible to the current user.
        """
        domain = [("active", "=", True)]
        if source_lang:
            domain.append(("source_lang", "=", source_lang))
        if target_lang:
            domain.append(("target_lang", "=", target_lang))
        
        # Filter by current user's temp status
        is_guest = bool(getattr(request.env.user, "is_temp_user", False))
        if is_guest:
            domain.append(("create_uid.is_temp_user", "=", True))
        else:
            domain.append(("create_uid.is_temp_user", "=", False))

        entries = request.env["llm.translation.glossary"].sudo().search(
            domain, order="create_date desc", limit=500
        )
        return [{
            "id": e.id,
            "source_text": e.source_text or e.source_phrase or "",
            "translated_text": e.translated_text or e.new_phrase or "",
            "source_phrase": e.source_phrase or "",
            "old_phrase": e.old_phrase or "",
            "new_phrase": e.new_phrase or "",
            "context_source": e.context_source or "",
            "old_translated": e.old_translated or "",
            "new_translated": e.new_translated or "",
            "ai_analysis": e.ai_analysis or "",
            "source_lang": e.source_lang,
            "target_lang": e.target_lang,
            "frequency": e.frequency,
            "origin": e.origin,
            "create_uid_name": e.create_uid.name if e.create_uid else "",
            "create_date": e.create_date.isoformat() if e.create_date else "",
        } for e in entries]

    @http.route("/llm_translate/glossary/add", type="json", auth="public", methods=["POST"])
    def glossary_add(self, source_text, translated_text, source_lang="en", target_lang="zh"):
        """Manually add a glossary entry.

        Returns:
            dict: Created entry data.
        """
        GlossaryModel = request.env["llm.translation.glossary"].sudo()
        entry = GlossaryModel._upsert_entry(
            source_text, translated_text, source_lang, target_lang
        )
        if entry:
            entry.write({"origin": "manual"})
            return {
                "success": True,
                "entry": {
                    "id": entry.id,
                    "source_text": entry.source_text or entry.source_phrase or "",
                    "translated_text": entry.translated_text or entry.new_phrase or "",
                    "source_phrase": entry.source_phrase or "",
                    "new_phrase": entry.new_phrase or "",
                    "frequency": entry.frequency,
                },
            }
        return {"error": "Failed to add glossary entry"}

    @http.route("/llm_translate/glossary/delete", type="json", auth="public", methods=["POST"])
    def glossary_delete(self, entry_id):
        """Delete a glossary entry.

        Returns:
            dict: Success status.
        """
        entry = request.env["llm.translation.glossary"].sudo().browse(int(entry_id))
        if entry.exists():
            # Remove from knowledge collection first
            request.env["llm.translation.glossary"]._remove_entry_from_knowledge(entry)
            entry.unlink()
            return {"success": True}
        return {"error": "Entry not found"}

    @http.route("/llm_translate/glossary/update", type="json", auth="public", methods=["POST"])
    def glossary_update(self, entry_id, translated_text):
        """Update a glossary entry's translation.

        Returns:
            dict: Success status.
        """
        entry = request.env["llm.translation.glossary"].sudo().browse(int(entry_id))
        if not entry.exists():
            return {"error": "Entry not found"}
        entry.write({"translated_text": translated_text, "new_phrase": translated_text})
        # Sync updated entry to knowledge collection
        request.env["llm.translation.glossary"]._sync_entry_to_knowledge(entry)
        return {"success": True}

    @http.route("/llm_translate/ocr_block/update", type="json", auth="public", methods=["POST"])
    def update_ocr_block(self, line_id, block_index, x_pct=None, y_pct=None, font_size=None, w_pct=None, h_pct=None):
        """Update the position, size, or font size of a single OCR text block.

        Persists changes into the image_ocr_result JSON of the translation line.

        Args:
            line_id: ID of the llm.translation.line record (image_ocr type).
            block_index: Index of the text block within text_blocks array.
            x_pct: New x_pct value (optional).
            y_pct: New y_pct value (optional).
            font_size: New font_size_px value (optional).
            w_pct: New w_pct value (optional).
            h_pct: New h_pct value (optional).

        Returns:
            dict: Success status.
        """
        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        if not line.image_ocr_result:
            return {"error": "No OCR result on this line"}

        try:
            ocr_data = json.loads(line.image_ocr_result)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid OCR result JSON"}

        blocks = ocr_data.get("text_blocks", [])
        if block_index < 0 or block_index >= len(blocks):
            return {"error": f"Block index {block_index} out of range (0-{len(blocks)-1})"}

        block = blocks[block_index]
        if x_pct is not None:
            block["x_pct"] = max(0, min(100, float(x_pct)))
        if y_pct is not None:
            block["y_pct"] = max(0, min(100, float(y_pct)))
        if font_size is not None:
            block["font_size_px"] = max(5, min(48, int(font_size)))
        if w_pct is not None:
            block["w_pct"] = max(1, min(100, float(w_pct)))
        if h_pct is not None:
            block["h_pct"] = max(1, min(100, float(h_pct)))

        _logger.info(
            "update_ocr_block: line_id=%s block_index=%s x_pct=%s y_pct=%s font_size=%s w_pct=%s h_pct=%s",
            line_id, block_index, x_pct, y_pct, font_size, w_pct, h_pct,
        )
        line.write({"image_ocr_result": json.dumps(ocr_data)})
        return {"success": True}

    @http.route("/llm_translate/ocr_block/delete", type="json", auth="public", methods=["POST"])
    def delete_ocr_block(self, line_id, block_index):
        """Delete a single OCR text block from image_ocr_result.

        Args:
            line_id: ID of the llm.translation.line record (image_ocr type).
            block_index: Index of the text block within text_blocks array.

        Returns:
            dict: Success status.
        """
        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        if not line.image_ocr_result:
            return {"error": "No OCR result on this line"}

        try:
            ocr_data = json.loads(line.image_ocr_result)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid OCR result JSON"}

        blocks = ocr_data.get("text_blocks", [])
        if block_index < 0 or block_index >= len(blocks):
            return {"error": f"Block index {block_index} out of range (0-{len(blocks)-1})"}

        del blocks[block_index]
        ocr_data["text_blocks"] = blocks
        line.write({"image_ocr_result": json.dumps(ocr_data)})
        return {"success": True}

    @http.route("/llm_translate/ocr_block/update_text", type="json", auth="public", methods=["POST"])
    def update_ocr_block_text(self, line_id, block_index, translated_text):
        """Update the translated text of a single OCR text block.

        Also triggers glossary auto-learning from the edit.

        Args:
            line_id: ID of the llm.translation.line record (image_ocr type).
            block_index: Index of the text block within text_blocks array.
            translated_text: New translated text content.

        Returns:
            dict: Success status with auto-learned glossary entries.
        """
        line = request.env["llm.translation.line"].sudo().browse(int(line_id))
        if not line.exists():
            return {"error": "Line not found"}

        if not line.image_ocr_result:
            return {"error": "No OCR result on this line"}

        try:
            ocr_data = json.loads(line.image_ocr_result)
        except (json.JSONDecodeError, TypeError):
            return {"error": "Invalid OCR result JSON"}

        blocks = ocr_data.get("text_blocks", [])
        if block_index < 0 or block_index >= len(blocks):
            return {"error": f"Block index {block_index} out of range (0-{len(blocks)-1})"}

        block = blocks[block_index]
        old_translated = block.get("translated", "")
        new_translated = (translated_text or "").strip()

        if old_translated == new_translated:
            return {"success": True, "learned": []}

        block["translated"] = new_translated
        line.write({"image_ocr_result": json.dumps(ocr_data)})

        # Auto-learn glossary from manual translation edits
        learned = []
        if old_translated and new_translated:
            try:
                translation = line.translation_id
                source_lang = translation.source_lang or "en"
                target_lang = translation.target_lang or "zh"
                provider = translation.provider_id or False
                model = translation.model_id or False
                GlossaryModel = request.env["llm.translation.glossary"]
                learned = GlossaryModel.learn_from_edit(
                    block.get("original", ""),
                    old_translated,
                    new_translated,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    provider=provider,
                    model=model,
                    is_guest=bool(getattr(request.env.user, "is_temp_user", False)),
                )
                if learned:
                    _logger.info(
                        "Auto-learned %d glossary entries from OCR block edit (line %s, block %s)",
                        len(learned), line_id, block_index,
                    )
            except Exception as e:
                _logger.warning("Glossary auto-learn from OCR block failed: %s", e)

        return {"success": True, "learned": learned}

    @http.route("/llm_translate/check_user", type="json", auth="public", methods=["POST"])
    def check_user_status(self):
        """Check if the current user is a temp/guest user.

        Returns info needed by the frontend to show guest notice bar
        and restrict downloads.
        """
        user = request.env.user
        is_temp = getattr(user, "is_temp_user", False)
        return {
            "is_temp_user": is_temp,
            "user_name": user.name,
            "user_login": user.login,
            "login_url": "/web/login",
            "wechat_login_url": "/auth_wechat/login",
        }
