/* Keep Manager rendering focused on channel actions and phase summaries. */
let selectedTask = '';
let lastTask = null;
const projectData = {};

function esc(value) {
  /* Escape server values before inserting them into the dashboard. */
  return String(value ?? '').replace(/[&<>"']/g, character => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]));
}

function branchKey(key) {
  /* Return the stable browser storage key for one Agent/project/channel. */
  return 'ab2.last-branch.' + key;
}

function rememberBranch(key, branch) {
  /* Persist the last selected branch without storing build configuration. */
  if (branch) localStorage.setItem(branchKey(key), branch);
}

function setChannelAction(key, running) {
  /* Hide the build action while this channel has an active task. */
  const build = document.querySelector('#build-' + key);
  if (build) build.style.display = running ? 'none' : '';
}

async function loadAgents() {
  /* Render channels inside one collapsible group per machine. */
  const agents = await fetch('/api/agents').then(response => response.json());
  // Remove stale channel references when an Agent or project disappears from the live snapshot.
  Object.keys(projectData).forEach(key => delete projectData[key]);
  // The API contains only live connections, so every rendered machine is currently online.
  document.querySelector('#agents').innerHTML = agents.map(agent => {
    const cards = agent.projects.map(project => (project.channels || []).map((channel, index) => {
      const key = ('p_' + agent.id + '_' + project.id + '_' + (channel.name || channel.switch_to || index)).replace(/[^A-Za-z0-9_-]/g, '_');
      const channelName = channel.name || channel.switch_to;
      const branches = (project.channel_branches || {})[channelName] || [];
      const usesDefaultBranch = channel.branch_filter === 'default';
      const saved = localStorage.getItem(branchKey(key));
      const selected = usesDefaultBranch ? (project.default_branch || branches[0] || '') : (branches.includes(saved) ? saved : (branches[0] || ''));
      projectData[key] = { ...(projectData[key] || {}), agent_id: agent.id, project_id: project.id, channel: channelName, branch_filter: channel.branch_filter || 'all_dev', default_branch: project.default_branch || '' };
      return `<div class="channel-card"><div class="channel-main"><div class="channel-title">${esc(channel.switch_to || channel.name || '未命名渠道')}</div><div class="project-title">${esc(project.name)}</div><div class="channel-actions"><select id="branch-${key}" onchange="rememberBranch('${key}',this.value)" ${usesDefaultBranch ? 'disabled' : ''}>${branches.map(branch => `<option value="${esc(branch)}" ${branch === selected ? 'selected' : ''}>${esc(branch)}</option>`).join('') || '<option value="">暂无符合条件的分支</option>'}</select><button id="build-${key}" onclick="buildProject('${key}')">打资源包</button></div></div><div class="channel-phase" data-channel-phase="${key}">未开始构建</div></div>`;
    })).flat(2).join('');
    return `<details class="machine-group online" open><summary><span class="machine-name">${esc(agent.name)}</span><span class="machine-status">在线</span><span class="machine-meta">${esc(agent.hostname)} · ${esc(agent.platform)}</span></summary><div class="machine-channels">${cards || '暂无已配置渠道'}</div></details>`;
  }).join('') || '暂无已配置渠道';
  restoreLatestTasks();
}

async function restoreLatestTasks() {
  /* Restore the newest task for every channel from Manager persistence. */
  const entries = await Promise.all(Object.entries(projectData).map(async ([key, data]) => {
    const query = new URLSearchParams(data).toString();
    const task = await fetch('/api/tasks/latest?' + query).then(response => response.json());
    return [key, task];
  }));
  let newest = null;
  entries.forEach(([key, task]) => {
    if (!task.id) return;
    projectData[key].task_id = task.id;
    const panel = document.querySelector('[data-channel-phase="' + key + '"]');
    if (panel) { panel.innerHTML = taskHtml(task); panel.dataset.taskId = task.id; }
    setChannelAction(key, ['queued', 'running', 'cancel_requested'].includes(task.status));
    if (!newest || task.created_at > newest.created_at) newest = task;
  });
  if (!selectedTask && newest) { selectedTask = newest.id; }
}

async function buildProject(key) {
  /* Create a task using only the selected branch and Agent-side channel config. */
  const data = projectData[key];
  // Use the project default branch directly when the channel locks branch selection.
  const branch = data.branch_filter === 'default' ? data.default_branch : document.querySelector('#branch-' + key)?.value;
  if (!data || !branch) return;
  const response = await fetch('/api/tasks', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ...data, branch }) });
  const result = await response.json();
  if (!response.ok) { showTaskMessage(key, '创建任务失败：' + (result.detail || result.error)); return; }
  data.task_id = result.task_id;
  const panel = document.querySelector('[data-channel-phase="' + key + '"]');
  if (panel) panel.dataset.taskId = result.task_id;
  selectedTask = result.task_id;
  setChannelAction(key, true);
  loadTask();
}

function showTaskMessage(key, message) {
  /* Show task errors in the corresponding channel panel. */
  const panel = document.querySelector('[data-channel-phase="' + key + '"]');
  if (panel) panel.textContent = message;
}

function phaseDuration(task, phase) {
  /* Format the persisted duration of one phase, including live elapsed time. */
  const timing = (task.phase_times || {})[phase];
  if (!timing) return '';
  const end = timing.finished_at || Date.now() / 1000;
  const seconds = Math.max(0, Math.round(end - timing.started_at));
  return ' · ' + Math.floor(seconds / 60) + '分' + (seconds % 60) + '秒';
}

function taskHtml(task) {
  /* Render phase states and a compact summary without the log stream. */
  const phases = ['preflight', 'git', 'xlua', 'ab', 'analysis'];
  const names = { preflight: '环境预检', git: '同步 Git 资源', xlua: '清空并重新生成 XLua', ab: '生成 AB 资源包', analysis: 'AI 结果分析' };
  const effectiveStage = phases.includes(task.stage) ? task.stage : (task.status === 'failed' ? 'ab' : '');
  const current = phases.indexOf(effectiveStage);
  const analysis = task.logs.filter(log => log.stage === 'analysis' && log.message !== '__AB2_ANALYSIS_STARTED__');
  const started = task.logs.some(log => log.message === '__AB2_ANALYSIS_STARTED__');
  const analysisDone = Boolean(task.report_available) || analysis.some(log => log.message.includes('根因') || log.message.includes('关键证据'));
  const analysisFailed = !analysisDone && analysis.some(log => log.message.includes('分析失败') || log.message.includes('未返回分析结果') || log.message.includes('未找到 OpenCode') || log.message.includes('分析超时'));
  const cards = phases.map((phase, index) => {
    let state = 'pending';
    if (phase === 'analysis' && analysisFailed) state = 'failed';
    else if (phase === 'analysis' && analysisDone) state = 'success';
    else if (phase === 'analysis' && started) state = 'running';
    else if (task.status === 'failed' && phase === effectiveStage) state = 'failed';
    else if (task.status === 'success' && index < 4) state = 'success';
    else if (index < current) state = 'success';
    else if (phase === effectiveStage && task.status !== 'failed') state = 'running';
    const detail = phase === 'analysis' && analysisDone ? `分析完成 <button class="report" onclick="openAiReport('${task.id}')">查看报告</button>` : phase === 'analysis' && started ? '报告分析中...' : state === 'success' ? '已完成' : state === 'failed' ? '失败' : phase === effectiveStage ? '执行中' : '未执行';
    return `<div class="phase ${state}"><b>${names[phase]}</b><span>${state === 'skipped' ? '已禁用' : state === 'pending' ? '未执行' : state === 'running' ? '执行中' : state === 'success' ? '成功' : '失败'}${phaseDuration(task, phase)}</span><small>${detail}</small></div>`;
  }).join('');
    return `<div class="task-inline-head"><span>${task.status} · ${(phases.includes(task.stage) ? task.stage : task.status === 'failed' ? 'ab' : 'waiting')}</span></div><div class="phases">${cards}</div><div class="task-summary">分支：${esc(task.branch)} · Commit：${esc(task.commit_sha || '-')}</div>`;
}

async function loadTask() {
  /* Refresh every channel task so one selected task cannot hide other states. */
  await Promise.all(Object.keys(projectData).filter(key => projectData[key].task_id).map(key => loadTaskForKey(key)));
}

async function loadTaskForKey(key) {
  /* Poll and render the durable task belonging to one channel card. */
  const taskId = projectData[key].task_id;
  const response = await fetch('/api/tasks/' + taskId);
  if (!response.ok) return;
  const task = await response.json();
  if (task.id !== taskId) return;
  lastTask = task;
  const panel = document.querySelector('[data-channel-phase="' + key + '"]');
  if (panel) panel.innerHTML = taskHtml(task);
  const analysisRunning = task.status === 'failed' && task.logs.some(log => log.message === '__AB2_ANALYSIS_STARTED__') && !task.logs.some(log => log.stage === 'analysis' && log.message !== '__AB2_ANALYSIS_STARTED__');
  setChannelAction(key, analysisRunning || ['queued', 'running', 'cancel_requested'].includes(task.status));
}

async function loadSelectedTask() {
  /* Keep the selected task variable for report context while cards poll independently. */
  if (!selectedTask) return;
  const response = await fetch('/api/tasks/' + selectedTask);
  if (!response.ok) return;
  lastTask = await response.json();
   const panel = document.querySelector('[data-channel-phase="' + Object.keys(projectData).find(key => projectData[key].task_id === lastTask.id) + '"]');
   if (panel) panel.innerHTML = taskHtml(lastTask);
   const analysisRunning = lastTask.status === 'failed' && lastTask.logs.some(log => log.message === '__AB2_ANALYSIS_STARTED__') && !lastTask.logs.some(log => log.stage === 'analysis' && log.message !== '__AB2_ANALYSIS_STARTED__');
   Object.keys(projectData).forEach(key => { if (projectData[key].task_id === lastTask.id) setChannelAction(key, analysisRunning || ['queued', 'running', 'cancel_requested'].includes(lastTask.status)); });
}

async function openAiReport(taskId) {
  /* Open the durable HTML report generated for this task. */
  const popup = window.open('about:blank', 'ab2-ai-report', 'width=900,height=700');
  if (!popup) return;
  const result = await fetch('/api/tasks/' + taskId + '/report').then(response => response.json());
  if (result.url) popup.location.href = result.url;
}

document.querySelector('#refresh').onclick = loadAgents;
loadAgents();
// Refresh live Agent and project membership independently from task polling.
setInterval(loadAgents, 5000);
setInterval(loadTask, 2000);
