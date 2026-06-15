/** @odoo-module **/
import { registry } from "@web/core/registry";
import { ConfirmationDialog } from "@web/core/confirmation_dialog/confirmation_dialog";

/**
 * 简化 Odoo 删除确认对话框的啰嗦文案。
 *
 * Odoo 18 标准的删除确认消息包含多段文字：
 *   "准备好让你的记录消失得无影无踪了吗？确定吗？"
 *   "一旦删除，它将永远消失！"
 *   "在点击'删除'按钮之前请三思！"
 *
 * 导致对话框臃肿，且可能因 CSS 隐藏导致确认按钮不可见。
 * 本 patch 通过包装 dialog service 的 add 方法，拦截 ConfirmationDialog，
 * 将冗长的 body 替换为简洁的提示，同时确保确认/取消按钮始终可用。
 */

const dialogServiceDef = registry.category("services").get("dialog");

if (dialogServiceDef) {
    const originalStart = dialogServiceDef.start;

    dialogServiceDef.start = function (env, deps) {
        const dialogService = originalStart.call(this, env, deps);
        const originalAdd = dialogService.add.bind(dialogService);

        dialogService.add = function (DialogClass, props = {}) {
            if (DialogClass === ConfirmationDialog) {
                const body = props.body || "";
                // 检测是否是 Odoo 标准的删除确认消息
                if (typeof body === "string" && (
                    body.includes("消失得无影无踪") ||
                    body.includes("Are you sure you want to delete") ||
                    body.includes("delete this record")
                )) {
                    props = { ...props, body: "确定要删除吗？此操作不可撤销。" };
                }
                // 确保按钮标签始终有值（防止 CSS 隐藏导致按钮消失）
                if (!props.confirmLabel) {
                    props.confirmLabel = "删除";
                }
                if (!props.cancelLabel) {
                    props.cancelLabel = "取消";
                }
            }
            return originalAdd(DialogClass, props);
        };

        return dialogService;
    };
}
