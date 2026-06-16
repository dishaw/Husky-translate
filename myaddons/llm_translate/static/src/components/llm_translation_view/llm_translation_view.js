/** @odoo-module **/

import { _t } from "@web/core/l10n/translation";
import { Component, useState, onWillStart, onMounted, onPatched, useRef, markup } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { DropdownItem } from "@web/core/dropdown/dropdown_item";

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const LARGE_UPLOAD_BYTES = 1 * 1024 * 1024;
const SUBMIT_INTERVAL_STORAGE_KEY = "llm_translate.submit_interval_ms";
const DEFAULT_SUBMIT_INTERVAL_MS = 300;

/**
 * LLM Translation View - Word-style split-pane document translation
 *
 * Features:
 * - Both panes render with Word-like styling (font sizes, bold, alignment)
 * - Both source and translated text are freely editable (contenteditable)
 * - Top-right dropdowns: provider/model selection (changeable anytime)
 * - File upload integrated into creation form
 * - Paragraph-by-paragraph translation with real-time progress
 */
export class LLMTranslationView extends Component {
    static template = "llm_translate.TranslationView";
    static components = { Dropdown, DropdownItem };
    static props = {
        action: { type: Object, optional: true },
        actionId: { type: Number, optional: true },
        updateActionState: { type: Function, optional: true },
        className: { type: String, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.action = useService("action");
        this.notification = useService("notification");

        this._abortTranslation = false;
        // Pending file for creation form
        this._pendingFile = null;

        // Root element ref for DOM queries (scroll sync)
        this.rootRef = useRef("root");

        this.state = useState({
            isLoading: true,
            isTranslating: false,
            isUploading: false,
            isLoadingUpdate: false,
            isLoadingMoreLines: false,
            uploadProgress: 0,
            uploadPhase: "",
            uploadSpeedText: "",
            uploadEtaText: "",

            providers: [],
            collections: [],
            projects: [],
            partners: [],
            translationList: [],

            currentTranslation: null,

            // Creation form
            selectedPartnerId: null,
            selectedProjectId: null,
            selectedCollectionId: null,
            sourceLang: "en",
            targetLang: "zh",
            sourceLangCustom: "",
            targetLangCustom: "",
            createFileName: "",

            modelSearchQuery: "",

            viewMode: "list",
            translationDisplayMode: "split",

            // Zoom level for source/translation panes
            zoomLevel: 1,

            // Reasoning modal
            showReasoningModal: false,
            reasoningText: "",

            // Glossary panel
            showGlossaryPanel: false,
            glossaryEntries: [],
            glossaryNewSource: "",
            glossaryNewTranslated: "",
            glossaryFilter: "",
            glossaryCount: 0,

            // Translation submit pacing
            showSettingsModal: false,
            submitIntervalMs: this._loadSubmitIntervalMs(),
            submitIntervalInput: "",

            // Guest/temp user support
            isTempUser: false,
            showLoginModal: false,
            guestUserName: "",
        });

        this.languages = [
            { value: "zh", label: "中文 (Chinese)" },
            { value: "en", label: "English" },
            { value: "ja", label: "日本語 (Japanese)" },
            { value: "ko", label: "한국어 (Korean)" },
            { value: "fr", label: "Français (French)" },
            { value: "de", label: "Deutsch (German)" },
            { value: "es", label: "Español (Spanish)" },
            { value: "pt", label: "Português (Portuguese)" },
            { value: "ru", label: "Русский (Russian)" },
            { value: "ar", label: "العربية (Arabic)" },
            { value: "it", label: "Italiano (Italian)" },
            { value: "nl", label: "Nederlands (Dutch)" },
            { value: "th", label: "ไทย (Thai)" },
            { value: "vi", label: "Tiếng Việt (Vietnamese)" },
            { value: "other", label: _t("Other") },
        ];

        onWillStart(async () => {
            await this._loadInitialData();
        });

        onMounted(() => {
            const params = this.props.action?.params;
            if (params?.translation_id) {
                this._openTranslation(params.translation_id);
            }
            
            // Initialize scroll sync + populate translated/source slots after DOM is ready
            setTimeout(() => {
                this._initializeScrollSync();
                this._updateTranslatedSlots();
                this._updateSourceSlots();
                this._initializeOcrDragHandlers();
            }, 100);
        });

        // After every reactive re-render, push new translated/source HTML into the
        // contenteditable slots imperatively (bypassing Owl's VHtml tracking).
        onPatched(() => {
            this._updateTranslatedSlots();
            this._updateSourceSlots();
            this._initializeOcrDragHandlers();
        });
    }

    // =========================================================================
    // Translated Slot Management (imperative innerHTML to avoid VHtml crashes)
    // =========================================================================

    /**
     * Return a raw HTML string representing the current display state of a
     * translated line. Used to populate the slot <span> inside each
     * contenteditable div WITHOUT going through Owl's VHtml/VToggler tracking.
     *
     * This avoids the "insertBefore: node not a child" NotFoundError caused
     * when browser contenteditable editing (cut/paste/typing) restructures the
     * DOM and invalidates the comment/text anchor nodes that Owl's VHtml
     * patcher stores as insertion reference points.
     */
    _getTranslatedDisplayHtmlStr(line) {
        const translatedText = this.getParaTranslatedText(line);
        if (translatedText) {
            const htmlMarkup = this.getTranslatedHtml(line);
            // markup() returns a special Owl string-like object; .toString() gives raw HTML
            return typeof htmlMarkup === "object" ? String(htmlMarkup) : (htmlMarkup || "");
        }
        if (line.state === "translating") {
            return '<span class="text-muted fst-italic"><i class="fa fa-spinner fa-spin me-1"></i>translating...</span>';
        }
        if (line.state === "error") {
            return '<span class="text-danger fst-italic"><i class="fa fa-exclamation-triangle me-1"></i>failed</span>';
        }
        return '<span class="text-muted fst-italic">pending...</span>';
    }

    /**
     * Return a raw HTML string for the source text of a line.
     * Used to populate the source slot <span> imperatively, same pattern as
     * _getTranslatedDisplayHtmlStr to avoid VHtml crashes.
     */
    _getSourceDisplayHtmlStr(line) {
        const htmlMarkup = this.getSourceHtml(line);
        return typeof htmlMarkup === "object" ? String(htmlMarkup) : (htmlMarkup || "");
    }

    /**
     * Imperatively update all `.llm-source-text-slot` spans in the left
     * pane with their current display HTML.
     *
     * Same pattern as _updateTranslatedSlots — bypasses Owl VHtml to prevent
     * the "insertBefore: node not a child" NotFoundError on the source pane.
     */
    _updateSourceSlots() {
        const root = this.rootRef.el;
        if (!root) return;

        const slots = root.querySelectorAll(
            ".llm-translate-pane-left .llm-source-text-slot[data-source-slot], " +
            ".llm-bilingual-pane .llm-source-text-slot[data-source-slot]"
        );
        for (const slot of slots) {
            const ceDiv = slot.closest("[contenteditable]");
            if (!ceDiv) continue;
            if (document.activeElement === ceDiv) continue;

            const lineId = parseInt(slot.getAttribute("data-source-slot"), 10);
            const line = this.currentLines?.find((l) => l.id === lineId);
            if (!line) continue;

            const newHtml = this._getSourceDisplayHtmlStr(line);

            // Clean stale orphan nodes created by browser cut/paste/typing
            for (const child of Array.from(ceDiv.childNodes)) {
                if (child === slot) continue;
                if (child.nodeType === Node.ELEMENT_NODE) {
                    const cl = child.classList;
                    if (cl && (
                        cl.contains("llm-bilingual-inline-actions") ||
                        cl.contains("llm-doc-image-inline") ||
                        cl.contains("llm-doc-image-float") ||
                        cl.contains("llm-doc-image-block") ||
                        cl.contains("llm-doc-image") ||
                        cl.contains("clearfix")
                    )) {
                        continue;
                    }
                }
                child.remove();
            }

            if (slot.innerHTML !== newHtml) {
                slot.innerHTML = newHtml;
            }
        }
    }

    /**
     * Imperatively update all `.llm-translated-text-slot` spans in the right
     * pane with their current display HTML.
     *
     * Called from onMounted (via setTimeout) and onPatched so that translated
     * content is always in sync with reactive state, without using Owl's VHtml
     * inside a contenteditable div.
     */
    _updateTranslatedSlots() {
        const root = this.rootRef.el;
        if (!root) return;

        const slots = root.querySelectorAll(
            ".llm-translate-pane-right .llm-translated-text-slot[data-line-slot], " +
            ".llm-bilingual-pane .llm-translated-text-slot[data-line-slot]"
        );
        for (const slot of slots) {
            // Skip if the containing contenteditable is currently focused
            // (don't overwrite while user is actively editing this line)
            const ceDiv = slot.closest("[contenteditable]");
            if (!ceDiv) continue;
            if (document.activeElement === ceDiv) continue;

            const lineId = parseInt(slot.getAttribute("data-line-slot"), 10);
            const line = this.currentLines?.find((l) => l.id === lineId);
            if (!line) continue;

            const newHtml = this._getTranslatedDisplayHtmlStr(line);

            // Clean stale DOM nodes: browser cut/paste operations split the
            // slot <span> and create orphan text/element nodes outside it,
            // causing content duplication. Remove everything except:
            //   - the primary slot span (the one with data-line-slot)
            //   - image containers (.llm-doc-image-inline, .llm-doc-image-float)
            //   - clearfix divs
            for (const child of Array.from(ceDiv.childNodes)) {
                if (child === slot) continue;
                if (child.nodeType === Node.ELEMENT_NODE) {
                    const cl = child.classList;
                    if (cl && (
                        cl.contains("llm-bilingual-inline-actions") ||
                        cl.contains("llm-doc-image-inline") ||
                        cl.contains("llm-doc-image-float") ||
                        cl.contains("llm-doc-image-block") ||
                        cl.contains("llm-doc-image") ||
                        cl.contains("clearfix")
                    )) {
                        continue;
                    }
                }
                child.remove();
            }

            if (slot.innerHTML !== newHtml) {
                slot.innerHTML = newHtml;
            }
        }
    }

    // =========================================================================
    // Scroll Synchronization (Split Pane)
    // =========================================================================

    /**
     * Initialize scroll sync between left (source) and right (translated) panes.
     * When one pane scrolls, the other pane scrolls to the corresponding line.
     */
    _initializeScrollSync() {
        if (this._scrollSyncListeners) {
            const { leftPane, rightPane, syncLeftToRight, syncRightToLeft } = this._scrollSyncListeners;
            if (leftPane) leftPane.removeEventListener("scroll", syncLeftToRight);
            if (rightPane) rightPane.removeEventListener("scroll", syncRightToLeft);
            this._scrollSyncListeners = null;
        }

        const leftPane = this.rootRef.el?.querySelector(".llm-translate-pane-left .pane-content");
        const rightPane = this.rootRef.el?.querySelector(".llm-translate-pane-right .pane-content");
        
        if (!leftPane || !rightPane) {
            return; // Not in detail view yet
        }

        // Debounce function to prevent excessive syncing
        const debounce = (func, delay) => {
            let timeoutId;
            return (...args) => {
                clearTimeout(timeoutId);
                timeoutId = setTimeout(() => func(...args), delay);
            };
        };

        // Sync left → right
        const syncLeftToRight = debounce(() => {
            if (this._syncInProgress) return;
            if (this._suppressSyncUntil && Date.now() < this._suppressSyncUntil) return;
            this._syncInProgress = true;
            
            const scrollRatio = leftPane.scrollTop / (leftPane.scrollHeight - leftPane.clientHeight || 1);
            rightPane.scrollTop = scrollRatio * (rightPane.scrollHeight - rightPane.clientHeight);
            
            this._syncInProgress = false;
        }, 50);

        // Sync right → left  
        const syncRightToLeft = debounce(() => {
            if (this._syncInProgress) return;
            if (this._suppressSyncUntil && Date.now() < this._suppressSyncUntil) return;
            this._syncInProgress = true;
            
            const scrollRatio = rightPane.scrollTop / (rightPane.scrollHeight - rightPane.clientHeight || 1);
            leftPane.scrollTop = scrollRatio * (leftPane.scrollHeight - leftPane.clientHeight);
            
            this._syncInProgress = false;
        }, 50);

        // Add event listeners
        leftPane.addEventListener("scroll", syncLeftToRight);
        rightPane.addEventListener("scroll", syncRightToLeft);

        // Store for cleanup on unmount or re-initialization
        this._scrollSyncListeners = { leftPane, rightPane, syncLeftToRight, syncRightToLeft };
    }

    // =========================================================================
    // Data Loading
    // =========================================================================

    _loadSubmitIntervalMs() {
        const raw = window.localStorage?.getItem(SUBMIT_INTERVAL_STORAGE_KEY);
        const parsed = Number.parseInt(raw || "", 10);
        if (Number.isFinite(parsed)) {
            return Math.max(0, Math.min(parsed, 60000));
        }
        return DEFAULT_SUBMIT_INTERVAL_MS;
    }

    _submitDelay() {
        const delay = Math.max(0, Math.min(Number(this.state.submitIntervalMs) || 0, 60000));
        if (!delay) {
            return Promise.resolve();
        }
        return new Promise((resolve) => setTimeout(resolve, delay));
    }

    async _loadInitialData() {
        try {
            // Check if user is a temp/guest user
            await this._checkUserStatus();

            const [providers, collections, projects] = await Promise.all([
                rpc("/llm_translate/providers", {}),
                rpc("/llm_translate/collections", {}),
                rpc("/llm_translate/projects", {})
            ]);
            this.state.providers = providers || [];
            this.state.collections = collections || [];
            this.state.projects = projects || [];
            
            // Extract unique partners from projects
            const partnerMap = new Map();
            for (const p of this.state.projects) {
                const pId = p.partner_id || 0;
                if (!partnerMap.has(pId)) {
                    partnerMap.set(pId, { id: pId, name: p.partner_name || "No Company" });
                }
            }
            this.state.partners = Array.from(partnerMap.values());

            // Restore last selected company/project, or default to first
            const lastSelection = this._getLastCompanyProject();
            if (lastSelection.partnerId && this.state.partners.find(p => p.id === lastSelection.partnerId)) {
                this.state.selectedPartnerId = lastSelection.partnerId;
            } else if (this.state.partners.length > 0) {
                this.state.selectedPartnerId = this.state.partners[0].id;
            }
            
            this._updateAvailableProjects();

            // Restore last selected project
            if (lastSelection.projectId && this.availableProjects.find(p => p.id === lastSelection.projectId)) {
                this.state.selectedProjectId = lastSelection.projectId;
            }

            // Load translation list based on selected project
            await this._loadTranslationList();

            // Load glossary count for default language pair
            await this._loadGlossaryCount();
        } catch (e) {
            console.error("Failed to load initial data:", e);
            this.notification.add(_t("Failed to load translation data"), { type: "danger" });
        } finally {
            this.state.isLoading = false;
        }
    }

    _updateAvailableProjects() {
        if (this.state.selectedPartnerId !== null) {
            const pid = this.state.selectedPartnerId === 0 ? false : this.state.selectedPartnerId;
            const available = this.state.projects.filter(p => p.partner_id === pid);
            if (available.length > 0) {
                this.state.selectedProjectId = available[0].id;
            } else {
                this.state.selectedProjectId = null;
            }
        } else {
            this.state.selectedProjectId = null;
        }
    }

    async _loadTranslationList() {
        try {
            const result = await rpc("/llm_translate/list", {
                project_id: this.state.selectedProjectId || false,
            });
            this.state.translationList = result || [];
        } catch (e) {
            console.error("Failed to load translation list:", e);
        }
    }

    // =========================================================================
    // Computed - Provider/Model
    // =========================================================================

    get currentProvider() {
        const t = this.state.currentTranslation;
        if (!t) return null;
        return this.state.providers.find((p) => p.id === t.provider_id) || null;
    }

    get currentModel() {
        const provider = this.currentProvider;
        if (!provider) return null;
        const t = this.state.currentTranslation;
        return (provider.models || []).find((m) => m.id === t.model_id) || null;
    }

    get availableProjects() {
        if (this.state.selectedPartnerId === null) return [];
        const pid = this.state.selectedPartnerId === 0 ? false : this.state.selectedPartnerId;
        return this.state.projects.filter(p => p.partner_id === pid);
    }

    get availableProviders() {
        return this.state.providers;
    }

    get availableModels() {
        const provider = this.currentProvider;
        if (!provider) return [];
        const models = provider.models || [];
        const query = (this.state.modelSearchQuery || "").toLowerCase().trim();
        if (!query) return models;
        return models.filter((m) => m.name.toLowerCase().includes(query));
    }

    // =========================================================================
    // Computed - Translation state
    // =========================================================================

    get hasCurrentTranslation() {
        return !!this.state.currentTranslation;
    }

    get currentLines() {
        return this.state.currentTranslation?.lines || [];
    }

    /** All lines including empty ones */
    get allLines() {
        return this.currentLines;
    }

    get contentLines() {
        return this.currentLines.filter((l) => !l.is_empty);
    }

    /** Lines that form the document header (line_type === 'header') */
    get headerLines() {
        return this.currentLines.filter((l) => l.line_type === "header");
    }

    /** Lines that form the document footer (line_type === 'footer') */
    get footerLines() {
        return this.currentLines.filter((l) => l.line_type === "footer");
    }

    /** Body lines only (regular paragraphs + table cells; textboxes are embedded in parent) */
    get bodyLines() {
        return this.currentLines.filter(
            (l) => !l.line_type || l.line_type === "body" || l.line_type === "table_cell"
        );
    }

    /**
     * Split body lines into pages for Word-like page preview.
     * Each page has ~28 body paragraphs plus repeated header/footer.
     */
    get pages() {
        const LINES_PER_PAGE = 28;
        const body = this.bodyLines;
        const pages = [];
        for (let i = 0; i < body.length; i += LINES_PER_PAGE) {
            pages.push({
                pageNumber: Math.floor(i / LINES_PER_PAGE) + 1,
                lines: body.slice(i, i + LINES_PER_PAGE),
            });
        }
        if (pages.length === 0) {
            pages.push({ pageNumber: 1, lines: [] });
        }
        return pages;
    }

    get totalPages() {
        return this.pages.length;
    }

    /**
     * Group page lines into elements: consecutive table rows from the
     * same table become a single "table" element; other lines are "line".
     * This lets us render one <table> per table group with aligned columns.
     */
    getPageElements(page) {
        const elements = [];
        let currentTableGroup = null;

        for (const line of page.lines) {
            if (this.isTableRow(line)) {
                const meta = line.style_metadata || {};
                const tableIdx = meta.table_index ?? -1;

                if (currentTableGroup && currentTableGroup.tableIndex === tableIdx) {
                    currentTableGroup.rows.push(line);
                } else {
                    const colCount = meta.table_col_count || 4;
                    currentTableGroup = {
                        type: "table",
                        tableIndex: tableIdx,
                        colCount: colCount,
                        rows: [line],
                        key: `tbl_${line.id}`,
                    };
                    elements.push(currentTableGroup);
                }
            } else {
                currentTableGroup = null;
                elements.push({
                    type: "line",
                    line: line,
                    key: `line_${line.id}`,
                });
            }
        }
        return elements;
    }

    get progressPercent() {
        return Math.round(this.state.currentTranslation?.progress || 0);
    }

    get lineWindow() {
        return this.state.currentTranslation?.line_window || null;
    }

    get hasMoreLines() {
        return !!this.lineWindow?.has_more;
    }

    get loadedLineCount() {
        return this.state.currentTranslation?.lines?.length || 0;
    }

    get canTranslate() {
        const t = this.state.currentTranslation;
        return t && t.lines && t.lines.length > 0 &&
            ["draft", "error", "translating", "done"].includes(t.state);
    }

    get isStuckTranslating() {
        const t = this.state.currentTranslation;
        return t && t.state === "translating" && !this.state.isTranslating;
    }

    get errorLineCount() {
        const lines = this.state.currentTranslation?.lines || [];
        return lines.filter((l) => l.state === "error").length;
    }

    get canDownload() {
        const t = this.state.currentTranslation;
        // Allow download for done and error (partial translation) states
        // Temp users can see the button but will be prompted to login
        return t && (t.state === "done" || t.state === "error") && (t.result_filename || t.translated_lines > 0 || this.state.isTempUser);
    }

    async _checkUserStatus() {
        try {
            const result = await rpc("/llm_translate/check_user", {});
            if (result) {
                this.state.isTempUser = result.is_temp_user || false;
                this.state.guestUserName = result.user_name || "";
            }
        } catch (e) {
            console.warn("Could not check user status:", e);
        }
    }

    onShowLoginModal() {
        this.state.showLoginModal = true;
    }

    onCloseLoginModal() {
        this.state.showLoginModal = false;
    }

    onGoToLogin() {
        window.location.href = "/web/login";
    }

    onGoToWeChatLogin() {
        window.location.href = "/auth_wechat/login";
    }

    get stateLabel() {
        const labels = {
            draft: _t("Draft"),
            extracting: _t("Extracting"),
            translating: _t("Translating..."),
            done: _t("Completed"),
            error: _t("Error"),
        };
        return labels[this.state.currentTranslation?.state] || "";
    }

    get stateBadgeClass() {
        const classes = {
            draft: "bg-info",
            extracting: "bg-warning",
            translating: "bg-primary",
            done: "bg-success",
            error: "bg-danger",
        };
        return classes[this.state.currentTranslation?.state] || "bg-secondary";
    }

    // =========================================================================
    // Word-style rendering helpers
    // =========================================================================

    /**
     * Convert style_metadata to inline CSS for Word-like rendering.
     * Applied to each paragraph div.
     *
     * For the SOURCE pane: mixed bold is handled by per-run <span> tags
     * in getSourceHtml(), so paragraph-level bold is only set when ALL
     * runs are bold.
     *
     * Use getTranslatedLineStyle() for the translated pane instead,
     * which applies bold when the *majority* of source text is bold.
     */
    getLineStyle(line) {
        return this._buildLineStyle(line, /* forTranslation */ false);
    }

    /**
     * Inline CSS for the TRANSLATED pane.
     *
     * Since translated text is plain (no per-run spans), we apply bold
     * at paragraph level when ≥50 % of the source characters are bold.
     * This gives the closest visual match to the original formatting.
     */
    getTranslatedLineStyle(line) {
        return this._buildLineStyle(line, /* forTranslation */ true);
    }

    /** @private shared builder */
    _buildLineStyle(line, forTranslation) {
        const meta = line.style_metadata || {};
        const parts = [];

        // Font size - use the pt value extracted from python-docx
        const fontSize = meta.font_size;
        if (fontSize) {
            parts.push(`font-size: ${fontSize}pt`);
        } else {
            // Check runs for font size
            const runs = meta.runs || [];
            const runFontSize = runs.length > 0 ? runs[0].font_size : null;
            if (runFontSize) {
                parts.push(`font-size: ${runFontSize}pt`);
            } else {
                const style = (meta.style || "").toLowerCase();
                if (style.includes("heading 1") || style === "title") {
                    parts.push("font-size: 22pt");
                } else if (style.includes("heading 2")) {
                    parts.push("font-size: 18pt");
                } else if (style.includes("heading 3")) {
                    parts.push("font-size: 14pt");
                } else if (style.includes("heading 4")) {
                    parts.push("font-size: 12pt");
                }
            }
        }

        // Bold
        const runs = meta.runs || [];
        if (runs.length > 0) {
            if (forTranslation) {
                // For translated text: bold if ≥50% of source characters are bold
                let boldChars = 0, totalChars = 0;
                for (const r of runs) {
                    const len = (r.text || "").length;
                    totalChars += len;
                    if (r.bold) boldChars += len;
                }
                if (totalChars > 0 && boldChars / totalChars >= 0.5) {
                    parts.push("font-weight: bold");
                }
            } else {
                // For source text: only if ALL runs are bold (per-run spans handle mixed)
                const allBold = runs.every((r) => r.bold);
                if (allBold) parts.push("font-weight: bold");
            }
        } else if (meta.bold) {
            parts.push("font-weight: bold");
        }

        // Alignment
        const alignment = meta.alignment;
        if (alignment === "CENTER") {
            parts.push("text-align: center");
        } else if (alignment === "RIGHT") {
            parts.push("text-align: right");
        } else if (alignment === "JUSTIFY") {
            parts.push("text-align: justify");
        }

        // Color / italic / underline / font — apply at paragraph level
        // when all runs agree (or for translation when majority agrees)
        if (runs.length > 0 && runs[0].color) {
            const sameColor = runs.every((r) => r.color === runs[0].color);
            if (sameColor) parts.push(`color: #${runs[0].color}`);
        }
        if (runs.length > 0) {
            if (forTranslation) {
                let italicChars = 0, totalC = 0;
                for (const r of runs) {
                    const len = (r.text || "").length;
                    totalC += len;
                    if (r.italic) italicChars += len;
                }
                if (totalC > 0 && italicChars / totalC >= 0.5) {
                    parts.push("font-style: italic");
                }
            } else if (runs[0].italic) {
                const allItalic = runs.every((r) => r.italic);
                if (allItalic) parts.push("font-style: italic");
            }
        }
        if (runs.length > 0 && runs[0].underline) {
            const allUnderline = runs.every((r) => r.underline);
            if (allUnderline) parts.push("text-decoration: underline");
        }
        if (runs.length > 0 && runs[0].font_name) {
            parts.push(`font-family: "${runs[0].font_name}", "SimSun", serif`);
        }

        return parts.join("; ");
    }

    /**
     * Check if a line has mixed run formatting (bold/italic/color differ
     * between runs).  If so, the source pane should use per-run styled
     * spans; otherwise plain text with paragraph-level CSS suffices.
     */
    hasMixedRuns(line) {
        const runs = (line.style_metadata || {}).runs || [];
        if (runs.some((r) => this._getRunRevisionClass(r))) return true;
        if (runs.length <= 1) return false;
        return runs.some((r) =>
            r.bold !== runs[0].bold ||
            r.italic !== runs[0].italic ||
            r.underline !== runs[0].underline ||
            r.color !== runs[0].color ||
            r.font_size !== runs[0].font_size
        );
    }

    _getRunRevisionClass(run) {
        const rawType = (
            run.revision_type ||
            run.change_type ||
            run.track_change_type ||
            run.tracked_change ||
            run.revision ||
            ""
        ).toString().toLowerCase();
        const isInserted = run.inserted || run.is_inserted || rawType.includes("ins") || rawType.includes("insert");
        const isDeleted = run.deleted || run.is_deleted || rawType.includes("del") || rawType.includes("delete");
        if (isDeleted) return "llm-doc-revision-delete";
        if (isInserted) return "llm-doc-revision-insert";
        return "";
    }

    _buildRunsHtml(runs, meta = {}) {
        const parts = [];
        for (const run of runs || []) {
            const rText = run.text || "";
            if (!rText) continue;
            const styles = [];
            if (run.bold) styles.push("font-weight:bold");
            if (run.italic) styles.push("font-style:italic");
            if (run.underline) styles.push("text-decoration:underline");
            if (run.color) styles.push(`color:#${run.color}`);
            if (run.font_size && run.font_size !== meta.font_size) {
                styles.push(`font-size:${run.font_size}pt`);
            }

            const cls = this._getRunRevisionClass(run);
            const escaped = this._escapeHtml(rText).replace(/\n/g, "<br/>");
            if (styles.length > 0 || cls) {
                const attrs = [];
                if (cls) attrs.push(`class="${cls}"`);
                if (styles.length > 0) attrs.push(`style="${styles.join(';')}"`);
                parts.push(`<span ${attrs.join(" ")}>${escaped}</span>`);
            } else {
                parts.push(escaped);
            }
        }
        return markup(parts.join(""));
    }

    /**
     * Generate rich-text HTML for source paragraph.
     *
     * Only emits per-run <span> tags when runs actually have DIFFERENT
     * formatting (mixed bold/italic/color/size).  When all runs share
     * the same formatting, returns escaped plain text and lets the
     * paragraph-level CSS (from getLineStyle) handle the styling.
     */
    getSourceHtml(line) {
        const meta = line.style_metadata || {};
        const runs = meta.runs || [];
        const text = this.getParaSourceText(line);

        // No mixed formatting → plain text (paragraph CSS handles it)
        if (!this.hasMixedRuns(line)) {
            // Convert newlines to <br> for multi-paragraph table cells
            const escaped = this._escapeHtml(text);
            if (text.includes("\n")) {
                return markup(escaped.replace(/\n/g, "<br/>"));
            }
            return escaped;
        }

        // Mixed formatting → per-run spans.
        // Paragraph-level getLineStyle() does NOT set bold/italic when
        // runs are mixed, so the inherited baseline is always "normal".
        // We only need to output ACTIVE styles (bold=true, italic=true).
        return this._buildRunsHtml(runs, meta);
    }

    /** Escape HTML special characters for safe innerHTML rendering. */
    _escapeHtml(str) {
        if (!str) return "";
        return str
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;");
    }

    /**
     * Extract text from a contenteditable element, preserving <b>/<i>/<u> tags.
     *
     * The rendered HTML uses <span style="font-weight:bold"> etc.  This method
     * walks the DOM tree and converts those back to the canonical <b>/<i>/<u>
     * markers that the database and docx export expect.
     */
    _extractFormattedText(element) {
        let result = "";
        for (const node of element.childNodes) {
            if (node.nodeType === Node.TEXT_NODE) {
                result += node.textContent;
                continue;
            }
            if (node.nodeType !== Node.ELEMENT_NODE) continue;

            const tag = node.tagName.toLowerCase();

            // Skip non-editable badges / action buttons / images / clearfix
            if (node.classList?.contains("llm-table-cell-badge") ||
                node.classList?.contains("llm-line-actions") ||
                node.classList?.contains("llm-bilingual-inline-actions") ||
                node.classList?.contains("llm-doc-image-inline") ||
                node.classList?.contains("llm-doc-image-float") ||
                node.classList?.contains("clearfix") ||
                tag === "img") {
                continue;
            }

            // Detect formatting from style attribute or semantic tags
            const style = node.getAttribute("style") || "";
            const openTags = [];
            const closeTags = [];

            if (tag === "b" || tag === "strong" || /font-weight\s*:\s*bold/i.test(style)) {
                openTags.push("<b>");
                closeTags.unshift("</b>");
            }
            if (tag === "i" || tag === "em" || /font-style\s*:\s*italic/i.test(style)) {
                openTags.push("<i>");
                closeTags.unshift("</i>");
            }
            if (tag === "u" || /text-decoration\s*:[^;]*underline/i.test(style)) {
                openTags.push("<u>");
                closeTags.unshift("</u>");
            }

            // Handle <br> as newline
            if (tag === "br") {
                result += "\n";
                continue;
            }

            const inner = this._extractFormattedText(node);
            result += openTags.join("") + inner + closeTags.join("");
        }
        return result;
    }

    /**
     * Generate rich-text HTML for a translated paragraph.
     *
     * The LLM is instructed to preserve <b>/<i>/<u> tags from the source.
     * This method parses those tags from translated_text and converts them
     * to <span style="..."> elements for safe HTML rendering.
     *
     * Falls back to escaped plain text when no tags are present.
     */
    getTranslatedHtml(line) {
        const text = this.getParaTranslatedText(line);
        if (!text) return "";

        // Quick check: does translated text contain our formatting markers?
        if (!/<[biu]>|<\/[biu]>/.test(text)) {
            // Convert newlines to <br> for multi-paragraph table cells
            const escaped = this._escapeHtml(text);
            if (text.includes("\n")) {
                return markup(escaped.replace(/\n/g, "<br/>"));
            }
            return escaped;
        }

        const styleMap = {
            b: "font-weight:bold",
            i: "font-style:italic",
            u: "text-decoration:underline",
        };
        const TAG_RE = /<(\/?)(b|i|u)>/g;
        let result = "";
        let lastIndex = 0;
        let m;

        while ((m = TAG_RE.exec(text)) !== null) {
            // Escaped text before this tag
            if (m.index > lastIndex) {
                result += this._escapeHtml(text.slice(lastIndex, m.index));
            }
            const isClose = m[1] === "/";
            const tag = m[2];
            result += isClose ? "</span>" : `<span style="${styleMap[tag]}">`;
            lastIndex = m.index + m[0].length;
        }
        if (lastIndex < text.length) {
            result += this._escapeHtml(text.slice(lastIndex));
        }
        return markup(result);
    }

    /**
     * Get images for a line from style_metadata.
     * Returns array of image objects.
     */
    getLineImages(line) {
        const meta = line.style_metadata || {};
        return meta.images || [];
    }

    /**
     * Get inline images only (flow with text).
     */
    getInlineImages(line) {
        return this.getLineImages(line).filter(
            (img) => img.placement === "inline" || !img.placement
        );
    }

    /**
     * Get floating/anchored images (positioned outside text flow).
     */
    getFloatingImages(line) {
        return this.getLineImages(line).filter(
            (img) => img.placement === "anchor"
        );
    }

    /**
     * Compute CSS style for a single image based on its Word positioning.
     */
    getImageStyle(img) {
        const parts = [];
        if (img.width) parts.push(`width: ${Math.min(img.width, 500)}px`);
        if (img.height) parts.push(`height: auto`);
        parts.push("max-width: 100%");

        if (img.placement === "anchor") {
            const wrap = img.wrap_type || "none";
            const align = img.align_h || "left";

            if (wrap === "square" || wrap === "tight" || wrap === "through") {
                // Float alongside text
                if (align === "right") {
                    parts.push("float: right");
                    parts.push("margin: 4px 0 4px 10px");
                } else {
                    parts.push("float: left");
                    parts.push("margin: 4px 10px 4px 0");
                }
            } else if (wrap === "topAndBottom") {
                // Block between text, respect alignment
                parts.push("display: block");
                if (align === "center") {
                    parts.push("margin: 6px auto");
                } else if (align === "right") {
                    parts.push("margin: 6px 0 6px auto");
                } else {
                    parts.push("margin: 6px auto 6px 0");
                }
            } else {
                // wrapNone or unknown – use alignment hint
                parts.push("display: block");
                if (align === "center") {
                    parts.push("margin: 4px auto");
                } else if (align === "right") {
                    parts.push("margin: 4px 0 4px auto");
                } else if (img.offset_h != null && img.offset_h > 20) {
                    parts.push(`margin-left: ${Math.min(img.offset_h, 300)}px`);
                    parts.push("margin-top: 4px");
                    parts.push("margin-bottom: 4px");
                } else {
                    parts.push("margin: 4px auto 4px 0");
                }
            }
        } else {
            // Inline image
            parts.push("vertical-align: middle");
        }
        return parts.join("; ");
    }

    /**
     * Get CSS class for an image container div.
     */
    getImageContainerClass(img) {
        if (img.placement !== "anchor") return "llm-doc-image-inline";
        const wrap = img.wrap_type || "none";
        if (wrap === "square" || wrap === "tight" || wrap === "through") {
            return "llm-doc-image-float";
        }
        if (wrap === "topAndBottom") {
            return "llm-doc-image-block";
        }
        // wrapNone
        const align = img.align_h || "left";
        if (align === "center") return "llm-doc-image-block llm-doc-image-center";
        if (align === "right") return "llm-doc-image-block llm-doc-image-right";
        return "llm-doc-image-block";
    }

    /**
     * Get textboxes attached to a line from style_metadata.
     * Returns array of textbox objects with text and styling info.
     */
    getLineTextboxes(line) {
        const meta = line.style_metadata || {};
        return meta.textboxes || [];
    }

    /**
     * Check if a line has textbox(es) attached.
     */
    hasTextboxes(line) {
        return this.getLineTextboxes(line).length > 0;
    }

    /**
     * Get OCR text blocks for a specific image in a body/image paragraph.
     * Finds image_ocr lines whose para_index AND image_index match,
     * then returns their text_blocks with translated text.
     * Each block is enriched with _imgW / _imgH so getOcrBlockStyle()
     * can estimate rendered dimensions for font-size calculation.
     *
     * @param {Object} line - The paragraph line object.
     * @param {number} imageIndex - The index of the image within the paragraph.
     */
    getImageOcrBlocks(line, imageIndex) {
        const meta = line.style_metadata || {};
        const paraIndex = meta.para_index;
        if (paraIndex === null || paraIndex === undefined) return [];
        if (imageIndex === null || imageIndex === undefined) imageIndex = 0;

        // Find image_ocr lines for this paragraph AND this specific image
        const ocrLines = this.currentLines.filter(
            (l) => l.line_type === "image_ocr" &&
                   l.style_metadata?.para_index === paraIndex &&
                   (l.style_metadata?.image_index ?? 0) === imageIndex &&
                   l.state === "done"
        );
        // Collect all text_blocks from OCR results
        const blocks = [];
        for (const ocrLine of ocrLines) {
            const ocrMeta = ocrLine.style_metadata || {};
            const imgW = ocrMeta.image_width_px || 600;
            const imgH = ocrMeta.image_height_px || 800;
            const ocrResult = ocrLine.image_ocr_result;
            if (ocrResult && ocrResult.text_blocks) {
                for (const block of ocrResult.text_blocks) {
                    if (block.translated && block.translated.trim()) {
                        blocks.push({ ...block, _imgW: imgW, _imgH: imgH, _ocrLineId: ocrLine.id });
                    }
                }
            }
        }
        return blocks;
    }

    /**
     * Compute the inline CSS style for an OCR overlay block.
     *
     * The font-size is calculated by considering BOTH the box area AND the
     * number of characters in the translated text.  This avoids the old
     * problem where a tall-but-narrow box with many characters got an
     * oversized font (e.g. h_pct=10, 3 lines of text → old formula gave
     * 14px which only fits 1 line).
     *
     * Algorithm:
     *   1. Estimate the rendered pixel size of the box using the image's
     *      original aspect ratio and a ~450px render width assumption.
     *   2. areaFont = sqrt(boxW_px × boxH_px / charCount)
     *      → the font that fills the box area evenly.
     *   3. heightFont = boxH_px × 0.85
     *      → the max font that fits at least one line.
     *   4. Final font = clamp(min(areaFont, heightFont), 5, 14).
     */
    getOcrBlockStyle(block) {
        const x = Number(block.x_pct ?? 0);
        const y = Number(block.y_pct ?? 0);
        const w = Number(block.w_pct ?? 10);
        const h = Number(block.h_pct ?? 5);
        const text = block.translated || "";
        const charCount = Math.max(1, text.length);

        const imgW = block._imgW || 600;
        const imgH = block._imgH || 800;
        const estRenderW = Math.min(imgW, 500);
        const estRenderH = estRenderW * (imgH / imgW);

        // font_size_px stores the preview CSS px value (set by A+/A- buttons).
        // Use it directly for display; download renders at the equivalent size.
        let fontPx;
        if (block.font_size_px) {
            fontPx = Math.max(5, Math.min(48, Number(block.font_size_px)));
        } else {
            const boxW = (w / 100) * estRenderW;
            const boxH = (h / 100) * estRenderH;
            const areaFont = Math.sqrt((boxW * boxH) / charCount);
            const heightFont = boxH * 0.85;
            fontPx = Math.max(5, Math.min(28, Math.round(
                Math.min(areaFont, heightFont)
            )));
        }

        // Estimate wrapped line count and grow box height when OCR h_pct is
        // too small for the chosen font.
        const boxW = (w / 100) * estRenderW;
        const avgCharWidth = Math.max(1, fontPx * 0.62);
        const charsPerLine = Math.max(1, Math.floor(boxW / avgCharWidth));
        const hardLineCount = (text.match(/\n/g) || []).length + 1;
        const wrappedLineCount = Math.ceil(charCount / charsPerLine);
        const estimatedLines = Math.max(hardLineCount, wrappedLineCount);
        const neededBoxHPx = estimatedLines * fontPx * 1.2 + 2;
        const neededHPct = (neededBoxHPx / estRenderH) * 100;
        const displayH = Math.min(100 - y, Math.max(h, neededHPct));

        return (
            `left:${x}%;top:${y}%;width:${w}%;height:${displayH}%;` +
            `font-size:${fontPx}px;`
        );
    }

    /**
     * Get the source text for a line, excluding [TEXTBOX] markers.
     * The actual paragraph text (before any textbox content).
     */
    getParaSourceText(line) {
        const text = line.source_text || "";
        const parts = text.split(/\s*\[TEXTBOX\]\s*/);
        return parts[0].trim();
    }

    /**
     * Get translated text for the paragraph part only (without textbox portions).
     */
    getParaTranslatedText(line) {
        const text = this.getDisplayText(line) || "";
        const parts = text.split(/\s*\[TEXTBOX\]\s*/);
        return parts[0].trim();
    }

    /**
     * Get translated textbox texts from line translated_text.
     * Returns array of translated textbox strings.
     */
    getTranslatedTextboxTexts(line) {
        const text = this.getDisplayText(line) || "";
        const parts = text.split(/\s*\[TEXTBOX\]\s*/);
        return parts.slice(1).map((t) => t.trim());  // skip first part (paragraph text)
    }

    /**
     * Get source textbox texts from line source_text.
     * Returns array of source textbox strings.
     */
    getSourceTextboxTexts(line) {
        const text = line.source_text || "";
        const parts = text.split(/\s*\[TEXTBOX\]\s*/);
        return parts.slice(1).map((t) => t.trim());
    }

    /**
     * Get textbox inline style for rendering.
     */
    getTextboxStyle(tb) {
        const parts = [];

        // Use absolute positioning if we have position data
        const posH = tb.position_h;
        const posV = tb.position_v;
        if (posH != null || posV != null) {
            parts.push("position: absolute");
            // Scale positions relative to page width (Word ~595pt ≈ 793px, our page ~530px)
            const scale = 530 / 793;
            if (posH != null) {
                parts.push(`left: ${Math.round(posH * scale)}px`);
            }
            if (posV != null) {
                parts.push(`top: ${Math.round(posV * scale)}px`);
            }
            parts.push("z-index: 10");
        }

        if (tb.width) {
            const scale = 530 / 793;
            parts.push(`width: ${Math.round(Math.min(tb.width, 600) * scale)}px`);
        }
        if (tb.height) {
            const scale = 530 / 793;
            parts.push(`min-height: ${Math.round(Math.min(tb.height, 400) * scale)}px`);
        }

        const hasBorder = tb.has_border !== false;
        if (hasBorder) {
            const bc = tb.border_color;
            parts.push(`border: 1px solid ${bc ? '#' + bc : '#333'}`);
        } else {
            parts.push("border: 1px dashed #ccc");
        }

        if (tb.has_fill && tb.fill_color) {
            parts.push(`background-color: #${tb.fill_color}`);
        }

        parts.push("padding: 4px");
        parts.push("box-sizing: border-box");

        const tbParas = tb.paragraphs || [];
        if (tbParas.length > 0) {
            const first = tbParas[0];
            if (first.font_size) parts.push(`font-size: ${first.font_size}pt`);
            if (first.bold) parts.push("font-weight: bold");
            if (first.italic) parts.push("font-style: italic");
            if (first.color) parts.push(`color: #${first.color}`);
            if (first.font_name) parts.push(`font-family: "${first.font_name}", "SimSun", serif`);
            if (first.alignment === "center") parts.push("text-align: center");
            else if (first.alignment === "right") parts.push("text-align: right");
        }

        return parts.join("; ");
    }

    /**
     * Get CSS class for the paragraph based on Word style name.
     */
    getLineClass(line) {
        const meta = line.style_metadata || {};
        const style = (meta.style || "").toLowerCase();
        const classes = ["llm-doc-para"];

        if (style.includes("heading") || style === "title") {
            classes.push("llm-doc-heading");
        }
        if (line.is_empty) {
            classes.push("llm-doc-empty");
        }
        if (meta.is_table_row || line.line_type === "table_cell") {
            classes.push("llm-doc-table-row");
        }

        return classes.join(" ");
    }

    /** Check if a line is a table row */
    isTableRow(line) {
        const meta = line.style_metadata || {};
        return !!(meta.is_table_row || line.line_type === "table_cell");
    }

    /** Get table row label like "⊞ R3" */
    getTableRowLabel(line) {
        const meta = line.style_metadata || {};
        if (!meta.is_table_row) return "";
        const r = (meta.row_index ?? 0) + 1;
        return `\u229E R${r}`;
    }

    /**
     * Get individual cell texts from a table row line.
     * Source text has cells joined by [CELL] separator.
     */
    getTableCells(line) {
        const text = line.source_text || "";
        return text.split(/\s*\[CELL\]\s*/).map(c => c.trim());
    }

    /**
     * Get cells with metadata (text + gridSpan + html) for rendering with colspan.
     * Falls back to gridSpan=1 when metadata is missing.
     */
    getTableCellsWithMeta(line) {
        const texts = this.getTableCells(line);
        const meta = line.style_metadata || {};
        const cellsMeta = meta.cells || [];
        return texts.map((text, i) => {
            const cm = cellsMeta[i] || {};
            return {
                text: text,
                gridSpan: cm.grid_span || 1,
                rowSpan: cm.row_span || 1,
                html: this._buildCellSourceHtml(cm),
            };
        });
    }

    /**
     * Get translated cells with metadata (text + gridSpan + html).
     * ALWAYS uses source cell structure as base (cellsMeta from extraction),
     * only replaces text content with translations.
     * This guarantees column count, gridSpan, rowSpan always match source.
     */
    getTranslatedCellsWithMeta(line) {
        const translatedTexts = this.getTranslatedTableCells(line);
        const meta = line.style_metadata || {};
        const cellsMeta = meta.cells || [];

        // If no source cell metadata, fall back to translated texts
        if (cellsMeta.length === 0) {
            return translatedTexts.map((text) => ({
                text,
                gridSpan: 1,
                rowSpan: 1,
                html: text ? markup(this._escapeHtml(text)) : "",
            }));
        }

        // Use source cellsMeta as definitive structure
        return cellsMeta.map((cm, i) => {
            const transText = (i < translatedTexts.length) ? translatedTexts[i] : "";
            return {
                text: transText || cm.text || "",
                gridSpan: cm.grid_span || 1,
                rowSpan: cm.row_span || 1,
                // Has translated text → render with source formatting;
                // otherwise show source rich HTML as placeholder
                html: transText
                    ? this._buildCellTranslatedHtml(transText, cm)
                    : this._buildCellSourceHtml(cm),
            };
        });
    }

    /**
     * Build rich HTML for a source table cell using its per-cell runs.
     * Similar to getSourceHtml but scoped to a single cell.
     */
    _buildCellSourceHtml(cellMeta) {
        const runs = cellMeta.runs || [];
        const text = cellMeta.text || "";
        if (!text) return "";
        return this.getSourceHtml({
            source_text: text,
            style_metadata: {
                ...cellMeta,
                runs,
                font_size: cellMeta.font_size,
            },
        });
    }

    /**
     * Build rich HTML for a translated table cell.
     * Converts <b>/<i>/<u> tags from LLM output into styled <span> elements.
     * Falls back to plain escaped text if no tags.
     */
    _buildCellTranslatedHtml(text, cellMeta) {
        if (!text) return "";

        // Build base style from source cell metadata (font_size, bold)
        const baseStyles = [];
        if (cellMeta.font_size) baseStyles.push(`font-size:${cellMeta.font_size}pt`);
        if (cellMeta.bold) baseStyles.push("font-weight:bold");
        const baseStyle = baseStyles.length > 0 ? baseStyles.join(';') : '';

        const styleMap = {
            b: "font-weight:bold",
            i: "font-style:italic",
            u: "text-decoration:underline",
        };

        if (!/<[biu]>|<\/[biu]>/.test(text)) {
            const escaped = this._escapeHtml(text);
            if (baseStyle) {
                return markup(`<span style="${baseStyle}">${escaped}</span>`);
            }
            return escaped;
        }

        const TAG_RE = /<(\/?)(b|i|u)>/g;
        let inner = "";
        let lastIndex = 0;
        let m;

        while ((m = TAG_RE.exec(text)) !== null) {
            if (m.index > lastIndex) {
                inner += this._escapeHtml(text.slice(lastIndex, m.index));
            }
            const isClose = m[1] === "/";
            const tag = m[2];
            inner += isClose ? "</span>" : `<span style="${styleMap[tag]}">`;
            lastIndex = m.index + m[0].length;
        }
        if (lastIndex < text.length) {
            inner += this._escapeHtml(text.slice(lastIndex));
        }

        if (baseStyle) {
            return markup(`<span style="${baseStyle}">${inner}</span>`);
        }
        return markup(inner);
    }

    /**
     * Get individual translated cell texts from a table row line.
     */
    getTranslatedTableCells(line) {
        const text = this.getDisplayText(line) || "";
        return text.split(/\s*\[CELL\]\s*/).map(c => c.trim());
    }

    // =========================================================================
    // Language Selection (detail view dropdowns)
    // =========================================================================

    async onDetailSourceLangChange(ev) {
        const newLang = ev.target.value;
        if (!this.state.currentTranslation) return;
        if (newLang === this.state.currentTranslation.source_lang) return;

        this.state.isLoadingUpdate = true;
        try {
            await this.orm.write("llm.translation", [this.state.currentTranslation.id], {
                source_lang: newLang,
            });
            this.state.currentTranslation.source_lang = newLang;
        } catch (e) {
            console.error("Failed to update source language:", e);
            this.notification.add(_t("Failed to update source language"), { type: "danger" });
        } finally {
            this.state.isLoadingUpdate = false;
        }
    }

    async onSwapLanguages() {
        if (!this.state.currentTranslation) return;
        const source = this.state.currentTranslation.source_lang;
        const target = this.state.currentTranslation.target_lang;
        
        if (source === target) {
            this.notification.add(_t("Source and target languages are the same"), { type: "warning" });
            return;
        }

        this.state.isLoadingUpdate = true;
        try {
            await this.orm.write("llm.translation", [this.state.currentTranslation.id], {
                source_lang: target,
                target_lang: source,
            });
            this.state.currentTranslation.source_lang = target;
            this.state.currentTranslation.target_lang = source;
        } catch (e) {
            console.error("Failed to swap languages:", e);
            this.notification.add(_t("Failed to swap languages"), { type: "danger" });
        } finally {
            this.state.isLoadingUpdate = false;
        }
    }

    onZoomIn() {
        this.state.zoomLevel = Math.min(3, this.state.zoomLevel + 0.25);
    }

    onZoomOut() {
        this.state.zoomLevel = Math.max(0.25, this.state.zoomLevel - 0.25);
    }

    onZoomReset() {
        this.state.zoomLevel = 1;
    }

    toggleTranslationDisplayMode() {
        this.state.translationDisplayMode =
            this.state.translationDisplayMode === "split" ? "bilingual" : "split";

        setTimeout(() => {
            this._updateTranslatedSlots();
            this._updateSourceSlots();
            this._initializeOcrDragHandlers();
            if (this.state.translationDisplayMode === "split") {
                this._initializeScrollSync();
            }
        }, 0);
    }

    async onDetailTargetLangChange(ev) {
        const newLang = ev.target.value;
        if (!this.state.currentTranslation) return;
        if (newLang === this.state.currentTranslation.target_lang) return;

        this.state.isLoadingUpdate = true;
        try {
            await this.orm.write("llm.translation", [this.state.currentTranslation.id], {
                target_lang: newLang,
            });
            this.state.currentTranslation.target_lang = newLang;
        } catch (e) {
            console.error("Failed to update target language:", e);
            this.notification.add(_t("Failed to update target language"), { type: "danger" });
        } finally {
            this.state.isLoadingUpdate = false;
        }
    }

    // =========================================================================
    // Provider/Model Selection (dropdown style)
    // =========================================================================

    async selectProvider(provider) {
        if (!this.state.currentTranslation) return;
        if (provider.id === this.state.currentTranslation.provider_id) return;

        this.state.isLoadingUpdate = true;
        try {
            const models = provider.models || [];
            const defaultModel = models.length > 0 ? models[0] : null;
            const modelId = defaultModel ? defaultModel.id : false;

            await this.orm.write("llm.translation", [this.state.currentTranslation.id], {
                provider_id: provider.id,
                model_id: modelId,
            });

            this.state.currentTranslation.provider_id = provider.id;
            this.state.currentTranslation.provider_name = provider.name;
            this.state.currentTranslation.model_id = modelId;
            this.state.currentTranslation.model_name = defaultModel ? defaultModel.name : "";

            this._saveLastSelection(provider.id, modelId);
        } catch (e) {
            console.error("Failed to update provider:", e);
            this.notification.add(_t("Failed to update provider"), { type: "danger" });
        } finally {
            this.state.isLoadingUpdate = false;
        }
    }

    async selectModel(model) {
        if (!this.state.currentTranslation) return;
        if (model.id === this.state.currentTranslation.model_id) return;

        this.state.isLoadingUpdate = true;
        try {
            await this.orm.write("llm.translation", [this.state.currentTranslation.id], {
                model_id: model.id,
            });
            this.state.currentTranslation.model_id = model.id;
            this.state.currentTranslation.model_name = model.name;
            this._saveLastSelection(this.state.currentTranslation.provider_id, model.id);
        } catch (e) {
            console.error("Failed to update model:", e);
            this.notification.add(_t("Failed to update model"), { type: "danger" });
        } finally {
            this.state.isLoadingUpdate = false;
        }
    }

    onModelSearchInput() {}
    clearModelSearch() {
        this.state.modelSearchQuery = "";
    }

    // =========================================================================
    // localStorage
    // =========================================================================

    _saveLastSelection(providerId, modelId) {
        try {
            localStorage.setItem("llm_translate_last_provider", String(providerId));
            localStorage.setItem("llm_translate_last_model", String(modelId));
        } catch { /* ignore */ }
    }

    _getLastSelection() {
        try {
            return {
                providerId: parseInt(localStorage.getItem("llm_translate_last_provider")) || null,
                modelId: parseInt(localStorage.getItem("llm_translate_last_model")) || null,
            };
        } catch {
            return { providerId: null, modelId: null };
        }
    }

    _getDefaultProviderModel() {
        const last = this._getLastSelection();
        if (last.providerId) {
            const provider = this.state.providers.find((p) => p.id === last.providerId);
            if (provider) {
                if (last.modelId) {
                    const model = (provider.models || []).find((m) => m.id === last.modelId);
                    if (model) return { providerId: provider.id, modelId: model.id };
                }
                const models = provider.models || [];
                if (models.length > 0) return { providerId: provider.id, modelId: models[0].id };
            }
        }
        if (this.state.providers.length > 0) {
            const provider = this.state.providers[0];
            const models = provider.models || [];
            return {
                providerId: provider.id,
                modelId: models.length > 0 ? models[0].id : null,
            };
        }
        return { providerId: null, modelId: null };
    }

    _saveLastCompanyProject() {
        try {
            localStorage.setItem("llm_translate_last_company", String(this.state.selectedPartnerId || ""));
            localStorage.setItem("llm_translate_last_project", String(this.state.selectedProjectId || ""));
        } catch { /* ignore */ }
    }

    _getLastCompanyProject() {
        try {
            return {
                partnerId: parseInt(localStorage.getItem("llm_translate_last_company")) || null,
                projectId: parseInt(localStorage.getItem("llm_translate_last_project")) || null,
            };
        } catch {
            return { partnerId: null, projectId: null };
        }
    }

    // =========================================================================
    // Creation form actions
    // =========================================================================

    onPartnerChange(ev) {
        this.state.selectedPartnerId = parseInt(ev.target.value);
        this._updateAvailableProjects();
        this._loadTranslationList(); // Reload translations for new project
        this._saveLastCompanyProject();
    }

    onProjectChange(ev) {
        this.state.selectedProjectId = parseInt(ev.target.value) || null;
        this._loadTranslationList(); // Reload translations when project changes
        this._saveLastCompanyProject();
    }
    onCollectionChange(ev) {
        this.state.selectedCollectionId = parseInt(ev.target.value) || null;
    }
    onSourceLangChange(ev) {
        this.state.sourceLang = ev.target.value;
        this._loadGlossaryCount();
    }
    onTargetLangChange(ev) {
        this.state.targetLang = ev.target.value;
        this._loadGlossaryCount();
    }

    async _loadGlossaryCount() {
        try {
            const result = await rpc("/llm_translate/glossary/count", {
                source_lang: this.state.sourceLang || "",
                target_lang: this.state.targetLang || "",
            });
            this.state.glossaryCount = result?.count || 0;
        } catch (e) {
            this.state.glossaryCount = 0;
        }
    }

    /** Open file picker dialog when Create button clicked */
    onCreateNewOpenFile() {
        if (!this.state.selectedProjectId) {
            this.notification.add(_t("Please select a Project."), { type: "warning" });
            return;
        }
        // Trigger hidden file input click
        const fileInput = this.rootRef.el?.querySelector('input[type="file"]');
        if (fileInput) {
            fileInput.click();
        }
    }

    /** File selection in the creation form */
    onCreateFileChange(ev) {
        const file = ev.target.files?.[0];
        if (!file) {
            this._pendingFile = null;
            this.state.createFileName = "";
            return;
        }
        if (!this._validateUploadFile(file)) {
            this._pendingFile = null;
            this.state.createFileName = "";
            ev.target.value = "";
            return;
        }
        this._pendingFile = file;
        this.state.createFileName = file.name;
        // Auto-trigger creation after file selection
        this.onCreateNew();
    }

    async onCreateNew() {
        if (!this._pendingFile) {
            this.notification.add(_t("Please select a file."), { type: "warning" });
            return;
        }

        const defaults = this._getDefaultProviderModel();
        if (!defaults.providerId || !defaults.modelId) {
            this.notification.add(
                _t("No Provider/Model available. Please configure a provider in the LLM settings first."),
                { type: "warning" },
            );
            return;
        }
        if (!this.state.selectedProjectId) {
            this.notification.add(_t("Please select a Project."), { type: "warning" });
            return;
        }

        this.state.isUploading = true;
        this._resetUploadProgress();

        try {
            const file = this._pendingFile;
            const filename = file.name;
            const vals = {
                name: `Translation - ${filename}`,
                provider_id: defaults.providerId,
                model_id: defaults.modelId,
                project_id: this.state.selectedProjectId,
                source_lang: this.state.sourceLang,
                target_lang: this.state.targetLang,
                source_lang_custom: this.state.sourceLangCustom,
                target_lang_custom: this.state.targetLangCustom,
                knowledge_collection_id: this.state.selectedCollectionId || false,
            };

            let result;
            if (file.size > LARGE_UPLOAD_BYTES) {
                result = await this._createWithBinaryUpload(file, vals);
            } else {
                const fileData = await this._readFileAsBase64(file);
                result = await rpc("/llm_translate/create", {
                    vals,
                    file_data: fileData,
                    filename,
                });
            }

            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }

            this._pendingFile = null;
            this.state.createFileName = "";
            this.state.currentTranslation = result;
            this.state.viewMode = "detail";
            
            // Wait for DOM to update then initialize scroll sync
            setTimeout(() => {
                this._initializeScrollSync();
            }, 100);

            await this._loadTranslationList();

            if (filename && result.total_lines > 0) {
                this.notification.add(
                    _t("%s paragraphs extracted from %s", result.total_lines, filename),
                    { type: "success" },
                );
            }
        } catch (e) {
            console.error("Create failed:", e);
            this.notification.add(_t("Failed to create translation"), { type: "danger" });
        } finally {
            this.state.isUploading = false;
            this._resetUploadProgress();
            // Reset file input
            const fileInput = this.rootRef.el?.querySelector('input[type="file"]');
            if (fileInput) { fileInput.value = ""; }
        }
    }

    // =========================================================================
    // File Upload (in detail view toolbar, for re-upload)
    // =========================================================================

    async onFileUpload(ev) {
        const file = ev.target.files?.[0];
        if (!file) return;

        const validExtensions = [".doc", ".docx", ".pdf"];
        const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
        if (!validExtensions.includes(ext)) {
            this.notification.add(_t("Please upload a Word document (.doc/.docx) or PDF (.pdf)"), { type: "warning" });
            ev.target.value = "";
            return;
        }
        if (!this._validateUploadFile(file)) {
            ev.target.value = "";
            return;
        }
        if (!this.state.currentTranslation) {
            this.notification.add(_t("Please create a translation task first"), { type: "warning" });
            return;
        }

        this.state.isUploading = true;
        this._resetUploadProgress();
        try {
            let result;
            if (file.size > LARGE_UPLOAD_BYTES) {
                result = await this._uploadBinaryToTranslation(
                    this.state.currentTranslation.id,
                    file,
                );
            } else {
                const fileData = await this._readFileAsBase64(file);
                result = await rpc("/llm_translate/upload", {
                    translation_id: this.state.currentTranslation.id,
                    file_data: fileData,
                    filename: file.name,
                });
            }
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }
            this.state.currentTranslation = result;
            if (result.state === "error" && result.error_message) {
                this.notification.add(
                    _t("Document processing failed: %s", result.error_message),
                    { type: "danger", sticky: true },
                );
                return;
            }
            this.notification.add(
                _t("Document uploaded: %s paragraphs extracted", result.total_lines || 0),
                { type: "success" },
            );
        } catch (e) {
            console.error("Upload failed:", e);
            this.notification.add(_t("Upload failed"), { type: "danger" });
        } finally {
            this.state.isUploading = false;
            this._resetUploadProgress();
            ev.target.value = "";
        }
    }

    // =========================================================================
    // Inline Editing (contenteditable blur-to-save)
    // =========================================================================

    /**
     * Strip <think>...</think> tags from text for display.
     * Defensive frontend-side stripping for any residual think tags.
     */
    getDisplayText(line) {
        const text = line.translated_text || "";
        if (!text.includes("<think>")) return text;
        return text.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
    }

    /**
     * When user clicks a source paragraph, scroll the right pane
     * to the corresponding translated paragraph and flash it twice.
     */
    onSourceClick(lineId, ev) {
        // Don't interfere with text selection in contenteditable
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;

        const leftPane = this.rootRef.el?.querySelector(".llm-translate-pane-left .pane-content");
        const rightPane = this.rootRef.el?.querySelector(".llm-translate-pane-right .pane-content");
        if (!leftPane || !rightPane) return;

        const targetEl = rightPane.querySelector(`[data-line-id="${lineId}"]`);
        if (!targetEl) return;

        // Get clicked element's vertical position relative to its pane viewport
        const sourceEl = leftPane.querySelector(`[data-line-id="${lineId}"]`);
        const desiredTop = sourceEl
            ? sourceEl.getBoundingClientRect().top - leftPane.getBoundingClientRect().top
            : 0;

        // Scroll right pane so targetEl appears at the same vertical offset
        const targetTopInContainer = targetEl.getBoundingClientRect().top
            - rightPane.getBoundingClientRect().top
            + rightPane.scrollTop;

        // Suppress scroll-sync feedback during programmatic scroll (~700ms for smooth)
        this._suppressSyncUntil = Date.now() + 700;
        rightPane.scrollTo({ top: targetTopInContainer - desiredTop, behavior: "smooth" });

        // Flash animation (blink twice)
        targetEl.classList.remove("llm-flash");
        void targetEl.offsetWidth;
        targetEl.classList.add("llm-flash");
        targetEl.addEventListener("animationend", () => {
            targetEl.classList.remove("llm-flash");
        }, { once: true });
    }

    /**
     * When user clicks a translated paragraph, scroll the left pane
     * to the corresponding source paragraph and flash it twice.
     */
    onTranslatedClick(lineId, ev) {
        // Don't interfere with text selection in contenteditable
        const sel = window.getSelection();
        if (sel && sel.toString().length > 0) return;

        const leftPane = this.rootRef.el?.querySelector(".llm-translate-pane-left .pane-content");
        const rightPane = this.rootRef.el?.querySelector(".llm-translate-pane-right .pane-content");
        if (!leftPane || !rightPane) return;

        const targetEl = leftPane.querySelector(`[data-line-id="${lineId}"]`);
        if (!targetEl) return;

        // Get clicked element's vertical position relative to its pane viewport
        const sourceEl = rightPane.querySelector(`[data-line-id="${lineId}"]`);
        const desiredTop = sourceEl
            ? sourceEl.getBoundingClientRect().top - rightPane.getBoundingClientRect().top
            : 0;

        // Scroll left pane so targetEl appears at the same vertical offset
        const targetTopInContainer = targetEl.getBoundingClientRect().top
            - leftPane.getBoundingClientRect().top
            + leftPane.scrollTop;

        // Suppress scroll-sync feedback during programmatic scroll (~700ms for smooth)
        this._suppressSyncUntil = Date.now() + 700;
        leftPane.scrollTo({ top: targetTopInContainer - desiredTop, behavior: "smooth" });

        // Flash animation (blink twice)
        targetEl.classList.remove("llm-flash");
        void targetEl.offsetWidth;
        targetEl.classList.add("llm-flash");
        targetEl.addEventListener("animationend", () => {
            targetEl.classList.remove("llm-flash");
        }, { once: true });
    }

    /**
     * Save source text when user finishes editing (blur).
     */
    async onSourceBlur(lineId, ev) {
        const line = this.currentLines.find((l) => l.id === lineId);
        if (!line) return;

        // Extract text (source usually doesn't have b/i/u tags, but be safe)
        let newParaText;
        if (this.isTableRow(line)) {
            const tds = ev.target.querySelectorAll('td');
            if (tds.length > 0) {
                newParaText = Array.from(tds).map(td => td.innerText.trim()).join(' [CELL] ');
            } else {
                newParaText = ev.target.innerText;
            }
        } else {
            const clone = ev.target.cloneNode(true);
            clone.querySelectorAll('.llm-table-cell-badge').forEach(el => el.remove());
            newParaText = clone.innerText;
        }

        // Reconstruct full text preserving [TEXTBOX] portions
        const oldText = line.source_text || "";
        const parts = oldText.split(/\s*\[TEXTBOX\]\s*/);
        
        // Compare plain text to detect real content changes
        const oldPlain = (parts[0] || "").replace(/<[^>]*>?/gm, "").trim();
        const newPlain = (newParaText || "").trim();
        if (oldPlain === newPlain) return;

        parts[0] = newParaText;
        const newText = parts.join("\n[TEXTBOX]\n");

        if (line.source_text === newText) return;

        // No need to manually clear innerHTML — _updateSourceSlots() in onPatched
        // handles orphan node cleanup imperatively after state update.
        line.source_text = newText;
        try {
            await rpc("/llm_translate/update_line", {
                line_id: lineId,
                source_text: newText,
            });
        } catch (e) {
            console.error("Failed to save source text:", e);
        }
    }

    /**
     * Save translated text when user finishes editing (blur).
     */
    async onTranslatedBlur(lineId, ev) {
        const line = this.currentLines.find((l) => l.id === lineId);
        if (!line) return;

        // Extract formatted text preserving <b>/<i>/<u> tags
        let newParaText;
        if (this.isTableRow(line)) {
            const tds = ev.target.querySelectorAll('td');
            if (tds.length > 0) {
                newParaText = Array.from(tds).map(td => this._extractFormattedText(td).trim()).join(' [CELL] ');
            } else {
                newParaText = this._extractFormattedText(ev.target);
            }
        } else {
            newParaText = this._extractFormattedText(ev.target);
        }

        // Reconstruct full text preserving [TEXTBOX] portions
        const oldText = line.translated_text || "";
        const parts = oldText.split(/\s*\[TEXTBOX\]\s*/);
        
        // Compare plain text (strip tags) to detect real content changes
        const oldPlain = (parts[0] || "").replace(/<[^>]*>?/gm, "").trim();
        const newPlain = (newParaText || "").replace(/<[^>]*>?/gm, "").trim();
        if (oldPlain === newPlain) {
            // Text unchanged – but formatting may have changed.
            // If the tagged text is identical too, skip entirely.
            if ((parts[0] || "").trim() === (newParaText || "").trim()) {
                return;
            }
        }

        parts[0] = newParaText;
        const newText = parts.join("\n[TEXTBOX]\n");

        if (line.translated_text === newText) return;

        line.translated_text = newText;
        line.state = "done";
        try {
            const result = await rpc("/llm_translate/update_line", {
                line_id: lineId,
                translated_text: newText,
            });
            // Show notification for auto-learned glossary entries
            if (result && result.learned && result.learned.length > 0) {
                for (const entry of result.learned) {
                    const msg = entry.ai_analysis
                        ? `📚 AI: "${entry.source_phrase}" → "${entry.new_phrase}" (${entry.ai_analysis})`
                        : `📚 Glossary: "${entry.source_phrase}" → "${entry.new_phrase}"`;
                    this.notification.add(msg, { type: "info", sticky: false });
                }
            }
        } catch (e) {
            console.error("Failed to save translated text:", e);
        }
    }

    /**
     * Blur handler for individual <td> cells in the source table.
     * Reads ALL <td> texts from the parent <tr> and reconstructs [CELL] text.
     */
    async onSourceTableCellBlur(lineId, ev) {
        const tr = ev.target.closest("tr");
        if (!tr) return;

        const line = this.currentLines.find((l) => l.id === lineId);
        if (!line) return;

        const tds = tr.querySelectorAll("td");
        const newParaText = Array.from(tds).map((td) => {
            const clone = td.cloneNode(true);
            clone.querySelectorAll(".llm-table-cell-retranslate").forEach((el) => el.remove());
            return clone.innerText.trim();
        }).join(" [CELL] ");

        const oldPlain = (line.source_text || "").replace(/<[^>]*>?/gm, "").trim();
        const newPlain = newParaText.trim();

        if (oldPlain === newPlain) {
            // Restore HTML logic doesn't easily apply to tr as it involves multiple children,
            // but we can trust owl template re-rendering eventually or just leave it.
            // A simple refresh is to trigger state update for just this row if needed,
            // but usually it's fine.
            return;
        }

        line.source_text = newParaText;
        try {
            await rpc("/llm_translate/update_line", {
                line_id: lineId,
                source_text: newParaText,
            });
        } catch (e) {
            console.error("Failed to save source text:", e);
        }
    }

    /**
     * Blur handler for individual <td> cells in the translated table.
     * Reads ALL <td> texts from the parent <tr> and reconstructs [CELL] text.
     */
    async onTranslatedTableCellBlur(lineId, ev) {
        const tr = ev.target.closest("tr");
        if (!tr) return;

        const line = this.currentLines.find((l) => l.id === lineId);
        if (!line) return;

        const tds = tr.querySelectorAll("td");
        const newParaText = Array.from(tds).map(td => td.innerText.trim()).join(" [CELL] ");

        const oldPlain = (line.translated_text || "").replace(/<[^>]*>?/gm, "").trim();
        const newPlain = newParaText.trim();
        
        if (oldPlain === newPlain) {
            return;
        }

        line.translated_text = newParaText;
        line.state = "done";
        try {
            const result = await rpc("/llm_translate/update_line", {
                line_id: lineId,
                translated_text: newParaText,
            });
            if (result && result.learned && result.learned.length > 0) {
                for (const entry of result.learned) {
                    const msg = entry.ai_analysis
                        ? `📚 AI: "${entry.source_phrase}" → "${entry.new_phrase}" (${entry.ai_analysis})`
                        : `📚 Glossary: "${entry.source_phrase}" → "${entry.new_phrase}"`;
                    this.notification.add(msg, { type: "info", sticky: false });
                }
            }
        } catch (e) {
            console.error("Failed to save translated text:", e);
        }
    }

    // =========================================================================
    // Translation Actions (polling pattern)
    // =========================================================================

    async onStartTranslation() {
        if (!this.state.currentTranslation) return;

        // If there are error lines, reset them to pending so they get re-translated
        if (this.state.currentTranslation.state === "error") {
            try {
                await rpc("/llm_translate/retry_errors", {
                    translation_id: this.state.currentTranslation.id,
                });
                // Update local line states
                for (const line of (this.state.currentTranslation.lines || [])) {
                    if (line.state === "error") {
                        line.state = "pending";
                        line.translated_text = "";
                    }
                }
            } catch (e) {
                console.error("Failed to reset error lines:", e);
            }
        }

        this.state.isTranslating = true;
        this._abortTranslation = false;

        const translationId = this.state.currentTranslation.id;
        let consecutiveErrors = 0;
        let consecutiveLineErrors = 0;

        try {
            while (!this._abortTranslation) {
                let result;
                try {
                    result = await rpc("/llm_translate/translate_next", {
                        translation_id: translationId,
                    });
                } catch (e) {
                    consecutiveErrors++;
                    console.error(`translate_next error (${consecutiveErrors}):`, e);
                    if (consecutiveErrors >= 3) {
                        this.notification.add(
                            _t("Translation interrupted: connection lost. You can resume later."),
                            { type: "danger" },
                        );
                        break;
                    }
                    await this._submitDelay();
                    continue;
                }

                consecutiveErrors = 0;
                let updatedLineData = [];

                // Debug logging
                console.log("📡 translate_next response:", result);
                if (result.debug_info) {
                    console.log("🐛 Debug info from backend:", result.debug_info);
                }
                if (result.progress !== undefined) {
                    console.log(`📊 Progress: ${result.translated_lines}/${result.total_lines}`);
                    this.state.currentTranslation.progress = result.progress;
                    this.state.currentTranslation.total_lines = result.total_lines;
                    this.state.currentTranslation.translated_lines = result.translated_lines;
                }

                // Handle batch response: update ALL translated lines
                const linesData = result.lines_data || [];
                if (linesData.length > 0) {
                    updatedLineData = linesData;
                    console.log(`🔄 Updating ${linesData.length} lines from batch response`);
                    for (const ld of linesData) {
                        const line = this.state.currentTranslation.lines?.find(
                            (l) => l.id === ld.id
                        );
                        if (line) {
                            if (ld.state === "error") {
                                console.warn(`❌ Line ${ld.id} error: ${ld.translated_text}`);
                            }
                            line.translated_text = ld.translated_text;
                            line.state = ld.state;
                            line.reasoning = ld.reasoning || "";
                            // Sync OCR result for image_ocr lines
                            if (ld.image_ocr_result) {
                                line.image_ocr_result = ld.image_ocr_result;
                            }
                        }
                    }
                } else if (result.line_data) {
                    updatedLineData = [result.line_data];
                    // Backward compat: single line_data
                    console.log("🔄 Updating single line from line_data:", result.line_data);
                    const line = this.state.currentTranslation.lines?.find(
                        (l) => l.id === result.line_data.id
                    );
                    if (line) {
                        if (result.line_data.state === "error") {
                            console.warn(`❌ Line ${result.line_data.id} error: ${result.line_data.translated_text}`);
                        }
                        line.translated_text = result.line_data.translated_text;
                        line.state = result.line_data.state;
                        line.reasoning = result.line_data.reasoning || "";
                    }
                }

                for (const ld of [...updatedLineData].sort((a, b) => (a.sequence || 0) - (b.sequence || 0))) {
                    if (ld.state === "error") {
                        consecutiveLineErrors++;
                    } else if (ld.state === "done") {
                        consecutiveLineErrors = 0;
                    }

                    if (consecutiveLineErrors >= 5) {
                        const shouldStop = window.confirm(
                            _t("连续出现 5 个翻译错误，是否终止当前翻译？")
                        );
                        if (shouldStop) {
                            this._abortTranslation = true;
                            break;
                        }
                        consecutiveLineErrors = 0;
                    }
                }
                if (this._abortTranslation) {
                    break;
                }

                if (result.error && !result.finished) {
                    this.notification.add(result.error, { type: "warning" });
                }
                if (result.error && result.finished) {
                    this.notification.add(result.error, { type: "danger" });
                    console.error("❌ Translation finished with error:", result.error);
                    console.error("📊 Final result:", result);
                    break;
                }
                if (result.finished) {
                    console.log("✅ Translation finished!");
                    console.log("📊 Final result:", JSON.stringify(result, null, 2));
                    
                    await this._refreshStatus();
                    
                    console.log("📊 After refreshStatus - Current state:", JSON.stringify(this.state.currentTranslation, null, 2));
                    
                    // Debug: Show ALL lines with their states
                    const allLines = this.state.currentTranslation.lines || [];
                    console.log(`📋 Total lines in UI: ${allLines.length}`);
                    const lineStates = {
                        pending: allLines.filter((l) => l.state === "pending").length,
                        done: allLines.filter((l) => l.state === "done").length,
                        error: allLines.filter((l) => l.state === "error").length,
                        translating: allLines.filter((l) => l.state === "translating").length,
                    };
                    console.log(`📊 Line states breakdown:`, lineStates);
                    
                    const errorLines = allLines.filter((l) => l.state === "error") || [];
                    if (errorLines.length > 0) {
                        console.warn(`⚠️  Found ${errorLines.length} error lines:`);
                        errorLines.forEach((line) => {
                            console.warn(`   ID ${line.id}: "${line.source_text?.substring(0, 50)}..." -> state=${line.state}`);
                            console.warn(`   Error: ${line.translated_text?.substring(0, 100)}...`);
                            
                            // Special handling for image_ocr errors
                            if (line.line_type === "image_ocr") {
                                if (line.translated_text?.includes("multimodal")) {
                                    console.error(`❌ IMAGE OCR ERROR: Selected model does not support multimodal vision!`);
                                    console.error(`   Current model: ${this.state.currentTranslation.model_name}`);
                                    console.error(`   Provider: ${this.state.currentTranslation.provider_name}`);
                                    console.error(`   ⚠️  Please select a multimodal model (e.g., GPT-4o, Claude Sonnet) for image OCR`);
                                } else if (line.translated_text?.includes("400") || line.translated_text?.includes("BadRequestError")) {
                                    console.error(`❌ IMAGE OCR ERROR: Model compatibility issue!`);
                                    console.error(`   ${line.translated_text?.substring(0, 200)}`);
                                    console.error(`   ⚠️  Please ensure the provider and model are compatible`);
                                }
                            }
                        });
                    }
                    
                    const pendingLines = allLines.filter((l) => l.state === "pending");
                    if (pendingLines.length > 0) {
                        console.warn(`⚠️  Found ${pendingLines.length} pending (untranslated) lines:`);
                        pendingLines.forEach((line) => {
                            console.warn(`   ID ${line.id}: "${line.source_text?.substring(0, 50)}..." -> state=${line.state}`);
                        });
                    }
                    
                    if (this.state.currentTranslation?.state === "done") {
                        this.notification.add(_t("Translation completed!"), { type: "success" });
                        console.log("✅ Translation state: done");
                    } else if (this.state.currentTranslation?.state === "error") {
                        this.notification.add(
                            _t("Translation finished with errors. You can retry failed paragraphs."),
                            { type: "warning" },
                        );
                        console.warn("⚠️  Translation state: error");
                        console.warn("📋 Error message:", this.state.currentTranslation.error_message);
                        console.warn("📋 Total lines:", this.state.currentTranslation.total_lines);
                        console.warn("📋 Translated lines:", this.state.currentTranslation.translated_lines);
                    } else {
                        console.warn(`⚠️  Unexpected final state: "${this.state.currentTranslation?.state}"`);
                        console.warn(`📋 Total: ${this.state.currentTranslation.total_lines}, Translated: ${this.state.currentTranslation.translated_lines}, Remaining: ${this.state.currentTranslation.total_lines - this.state.currentTranslation.translated_lines}`);
                    }
                    break;
                }

                await this._submitDelay();
            }

            if (this._abortTranslation) {
                this.notification.add(_t("Translation paused."), { type: "info" });
                await this._refreshStatus();
            }
        } catch (e) {
            console.error("Translation loop error:", e);
            this.notification.add(_t("Translation error"), { type: "danger" });
            await this._refreshStatus();
        } finally {
            this.state.isTranslating = false;
            this._abortTranslation = false;
        }
    }

    onStopTranslation() {
        this._abortTranslation = true;
    }

    onOpenSettings() {
        this.state.submitIntervalInput = String(this.state.submitIntervalMs ?? DEFAULT_SUBMIT_INTERVAL_MS);
        this.state.showSettingsModal = true;
    }

    onCloseSettings() {
        this.state.showSettingsModal = false;
    }

    onSubmitIntervalInput(ev) {
        this.state.submitIntervalInput = ev.target.value;
    }

    onSaveSettings() {
        const raw = Number.parseInt(this.state.submitIntervalInput || "0", 10);
        if (!Number.isFinite(raw) || raw < 0 || raw > 60000) {
            this.notification.add(_t("Interval must be between 0 and 60000 ms."), {
                type: "warning",
            });
            return;
        }
        this.state.submitIntervalMs = raw;
        window.localStorage?.setItem(SUBMIT_INTERVAL_STORAGE_KEY, String(raw));
        this.state.showSettingsModal = false;
        this.notification.add(_t("Translation interval saved."), { type: "success" });
    }

    async onResetToDraft() {
        if (!this.state.currentTranslation?.id) return;
        try {
            const result = await rpc("/llm_translate/reset", {
                translation_id: this.state.currentTranslation.id,
            });
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }
            this.state.currentTranslation = result;
            this.notification.add(_t("Translation reset to draft."), { type: "info" });
        } catch (e) {
            console.error("Reset failed:", e);
            this.notification.add(_t("Failed to reset"), { type: "danger" });
        }
    }

    async onDownloadResult() {
        if (!this.state.currentTranslation?.id) return;

        // Temp users cannot download - show login modal
        if (this.state.isTempUser) {
            this.state.showLoginModal = true;
            return;
        }

        // Flush any pending OCR block saves
        await this._flushOcrPendingSaves();

        // For image translations: screenshot the preview pane directly.
        // This is true WYSIWYG — what you see is exactly what you get.
        if (this.state.currentTranslation.is_image) {
            await this._downloadImageScreenshot();
            return;
        }

        // For document translations: rebuild via backend
        const result = await rpc("/llm_translate/rebuild", {
            translation_id: this.state.currentTranslation.id,
            export_mode: this.state.translationDisplayMode === "bilingual" ? "bilingual" : "translated",
        });
        this.state.currentTranslation = result;

        if (result.result_filename) {
            const data = await this.orm.read(
                "llm.translation",
                [this.state.currentTranslation.id],
                ["result_attachment_id"],
            );
            if (data?.[0]?.result_attachment_id) {
                const attachId = data[0].result_attachment_id[0];
                window.open(`/web/content/${attachId}?download=true`, "_blank");
            }
        }
    }

    /**
     * Screenshot the right (translated) preview pane and trigger download.
     * Uses html2canvas for pixel-perfect WYSIWYG capture.
     */
    async _downloadImageScreenshot() {
        const pane = this.rootRef.el?.querySelector(".llm-translate-pane-right .pane-content");
        if (!pane) {
            this.notification.add("找不到预览面板", { type: "danger" });
            return;
        }

        try {
            // Dynamically load html2canvas
            const html2canvas = await this._loadHtml2Canvas();
            if (!html2canvas) {
                this.notification.add("截图库加载失败", { type: "danger" });
                return;
            }

            this.notification.add("正在生成截图...", { type: "info" });

            // Find the actual page content (skip gray background wrapper)
            const target = pane.querySelector(".llm-doc-page") || pane.querySelector("div > div") || pane;

            const canvas = await html2canvas(target, {
                backgroundColor: "#ffffff",
                scale: 2, // 2x for retina quality
                useCORS: true,
                allowTaint: true,
            });

            // Convert to blob and download
            canvas.toBlob((blob) => {
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                const name = (this.state.currentTranslation.source_filename || "translated").replace(/\.[^.]+$/, "");
                a.href = url;
                a.download = `${name}_translated.png`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            }, "image/png");
        } catch (e) {
            console.error("Screenshot failed:", e);
            this.notification.add("截图失败", { type: "danger" });
        }
    }

    /**
     * Dynamically load html2canvas library.
     */
    async _loadHtml2Canvas() {
        if (window.html2canvas) return window.html2canvas;
        return new Promise((resolve) => {
            const script = document.createElement("script");
            script.src = "https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js";
            script.onload = () => resolve(window.html2canvas);
            script.onerror = () => resolve(null);
            document.head.appendChild(script);
        });
    }

    // =========================================================================
    // Navigation
    // =========================================================================

    async _openTranslation(translationId) {
        try {
            const result = await rpc("/llm_translate/status", {
                translation_id: translationId,
            });
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }
            this.state.currentTranslation = result;
            this.state.viewMode = "detail";
            this.state.translationDisplayMode = "split";
            
            // Wait for DOM to update then initialize scroll sync
            setTimeout(() => {
                this._initializeScrollSync();
            }, 100);
        } catch (e) {
            console.error("Failed to open translation:", e);
        }
    }

    async _refreshStatus() {
        if (!this.state.currentTranslation?.id) return;
        try {
            const result = await rpc("/llm_translate/status", {
                translation_id: this.state.currentTranslation.id,
            });
            if (!result.error) {
                this.state.currentTranslation = result;
            }
        } catch (e) {
            console.error("Status refresh failed:", e);
        }
    }

    async onLoadMoreLines() {
        const t = this.state.currentTranslation;
        if (!t?.id || this.state.isLoadingMoreLines) return;
        const nextOffset = t.line_window?.next_offset ?? (t.lines || []).length;
        this.state.isLoadingMoreLines = true;
        try {
            const result = await rpc("/llm_translate/lines", {
                translation_id: t.id,
                line_offset: nextOffset,
            });
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }
            const existingIds = new Set((t.lines || []).map((line) => line.id));
            for (const line of result.lines || []) {
                if (!existingIds.has(line.id)) {
                    t.lines.push(line);
                    existingIds.add(line.id);
                }
            }
            t.line_window = result.line_window;
            setTimeout(() => {
                this._initializeScrollSync();
                this._updateTranslatedSlots();
                this._updateSourceSlots();
                this._initializeOcrDragHandlers();
            }, 50);
        } catch (e) {
            console.error("Failed to load more lines:", e);
            this.notification.add(_t("Failed to load more content"), { type: "danger" });
        } finally {
            this.state.isLoadingMoreLines = false;
        }
    }

    onOpenTranslation(translationId) {
        this._openTranslation(translationId);
    }

    onBackToList() {
        this.state.currentTranslation = null;
        this.state.viewMode = "list";
        this._loadTranslationList();
    }

    onOpenInForm() {
        if (!this.state.currentTranslation?.id) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            res_model: "llm.translation",
            res_id: this.state.currentTranslation.id,
            views: [[false, "form"]],
            target: "current",
        });
    }

    async onRetryLine(lineId) {
        if (!this.state.currentTranslation?.id) return;
        try {
            const result = await rpc("/llm_translate/retry_line", {
                translation_id: this.state.currentTranslation.id,
                line_id: lineId,
            });
            if (!result.error) {
                this.state.currentTranslation = result;
            } else {
                this.notification.add(result.error, { type: "danger" });
            }
        } catch (e) {
            console.error("Retry failed:", e);
        }
    }

    // =========================================================================
    // Delete Translation
    // =========================================================================

    async onDeleteTranslation(translationId, ev) {
        if (!confirm(_t("Are you sure you want to delete this translation? This will also remove all uploaded files and translated text."))) {
            return;
        }
        try {
            const result = await rpc("/llm_translate/delete", {
                translation_id: translationId,
            });
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                return;
            }
            this.notification.add(_t("Translation deleted."), { type: "success" });
            await this._loadTranslationList();
        } catch (e) {
            console.error("Delete failed:", e);
            this.notification.add(_t("Failed to delete translation"), { type: "danger" });
        }
    }

    // =========================================================================
    // Re-translate & Reasoning actions on translated paragraphs
    // =========================================================================

    async onRetranslateLine(lineId) {
        if (!this.state.currentTranslation?.id) return;

        const line = this.currentLines.find((l) => l.id === lineId);

        // Detect image-only paragraph: no source_text, not is_empty (has images)
        const isImagePara = line && !line.source_text && !line.is_empty;
        const paraIndex = line?.style_metadata?.para_index;

        if (line) {
            line.state = "translating";
            line.translated_text = "";
        }

        // For image paragraphs, also mark sibling OCR lines as translating
        let ocrLines = [];
        if (isImagePara && paraIndex !== null && paraIndex !== undefined) {
            ocrLines = this.currentLines.filter(
                (l) => l.line_type === "image_ocr" &&
                       l.style_metadata?.para_index === paraIndex
            );
            for (const ocrLine of ocrLines) {
                ocrLine.state = "translating";
                ocrLine.image_ocr_result = null;
            }
        }

        try {
            const result = await rpc(isImagePara ? "/llm_translate/retranslate_line" : "/llm_translate/retry_line", {
                translation_id: this.state.currentTranslation.id,
                line_id: lineId,
            });
            if (result.error) {
                this.notification.add(result.error, { type: "danger" });
                if (line) line.state = "error";
                for (const ocrLine of ocrLines) ocrLine.state = "error";
                return;
            }
            if (!isImagePara) {
                this.state.currentTranslation = result;
            } else if (result.is_image_para && result.ocr_lines_data) {
                // Update each OCR line from the returned data
                for (const ocrData of result.ocr_lines_data) {
                    const ocrLine = this.currentLines.find((l) => l.id === ocrData.id);
                    if (ocrLine) {
                        ocrLine.state = ocrData.state;
                        if (ocrData.image_ocr_result) {
                            ocrLine.image_ocr_result = ocrData.image_ocr_result;
                        }
                    }
                }
                if (line) line.state = "done";
            } else if (result.line_data && line) {
                line.translated_text = result.line_data.translated_text;
                line.state = result.line_data.state;
                line.reasoning = result.line_data.reasoning || "";
            }
            this.notification.add(_t("Paragraph re-translated."), { type: "success" });
        } catch (e) {
            console.error("Retranslate failed:", e);
            if (line) line.state = "error";
            for (const ocrLine of ocrLines) ocrLine.state = "error";
            this.notification.add(_t("Re-translation failed"), { type: "danger" });
        }
    }

    onShowReasoning(lineId) {
        const line = this.currentLines.find((l) => l.id === lineId);
        this.state.reasoningText = line?.reasoning || "";
        this.state.showReasoningModal = true;
    }

    onCloseReasoning() {
        this.state.showReasoningModal = false;
        this.state.reasoningText = "";
    }

    // =========================================================================
    // Glossary Panel
    // =========================================================================

    async onToggleGlossary() {
        this.state.showGlossaryPanel = !this.state.showGlossaryPanel;
        if (this.state.showGlossaryPanel) {
            await this.loadGlossaryEntries();
        }
    }

    onCloseGlossary() {
        this.state.showGlossaryPanel = false;
    }

    async loadGlossaryEntries() {
        try {
            const trans = this.state.currentTranslation;
            const entries = await rpc("/llm_translate/glossary/list", {
                source_lang: trans?.source_lang || "",
                target_lang: trans?.target_lang || "",
            });
            this.state.glossaryEntries = entries || [];
        } catch (e) {
            console.error("Failed to load glossary:", e);
            this.state.glossaryEntries = [];
        }
    }

    get filteredGlossaryEntries() {
        const filter = (this.state.glossaryFilter || "").toLowerCase();
        if (!filter) return this.state.glossaryEntries;
        return this.state.glossaryEntries.filter(
            (e) =>
                (e.source_phrase || "").toLowerCase().includes(filter) ||
                (e.source_text || "").toLowerCase().includes(filter) ||
                (e.new_phrase || "").toLowerCase().includes(filter) ||
                (e.translated_text || "").toLowerCase().includes(filter) ||
                (e.ai_analysis || "").toLowerCase().includes(filter),
        );
    }

    onGlossaryFilterInput(ev) {
        this.state.glossaryFilter = ev.target.value;
    }

    onGlossarySourceInput(ev) {
        this.state.glossaryNewSource = ev.target.value;
    }

    onGlossaryTranslatedInput(ev) {
        this.state.glossaryNewTranslated = ev.target.value;
    }

    async onAddGlossaryEntry() {
        const src = this.state.glossaryNewSource.trim();
        const tgt = this.state.glossaryNewTranslated.trim();
        if (!src || !tgt) {
            this.notification.add(_t("Please fill in both source and translated text."), {
                type: "warning",
            });
            return;
        }
        const trans = this.state.currentTranslation;
        try {
            await rpc("/llm_translate/glossary/add", {
                source_text: src,
                translated_text: tgt,
                source_lang: trans?.source_lang || "en",
                target_lang: trans?.target_lang || "zh",
            });
            this.state.glossaryNewSource = "";
            this.state.glossaryNewTranslated = "";
            await this.loadGlossaryEntries();
            this.notification.add(_t("Glossary entry added."), { type: "success" });
        } catch (e) {
            console.error("Failed to add glossary entry:", e);
            this.notification.add(_t("Failed to add glossary entry."), { type: "danger" });
        }
    }

    async onDeleteGlossaryEntry(entryId) {
        try {
            await rpc("/llm_translate/glossary/delete", { entry_id: entryId });
            await this.loadGlossaryEntries();
        } catch (e) {
            console.error("Failed to delete glossary entry:", e);
        }
    }

    async onUpdateGlossaryEntry(entryId, ev) {
        const newText = ev.target.innerText.trim();
        const entry = this.state.glossaryEntries.find((e) => e.id === entryId);
        if (!entry) return;
        const oldText = (entry.new_phrase || entry.translated_text || "").trim();
        if (oldText === newText) return;
        try {
            await rpc("/llm_translate/glossary/update", {
                entry_id: entryId,
                translated_text: newText,
            });
            entry.translated_text = newText;
            entry.new_phrase = newText;
        } catch (e) {
            console.error("Failed to update glossary entry:", e);
        }
    }

    /**
     * Show full context for a glossary entry (source paragraph, old/new translations).
     */
    onShowGlossaryContext(entry) {
        const lines = [];
        if (entry.context_source) lines.push(`📄 Source:\n${entry.context_source}`);
        if (entry.old_translated) lines.push(`📝 Original translation:\n${entry.old_translated}`);
        if (entry.new_translated) lines.push(`✅ Modified translation:\n${entry.new_translated}`);
        if (entry.ai_analysis) lines.push(`🤖 AI analysis: ${entry.ai_analysis}`);
        // Reuse the reasoning modal to display context
        this.state.reasoningText = lines.join("\n\n");
        this.state.showReasoningModal = true;
    }

    // =========================================================================
    // =========================================================================
    // OCR Overlay Dragging & Font-Size Adjustment (for image translation)
    // =========================================================================

    /**
     * Called when the OCR overlay font size +/- button is clicked.
     * Adjusts the font-size on the overlay element directly in the DOM
     * and persists the delta to the database.
     */
    onOcrFontSizeChange(ev, lineId, blockIndex, delta) {
        ev.stopPropagation();
        ev.preventDefault();

        const overlay = ev.target.closest(".llm-ocr-overlay");
        if (!overlay) return;

        // Read current font-size from the inline style
        const currentStyle = overlay.style.fontSize || "";
        const match = currentStyle.match(/(\d+)px/);
        let currentPx = match ? parseInt(match[1], 10) : 12;
        let newPx = Math.max(5, Math.min(48, currentPx + delta));

        // Only change font-size; box size is now controlled independently
        // via the resize handle (↘) at bottom-right corner.
        overlay.style.fontSize = newPx + "px";

        // Persist the font-size adjustment locally
        const key = `${lineId}_${blockIndex}`;
        if (!this._ocrFontAdjustments) {
            this._ocrFontAdjustments = {};
        }
        this._ocrFontAdjustments[key] = newPx;

        const ocrLineId = parseInt(overlay.dataset.ocrLineId, 10);
        if (ocrLineId) {
            this._saveOcrBlockToDb(ocrLineId, blockIndex, null, null, newPx, null, null);
        }
    }

    /**
     * Save OCR block position/font-size to the database via RPC.
     * Debounced: multiple rapid calls within 500ms are coalesced.
     */
    _saveOcrBlockToDb(lineId, blockIndex, xPct, yPct, fontSize, widthPct, heightPct) {
        const key = `ocr_save_${lineId}_${blockIndex}`;
        if (!this._ocrSaveTimers) this._ocrSaveTimers = {};

        // Merge pending updates
        if (!this._ocrPendingSaves) this._ocrPendingSaves = {};
        const pk = `${lineId}_${blockIndex}`;
        const pending = this._ocrPendingSaves[pk] || {};
        if (xPct !== null && xPct !== undefined) pending.x_pct = xPct;
        if (yPct !== null && yPct !== undefined) pending.y_pct = yPct;
        if (fontSize !== null && fontSize !== undefined) pending.font_size = fontSize;
        if (widthPct !== null && widthPct !== undefined) pending.w_pct = widthPct;
        if (heightPct !== null && heightPct !== undefined) pending.h_pct = heightPct;
        this._ocrPendingSaves[pk] = pending;

        // Debounce: clear existing timer and set a new one
        if (this._ocrSaveTimers[key]) {
            clearTimeout(this._ocrSaveTimers[key]);
        }
        this._ocrSaveTimers[key] = setTimeout(async () => {
            const p = this._ocrPendingSaves[pk];
            if (!p) return;
            delete this._ocrPendingSaves[pk];
            try {
                await rpc("/llm_translate/ocr_block/update", {
                    line_id: lineId,
                    block_index: blockIndex,
                    x_pct: p.x_pct,
                    y_pct: p.y_pct,
                    font_size: p.font_size,
                    w_pct: p.w_pct,
                    h_pct: p.h_pct,
                });
            } catch (e) {
                console.warn("Failed to save OCR block position:", e);
            }
        }, 500);
    }

    /**
     * Flush all pending OCR block saves immediately.
     * Called before download to ensure all adjustments are persisted.
     */
    async _flushOcrPendingSaves() {
        if (!this._ocrSaveTimers || !this._ocrPendingSaves) return;

        // Clear all timers to prevent duplicate saves
        for (const key of Object.keys(this._ocrSaveTimers)) {
            clearTimeout(this._ocrSaveTimers[key]);
            delete this._ocrSaveTimers[key];
        }

        // Fire all pending saves immediately
        const promises = [];
        for (const [pk, pending] of Object.entries(this._ocrPendingSaves)) {
            const [lineIdStr, blockIndexStr] = pk.split("_");
            const lineId = parseInt(lineIdStr, 10);
            const blockIndex = parseInt(blockIndexStr, 10);
            if (!lineId || isNaN(blockIndex)) continue;

            promises.push(
                rpc("/llm_translate/ocr_block/update", {
                    line_id: lineId,
                    block_index: blockIndex,
                    x_pct: pending.x_pct,
                    y_pct: pending.y_pct,
                    font_size: pending.font_size,
                    w_pct: pending.w_pct,
                    h_pct: pending.h_pct,
                }).catch((e) => {
                    console.warn("Failed to flush OCR block save:", e);
                })
            );
        }
        this._ocrPendingSaves = {};

        if (promises.length > 0) {
            await Promise.all(promises);
        }
    }

    /**
     * Called when the delete button (✕) on an OCR overlay is clicked.
     * Removes the text block from the DOM and deletes it from the database.
     */
    async onOcrBlockDelete(ev, lineId, blockIndex) {
        ev.stopPropagation();
        ev.preventDefault();

        const overlay = ev.target.closest(".llm-ocr-overlay");
        if (!overlay) return;

        const ocrLineId = parseInt(overlay.dataset.ocrLineId, 10);
        if (!ocrLineId) return;

        // Confirm deletion
        if (!confirm("确定要删除此译文文本框吗？")) return;

        // Remove from DOM immediately for responsive UI
        overlay.remove();

        // Remove from local state caches
        const key = `${lineId}_${blockIndex}`;
        if (this._ocrPosAdjustments) delete this._ocrPosAdjustments[key];
        if (this._ocrFontAdjustments) delete this._ocrFontAdjustments[key];
        if (this._ocrSizeAdjustments) delete this._ocrSizeAdjustments[key];
        if (this._ocrPendingSaves) delete this._ocrPendingSaves[`${lineId}_${blockIndex}`];

        // Delete from database
        try {
            await rpc("/llm_translate/ocr_block/delete", {
                line_id: ocrLineId,
                block_index: blockIndex,
            });
        } catch (e) {
            console.warn("Failed to delete OCR block:", e);
            this.notification.add("删除失败，请刷新页面重试", { type: "danger" });
        }
    }

    /**
     * Called when user finishes editing OCR translated text (blur).
     * Saves the new text to the database and triggers glossary learning.
     */
    async onOcrTextBlur(ev, lineId, blockIndex) {
        const span = ev.target;
        if (!span) return;

        const newText = (span.innerText || span.textContent || "").trim();
        if (!newText) return;

        // Find the block in currentLines to compare with old text
        const ocrLine = this.currentLines.find((l) => l.id === lineId);
        if (!ocrLine || !ocrLine.image_ocr_result) return;

        const blocks = ocrLine.image_ocr_result.text_blocks || [];
        if (blockIndex < 0 || blockIndex >= blocks.length) return;

        const oldText = (blocks[blockIndex].translated || "").trim();
        if (oldText === newText) return;

        // Update local state immediately for responsive UI
        blocks[blockIndex].translated = newText;

        // Save to database with glossary learning
        try {
            const result = await rpc("/llm_translate/ocr_block/update_text", {
                line_id: lineId,
                block_index: blockIndex,
                translated_text: newText,
            });
            if (result.learned && result.learned.length > 0) {
                this.notification.add(
                    `已学习 ${result.learned.length} 条翻译记忆`,
                    { type: "info" }
                );
            }
        } catch (e) {
            console.warn("Failed to save OCR block text:", e);
            // Revert on failure
            blocks[blockIndex].translated = oldText;
            span.innerText = oldText;
        }
    }

    /**
     * Initialize drag-to-reposition on OCR overlay elements.
     * Called from onMounted and onPatched to attach listeners to new overlays.
     */
    _initializeOcrDragHandlers() {
        const root = this.rootRef.el;
        if (!root) return;

        const overlays = root.querySelectorAll(".llm-ocr-draggable");
        for (const overlay of overlays) {
            if (overlay.dataset._ocrDragInit) continue;
            overlay.dataset._ocrDragInit = "1";

            overlay.addEventListener("mousedown", (ev) => {
                // Resize handle: change width/height, keep font-size unchanged
                if (ev.target.closest(".llm-ocr-resize-handle")) {
                    ev.preventDefault();
                    ev.stopPropagation();
                    const parentImg = overlay.closest("div[style*='position: relative']");
                    if (!parentImg) return;

                    const rect = parentImg.getBoundingClientRect();
                    const startX = ev.clientX;
                    const startY = ev.clientY;
                    const startW = parseFloat(overlay.style.width) || 10;
                    const startH = parseFloat(overlay.style.height) || 5;
                    const startLeft = parseFloat(overlay.style.left) || 0;
                    const startTop = parseFloat(overlay.style.top) || 0;

                    const onResizeMove = (moveEv) => {
                        const dw = ((moveEv.clientX - startX) / rect.width) * 100;
                        const dh = ((moveEv.clientY - startY) / rect.height) * 100;
                        overlay.style.width = Math.max(2, Math.min(100 - startLeft, startW + dw)) + "%";
                        overlay.style.height = Math.max(2, Math.min(100 - startTop, startH + dh)) + "%";
                    };

                    const onResizeUp = () => {
                        document.removeEventListener("mousemove", onResizeMove);
                        document.removeEventListener("mouseup", onResizeUp);
                        // Persist new size
                        const lineId = overlay.dataset.ocrLineId;
                        const blockIndex = overlay.dataset.ocrBlockIndex;
                        if (lineId != null && blockIndex != null) {
                            const key = `${lineId}_${blockIndex}`;
                            if (!this._ocrSizeAdjustments) {
                                this._ocrSizeAdjustments = {};
                            }
                            this._ocrSizeAdjustments[key] = {
                                width: parseFloat(overlay.style.width) || 10,
                                height: parseFloat(overlay.style.height) || 5,
                            };
                            const ocrLineId = parseInt(lineId, 10);
                            const bi = parseInt(blockIndex, 10);
                            if (ocrLineId) {
                                const newW = parseFloat(overlay.style.width) || 10;
                                const newH = parseFloat(overlay.style.height) || 5;
                                this._saveOcrBlockToDb(ocrLineId, bi, null, null, null, newW, newH);
                            }
                        }
                    };

                    document.addEventListener("mousemove", onResizeMove);
                    document.addEventListener("mouseup", onResizeUp);
                    return;
                }

                // Don't start drag on font-size buttons or editable text
                if (ev.target.closest(".llm-ocr-font-btn")) return;
                if (ev.target.closest("[contenteditable]")) return;

                ev.preventDefault();
                const parentImg = overlay.closest("div[style*='position: relative']");
                if (!parentImg) return;

                const rect = parentImg.getBoundingClientRect();
                const startX = ev.clientX;
                const startY = ev.clientY;
                const startLeft = parseFloat(overlay.style.left) || 0;
                const startTop = parseFloat(overlay.style.top) || 0;

                const onMouseMove = (moveEv) => {
                    const dx = ((moveEv.clientX - startX) / rect.width) * 100;
                    const dy = ((moveEv.clientY - startY) / rect.height) * 100;
                    overlay.style.left = Math.max(0, Math.min(100 - parseFloat(overlay.style.width) || 10, startLeft + dx)) + "%";
                    overlay.style.top = Math.max(0, Math.min(100 - parseFloat(overlay.style.height) || 5, startTop + dy)) + "%";
                };

                const onMouseUp = () => {
                    document.removeEventListener("mousemove", onMouseMove);
                    document.removeEventListener("mouseup", onMouseUp);
                    // Persist the new position locally
                    const lineId = overlay.dataset.ocrLineId;
                    const blockIndex = overlay.dataset.ocrBlockIndex;
                    if (lineId != null && blockIndex != null) {
                        const key = `${lineId}_${blockIndex}`;
                        if (!this._ocrPosAdjustments) {
                            this._ocrPosAdjustments = {};
                        }
                        this._ocrPosAdjustments[key] = {
                            left: overlay.style.left,
                            top: overlay.style.top,
                        };
                        // Save to database
                        const ocrLineId = parseInt(lineId, 10);
                        const bi = parseInt(blockIndex, 10);
                        if (ocrLineId) {
                            const newLeft = parseFloat(overlay.style.left) || 0;
                            const newTop = parseFloat(overlay.style.top) || 0;
                            this._saveOcrBlockToDb(ocrLineId, bi, newLeft, newTop, null);
                        }
                    }
                };

                document.addEventListener("mousemove", onMouseMove);
                document.addEventListener("mouseup", onMouseUp);
            });

            // Touch support
            overlay.addEventListener("touchstart", (ev) => {
                if (ev.target.closest(".llm-ocr-font-btn")) return;
                if (ev.target.closest("[contenteditable]")) return;

                ev.preventDefault();
                const touch = ev.touches[0];
                const parentImg = overlay.closest("div[style*='position: relative']");
                if (!parentImg) return;

                const rect = parentImg.getBoundingClientRect();
                const startX = touch.clientX;
                const startY = touch.clientY;
                const startLeft = parseFloat(overlay.style.left) || 0;
                const startTop = parseFloat(overlay.style.top) || 0;

                const onTouchMove = (moveEv) => {
                    const moveTouch = moveEv.touches[0];
                    const dx = ((moveTouch.clientX - startX) / rect.width) * 100;
                    const dy = ((moveTouch.clientY - startY) / rect.height) * 100;
                    overlay.style.left = Math.max(0, Math.min(100 - parseFloat(overlay.style.width) || 10, startLeft + dx)) + "%";
                    overlay.style.top = Math.max(0, Math.min(100 - parseFloat(overlay.style.height) || 5, startTop + dy)) + "%";
                };

                const onTouchEnd = () => {
                    document.removeEventListener("touchmove", onTouchMove);
                    document.removeEventListener("touchend", onTouchEnd);
                    // Persist the new position locally + to database
                    const lineId = overlay.dataset.ocrLineId;
                    const blockIndex = overlay.dataset.ocrBlockIndex;
                    if (lineId != null && blockIndex != null) {
                        const key = `${lineId}_${blockIndex}`;
                        if (!this._ocrPosAdjustments) {
                            this._ocrPosAdjustments = {};
                        }
                        this._ocrPosAdjustments[key] = {
                            left: overlay.style.left,
                            top: overlay.style.top,
                        };
                        const ocrLineId = parseInt(lineId, 10);
                        const bi = parseInt(blockIndex, 10);
                        if (ocrLineId) {
                            const newLeft = parseFloat(overlay.style.left) || 0;
                            const newTop = parseFloat(overlay.style.top) || 0;
                            this._saveOcrBlockToDb(ocrLineId, bi, newLeft, newTop, null);
                        }
                    }
                };

                document.addEventListener("touchmove", onTouchMove, { passive: false });
                document.addEventListener("touchend", onTouchEnd);
            });
        }
    }

    /**
     * Check if OCR is done for a specific image but no text was detected.
     * Used to show a "no text detected" overlay on the translated side.
     *
     * @param {Object} line - The paragraph line object.
     * @param {number} imageIndex - The index of the image within the paragraph.
     */
    isImageOcrDoneButEmpty(line, imageIndex) {
        const meta = line.style_metadata || {};
        const paraIndex = meta.para_index;
        if (paraIndex === null || paraIndex === undefined) return false;
        if (imageIndex === null || imageIndex === undefined) imageIndex = 0;

        // Find image_ocr lines for this paragraph AND this specific image
        const ocrLines = this.currentLines.filter(
            (l) => l.line_type === "image_ocr" &&
                   l.style_metadata?.para_index === paraIndex &&
                   (l.style_metadata?.image_index ?? 0) === imageIndex &&
                   l.state === "done"
        );
        if (ocrLines.length === 0) return false;

        // Check if ALL matching OCR lines have empty text_blocks
        return ocrLines.every((ocrLine) => {
            const ocrResult = ocrLine.image_ocr_result;
            if (!ocrResult) return true; // No result at all = empty
            const blocks = ocrResult.text_blocks || [];
            // Check if there are no blocks with translated text
            return !blocks.some((b) => b.translated && b.translated.trim());
        });
    }

    /**
     * Get the adjusted OCR block style, merging the computed style with
     * any user drag/font-size adjustments (from local state and database).
     */
    getAdjustedOcrBlockStyle(block, lineId, blockIndex) {
        const baseStyle = this.getOcrBlockStyle(block);
        const key = `${lineId}_${blockIndex}`;

        let style = baseStyle;

        // Apply persisted font-size adjustment (from local state — A+/A- clicks)
        const fontAdj = this._ocrFontAdjustments?.[key];
        if (fontAdj) {
            style = style.replace(/font-size:\d+px/, `font-size:${fontAdj}px`);
        }

        // Also check if block has font_size_px from DB (saved from previous session)
        if (!fontAdj && block.font_size_px) {
            style = style.replace(/font-size:\d+px/, `font-size:${block.font_size_px}px`);
        }

        // Apply persisted position adjustment (from local state)
        const posAdj = this._ocrPosAdjustments?.[key];
        if (posAdj) {
            style = style.replace(/left:\d+(\.\d+)?%/, `left:${posAdj.left}`);
            style = style.replace(/top:\d+(\.\d+)?%/, `top:${posAdj.top}`);
        }

        // Apply persisted size adjustment (width/height from resize handle drag)
        const sizeAdj = this._ocrSizeAdjustments?.[key];
        if (sizeAdj) {
            style = style.replace(/width:\d+(\.\d+)?%/, `width:${sizeAdj.width}%`);
            style = style.replace(/height:\d+(\.\d+)?%/, `height:${sizeAdj.height}%`);
        }

        return style;
    }

    // Helpers
    // =========================================================================

    _formatFileSize(size) {
        const units = ["B", "KB", "MB", "GB"];
        let value = Number(size || 0);
        for (const unit of units) {
            if (value < 1024 || unit === units[units.length - 1]) {
                return unit === "B" ? `${Math.round(value)} ${unit}` : `${value.toFixed(1)} ${unit}`;
            }
            value /= 1024;
        }
        return "0 B";
    }

    _formatDuration(seconds) {
        seconds = Math.max(0, Math.ceil(Number(seconds || 0)));
        if (seconds < 60) return `${seconds}s`;
        const minutes = Math.floor(seconds / 60);
        const rest = seconds % 60;
        return rest ? `${minutes}m ${rest}s` : `${minutes}m`;
    }

    _resetUploadProgress() {
        this.state.uploadProgress = 0;
        this.state.uploadPhase = "";
        this.state.uploadSpeedText = "";
        this.state.uploadEtaText = "";
    }

    _validateUploadFile(file) {
        if (!file) return false;
        if (file.size > MAX_UPLOAD_BYTES) {
            this.notification.add(
                _t(
                    "文件过大：%s 为 %s。当前单文件上传/抽取上限是 %s。请先拆分/压缩文件，或分批上传后再翻译。",
                    file.name,
                    this._formatFileSize(file.size),
                    this._formatFileSize(MAX_UPLOAD_BYTES),
                ),
                { type: "warning", sticky: true },
            );
            return false;
        }
        return true;
    }

    _createWithBinaryUpload(file, vals) {
        const formData = new FormData();
        formData.append("vals", JSON.stringify(vals));
        formData.append("file", file, file.name);
        return this._uploadWithProgress("/llm_translate/create_binary", formData, file.size);
    }

    _uploadBinaryToTranslation(translationId, file) {
        const formData = new FormData();
        formData.append("translation_id", String(translationId));
        formData.append("file", file, file.name);
        return this._uploadWithProgress("/llm_translate/upload_binary", formData, file.size);
    }

    _uploadWithProgress(url, formData, totalBytes) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const startedAt = Date.now();
            this.state.uploadPhase = _t("Uploading file to filestore...");
            this.state.uploadProgress = 0;

            xhr.upload.onprogress = (ev) => {
                if (!ev.lengthComputable) return;
                const elapsed = Math.max((Date.now() - startedAt) / 1000, 0.1);
                const speed = ev.loaded / elapsed;
                const remainingBytes = Math.max((totalBytes || ev.total || 0) - ev.loaded, 0);
                const remainingSeconds = speed > 0 ? remainingBytes / speed : 0;
                this.state.uploadProgress = Math.min(100, Math.round((ev.loaded / ev.total) * 100));
                this.state.uploadSpeedText = `${this._formatFileSize(speed)}/s`;
                this.state.uploadEtaText = remainingBytes > 0 ? this._formatDuration(remainingSeconds) : _t("processing");
                if (this.state.uploadProgress >= 100) {
                    this.state.uploadPhase = _t("Upload complete. Extracting document...");
                }
            };

            xhr.onload = () => {
                let payload = {};
                try {
                    payload = JSON.parse(xhr.responseText || "{}");
                } catch (err) {
                    reject(new Error(xhr.responseText || xhr.statusText || "Upload failed"));
                    return;
                }
                if (xhr.status >= 200 && xhr.status < 300) {
                    resolve(payload);
                } else {
                    resolve(payload.error ? payload : { error: xhr.statusText || "Upload failed" });
                }
            };
            xhr.onerror = () => reject(new Error("Upload failed"));
            xhr.open("POST", url, true);
            xhr.send(formData);
        });
    }

    _readFileAsBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result.split(",")[1]);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }

    getLineStateIcon(state) {
        const icons = {
            pending: "fa-clock-o text-muted",
            translating: "fa-spinner fa-spin text-primary",
            done: "fa-check text-success",
            error: "fa-exclamation-triangle text-danger",
        };
        return icons[state] || "fa-question text-muted";
    }

    getStateBadgeClass(state) {
        const classes = {
            draft: "text-bg-info",
            extracting: "text-bg-warning",
            translating: "text-bg-primary",
            done: "text-bg-success",
            error: "text-bg-danger",
        };
        return classes[state] || "text-bg-secondary";
    }

    formatDate(dateString) {
        if (!dateString) return "";
        // Odoo returns datetimes as UTC strings without timezone indicator:
        //   searchRead →  "2026-04-11 08:30:00"  (space, no tz)
        //   isoformat() → "2026-04-11T08:30:00"  (T, no tz)
        // new Date() treats both as LOCAL time, so no UTC→local conversion happens.
        // Normalize to ISO 8601 + "Z" so the browser treats it as UTC and
        // toLocaleString() automatically converts to the user's local timezone.
        let normalized = dateString.replace(" ", "T");
        if (!normalized.endsWith("Z") && !/[+-]\d{2}:\d{2}$/.test(normalized)) {
            normalized += "Z";
        }
        const date = new Date(normalized);
        return date.toLocaleDateString() + " " + date.toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
        });
    }

    /**
     * Get display label for a language code.
     */
    getLangLabel(code) {
        const lang = this.languages.find((l) => l.value === code);
        return lang ? lang.label : code || "";
    }
}

registry.category("actions").add("llm_translate.translation_view", LLMTranslationView);
