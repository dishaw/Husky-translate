import json
import logging

from odoo import _, api, http
from odoo.exceptions import MissingError
from odoo.http import Response, request
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


class LLMThreadController(http.Controller):
    @http.route(
        "/llm/thread/<int:thread_id>/update",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def llm_thread_update(self, thread_id, **kwargs):
        try:
            thread = request.env["llm.thread"].browse(thread_id)
            if not thread.exists():
                raise MissingError(_("LLM Thread not found."))
            thread.write(kwargs)
            return {"status": "success"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    def _safe_yield(data_to_yield):
        """Helper generator to yield data safely, handling BrokenPipeError(Disconnected user)."""
        try:
            yield data_to_yield
            return True
        except BrokenPipeError:
            return False
        except Exception:
            return False

    @classmethod
    def _llm_thread_generate(cls, dbname, env, thread_id, user_message_body, **kwargs):
        """Generate LLM responses with streaming and safe yielding."""
        with Registry(dbname).cursor() as cr:
            env = api.Environment(cr, env.uid, env.context)
            llm_thread = env["llm.thread"].browse(int(thread_id))
            if not llm_thread.exists():
                yield from cls._safe_yield(
                    f"data: {json.dumps({'type': 'error', 'error': 'LLM Thread not found.'})}\n\n".encode(),
                )
                return

            client_connected = True
            try:
                for response in llm_thread.generate(user_message_body, **kwargs):
                    json_data = json.dumps(response, default=str)
                    success = yield from cls._safe_yield(
                        f"data: {json_data}\n\n".encode(),
                    )
                    if not success:
                        client_connected = False
                        break

            except GeneratorExit:
                client_connected = False

            except Exception as e:
                _logger.exception(
                    f"Error in llm_thread_generate for thread {thread_id}: {e}",
                )
                # Lock will be automatically released by context manager

                if client_connected:
                    success = yield from cls._safe_yield(
                        f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n".encode(),
                    )
                    if not success:
                        client_connected = False

            finally:
                if client_connected:
                    yield from cls._safe_yield(
                        f"data: {json.dumps({'type': 'done'})}\n\n".encode(),
                    )

    @http.route(
        "/llm/thread/prepare_message",
        type="json",
        auth="user",
        methods=["POST"],
        csrf=True,
    )
    def llm_thread_prepare_message(self, thread_id, message=None, attachment_ids=None):
        """Pre-save a user message before starting SSE streaming.

        This avoids putting large message content in the URL query string
        which causes 414 URI Too Long errors with long messages.

        Returns a short token that the frontend passes to the GET SSE endpoint.
        """
        import hashlib
        import time

        thread = request.env["llm.thread"].browse(int(thread_id))
        if not thread.exists():
            return {"error": "Thread not found"}

        # Store pending message in a transient cache on the thread
        token = hashlib.md5(
            f"{thread_id}-{request.env.uid}-{time.time()}".encode()
        ).hexdigest()[:16]

        # Store in session so it's user-scoped and auto-expires
        pending_key = f"llm_pending_{token}"
        request.session[pending_key] = {
            "thread_id": int(thread_id),
            "message": message or "",
            "attachment_ids": attachment_ids or [],
        }
        return {"token": token}

    @http.route("/llm/thread/generate", type="http", auth="user", csrf=True)
    def llm_thread_generate(
        self,
        thread_id,
        message=None,
        attachment_ids=None,
        token=None,
        **kwargs,
    ):
        headers = {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }

        # If a token is provided, retrieve the pre-saved message
        if token:
            pending_key = f"llm_pending_{token}"
            pending = request.session.pop(pending_key, None)
            if pending:
                message = pending.get("message") or message
                attachment_ids = pending.get("attachment_ids") or attachment_ids
                thread_id = pending.get("thread_id", thread_id)

        parsed_attachment_ids = []
        if attachment_ids:
            if isinstance(attachment_ids, str):
                parsed_attachment_ids = [
                    int(x) for x in attachment_ids.split(",") if x.strip().isdigit()
                ]
            elif isinstance(attachment_ids, list):
                parsed_attachment_ids = [int(x) for x in attachment_ids]
        return Response(
            self._llm_thread_generate(
                request.cr.dbname,
                request.env,
                thread_id,
                message,
                attachment_ids=parsed_attachment_ids,
                **kwargs,
            ),
            direct_passthrough=True,
            headers=headers,
        )
