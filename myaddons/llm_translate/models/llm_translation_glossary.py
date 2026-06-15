"""Translation Glossary / Translation Memory Model.

Stores full edit context (source paragraph, original translation, corrected
translation) plus AI-analyzed source_phrase → new_phrase mappings learned
from manual user edits.  These are used to ensure consistent translation
of recurring terms, phrases, and sentences.
"""

import json
import logging
import re

from odoo import _, api, fields, models

_logger = logging.getLogger(__name__)


class LLMTranslationGlossary(models.Model):
    _name = "llm.translation.glossary"
    _description = "LLM Translation Glossary Entry"
    _order = "create_date desc"
    _rec_name = "source_phrase"

    # ── Full context of the edit ─────────────────────────────────────────
    context_source = fields.Text(
        string="Source Paragraph",
        help="Full source-language paragraph that was being translated.",
    )
    old_translated = fields.Text(
        string="Original Translation",
        help="Machine translation before the user's edit.",
    )
    new_translated = fields.Text(
        string="Modified Translation",
        help="Translation after the user's manual correction.",
    )

    # ── AI-extracted phrase-level mapping ─────────────────────────────────
    source_phrase = fields.Char(
        string="Source Phrase",
        required=True,
        index=True,
        help="The specific source-language word/phrase identified by AI.",
    )
    old_phrase = fields.Char(
        string="Old Translation Phrase",
        help="How this phrase was originally translated (before edit).",
    )
    new_phrase = fields.Char(
        string="New Translation Phrase",
        required=True,
        help="User-corrected translation for this phrase.",
    )
    ai_analysis = fields.Text(
        string="AI Analysis",
        help="Full AI reasoning about what was changed and why.",
    )

    # ── Legacy / compatibility (kept for manual entries & matching) ───────
    source_text = fields.Char(
        string="Source Text",
        index=True,
        help="(Legacy) Original source segment. For manual entries equals source_phrase.",
    )
    translated_text = fields.Char(
        string="Translated Text",
        help="(Legacy) Preferred translation. For manual entries equals new_phrase.",
    )

    # ── Metadata ─────────────────────────────────────────────────────────
    source_lang = fields.Char(
        string="Source Language",
        required=True,
        default="en",
        index=True,
    )
    target_lang = fields.Char(
        string="Target Language",
        required=True,
        default="zh",
        index=True,
    )
    frequency = fields.Integer(
        string="Times Used",
        default=1,
    )
    origin = fields.Selection(
        [
            ("auto", "Auto-learned"),
            ("manual", "Manually Added"),
        ],
        string="Origin",
        default="auto",
    )
    active = fields.Boolean(default=True)

    # Link to knowledge collection for vector search
    knowledge_collection_id = fields.Many2one(
        "llm.knowledge.collection",
        string="Knowledge Collection",
        ondelete="set null",
        help="Auto-managed knowledge collection for this glossary's language pair.",
    )

    _sql_constraints = [
        (
            "unique_source_phrase_lang_pair",
            "UNIQUE(source_phrase, source_lang, target_lang)",
            "A glossary entry with the same source phrase and language pair already exists.",
        ),
    ]

    # =====================================================================
    # REGISTER HOOK — auto-create / migrate table
    # =====================================================================

    @api.model
    def _register_hook(self):
        """Ensure table and columns exist without requiring module upgrade."""
        super()._register_hook()
        cr = self.env.cr
        cr.execute("""
            SELECT 1 FROM information_schema.tables
            WHERE table_name = 'llm_translation_glossary'
        """)
        if not cr.fetchone():
            # Fresh install — create the table from scratch
            cr.execute("""
                CREATE TABLE IF NOT EXISTS llm_translation_glossary (
                    id SERIAL PRIMARY KEY,
                    context_source TEXT,
                    old_translated TEXT,
                    new_translated TEXT,
                    source_phrase VARCHAR NOT NULL DEFAULT '',
                    old_phrase VARCHAR,
                    new_phrase VARCHAR NOT NULL DEFAULT '',
                    ai_analysis TEXT,
                    source_text VARCHAR,
                    translated_text VARCHAR,
                    source_lang VARCHAR NOT NULL DEFAULT 'en',
                    target_lang VARCHAR NOT NULL DEFAULT 'zh',
                    frequency INTEGER NOT NULL DEFAULT 1,
                    origin VARCHAR NOT NULL DEFAULT 'auto',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    knowledge_collection_id INTEGER,
                    create_uid INTEGER REFERENCES res_users(id),
                    write_uid INTEGER REFERENCES res_users(id),
                    create_date TIMESTAMP DEFAULT NOW(),
                    write_date TIMESTAMP DEFAULT NOW()
                )
            """)
            cr.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                    llm_translation_glossary_unique_phrase_lang
                ON llm_translation_glossary (source_phrase, source_lang, target_lang)
            """)
            cr.execute("""
                CREATE INDEX IF NOT EXISTS
                    llm_translation_glossary_lang_idx
                ON llm_translation_glossary (source_lang, target_lang)
            """)
        else:
            # Table exists — add new columns if missing (migration)
            new_cols = {
                "context_source": "TEXT",
                "old_translated": "TEXT",
                "new_translated": "TEXT",
                "source_phrase": "VARCHAR NOT NULL DEFAULT ''",
                "old_phrase": "VARCHAR",
                "new_phrase": "VARCHAR NOT NULL DEFAULT ''",
                "ai_analysis": "TEXT",
                "knowledge_collection_id": "INTEGER REFERENCES llm_knowledge_collection(id)",
            }
            for col, col_type in new_cols.items():
                cr.execute("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'llm_translation_glossary'
                      AND column_name = %s
                """, (col,))
                if not cr.fetchone():
                    cr.execute(
                        f"ALTER TABLE llm_translation_glossary ADD COLUMN {col} {col_type}"
                    )
                    _logger.info("Added column %s to llm_translation_glossary", col)

            # Migrate old data: copy source_text → source_phrase if empty
            cr.execute("""
                UPDATE llm_translation_glossary
                SET source_phrase = COALESCE(source_text, ''),
                    new_phrase    = COALESCE(translated_text, '')
                WHERE (source_phrase IS NULL OR source_phrase = '')
                  AND source_text IS NOT NULL AND source_text != ''
            """)

            # Drop old unique constraint/index and create new one (ignore errors)
            # Use savepoints to prevent transaction abort on failure
            try:
                with cr.savepoint():
                    cr.execute("""
                        ALTER TABLE llm_translation_glossary
                        DROP CONSTRAINT IF EXISTS
                            llm_translation_glossary_unique_source_lang_pair
                    """)
            except Exception:
                pass
            try:
                with cr.savepoint():
                    cr.execute("""
                        DROP INDEX IF EXISTS
                            llm_translation_glossary_unique_source_lang_pair
                    """)
            except Exception:
                pass
            try:
                cr.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        llm_translation_glossary_unique_phrase_lang
                    ON llm_translation_glossary (source_phrase, source_lang, target_lang)
                """)
            except Exception:
                pass  # index may already exist

    # =========================================================================
    # KNOWLEDGE COLLECTION SYNC
    # =========================================================================

    def _get_or_create_knowledge_collection(self, source_lang, target_lang):
        """Get or create a dedicated knowledge collection for the glossary language pair.

        This creates a collection named 'Translation Glossary (XX→YY)' that
        stores glossary entries as knowledge chunks for vector search during
        translation.

        Returns:
            llm.knowledge.collection record or False if prerequisites missing.
        """
        collection_name = f"Translation Glossary ({source_lang}→{target_lang})"

        CollectionModel = self.env["llm.knowledge.collection"].sudo()
        existing = CollectionModel.search([
            ("name", "=", collection_name),
            ("active", "=", True),
        ], limit=1)

        if existing:
            return existing

        # Find an embedding model
        embedding_model = self.env["llm.model"].sudo().search([
            ("model_use", "=", "embedding"),
        ], limit=1)
        if not embedding_model:
            _logger.warning(
                "No embedding model available, cannot create knowledge collection "
                "for glossary (%s→%s)", source_lang, target_lang,
            )
            return False

        # Find a vector store
        store = self.env["llm.store"].sudo().search([], limit=1)
        if not store:
            _logger.warning(
                "No vector store available, cannot create knowledge collection "
                "for glossary (%s→%s)", source_lang, target_lang,
            )
            return False

        try:
            collection = CollectionModel.create({
                "name": collection_name,
                "description": (
                    f"Auto-managed glossary entries for {source_lang}→{target_lang} "
                    f"translation. Updated automatically when glossary entries change."
                ),
                "store_id": store.id,
                "embedding_model_id": embedding_model.id,
            })
            _logger.info(
                "Created knowledge collection '%s' (id=%d) for glossary",
                collection_name, collection.id,
            )
            return collection
        except Exception as e:
            _logger.warning("Failed to create knowledge collection for glossary: %s", e)
            return False

    def _sync_entry_to_knowledge(self, entry):
        """Sync a single glossary entry to the knowledge collection.

        Creates or updates a knowledge chunk containing the glossary mapping
        in a format suitable for semantic search.

        Args:
            entry: llm.translation.glossary record.
        """
        if not entry or not entry.source_phrase or entry.source_phrase.startswith("_pending_"):
            return

        try:
            collection = self._get_or_create_knowledge_collection(
                entry.source_lang, entry.target_lang,
            )
            if not collection:
                return

            # Link this entry to the collection
            if entry.knowledge_collection_id != collection:
                entry.with_context(skip_glossary_sync=True).write({
                    "knowledge_collection_id": collection.id,
                })

            # Build the chunk content
            chunk_content = self._build_chunk_content(entry)

            # Get the ir.model record for llm.translation.glossary
            ir_model = self.env["ir.model"].sudo().search([
                ("model", "=", "llm.translation.glossary"),
            ], limit=1)
            if not ir_model:
                return

            ResourceModel = self.env["llm.resource"].sudo()
            ChunkModel = self.env["llm.knowledge.chunk"].sudo()

            # Check if a resource already exists for this glossary entry
            existing_resource = ResourceModel.search([
                ("model_id", "=", ir_model.id),
                ("res_id", "=", entry.id),
            ], limit=1)

            if existing_resource:
                # Update existing chunks
                for chunk in existing_resource.chunk_ids:
                    chunk.write({"content": chunk_content})
                # Reset state to re-embed
                existing_resource.write({"state": "chunked"})
            else:
                # Create new resource + chunk
                resource = ResourceModel.create({
                    "name": f"Glossary: {entry.source_phrase[:50]}",
                    "model_id": ir_model.id,
                    "res_id": entry.id,
                    "content": chunk_content,
                    "state": "parsed",
                    "collection_ids": [(4, collection.id)],
                })
                ChunkModel.create({
                    "resource_id": resource.id,
                    "sequence": 1,
                    "content": chunk_content,
                })
                resource.write({"state": "chunked"})

            # Trigger embedding asynchronously (best-effort)
            try:
                collection.embed_resources()
            except Exception as e:
                _logger.warning("Glossary embedding failed (will retry later): %s", e)

        except Exception as e:
            _logger.warning("Failed to sync glossary entry %s to knowledge: %s", entry.id, e)

    def _remove_entry_from_knowledge(self, entry):
        """Remove a glossary entry's knowledge resource/chunks.

        Args:
            entry: llm.translation.glossary record.
        """
        try:
            ir_model = self.env["ir.model"].sudo().search([
                ("model", "=", "llm.translation.glossary"),
            ], limit=1)
            if not ir_model:
                return

            resource = self.env["llm.resource"].sudo().search([
                ("model_id", "=", ir_model.id),
                ("res_id", "=", entry.id),
            ], limit=1)
            if resource:
                resource.unlink()
        except Exception as e:
            _logger.warning("Failed to remove glossary entry %s from knowledge: %s", entry.id, e)

    @staticmethod
    def _build_chunk_content(entry):
        """Build searchable chunk content from a glossary entry.

        The content is formatted to be both human-readable and effective
        for semantic search matching.
        """
        parts = [f'"{entry.source_phrase}" → "{entry.new_phrase or entry.translated_text}"']

        if entry.old_phrase:
            parts.append(f"Previous translation: \"{entry.old_phrase}\"")
        if entry.context_source:
            parts.append(f"Context: {entry.context_source[:200]}")
        if entry.ai_analysis:
            parts.append(f"Note: {entry.ai_analysis}")

        return "\n".join(parts)

    def _sync_all_glossary_entries(self, source_lang=None, target_lang=None):
        """Bulk sync all glossary entries to knowledge collections.

        Can be called manually or via cron to ensure all entries are synced.
        """
        domain = [
            ("active", "=", True),
            ("source_phrase", "!=", False),
            ("source_phrase", "not like", "_pending_%"),
        ]
        if source_lang:
            domain.append(("source_lang", "=", source_lang))
        if target_lang:
            domain.append(("target_lang", "=", target_lang))

        entries = self.sudo().search(domain)
        synced = 0
        for entry in entries:
            try:
                self._sync_entry_to_knowledge(entry)
                synced += 1
            except Exception as e:
                _logger.warning("Failed to sync glossary entry %d: %s", entry.id, e)

        _logger.info("Synced %d/%d glossary entries to knowledge collections", synced, len(entries))
        return synced

    # =========================================================================
    # AI-BASED LEARNING FROM USER EDITS
    # =========================================================================

    @api.model
    def learn_from_edit(self, source_text, old_translated, new_translated,
                        source_lang="en", target_lang="zh",
                        provider=None, model=None, is_guest=False):
        """Record a user edit and use AI to identify the changed phrase.

        Step 1: Save the full context (source, old translation, new translation).
        Step 2: Call LLM to analyse exactly which source word/phrase maps to
                 the changed portion of the translation.
        Step 3: Parse AI response and update the glossary record.

        Args:
            source_text: Full source paragraph.
            old_translated: Machine translation before edit.
            new_translated: User-corrected translation after edit.
            source_lang / target_lang: Language codes.
            provider: ``llm.provider`` record to use for AI analysis.
            model: ``llm.model`` record to use for AI analysis.

        Returns:
            list[dict]: Learned entries, each with source_phrase / new_phrase / ai_analysis.
        """
        if not source_text or not old_translated or not new_translated:
            return []
        if old_translated.strip() == new_translated.strip():
            return []

        # Strip [TEXTBOX] markers for comparison
        source_clean = re.split(r'\s*\[TEXTBOX\]\s*', source_text)[0].strip()
        old_clean = re.split(r'\s*\[TEXTBOX\]\s*', old_translated)[0].strip()
        new_clean = re.split(r'\s*\[TEXTBOX\]\s*', new_translated)[0].strip()
        
        # Strip HTML tags (e.g. <b>, <i>, <u>) so AI only works with plain text
        source_clean = re.sub(r'<[^>]*>', '', source_clean)
        old_clean = re.sub(r'<[^>]*>', '', old_clean)
        new_clean = re.sub(r'<[^>]*>', '', new_clean)

        if old_clean == new_clean:
            return []

        # ── Step 1: Store full context first (with placeholder phrases) ──
        glossary_sudo = self.sudo()
        try:
            record = glossary_sudo.create({
                "context_source": source_clean,
                "old_translated": old_clean,
                "new_translated": new_clean,
                "source_phrase": f"_pending_{fields.Datetime.now()}",
                "old_phrase": "",
                "new_phrase": "",
                "source_text": source_clean[:200],
                "translated_text": new_clean[:200],
                "source_lang": source_lang,
                "target_lang": target_lang,
                "frequency": 1,
                "origin": "auto",
            })
        except Exception as e:
            _logger.warning("Failed to create glossary context record: %s", e)
            return []

        # ── Step 2: Call AI to analyse the edit ──
        ai_result = self._ai_analyse_edit(
            source_clean, old_clean, new_clean,
            source_lang, target_lang,
            provider, model,
        )

        if not ai_result or not ai_result.get("source_phrase"):
            # AI analysis failed — remove the pending record
            try:
                record.unlink()
            except Exception:
                pass
            _logger.info("AI analysis returned no result, discarded pending record.")
            return []

        # ── Step 3: Update the record with AI results ──
        source_phrase = ai_result["source_phrase"].strip()
        old_phrase = (ai_result.get("old_phrase") or "").strip()
        new_phrase = (ai_result.get("new_phrase") or "").strip()
        ai_analysis = (ai_result.get("ai_analysis") or "").strip()

        if not source_phrase or not new_phrase:
            try:
                record.unlink()
            except Exception:
                pass
            return []

        # Check for duplicate — if same source_phrase already exists, merge
        existing = glossary_sudo.search([
            ("source_phrase", "=", source_phrase),
            ("source_lang", "=", source_lang),
            ("target_lang", "=", target_lang),
            ("id", "!=", record.id),
        ], limit=1)

        if existing:
            # Update the existing entry and remove the new one
            existing.write({
                "context_source": source_clean,
                "old_translated": old_clean,
                "new_translated": new_clean,
                "old_phrase": old_phrase,
                "new_phrase": new_phrase,
                "ai_analysis": ai_analysis,
                "source_text": source_phrase,
                "translated_text": new_phrase,
                "frequency": existing.frequency + 1,
            })
            try:
                record.unlink()
            except Exception:
                pass
            _logger.info(
                "Glossary updated (freq=%d): '%s' → '%s'",
                existing.frequency, source_phrase, new_phrase,
            )
            # Sync to knowledge collection
            self._sync_entry_to_knowledge(existing)
        else:
            record.write({
                "source_phrase": source_phrase,
                "old_phrase": old_phrase,
                "new_phrase": new_phrase,
                "ai_analysis": ai_analysis,
                "source_text": source_phrase,
                "translated_text": new_phrase,
            })
            _logger.info(
                "Glossary learned: '%s' → '%s' (AI: %s)",
                source_phrase, new_phrase, ai_analysis[:80] if ai_analysis else "",
            )
            # Sync to knowledge collection
            self._sync_entry_to_knowledge(record)

        return [{
            "source_phrase": source_phrase,
            "old_phrase": old_phrase,
            "new_phrase": new_phrase,
            "ai_analysis": ai_analysis,
            "type": "ai",
        }]

    # ── AI analysis helper ───────────────────────────────────────────────

    @api.model
    def _ai_analyse_edit(self, source_text, old_translated, new_translated,
                         source_lang, target_lang,
                         provider=None, model=None):
        """Call LLM to identify which source phrase was affected by the edit.

        Returns:
            dict: {source_phrase, old_phrase, new_phrase, ai_analysis} or None.
        """
        if not provider or not model:
            _logger.info("No LLM provider/model for glossary AI analysis, skipping.")
            return None

        prompt = (
            "You are a translation analysis assistant. A user edited a machine "
            "translation. Identify the SPECIFIC source-language word or short "
            "phrase whose translation was changed.\n\n"
            f"Source language: {source_lang}\n"
            f"Target language: {target_lang}\n\n"
            f"SOURCE TEXT:\n{source_text}\n\n"
            f"ORIGINAL TRANSLATION:\n{old_translated}\n\n"
            f"USER-CORRECTED TRANSLATION:\n{new_translated}\n\n"
            "Respond in EXACTLY this JSON format (no extra text):\n"
            "```json\n"
            "{\n"
            '  "source_phrase": "<the source word/phrase that was changed>",\n'
            '  "old_phrase": "<how it was originally translated>",\n'
            '  "new_phrase": "<how the user corrected it>",\n'
            '  "analysis": "<brief explanation of the change>"\n'
            "}\n"
            "```\n"
            "Rules:\n"
            "- source_phrase must be text from the SOURCE TEXT, not the translation.\n"
            "- Keep phrases short — prefer individual words or 2-4 word phrases.\n"
            "- If multiple changes exist, pick the most significant one.\n"
            "- old_phrase and new_phrase should be in the target language.\n"
        )

        try:
            messages = [
                {"role": "system", "content": "You are a precise translation diff analyser. Always respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ]
            response = provider.chat(
                self.env["mail.message"],
                model=model,
                stream=False,
                tools=None,
                prepend_messages=messages,
            )
            raw_text = self._extract_ai_text(response)
            _logger.info("AI glossary analysis raw: %s", raw_text[:300] if raw_text else "")
            return self._parse_ai_json(raw_text)
        except Exception as e:
            _logger.warning("AI glossary analysis failed: %s", e)
            return None

    @api.model
    def _extract_ai_text(self, response):
        """Extract plain text from an LLM response (various formats)."""
        if isinstance(response, str):
            return response.strip()
        if hasattr(response, "choices"):
            choices = response.choices
            if choices and len(choices) > 0:
                msg = choices[0].message
                if hasattr(msg, "content"):
                    return (msg.content or "").strip()
        if isinstance(response, dict):
            if "content" in response:
                return response["content"].strip()
            if "choices" in response:
                ch = response["choices"]
                if ch:
                    return ch[0].get("message", {}).get("content", "").strip()
        # Generator / iterator
        if hasattr(response, "__iter__") and not isinstance(response, (str, dict, list)):
            parts = []
            for chunk in response:
                if isinstance(chunk, str):
                    parts.append(chunk)
                elif isinstance(chunk, dict):
                    msg = chunk.get("message", {})
                    if isinstance(msg, dict) and msg.get("content"):
                        parts.append(msg["content"])
                    elif chunk.get("content"):
                        parts.append(chunk["content"])
            return "".join(parts).strip()
        return str(response).strip() if response else ""

    @api.model
    def _parse_ai_json(self, text):
        """Parse the JSON object from AI response text."""
        if not text:
            return None

        # Strip <think>…</think> tags
        text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

        # Try to extract JSON from ```json ... ``` code block
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if m:
            text = m.group(1)
        else:
            # Try bare JSON object
            m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
            if m:
                text = m.group(0)

        try:
            data = json.loads(text)
            return {
                "source_phrase": data.get("source_phrase", ""),
                "old_phrase": data.get("old_phrase", ""),
                "new_phrase": data.get("new_phrase", ""),
                "ai_analysis": data.get("analysis", ""),
            }
        except (json.JSONDecodeError, TypeError) as e:
            _logger.warning("Failed to parse AI JSON: %s — raw: %s", e, text[:200])
            return None

    # =========================================================================
    # MANUAL ENTRY UPSERT
    # =========================================================================

    @api.model
    def _upsert_entry(self, source_text, translated_text, source_lang, target_lang):
        """Insert or update a glossary entry (for manual additions).

        Returns the glossary record (or False on failure).
        """
        if not source_text.strip() or not translated_text.strip():
            return False
        if len(source_text.strip()) < 2 or len(translated_text.strip()) < 1:
            return False

        try:
            glossary_sudo = self.sudo()
            existing = glossary_sudo.search([
                ("source_phrase", "=", source_text.strip()),
                ("source_lang", "=", source_lang),
                ("target_lang", "=", target_lang),
            ], limit=1)

            if existing:
                existing.write({
                    "new_phrase": translated_text.strip(),
                    "translated_text": translated_text.strip(),
                    "frequency": existing.frequency + 1,
                })
                # Sync to knowledge collection
                self._sync_entry_to_knowledge(existing)
                return existing
            else:
                entry = glossary_sudo.create({
                    "source_phrase": source_text.strip(),
                    "new_phrase": translated_text.strip(),
                    "source_text": source_text.strip(),
                    "translated_text": translated_text.strip(),
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "frequency": 1,
                    "origin": "manual",
                })
                # Sync to knowledge collection
                self._sync_entry_to_knowledge(entry)
                return entry
        except Exception as e:
            _logger.warning("Failed to upsert glossary entry: %s", e)
            return False

    # =========================================================================
    # LOOKUP FOR TRANSLATION
    # =========================================================================

    @staticmethod
    def _normalize_for_match(text):
        """Normalize text for matching: lowercase, strip ALL punctuation, collapse whitespace."""
        if not text:
            return ""
        s = text.lower()
        # Remove all punctuation (keep unicode letters/digits and whitespace)
        s = re.sub(r'[^\w\s]', ' ', s)
        # Collapse whitespace
        s = re.sub(r'\s+', ' ', s).strip()
        return s

    @staticmethod
    def _tokenize(text):
        """Split normalized text into word tokens."""
        return text.split() if text else []

    @staticmethod
    def _token_similar(a, b):
        """Check if two word tokens are similar enough to match.

        Handles: exact match, plural/singular, abbreviations (prefix),
        common English suffixes (-ing, -ed, -tion, -ment, etc.).
        """
        if a == b:
            return True
        # Plural/singular: strip trailing 's'
        if len(a) > 2 and len(b) > 2 and a.rstrip('s') == b.rstrip('s'):
            return True
        # Abbreviation: shorter is prefix of longer (min 3 chars)
        if len(a) >= 3 and len(b) >= 3:
            short, long_ = (a, b) if len(a) <= len(b) else (b, a)
            if long_.startswith(short):
                return True
        # Simple stemming: strip common suffixes and compare roots
        suffixes = ('ing', 'tion', 'sion', 'ment', 'ness', 'able', 'ible',
                     'ed', 'er', 'est', 'ly', 'ity', 'ful', 'less', 'ous')
        root_a, root_b = a, b
        for sfx in suffixes:
            if a.endswith(sfx) and len(a) > len(sfx) + 2:
                root_a = a[:-len(sfx)]
            if b.endswith(sfx) and len(b) > len(sfx) + 2:
                root_b = b[:-len(sfx)]
        if root_a == root_b and len(root_a) >= 3:
            return True
        return False

    @classmethod
    def _fuzzy_phrase_match(cls, phrase_norm, source_norm):
        """Multi-strategy fuzzy matching between a glossary phrase and source text.

        Strategies (tried in order, returns True on first hit):
        1. Exact normalized substring containment
        2. Ordered token subsequence (exact tokens, allowing gaps)
        3. Ordered token subsequence (fuzzy tokens — plurals, abbreviations, suffixes)
        4. Token set containment (for 3+ word phrases, ≥80% fuzzy-matched, any order)

        Returns:
            bool: True if the phrase matches within the source text.
        """
        # ── Strategy 1: exact normalized substring ──
        if phrase_norm in source_norm:
            return True

        phrase_tokens = cls._tokenize(phrase_norm)
        source_tokens = cls._tokenize(source_norm)

        if not phrase_tokens:
            return False

        # ── Strategy 2: ordered subsequence (exact tokens) ──
        pi = 0
        for st in source_tokens:
            if pi < len(phrase_tokens) and phrase_tokens[pi] == st:
                pi += 1
        if pi == len(phrase_tokens):
            return True

        # ── Strategy 3: ordered subsequence (fuzzy tokens) ──
        pi = 0
        for st in source_tokens:
            if pi < len(phrase_tokens) and cls._token_similar(phrase_tokens[pi], st):
                pi += 1
        if pi == len(phrase_tokens):
            return True

        # ── Strategy 4: unordered set match (≥80%, for longer phrases) ──
        if len(phrase_tokens) >= 3:
            matched = 0
            used = set()
            for pt in phrase_tokens:
                for i, st in enumerate(source_tokens):
                    if i not in used and cls._token_similar(pt, st):
                        matched += 1
                        used.add(i)
                        break
            if matched / len(phrase_tokens) >= 0.8:
                return True

        return False

    def find_matches(self, source_text, source_lang="en", target_lang="zh", is_guest=False):
        """Find glossary entries whose source_phrase matches in the source text.

        Uses multi-strategy fuzzy matching:
        - Ignores case, punctuation, extra whitespace
        - Handles plurals, abbreviations, common suffixes
        - Ordered subsequence matching (tokens in order with gaps)
        - Unordered set matching for long phrases (≥80% token overlap)

        Args:
            is_guest: If True, only return entries created by guests.
                      If False, return entries created by registered users.

        Returns matches sorted by length (longest first), max 15.
        """
        if not source_text or not source_text.strip():
            return []

        try:
            # Build the domain to filter by language pair and status
            domain = [
                ("source_lang", "=", source_lang),
                ("target_lang", "=", target_lang),
                ("active", "=", True),
                ("source_phrase", "!=", False),
                ("source_phrase", "not like", "_pending_%"),
            ]
            
            # Add create_uid.is_temp_user filter based on is_guest
            # If is_guest=True, filter for entries created by guests (is_temp_user=True)
            # If is_guest=False, filter for entries created by non-guests (is_temp_user=False)
            if is_guest:
                domain.append(("create_uid.is_temp_user", "=", True))
            else:
                domain.append(("create_uid.is_temp_user", "=", False))
            
            # Fetch filtered glossary entries
            entries = self.sudo().search(domain, order="frequency desc")

            matches = []
            source_norm = self._normalize_for_match(source_text)
            for entry in entries:
                phrase = (entry.source_phrase or "").strip()
                if not phrase:
                    continue
                phrase_norm = self._normalize_for_match(phrase)
                if not phrase_norm:
                    continue
                if self._fuzzy_phrase_match(phrase_norm, source_norm):
                    matches.append({
                        "source": phrase,
                        "translated": (entry.new_phrase or entry.translated_text or "").strip(),
                        "frequency": entry.frequency,
                    })

            matches.sort(key=lambda m: len(m["source"]), reverse=True)
            return matches[:15]

        except Exception as e:
            _logger.warning("Glossary lookup failed: %s", e)
            return []

    @api.model
    def format_for_prompt(self, matches):
        """Format glossary matches as a prompt section for the LLM."""
        if not matches:
            return ""

        lines = []
        for m in matches:
            lines.append(f"  \"{m['source']}\" → \"{m['translated']}\"")

        return (
            "\n\n[TRANSLATION MEMORY - MANDATORY]\n"
            "The following translations were manually verified by the user. "
            "You MUST use these exact translations when the source text contains "
            "these terms or phrases:\n"
            + "\n".join(lines)
            + "\n[END TRANSLATION MEMORY]\n"
        )
