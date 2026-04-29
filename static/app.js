// Global state
let ws = null;
let currentStats = {};
let currentTaskProgress = {
    taskId: null,
    conversationId: null,
    turn: 0,
    max_turns: 10,
    status: 'idle'
};

// 创建一个 turn 渲染状态：每条 turn 拥有独立的容器与流式渲染游标，
// 这样在对话视图里多轮事件不会互相串扰。
function newTurnState(container) {
    return {
        container,
        lastSeq: 0,
        rendered: new Set(),
        bufferedMissing: [],
        pendingFetch: false,
        blocks: {},
        activeMessageEl: null,
        toolBlocks: {},
        eventCount: 0,
    };
}

// ============ 实时事件流模块 ============
// 同时支持两种模式：
// - mode = 'task'：单 task 视图（保留旧的任务详情交互）
// - mode = 'conversation'：对话视图，按 taskId 区分多个 turn 容器
const taskEventStream = {
    mode: null,
    taskId: null,           // 单 task 模式下当前订阅的 task
    conversationId: null,   // 对话模式下当前订阅的对话
    turns: {},              // 对话模式：taskId -> turnState
    streamEl: null,
    metaEl: null,
    wrapperEl: null,
    singleState: null,      // 单 task 模式下的 turn state（容器为 streamEl）

    bind() {
        this.streamEl = document.getElementById('taskEventStream');
        this.metaEl = document.getElementById('eventStreamMeta');
        this.wrapperEl = document.getElementById('taskEventStreamWrapper');
    },

    reset(taskId) {
        this.bind();
        this.mode = 'task';
        this.taskId = taskId;
        this.conversationId = null;
        this.turns = {};
        if (this.streamEl) this.streamEl.innerHTML = '';
        if (this.metaEl) this.metaEl.textContent = '加载历史事件...';
        this.singleState = newTurnState(this.streamEl);
    },

    show() {
        if (this.wrapperEl) this.wrapperEl.style.display = 'block';
    },

    hide() {
        if (this.wrapperEl) this.wrapperEl.style.display = 'none';
    },

    setMeta(text) {
        if (this.metaEl) this.metaEl.textContent = text;
    },

    isActive(taskId) {
        if (this.mode === 'task') return this.taskId && this.taskId === taskId;
        if (this.mode === 'conversation') return !!this.turns[taskId];
        return false;
    },

    isActiveConversation(conversationId) {
        return this.mode === 'conversation' && this.conversationId === conversationId;
    },

    // ---------------- 单 task 模式 ----------------
    async open(taskId) {
        this.reset(taskId);
        this.show();
        await this.fetchSince(0);
        if (this.singleState && this.singleState.eventCount === 0) {
            this.setMeta('暂无事件，等待客户端输出...');
        }
    },

    close() {
        this.mode = null;
        this.taskId = null;
        this.conversationId = null;
        this.turns = {};
        this.singleState = null;
        this.hide();
    },

    async fetchSince(sinceSeq) {
        const state = this.singleState;
        if (!state || state.pendingFetch) return;
        state.pendingFetch = true;
        try {
            const taskId = this.taskId;
            const data = await apiGet(`/api/tasks/${taskId}/events?since_seq=${sinceSeq}&limit=2000`);
            if (this.taskId !== taskId) return;
            if (data && data.data && Array.isArray(data.data.events)) {
                for (const evt of data.data.events) {
                    this._applyEventTo(state, evt);
                }
            }
        } catch (err) {
            console.error('fetch events failed', err);
        } finally {
            state.pendingFetch = false;
        }
        this._flushBuffered(state);
    },

    handleWsEvent(msg) {
        if (this.mode === 'task') {
            if (msg.task_id !== this.taskId) return;
            this._handleWsForState(this.singleState, msg, () => this.fetchSince(this.singleState.lastSeq));
            return;
        }
        if (this.mode === 'conversation') {
            const state = this.turns[msg.task_id];
            if (!state) return;
            this._handleWsForState(state, msg,
                () => this._fetchHistoryForTurn(msg.task_id));
        }
    },

    // ---------------- 对话模式 ----------------
    activateConversation(conversationId) {
        this.mode = 'conversation';
        this.conversationId = conversationId;
        this.turns = {};
        this.taskId = null;
        this.singleState = null;
    },

    deactivateConversation() {
        if (this.mode === 'conversation') {
            this.mode = null;
            this.conversationId = null;
            this.turns = {};
        }
    },

    registerTurn(taskId, container) {
        if (!taskId || !container) return;
        if (this.turns[taskId]) {
            this.turns[taskId].container = container;
            return;
        }
        this.turns[taskId] = newTurnState(container);
    },

    async hydrateTurnHistory(taskId) {
        const state = this.turns[taskId];
        if (!state || state.pendingFetch) return;
        state.pendingFetch = true;
        try {
            const data = await apiGet(`/api/tasks/${taskId}/events?since_seq=0&limit=2000`);
            if (!this.turns[taskId]) return;
            if (data && data.data && Array.isArray(data.data.events)) {
                for (const evt of data.data.events) {
                    this._applyEventTo(state, evt);
                }
            }
        } catch (err) {
            console.error('hydrateTurnHistory failed', err);
        } finally {
            state.pendingFetch = false;
        }
        this._flushBuffered(state);
    },

    async _fetchHistoryForTurn(taskId) {
        const state = this.turns[taskId];
        if (!state) return;
        if (state.pendingFetch) return;
        state.pendingFetch = true;
        try {
            const data = await apiGet(`/api/tasks/${taskId}/events?since_seq=${state.lastSeq}&limit=2000`);
            if (!this.turns[taskId]) return;
            if (data && data.data && Array.isArray(data.data.events)) {
                for (const evt of data.data.events) {
                    this._applyEventTo(state, evt);
                }
            }
        } catch (err) {
            console.error('fetch turn history failed', err);
        } finally {
            state.pendingFetch = false;
        }
        this._flushBuffered(state);
    },

    // ---------------- 内部：状态化的渲染逻辑 ----------------
    _handleWsForState(state, msg, refetch) {
        if (!state) return;
        const seq = msg.seq;
        if (seq <= state.lastSeq) return;
        if (seq === state.lastSeq + 1) {
            this._applyEventTo(state, {
                seq,
                event_type: msg.event_type,
                payload: msg.payload,
                timestamp: msg.timestamp,
            });
            return;
        }
        state.bufferedMissing.push({
            seq,
            event_type: msg.event_type,
            payload: msg.payload,
            timestamp: msg.timestamp,
        });
        if (typeof refetch === 'function') refetch();
    },

    _flushBuffered(state) {
        if (!state || state.bufferedMissing.length === 0) return;
        const queued = state.bufferedMissing.sort((a, b) => a.seq - b.seq);
        state.bufferedMissing = [];
        for (const evt of queued) {
            this._applyEventTo(state, evt);
        }
    },

    _applyEventTo(state, evt) {
        if (!state || state.rendered.has(evt.seq)) return;
        state.rendered.add(evt.seq);
        state.lastSeq = Math.max(state.lastSeq, evt.seq);
        state.eventCount += 1;
        if (this.mode === 'task' && state === this.singleState) {
            this.setMeta(`事件数 ${state.eventCount} · 最新序号 ${state.lastSeq}`);
        }
        this._renderEventInto(state, evt);
        if (state.container) {
            state.container.scrollTop = state.container.scrollHeight;
        }
    },

    _renderEventInto(state, evt) {
        const type = evt.event_type;
        const payload = evt.payload || {};
        switch (type) {
            case 'session_init':
                this._renderSimple(state, 'session', '<i class="fa fa-sign-in"></i> 会话初始化',
                    `model=${escapeHtml(payload.model || '')} · session=${escapeHtml(payload.session_id || '')} · permission=${escapeHtml(payload.permission_mode || '')}`,
                    evt.timestamp);
                break;
            case 'message_start':
                this._beginAssistantMessage(state, evt.timestamp);
                break;
            case 'content_block_start': {
                const block = payload.content_block || {};
                this._beginContentBlock(state, payload.index, block);
                break;
            }
            case 'text_delta':
                this._appendTextDelta(state, payload.index, payload.text || '');
                break;
            case 'thinking_delta':
                this._appendThinkingDelta(state, payload.index, payload.thinking || '');
                break;
            case 'tool_input_delta':
                this._appendToolInputDelta(state, payload.index, payload.partial_json || '');
                break;
            case 'content_block_stop':
                this._finalizeContentBlock(state, payload.index);
                break;
            case 'message_delta':
                if (payload.delta && payload.delta.stop_reason) {
                    this._markMessageFooter(state, payload.delta.stop_reason);
                }
                break;
            case 'message_stop':
                state.activeMessageEl = null;
                state.blocks = {};
                break;
            case 'assistant_message':
                this._renderAssistantSummary(state, payload, evt.timestamp);
                break;
            case 'tool_result':
                this._renderToolResult(state, payload, evt.timestamp);
                break;
            case 'api_retry':
                this._renderApiRetry(state, payload, evt.timestamp);
                break;
            case 'rate_limit':
                this._renderRateLimit(state, payload, evt.timestamp);
                break;
            case 'stderr':
                this._renderStderr(state, payload, evt.timestamp);
                break;
            case 'result':
                this._renderResult(state, payload, evt.timestamp);
                break;
            case 'system_init':
                break;
            default:
                this._renderSimple(state, 'misc', `<i class="fa fa-circle-o"></i> ${escapeHtml(type)}`,
                    `<pre class="evt-json">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`,
                    evt.timestamp);
        }
    },

    _renderSimple(state, klass, header, body, ts) {
        if (!state.container) return;
        const card = document.createElement('div');
        card.className = `evt-card ${klass}`;
        card.innerHTML = `
            <div class="evt-card-header">
                <span class="evt-title">${header}</span>
                <span class="evt-time">${formatTs(ts)}</span>
            </div>
            <div class="evt-card-body">${body}</div>
        `;
        state.container.appendChild(card);
    },

    _beginAssistantMessage(state, ts) {
        if (!state.container) return;
        const card = document.createElement('div');
        card.className = 'evt-card assistant';
        card.innerHTML = `
            <div class="evt-card-header">
                <span class="evt-title"><i class="fa fa-comment"></i> Assistant</span>
                <span class="evt-time">${formatTs(ts)}</span>
            </div>
            <div class="evt-blocks"></div>
            <div class="evt-card-footer" style="display:none"></div>
        `;
        state.container.appendChild(card);
        state.activeMessageEl = card;
        state.blocks = {};
    },

    _ensureActiveMessage(state) {
        if (!state.activeMessageEl) {
            this._beginAssistantMessage(state, Date.now() / 1000);
        }
        return state.activeMessageEl;
    },

    _beginContentBlock(state, index, block) {
        const parent = this._ensureActiveMessage(state);
        if (!parent) return;
        const blocksWrap = parent.querySelector('.evt-blocks');
        const node = document.createElement('div');
        if (block && block.type === 'tool_use') {
            node.className = 'evt-block evt-block-tool';
            const toolName = block.name || 'tool';
            const toolUseId = block.id || `tool-${index}`;
            node.innerHTML = `
                <div class="evt-block-header">
                    <i class="fa fa-wrench"></i>
                    <span class="evt-tool-name">${escapeHtml(toolName)}</span>
                    <span class="evt-tool-id">${escapeHtml(toolUseId)}</span>
                </div>
                <pre class="evt-tool-input"><code></code></pre>
            `;
            state.toolBlocks[toolUseId] = node;
            node.dataset.toolUseId = toolUseId;
        } else if (block && block.type === 'thinking') {
            node.className = 'evt-block evt-block-thinking';
            node.innerHTML = `<div class="evt-block-header"><i class="fa fa-lightbulb-o"></i> Thinking</div><div class="evt-text"></div>`;
        } else {
            node.className = 'evt-block evt-block-text';
            node.innerHTML = `<div class="evt-text"></div>`;
        }
        blocksWrap.appendChild(node);
        state.blocks[index] = node;
    },

    _appendTextDelta(state, index, text) {
        let node = state.blocks[index];
        if (!node) {
            this._beginContentBlock(state, index, { type: 'text' });
            node = state.blocks[index];
        }
        if (!node) return;
        const target = node.querySelector('.evt-text');
        if (!target) return;
        target.appendChild(document.createTextNode(text));
    },

    _appendThinkingDelta(state, index, thinking) {
        let node = state.blocks[index];
        if (!node) {
            this._beginContentBlock(state, index, { type: 'thinking' });
            node = state.blocks[index];
        }
        if (!node) return;
        const target = node.querySelector('.evt-text');
        if (!target) return;
        target.appendChild(document.createTextNode(thinking));
    },

    _appendToolInputDelta(state, index, partialJson) {
        const node = state.blocks[index];
        if (!node) return;
        const target = node.querySelector('.evt-tool-input code');
        if (!target) return;
        target.appendChild(document.createTextNode(partialJson));
    },

    _finalizeContentBlock(state, index) {
        const node = state.blocks[index];
        if (!node) return;
        node.classList.add('evt-block-done');
    },

    _markMessageFooter(state, stopReason) {
        const card = state.activeMessageEl;
        if (!card) return;
        const footer = card.querySelector('.evt-card-footer');
        if (footer) {
            footer.style.display = '';
            footer.textContent = `stop_reason=${stopReason}`;
        }
    },

    _renderAssistantSummary(state, payload, ts) {
        if (state.activeMessageEl) return;
        const content = Array.isArray(payload.content) ? payload.content : [];
        if (content.length === 0) return;
        const card = document.createElement('div');
        card.className = 'evt-card assistant';
        let blocksHtml = '';
        for (const block of content) {
            if (!block) continue;
            if (block.type === 'text') {
                blocksHtml += `<div class="evt-block evt-block-text"><div class="evt-text">${escapeHtml(block.text || '')}</div></div>`;
            } else if (block.type === 'tool_use') {
                blocksHtml += `
                    <div class="evt-block evt-block-tool" data-tool-use-id="${escapeHtml(block.id || '')}">
                        <div class="evt-block-header">
                            <i class="fa fa-wrench"></i>
                            <span class="evt-tool-name">${escapeHtml(block.name || 'tool')}</span>
                            <span class="evt-tool-id">${escapeHtml(block.id || '')}</span>
                        </div>
                        <pre class="evt-tool-input"><code>${escapeHtml(JSON.stringify(block.input || {}, null, 2))}</code></pre>
                    </div>`;
            } else if (block.type === 'thinking') {
                blocksHtml += `<div class="evt-block evt-block-thinking"><div class="evt-block-header"><i class="fa fa-lightbulb-o"></i> Thinking</div><div class="evt-text">${escapeHtml(block.thinking || '')}</div></div>`;
            }
        }
        card.innerHTML = `
            <div class="evt-card-header">
                <span class="evt-title"><i class="fa fa-comment"></i> Assistant 消息（turn ${payload.turn || ''}）</span>
                <span class="evt-time">${formatTs(ts)}</span>
            </div>
            <div class="evt-blocks">${blocksHtml}</div>
        `;
        state.container.appendChild(card);
        for (const block of content) {
            if (block && block.type === 'tool_use' && block.id) {
                const node = card.querySelector(`[data-tool-use-id="${cssEscape(block.id)}"]`);
                if (node) state.toolBlocks[block.id] = node;
            }
        }
    },

    _renderToolResult(state, payload, ts) {
        const content = Array.isArray(payload.content) ? payload.content : [];
        for (const block of content) {
            if (!block || block.type !== 'tool_result') continue;
            const toolUseId = block.tool_use_id;
            const isError = !!block.is_error;
            const text = formatToolResultContent(block.content);
            const card = document.createElement('div');
            card.className = `evt-card tool-result ${isError ? 'tool-error' : ''}`;
            card.innerHTML = `
                <div class="evt-card-header">
                    <span class="evt-title"><i class="fa fa-${isError ? 'times-circle' : 'check-circle'}"></i> 工具结果${toolUseId ? ' · ' + escapeHtml(toolUseId) : ''}</span>
                    <span class="evt-time">${formatTs(ts)}</span>
                </div>
                <pre class="evt-tool-result"><code>${escapeHtml(text)}</code></pre>
            `;
            state.container.appendChild(card);
        }
    },

    _renderApiRetry(state, payload, ts) {
        const card = document.createElement('div');
        card.className = 'evt-card retry';
        card.innerHTML = `
            <div class="evt-card-header">
                <span class="evt-title"><i class="fa fa-exclamation-triangle"></i> API 重试</span>
                <span class="evt-time">${formatTs(ts)}</span>
            </div>
            <div class="evt-card-body">
                attempt ${escapeHtml(String(payload.attempt ?? '?'))}/${escapeHtml(String(payload.max_retries ?? '?'))} · 状态码 ${escapeHtml(String(payload.error_status ?? ''))} · 等待 ${escapeHtml(String(payload.retry_delay_ms ?? '?'))}ms
                ${payload.error ? `<div class="evt-mono">${escapeHtml(String(payload.error))}</div>` : ''}
            </div>
        `;
        state.container.appendChild(card);
    },

    _renderRateLimit(state, payload, ts) {
        const info = payload.rate_limit_info || {};
        this._renderSimple(state, 'rate-limit',
            '<i class="fa fa-tachometer"></i> Rate Limit',
            `<pre class="evt-json">${escapeHtml(JSON.stringify(info, null, 2))}</pre>`,
            ts);
    },

    _renderStderr(state, payload, ts) {
        this._renderSimple(state, 'stderr',
            '<i class="fa fa-terminal"></i> stderr',
            `<pre class="evt-mono">${escapeHtml(payload.text || '')}</pre>`,
            ts);
    },

    _renderResult(state, payload, ts) {
        const ok = !payload.is_error;
        const card = document.createElement('div');
        card.className = `evt-card result ${ok ? 'ok' : 'fail'}`;
        const usage = payload.usage ? `<pre class="evt-json">${escapeHtml(JSON.stringify(payload.usage, null, 2))}</pre>` : '';
        card.innerHTML = `
            <div class="evt-card-header">
                <span class="evt-title"><i class="fa fa-flag-checkered"></i> 终态 · ${escapeHtml(payload.subtype || '')}</span>
                <span class="evt-time">${formatTs(ts)}</span>
            </div>
            <div class="evt-card-body">
                turns=${escapeHtml(String(payload.num_turns ?? ''))} · 耗时=${escapeHtml(String(payload.duration_ms ?? ''))}ms · cost=$${escapeHtml(String(payload.total_cost_usd ?? '0'))}
            </div>
            ${payload.result ? `<pre class="evt-result"><code>${escapeHtml(payload.result)}</code></pre>` : ''}
            ${usage}
        `;
        state.container.appendChild(card);
    },
};

function formatTs(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    if (Number.isNaN(d.getTime())) return '';
    return d.toLocaleTimeString();
}

function formatToolResultContent(content) {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
        return content.map(item => {
            if (!item) return '';
            if (typeof item === 'string') return item;
            if (item.type === 'text') return item.text || '';
            return JSON.stringify(item);
        }).join('\n');
    }
    if (content == null) return '';
    try { return JSON.stringify(content, null, 2); } catch (e) { return String(content); }
}

function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/[^a-zA-Z0-9_\-]/g, '\\$&');
}

// Initialize
document.addEventListener('DOMContentLoaded', () => {
    initTabs();
    connectWebSocket();
    refreshAll();
    setInterval(refreshStats, 5000);
});

// Tab switching
function initTabs() {
    const tabs = document.querySelectorAll('.tab-btn');
    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            tab.classList.add('active');

            document.querySelectorAll('.tab-content').forEach(content => {
                content.classList.remove('active');
            });
            document.getElementById(tab.dataset.tab).classList.add('active');

            if (tab.dataset.tab === 'clients') loadClients();
            if (tab.dataset.tab === 'agents') loadAgents();
            if (tab.dataset.tab === 'conversations') loadConversations();
            if (tab.dataset.tab === 'tasks') loadTasks();
        });
    });
}

// WebSocket connection
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${protocol}//${window.location.host}/ws/frontend`);

    ws.onopen = () => {
        document.getElementById('wsDot').className = 'status-dot online';
        document.getElementById('wsText').textContent = 'WebSocket 已连接';
        showToast('WebSocket 已连接', 'success');
        if (taskEventStream.taskId) {
            // 重连后用当前 lastSeq 拉补齐缺失事件
            taskEventStream.fetchSince(taskEventStream.lastSeq);
        }
    };

    ws.onmessage = (event) => {
        const message = JSON.parse(event.data);
        handleWebSocketMessage(message);
    };

    ws.onclose = () => {
        document.getElementById('wsDot').className = 'status-dot offline';
        document.getElementById('wsText').textContent = 'WebSocket 断开';
        setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = (error) => {
        console.error('WebSocket error:', error);
    };
}

function handleWebSocketMessage(message) {
    console.log('WebSocket message:', message);

    switch (message.type) {
        case 'client_connected':
        case 'client_disconnected':
            refreshStats();
            loadClients();
            // 客户端在线状态变化会影响对话页的"发送"按钮可用性
            if (currentConversation && currentConversation.id) {
                refreshConversationDetail(currentConversation.id);
            }
            break;
        case 'task_started':
            refreshStats();
            loadTasks();
            loadRecentTasks();
            // 对话视图下，更新对应 turn 的状态
            if (taskEventStream.isActiveConversation(message.conversation_id)) {
                markTurnStatus(message.task_id, 'running');
            }
            break;
        case 'task_progress':
            updateTaskProgressRealtime(message.task_id, message.progress);
            break;
        case 'task_event':
            taskEventStream.handleWsEvent(message);
            break;
        case 'task_completed':
        case 'task_failed':
        case 'task_cancelled':
            refreshStats();
            loadTasks();
            loadRecentTasks();
            // 对话视图：更新 turn 状态、刷新元信息条与输入框可用性
            if (taskEventStream.isActiveConversation(message.conversation_id)) {
                const status = message.type === 'task_completed' ? 'completed'
                    : message.type === 'task_failed' ? 'failed' : 'cancelled';
                markTurnStatus(message.task_id, status);
                refreshConversationDetail(message.conversation_id);
            }
            // 任务列表也刷一下
            loadConversations();
            break;
        case 'conversation_created':
        case 'conversation_message_queued':
            loadConversations();
            break;
        case 'user_confirmation_request':
            showUserConfirmation(message.client_id, message.request);
            break;
    }
}

// API helpers
async function apiGet(endpoint) {
    try {
        const response = await fetch(endpoint);
        return await response.json();
    } catch (error) {
        console.error('API error:', error);
        return null;
    }
}

async function apiPost(endpoint, data) {
    try {
        const response = await fetch(endpoint, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return await response.json();
    } catch (error) {
        console.error('API error:', error);
        return null;
    }
}

async function apiPut(endpoint, data) {
    try {
        const response = await fetch(endpoint, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        return await response.json();
    } catch (error) {
        console.error('API error:', error);
        return null;
    }
}

async function apiDelete(endpoint) {
    try {
        const response = await fetch(endpoint, {
            method: 'DELETE'
        });
        return await response.json();
    } catch (error) {
        console.error('API error:', error);
        return null;
    }
}

// Refresh all data
function refreshAll() {
    refreshStats();
    loadClients();
    loadAgents();
    loadConversations();
    loadTasks();
    loadRecentTasks();
}

// Statistics
async function refreshStats() {
    const data = await apiGet('/api/stats');
    if (data && data.data) {
        const stats = data.data;
        document.getElementById('totalClients').textContent = stats.clients.total;
        document.getElementById('onlineClientsBadge').textContent = `${stats.clients.online} 在线`;
        document.getElementById('totalAgents').textContent = stats.agents.total;
        document.getElementById('totalTasks').textContent = stats.tasks.total;
        document.getElementById('completedTasks').textContent = stats.tasks.completed;
        currentStats = stats;
    }
}

// Clients
async function loadClients() {
    const container = document.getElementById('clientList');
    const data = await apiGet('/api/clients');

    if (!data || !data.data || data.data.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无客户端</div>';
        return;
    }

    container.innerHTML = data.data.map(client => `
        <div class="client-item">
            <div class="client-header">
                <span class="client-name">
                    ${client.name}
                    <span class="client-id">(${client.id})</span>
                </span>
                <div class="client-actions">
                    <span class="client-status ${client.is_online ? 'online' : 'offline'}">
                        ${client.is_online ? '在线' : '离线'}
                    </span>
                    <button class="btn btn-secondary btn-sm" onclick="showClientDetail('${client.id}')">
                        <i class="fa fa-info-circle"></i> 详情
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="showEditClientModal('${client.id}', '${escapeHtml(client.name)}', '${escapeHtml(client.description || '')}')">
                        <i class="fa fa-edit"></i> 编辑
                    </button>
                    <button class="btn btn-danger btn-sm" onclick="showConfirmDelete('client', '${client.id}', '${escapeHtml(client.name)}')">
                        <i class="fa fa-trash"></i> 删除
                    </button>
                </div>
            </div>
            <div class="client-meta">
                ${client.description || '无描述'}
                ${client.last_connected_at ? `<br>最后连接: ${new Date(client.last_connected_at).toLocaleString()}` : ''}
            </div>
        </div>
    `).join('');
}

function showCreateClientModal() {
    document.getElementById('clientName').value = '';
    document.getElementById('clientDescription').value = '';
    document.getElementById('createClientModal').classList.add('active');
}

async function createClient() {
    const name = document.getElementById('clientName').value.trim();
    if (!name) {
        showToast('请输入客户端名称', 'error');
        return;
    }

    const description = document.getElementById('clientDescription').value.trim();
    const result = await apiPost('/api/clients', { name, description });

    if (result && result.data) {
        showToast('客户端创建成功', 'success');
        closeModal('createClientModal');
        loadClients();
        refreshStats();
    } else {
        showToast('创建失败', 'error');
    }
}

// 当前编辑的客户端 ID
let currentEditingClientId = null;

function showEditClientModal(clientId, name, description) {
    currentEditingClientId = clientId;
    document.getElementById('editClientName').value = name;
    document.getElementById('editClientDescription').value = description || '';
    document.getElementById('editClientModal').classList.add('active');
}

async function updateClient() {
    if (!currentEditingClientId) {
        showToast('未知的客户端', 'error');
        return;
    }

    const name = document.getElementById('editClientName').value.trim();
    if (!name) {
        showToast('请输入客户端名称', 'error');
        return;
    }

    const description = document.getElementById('editClientDescription').value.trim();
    const result = await apiPut(`/api/clients/${currentEditingClientId}`, { name, description });

    if (result && result.data) {
        showToast('更新成功', 'success');
        closeModal('editClientModal');
        loadClients();
        refreshStats();
    } else {
        showToast('更新失败', 'error');
    }
}

// 全局删除确认状态
let deleteInfo = {
    type: null,
    id: null
};

function showConfirmDelete(type, id, name) {
    deleteInfo.type = type;
    deleteInfo.id = id;
    document.getElementById('deleteConfirmMessage').textContent = `确定要删除 ${name} 吗？此操作不可撤销。`;
    document.getElementById('confirmDeleteModal').classList.add('active');
}

async function confirmDelete() {
    if (!deleteInfo.type || !deleteInfo.id) {
        showToast('无效的删除操作', 'error');
        return;
    }

    let url = '';
    if (deleteInfo.type === 'client') {
        url = `/api/clients/${deleteInfo.id}`;
    } else if (deleteInfo.type === 'agent') {
        url = `/api/agents/${deleteInfo.id}`;
    } else if (deleteInfo.type === 'task') {
        url = `/api/tasks/${deleteInfo.id}`;
    }

    const result = await apiDelete(url);

    if (result && result.data && result.data.success) {
        showToast('删除成功', 'success');
        closeModal('confirmDeleteModal');

        if (deleteInfo.type === 'client') {
            loadClients();
        } else if (deleteInfo.type === 'agent') {
            loadAgents();
        } else if (deleteInfo.type === 'task') {
            loadTasks();
            loadRecentTasks();
        }

        refreshStats();
    } else {
        const errorMsg = result && result.detail ? result.detail : '删除失败，可能存在正在运行的任务';
        showToast(errorMsg, 'error');
    }
}

// 客户端详情
async function showClientDetail(clientId) {
    const container = document.getElementById('clientDetailContent');
    container.innerHTML = '<div class="empty-state">加载中...</div>';
    document.getElementById('clientDetailModal').classList.add('active');

    const data = await apiGet(`/api/clients/${clientId}`);
    if (!data || !data.data) {
        container.innerHTML = '<div class="empty-state">加载失败</div>';
        return;
    }

    const client = data.data;

    let agentsHtml = '';
    if (client.agents && client.agents.length > 0) {
        agentsHtml = `
            <div style="margin-top: 20px;">
                <h4>绑定的 Agent 列表 (${client.agents.length})</h4>
                <div class="agent-list-sm">
                    ${client.agents.map(agent => `
                        <div class="agent-item-sm">
                            <div>
                                <strong>${escapeHtml(agent.name)}</strong>
                                <span class="client-id">(${agent.id})</span>
                            </div>
                            <div class="agent-meta-sm">
                                模型: ${agent.default_model} | ${agent.is_active ? '启用' : '禁用'}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    } else {
        agentsHtml = '<div style="margin-top: 20px;"><p class="empty-state">暂无绑定的 Agent</p></div>';
    }

    container.innerHTML = `
        <div class="detail-info">
            <div class="detail-row">
                <div class="detail-label">客户端 ID</div>
                <div class="detail-value">${client.id}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">名称</div>
                <div class="detail-value">${escapeHtml(client.name)}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">描述</div>
                <div class="detail-value">${client.description || '无描述'}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">状态</div>
                <div class="detail-value">
                    <span class="client-status ${client.is_online ? 'online' : 'offline'}">
                        ${client.is_online ? '在线' : '离线'}
                    </span>
                </div>
            </div>
            ${client.version ? `
            <div class="detail-row">
                <div class="detail-label">客户端版本</div>
                <div class="detail-value">${client.version}</div>
            </div>
            ` : ''}
            ${client.claude_version ? `
            <div class="detail-row">
                <div class="detail-label">Claude 版本</div>
                <div class="detail-value">${client.claude_version}</div>
            </div>
            ` : ''}
            ${client.last_connected_at ? `
            <div class="detail-row">
                <div class="detail-label">最后连接</div>
                <div class="detail-value">${new Date(client.last_connected_at).toLocaleString()}</div>
            </div>
            ` : ''}
            <div class="detail-row">
                <div class="detail-label">创建时间</div>
                <div class="detail-value">${new Date(client.created_at).toLocaleString()}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">Agent 数量</div>
                <div class="detail-value">${client.agent_count}</div>
            </div>
        </div>
        ${agentsHtml}
    `;
}

// Agents
async function loadAgents() {
    const container = document.getElementById('agentList');
    const data = await apiGet('/api/agents');

    if (!data || !data.data || data.data.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无 Agents</div>';
        return;
    }

    container.innerHTML = data.data.map(agent => `
        <div class="agent-item">
            <div class="agent-header">
                <span class="agent-name">
                    ${agent.name}
                    <span class="client-id">(${agent.id})</span>
                </span>
                <div class="agent-actions">
                    <button class="btn btn-secondary btn-sm" onclick="showAgentDetail('${agent.id}')">
                        <i class="fa fa-info-circle"></i> 详情
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="showBindClientModal('${agent.id}', '${agent.client_id || ''}')">
                        <i class="fa fa-link"></i> ${agent.client_id ? '重新绑定' : '绑定'}
                    </button>
                    <button class="btn btn-secondary btn-sm" onclick="showEditAgentModal('${agent.id}', '${escapeHtml(agent.name)}', '${escapeHtml(agent.description || '')}', '${agent.default_model}', ${agent.max_turns}, '${agent.client_id || ''}')">
                        <i class="fa fa-edit"></i> 编辑
                    </button>
                    <button class="btn btn-danger btn-sm" onclick="showConfirmDelete('agent', '${agent.id}', '${escapeHtml(agent.name)}')">
                        <i class="fa fa-trash"></i> 删除
                    </button>
                </div>
            </div>
            <div class="agent-meta">
                ${agent.description || '无描述'}
                <br>模型: ${agent.default_model} | 最大迭代: ${agent.max_turns}
                ${agent.client_id ? `<br>绑定客户端: ${agent.client_id}` : '<br>未绑定客户端'}
            </div>
        </div>
    `).join('');
}

async function loadClientsIntoSelect(selectId, includeEmptyOption, emptyLabel) {
    const select = document.getElementById(selectId);
    const data = await apiGet('/api/clients');
    if (data && data.data && data.data.length >= 0) {
        let options = [];
        if (includeEmptyOption) {
            options.push(`<option value="">${emptyLabel}</option>`);
        }
        if (data && data.data && data.data.length > 0) {
            options = options.concat(data.data.map(client =>
                `<option value="${client.id}">${client.name} (${client.id}) ${client.is_online ? '[在线]' : '[离线]'}</option>`
            ));
        } else {
            options.push('<option value="">无可用客户端</option>');
        }
        select.innerHTML = options.join('');
    } else {
        console.error('加载客户端列表失败');
        select.innerHTML = '<option value="">加载失败，请刷新重试</option>';
    }
}

function showCreateAgentModal() {
    document.getElementById('agentName').value = '';
    document.getElementById('agentDescription').value = '';
    document.getElementById('agentModel').value = 'sonnet';
    document.getElementById('agentMaxTurns').value = 10;

    // 设置初始占位文本
    const clientSelect = document.getElementById('agentClient');
    clientSelect.innerHTML = '<option value="">正在加载...</option>';

    // 先显示模态框
    document.getElementById('createAgentModal').classList.add('active');

    // 异步加载客户端列表
    setTimeout(() => {
        loadClientsIntoSelect('agentClient', true, '未选择（使用默认客户端）');
    }, 10);
}

async function createAgent() {
    const name = document.getElementById('agentName').value.trim();
    if (!name) {
        showToast('请输入 Agent 名称', 'error');
        return;
    }

    const data = {
        name,
        description: document.getElementById('agentDescription').value.trim(),
        default_model: document.getElementById('agentModel').value,
        max_turns: parseInt(document.getElementById('agentMaxTurns').value),
        client_id: document.getElementById('agentClient').value || null
    };

    const result = await apiPost('/api/agents', data);

    if (result && result.data) {
        showToast('Agent 创建成功', 'success');
        closeModal('createAgentModal');
        loadAgents();
        refreshStats();
    } else {
        showToast('创建失败', 'error');
    }
}

// 当前正在绑定的 Agent ID
let currentBindingAgentId = null;

function showBindClientModal(agentId, currentClientId) {
    currentBindingAgentId = agentId;
    document.getElementById('unbindClient').checked = false;

    // 设置初始占位文本
    const clientSelect = document.getElementById('bindAgentClient');
    clientSelect.innerHTML = '<option value="">正在加载...</option>';

    // 先显示模态框
    document.getElementById('bindClientModal').classList.add('active');

    // 异步加载客户端列表使用共享函数
    setTimeout(() => {
        (async () => {
            const data = await apiGet('/api/clients');
            if (data && data.data && data.data.length > 0) {
                let options = ['<option value="">未绑定</option>'];
                options = options.concat(data.data.map(client =>
                    `<option value="${client.id}" ${client.id === currentClientId ? 'selected' : ''}>${client.name} (${client.id}) ${client.is_online ? '[在线]' : '[离线]'}</option>`
                ));
                clientSelect.innerHTML = options.join('');
            } else {
                clientSelect.innerHTML = '<option value="">无可用客户端</option>';
            }
        })();
    }, 10);
}

// 监听解绑复选框
document.addEventListener('change', (e) => {
    if (e.target.id === 'unbindClient') {
        const clientSelect = document.getElementById('bindAgentClient');
        if (e.target.checked) {
            clientSelect.disabled = true;
            clientSelect.value = '';
        } else {
            clientSelect.disabled = false;
        }
    }
});

async function bindAgentToClient() {
    if (!currentBindingAgentId) {
        showToast('未知的 Agent', 'error');
        return;
    }

    let clientId = document.getElementById('unbindClient').checked
        ? null
        : document.getElementById('bindAgentClient').value || null;

    const result = await apiPost(`/api/agents/${currentBindingAgentId}/bind-client`, {
        client_id: clientId
    });

    if (result && result.data && result.data.success) {
        showToast('绑定成功', 'success');
        closeModal('bindClientModal');
        loadAgents();
        refreshStats();
    } else {
        showToast('绑定失败: ' + (result?.message || '未知错误'), 'error');
    }
}

// 当前编辑的 Agent ID
let currentEditingAgentId = null;

function showEditAgentModal(agentId, name, description, defaultModel, maxTurns, clientId) {
    currentEditingAgentId = agentId;
    document.getElementById('editAgentName').value = name;
    document.getElementById('editAgentDescription').value = description || '';
    document.getElementById('editAgentModel').value = defaultModel;
    document.getElementById('editAgentMaxTurns').value = maxTurns;

    // 设置初始占位文本
    const clientSelect = document.getElementById('editAgentClient');
    clientSelect.innerHTML = '<option value="">正在加载...</option>';

    // 先显示模态框
    document.getElementById('editAgentModal').classList.add('active');

    // 异步加载客户端列表使用共享函数，需要预选择当前 clientId
    setTimeout(async () => {
        const data = await apiGet('/api/clients');
        if (data && data.data && data.data.length > 0) {
            let options = ['<option value="">未选择（使用默认客户端）</option>'];
            options = options.concat(data.data.map(c =>
                `<option value="${c.id}" ${c.id === clientId ? 'selected' : ''}>${c.name} (${c.id}) ${c.is_online ? '[在线]' : '[离线]'}</option>`
            ));
            clientSelect.innerHTML = options.join('');
        } else {
            clientSelect.innerHTML = '<option value="">无可用客户端</option>';
        }
    }, 10);
}

async function updateAgent() {
    if (!currentEditingAgentId) {
        showToast('未知的 Agent', 'error');
        return;
    }

    const name = document.getElementById('editAgentName').value.trim();
    if (!name) {
        showToast('请输入 Agent 名称', 'error');
        return;
    }

    const data = {
        name: name,
        description: document.getElementById('editAgentDescription').value.trim(),
        default_model: document.getElementById('editAgentModel').value,
        max_turns: parseInt(document.getElementById('editAgentMaxTurns').value),
        client_id: document.getElementById('editAgentClient').value || null
    };

    const result = await apiPut(`/api/agents/${currentEditingAgentId}`, data);

    if (result && result.data) {
        showToast('更新成功', 'success');
        closeModal('editAgentModal');
        loadAgents();
        refreshStats();
    } else {
        showToast('更新失败', 'error');
    }
}

// Agent 详情
async function showAgentDetail(agentId) {
    const container = document.getElementById('agentDetailContent');
    container.innerHTML = '<div class="empty-state">加载中...</div>';
    document.getElementById('agentDetailModal').classList.add('active');

    const data = await apiGet(`/api/agents/${agentId}`);
    if (!data || !data.data) {
        container.innerHTML = '<div class="empty-state">加载失败</div>';
        return;
    }

    const agent = data.data;

    let tasksHtml = '';
    if (agent.tasks && agent.tasks.length > 0) {
        tasksHtml = `
            <div style="margin-top: 20px;">
                <h4>关联任务列表 (${agent.task_count}, 最近 ${agent.tasks.length} 条)</h4>
                <div class="task-list-sm">
                    ${agent.tasks.map(task => `
                        <div class="task-item-sm" onclick="closeModal('agentDetailModal'); showTaskDetail('${task.id}')" style="cursor: pointer;">
                            <div class="task-item-sm-header">
                                <span class="task-name-sm">${escapeHtml(task.prompt)}</span>
                                <span class="task-status ${task.status}">${getStatusText(task.status)}</span>
                            </div>
                            <div class="task-meta-sm">
                                ID: ${task.id.substring(0, 8)}... | 创建: ${new Date(task.created_at).toLocaleString()}
                                ${task.duration_ms ? ` | 耗时: ${task.duration_ms}ms` : ''}
                            </div>
                        </div>
                    `).join('')}
                </div>
            </div>
        `;
    } else {
        tasksHtml = '<div style="margin-top: 20px;"><p class="empty-state">暂无关联任务</p></div>';
    }

    container.innerHTML = `
        <div class="detail-info">
            <div class="detail-row">
                <div class="detail-label">Agent ID</div>
                <div class="detail-value">${agent.id}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">名称</div>
                <div class="detail-value">${escapeHtml(agent.name)}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">描述</div>
                <div class="detail-value">${agent.description || '无描述'}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">绑定客户端</div>
                <div class="detail-value">${agent.client_name ? `${agent.client_name} (${agent.client_id})` : '未绑定'}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">默认模型</div>
                <div class="detail-value">${agent.default_model}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">最大迭代次数</div>
                <div class="detail-value">${agent.max_turns}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">状态</div>
                <div class="detail-value">${agent.is_active ? '启用' : '禁用'}</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">超时时间</div>
                <div class="detail-value">${agent.timeout} 秒</div>
            </div>
            <div class="detail-row">
                <div class="detail-label">创建时间</div>
                <div class="detail-value">${new Date(agent.created_at).toLocaleString()}</div>
            </div>
            ${agent.updated_at ? `
            <div class="detail-row">
                <div class="detail-label">更新时间</div>
                <div class="detail-value">${new Date(agent.updated_at).toLocaleString()}</div>
            </div>
            ` : ''}
            <div class="detail-row">
                <div class="detail-label">任务总数</div>
                <div class="detail-value">${agent.task_count}</div>
            </div>
        </div>
        ${tasksHtml}
    `;
}

// Tasks
async function loadTasks() {
    const container = document.getElementById('taskList');
    const data = await apiGet('/api/tasks?limit=50');
    
    if (!data || !data.data || data.data.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无任务</div>';
        return;
    }
    
    container.innerHTML = data.data.map(task => {
        const convTag = task.conversation_id
            ? `<span class="conv-status-tag active" title="点击打开所属对话" onclick="event.stopPropagation(); openConversation('${task.conversation_id}')" style="cursor:pointer;"><i class="fa fa-comments"></i> 对话 #${task.turn_index || 1}</span>`
            : `<span class="conv-status-tag archived" title="历史单轮任务"><i class="fa fa-tag"></i> 单轮任务</span>`;
        return `
        <div class="task-item">
            <div class="task-header">
                <span class="task-name" onclick="showTaskDetail('${task.id}')" style="cursor: pointer;">
                    ${task.prompt.substring(0, 50)}${task.prompt.length > 50 ? '...' : ''}
                </span>
                <span class="task-status ${task.status}">${getStatusText(task.status)}</span>
            </div>
            <div class="task-meta">
                ${convTag}
                ID: ${task.id.substring(0, 12)}... | Agent: ${task.agent_id}
                <br>创建时间: ${new Date(task.created_at).toLocaleString()}
                ${task.duration_ms ? ` | 耗时: ${task.duration_ms}ms` : ''}
            </div>
            <div class="task-actions">
                <button class="btn btn-secondary btn-sm" onclick="showTaskDetail('${task.id}')">
                    <i class="fa fa-eye"></i> 查看详情
                </button>
                ${task.conversation_id ? `
                <button class="btn btn-secondary btn-sm" onclick="openConversation('${task.conversation_id}')">
                    <i class="fa fa-comments"></i> 打开对话
                </button>` : ''}
                <button class="btn btn-danger btn-sm" onclick="showConfirmDelete('task', '${task.id}', '任务 ${task.id.substring(0, 8)}')">
                    <i class="fa fa-trash"></i> 删除
                </button>
            </div>
        </div>
        `;
    }).join('');
}

async function loadRecentTasks() {
    const container = document.getElementById('recentTasks');
    const data = await apiGet('/api/tasks?limit=5');
    
    if (!data || !data.data || data.data.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无任务</div>';
        return;
    }
    
    container.innerHTML = data.data.map(task => `
        <div class="task-item" style="cursor: pointer;" onclick="showTaskDetail('${task.id}')">
            <div class="task-header">
                <span class="task-name">
                    ${task.prompt.substring(0, 40)}${task.prompt.length > 40 ? '...' : ''}
                </span>
                <span class="task-status ${task.status}">${getStatusText(task.status)}</span>
            </div>
            <div class="task-meta">
                ${new Date(task.created_at).toLocaleString()}
            </div>
        </div>
    `).join('');
}

function getStatusText(status) {
    const statusMap = {
        'pending': '等待中',
        'queued': '已排队',
        'running': '执行中',
        'completed': '已完成',
        'failed': '失败',
        'cancelled': '已取消'
    };
    return statusMap[status] || status;
}

async function showTaskDetail(taskId, opts = {}) {
    const keepStream = !!opts.keepStream;
    const container = document.getElementById('taskDetailContent');
    container.innerHTML = '<div class="empty-state">加载中...</div>';
    document.getElementById('taskDetailModal').classList.add('active');

    const data = await apiGet(`/api/tasks/${taskId}`);

    if (!data || !data.data) {
        container.innerHTML = '<div class="empty-state">加载失败</div>';
        return;
    }

    const task = data.data;

    currentTaskProgress.taskId = taskId;
    currentTaskProgress.max_turns = currentTaskProgress.max_turns || 10;

    // 实时进度状态条（轻量，仅显示 turn/状态）
    const isRunning = (task.status === 'running' || task.status === 'queued' || task.status === 'pending');
    let progressBadgeHtml = '';
    if (isRunning) {
        const displayTurn = Math.min(currentTaskProgress.turn, currentTaskProgress.max_turns);
        const percent = currentTaskProgress.max_turns > 0 ? (displayTurn / currentTaskProgress.max_turns) * 100 : 0;
        progressBadgeHtml = `
            <div id="realtimeOutput" class="realtime-output">
                <div class="realtime-header">
                    <span class="realtime-status">${describeRuntimeStatus(currentTaskProgress.status)}</span>
                    <span class="realtime-turns">${displayTurn} / ${currentTaskProgress.max_turns}</span>
                </div>
                <div class="realtime-progress-bar">
                    <div class="realtime-fill" style="width: ${percent}%"></div>
                </div>
            </div>
        `;
    }

    // 初始化或保留事件流
    if (!keepStream || !taskEventStream.isActive(taskId)) {
        taskEventStream.open(taskId);
    } else {
        taskEventStream.show();
        taskEventStream.fetchSince(taskEventStream.lastSeq);
    }

    let logsHtml = '';
    if (task.logs && task.logs.length > 0) {
        logsHtml = `
            <div class="task-logs">
                <h4>任务日志</h4>
                ${task.logs.map(log => `
                    <div class="log-item ${log.type}">
                        <div class="log-time">${new Date(log.created_at).toLocaleString()}</div>
                        <div>${log.message}</div>
                    </div>
                `).join('')}
            </div>
        `;
    }

    container.innerHTML = `
        ${progressBadgeHtml}

        <div class="task-detail-info">
            <div class="task-detail-row">
                <div class="task-detail-label">任务 ID</div>
                <div class="task-detail-value">${task.id}</div>
            </div>
            <div class="task-detail-row">
                <div class="task-detail-label">Agent ID</div>
                <div class="task-detail-value">${task.agent_id}</div>
            </div>
            <div class="task-detail-row">
                <div class="task-detail-label">状态</div>
                <div class="task-detail-value">
                    <span class="task-status ${task.status}">${getStatusText(task.status)}</span>
                </div>
            </div>
            <div class="task-detail-row">
                <div class="task-detail-label">创建时间</div>
                <div class="task-detail-value">${new Date(task.created_at).toLocaleString()}</div>
            </div>
            ${task.completed_at ? `
            <div class="task-detail-row">
                <div class="task-detail-label">完成时间</div>
                <div class="task-detail-value">${new Date(task.completed_at).toLocaleString()}</div>
            </div>
            ` : ''}
            ${task.duration_ms ? `
            <div class="task-detail-row">
                <div class="task-detail-label">耗时</div>
                <div class="task-detail-value">${task.duration_ms}ms</div>
            </div>
            ` : ''}
            ${task.num_turns ? `
            <div class="task-detail-row">
                <div class="task-detail-label">迭代次数</div>
                <div class="task-detail-value">${task.num_turns}</div>
            </div>
            ` : ''}
            <div class="task-detail-row">
                <div class="task-detail-label">提示词</div>
                <div class="task-detail-value">${task.prompt}</div>
            </div>
            ${task.context ? `
            <div class="task-detail-row">
                <div class="task-detail-label">上下文</div>
                <div class="task-detail-value">${task.context}</div>
            </div>
            ` : ''}
        </div>

        ${task.result ? `
            <h4>执行结果</h4>
            <div class="task-result">${escapeHtml(task.result)}</div>
        ` : ''}

        ${task.error_message ? `
            <h4>错误信息</h4>
            <div class="task-result" style="background: #f8d7da; color: #721c24;">${escapeHtml(task.error_message)}</div>
        ` : ''}

        ${logsHtml}
    `;

    // 添加删除按钮到 footer
    const footer = document.getElementById('taskDetailFooter');
    footer.innerHTML = `
        <button class="btn btn-danger btn-sm" onclick="closeModal('taskDetailModal'); showConfirmDelete('task', '${taskId}', '任务 ${taskId.substring(0, 8)}')">
            <i class="fa fa-trash"></i> 删除任务
        </button>
        <button class="btn btn-secondary" onclick="closeModal('taskDetailModal')">关闭</button>
    `;
}

// 兼容入口：旧的"新建任务"操作已统一收口到"新建对话"
function showCreateTaskModal() {
    showCreateConversationModal();
}

function createTask() {
    showCreateConversationModal();
}

// Modal helpers
function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('active');
    if (modalId === 'taskDetailModal') {
        taskEventStream.close();
        hideProgressContainer();
    }
    if (modalId === 'conversationDetailModal') {
        taskEventStream.deactivateConversation();
        currentConversation = null;
    }
}

// Close modal on outside click
document.addEventListener('click', (e) => {
    if (e.target.classList.contains('modal')) {
        e.target.classList.remove('active');
    }
});

// Toast notifications
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    
    setTimeout(() => {
        toast.remove();
    }, 3000);
}

// Escape HTML
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// ============ 实时任务进度（高层状态） ============
function updateTaskProgressRealtime(taskId, progress) {
    currentTaskProgress.taskId = taskId;
    currentTaskProgress.turn = progress.turn || 0;
    currentTaskProgress.max_turns = progress.max_turns || currentTaskProgress.max_turns || 10;
    currentTaskProgress.status = progress.status || 'thinking';
    updateRunningTaskDisplay();
}

function updateRunningTaskDisplay() {
    const outputArea = document.getElementById('realtimeOutput');
    if (!outputArea) return;
    const displayTurn = Math.min(currentTaskProgress.turn, currentTaskProgress.max_turns);
    const percent = currentTaskProgress.max_turns > 0 ? (displayTurn / currentTaskProgress.max_turns) * 100 : 0;
    const statusEl = outputArea.querySelector('.realtime-status');
    const turnsEl = outputArea.querySelector('.realtime-turns');
    const fillEl = outputArea.querySelector('.realtime-fill');
    if (statusEl) statusEl.textContent = describeRuntimeStatus(currentTaskProgress.status);
    if (turnsEl) turnsEl.textContent = displayTurn + ' / ' + currentTaskProgress.max_turns;
    if (fillEl) fillEl.style.width = percent + '%';
}

function describeRuntimeStatus(status) {
    const map = {
        'idle': '空闲',
        'thinking': '思考中',
        'tool_use': '使用工具',
        'waiting_confirmation': '等待确认',
        'working': '执行中',
        'completed': '已完成',
        'failed': '已失败'
    };
    return map[status] || status || '运行中';
}

function hideProgressContainer() {
    currentTaskProgress.taskId = null;
}

// ============ 用户确认 ============
let currentConfirmation = null;

function showUserConfirmation(clientId, request) {
    currentConfirmation = {
        clientId: clientId,
        requestId: request.request_id,
        taskId: request.task_id
    };

    document.getElementById('confirmationTitle').textContent = request.title || '需要确认';
    document.getElementById('confirmationMessage').textContent = request.message || '';
    document.getElementById('confirmationPrompt').textContent = request.prompt || '';
    document.getElementById('requestIdInfo').textContent = `请求 ID: ${request.request_id}`;

    const toolWrap = document.getElementById('confirmationTool');
    const toolNameEl = document.getElementById('confirmationToolName');
    const toolInputEl = document.getElementById('confirmationToolInput');
    if (request.source === 'permission_mcp' || request.tool_name) {
        toolWrap.style.display = '';
        toolNameEl.textContent = request.tool_name || '(未知工具)';
        let inputText = '';
        try {
            inputText = JSON.stringify(request.tool_input || {}, null, 2);
        } catch (e) {
            inputText = String(request.tool_input || '');
        }
        toolInputEl.textContent = inputText;
    } else {
        toolWrap.style.display = 'none';
        toolNameEl.textContent = '';
        toolInputEl.textContent = '';
    }

    // 生成选项按钮
    const optionsContainer = document.getElementById('confirmationOptions');
    optionsContainer.innerHTML = '';
    if (request.options && request.options.length > 0) {
        request.options.forEach(option => {
            const btn = document.createElement('button');
            btn.className = 'btn btn-confirmation';
            btn.textContent = option.label;
            btn.onclick = () => submitUserConfirmation(option.value);
            optionsContainer.appendChild(btn);
        });
    } else {
        // 默认选项
        const btnYes = document.createElement('button');
        btnYes.className = 'btn btn-primary btn-confirmation';
        btnYes.textContent = '确认';
        btnYes.onclick = () => submitUserConfirmation('yes');
        optionsContainer.appendChild(btnYes);

        const btnNo = document.createElement('button');
        btnNo.className = 'btn btn-secondary btn-confirmation';
        btnNo.textContent = '取消';
        btnNo.onclick = () => submitUserConfirmation('no');
        optionsContainer.appendChild(btnNo);
    }

    document.getElementById('userConfirmationModal').classList.add('active');
    showToast('收到用户确认请求', 'info');
}

async function submitUserConfirmation(value) {
    if (!currentConfirmation) {
        showToast('没有待处理的确认请求', 'error');
        return;
    }

    const response = await apiPost('/api/user-confirmation/respond', {
        client_id: currentConfirmation.clientId,
        request_id: currentConfirmation.requestId,
        task_id: currentConfirmation.taskId,
        value: value
    });

    if (response && response.data && response.data.success) {
        showToast('确认已提交', 'success');
        closeUserConfirmation();
    } else {
        showToast('提交失败，请检查客户端是否在线', 'error');
    }
}

function closeUserConfirmation() {
    document.getElementById('userConfirmationModal').classList.remove('active');
    currentConfirmation = null;
}

// ============ 对话（多轮上下文延续） ============
let currentConversation = null;

const CONV_STATUS_LABEL = {
    active: '活跃',
    archived: '已归档',
    lost_session: '会话已丢失',
};

function describeConversationStatus(status) {
    return CONV_STATUS_LABEL[status] || status || '未知';
}

async function loadConversations() {
    const container = document.getElementById('conversationList');
    if (!container) return;
    const data = await apiGet('/api/conversations?limit=100');
    if (!data || !Array.isArray(data.data) || data.data.length === 0) {
        container.innerHTML = '<div class="empty-state">暂无对话，点击右上角"新建对话"开始</div>';
        return;
    }
    container.innerHTML = data.data.map(conv => {
        const lastTime = conv.last_prompt_at ? new Date(conv.last_prompt_at).toLocaleString()
            : new Date(conv.created_at).toLocaleString();
        return `
            <div class="conversation-item" onclick="openConversation('${conv.id}')">
                <div class="conv-title">${escapeHtml(conv.title || '(未命名对话)')}</div>
                <div class="conv-meta">
                    <span><i class="fa fa-user-circle"></i> ${escapeHtml(conv.agent_id)}</span>
                    <span><i class="fa fa-desktop"></i> ${escapeHtml(conv.client_id)}</span>
                    <span><i class="fa fa-folder-o"></i> ${escapeHtml(conv.workdir || '.')}</span>
                    <span><i class="fa fa-comments"></i> ${conv.turn_count || 0} 轮</span>
                    <span><i class="fa fa-clock-o"></i> ${lastTime}</span>
                    <span class="conv-status-tag ${conv.status}">${describeConversationStatus(conv.status)}</span>
                </div>
                <div class="conv-actions">
                    <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation(); openConversation('${conv.id}')">
                        <i class="fa fa-eye"></i> 打开
                    </button>
                    ${conv.status === 'active' ? `
                        <button class="btn btn-warning btn-sm" onclick="event.stopPropagation(); archiveConversation('${conv.id}')">
                            <i class="fa fa-archive"></i> 归档
                        </button>
                    ` : ''}
                    <button class="btn btn-danger btn-sm" onclick="event.stopPropagation(); deleteConversation('${conv.id}')">
                        <i class="fa fa-trash"></i> 删除
                    </button>
                </div>
            </div>
        `;
    }).join('');
}

async function showCreateConversationModal() {
    document.getElementById('conversationPrompt').value = '';
    document.getElementById('conversationContext').value = '';
    document.getElementById('conversationWorkdir').value = '';
    const modal = document.getElementById('createConversationModal');
    modal.classList.add('active');

    const select = document.getElementById('conversationAgent');
    select.innerHTML = '<option value="">加载中...</option>';
    const data = await apiGet('/api/agents');
    if (data && data.data && data.data.length > 0) {
        select.innerHTML = data.data.map(agent =>
            `<option value="${agent.id}">${escapeHtml(agent.name)} (${escapeHtml(agent.id)})</option>`
        ).join('');
    } else {
        select.innerHTML = '<option value="">无可用 Agent</option>';
    }
}

async function createConversation() {
    const agentId = document.getElementById('conversationAgent').value;
    const prompt = document.getElementById('conversationPrompt').value.trim();
    if (!agentId) {
        showToast('请选择 Agent', 'error');
        return;
    }
    if (!prompt) {
        showToast('请输入首条提示词', 'error');
        return;
    }
    const context = document.getElementById('conversationContext').value.trim();
    const workdir = document.getElementById('conversationWorkdir').value.trim();
    const body = { agent_id: agentId, prompt };
    if (context) body.context = context;
    if (workdir) body.workdir = workdir;

    const result = await apiPost('/api/conversations', body);
    if (result && result.data && result.data.conversation_id) {
        showToast('对话已创建，等待 Claude 响应', 'success');
        closeModal('createConversationModal');
        loadConversations();
        openConversation(result.data.conversation_id);
    } else {
        const detail = (result && (result.detail || result.message)) || '创建失败';
        showToast('对话创建失败: ' + detail, 'error');
    }
}

async function openConversation(conversationId) {
    const modal = document.getElementById('conversationDetailModal');
    modal.classList.add('active');
    document.getElementById('conversationDetailBody').innerHTML = '<div class="empty-state">加载中...</div>';
    document.getElementById('conversationMetaBar').innerHTML = '';
    setConversationInputState({ disabled: true, tip: '加载中...' });

    taskEventStream.activateConversation(conversationId);

    await refreshConversationDetail(conversationId, { hydrateHistory: true });
}

async function refreshConversationDetail(conversationId, opts = {}) {
    const data = await apiGet(`/api/conversations/${conversationId}`);
    if (!data || !data.data) {
        document.getElementById('conversationDetailBody').innerHTML =
            '<div class="empty-state">加载失败</div>';
        setConversationInputState({ disabled: true, tip: '加载失败' });
        return;
    }
    const conv = data.data;
    currentConversation = conv;
    document.getElementById('conversationDetailTitle').textContent =
        conv.title || '对话详情';

    renderConversationMetaBar(conv);
    renderConversationTurns(conv, !!opts.hydrateHistory);
    updateConversationInputState(conv);
}

function renderConversationMetaBar(conv) {
    const bar = document.getElementById('conversationMetaBar');
    const onlineClass = conv.client_online ? 'online' : 'offline';
    const onlineText = conv.client_online ? '在线' : '离线';
    const statusClass = conv.status === 'archived' ? 'archived'
        : conv.status === 'lost_session' ? 'lost' : 'online';
    bar.innerHTML = `
        <span class="meta-pill"><i class="fa fa-user-circle"></i> Agent ${escapeHtml(conv.agent_id)}</span>
        <span class="meta-pill ${onlineClass}"><i class="fa fa-desktop"></i> ${escapeHtml(conv.client_id)} · ${onlineText}</span>
        <span class="meta-pill"><i class="fa fa-folder-o"></i> ${escapeHtml(conv.workdir || '.')}</span>
        <span class="meta-pill ${statusClass}"><i class="fa fa-info-circle"></i> ${escapeHtml(describeConversationStatus(conv.status))}</span>
        <span class="meta-pill"><i class="fa fa-comments"></i> ${conv.turn_count || 0} 轮</span>
        ${conv.claude_session_id ? `<span class="meta-pill"><i class="fa fa-key"></i> session ${escapeHtml(String(conv.claude_session_id).slice(0, 8))}...</span>` : ''}
    `;
}

function renderConversationTurns(conv, hydrateHistory) {
    const body = document.getElementById('conversationDetailBody');
    if (!body) return;
    const tasks = Array.isArray(conv.tasks) ? conv.tasks : [];
    if (tasks.length === 0) {
        body.innerHTML = '<div class="empty-state">尚无轮次</div>';
        return;
    }

    // 第一次进入时按 task 列表初始化容器并拉历史；后续刷新尽量保留已有 turn 容器以
    // 避免擦除流式渲染。这里采用 diff 增量策略：
    const existing = new Map();
    body.querySelectorAll('.chat-turn').forEach(el => {
        existing.set(el.dataset.taskId, el);
    });

    const seen = new Set();
    for (const task of tasks) {
        seen.add(task.id);
        let card = existing.get(task.id);
        if (!card) {
            card = createTurnCard(task);
            body.appendChild(card);
            const stream = card.querySelector('.chat-assistant-stream');
            taskEventStream.registerTurn(task.id, stream);
            if (hydrateHistory || task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled') {
                taskEventStream.hydrateTurnHistory(task.id);
            }
        } else {
            updateTurnCardStatus(card, task);
            // 已存在的 turn：若是结束态且尚未拉过历史，补一次
            const stream = card.querySelector('.chat-assistant-stream');
            if (stream && taskEventStream.turns[task.id] && taskEventStream.turns[task.id].lastSeq === 0
                && (task.status === 'completed' || task.status === 'failed' || task.status === 'cancelled')) {
                taskEventStream.hydrateTurnHistory(task.id);
            }
        }
    }

    // 移除被删除的 turn（极少发生，但兜底）
    existing.forEach((el, taskId) => {
        if (!seen.has(taskId)) el.remove();
    });

    body.scrollTop = body.scrollHeight;
}

function createTurnCard(task) {
    const card = document.createElement('div');
    card.className = 'chat-turn';
    card.dataset.taskId = task.id;
    card.dataset.turnIndex = task.turn_index || 1;
    card.innerHTML = `
        <div class="chat-turn-header">
            <span class="turn-label">轮次 #${task.turn_index || 1}</span>
            <span class="turn-status ${task.status || 'pending'}">${getStatusText(task.status || 'pending')}</span>
        </div>
        <div class="chat-user">
            <div class="chat-user-label">你</div>
            <div class="chat-user-text">${escapeHtml(task.prompt || '')}</div>
        </div>
        <div class="chat-assistant-stream"></div>
    `;
    return card;
}

function updateTurnCardStatus(card, task) {
    const statusEl = card.querySelector('.turn-status');
    if (statusEl) {
        statusEl.className = `turn-status ${task.status || 'pending'}`;
        statusEl.textContent = getStatusText(task.status || 'pending');
    }
}

function markTurnStatus(taskId, status) {
    const body = document.getElementById('conversationDetailBody');
    if (!body) return;
    const card = body.querySelector(`.chat-turn[data-task-id="${cssEscape(taskId)}"]`);
    if (!card) return;
    const statusEl = card.querySelector('.turn-status');
    if (statusEl) {
        statusEl.className = `turn-status ${status}`;
        statusEl.textContent = getStatusText(status);
    }
}

function updateConversationInputState(conv) {
    const hasPending = (conv.tasks || []).some(t =>
        ['pending', 'queued', 'running', 'cancelling'].includes(t.status));
    if (conv.status === 'archived') {
        setConversationInputState({ disabled: true, tip: '对话已归档，不可继续' });
    } else if (conv.status === 'lost_session') {
        setConversationInputState({ disabled: true, tip: '会话已丢失，请新建对话' });
    } else if (!conv.client_online) {
        setConversationInputState({ disabled: true, tip: '绑定的客户端离线，待客户端上线后可继续' });
    } else if (hasPending) {
        setConversationInputState({ disabled: true, tip: '上一轮还在执行...' });
    } else if (!conv.claude_session_id) {
        setConversationInputState({ disabled: true, tip: '首轮尚未产生 session，请稍候' });
    } else {
        setConversationInputState({ disabled: false, tip: '回车换行；点击"发送"提交' });
    }
}

function setConversationInputState({ disabled, tip }) {
    const ta = document.getElementById('conversationFollowupPrompt');
    const btn = document.getElementById('conversationSendBtn');
    const tipEl = document.getElementById('conversationInputTip');
    if (ta) ta.disabled = !!disabled;
    if (btn) btn.disabled = !!disabled;
    if (tipEl) {
        tipEl.textContent = tip || '';
        tipEl.classList.toggle('ok', !disabled);
    }
}

async function sendConversationFollowup() {
    if (!currentConversation) return;
    const ta = document.getElementById('conversationFollowupPrompt');
    const prompt = (ta.value || '').trim();
    if (!prompt) {
        showToast('请输入要追问的内容', 'error');
        return;
    }
    setConversationInputState({ disabled: true, tip: '发送中...' });
    const result = await apiPost(`/api/conversations/${currentConversation.id}/messages`, { prompt });
    if (result && result.data && result.data.task_id) {
        ta.value = '';
        // 立即在视图末尾追加一张 turn 卡，等待 WS 事件流灌入
        const body = document.getElementById('conversationDetailBody');
        const placeholder = {
            id: result.data.task_id,
            turn_index: result.data.turn_index,
            status: 'queued',
            prompt,
        };
        const card = createTurnCard(placeholder);
        body.appendChild(card);
        const stream = card.querySelector('.chat-assistant-stream');
        taskEventStream.registerTurn(result.data.task_id, stream);
        body.scrollTop = body.scrollHeight;
        // 服务端会很快推 task_started/task_event；同时刷一次详情兜底
        refreshConversationDetail(currentConversation.id);
    } else {
        const detail = (result && (result.detail || result.message)) || '发送失败';
        showToast('追问失败: ' + detail, 'error');
        // 失败后重新评估输入状态
        if (currentConversation) refreshConversationDetail(currentConversation.id);
    }
}

async function archiveConversation(conversationId) {
    if (!confirm('归档后该对话将不可继续，是否确认？')) return;
    const result = await apiPost(`/api/conversations/${conversationId}/archive`, {});
    if (result && result.data) {
        showToast('已归档', 'success');
        loadConversations();
        if (currentConversation && currentConversation.id === conversationId) {
            refreshConversationDetail(conversationId);
        }
    } else {
        showToast('归档失败', 'error');
    }
}

async function archiveCurrentConversation() {
    if (!currentConversation) return;
    await archiveConversation(currentConversation.id);
}

async function deleteConversation(conversationId) {
    if (!confirm('确认删除该对话？所有轮次与事件都会被清除。')) return;
    const result = await apiDelete(`/api/conversations/${conversationId}`);
    if (result && result.data && result.data.success) {
        showToast('已删除', 'success');
        loadConversations();
        if (currentConversation && currentConversation.id === conversationId) {
            closeModal('conversationDetailModal');
        }
    } else {
        const detail = (result && (result.detail || result.message)) || '删除失败';
        showToast('删除失败: ' + detail, 'error');
    }
}

async function deleteCurrentConversation() {
    if (!currentConversation) return;
    await deleteConversation(currentConversation.id);
}
