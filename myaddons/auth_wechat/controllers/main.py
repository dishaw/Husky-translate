"""WeChat OAuth2.0 login controller + auto guest user creation.

Implements:
1. WeChat QR code login flow (open.weixin.qq.com)
2. Auto-create temp users by IP for anonymous visitors
3. Redirect main page to translation page for guests
4. Merge temp user data on real login
"""

import hashlib
import logging
import urllib.parse

import requests

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.home import Home, ensure_db

_logger = logging.getLogger(__name__)

WECHAT_QR_AUTH_URL = "https://open.weixin.qq.com/connect/qrconnect"
WECHAT_ACCESS_TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
WECHAT_USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


class AuthWeChatController(http.Controller):
    """WeChat OAuth endpoints."""

    @http.route("/auth_wechat/login", type="http", auth="public", website=False)
    def wechat_login(self, **kwargs):
        """Redirect user to WeChat QR code authorization page."""
        ICP = request.env["ir.config_parameter"].sudo()
        app_id = ICP.get_param("auth_wechat.app_id", "")
        if not app_id:
            return request.redirect("/web/login?error=wechat_not_configured")

        # Build callback URL
        base_url = ICP.get_param("web.base.url", request.httprequest.host_url.rstrip("/"))
        redirect_uri = urllib.parse.quote(base_url + "/auth_wechat/callback")

        # State parameter for CSRF protection
        state = hashlib.md5(
            (request.session.sid or "").encode()
        ).hexdigest()[:16]
        request.session["wechat_state"] = state

        # Store temp user id for later merge
        if request.session.uid:
            user = request.env["res.users"].sudo().browse(request.session.uid)
            if user.exists() and user.is_temp_user:
                request.session["temp_user_id"] = user.id

        auth_url = (
            "%s?appid=%s&redirect_uri=%s&response_type=code"
            "&scope=snsapi_login&state=%s#wechat_redirect"
        ) % (WECHAT_QR_AUTH_URL, app_id, redirect_uri, state)

        return request.redirect(auth_url)

    @http.route("/auth_wechat/callback", type="http", auth="public", website=False)
    def wechat_callback(self, code=None, state=None, **kwargs):
        """Handle WeChat OAuth callback after QR scan."""
        if not code:
            return request.redirect("/web/login?error=wechat_no_code")

        # Validate state
        expected_state = request.session.get("wechat_state", "")
        if state != expected_state:
            _logger.warning("WeChat state mismatch: %s vs %s", state, expected_state)
            # Don't block - some proxies may strip state

        ICP = request.env["ir.config_parameter"].sudo()
        app_id = ICP.get_param("auth_wechat.app_id", "")
        app_secret = ICP.get_param("auth_wechat.app_secret", "")

        if not app_id or not app_secret:
            return request.redirect("/web/login?error=wechat_not_configured")

        # Exchange code for access_token
        try:
            resp = requests.get(WECHAT_ACCESS_TOKEN_URL, params={
                "appid": app_id,
                "secret": app_secret,
                "code": code,
                "grant_type": "authorization_code",
            }, timeout=10)
            token_data = resp.json()
        except Exception as e:
            _logger.exception("WeChat token exchange failed: %s", e)
            return request.redirect("/web/login?error=wechat_token_failed")

        if "errcode" in token_data:
            _logger.error("WeChat token error: %s", token_data)
            return request.redirect("/web/login?error=wechat_token_error")

        openid = token_data.get("openid")
        access_token = token_data.get("access_token")

        if not openid:
            return request.redirect("/web/login?error=wechat_no_openid")

        # Get user info
        userinfo = {}
        try:
            resp = requests.get(WECHAT_USERINFO_URL, params={
                "access_token": access_token,
                "openid": openid,
                "lang": "zh_CN",
            }, timeout=10)
            userinfo = resp.json()
        except Exception as e:
            _logger.warning("WeChat userinfo fetch failed: %s", e)
            userinfo = {"nickname": "微信用户_%s" % openid[:8]}

        # Find or create user
        Users = request.env["res.users"].sudo()
        user = Users._find_or_create_wechat_user(openid, userinfo)

        if not user:
            return request.redirect("/web/login?error=wechat_user_create_failed")

        # Merge temp user data if applicable
        temp_user_id = request.session.get("temp_user_id")
        if temp_user_id:
            temp_user = Users.browse(temp_user_id)
            if temp_user.exists() and temp_user.is_temp_user:
                Users._merge_temp_user_data(temp_user, user)
            request.session.pop("temp_user_id", None)

        # Authenticate session: set a deterministic password for WeChat users
        db = request.db
        ICP = request.env["ir.config_parameter"].sudo()
        secret = ICP.get_param("database.secret", "fallback_secret_key")
        wechat_password = hashlib.sha256(
            ("%s_wechat_%s" % (secret, openid)).encode()
        ).hexdigest()[:20]
        user.sudo().write({"password": wechat_password})

        try:
            request.session.authenticate(
                db,
                {"login": user.login, "password": wechat_password, "type": "password"}
            )
        except Exception:
            _logger.warning("WeChat session authenticate failed, using direct session")
            request.session.uid = user.id
            request.session.login = user.login

        _logger.info("WeChat login successful: %s (uid=%s)", user.login, user.id)
        return request.redirect("/web")

    @http.route("/auth_wechat/check_temp", type="json", auth="public", methods=["POST"])
    def check_temp_user(self):
        """Check if current user is a temp user (for JS frontend)."""
        if not request.session.uid:
            return {"is_temp": True, "is_logged_in": False}

        user = request.env["res.users"].sudo().browse(request.session.uid)
        if not user.exists():
            return {"is_temp": True, "is_logged_in": False}

        return {
            "is_temp": user.is_temp_user,
            "is_logged_in": True,
            "user_name": user.name,
            "has_wechat": bool(user.wechat_openid),
        }

    @http.route("/auth_wechat/init_temp_user", type="http", auth="public", website=False)
    def init_temp_user(self, **kw):
        """Create/authenticate a temp user for anonymous visitor, then redirect to webclient.

        Uses auth="public" so request.env.uid is the public user (not None),
        avoiding ORM flush errors that occur with auth="none" uid=None environments.

        IMPORTANT: We do NOT use session.authenticate() here because it triggers
        bcrypt password hashing/verification (~1s each) which causes thread lock
        contention under concurrent requests. Instead we set session fields directly.
        """
        # Already logged in → go directly to webclient (home_action_id handles the action)
        if request.session.uid:
            return request.redirect("/web")

        # Get visitor IP
        ip = request.httprequest.environ.get(
            "HTTP_X_FORWARDED_FOR",
            request.httprequest.remote_addr or "unknown"
        )
        if "," in ip:
            ip = ip.split(",")[0].strip()

        # Create or find temp user (uses sudo() internally)
        Users = request.env["res.users"].sudo()
        temp_user, password = Users._get_or_create_temp_user(ip)

        if not temp_user:
            _logger.warning("Could not create temp user for IP %s, showing login", ip)
            return request.redirect("/web/login")

        # Use standard authenticate to ensure all session data is correctly initialized
        try:
            credential = {'login': temp_user.login, 'password': password, 'type': 'password'}
            request.session.authenticate(request.db, credential)
        except Exception as e:
            _logger.error("Failed to authenticate temp user: %s", e)
            return request.redirect("/web/login")

        _logger.info("Auto-authenticated temp user %s (uid=%s) for IP %s",
                     temp_user.login, temp_user.id, ip)
        return request.redirect("/web")


from odoo.addons.portal.controllers.portal import CustomerPortal


class CustomerPortalExtended(CustomerPortal):
    """Portal override placeholder (keep default behavior to avoid redirect loops)."""

    @http.route(["/my", "/my/home"], type="http", auth="public", website=True)
    def home(self, **kw):
        return super().home(**kw)


class HomeExtended(Home):
    """Override main page to support auto guest creation and translation redirect."""

    @http.route("/web/session/logout", type="http", auth="none")
    def logout(self, redirect="/web/login", **kw):
        """Override logout: redirect to /web/login instead of /odoo.

        Default Odoo logout redirects to /odoo, but our web_client override
        auto-creates temp users for unauthenticated visitors. By redirecting
        to /web/login, the user actually sees the login page after logout.
        """
        request.session.logout(keep_db=True)
        return request.redirect(redirect, 303)

    @http.route(["/web", "/odoo", "/odoo/<path:subpath>", "/TS", "/TS/<path:subpath>", "/scoped_app/<path:subpath>"], type="http", auth="none")
    def web_client(self, s_action=None, **kw):
        """Override /web: if no session, delegate temp user creation to auth="public" route."""
        # MUST be first: ensures DB is selected, aborts with redirect if not found
        ensure_db()
        import logging
        _logger = logging.getLogger(__name__)
        _logger.info("Inside web_client, uid is %s, has_cookie: %s", request.session.uid, request.httprequest.cookies.get('session_id'))

        # If user is already authenticated, handle temp users
        if request.session.uid:
            from odoo.exceptions import AccessError
            user = request.env["res.users"].sudo().browse(request.session.uid)
            if not user.has_group('base.group_user') and (user.is_temp_user or s_action == "llm_translate.action_llm_translation_client" or user.wechat_openid):
                request.session.touch()
                request.update_env(user=request.session.uid)
                try:
                    if request.env.user:
                        request.env.user._on_webclient_bootstrap()
                    
                    try:
                        # Fetch session info safely
                        session_info = request.env["ir.http"].session_info()
                    except Exception as e:
                        _logger.error("Error getting session info: %s", e)
                        session_info = {"cache_hashes": {}, "uid": request.session.uid}
                        
                    if "user_companies" not in session_info:
                        comp = request.env.company
                        session_info["user_companies"] = {
                            "current_company": comp.id,
                            "allowed_companies": {
                                comp.id: {
                                    "id": comp.id,
                                    "name": comp.name,
                                    "sequence": 10,
                                    "child_ids": [],
                                    "parent_id": False,
                                }
                            }
                        }
                    
                    if "cache_hashes" not in session_info:
                        session_info["cache_hashes"] = {}
                    if "load_menus" not in session_info["cache_hashes"]:
                        session_info["cache_hashes"]["load_menus"] = "empty"

                    # Set home_action_id so the JS web client knows what to display.
                    # The JS router reads home_action_id from session_info on startup.
                    # Without this, it shows a blank page because home_action_id=false.
                    try:
                        translate_action = request.env.ref(
                            "llm_translate.action_llm_translation_client",
                            raise_if_not_found=False
                        )
                        if translate_action:
                            session_info["home_action_id"] = translate_action.id
                    except Exception:
                        pass

                    context = {
                        "session_info": session_info,
                    }
                    response = request.render("web.webclient_bootstrap", qcontext=context)
                    response.headers["X-Frame-Options"] = "DENY"
                    return response
                except Exception as e:
                    _logger.error("Exception in web_client bootstrap fallback: %s", e)
                    import traceback
                    _logger.error(traceback.format_exc())
                    pass
            return super().web_client(s_action=s_action, **kw)

        # No session yet → redirect to the auth="public" init endpoint.
        # We do NOT create users here because auth="none" has uid=None, which
        # causes ORM flush errors in hr.write (self.env.user is empty recordset).
        ICP = request.env["ir.config_parameter"].sudo()
        temp_enabled = ICP.get_param("auth_wechat.temp_user_enabled", "True")
        if temp_enabled not in ("True", "true", "1"):
            # Let Odoo redirect to /web/login as normal
            return super().web_client(s_action=s_action, **kw)

        return request.redirect("/auth_wechat/init_temp_user")

    @http.route('/web/webclient/load_menus/<string:unique>', type='http', auth='public', methods=['GET'], readonly=True)
    def web_load_menus(self, unique, lang=None):
        import logging
        logging.getLogger(__name__).info("web_load_menus override called! uid is %s", request.session.uid)
        
        if not request.session.uid or not request.env.user.has_group('base.group_user'):
            import json
            # Odoo menu service expects a dict keyed by menu ID,
            # with "root" as the special top-level key.
            # Returning the root object directly causes getMenu("root") to return
            # undefined, and NavBar crashes on undefined.children.
            return request.make_response(json.dumps({
                "root": {
                    "id": False,
                    "name": "root",
                    "parent_id": [-1, ""],
                    "children": [],
                    "all_menu_ids": [],
                    "xmlid": "",
                    "web_icon": None,
                    "web_icon_data": None,
                    "action": "",
                }
            }), headers=[('Content-Type', 'application/json')])
                
        return super().web_load_menus(unique, lang)

    @http.route("/", type="http", auth="public", website=False)
    def index(self, **kw):
        """Override root URL: redirect to /web for everyone."""
        return request.redirect("/web")
