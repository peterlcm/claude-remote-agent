// Global state
let ws = null;
let currentStats = {};
let currentTaskProgress = {
    taskId: null,
    turn: 0,
    max_turns: 10,
    status: 'idle',
    output: ''  // 缓存完整输出内容
};

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
            break;
        case 'task_started':
            refreshStats();
            break;
        case 'task_progress':
            // 只更新实时进度，不需要全量刷新 API
            updateTaskProgressRealtime(message.task_id, message.progress);
            break;
        case 'task_completed':
        case 'task_failed':
        case 'task_cancelled':
            refreshStats();
            // 如果详情 modal 正在显示这个任务，重新加载
            if (currentTaskProgress.taskId === message.task_id) {
                hideProgressContainer();
                showTaskDetail(message.task_id);
            }
            loadTasks();
            loadRecentTasks();
            break;
        case 'user_confirmation_request':
            // 显示用户确认对话框
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
                    <button class="btn btn-secondary btn-sm" onclick="showEditAgentModal('${agent.id}', '${escapeHtml(agent.name)}', '${escapeHtml(agent.description || '')}', '${agent.default_model}', ${agent.max_turns}, '${agent.effort}', '${agent.client_id || ''}')">
                        <i class="fa fa-edit"></i> 编辑
                    </button>
                    <button class="btn btn-danger btn-sm" onclick="showConfirmDelete('agent', '${agent.id}', '${escapeHtml(agent.name)}')">
                        <i class="fa fa-trash"></i> 删除
                    </button>
                </div>
            </div>
            <div class="agent-meta">
                ${agent.description || '无描述'}
                <br>模型: ${agent.default_model} | 最大迭代: ${agent.max_turns} | 强度: ${agent.effort}
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
    document.getElementById('agentEffort').value = 'medium';

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
        effort: document.getElementById('agentEffort').value,
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

function showEditAgentModal(agentId, name, description, defaultModel, maxTurns, effort, clientId) {
    currentEditingAgentId = agentId;
    document.getElementById('editAgentName').value = name;
    document.getElementById('editAgentDescription').value = description || '';
    document.getElementById('editAgentModel').value = defaultModel;
    document.getElementById('editAgentMaxTurns').value = maxTurns;
    document.getElementById('editAgentEffort').value = effort || 'medium';

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
        effort: document.getElementById('editAgentEffort').value,
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
                <div class="detail-label">推理强度</div>
                <div class="detail-value">${agent.effort || '未设置'}</div>
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
    
    container.innerHTML = data.data.map(task => `
        <div class="task-item">
            <div class="task-header">
                <span class="task-name" onclick="showTaskDetail('${task.id}')" style="cursor: pointer;">
                    ${task.prompt.substring(0, 50)}${task.prompt.length > 50 ? '...' : ''}
                </span>
                <span class="task-status ${task.status}">${getStatusText(task.status)}</span>
            </div>
            <div class="task-meta">
                ID: ${task.id.substring(0, 12)}... | Agent: ${task.agent_id}
                <br>创建时间: ${new Date(task.created_at).toLocaleString()}
                ${task.duration_ms ? ` | 耗时: ${task.duration_ms}ms` : ''}
            </div>
            <div class="task-actions">
                <button class="btn btn-secondary btn-sm" onclick="showTaskDetail('${task.id}')">
                    <i class="fa fa-eye"></i> 查看详情
                </button>
                <button class="btn btn-danger btn-sm" onclick="showConfirmDelete('task', '${task.id}', '任务 ${task.id.substring(0, 8)}')">
                    <i class="fa fa-trash"></i> 删除
                </button>
            </div>
        </div>
    `).join('');
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

async function showTaskDetail(taskId) {
    const container = document.getElementById('taskDetailContent');
    container.innerHTML = '<div class="empty-state">加载中...</div>';
    document.getElementById('taskDetailModal').classList.add('active');

    // 隐藏旧的进度容器（如果存在）
    const oldProgressContainer = document.getElementById('taskProgressContainer');
    if (oldProgressContainer) {
        oldProgressContainer.style.display = 'none';
    }

    const data = await apiGet(`/api/tasks/${taskId}`);

    if (!data || !data.data) {
        container.innerHTML = '<div class="empty-state">加载失败</div>';
        return;
    }

    const task = data.data;

    // 判断是否正在运行
    const isRunning = (task.status === 'running' || task.status === 'queued' || task.status === 'pending');

    // 实时输出区域（只在任务运行中显示）
    let realtimeHtml = '';
    if (isRunning) {
        currentTaskProgress.taskId = taskId;
        const displayTurn = Math.min(currentTaskProgress.turn, currentTaskProgress.max_turns);
        const percent = currentTaskProgress.max_turns > 0 ? (displayTurn / currentTaskProgress.max_turns) * 100 : 0;

        realtimeHtml = `
            <div id="realtimeOutput" class="realtime-output">
                <div class="realtime-header">
                    <span class="realtime-status">${currentTaskProgress.status === 'working' ? '执行中' : '思考中'}</span>
                    <span class="realtime-turns">${displayTurn} / ${currentTaskProgress.max_turns}</span>
                </div>
                <div class="realtime-progress-bar">
                    <div class="realtime-fill" style="width: ${percent}%"></div>
                </div>
                <pre class="realtime-log">${escapeHtml(currentTaskProgress.output || '等待输出...')}</pre>
            </div>
        `;
    } else {
        // 任务已结束，清除进度
        hideProgressContainer();
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
        ${realtimeHtml}

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

async function showCreateTaskModal() {
    document.getElementById('taskPrompt').value = '';
    document.getElementById('taskContext').value = '';
    document.getElementById('createTaskModal').classList.add('active');
    
    const agentSelect = document.getElementById('taskAgent');
    const data = await apiGet('/api/agents');
    
    if (data && data.data && data.data.length > 0) {
        agentSelect.innerHTML = data.data.map(agent => 
            `<option value="${agent.id}">${agent.name} (${agent.id})</option>`
        ).join('');
    } else {
        agentSelect.innerHTML = '<option value="">无可用 Agent</option>';
    }
}

async function createTask() {
    const agentId = document.getElementById('taskAgent').value;
    const prompt = document.getElementById('taskPrompt').value.trim();
    
    if (!agentId) {
        showToast('请选择 Agent', 'error');
        return;
    }
    if (!prompt) {
        showToast('请输入任务提示词', 'error');
        return;
    }
    
    const context = document.getElementById('taskContext').value.trim();
    const result = await apiPost('/api/tasks', { agent_id: agentId, prompt, context });
    
    if (result && result.data) {
        showToast('任务已发送', 'success');
        closeModal('createTaskModal');
        loadTasks();
        loadRecentTasks();
        refreshStats();
    } else {
        showToast('任务发送失败，请确保客户端已连接', 'error');
    }
}

// Modal helpers
function closeModal(modalId) {
    document.getElementById(modalId).classList.remove('active');
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

// ============ 实时任务进度 ============
function updateTaskProgressRealtime(taskId, progress) {
    // 更新全局状态
    currentTaskProgress.taskId = taskId;
    currentTaskProgress.turn = progress.turn || 0;
    currentTaskProgress.max_turns = progress.max_turns || 10;
    currentTaskProgress.status = progress.status || 'thinking';

    // 缓存输出内容
    if (progress.message !== undefined && progress.message !== null) {
        currentTaskProgress.output = progress.message;
    }

    // 更新 UI：如果任务详情 modal 已打开且是当前任务，直接渲染
    updateRunningTaskDisplay();
}

function updateRunningTaskDisplay() {
    // 检查任务详情 modal 是否打开且是当前运行的任务
    const modal = document.getElementById('taskDetailModal');
    if (!modal || !modal.classList.contains('active')) {
        return;
    }
    if (currentTaskProgress.taskId === null) {
        return;
    }

    // 查找或创建实时输出区域
    let outputArea = document.getElementById('realtimeOutput');
    if (!outputArea) {
        return; // 不在任务详情页面
    }

    // 更新进度条
    const displayTurn = Math.min(currentTaskProgress.turn, currentTaskProgress.max_turns);
    const percent = (displayTurn / currentTaskProgress.max_turns) * 100;

    const statusEl = outputArea.querySelector('.realtime-status');
    const turnsEl = outputArea.querySelector('.realtime-turns');
    const fillEl = outputArea.querySelector('.realtime-fill');
    const logEl = outputArea.querySelector('.realtime-log');

    if (statusEl) {
        const statusText = {
            'idle': '空闲', 'thinking': '思考中', 'tool_use': '使用工具',
            'waiting_confirmation': '等待确认', 'working': '执行中'
        };
        statusEl.textContent = statusText[currentTaskProgress.status] || currentTaskProgress.status;
    }
    if (turnsEl) {
        turnsEl.textContent = displayTurn + ' / ' + currentTaskProgress.max_turns;
    }
    if (fillEl) {
        fillEl.style.width = percent + '%';
    }

    // 更新输出日志
    if (logEl && currentTaskProgress.output) {
        logEl.textContent = currentTaskProgress.output;
        logEl.scrollTop = logEl.scrollHeight;
    }
}

function hideProgressContainer() {
    currentTaskProgress.taskId = null;
    currentTaskProgress.output = '';
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
