/** @odoo-module **/

import { registry } from "@web/core/registry";

const USER_MENU_KEYS = ["documentation", "support", "odoo_account"];
const ODOO_HOST_RE = /(^|\.)odoo\.com$/i;
const BRAND_TEXT_RE =
    /(powered\s+by\s+odoo|powered\s+by|\u7531\s*odoo\s*\u63d0\u4f9b\u652f\u6301|odoo\s*s\.?a\.?|my\s+odoo\.com\s+account)/i;

function removeUserMenuBrandItems() {
    const menuItems = registry.category("user_menuitems");
    for (const key of USER_MENU_KEYS) {
        if (menuItems.contains(key)) {
            menuItems.remove(key);
        }
    }
}

registry.category("user_menuitems").addEventListener("UPDATE", () => {
    setTimeout(removeUserMenuBrandItems);
});

function isOdooUrl(value) {
    if (!value) {
        return false;
    }
    try {
        return ODOO_HOST_RE.test(new URL(value, window.location.origin).hostname);
    } catch {
        return /odoo\.com/i.test(value);
    }
}

function removableContainer(el) {
    return (
        el.closest(".o_brand_promotion") ||
        el.closest(".o_footer_copyright") ||
        el.closest(".o_database_list .card-body > .border-top") ||
        el.closest(".dropdown-item") ||
        el.closest("small") ||
        el
    );
}

function removeBrandElement(el) {
    const target = removableContainer(el);
    if (target && target.parentNode) {
        target.remove();
    }
}

function cleanupBranding(root = document) {
    if (document.title && /odoo/i.test(document.title)) {
        document.title = document.title.replace(/odoo/gi, "TS");
    }

    const brandSelector = [
        ".o_brand_promotion",
        ".o_database_list .card-body > .border-top",
        "a[href*='odoo.com']",
        "a[href*='accounts.odoo.com']",
        "img[src*='odoo_logo']",
        "img[alt='Odoo']",
        "[title*='Odoo']",
        "[aria-label*='Odoo']",
    ].join(",");
    const brandNodes = [
        ...(root.matches?.(brandSelector) ? [root] : []),
        ...root.querySelectorAll(brandSelector),
    ];
    for (const el of brandNodes) {
        if (el.href && !isOdooUrl(el.href)) {
            continue;
        }
        removeBrandElement(el);
    }

    const textSelector =
        ".o_footer, .o_footer_copyright, .o_database_list, small, .dropdown-menu, .modal, .o_setting_box";
    const textNodes = [
        ...(root.matches?.(textSelector) ? [root] : []),
        ...root.querySelectorAll(textSelector),
    ];
    for (const el of textNodes) {
        if (BRAND_TEXT_RE.test(el.textContent || "")) {
            removeBrandElement(el);
        }
    }
}

removeUserMenuBrandItems();

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => cleanupBranding());
} else {
    cleanupBranding();
}

const observer = new MutationObserver((mutations) => {
    for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
            if (node.nodeType === Node.ELEMENT_NODE) {
                cleanupBranding(node);
            }
        }
    }
});

observer.observe(document.documentElement, {
    childList: true,
    subtree: true,
});
