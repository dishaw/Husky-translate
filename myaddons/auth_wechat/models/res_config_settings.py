"""Odoo System Settings for WeChat login configuration."""

from odoo import api, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    wechat_app_id = fields.Char(
        string="WeChat AppID",
        config_parameter="auth_wechat.app_id",
        help="微信开放平台网站应用的AppID",
    )
    wechat_app_secret = fields.Char(
        string="WeChat AppSecret",
        config_parameter="auth_wechat.app_secret",
        help="微信开放平台网站应用的AppSecret",
    )
    wechat_login_enabled = fields.Boolean(
        string="Enable WeChat Login",
        config_parameter="auth_wechat.login_enabled",
        default=True,
        help="是否在登录页显示微信扫码登录",
    )
    temp_user_enabled = fields.Boolean(
        string="Enable Guest Access",
        config_parameter="auth_wechat.temp_user_enabled",
        default=True,
        help="是否允许匿名访客自动创建临时用户使用翻译功能",
    )
