/** @odoo-module **/

import { patch } from "@web/core/utils/patch";
import { Message } from "@mail/core/common/message";

import mathRenderer from "../utils/math_renderer";

/**
 * Patch for mail.Message component
 * Renders math expressions after message content is displayed
 */
patch(Message.prototype, {
    setup() {
        super.setup();
        // Only schedule math rendering for LLM thread messages
        if (this.props.message?.model === "llm.thread") {
            setTimeout(() => this._renderMessageMath(), 100);
        }
    },

    async _renderMessageMath() {
        /**
         * Render math expressions in this message after it's displayed
         * This is called after the component is mounted
         */
        try {
            // Only process LLM messages
            if (this.props.message?.model !== "llm.thread") return;
            // Find the message element in the DOM
            const messageElement = this.el?.querySelector('.o-mail-Message-content');
            if (messageElement && mathRenderer) {
                mathRenderer.renderElement(messageElement);
            }
        } catch (error) {
            console.debug('[LLM Message Patch] Error rendering math:', error);
        }
    }
});
