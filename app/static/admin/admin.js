const ROUTES = {
  overview: {
    eyebrow: "工作概览",
    title: "安全跟进工作台",
    description: "聚焦需要管理员关注的报告、个案和系统状态。",
  },
  reports: {
    eyebrow: "报告中心",
    title: "心理支持报告",
    description: "智能报告用于辅助人工判断，不构成临床诊断。",
  },
  cases: {
    eyebrow: "人工跟进",
    title: "风险个案",
    description: "记录状态变化与跟进备注，形成可复盘的处理闭环。",
  },
  alerts: {
    eyebrow: "风险触达",
    title: "预警中心",
    description: "查看高风险预警的发送渠道、结果与时间。",
  },
  operations: {
    eyebrow: "运行审计",
    title: "系统运行",
    description: "检查异步工具任务与多 Agent 推理运行记录。",
  },
};

const state = {
  token: sessionStorage.getItem("mindnook_admin_token"),
  user: null,
  route: "overview",
  operationTab: "jobs",
  pages: { reports: 1, cases: 1, alerts: 1, jobs: 1, runs: 1 },
  pageSize: 10,
  reports: [],
  cases: [],
  alerts: [],
  jobs: [],
  runs: [],
  skills: [],
  noteCaseId: null,
};

const loginView = document.querySelector("#loginView");
const appView = document.querySelector("#appView");
const loginForm = document.querySelector("#loginForm");
const loginError = document.querySelector("#loginError");
const pageStatus = document.querySelector("#pageStatus");
const refreshButton = document.querySelector("#refreshButton");
const detailDrawer = document.querySelector("#detailDrawer");
const drawerContent = document.querySelector("#drawerContent");
const drawerPanel = detailDrawer.querySelector(".drawer-panel");
const confirmDialog = document.querySelector("#confirmDialog");
const noteDialog = document.querySelector("#noteDialog");
const toastElement = document.querySelector("#toast");

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    logout();
    throw new Error("登录状态已失效，请重新登录。");
  }
  if (!response.ok) {
    let message = "请求失败，请稍后重试。";
    try {
      const payload = await response.json();
      message = payload.detail || message;
    } catch (_error) {
      // Keep the stable fallback message for non-JSON failures.
    }
    throw new Error(message);
  }
  return response;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showApp() {
  loginView.classList.add("hidden");
  appView.classList.remove("hidden");
  document.querySelector("#adminName").textContent = state.user?.display_name || "管理员";
}

function logout() {
  sessionStorage.removeItem("mindnook_admin_token");
  state.token = null;
  state.user = null;
  closeDrawer();
  closeMobileMenu();
  appView.classList.add("hidden");
  loginView.classList.remove("hidden");
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}

function riskBadge(level) {
  const span = element("span", `badge ${level || "neutral"}`);
  span.textContent = { CHAT: "CHAT", CONSULT: "CONSULT", RISK: "RISK" }[level] || "未评估";
  return span;
}

function statusBadge(status, labels = {}) {
  const rawStatus = status || "unknown";
  const normalized = rawStatus.toLowerCase();
  const labelKey = { success: "succeeded", dead: "dead_letter" }[normalized] || normalized;
  const span = element("span", `status-badge status-${normalized}`);
  span.textContent = labels[rawStatus] || labels[labelKey] || rawStatus;
  return span;
}

function empty(text, tableColumns = 0) {
  if (tableColumns) {
    const row = document.createElement("tr");
    const cell = element("td", "table-empty", text);
    cell.colSpan = tableColumns;
    row.appendChild(cell);
    return row;
  }
  return element("div", "empty-state", text);
}

function setPageStatus(message = "", type = "") {
  pageStatus.textContent = message;
  pageStatus.className = `page-status${type ? ` ${type}` : ""}`;
}

let toastTimer;
function toast(message, type = "success") {
  window.clearTimeout(toastTimer);
  toastElement.textContent = message;
  toastElement.className = `toast visible ${type}`;
  toastTimer = window.setTimeout(() => {
    toastElement.className = "toast";
  }, 2800);
}

function truncate(value, maxLength = 72) {
  if (!value) return "—";
  return value.length > maxLength ? `${value.slice(0, maxLength)}…` : value;
}

function routeFromHash() {
  const route = window.location.hash.replace(/^#\/?/, "");
  return ROUTES[route] ? route : "overview";
}

function navigate(route) {
  const target = ROUTES[route] ? route : "overview";
  if (routeFromHash() === target) {
    activateRoute(target);
  } else {
    window.location.hash = `#/${target}`;
  }
}

async function activateRoute(route) {
  if (!state.token || !ROUTES[route]) return;
  state.route = route;
  const config = ROUTES[route];
  document.querySelector("#pageEyebrow").textContent = config.eyebrow;
  document.querySelector("#pageTitle").textContent = config.title;
  document.querySelector("#pageDescription").textContent = config.description;
  document.querySelectorAll("[data-page]").forEach((page) => {
    page.classList.toggle("hidden", page.dataset.page !== route);
  });
  document.querySelectorAll("[data-route]").forEach((link) => {
    const active = link.dataset.route === route;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  closeMobileMenu();
  closeDrawer();
  await loadCurrentPage();
}

async function loadCurrentPage() {
  refreshButton.disabled = true;
  setPageStatus("正在更新本页数据…", "loading");
  try {
    if (state.route === "overview") await loadOverview();
    if (state.route === "reports") await loadReports();
    if (state.route === "cases") await loadCases();
    if (state.route === "alerts") await loadAlerts();
    if (state.route === "operations") await loadOperationTab();
    setPageStatus("");
  } catch (error) {
    setPageStatus(error.message, "error");
  } finally {
    refreshButton.disabled = false;
  }
}

function paginate(items, key) {
  const pageCount = Math.max(1, Math.ceil(items.length / state.pageSize));
  state.pages[key] = Math.min(Math.max(1, state.pages[key]), pageCount);
  const start = (state.pages[key] - 1) * state.pageSize;
  return {
    items: items.slice(start, start + state.pageSize),
    page: state.pages[key],
    pageCount,
    total: items.length,
  };
}

function renderPagination(containerId, key, result, render) {
  const container = document.querySelector(containerId);
  container.replaceChildren();
  const summary = element(
    "span",
    "pagination-summary",
    `第 ${result.page} / ${result.pageCount} 页 · 共 ${result.total} 条`,
  );
  const actions = element("div", "pagination-actions");
  const previous = element("button", "page-btn", "上一页");
  previous.type = "button";
  previous.disabled = result.page <= 1;
  previous.addEventListener("click", () => {
    state.pages[key] -= 1;
    render();
  });
  const next = element("button", "page-btn", "下一页");
  next.type = "button";
  next.disabled = result.page >= result.pageCount;
  next.addEventListener("click", () => {
    state.pages[key] += 1;
    render();
  });
  actions.append(previous, next);
  container.append(summary, actions);
}

async function loadOverview() {
  const [summaryResponse, reportsResponse, casesResponse] = await Promise.all([
    api("/api/v1/admin/dashboard/summary"),
    api("/api/v1/admin/reports?risk_level=RISK"),
    api("/api/v1/admin/cases"),
  ]);
  const [summary, highRiskReports, cases] = await Promise.all([
    summaryResponse.json(),
    reportsResponse.json(),
    casesResponse.json(),
  ]);
  document.querySelector("#reportCount").textContent = summary.reports;
  document.querySelector("#highRiskCount").textContent = summary.high_risk;
  document.querySelector("#openCaseCount").textContent = summary.open_cases;
  document.querySelector("#failedJobCount").textContent = summary.failed_jobs;

  const riskList = document.querySelector("#overviewRiskList");
  riskList.replaceChildren();
  const recentRisks = highRiskReports.slice(0, 5);
  if (!recentRisks.length) riskList.appendChild(empty("当前没有高风险报告。"));
  recentRisks.forEach((report) => {
    const card = element("button", "overview-item");
    card.type = "button";
    const top = element("div", "overview-item-top");
    top.append(element("strong", "", report.student), riskBadge(report.risk_level));
    card.append(top, element("p", "", truncate(report.emotion_summary, 92)));
    card.appendChild(element("time", "", formatDate(report.created_at)));
    card.addEventListener("click", () => requestReportContext(report));
    riskList.appendChild(card);
  });

  const caseList = document.querySelector("#overviewCaseList");
  caseList.replaceChildren();
  const openCases = cases.filter((item) => item.status !== "closed").slice(0, 5);
  if (!openCases.length) caseList.appendChild(empty("当前没有待跟进个案。"));
  openCases.forEach((item) => {
    const card = element("button", "overview-item");
    card.type = "button";
    const top = element("div", "overview-item-top");
    top.append(riskBadge(item.risk_level), caseStatusBadge(item.status));
    card.append(top, element("p", "", truncate(item.handoff_summary, 92)));
    card.appendChild(element("time", "", formatDate(item.created_at)));
    card.addEventListener("click", () => viewCaseHistory(item));
    caseList.appendChild(card);
  });
}

async function loadReports() {
  const filter = document.querySelector("#riskFilter").value;
  const query = filter ? `?risk_level=${encodeURIComponent(filter)}` : "";
  const response = await api(`/api/v1/admin/reports${query}`);
  state.reports = await response.json();
  state.pages.reports = 1;
  renderReports();
}

function renderReports() {
  const result = paginate(state.reports, "reports");
  const body = document.querySelector("#reportRows");
  body.replaceChildren();
  result.items.forEach((report) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "cell-strong", report.student));
    const summary = element("td", "summary-cell");
    summary.append(element("strong", "", report.emotion_summary));
    summary.appendChild(element("p", "", truncate(report.reason_summary, 110)));
    row.appendChild(summary);
    const risk = document.createElement("td");
    risk.appendChild(riskBadge(report.risk_level));
    row.appendChild(risk);
    row.appendChild(element("td", "nowrap", formatDate(report.created_at)));
    const action = document.createElement("td");
    const button = element("button", "small-btn", "查看详情");
    button.type = "button";
    button.addEventListener("click", () => requestReportContext(report));
    action.appendChild(button);
    row.appendChild(action);
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("暂无符合条件的报告", 5));
  renderPagination("#reportPagination", "reports", result, renderReports);
}

async function requestReportContext(report) {
  const confirmed = await confirmAction(
    "查看必要上下文",
    "查看学生原始表达会被写入审计日志。请确认当前工作确有必要。",
    "继续查看",
  );
  if (!confirmed) return;
  try {
    const response = await api(`/api/v1/admin/reports/${report.id}/context`);
    const context = await response.json();
    const content = document.createDocumentFragment();
    content.appendChild(detailNotice(context.notice));
    content.appendChild(detailSection("学生原始表达", context.student_expression || "无可用内容"));
    content.appendChild(detailSection("工作人员交接摘要", context.handoff_summary || "暂无交接摘要"));
    content.appendChild(detailSection("情绪摘要", report.emotion_summary));
    content.appendChild(detailSection("判断依据", report.reason_summary));
    const facts = element("dl", "detail-facts");
    appendFact(facts, "风险等级", report.risk_level);
    appendFact(facts, "生成时间", formatDate(report.created_at));
    content.appendChild(facts);
    openDrawer("报告详情", content, "必要上下文");
  } catch (error) {
    toast(error.message, "error");
  }
}

function caseStatusBadge(status) {
  return statusBadge(status, {
    pending: "待处理",
    notified: "已通知",
    assigned: "已分配",
    following: "跟进中",
    closed: "已关闭",
  });
}

async function loadCases() {
  const response = await api("/api/v1/admin/cases");
  state.cases = await response.json();
  state.pages.cases = 1;
  renderCases();
}

function renderCases() {
  const status = document.querySelector("#caseStatusFilter").value;
  const filtered = status ? state.cases.filter((item) => item.status === status) : state.cases;
  const result = paginate(filtered, "cases");
  const body = document.querySelector("#caseRows");
  body.replaceChildren();
  result.items.forEach((item) => {
    const row = document.createElement("tr");
    const risk = document.createElement("td");
    risk.appendChild(riskBadge(item.risk_level));
    row.appendChild(risk);
    const summary = element("td", "summary-cell");
    summary.appendChild(element("strong", "", truncate(item.handoff_summary, 130)));
    if (item.note) summary.appendChild(element("p", "", `最近备注：${truncate(item.note, 90)}`));
    row.appendChild(summary);
    const statusCell = document.createElement("td");
    statusCell.appendChild(caseStatusBadge(item.status));
    row.appendChild(statusCell);
    row.appendChild(element("td", "nowrap", formatDate(item.created_at)));
    const actions = element("td", "table-actions");
    const history = element("button", "small-btn", "详情");
    history.type = "button";
    history.addEventListener("click", () => viewCaseHistory(item));
    const note = element("button", "small-btn", "备注");
    note.type = "button";
    note.addEventListener("click", () => openNoteDialog(item));
    actions.append(history, note);
    if (item.status !== "following") {
      const follow = element("button", "small-btn", "开始跟进");
      follow.type = "button";
      follow.addEventListener("click", () => updateCaseStatus(item, "following"));
      actions.appendChild(follow);
    }
    if (item.status !== "closed") {
      const close = element("button", "small-btn danger-btn", "关闭");
      close.type = "button";
      close.addEventListener("click", () => updateCaseStatus(item, "closed"));
      actions.appendChild(close);
    }
    row.appendChild(actions);
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("暂无符合条件的风险个案", 5));
  renderPagination("#casePagination", "cases", result, renderCases);
}

async function updateCaseStatus(item, status) {
  if (status === item.status) return;
  if (status === "closed") {
    const confirmed = await confirmAction(
      "关闭风险个案",
      "关闭后个案将移出待跟进范围，但仍会保留完整历史记录。",
      "确认关闭",
    );
    if (!confirmed) return;
  }
  try {
    await api(`/api/v1/admin/cases/${item.id}/transitions`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    toast(status === "closed" ? "个案已关闭" : "个案已进入跟进状态");
    await loadCases();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function viewCaseHistory(item) {
  try {
    const response = await api(`/api/v1/admin/cases/${item.id}/events`);
    const events = await response.json();
    const content = document.createDocumentFragment();
    content.appendChild(detailSection("交接摘要", item.handoff_summary));
    const facts = element("dl", "detail-facts");
    appendFact(facts, "当前状态", caseStatusBadgeText(item.status));
    appendFact(facts, "风险等级", item.risk_level);
    appendFact(facts, "创建时间", formatDate(item.created_at));
    content.appendChild(facts);
    if (item.note) content.appendChild(detailSection("最近备注", item.note));
    const timeline = element("section", "detail-section");
    timeline.appendChild(element("h3", "", "处理时间线"));
    const list = element("ol", "timeline");
    if (!events.length) list.appendChild(element("li", "", "暂无历史记录"));
    events.forEach((event) => {
      const entry = document.createElement("li");
      entry.appendChild(element("time", "", formatDate(event.created_at)));
      entry.appendChild(element("strong", "", caseEventLabel(event.event_type)));
      const detail = event.content || [caseStatusBadgeText(event.from_status), caseStatusBadgeText(event.to_status)].filter(Boolean).join(" → ");
      if (detail) entry.appendChild(element("p", "", detail));
      list.appendChild(entry);
    });
    timeline.appendChild(list);
    content.appendChild(timeline);
    openDrawer("个案详情", content, "人工跟进");
  } catch (error) {
    toast(error.message, "error");
  }
}

function caseStatusBadgeText(status) {
  return { pending: "待处理", notified: "已通知", assigned: "已分配", following: "跟进中", closed: "已关闭" }[status] || status || "—";
}

function caseEventLabel(type) {
  return { created: "创建个案", note_added: "添加备注", status_changed: "更新状态" }[type] || type;
}

function openNoteDialog(item) {
  state.noteCaseId = item.id;
  document.querySelector("#caseNote").value = item.note || "";
  document.querySelector("#noteError").textContent = "";
  noteDialog.showModal();
  document.querySelector("#caseNote").focus();
}

async function loadAlerts() {
  const response = await api("/api/v1/admin/alerts");
  state.alerts = await response.json();
  state.pages.alerts = 1;
  renderAlerts();
}

function renderAlerts() {
  const status = document.querySelector("#alertStatusFilter").value;
  const filtered = status ? state.alerts.filter((item) => item.status === status) : state.alerts;
  const result = paginate(filtered, "alerts");
  const body = document.querySelector("#alertRows");
  body.replaceChildren();
  result.items.forEach((item) => {
    const row = document.createElement("tr");
    const statusCell = document.createElement("td");
    statusCell.appendChild(statusBadge(item.status, { sent: "已发送", failed: "发送失败", pending: "待发送" }));
    row.appendChild(statusCell);
    row.appendChild(element("td", "cell-strong", item.channel));
    row.appendChild(element("td", "summary-cell", item.summary));
    row.appendChild(element("td", "nowrap", formatDate(item.created_at)));
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("暂无符合条件的预警记录", 4));
  renderPagination("#alertPagination", "alerts", result, renderAlerts);
}

async function loadOperationTab() {
  if (state.operationTab === "jobs") await loadJobs();
  else if (state.operationTab === "runs") await loadAgentRuns();
  else await loadSkills();
}

async function switchOperationTab(tab) {
  state.operationTab = ["jobs", "runs", "skills"].includes(tab) ? tab : "jobs";
  document.querySelectorAll("[data-operation-tab]").forEach((button) => {
    const active = button.dataset.operationTab === state.operationTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelector("#jobsPanel").classList.toggle("hidden", state.operationTab !== "jobs");
  document.querySelector("#runsPanel").classList.toggle("hidden", state.operationTab !== "runs");
  document.querySelector("#skillsPanel").classList.toggle("hidden", state.operationTab !== "skills");
  await loadCurrentPage();
}

async function loadJobs() {
  const response = await api("/api/v1/admin/jobs");
  state.jobs = await response.json();
  state.pages.jobs = 1;
  renderJobs();
}

function renderJobs() {
  const selectedStatus = document.querySelector("#jobStatusFilter").value;
  const status = {
    succeeded: "SUCCESS",
    dead_letter: "DEAD",
  }[selectedStatus] || selectedStatus.toUpperCase();
  const filtered = status ? state.jobs.filter((item) => item.status === status) : state.jobs;
  const result = paginate(filtered, "jobs");
  const body = document.querySelector("#jobRows");
  body.replaceChildren();
  result.items.forEach((item) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "cell-strong", jobTypeLabel(item.job_type)));
    const statusCell = document.createElement("td");
    statusCell.appendChild(statusBadge(item.status, {
      pending: "等待执行",
      running: "执行中",
      succeeded: "已完成",
      dead_letter: "异常任务",
    }));
    row.appendChild(statusCell);
    row.appendChild(element("td", "", String(item.attempt_count)));
    row.appendChild(element("td", item.last_error ? "error-text" : "muted-text", truncate(item.last_error || "无", 100)));
    row.appendChild(element("td", "nowrap", formatDate(item.created_at)));
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("暂无符合条件的后台任务", 5));
  renderPagination("#jobPagination", "jobs", result, renderJobs);
}

function jobTypeLabel(type) {
  type = {
    EXCEL_REPORT: "ledger_write",
    CASE_CREATE: "case_create",
    ALERT_SEND: "alert_send",
  }[type] || type;
  return { ledger_write: "写入风险台账", case_create: "创建风险个案", alert_send: "发送风险预警" }[type] || type;
}

async function loadSkills() {
  const response = await api("/api/v1/admin/skills");
  state.skills = await response.json();
  const body = document.querySelector("#skillRows");
  body.replaceChildren();
  state.skills.forEach((skill) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "cell-strong", skill.name));
    const status = document.createElement("td");
    status.appendChild(statusBadge(skill.status, {
      READY: "READY",
      WARN: "WARN",
      FAILED: "FAILED",
    }));
    row.appendChild(status);
    row.appendChild(element("td", "", skill.description || "—"));
    row.appendChild(element("td", skill.issues.length ? "error-text" : "muted-text", skill.issues.join("；") || "校验通过"));
    body.appendChild(row);
  });
  if (!state.skills.length) body.appendChild(empty("未发现 Skill 资产", 4));
}

async function loadAgentRuns() {
  const response = await api("/api/v1/admin/agent-runs");
  state.runs = await response.json();
  state.pages.runs = 1;
  renderAgentRuns();
}

function renderAgentRuns() {
  const result = paginate(state.runs, "runs");
  const body = document.querySelector("#agentRunRows");
  body.replaceChildren();
  result.items.forEach((run) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "cell-strong", run.risk_level));
    row.appendChild(element("td", "", `${run.provider} · ${run.model_calls} 次`));
    const status = document.createElement("td");
    status.appendChild(statusBadge(run.degraded ? "degraded" : run.status, {
      succeeded: "正常",
      completed: "正常",
      failed: "失败",
      degraded: "已降级",
    }));
    row.appendChild(status);
    row.appendChild(element("td", "nowrap", `${run.total_latency_ms} ms`));
    const action = document.createElement("td");
    const button = element("button", "small-btn", "查看运行");
    button.type = "button";
    button.addEventListener("click", () => viewAgentRun(run));
    action.appendChild(button);
    row.appendChild(action);
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("暂无 Agent 运行记录", 5));
  renderPagination("#runPagination", "runs", result, renderAgentRuns);
}

async function viewAgentRun(run) {
  try {
    const [stepsResponse, traceResponse] = await Promise.all([
      api(`/api/v1/admin/agent-runs/${run.id}/steps`),
      api(`/api/v1/admin/agent-runs/${run.id}/trace`),
    ]);
    const [steps, trace] = await Promise.all([stepsResponse.json(), traceResponse.json()]);
    const content = document.createDocumentFragment();
    const facts = element("dl", "detail-facts");
    appendFact(facts, "风险等级", run.risk_level);
    appendFact(facts, "模型服务", run.provider);
    appendFact(facts, "模型调用", `${run.model_calls} 次`);
    appendFact(facts, "总耗时", `${run.total_latency_ms} ms`);
    appendFact(facts, "协作事件", `${trace.collaboration_events.length} 条`);
    appendFact(facts, "任务数量", `${trace.tasks.length} 个`);
    content.appendChild(facts);
    if (trace.trace_notice) content.appendChild(detailNotice(trace.trace_notice));
    content.appendChild(detailSection("脱敏输入", trace.redacted_input || "—"));
    content.appendChild(detailSection("记忆摘要 memoryBrief", trace.memoryBrief || "本轮没有历史摘要"));

    const riskSection = element("section", "detail-section");
    riskSection.appendChild(element("h3", "", "风险判断链"));
    riskSection.appendChild(detailNotice("UnderstandingAgent 展示的是初步判断；运行采用的是固定规则与 SafetyAgent 合并后的最终等级。"));
    const riskList = element("ol", "step-list");
    (trace.risk_decisions || []).forEach((decision) => {
      const item = document.createElement("li");
      const head = element("div", "step-head");
      head.appendChild(element("strong", "", decision.role || decision.stage));
      head.appendChild(riskBadge(decision.risk_level));
      item.appendChild(head);
      const metadata = [];
      if (decision.confidence !== null && decision.confidence !== undefined) {
        metadata.push(`置信度 ${Number(decision.confidence).toFixed(2)}`);
      }
      if (decision.degraded) metadata.push("降级判断");
      if (decision.hard_rule_hit === true) metadata.push("命中硬规则");
      if (decision.immediate_danger === true) metadata.push("存在即时危险信号");
      if (metadata.length) item.appendChild(element("p", "muted-text", metadata.join(" · ")));
      if (decision.reason) item.appendChild(element("p", "step-summary", decision.reason));
      if (decision.topics?.length) item.appendChild(element("p", "muted-text", `识别主题：${decision.topics.join("、")}`));
      riskList.appendChild(item);
    });
    if (!trace.risk_decisions?.length) riskList.appendChild(element("li", "", "该历史运行没有保存阶段风险判断。"));
    riskSection.appendChild(riskList);
    content.appendChild(riskSection);

    const stepSection = element("section", "detail-section");
    stepSection.appendChild(element("h3", "", "Agent Steps"));
    const stepList = element("ol", "step-list");
    steps.forEach((step) => {
      const item = document.createElement("li");
      const head = element("div", "step-head");
      head.append(element("strong", "", `${step.sequence}. ${step.step_name}`));
      head.appendChild(statusBadge(step.status, { succeeded: "成功", failed: "失败", skipped: "跳过" }));
      item.appendChild(head);
      item.appendChild(element("p", "", `${step.model_name || "规则引擎"} · ${step.latency_ms} ms · ${step.input_tokens || 0}/${step.output_tokens || 0} tokens`));
      if (step.summary) item.appendChild(element("p", "step-summary", step.summary));
      if (step.error_code) item.appendChild(element("p", "error-text", step.error_code));
      stepList.appendChild(item);
    });
    if (!steps.length) stepList.appendChild(element("li", "", "暂无步骤记录"));
    stepSection.appendChild(stepList);
    content.appendChild(stepSection);
    openDrawer("Agent 运行详情", content, "审计轨迹");
  } catch (error) {
    toast(error.message, "error");
  }
}

function appendFact(list, label, value) {
  const wrapper = document.createElement("div");
  wrapper.append(element("dt", "", label), element("dd", "", value || "—"));
  list.appendChild(wrapper);
}

function detailNotice(text) {
  return element("p", "detail-notice", text);
}

function detailSection(title, text) {
  const section = element("section", "detail-section");
  section.append(element("h3", "", title), element("p", "detail-copy", text || "—"));
  return section;
}

function openDrawer(title, content, eyebrow = "详细信息") {
  document.querySelector("#drawerTitle").textContent = title;
  document.querySelector("#drawerEyebrow").textContent = eyebrow;
  drawerContent.replaceChildren(content);
  detailDrawer.classList.add("open");
  detailDrawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  drawerPanel.focus();
}

function closeDrawer() {
  detailDrawer.classList.remove("open");
  detailDrawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
}

function confirmAction(title, message, acceptLabel = "确认") {
  document.querySelector("#confirmTitle").textContent = title;
  document.querySelector("#confirmMessage").textContent = message;
  document.querySelector("#confirmAcceptButton").textContent = acceptLabel;
  confirmDialog.returnValue = "";
  confirmDialog.showModal();
  return new Promise((resolve) => {
    confirmDialog.addEventListener(
      "close",
      () => resolve(confirmDialog.returnValue === "confirm"),
      { once: true },
    );
  });
}

function openMobileMenu() {
  document.querySelector("#adminSidebar").classList.add("open");
  document.querySelector("#sidebarBackdrop").classList.remove("hidden");
  document.body.classList.add("nav-open");
}

function closeMobileMenu() {
  document.querySelector("#adminSidebar").classList.remove("open");
  document.querySelector("#sidebarBackdrop").classList.add("hidden");
  document.body.classList.remove("nav-open");
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  loginError.textContent = "";
  const submit = loginForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const response = await fetch("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: loginForm.username.value.trim(),
        password: loginForm.password.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "登录失败。");
    if (payload.user.role !== "admin") throw new Error("该账号不能登录管理端。");
    state.token = payload.access_token;
    state.user = payload.user;
    sessionStorage.setItem("mindnook_admin_token", state.token);
    showApp();
    if (!window.location.hash) window.location.hash = "#/overview";
    else await activateRoute(routeFromHash());
  } catch (error) {
    loginError.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#logoutButton").addEventListener("click", logout);
refreshButton.addEventListener("click", loadCurrentPage);
document.querySelector("#riskFilter").addEventListener("change", loadReports);
document.querySelector("#caseStatusFilter").addEventListener("change", () => {
  state.pages.cases = 1;
  renderCases();
});
document.querySelector("#alertStatusFilter").addEventListener("change", () => {
  state.pages.alerts = 1;
  renderAlerts();
});
document.querySelector("#jobStatusFilter").addEventListener("change", () => {
  state.pages.jobs = 1;
  renderJobs();
});
document.querySelectorAll("[data-operation-tab]").forEach((button) => {
  button.addEventListener("click", () => switchOperationTab(button.dataset.operationTab));
});
document.querySelectorAll("[data-nav]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.risk) document.querySelector("#riskFilter").value = button.dataset.risk;
    if (button.dataset.nav === "operations") state.operationTab = "jobs";
    navigate(button.dataset.nav);
  });
});
document.querySelector("#openMenuButton").addEventListener("click", openMobileMenu);
document.querySelector("#closeMenuButton").addEventListener("click", closeMobileMenu);
document.querySelector("#sidebarBackdrop").addEventListener("click", closeMobileMenu);
document.querySelector("#closeDrawerButton").addEventListener("click", closeDrawer);
document.querySelector("#drawerBackdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && detailDrawer.classList.contains("open")) closeDrawer();
});

document.querySelector("#cancelNoteButton").addEventListener("click", () => noteDialog.close());
document.querySelector("#noteForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const note = document.querySelector("#caseNote").value.trim();
  if (!note) return;
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  document.querySelector("#noteError").textContent = "";
  try {
    await api(`/api/v1/admin/cases/${state.noteCaseId}/notes`, {
      method: "POST",
      body: JSON.stringify({ note }),
    });
    noteDialog.close();
    toast("跟进备注已保存");
    await loadCases();
  } catch (error) {
    document.querySelector("#noteError").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

window.addEventListener("hashchange", () => activateRoute(routeFromHash()));

async function restoreSession() {
  if (!state.token) return;
  try {
    const response = await api("/api/v1/me");
    const user = await response.json();
    if (user.role !== "admin") return logout();
    state.user = user;
    showApp();
    if (!window.location.hash) window.location.hash = "#/overview";
    else await activateRoute(routeFromHash());
  } catch (_error) {
    logout();
  }
}

restoreSession();
