const session_id = $('#session-id').val();
const $chatBox = $("#chat-box");

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
                        <li><a class="btn-floating red lighten-1"><i class="material-icons">delete</i></a></li>
                        <li><a class="btn-floating blue lighten-1"><i class="material-icons">description</i></a></li>
                        <li><a class="btn-floating teal lighten-2"><i class="material-icons">edit</i></a></li>
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

// 加载历史消息
async function loadMessages() {
	try {
		const resp = await authFetch(`/messages/${session_id}`);
		const data = await resp.json();
        $chatBox.empty();  // 清空再渲染
        data.forEach(msg => {
            const rendered = marked.parse(msg.content);
            const $msgDiv = $("<div>", {
                class: `message ${msg.role}`,
                html: rendered
            });
            $chatBox.append($msgDiv);
        });

        // 自动滚动到底部
        $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
	} catch (err) {
		console.error("加载历史消息失败:", err);
        M.toast({ html: "加载历史消息失败", classes: "red lighten-2" });
	}
}

// 加载引用资料
async function loadCollections(id) {
	try {
		const resp = await authFetch(`/collections/${id}`);
		const collections = await resp.json();
        const $collectList = $("#collect-modal");
        $collectList.empty();  // 清空再渲染
        collections.forEach(s => {
            const $li = $(`
                <li class="collection-item">
                    <div>${escapeHtml(s.filename)}
                        <a href="${s.filepath}" class="secondary-content" download>
                        <i class="material-icons">send</i>
                        </a>
                    </div>
                </li>
            `);
            $collectList.append($li);
		});
	} catch (err) {
        const $collectList = $("#collect-modal");
        const $p = $("<p>").text("加载引用资料失败");
        $collectList.empty().append($p);
		console.error("加载引用资料失败:", err);
	}
}

$(function () {
    $('.sidenav').sidenav();
    $('#input-form').on('submit', async function (e) {
        e.preventDefault();
        const $input = $('#message-input');
        const message = $input.val().trim();
        if (!message) return;
        const rendered= marked.parse(message);

        // 用户消息
        $chatBox.append(`<div class="message user">${rendered}</div>`);
        $input.val('');
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
                body: new URLSearchParams({session_id, message})
            });
            const data = await resp.json();
            const renderedAnswer = marked.parse(data.answer);
            // 移除 loading 占位符
            $('#' + loadingId).replaceWith(`<div class="message assistant">${renderedAnswer}</div>`);
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        } catch (error) {
            console.error("Error:", error);
            $('#' + loadingId).replaceWith(`<div class="message assistant">Error: Unable to fetch response.</div>`);
            $chatBox.animate({ scrollTop: $chatBox[0].scrollHeight }, 400);
        }
    });
    $('#upload-form').on('submit', async function (e) {
        e.preventDefault();
        // 插入 bot 占位符（loading）
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
            const res = await fetch("/upload", {method: "POST", body: formData});
            const result = await res.json();
            alert(result.message);
            // 移除 loading 占位符
            $('#' + loadingId).replaceWith(`<div class="message assistant">${result.message}</div>`);
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
            const res = await fetch("/upload", {method: "POST", body: formData});
            const result = await res.json();
            alert(result.message);
            const $li = $(`
                <li class="collection-item">
                    <div>${escapeHtml(result.filename)}
                        <a href="${result.filepath}" class="secondary-content" download>
                        <i class="material-icons">send</i>
                        </a>
                    </div>
                </li>
            `);
            $("#collect-modal").append($li);
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
});
