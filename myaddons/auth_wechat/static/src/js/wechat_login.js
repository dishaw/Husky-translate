/**
 * WeChat Login frontend JS for login page enhancements.
 * Replaces "Odoo" branding with "TS" on the login page.
 */
(function () {
    "use strict";

    // Wait for DOM to be ready
    document.addEventListener("DOMContentLoaded", function () {
        // Replace "Odoo" branding text on login page
        _replaceBranding();
    });

    function _replaceBranding() {
        // Replace page title
        if (document.title.toLowerCase().includes("odoo")) {
            document.title = document.title.replace(/odoo/gi, "TS");
        }

        // Replace login page brand link/text
        var brandElements = document.querySelectorAll(
            ".o_login_brand, .oe_login_brand, a[href*='odoo.com']"
        );
        brandElements.forEach(function (el) {
            if (el.textContent.toLowerCase().includes("odoo")) {
                el.textContent = el.textContent.replace(/odoo/gi, "TS");
            }
            // Remove links to odoo.com
            if (el.href && el.href.includes("odoo.com")) {
                el.href = "#";
                el.removeAttribute("target");
            }
        });

        // Replace "Powered by Odoo" in footer
        var footerElements = document.querySelectorAll(
            ".o_footer, .o_footer_info, footer, .text-muted"
        );
        footerElements.forEach(function (el) {
            if (el.innerHTML.toLowerCase().includes("odoo")) {
                el.innerHTML = el.innerHTML.replace(/odoo/gi, "TS");
            }
        });

        // Replace in all links containing 'odoo'
        var allLinks = document.querySelectorAll("a");
        allLinks.forEach(function (a) {
            if (a.textContent.toLowerCase().includes("odoo")) {
                a.textContent = a.textContent.replace(/Odoo/g, "TS").replace(/odoo/g, "TS");
            }
            if (a.href && a.href.includes("odoo.com")) {
                a.href = "#";
            }
        });

        // Replace in meta tags
        var metaDesc = document.querySelector('meta[name="description"]');
        if (metaDesc && metaDesc.content.toLowerCase().includes("odoo")) {
            metaDesc.content = metaDesc.content.replace(/odoo/gi, "TS");
        }
    }
})();
