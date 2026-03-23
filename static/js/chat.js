const session_id = $('#session-id').val();
const $chatBox = $("#chat-box");

// ── @ 书籍提及 ────────────────────────────────────────────────
let _availableBooks = [];  // 当前 Session 已入库的文件名列表

async function loadAvailableBooks() {
    try {
        const resp = await authFetch(`/upload/status/${session_id}`);
        const statuses = await resp.json();
        _availableBooks = statuses.filter(s => s.status === 'done').map(s => s.filename);
    } catch (e) { _availableBooks = []; }
}

// 获取光标前的 @query（仅在文本节点内）
function _getAtQuery() {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    const node = range.startContainer;
    if (node.nodeType !== Node.TEXT_NODE) return null;
    const before = node.textContent.slice(0, range.startOffset);
    const m = before.match(/@([^@\s]*)$/);
    if (!m) return null;
    return { node, query: m[1], atIndex: m.index, caretOffset: range.startOffset };
}

// 将光标处的 @query 替换为带样式的 span
function _insertMentionTag(filename, found) {
    const info = _getAtQuery();
    if (!info) return;
    const { node, atIndex, caretOffset } = info;
    const before = node.textContent.slice(0, atIndex);
    const after  = node.textContent.slice(caretOffset);

    const span = document.createElement('span');
    span.className = `mention-tag ${found ? 'found' : 'not-found'}`;
    if (found) span.dataset.source = filename;
    span.contentEditable = 'false';
    span.textContent = '@' + filename;

    const spaceNode = document.createTextNode('\u00A0');  // 不换行空格
    node.textContent = before;
    const parent = node.parentNode, nextSib = node.nextSibling;
    parent.insertBefore(span, nextSib);
    parent.insertBefore(spaceNode, span.nextSibling);
    if (after) parent.insertBefore(document.createTextNode(after), spaceNode.nextSibling);

    // 移动光标到 span 之后
    const r = document.createRange();
    r.setStart(spaceNode, spaceNode.length);
    r.collapse(true);
    const s = window.getSelection();
    s.removeAllRanges();
    s.addRange(r);

    _hideMentionDropdown();
}

function _showMentionDropdown(matches) {
    const $dd = $('#mention-dropdown');
    $dd.empty();
    if (!matches.length) { $dd.hide(); return; }
    matches.forEach((filename, i) => {
        const $item = $(`<div class="mention-option">${escapeHtml(filename)}</div>`);
        if (i === 0) $item.addClass('active');
        $item.on('mousedown', e => { e.preventDefault(); _insertMentionTag(filename, true); });
        $dd.append($item);
    });
    $dd.show();
}
function _hideMentionDropdown() { $('#mention-dropdown').hide(); }

function _moveMentionSelection(dir) {
    const $items = $('#mention-dropdown .mention-option');
    let idx = $items.index($items.filter('.active'));
    idx = (idx + dir + $items.length) % $items.length;
    $items.removeClass('active').eq(idx).addClass('active');
}

function _selectActiveMentionOption() {
    const $active = $('#mention-dropdown .mention-option.active');
    if ($active.length) $active.trigger('mousedown');
}

// 获取 contenteditable 中的纯文本（保留 mention 的 @filename 文字）
function _getMessageText() {
    return $('#message-input')[0].innerText.replace(/\u00A0/g, ' ').trim();
}

// 获取已选中的书籍来源列表（去重）
function _getSourceFiles() {
    const sources = [];
    $('#message-input .mention-tag.found').each(function () {
        const src = $(this).data('source');
        if (src && !sources.includes(src)) sources.push(src);
    });
    return sources;
}

marked.use({
    gfm: true,
    breaks: true,
    renderer: {
        code({ text, lang }) {
            const language = (lang && hljs.getLanguage(lang)) ? lang : 'plaintext';
            const highlighted = hljs.highlight(text, { language, ignoreIllegals: true }).value;
            const safeLang = lang ? lang.replace(/[<>"'&]/g, '') : '';
            const langLabel = safeLang ? `<span class="code-lang">${safeLang}</span>` : '';
            return `<div class="code-block">
                <div class="code-header">${langLabel}<button class="copy-code-btn" title="复制代码"><i class="material-icons">content_copy</i></button></div>
                <pre><code class="hljs language-${language}">${highlighted}</code></pre>
            </div>`;
        }
    }
});

// marked 对紧邻中文标点的 ** 有时不识别为粗体，做一次兜底替换
function renderMarkdown(text) {
    let html = marked.parse(text);
    // 替换 marked 未处理的 **bold** 和 *italic*（排除已转换的 HTML 标签内容）
    html = html.replace(/\*\*([^*\n<>]+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/(?<!\*)\*(?!\*)([^*\n<>]+?)(?<!\*)\*(?!\*)/g, '<em>$1</em>');
    return html;
}

function escapeHtml(str) {
    return str.replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;")
              .replace(/"/g, "&quot;")
              .replace(/'/g, "&#039;");
}

// 加载对话列表
async function loadSessions() {
	try {
		const resp = await authFetch("/sessions");
		const sessions = await resp.json();
        const $chatList = $("#slide-out");
        const $footer = $chatList.find(".sidenav-footer");
		sessions.forEach(s => {
            const $li = $("<li>");
            const $a = $("<a>", {
                id: s.id,
                href: "/?session_id=" + s.id,
            });
            const $span = $("<span>", {
                class: "max-width-80",
                text: s.name || "未命名对话",
            });
            $a.append($span);

            // 加入 badge（菜单触发器）
            const $icon = $(`
                <span class="badge fixed-action-btn direction-right">
                    <i class="material-icons">menu</i>
                    <ul>
                        <li><a class="btn-floating red lighten-1"><i class="material-icons" title="删除当前对话">delete</i></a></li>
                        <li><a class="btn-floating blue lighten-1"><i class="material-icons" title="查看引用资料">description</i></a></li>
                        <li><a class="btn-floating teal lighten-2"><i class="material-icons" title="修改对话名称">edit</i></a></li>
                    </ul>
                </span>
            `);
			if (s.id === session_id) {
                $li.addClass("active");
			}
            $a.append($icon);
            $li.append($a);
            $li.insertBefore($footer);
            $('.fixed-action-btn').floatingActionButton({ direction: 'right'});
		});
	} catch (err) {
		console.error("加载会话失败:", err);
        M.toast({ html: "加载会话失败", classes: "red lighten-2" });
	}
}

function makeSaveBtn(rawContent) {
    const $btn = $(`
        <button class="save-rag-btn" title="保存到知识库">
            <i class="material-icons">bookmark_border</i>
        </button>
    `);
    $btn.on('click', async function () {
        const formData = new FormData();
        formData.append('session_id', session_id);
        formData.append('content', rawContent);
        $btn.prop('disabled', true);
        try {
            const resp = await authFetch('/save_to_rag', { method: 'POST', body: formData });
            const result = await resp.json();
            if (result.success) {
                $btn.html('<i class="material-icons">bookmark</i>').addClass('saved');
                M.toast({ html: '已保存到知识库', classes: 'teal' });
            } else {
                $btn.prop('disabled', false);
                M.toast({ html: result.detail || '保存失败', classes: 'red lighten-2' });
            }
        } catch (e) {
            $btn.prop('disabled', false);
            M.toast({ html: '保存失败', classes: 'red lighten-2' });
        }
    });
    return $btn;
}

// 加载历史消息
async function loadMessages() {
	try {
		const resp = await authFetch(`/messages/${session_id}`);
		const data = await resp.json();
        $chatBox.empty();  // 清空再渲染
        data.forEach(msg => {
            const rendered = renderMarkdown(msg.content);
            const $msgDiv = $("<div>", { class: `message ${msg.role}` });
            $msgDiv.append($("<div>", { class: "msg-body", html: rendered }));
            if (msg.role === 'assistant') {
                $msgDiv.append($("<div>", { class: "msg-actions" }).append(makeSaveBtn(msg.content)));
            }
            $chatBox.append($msgDiv);
        });

        // 自动滚动到底部
        $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
	} catch (err) {
		console.error("加载历史消息失败:", err);
        M.toast({ html: "加载历史消息失败", classes: "red lighten-2" });
	}
}

// 加载引用资料（含处理状态）
let _collectionsRefreshTimer = null;

async function loadCollections(id) {
    if (_collectionsRefreshTimer) {
        clearTimeout(_collectionsRefreshTimer);
        _collectionsRefreshTimer = null;
    }
    try {
        const [collectResp, statusResp] = await Promise.all([
            authFetch(`/collections/${id}`),
            authFetch(`/upload/status/${id}`)
        ]);
        const collections = await collectResp.json();
        const statuses = await statusResp.json();

        const filepathMap = {};
        collections.forEach(c => { filepathMap[c.filename] = c.filepath; });

        const $collectList = $("#collect-modal");
        $collectList.empty();

        if (statuses.length === 0) {
            $collectList.append($('<p class="collection-empty">暂无引用资料</p>'));
        } else {
            statuses.forEach(s => {
                const filepath = filepathMap[s.filename];
                let badgeHtml;
                if (s.status === 'done') {
                    badgeHtml = `<span class="file-status-badge done">✓ 已入库 ${s.total_chunks || 0} 段</span>`;
                } else if (s.status === 'processing') {
                    const pct = s.total_chunks > 0 ? ` ${s.processed_chunks || 0}/${s.total_chunks}` : '';
                    badgeHtml = `<span class="file-status-badge processing">⏳ 解析中${pct}</span>`;
                } else if (s.status === 'failed') {
                    badgeHtml = `<span class="file-status-badge failed reprocess-btn" title="${escapeHtml(s.error_msg || '')}" data-filename="${escapeHtml(s.filename)}">❌ 失败</span>`;
                } else {
                    badgeHtml = `<span class="file-status-badge pending reprocess-btn" title="点击开始处理" data-filename="${escapeHtml(s.filename)}">⏸ 等待处理</span>`;
                }
                const downloadHtml = filepath
                    ? `<a href="/${filepath}" class="secondary-content" download><i class="material-icons">get_app</i></a>`
                    : '';
                const $li = $(`
                    <li class="collection-item">
                        <div>${escapeHtml(s.filename)} ${badgeHtml}${downloadHtml}</div>
                    </li>
                `);
                $collectList.append($li);
            });
        }

        // 仍有文件在处理中则自动刷新（每4秒，直到模态框关闭）
        const hasActive = statuses.some(s => s.status === 'pending' || s.status === 'processing');
        if (hasActive && $('#table-chat-modal').hasClass('open')) {
            _collectionsRefreshTimer = setTimeout(() => loadCollections(id), 4000);
        }
    } catch (err) {
        const $collectList = $("#collect-modal");
        $collectList.empty().append($('<p>').text("加载引用资料失败"));
        console.error("加载引用资料失败:", err);
    }
}

// 文件处理进度轮询（用于聊天框中的状态气泡）
function startStatusPolling(filename, $msgDiv) {
    const MAX_POLLS = 200;
    let polls = 0;

    function renderStatus(s) {
        if (!s) {
            $msgDiv.html(`<span>📄 ${escapeHtml(filename)}：状态查询中…</span>`);
            return false;
        }
        if (s.status === 'done') {
            $msgDiv.html(`<span>✅ ${escapeHtml(filename)} 已处理完成，共 ${s.total_chunks || 0} 段入库</span>`);
            return true;
        }
        if (s.status === 'failed') {
            $msgDiv.html(`<span>❌ ${escapeHtml(filename)} 处理失败：${escapeHtml(s.error_msg || '未知错误')}</span>`);
            return true;
        }
        if (s.status === 'processing') {
            const pct = s.total_chunks > 0 ? `，已处理 ${s.processed_chunks || 0}/${s.total_chunks} 段` : '';
            $msgDiv.html(`<span>⏳ ${escapeHtml(filename)} 解析中${pct}…</span>`);
            return false;
        }
        $msgDiv.html(`<span>⏳ ${escapeHtml(filename)} 等待处理…</span>`);
        return false;
    }

    async function poll() {
        if (polls >= MAX_POLLS) {
            $msgDiv.html(`<span>⚠️ ${escapeHtml(filename)} 处理超时，请稍后刷新查看</span>`);
            return;
        }
        polls++;
        try {
            const resp = await authFetch(`/upload/status/${session_id}`);
            const statuses = await resp.json();
            const entry = statuses.find(s => s.filename === filename);
            if (!renderStatus(entry)) {
                setTimeout(poll, 3000);
            }
        } catch (e) {
            setTimeout(poll, 3000);
        }
    }

    renderStatus(null);
    setTimeout(poll, 2000);
}

$(function () {
    $('.sidenav').sidenav();
    // contenteditable 回车提交，方向键导航 dropdown
    $('#message-input').on('keydown', function (e) {
        const ddVisible = $('#mention-dropdown').is(':visible');
        if (ddVisible && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
            e.preventDefault();
            _moveMentionSelection(e.key === 'ArrowDown' ? 1 : -1);
            return;
        }
        if (ddVisible && e.key === 'Enter') {
            e.preventDefault();
            _selectActiveMentionOption();
            return;
        }
        if (ddVisible && e.key === 'Escape') {
            e.preventDefault();
            // 当前 @query 无匹配时标红
            const info = _getAtQuery();
            if (info && info.query) _insertMentionTag(info.query, false);
            else _hideMentionDropdown();
            return;
        }
        if (!ddVisible && e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            $('#input-form').submit();
        }
    });

    // 检测 @ 并弹出补全
    $('#message-input').on('input', function () {
        const info = _getAtQuery();
        if (!info) { _hideMentionDropdown(); return; }
        const matches = _availableBooks.filter(b =>
            b.toLowerCase().includes(info.query.toLowerCase())
        );
        if (matches.length) {
            _showMentionDropdown(matches);
        } else {
            // 用 Space 或 @ 再次触发时无匹配 → 不自动标红，用 Escape 显式标红
            _hideMentionDropdown();
        }
    });

    // 点击外部关闭 dropdown
    $(document).on('click', function (e) {
        if (!$(e.target).closest('#message-input, #mention-dropdown').length) {
            _hideMentionDropdown();
        }
    });

    $('#input-form').on('submit', async function (e) {
        e.preventDefault();
        const message = _getMessageText();
        if (!message) return;
        const sourceFiles = _getSourceFiles();
        const rendered = renderMarkdown(message);

        // 用户消息
        $chatBox.append(`<div class="message user">${rendered}</div>`);
        $('#message-input').empty();
        // 插入 bot 占位符（loading）
        const loadingId = "loading-" + Date.now();
        $chatBox.append(`
            <div id="${loadingId}" class="message assistant">
                <span class="dots">···</span>
            </div>
        `);
        $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        try {
            const resp = await authFetch("/chat", {
                method: "POST",
                body: new URLSearchParams({
                    session_id, message,
                    source_files: sourceFiles.join(',')
                })
            });
            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                const msg = err.detail || `请求失败（${resp.status}）`;
                $('#' + loadingId).replaceWith(`<div class="message assistant" style="color:#c62828;">${escapeHtml(msg)}</div>`);
                $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
                return;
            }
            const data = await resp.json();
            const renderedAnswer = renderMarkdown(data.answer);
            let citationsHtml = '';
            if (data.citations && data.citations.length > 0) {
                const citationItems = data.citations.map((c, i) => {
                    const snippet = c.snippet
                        ? `<blockquote class="citation-snippet">${escapeHtml(c.snippet)}${c.snippet.length >= 200 ? '…' : ''}</blockquote>`
                        : '';
                    return `<div class="citation-item">
                        <span class="citation-label">[${i + 1}]</span>
                        <span class="citation-source">${escapeHtml(c.source)}</span>
                        <span class="citation-meta">第 ${c.chunk + 1} 段 &nbsp;·&nbsp; 相关度 ${c.score}</span>
                        ${snippet}
                    </div>`;
                }).join('');
                citationsHtml = `<details class="citations">
                    <summary>📎 参考了 ${data.citations.length} 处文档内容</summary>
                    ${citationItems}
                </details>`;
            }
            // 移除 loading 占位符
            const $answerDiv = $("<div>", { class: "message assistant" });
            $answerDiv.append($("<div>", { class: "msg-body", html: renderedAnswer + citationsHtml }));
            $answerDiv.append($("<div>", { class: "msg-actions" }).append(makeSaveBtn(data.answer)));
            $('#' + loadingId).replaceWith($answerDiv);
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        } catch (error) {
            console.error("Error:", error);
            $('#' + loadingId).replaceWith(`<div class="message assistant">Error: Unable to fetch response.</div>`);
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        }
    });
    $('#upload-form').on('submit', async function (e) {
        e.preventDefault();
        const loadingId = "loading-" + Date.now();
        $chatBox.append(`
            <div id="${loadingId}" class="message assistant">
                文件上传中<span class="dots">···</span>
            </div>
        `);
        $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        try {
            const formData = new FormData(e.target);
            formData.append('session_id', session_id);
            const res = await fetch("/upload/", {method: "POST", body: formData});
            const result = await res.json();
            M.toast({ html: result.message, classes: 'teal' });
            const $msgDiv = $(`<div class="message assistant"></div>`);
            $('#' + loadingId).replaceWith($msgDiv);
            if (result.filename) {
                startStatusPolling(result.filename, $msgDiv);
            } else {
                $msgDiv.text(result.message);
            }
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        } catch (error) {
            console.error("Error:", error);
            $('#' + loadingId).replaceWith(`<div class="message assistant">Error: 上传文件失败.</div>`);
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        }
    });
    $('#input-btn').on('click', function() {$('#input-form').submit();});
    $('#upload-btn').on('click', function() {$('#file-input').click();});
    $('#file-input').on('change', function() {$('#upload-form').submit();});
    $('.modal').modal();
    $('#modal-upload-form').on('submit', async function (e) {
        e.preventDefault();
        try {
            const formData = new FormData(e.target);
            const modal_session_id = $('#modal-session-id').val();
            formData.append("session_id", modal_session_id);
            const res = await fetch("/upload/", {method: "POST", body: formData});
            const result = await res.json();
            M.toast({ html: result.message, classes: 'teal' });
            loadCollections(modal_session_id);
        } catch (error) {
            console.error("Error:", error);
            M.toast({ html: '上传文件失败', classes: 'red lighten-2' });
        }
    });
    $('#new-chat-btn').on('click', function() {
        $('#newchat-name-input').val('');
    });
    $('#newchat-confirm-btn').on('click', async function () {
        const name = $('#newchat-name-input').val().trim();
        if (!name) {
            M.toast({ html: '请输入对话名称', classes: 'red lighten-2' });
            return;
        }
        try {
            const formData = new FormData();
            formData.append("name", name);
            const resp = await authFetch("/new_session", { method: "POST", body: formData});
            const data = await resp.json();
            if(data.success) {
                $("new-chat-modal").modal('close'); // 关闭模态框
                window.location.href = "/?session_id=" + data.id;
            } else {
                M.toast({ html: '新建对话失败: ' + (data.error || '未知错误'), classes: 'red lighten-2' });
            }
        } catch (err) {
            console.error("新建对话失败:", err);
            M.toast({ html: '新建对话失败', classes: 'red lighten-2' });
        }
    });
    $('#changechat-confirm-btn').on('click', async function () {
        const name = $('#changechat-name-input').val().trim();
        const modal_session_id = $('#modal-session-id').val();
        if (!name) {
            M.toast({ html: '请输入对话名称', classes: 'red lighten-2' });
            return;
        }
        if (!modal_session_id) {
            M.toast({ html: '未获取到当前对话ID', classes: 'red lighten-2' });
            return;
        }
        try {
            const formData = new FormData();
            formData.append("name", name);
            formData.append("session_id", modal_session_id);
            const resp = await authFetch("/change_session", { method: "POST", body: formData});
            const data = await resp.json();
            if(data.success) {
                $("change-chat-modal").modal('close'); // 关闭模态框
                window.location.reload(true);
            } else {
                M.toast({ html: '修改对话失败: ' + (data.error || '未知错误'), classes: 'red lighten-2' });
            }
        } catch (err) {
            console.error("修改对话失败:", err);
            M.toast({ html: '修改对话失败', classes: 'red lighten-2' });
        }
    });
    $('#delchat-confirm-btn').on('click', async function () {
        const modal_session_id = $('#modal-session-id').val();
        if (!modal_session_id) {
            M.toast({ html: '未获取到当前对话ID', classes: 'red lighten-2' });
            return;
        }
        try {
            const formData = new FormData();
            formData.append("session_id", modal_session_id);
            const resp = await authFetch("/del_session", { method: "POST", body: formData});
            const data = await resp.json();
            if(data.success) {
                $("del-chat-modal").modal('close'); // 关闭模态框
                window.location.reload(true);
            } else {
                M.toast({ html: '删除对话失败: ' + (data.error || '未知错误'), classes: 'red lighten-2' });
            }
        } catch (err) {
            console.error("删除对话失败:", err);
            M.toast({ html: '删除对话失败', classes: 'red lighten-2' });
        }
    });
    $('#tablechat-confirm-btn').on('click', function() {$('#modal-file-input').click();});
    $('#modal-file-input').on('change', function() {$('#modal-upload-form').submit();});

    // 代码块复制按钮（事件委托，适用于动态插入的内容）
    $(document).on('click', '.copy-code-btn', function () {
        const code = $(this).closest('.code-block').find('code').text();
        navigator.clipboard.writeText(code).then(() => {
            const $icon = $(this).find('.material-icons');
            $icon.text('check');
            setTimeout(() => $icon.text('content_copy'), 1500);
            M.toast({ html: '已复制到剪贴板', classes: 'teal' });
        }).catch(() => {
            M.toast({ html: '复制失败', classes: 'red lighten-2' });
        });
    });

    $('#logout-btn').on('click', async function () {
        await fetch('/account/logout', { method: 'POST', credentials: 'include' });
        window.location.href = '/account/login';
    });

    loadAvailableBooks();
    loadSessions();
    loadMessages();

    $('#slide-out').on('click', '.btn-floating.red', async function (e) {
        e.preventDefault();   // 阻止 <a href> 跳转
        e.stopPropagation();  // 阻止事件冒泡到上层 <a>，避免触发波纹效果
        const $a = $(this).closest('a[href]');
        const name = $a.find('.max-width-80').text().trim();
        const aId = $a.attr('id');
        $('#delchat-name').text(name);
        $('#modal-session-id').val(aId);
        $('#del-chat-modal').modal('open');
    });
    $('#slide-out').on('click', '.btn-floating.teal', async function (e) {
        e.preventDefault();   // 阻止 <a href> 跳转
        e.stopPropagation();  // 阻止事件冒泡到上层 <a>，避免触发波纹效果
        const $a = $(this).closest('a[href]');
        const name = $a.find('.max-width-80').text().trim();
        const aId = $a.attr('id');
        $('#modal-session-id').val(aId);
        $('#changechat-name-input').val(name);
        $('#change-chat-modal').modal('open');
    });
    $('#slide-out').on('click', '.btn-floating.blue', async function (e) {
        e.preventDefault();   // 阻止 <a href> 跳转
        e.stopPropagation();  // 阻止事件冒泡到上层 <a>，避免触发波纹效果
        const $a = $(this).closest('a[href]');
        const name = $a.find('.max-width-80').text().trim();
        const aId = $a.attr('id');
        $('#tablechat-name').text(name);
        $('#modal-session-id').val(aId);
        loadCollections(aId);
        $('#table-chat-modal').modal('open');
    });
    $('#tablechat-refresh-btn').on('click', function (e) {
        e.preventDefault();
        const aId = $('#modal-session-id').val();
        if (aId) loadCollections(aId);
    });

    // 点击 pending/failed badge 触发重新处理
    $('#collect-modal').on('click', '.reprocess-btn', async function () {
        const $badge = $(this);
        const filename = $badge.data('filename');
        const sessionId = $('#modal-session-id').val();
        if (!filename || !sessionId) return;

        $badge.text('⏳ 提交中…').removeClass('reprocess-btn').css('cursor', 'default');
        try {
            const formData = new FormData();
            formData.append('session_id', sessionId);
            formData.append('filename', filename);
            const resp = await authFetch('/upload/reprocess', { method: 'POST', body: formData });
            const result = await resp.json();
            if (resp.ok && result.success) {
                $badge.text('⏳ 解析中…').addClass('processing').removeClass('pending failed');
                M.toast({ html: `${filename} 已开始处理`, classes: 'teal' });
                // 触发自动轮询刷新
                if (_collectionsRefreshTimer) clearTimeout(_collectionsRefreshTimer);
                _collectionsRefreshTimer = setTimeout(() => loadCollections(sessionId), 3000);
            } else {
                $badge.addClass('reprocess-btn');
                M.toast({ html: result.detail || '提交失败', classes: 'red lighten-2' });
            }
        } catch (e) {
            $badge.addClass('reprocess-btn');
            M.toast({ html: '请求失败', classes: 'red lighten-2' });
        }
    });
});
