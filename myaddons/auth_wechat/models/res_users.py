"""res.users extensions for WeChat login and temp user support."""

import hashlib
import logging
import uuid

from odoo import api, fields, models

_logger = logging.getLogger(__name__)


class ResUsers(models.Model):
    _inherit = "res.users"

    wechat_openid = fields.Char(
        string="WeChat OpenID",
        index=True,
        copy=False,
        help="WeChat Open Platform unique user identifier",
    )
    wechat_unionid = fields.Char(
        string="WeChat UnionID",
        index=True,
        copy=False,
        help="WeChat UnionID for cross-platform identification",
    )
    wechat_nickname = fields.Char(
        string="WeChat Nickname",
        copy=False,
    )
    wechat_avatar = fields.Char(
        string="WeChat Avatar URL",
        copy=False,
    )
    is_temp_user = fields.Boolean(
        string="Temporary User",
        default=False,
        help="Auto-created temporary user from IP visit. "
             "Limited permissions: can translate but not download.",
    )
    temp_ip = fields.Char(
        string="Temp User IP",
        copy=False,
    )

    @api.model
    def _get_or_create_temp_user(self, ip_address):
        """Find or create a temporary user for the given IP address.

        Returns (user, password) tuple.
        """
        login = "guest_%s" % ip_address.replace(".", "_").replace(":", "_")

        # Search existing temp user for this IP
        user = self.sudo().search([
            ("login", "=", login),
            ("is_temp_user", "=", True),
        ], limit=1)

        # Generate deterministic password from IP + db secret
        secret = self.env["ir.config_parameter"].sudo().get_param(
            "database.secret", "fallback_secret_key"
        )
        password = hashlib.sha256(
            ("%s_%s_temp_guest" % (secret, ip_address)).encode()
        ).hexdigest()[:20]

        if user:
            # Backward compatibility: some old temp users were created with
            # portal/internal groups. Normalize to portal group every time.
            portal_group = self.env.ref("base.group_portal")
            if user.groups_id != portal_group:
                user.sudo().write({"groups_id": [(6, 0, [portal_group.id])]})
            # User already exists, return it.
            # Do NOT rewrite password here - bcrypt hashing is very expensive
            # (~1s) and causes thread lock contention under concurrent requests.
            # Password is deterministic from IP+secret, set only at creation.
            return user, password

        # Create new temp user
        try:
            portal_group = self.env.ref("base.group_portal")
            main_company = self.env["res.company"].sudo().search([], order="id asc", limit=1)
            user = self.sudo().create({
                "name": "访客_%s" % ip_address,
                "login": login,
                "password": password,
                "is_temp_user": True,
                "temp_ip": ip_address,
                "active": True,
                "company_id": main_company.id,
                "company_ids": [(4, main_company.id)],
                "groups_id": [(6, 0, [portal_group.id])],
            })
            _logger.info("Created temp user %s for IP %s", login, ip_address)
            return user, password
        except Exception as e:
            _logger.exception("Failed to create temp user for IP %s: %s", ip_address, e)
            return None, None

    @api.model
    def _find_or_create_wechat_user(self, openid, userinfo):
        """Find or create a user based on WeChat openid.

        Args:
            openid: WeChat openid string.
            userinfo: Dict with nickname, headimgurl, unionid, etc.

        Returns:
            res.users record.
        """
        # Search by openid
        user = self.sudo().search([
            ("wechat_openid", "=", openid),
        ], limit=1)

        if user:
            # Update info
            vals = {}
            nickname = userinfo.get("nickname")
            if nickname:
                vals["wechat_nickname"] = nickname
                vals["name"] = nickname
            avatar = userinfo.get("headimgurl")
            if avatar:
                vals["wechat_avatar"] = avatar
            unionid = userinfo.get("unionid")
            if unionid:
                vals["wechat_unionid"] = unionid
            if vals:
                user.sudo().write(vals)
            return user

        # Create new user from WeChat info
        nickname = userinfo.get("nickname", "微信用户_%s" % openid[:8])
        password = str(uuid.uuid4())

        try:
            portal_group = self.env.ref("base.group_portal")
            main_company = self.env["res.company"].sudo().search([], order="id asc", limit=1)

            user = self.sudo().create({
                "name": nickname,
                "login": "wechat_%s" % openid,
                "password": password,
                "wechat_openid": openid,
                "wechat_unionid": userinfo.get("unionid", ""),
                "wechat_nickname": nickname,
                "wechat_avatar": userinfo.get("headimgurl", ""),
                "is_temp_user": False,
                "active": True,
                "company_id": main_company.id,
                "company_ids": [(4, main_company.id)],
                "groups_id": [(6, 0, [portal_group.id])],
            })
            _logger.info(
                "Created new WeChat user: %s (openid=%s)",
                nickname, openid,
            )
            return user
        except Exception as e:
            _logger.exception("Failed to create WeChat user: %s", e)
            return None

    @api.model
    def _merge_temp_user_data(self, temp_user, real_user):
        """Transfer translation records from temp user to real user.

        Called after WeChat login when there was a temp user session.
        """
        if not temp_user or not real_user:
            return
        if temp_user.id == real_user.id:
            return

        try:
            # Transfer llm.translation records
            translations = self.env["llm.translation"].sudo().search([
                ("user_id", "=", temp_user.id),
            ])
            if translations:
                translations.sudo().write({"user_id": real_user.id})
                _logger.info(
                    "Transferred %d translations from temp user %s to %s",
                    len(translations), temp_user.login, real_user.login,
                )

            # Deactivate temp user
            temp_user.sudo().write({"active": False})
        except Exception as e:
            _logger.warning("Failed to merge temp user data: %s", e)
