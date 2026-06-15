/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";

/**
 * Patch for mail.Message component
 * Adds copy-to-clipboard functionality for tables in LLM responses.
 * When user clicks the "复制到 Excel" button, the table content is
 * copied as TSV (tab-separated values) which can be pasted into Excel.
 */
patch(Message.prototype, {
    setup() {
        super.setup();
        // Only bind table copy buttons for LLM thread messages
        if (this.props.message?.model === "llm.thread") {
            setTimeout(() => this._bindTableCopyButtons(), 200);
        }
    },

    _bindTableCopyButtons() {
        /**
         * Find all .llm-table-copy-btn buttons in this message and attach
         * click handlers that convert the adjacent <table> to TSV and copy
         * it to the clipboard.
         */
        try {
            const messageEl = this.el;
            if (!messageEl) return;

            const wrappers = messageEl.querySelectorAll('.llm-table-wrapper');
            for (const wrapper of wrappers) {
                const btn = wrapper.querySelector('.llm-table-copy-btn');
                const table = wrapper.querySelector('table');
                if (!btn || !table) continue;

                // Avoid binding twice
                if (btn.dataset.bound) continue;
                btn.dataset.bound = "1";

                btn.addEventListener('click', async (ev) => {
                    ev.preventDefault();
                    ev.stopPropagation();

                    try {
                        const tsv = this._tableToTSV(table);
                        await navigator.clipboard.writeText(tsv);

                        // Visual feedback
                        const origHTML = btn.innerHTML;
                        btn.innerHTML = '<i class="fa fa-check"></i> 已复制';
                        btn.classList.add('llm-table-copy-btn--success');
                        setTimeout(() => {
                            btn.innerHTML = origHTML;
                            btn.classList.remove('llm-table-copy-btn--success');
                        }, 2000);
                    } catch (err) {
                        console.error('[LLM Table Copy] Failed:', err);
                        // Fallback: select the table text
                        const range = document.createRange();
                        range.selectNodeContents(table);
                        const sel = window.getSelection();
                        sel.removeAllRanges();
                        sel.addRange(range);
                    }
                });
            }
        } catch (error) {
            console.debug('[LLM Table Patch] Error binding copy buttons:', error);
        }
    },

    _tableToTSV(tableEl) {
        /**
         * Convert an HTML <table> element to TSV (tab-separated values) string.
         * This format is directly pasteable into Excel, Google Sheets, etc.
         *
         * @param {HTMLTableElement} tableEl
         * @returns {string} TSV content
         */
        const rows = [];

        // Process thead
        const thead = tableEl.querySelector('thead');
        if (thead) {
            for (const tr of thead.querySelectorAll('tr')) {
                const cells = [];
                for (const cell of tr.querySelectorAll('th, td')) {
                    cells.push(this._cleanCellText(cell.textContent));
                }
                rows.push(cells.join('\t'));
            }
        }

        // Process tbody
        const tbody = tableEl.querySelector('tbody') || tableEl;
        for (const tr of tbody.querySelectorAll('tr')) {
            const cells = [];
            for (const cell of tr.querySelectorAll('td, th')) {
                cells.push(this._cleanCellText(cell.textContent));
            }
            rows.push(cells.join('\t'));
        }

        return rows.join('\n');
    },

    _cleanCellText(text) {
        /**
         * Clean cell text for TSV output:
         * - Trim whitespace
         * - Replace tabs and newlines with spaces
         * - Remove leading/trailing quotes if any
         */
        return (text || '')
            .trim()
            .replace(/\t/g, ' ')
            .replace(/\n/g, ' ')
            .replace(/\r/g, '');
    },
});
