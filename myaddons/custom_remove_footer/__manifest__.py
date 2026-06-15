{
    "name": "Custom Remove Odoo Branding",
    "version": "1.1",
    "depends": ["web", "portal", "sale", "account"],
    "data": ["views/templates.xml"],
    "assets": {
        "web.assets_backend": [
            "custom_remove_footer/static/src/js/remove_odoo_branding.js",
            "custom_remove_footer/static/src/scss/remove_odoo_branding.scss",
        ],
        "web.assets_frontend": [
            "custom_remove_footer/static/src/js/remove_odoo_branding.js",
            "custom_remove_footer/static/src/scss/remove_odoo_branding.scss",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
}
