/** @odoo-module **/

/**
 * Math Renderer Utility for KaTeX and LaTeX Support
 * 
 * This module handles rendering of mathematical expressions in LLM responses.
 * It supports both block and inline LaTeX/KaTeX expressions.
 * 
 * Features:
 * - Auto-loads KaTeX from CDN if not already loaded
 * - Renders $...$ (inline) and $$...$$ (display) LaTeX
 * - Renders \(...\) (inline) and \[...\] (display) LaTeX
 * - Handles script[type="math/tex"] tags from markdown processors
 * - Graceful fallback if KaTeX unavailable
 */

class MathRenderer {
    constructor() {
        this.katexLoaded = false;
        this.renderQueue = [];
        this.autoRenderLoaded = false;
        this._initialized = false;
        this._observing = false;
        // Don't load KaTeX eagerly - wait until actually needed
    }

    /**
     * Lazily initialize KaTeX only when first needed
     */
    _ensureInitialized() {
        if (this._initialized) return;
        this._initialized = true;
        this.initKaTeX();
    }

    /**
     * Initialize KaTeX library from CDN
     */
    initKaTeX() {
        // Check if KaTeX is already loaded globally
        if (window.katex) {
            this.katexLoaded = true;
            // Check if auto-render is available
            if (window.renderMathInElement) {
                this.autoRenderLoaded = true;
                this._processQueue();
            } else {
                this._loadAutoRender();
            }
            return;
        }

        // Load KaTeX CSS
        const katexCss = document.createElement('link');
        katexCss.rel = 'stylesheet';
        katexCss.href = 'https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.css';
        document.head.appendChild(katexCss);

        // Load KaTeX JS
        const katexScript = document.createElement('script');
        katexScript.src = 'https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/katex.min.js';
        katexScript.async = true;
        katexScript.onload = () => {
            this.katexLoaded = true;
            this._loadAutoRender();
        };
        katexScript.onerror = () => {
            console.warn('[MathRenderer] Failed to load KaTeX from CDN');
            this.katexLoaded = false;
        };
        document.head.appendChild(katexScript);
    }

    /**
     * Load KaTeX auto-render extension
     */
    _loadAutoRender() {
        if (this.autoRenderLoaded || !this.katexLoaded) return;

        const autoRenderScript = document.createElement('script');
        autoRenderScript.src = 'https://cdn.jsdelivr.net/npm/katex@0.16.8/dist/contrib/auto-render.min.js';
        autoRenderScript.async = true;
        autoRenderScript.onload = () => {
            this.autoRenderLoaded = true;
            this._processQueue();
        };
        autoRenderScript.onerror = () => {
            console.warn('[MathRenderer] Failed to load KaTeX auto-render');
        };
        document.head.appendChild(autoRenderScript);
    }

    /**
     * Process queued renders
     */
    _processQueue() {
        while (this.renderQueue.length > 0) {
            const element = this.renderQueue.shift();
            this.renderElement(element);
        }
    }

    /**
     * Render math expressions in a given DOM element
     * @param {HTMLElement} element - The element to search for math expressions
     */
    renderElement(element) {
        if (!element) return;

        // Lazily initialize KaTeX on first use
        this._ensureInitialized();

        // If KaTeX not loaded yet, queue for later
        if (!this.katexLoaded) {
            this.renderQueue.push(element);
            return;
        }

        try {
            // Use KaTeX auto-render for automatic LaTeX detection
            if (window.renderMathInElement && this.autoRenderLoaded) {
                window.renderMathInElement(element, {
                    delimiters: [
                        { left: '$$', right: '$$', display: true },
                        { left: '\\[', right: '\\]', display: true },
                        { left: '$', right: '$', display: false },
                        { left: '\\(', right: '\\)', display: false },
                    ],
                    throwOnError: false,
                    strict: 'ignore',
                });
            } else if (window.katex) {
                // Fallback: manually render script[type="math/tex"] tags
                this._renderMathScripts(element);
            }
        } catch (error) {
            console.warn('[MathRenderer] Error rendering math in element:', error);
        }
    }

    /**
     * Manually render math/tex script tags
     * @param {HTMLElement} element - The element containing math/tex scripts
     */
    _renderMathScripts(element) {
        if (!window.katex) return;

        const mathScripts = element.querySelectorAll('script[type="math/tex"]');
        mathScripts.forEach((script) => {
            const latex = script.textContent;
            const isDisplay = script.type === 'math/tex; mode=display';
            
            try {
                const span = document.createElement('span');
                span.className = 'katex-render';
                const html = window.katex.renderToString(latex, {
                    displayMode: isDisplay,
                    throwOnError: false,
                    strict: 'ignore',
                });
                span.innerHTML = html;
                script.parentNode.replaceChild(span, script);
            } catch (error) {
                console.warn('[MathRenderer] Failed to render LaTeX:', latex, error);
            }
        });
    }

    /**
     * Watch for new math content and render it
     * Useful for dynamically inserted content (e.g., streaming messages)
     * Uses a debounced approach to avoid interfering with OWL's DOM management
     */
    observeNewContent() {
        if (this._observing) return null;
        this._observing = true;
        // Use a simple polling approach instead of MutationObserver
        // to avoid interfering with OWL's virtual DOM
        // IMPORTANT: Only scan within LLM containers to avoid freezing other pages
        setInterval(() => {
            // Only scan within LLM-related containers
            const llmContainers = document.querySelectorAll('.o-llm-thread, .o-llm-chat-container, .o-llm-message');
            if (llmContainers.length === 0) return;

            llmContainers.forEach((container) => {
                // Find all unrendered math/tex elements
                const mathScripts = container.querySelectorAll('script[type="math/tex"]:not([data-rendered])');
                mathScripts.forEach((script) => {
                    this.renderElement(script.parentElement);
                    script.setAttribute('data-rendered', 'true');
                });

                // Find all .llm-math elements and ensure they're rendered
                const mathDivs = container.querySelectorAll('.llm-math:not([data-rendered])');
                mathDivs.forEach((div) => {
                    this.renderElement(div);
                    div.setAttribute('data-rendered', 'true');
                });
            });
        }, 1000); // Check every 1 second

        return null; // No observer to return
    }
}

// Initialize singleton (lazy - KaTeX won't load until first use)
const mathRenderer = new MathRenderer();

export default mathRenderer;
