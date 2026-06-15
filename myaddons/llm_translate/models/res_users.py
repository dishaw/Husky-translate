from odoo import api, models


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def _llm_translate_set_translation_home_action(self):
        translation_action = self.env.ref(
            "llm_translate.action_llm_translation_client",
            raise_if_not_found=False,
        )
        if not translation_action:
            return False

        self.with_context(active_test=False).sudo().search([("share", "=", False)]).write({
            "action_id": translation_action.id,
        })
        return True
