(function () {
    function send(message) {
        var payload = Object.assign({ source: "oopm-office-ai-bridge" }, message);
        var sent = [];
        function post(target) {
            if (!target) return;
            for (var i = 0; i < sent.length; i++) {
                if (sent[i] === target) return;
            }
            sent.push(target);
            try {
                target.postMessage(payload, "*");
            } catch (e) {}
        }
        try {
            var w = window;
            for (var level = 0; level < 10; level++) {
                if (!w.parent || w.parent === w) break;
                w = w.parent;
                post(w);
            }
        } catch (e) {}
        try {
            post(window.top);
        } catch (e) {}
        try {
            post(window.opener);
        } catch (e) {}
    }

    function callCommand(fn, isCalc) {
        return new Promise(function (resolve, reject) {
            if (!window.Asc || !Asc.plugin || typeof Asc.plugin.callCommand !== "function") {
                reject(new Error("OnlyOffice plugin command API is not ready"));
                return;
            }
            var done = false;
            var timer = setTimeout(function () {
                if (done) return;
                done = true;
                reject(new Error("OnlyOffice command timeout"));
            }, 30000);
            try {
                Asc.plugin.callCommand(fn, false, isCalc !== false, function (result) {
                    if (done) return;
                    done = true;
                    clearTimeout(timer);
                    resolve(result);
                });
            } catch (e) {
                if (done) return;
                done = true;
                clearTimeout(timer);
                reject(e);
            }
        });
    }

    function parseCommandJson(value) {
        if (typeof value !== "string") {
            return value || {};
        }
        return JSON.parse(value || "{}");
    }

    async function getPages(pageFrom, pageTo) {
        Asc.scope.oopmPageFrom = pageFrom;
        Asc.scope.oopmPageTo = pageTo;
        var raw = await callCommand(function () {
            var doc = Api.GetDocument();
            if (!doc || typeof doc.GetAllParagraphs !== "function") {
                return JSON.stringify({ error: "OnlyOffice document API does not expose paragraph access." });
            }
            if (typeof doc.GoToPage !== "function" || typeof doc.GetPageCount !== "function") {
                return JSON.stringify({ error: "OnlyOffice document API does not expose page navigation." });
            }
            var pageCount = Math.max(1, doc.GetPageCount() || 1);
            var fromPage = Math.max(1, parseInt(Asc.scope.oopmPageFrom, 10) || 1);
            var toPage = Math.max(fromPage, parseInt(Asc.scope.oopmPageTo, 10) || fromPage);
            fromPage = Math.min(fromPage, pageCount);
            toPage = Math.min(toPage, pageCount);
            var fromIndex = fromPage - 1;
            var toIndex = toPage - 1;
            var currentPage = 1;
            try {
                currentPage = (doc.GetCurrentPage ? doc.GetCurrentPage() : fromIndex) + 1;
            } catch (e) {}

            var paragraphs = doc.GetAllParagraphs() || [];
            var indexById = {};
            for (var i = 0; i < paragraphs.length; i++) {
                if (paragraphs[i] && typeof paragraphs[i].GetInternalId === "function") {
                    indexById[paragraphs[i].GetInternalId()] = i;
                }
            }
            function pageStart(pageIndex) {
                if (pageIndex >= pageCount) return paragraphs.length;
                if (!doc.GoToPage(pageIndex)) return Math.floor(paragraphs.length * pageIndex / pageCount);
                var cur = doc.GetCurrentParagraph ? doc.GetCurrentParagraph() : null;
                if (cur && typeof cur.GetInternalId === "function") {
                    var id = cur.GetInternalId();
                    if (Object.prototype.hasOwnProperty.call(indexById, id)) return indexById[id];
                }
                return Math.floor(paragraphs.length * pageIndex / pageCount);
            }

            var starts = {};
            for (var page = fromIndex; page <= toIndex + 1; page++) {
                starts[page] = pageStart(page);
            }
            var pages = [];
            var lastEnd = -1;
            for (var p = fromIndex; p <= toIndex; p++) {
                var start = Math.max(0, starts[p] || 0);
                var end = Math.max(start, starts[p + 1] == null ? paragraphs.length : starts[p + 1]);
                if (start < lastEnd) start = lastEnd;
                if (end < start) end = start;
                var lines = [];
                for (var j = start; j < end && j < paragraphs.length; j++) {
                    var para = paragraphs[j];
                    if (!para || typeof para.GetText !== "function") continue;
                    var text = para.GetText({ Numbering: false }) || "";
                    lines.push({
                        id: para.GetInternalId ? para.GetInternalId() : String(j),
                        index: j,
                        type: para.GetParentTableCell && para.GetParentTableCell() ? "table_cell" : "paragraph",
                        text: text
                    });
                }
                pages.push({ page: p + 1, lines: lines });
                lastEnd = end;
            }
            try {
                doc.GoToPage(Math.max(0, Math.min(currentPage - 1, pageCount - 1)));
            } catch (e) {}
            return JSON.stringify({ pages: pages, page_count: pageCount, current_page: currentPage });
        }, false);
        return parseCommandJson(raw);
    }

    async function writeLines(items, restorePage) {
        Asc.scope.oopmWriteItems = items || [];
        Asc.scope.oopmRestorePage = restorePage || 1;
        var raw = await callCommand(function () {
            var doc = Api.GetDocument();
            if (!doc || typeof doc.GetAllParagraphs !== "function") {
                return JSON.stringify({ ok: false, error: "OnlyOffice document API does not expose paragraph access." });
            }
            var paragraphs = doc.GetAllParagraphs() || [];
            var byId = {};
            for (var i = 0; i < paragraphs.length; i++) {
                if (paragraphs[i] && typeof paragraphs[i].GetInternalId === "function") {
                    byId[paragraphs[i].GetInternalId()] = paragraphs[i];
                }
            }
            var updated = 0;
            var items = Asc.scope.oopmWriteItems || [];
            for (var j = 0; j < items.length; j++) {
                var item = items[j] || {};
                var para = byId[item.id] || null;
                if (!para && typeof item.index === "number") para = paragraphs[item.index];
                if (!para) continue;
                var newText = String(item.text || "");
                var handled = false;
                // Try run-level replacement to preserve font/formatting
                if (typeof para.GetElementsCount === "function" && typeof para.GetElement === "function") {
                    var count = para.GetElementsCount();
                    var runs = [];
                    for (var k = 0; k < count; k++) {
                        var el = para.GetElement(k);
                        if (el && typeof el.GetClassType === "function" &&
                            el.GetClassType() === "run" &&
                            typeof el.SetText === "function") {
                            runs.push(el);
                        }
                    }
                    if (runs.length > 0) {
                        // Put all translated text in the first run (preserves its formatting)
                        runs[0].SetText(newText);
                        // Clear remaining runs
                        for (var r = 1; r < runs.length; r++) {
                            runs[r].SetText("");
                        }
                        handled = true;
                    }
                }
                // Fallback: SetText on the whole paragraph
                if (!handled && typeof para.SetText === "function") {
                    para.SetText(newText);
                }
                updated++;
            }
            if (typeof doc.GoToPage === "function" && typeof doc.GetPageCount === "function") {
                var pageCount = Math.max(1, doc.GetPageCount() || 1);
                var restore = Math.max(0, Math.min((parseInt(Asc.scope.oopmRestorePage, 10) || 1) - 1, pageCount - 1));
                doc.GoToPage(restore);
            }
            return JSON.stringify({ ok: true, updated: updated });
        }, true);
        return parseCommandJson(raw);
    }

    window.Asc = window.Asc || {};
    Asc.plugin = Asc.plugin || {};
    Asc.plugin.init = function () {
        send({ type: "oopm-office-ai-bridge-ready" });
    };

    window.addEventListener("message", async function (ev) {
        var data = ev.data || {};
        if (data.target !== "oopm-office-ai-bridge") return;
        if (data.type === "oopm-office-ai-get-pages") {
            try {
                var result = await getPages(data.page_from || 1, data.page_to || data.page_from || 1);
                if (result.error) throw new Error(result.error);
                send(Object.assign({ type: "oopm-office-ai-pages", token: data.token }, result));
            } catch (e) {
                send({ type: "oopm-office-ai-pages", token: data.token, error: e && e.message ? e.message : String(e) });
            }
        }
        if (data.type === "oopm-office-ai-write-lines") {
            try {
                var writeResult = await writeLines(data.items || [], data.restore_page || data.page || 1);
                if (writeResult.error || writeResult.ok === false) throw new Error(writeResult.error || "OnlyOffice write failed");
                send({ type: "oopm-office-ai-write-result", token: data.token, ok: true, updated: writeResult.updated || 0 });
            } catch (e) {
                send({ type: "oopm-office-ai-write-result", token: data.token, ok: false, error: e && e.message ? e.message : String(e) });
            }
        }
    });

    function trySendReady() {
        send({ type: "oopm-office-ai-bridge-ready" });
    }
    trySendReady();
    setTimeout(trySendReady, 200);
    setTimeout(trySendReady, 500);
    setTimeout(trySendReady, 1500);
    setTimeout(trySendReady, 3000);
    setTimeout(trySendReady, 6000);
    setTimeout(trySendReady, 10000);
    setTimeout(trySendReady, 15000);
})();
