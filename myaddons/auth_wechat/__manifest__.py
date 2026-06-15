{
    "name": "WeChat QR Login & Auto Guest",
    "summary": "微信扫码登录(自动注册) + 匿名临时用户自动创建",
    "description": """
WeChat QR Code Login for Odoo 18
================================
- 微信开放平台扫码登录 (OAuth2.0)
- 扫码后自动注册新用户
- 未登录访客自动创建IP临时用户
- 临时用户可使用翻译功能，下载需登录
- 登录后自动合并临时用户翻译记录
    """,
    "category": "Authentication",
    "version": "18.0.1.0.0",
    "depends": [
        "base",
        "web",
        "auth_signup",
        "portal",
        "sale",
        "account",
    ],
    "author": "Custom",
    "data": [
        "security/ir.model.access.csv",
        "security/auth_wechat_security.xml",
        "data/ir_config_parameter_data.xml",
        "views/res_config_settings_views.xml",
        "views/auth_wechat_templates.xml",
    ],
    "assets": {
        "web.assets_frontend": [
            "auth_wechat/static/src/css/wechat_login.css",
            "auth_wechat/static/src/js/wechat_login.js",
        ],
    },
    "license": "LGPL-3",
    "installable": True,
    "application": False,
    "auto_install": False,
}
