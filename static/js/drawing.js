(function () {
    const initData = JSON.parse(document.getElementById('drawing-init-data').textContent);
    let STYLE_ID = initData.styleId;
    let currentStyle = null;
    let allPrompts = [];
    let allSkillPackages = [];
    let generating = false;
    let pendingPromptIds = [];
    let pendingSkillPackageId = '';
    let pickPromptsSelection = new Set();
    // 后台出图轮询：generationId -> intervalId。切页面回来时 loadGallery 会按 processing 记录自动续上。
    const activePolls = new Map();

    function stopPolling(genId) {
        const t = activePolls.get(genId);
        if (t) clearInterval(t);
        activePolls.delete(genId);
    }

    function startPolling(genId) {
        if (activePolls.has(genId)) return;
        let attempts = 0;
        const MAX_ATTEMPTS = 240; // 3s * 240 = 12 分钟；超过基本是后台任务丢了（如进程重启）
        const timer = setInterval(async () => {
            attempts += 1;
            if (attempts > MAX_ATTEMPTS) {
                stopPolling(genId);
                M.toast({ html: '这张图等待过久，可能出图失败了，请刷新页面查看或重试', classes: 'orange darken-2', displayLength: 9000 });
                return;
            }
            let data;
            try {
                const r = await authFetch(`/drawing/generations/${genId}/status`);
                if (r.status === 404) { stopPolling(genId); await loadGallery(STYLE_ID); return; }
                if (!r.ok) return; // 瞬时错误，下一轮再试
                data = await r.json();
            } catch (_) { return; }
            if (data.status === 'processing' || data.status === 'pending') return;
            stopPolling(genId);
            await loadGallery(STYLE_ID);
            if (data.status === 'failed') {
                M.toast({ html: data.error_msg || '图片生成失败', classes: 'red darken-1', displayLength: 9000 });
            }
        }, 3000);
        activePolls.set(genId, timer);
    }

    document.addEventListener('DOMContentLoaded', () => {
        M.Sidenav.init(document.querySelectorAll('.sidenav'));
        M.Modal.init(document.querySelectorAll('.modal'));

        document.getElementById('logout-btn').addEventListener('click', logout);
        document.getElementById('btn-go-chat')?.addEventListener('click', () => { window.location.href = '/'; });
        document.getElementById('btn-new-drawing').addEventListener('click', openNewDrawingModal);
        document.getElementById('btn-new-drawing-nav').addEventListener('click', openNewDrawingModal);
        document.getElementById('btn-confirm-new-drawing').addEventListener('click', confirmNewDrawing);
        document.getElementById('btn-confirm-del-drawing').addEventListener('click', confirmDeleteDrawing);
        document.getElementById('btn-save-style').addEventListener('click', saveStyleSettings);
        document.getElementById('btn-generate').addEventListener('click', runGenerate);
        document.getElementById('prompt-input').addEventListener('keydown', (e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                runGenerate();
            }
        });
        document.getElementById('btn-upload-image').addEventListener('click', () => {
            document.getElementById('drawing-file-input').click();
        });
        document.getElementById('drawing-file-input').addEventListener('change', runUploadImage);
        document.getElementById('btn-manage-prompts').addEventListener('click', openManagePromptsModal);
        document.getElementById('form-new-prompt').addEventListener('submit', submitNewPrompt);
        document.getElementById('btn-cancel-edit-prompt').addEventListener('click', (e) => { e.preventDefault(); resetPromptForm(); });
        document.getElementById('btn-manage-skill-packages').addEventListener('click', openManageSkillPackagesModal);
        document.getElementById('form-new-skill-package').addEventListener('submit', submitNewSkillPackage);
        document.getElementById('btn-cancel-edit-skill-package').addEventListener('click', (e) => { e.preventDefault(); resetSkillPackageForm(); });
        document.getElementById('btn-fetch-skill-package').addEventListener('click', fetchSkillPackagePreview);
        document.getElementById('btn-pick-prompts').addEventListener('click', openPickPromptsModal);
        document.getElementById('btn-confirm-pick-prompts').addEventListener('click', (e) => { e.preventDefault(); confirmPickPrompts(); });
        document.getElementById('pick-prompts-filter').addEventListener('input', (e) => renderPickPromptsList(e.target.value));
        document.getElementById('prompt-manage-search').addEventListener('input', () => renderPromptManageList());
        document.getElementById('btn-pick-skill-package').addEventListener('click', openPickSkillPackageModal);
        document.getElementById('btn-confirm-pick-skill-package').addEventListener('click', (e) => { e.preventDefault(); confirmPickSkillPackage(); });
        document.getElementById('pick-skill-package-filter').addEventListener('input', (e) => renderPickSkillPackageList(e.target.value));
        document.getElementById('lightbox-close').addEventListener('click', closeLightbox);
        document.getElementById('image-lightbox-overlay').addEventListener('click', (e) => {
            if (e.target.id === 'image-lightbox-overlay') closeLightbox();
        });

        ['setting-name'].forEach(id => {
            document.getElementById(id).addEventListener('input', () => {
                document.getElementById('btn-save-style').disabled = false;
            });
        });

        loadStyles();
        Promise.all([loadPrompts(), loadSkillPackages()]).then(() => {
            if (STYLE_ID) {
                loadStyle(STYLE_ID);
                loadGallery(STYLE_ID);
            }
        });
    });

    function logout() {
        authFetch('/account/logout', { method: 'POST' }).then(() => {
            window.location.href = '/account/login';
        });
    }

    // ---------------- 侧栏：风格列表 ----------------
    async function loadStyles() {
        const resp = await authFetch('/drawing/styles');
        if (!resp.ok) return;
        const styles = await resp.json();
        renderStyleList(styles);
    }

    function renderStyleList(styles) {
        const list = document.getElementById('drawing-list');
        list.innerHTML = '';
        if (!styles.length) {
            list.innerHTML = '<p class="drawing-empty center-align">还没有作图风格，点击下方按钮新建</p>';
            return;
        }
        styles.forEach(style => {
            const item = document.createElement('div');
            item.className = 'drawing-item' + (String(style.id) === String(STYLE_ID) ? ' active' : '');
            const titleSpan = document.createElement('span');
            titleSpan.className = 'drawing-item-title';
            titleSpan.textContent = style.name || '未命名风格';
            item.appendChild(titleSpan);

            const delBtn = document.createElement('button');
            delBtn.className = 'drawing-item-del';
            delBtn.type = 'button';
            delBtn.innerHTML = '<i class="material-icons">close</i>';
            delBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                openDeleteDrawingModal(style.id, style.name);
            });
            item.appendChild(delBtn);

            item.addEventListener('click', () => { window.location.href = `/drawing/${style.id}`; });
            list.appendChild(item);
        });
    }

    // ---------------- 新建 / 删除风格 ----------------
    function openNewDrawingModal() {
        document.getElementById('new-drawing-name').value = '';
        M.updateTextFields();
        M.Modal.getInstance(document.getElementById('modal-new-drawing')).open();
    }

    async function confirmNewDrawing() {
        const name = document.getElementById('new-drawing-name').value.trim();
        if (!name) { M.toast({ html: '请输入风格名称', classes: 'red darken-1' }); return; }
        const resp = await authFetch('/drawing/styles', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) { M.toast({ html: '新建失败', classes: 'red darken-1' }); return; }
        const data = await resp.json();
        window.location.href = `/drawing/${data.id}`;
    }

    let pendingDeleteId = null;
    function openDeleteDrawingModal(id, name) {
        pendingDeleteId = id;
        document.getElementById('del-drawing-name').textContent = name || '未命名风格';
        M.Modal.getInstance(document.getElementById('modal-del-drawing')).open();
    }

    async function confirmDeleteDrawing() {
        if (!pendingDeleteId) return;
        const id = pendingDeleteId;
        pendingDeleteId = null;
        const resp = await authFetch(`/drawing/styles/${id}`, { method: 'DELETE' });
        if (!resp.ok) { M.toast({ html: '删除失败', classes: 'red darken-1' }); return; }
        if (String(id) === String(STYLE_ID)) {
            window.location.href = '/drawing/';
        } else {
            loadStyles();
        }
    }

    // ---------------- 风格设置 ----------------
    async function loadStyle(styleId) {
        const resp = await authFetch(`/drawing/styles/${styleId}`);
        if (!resp.ok) return;
        currentStyle = await resp.json();
        document.getElementById('setting-name').value = currentStyle.name || '';
        pendingPromptIds = currentStyle.prompt_ids || [];
        pendingSkillPackageId = currentStyle.skill_package_id || '';
        renderBoundPromptList();
        renderBoundSkillPackage();
        document.getElementById('btn-save-style').disabled = true;
    }

    // 绑定 Prompt/Skill 包的改动立即生效（选择器点确定、移除 chip 都直接 PATCH），
    // 不依赖"保存设置"按钮——那个按钮只负责风格名称这一项。
    async function patchStyleBinding(payload) {
        if (!STYLE_ID) return false;
        const resp = await authFetch(`/drawing/styles/${STYLE_ID}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        if (!resp.ok) { M.toast({ html: '保存失败', classes: 'red darken-1' }); return false; }
        return true;
    }

    function renderBoundPromptList() {
        const box = document.getElementById('bound-prompt-list');
        if (!box) return;
        const bound = pendingPromptIds.map(id => allPrompts.find(p => p.id === id)).filter(Boolean);
        if (!bound.length) {
            box.innerHTML = '<p class="bound-empty-hint">未绑定任何 Prompt</p>';
            return;
        }
        box.innerHTML = '';
        bound.forEach(prompt => {
            const row = document.createElement('div');
            row.className = 'bound-chip-row';
            const label = document.createElement('span');
            label.textContent = prompt.name;
            const remove = document.createElement('span');
            remove.className = 'bound-chip-remove';
            remove.innerHTML = '&times;';
            remove.addEventListener('click', async () => {
                const newIds = pendingPromptIds.filter(id => id !== prompt.id);
                const ok = await patchStyleBinding({ prompt_ids: newIds });
                if (ok) {
                    pendingPromptIds = newIds;
                    renderBoundPromptList();
                    M.toast({ html: '已解除绑定', classes: 'green darken-1' });
                } else {
                    await loadStyle(STYLE_ID);
                }
            });
            row.appendChild(label);
            row.appendChild(remove);
            box.appendChild(row);
        });
    }

    function renderBoundSkillPackage() {
        const box = document.getElementById('bound-skill-package');
        if (!box) return;
        const pkg = allSkillPackages.find(p => p.id === pendingSkillPackageId);
        if (!pkg) {
            box.innerHTML = '<p class="bound-empty-hint">未绑定 Skill 包</p>';
            return;
        }
        box.innerHTML = '';
        const row = document.createElement('div');
        row.className = 'bound-chip-row';
        const label = document.createElement('span');
        label.textContent = pkg.name;
        const remove = document.createElement('span');
        remove.className = 'bound-chip-remove';
        remove.innerHTML = '&times;';
        remove.addEventListener('click', async () => {
            const ok = await patchStyleBinding({ skill_package_id: '' });
            if (ok) {
                pendingSkillPackageId = '';
                renderBoundSkillPackage();
                M.toast({ html: '已解除绑定', classes: 'green darken-1' });
            } else {
                await loadStyle(STYLE_ID);
            }
        });
        row.appendChild(label);
        row.appendChild(remove);
        box.appendChild(row);
    }

    async function saveStyleSettings() {
        if (!STYLE_ID) return;
        const name = document.getElementById('setting-name').value.trim() || '未命名风格';
        const resp = await authFetch(`/drawing/styles/${STYLE_ID}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name }),
        });
        if (!resp.ok) { M.toast({ html: '保存失败', classes: 'red darken-1' }); return; }
        M.toast({ html: '设置已保存', classes: 'green darken-1' });
        document.getElementById('btn-save-style').disabled = true;
        loadStyle(STYLE_ID);
        loadStyles();
    }

    // ---------------- Prompt 库管理 ----------------
    async function loadPrompts() {
        const resp = await authFetch('/drawing/prompts');
        if (!resp.ok) return;
        allPrompts = await resp.json();
        renderBoundPromptList();
        renderPromptManageList();
    }

    let editingPromptId = null;

    function renderPromptManageList() {
        const list = document.getElementById('prompt-manage-list');
        if (!list) return;
        const needle = (document.getElementById('prompt-manage-search')?.value || '').trim().toLowerCase();
        const items = allPrompts.filter(p => !needle || p.name.toLowerCase().includes(needle));
        list.innerHTML = '';
        if (!allPrompts.length) {
            list.innerHTML = '<p style="font-size:12px;color:#bdbdbd;">暂无 Prompt</p>';
            return;
        }
        if (!items.length) {
            list.innerHTML = '<p style="font-size:12px;color:#bdbdbd;">没有匹配的 Prompt</p>';
            return;
        }
        items.forEach(prompt => {
            const row = document.createElement('div');
            row.className = 'skill-manage-row';
            const left = document.createElement('div');
            left.className = 'prompt-manage-row-main';
            left.innerHTML = `<div class="skill-manage-row-name">${escapeHtml(prompt.name)}</div>` +
                (prompt.content ? `<div class="prompt-manage-snippet">${escapeHtml(prompt.content)}</div>` : '');
            const actions = document.createElement('div');
            actions.style.display = 'flex';
            actions.style.gap = '4px';
            actions.style.flexShrink = '0';
            const editBtn = document.createElement('a');
            editBtn.href = '#!';
            editBtn.className = 'waves-effect btn-flat';
            editBtn.textContent = '编辑';
            editBtn.addEventListener('click', (e) => {
                e.preventDefault();
                startEditPrompt(prompt);
            });
            const delBtn = document.createElement('a');
            delBtn.href = '#!';
            delBtn.className = 'waves-effect btn-flat red-text';
            delBtn.textContent = '删除';
            delBtn.addEventListener('click', async (e) => {
                e.preventDefault();
                if (!confirm(`删除 Prompt「${prompt.name}」？已勾选此 Prompt 的风格会自动忽略它。`)) return;
                await authFetch(`/drawing/prompts/${prompt.id}`, { method: 'DELETE' });
                if (editingPromptId === prompt.id) resetPromptForm();
                await loadPrompts();
            });
            actions.appendChild(editBtn);
            actions.appendChild(delBtn);
            row.appendChild(left);
            row.appendChild(actions);
            list.appendChild(row);
        });
    }

    function openManagePromptsModal() {
        M.Modal.getInstance(document.getElementById('modal-manage-prompts')).open();
        resetPromptForm();
        const search = document.getElementById('prompt-manage-search');
        if (search) search.value = '';
        renderPromptManageList();
    }

    function startEditPrompt(prompt) {
        editingPromptId = prompt.id;
        document.getElementById('new-prompt-name').value = prompt.name || '';
        const contentEl = document.getElementById('new-prompt-content');
        contentEl.value = prompt.content || '';
        M.updateTextFields();
        M.textareaAutoResize(contentEl);
        document.getElementById('btn-add-prompt').textContent = '保存修改';
        document.getElementById('btn-cancel-edit-prompt').style.display = '';
    }

    function resetPromptForm() {
        editingPromptId = null;
        document.getElementById('new-prompt-name').value = '';
        const contentEl = document.getElementById('new-prompt-content');
        contentEl.value = '';
        M.updateTextFields();
        M.textareaAutoResize(contentEl);
        document.getElementById('btn-add-prompt').textContent = '添加 Prompt';
        document.getElementById('btn-cancel-edit-prompt').style.display = 'none';
    }

    async function submitNewPrompt(e) {
        e.preventDefault();
        const name = document.getElementById('new-prompt-name').value.trim();
        const content = document.getElementById('new-prompt-content').value.trim();
        if (!name) return;
        const url = editingPromptId ? `/drawing/prompts/${editingPromptId}` : '/drawing/prompts';
        const method = editingPromptId ? 'PATCH' : 'POST';
        const resp = await authFetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, content }),
        });
        if (!resp.ok) { M.toast({ html: editingPromptId ? '保存失败' : '添加失败', classes: 'red darken-1' }); return; }
        resetPromptForm();
        await loadPrompts();
    }

    // ---------------- Skill 包管理（第三方完整方法论，一个风格最多绑一个） ----------------
    async function loadSkillPackages() {
        const resp = await authFetch('/drawing/skill-packages');
        if (!resp.ok) return;
        allSkillPackages = await resp.json();
        renderBoundSkillPackage();
        renderSkillPackageManageList();
    }

    let editingSkillPackageId = null;

    function renderSkillPackageManageList() {
        const list = document.getElementById('skill-package-manage-list');
        if (!list) return;
        list.innerHTML = '';
        if (!allSkillPackages.length) {
            list.innerHTML = '<p style="font-size:12px;color:#bdbdbd;">暂无 skill 包</p>';
            return;
        }
        allSkillPackages.forEach(pkg => {
            const row = document.createElement('div');
            row.className = 'skill-manage-row';
            const left = document.createElement('div');
            left.innerHTML = `<div class="skill-manage-row-name">${escapeHtml(pkg.name)}</div>` +
                (pkg.source_url ? `<div class="skill-manage-row-snippet">${escapeHtml(pkg.source_url)}</div>` : '');
            const actions = document.createElement('div');
            actions.style.display = 'flex';
            actions.style.gap = '4px';
            actions.style.flexShrink = '0';
            const editBtn = document.createElement('a');
            editBtn.href = '#!';
            editBtn.className = 'waves-effect btn-flat';
            editBtn.textContent = '编辑';
            editBtn.addEventListener('click', async (e) => {
                e.preventDefault();
                await startEditSkillPackage(pkg);
            });
            const delBtn = document.createElement('a');
            delBtn.href = '#!';
            delBtn.className = 'waves-effect btn-flat red-text';
            delBtn.textContent = '删除';
            delBtn.addEventListener('click', async (e) => {
                e.preventDefault();
                if (!confirm(`删除 skill 包「${pkg.name}」？已绑定它的风格会自动解绑。`)) return;
                await authFetch(`/drawing/skill-packages/${pkg.id}`, { method: 'DELETE' });
                if (editingSkillPackageId === pkg.id) resetSkillPackageForm();
                await loadSkillPackages();
            });
            actions.appendChild(editBtn);
            actions.appendChild(delBtn);
            row.appendChild(left);
            row.appendChild(actions);
            list.appendChild(row);
        });
    }

    function openManageSkillPackagesModal() {
        M.Modal.getInstance(document.getElementById('modal-manage-skill-packages')).open();
        resetSkillPackageForm();
        renderSkillPackageManageList();
    }

    async function startEditSkillPackage(pkg) {
        const resp = await authFetch(`/drawing/skill-packages/${pkg.id}`);
        if (!resp.ok) { M.toast({ html: '加载失败', classes: 'red darken-1' }); return; }
        const detail = await resp.json();
        editingSkillPackageId = pkg.id;
        document.getElementById('new-skill-package-name').value = detail.name || '';
        document.getElementById('new-skill-package-source').value = detail.source_url || '';
        const instructionsEl = document.getElementById('new-skill-package-instructions');
        instructionsEl.value = detail.instructions || '';
        M.updateTextFields();
        M.textareaAutoResize(instructionsEl);
        document.getElementById('btn-add-skill-package').textContent = '保存修改';
        document.getElementById('btn-cancel-edit-skill-package').style.display = '';
    }

    function resetSkillPackageForm() {
        editingSkillPackageId = null;
        document.getElementById('new-skill-package-name').value = '';
        document.getElementById('new-skill-package-source').value = '';
        const instructionsEl = document.getElementById('new-skill-package-instructions');
        instructionsEl.value = '';
        M.updateTextFields();
        M.textareaAutoResize(instructionsEl);
        document.getElementById('btn-add-skill-package').textContent = '保存 Skill 包';
        document.getElementById('btn-cancel-edit-skill-package').style.display = 'none';
    }

    async function fetchSkillPackagePreview() {
        const repoUrl = document.getElementById('new-skill-package-source').value.trim();
        if (!repoUrl) { M.toast({ html: '请先填写 GitHub 仓库地址', classes: 'red darken-1' }); return; }
        const btn = document.getElementById('btn-fetch-skill-package');
        btn.disabled = true;
        btn.textContent = '抓取中…';
        try {
            const resp = await authFetch('/drawing/skill-packages/fetch-github', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ repo_url: repoUrl }),
            });
            if (!resp.ok) {
                let detail = '抓取失败';
                try { detail = (await resp.json()).detail || detail; } catch (_) {}
                M.toast({ html: detail, classes: 'red darken-1' });
                return;
            }
            const data = await resp.json();
            const instructionsEl = document.getElementById('new-skill-package-instructions');
            instructionsEl.value = data.instructions;
            M.updateTextFields();
            M.textareaAutoResize(instructionsEl);
            M.toast({ html: '已抓取，保存前可检查/编辑', classes: 'green darken-1' });
        } catch (err) {
            M.toast({ html: '网络错误：' + err, classes: 'red darken-1' });
        } finally {
            btn.disabled = false;
            btn.textContent = '从 GitHub 抓取';
        }
    }

    async function submitNewSkillPackage(e) {
        e.preventDefault();
        const name = document.getElementById('new-skill-package-name').value.trim();
        const source_url = document.getElementById('new-skill-package-source').value.trim();
        const instructions = document.getElementById('new-skill-package-instructions').value.trim();
        if (!name || !instructions) { M.toast({ html: '名称和 skill 全文都不能为空', classes: 'red darken-1' }); return; }
        const url = editingSkillPackageId ? `/drawing/skill-packages/${editingSkillPackageId}` : '/drawing/skill-packages';
        const method = editingSkillPackageId ? 'PATCH' : 'POST';
        const resp = await authFetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, source_url, instructions }),
        });
        if (!resp.ok) { M.toast({ html: '保存失败', classes: 'red darken-1' }); return; }
        resetSkillPackageForm();
        await loadSkillPackages();
    }

    // ---------------- 生成（含基于已有图片的迭代编辑） ----------------
    // 同一时刻最多一条历史链处于展开状态，展开时输入框的目标切换为"继续编辑这条链的最新一步"。
    // 镜像 writing.html 分段写作里 expandedSectionId 的单例展开模式。
    let expandedGenerationId = null;

    function updateInputAreaState() {
        const input = document.getElementById('prompt-input');
        if (input) {
            input.placeholder = expandedGenerationId ? '描述你想如何修改这张图片…' : '描述你想生成的画面…';
        }
        // 上传只用于新建一条链的第一张图；正在对某一行做"进一步改进"时不支持中途上传新图。
        const uploadBtn = document.getElementById('btn-upload-image');
        if (uploadBtn) {
            uploadBtn.disabled = !STYLE_ID || !!expandedGenerationId;
        }
    }

    async function runGenerate() {
        if (!STYLE_ID || generating) return;
        const input = document.getElementById('prompt-input');
        const userInput = input.value.trim();
        if (!userInput) { M.toast({ html: '请输入描述文字', classes: 'red darken-1' }); return; }

        generating = true;
        const btn = document.getElementById('btn-generate');
        btn.disabled = true;
        btn.innerHTML = '<span class="generate-spinner"></span>提交中…';
        const parentId = expandedGenerationId;

        try {
            const resp = await authFetch(`/drawing/styles/${STYLE_ID}/generate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ user_input: userInput, parent_generation_id: parentId }),
            });
            if (!resp.ok) {
                let detail = '生成失败';
                try { detail = (await resp.json()).detail || detail; } catch (_) {}
                M.toast({ html: detail, classes: 'red darken-1', displayLength: 9000 });
                return;
            }
            const data = await resp.json();
            // 出图在后台跑，接口只回 processing。编辑场景把展开目标指向新版本，支持连续追加指令；
            // 新建场景不自动展开，保持折叠列表整洁。
            expandedGenerationId = parentId ? data.id : null;
            input.value = '';
            if (data.compile_degraded) {
                M.toast({
                    html: '⚠️ 本次未成功应用风格/Skill 规则，已用原始输入直接生成（AI 编译服务暂时不可用），可重新生成一次',
                    classes: 'orange darken-2',
                    displayLength: 6000,
                });
            }
            M.toast({ html: '已提交，出图中…可能需要 1-3 分钟，可离开本页面稍后回来查看', classes: 'teal', displayLength: 6000 });
            await loadGallery(STYLE_ID); // 会渲染 processing 占位卡并自动开始轮询
        } catch (err) {
            M.toast({ html: '网络错误：' + err, classes: 'red darken-1' });
        } finally {
            generating = false;
            btn.disabled = false;
            btn.textContent = '生成';
            updateInputAreaState();
        }
    }

    async function runUploadImage(e) {
        const fileInput = e.target;
        const file = fileInput.files[0];
        fileInput.value = ''; // 允许重复选择同一个文件也能再次触发 change
        if (!file || !STYLE_ID || expandedGenerationId || generating) return;

        generating = true;
        const uploadBtn = document.getElementById('btn-upload-image');
        uploadBtn.disabled = true;
        uploadBtn.textContent = '上传中…';

        try {
            const formData = new FormData();
            formData.append('file', file);
            const resp = await authFetch(`/drawing/styles/${STYLE_ID}/upload`, { method: 'POST', body: formData });
            if (!resp.ok) {
                let detail = '上传失败';
                try { detail = (await resp.json()).detail || detail; } catch (_) {}
                M.toast({ html: detail, classes: 'red darken-1' });
                return;
            }
            await loadGallery(STYLE_ID);
        } catch (err) {
            M.toast({ html: '网络错误：' + err, classes: 'red darken-1' });
        } finally {
            generating = false;
            uploadBtn.textContent = '上传';
            updateInputAreaState();
        }
    }

    async function toggleLineageExpand(tipId) {
        expandedGenerationId = (expandedGenerationId === tipId) ? null : tipId;
        await loadGallery(STYLE_ID);
    }

    async function fetchHistory(tipId) {
        const resp = await authFetch(`/drawing/generations/${tipId}/history`);
        if (!resp.ok) return [];
        return await resp.json();
    }

    function buildLineageHistory(steps) {
        const wrap = document.createElement('div');
        wrap.className = 'lineage-history';
        steps.forEach(step => {
            const item = document.createElement('div');
            item.className = 'lineage-step';

            const thumb = document.createElement('img');
            thumb.className = 'lineage-step-thumb';
            thumb.src = step.image_url || '';
            thumb.loading = 'lazy';
            thumb.addEventListener('click', (e) => {
                e.stopPropagation();
                openLightbox(step.image_url);
            });
            item.appendChild(thumb);

            const text = document.createElement('div');
            text.className = 'lineage-step-text';
            text.textContent = step.user_input || '';
            item.appendChild(text);

            const delBtn = document.createElement('button');
            delBtn.className = 'lineage-step-del';
            delBtn.type = 'button';
            delBtn.innerHTML = '<i class="material-icons">close</i>';
            delBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (!confirm('删除这一步修改记录？')) return;
                const resp = await authFetch(`/drawing/generations/${step.id}/step`, { method: 'DELETE' });
                if (!resp.ok) return;
                if (expandedGenerationId === step.id) {
                    // 删的正好是当前链的最新一步：展开目标回退到它的上一步（若本来就是唯一一步，则整条链已被删空）。
                    expandedGenerationId = step.parent_generation_id || null;
                }
                await loadGallery(STYLE_ID);
            });
            item.appendChild(delBtn);

            wrap.appendChild(item);
        });
        return wrap;
    }

    function buildDelButton(tip, confirmText) {
        const delBtn = document.createElement('button');
        delBtn.className = 'lineage-row-del';
        delBtn.type = 'button';
        delBtn.innerHTML = '<i class="material-icons">close</i>';
        delBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm(confirmText)) return;
            const resp = await authFetch(`/drawing/generations/${tip.id}`, { method: 'DELETE' });
            if (!resp.ok) return;
            if (expandedGenerationId === tip.id) expandedGenerationId = null;
            stopPolling(tip.id);
            await loadGallery(STYLE_ID);
        });
        return delBtn;
    }

    function buildLineageRowCollapsed(tip) {
        const row = document.createElement('div');
        row.className = 'lineage-row';

        const main = document.createElement('div');
        main.className = 'lineage-row-main';

        // ── 后台出图中：占位卡 + 进度提示，不可展开/放大 ──
        if (tip.status === 'processing' || tip.status === 'pending') {
            row.classList.add('pending');
            const ph = document.createElement('div');
            ph.className = 'lineage-row-thumb lineage-row-thumb-state';
            ph.innerHTML = '<span class="generate-spinner"></span>';
            main.appendChild(ph);
            const text = document.createElement('div');
            text.className = 'lineage-row-text';
            text.innerHTML = escapeHtml(tip.user_input || '') +
                '<div class="gen-hint">出图中…可能需要 1-3 分钟，可离开本页面稍后回来查看</div>';
            main.appendChild(text);
            row.appendChild(main);
            return row;
        }

        // ── 失败：错误信息 + 删除按钮 ──
        if (tip.status === 'failed') {
            row.classList.add('failed');
            const ph = document.createElement('div');
            ph.className = 'lineage-row-thumb lineage-row-thumb-state failed';
            ph.innerHTML = '<i class="material-icons">error_outline</i>';
            main.appendChild(ph);
            const text = document.createElement('div');
            text.className = 'lineage-row-text';
            text.innerHTML = (tip.user_input ? escapeHtml(tip.user_input) + '<br>' : '') +
                '<span class="gen-error">' + escapeHtml(tip.error_msg || '图片生成失败，请稍后重试') + '</span>';
            main.appendChild(text);
            main.appendChild(buildDelButton(tip, '删除这条失败记录？'));
            row.appendChild(main);
            return row;
        }

        const thumb = document.createElement('img');
        thumb.className = 'lineage-row-thumb';
        thumb.src = tip.image_url || '';
        thumb.loading = 'lazy';
        thumb.addEventListener('click', (e) => {
            e.stopPropagation();
            openLightbox(tip.image_url);
        });
        main.appendChild(thumb);

        const text = document.createElement('span');
        text.className = 'lineage-row-text';
        text.textContent = tip.user_input || '';
        main.appendChild(text);

        main.appendChild(buildDelButton(tip, '删除这张图片将同时删除其全部创作历史，确定继续？'));

        main.addEventListener('click', () => toggleLineageExpand(tip.id));
        row.appendChild(main);
        return row;
    }

    async function loadGallery(styleId) {
        const resp = await authFetch(`/drawing/styles/${styleId}/generations`);
        if (!resp.ok) return;
        const tips = await resp.json();
        const gallery = document.getElementById('generation-gallery');
        gallery.innerHTML = '';
        if (!tips.length) {
            gallery.innerHTML = '<p class="gallery-empty" id="gallery-empty-hint">暂无生成记录，在下方输入文字开始作图</p>';
            expandedGenerationId = null;
            for (const id of [...activePolls.keys()]) stopPolling(id);
            updateInputAreaState();
            return;
        }
        // 展开目标只有在它仍然是某条链的当前 tip 时才继续生效（可能因为编辑/删除而已经变化）。
        if (expandedGenerationId && !tips.some(t => t.id === expandedGenerationId)) {
            expandedGenerationId = null;
        }
        for (const tip of tips) {
            const row = buildLineageRowCollapsed(tip);
            if (tip.id === expandedGenerationId && tip.status === 'done') {
                row.classList.add('expanded');
                const chain = await fetchHistory(tip.id); // 根 → 尖（时间正序）
                // 尖（链上最新一步）已经展示在折叠行头部，展开列表不重复显示；
                // 其余步骤按"最新在上"倒序排列，紧接在行头下方。
                const earlierSteps = chain.slice(0, -1).reverse();
                if (earlierSteps.length) {
                    row.appendChild(buildLineageHistory(earlierSteps));
                }
            }
            gallery.appendChild(row);
        }

        // 续上/清理后台出图轮询：processing 的自动开始轮询（切页面回来也能恢复），其余的停掉。
        const liveIds = new Set();
        for (const tip of tips) {
            if (tip.status === 'processing' || tip.status === 'pending') {
                liveIds.add(tip.id);
                startPolling(tip.id);
            }
        }
        for (const id of [...activePolls.keys()]) {
            if (!liveIds.has(id)) stopPolling(id);
        }

        updateInputAreaState();
    }

    // ---------------- Lightbox ----------------
    function openLightbox(url) {
        document.getElementById('lightbox-img').src = url;
        document.getElementById('image-lightbox-overlay').classList.add('open');
    }
    function closeLightbox() {
        document.getElementById('image-lightbox-overlay').classList.remove('open');
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ---------------- 绑定选择器：选择 Prompt（多选） ----------------
    function openPickPromptsModal() {
        pickPromptsSelection = new Set(pendingPromptIds);
        document.getElementById('pick-prompts-filter').value = '';
        renderPickPromptsList('');
        M.Modal.getInstance(document.getElementById('modal-pick-prompts')).open();
    }

    function renderPickPromptsList(filterText) {
        const list = document.getElementById('pick-prompts-list');
        const needle = (filterText || '').trim().toLowerCase();
        const items = allPrompts.filter(p => !needle || p.name.toLowerCase().includes(needle));
        list.innerHTML = '';
        if (!items.length) {
            list.innerHTML = '<p class="bound-empty-hint">没有匹配的 Prompt</p>';
            return;
        }
        items.forEach(prompt => {
            const row = document.createElement('div');
            row.className = 'prompt-check-item' + (pickPromptsSelection.has(prompt.id) ? ' selected' : '');
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.id = 'pick-prompt-cb-' + prompt.id;
            cb.checked = pickPromptsSelection.has(prompt.id);
            cb.addEventListener('change', () => {
                if (cb.checked) pickPromptsSelection.add(prompt.id);
                else pickPromptsSelection.delete(prompt.id);
                row.classList.toggle('selected', cb.checked);
            });
            const labelWrap = document.createElement('span');
            labelWrap.className = 'prompt-check-label';
            labelWrap.textContent = prompt.name;
            const wrap = document.createElement('div');
            wrap.appendChild(labelWrap);
            if (prompt.content) {
                const snip = document.createElement('div');
                snip.className = 'prompt-check-content';
                snip.textContent = prompt.content.length > 60 ? prompt.content.slice(0, 60) + '…' : prompt.content;
                wrap.appendChild(snip);
            }
            row.appendChild(cb);
            row.appendChild(wrap);
            row.addEventListener('click', (e) => {
                if (e.target === cb) return;
                cb.click();
            });
            list.appendChild(row);
        });
    }

    async function confirmPickPrompts() {
        const newIds = Array.from(pickPromptsSelection);
        const ok = await patchStyleBinding({ prompt_ids: newIds });
        if (ok) {
            pendingPromptIds = newIds;
            renderBoundPromptList();
            M.toast({ html: '已更新绑定的 Prompt', classes: 'green darken-1' });
        } else {
            await loadStyle(STYLE_ID);
        }
        M.Modal.getInstance(document.getElementById('modal-pick-prompts')).close();
    }

    // ---------------- 绑定选择器：选择 Skill 包（单选） ----------------
    function openPickSkillPackageModal() {
        document.getElementById('pick-skill-package-filter').value = '';
        renderPickSkillPackageList('');
        M.Modal.getInstance(document.getElementById('modal-pick-skill-package')).open();
    }

    function renderPickSkillPackageList(filterText) {
        const list = document.getElementById('pick-skill-package-list');
        const needle = (filterText || '').trim().toLowerCase();
        const items = allSkillPackages.filter(p => !needle || p.name.toLowerCase().includes(needle));
        list.innerHTML = '';

        const allRadioRows = [];
        function syncSelectedClasses() {
            allRadioRows.forEach(({ row, radio }) => row.classList.toggle('selected', radio.checked));
        }

        const noneRow = document.createElement('div');
        noneRow.className = 'prompt-check-item';
        const noneRadio = document.createElement('input');
        noneRadio.type = 'radio';
        noneRadio.name = 'pick-skill-package-radio';
        noneRadio.id = 'pick-skill-package-none';
        noneRadio.value = '';
        noneRadio.checked = !pendingSkillPackageId;
        noneRadio.addEventListener('change', syncSelectedClasses);
        const noneLabel = document.createElement('span');
        noneLabel.className = 'prompt-check-label';
        noneLabel.textContent = '（不绑定）';
        noneRow.appendChild(noneRadio);
        noneRow.appendChild(noneLabel);
        noneRow.addEventListener('click', (e) => { if (e.target !== noneRadio) noneRadio.click(); });
        list.appendChild(noneRow);
        allRadioRows.push({ row: noneRow, radio: noneRadio });

        if (!items.length && needle) {
            const hint = document.createElement('p');
            hint.className = 'bound-empty-hint';
            hint.textContent = '没有匹配的 Skill 包';
            list.appendChild(hint);
        }
        items.forEach(pkg => {
            const row = document.createElement('div');
            row.className = 'prompt-check-item';
            const radio = document.createElement('input');
            radio.type = 'radio';
            radio.name = 'pick-skill-package-radio';
            radio.id = 'pick-skill-package-' + pkg.id;
            radio.value = pkg.id;
            radio.checked = pendingSkillPackageId === pkg.id;
            radio.addEventListener('change', syncSelectedClasses);
            const labelWrap = document.createElement('span');
            labelWrap.className = 'prompt-check-label';
            labelWrap.textContent = pkg.name;
            const wrap = document.createElement('div');
            wrap.appendChild(labelWrap);
            if (pkg.source_url) {
                const snip = document.createElement('div');
                snip.className = 'prompt-check-content';
                snip.textContent = pkg.source_url;
                wrap.appendChild(snip);
            }
            row.appendChild(radio);
            row.appendChild(wrap);
            row.addEventListener('click', (e) => { if (e.target !== radio) radio.click(); });
            list.appendChild(row);
            allRadioRows.push({ row, radio });
        });

        syncSelectedClasses();
    }

    async function confirmPickSkillPackage() {
        const checked = document.querySelector('input[name="pick-skill-package-radio"]:checked');
        const newId = checked ? checked.value : '';
        const ok = await patchStyleBinding({ skill_package_id: newId });
        if (ok) {
            pendingSkillPackageId = newId;
            renderBoundSkillPackage();
            M.toast({ html: '已更新绑定的 Skill 包', classes: 'green darken-1' });
        } else {
            await loadStyle(STYLE_ID);
        }
        M.Modal.getInstance(document.getElementById('modal-pick-skill-package')).close();
    }
})();
