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
    description: "优先查看需要处理的问题，再按需展开运行技术细节。",
  },
  audit: {
    eyebrow: "访问与操作追溯",
    title: "审计中心",
    description: "查询敏感访问、账号创建、个案操作和数据导出记录。",
  },
  users: {
    eyebrow: "账号管理",
    title: "用户管理",
    description: "查看数据库用户，并由已有管理员创建新的管理账号。",
  },
};

const RISK_LEVEL_LABELS = {
  CHAT: "普通交流",
  CONSULT: "需要关注",
  RISK: "高风险",
};

const AUDIT_ACTION_LABELS = {
  student_registered: "学生注册账号",
  admin_account_created: "创建管理员账号",
  user_list_viewed: "查看用户列表",
  report_context_viewed: "查看报告敏感原文",
  case_list_viewed: "查看个案列表",
  case_owner_assigned: "分配或转交个案",
  case_note_added: "添加个案备注",
  case_status_changed: "变更个案状态",
  case_history_viewed: "查看个案处理记录",
  audit_log_viewed: "查看审计日志",
  excel_exported: "导出 Excel",
  agent_trace_viewed: "查看 Agent 执行步骤",
  agent_collaboration_viewed: "查看 Agent 协作事件",
  agent_audit_memory_viewed: "查看 Agent 审计轨迹",
  job_group_retried: "重新执行失败流程",
};

const AUDIT_RESOURCE_LABELS = {
  user: "用户账号",
  report: "心理支持报告",
  case: "风险个案",
  audit_log: "审计日志",
  agent_run: "Agent 运行",
  job_group: "后台处理流程",
  reports: "报告导出",
  cases: "个案导出",
  "audit-logs": "审计日志导出",
};

const STEP_NAME_LABELS = {
  understanding: "理解学生表达",
  understanding_fallback: "理解降级处理",
  risk_assessment: "风险评估",
  risk_fallback: "风险评估降级处理",
  response_review: "回复安全审查",
  review_fallback: "安全审查降级处理",
  rag_fallback: "知识检索降级处理",
  context_loading: "加载上下文",
  response_prompt: "构建回复提示",
  response_generation: "生成回复",
  response_repair: "修复回复",
  skill_fallback: "技能降级处理",
  hard_safety_rules: "固定安全规则",
  final_safety_fallback: "最终安全回退",
  response_fallback: "回复生成降级处理",
};

const SKILL_NAME_LABELS = {
  academic_stress_planning: "学业压力行动计划",
  anxiety_grounding_support: "焦虑稳定支持",
  counselor_handoff_summary: "工作人员交接摘要",
  high_risk_safety_plan: "高风险安全计划",
  referral_resource_guidance: "现实求助与转介指引",
  sleep_routine_support: "睡眠作息支持",
  supportive_response_baseline: "支持性回复基准",
};

const state = {
  token: sessionStorage.getItem("mindnook_admin_token"),
  user: null,
  route: "overview",
  operationTab: "overview",
  jobStatus: "",
  jobType: "",
  pages: { reports: 1, cases: 1, alerts: 1, jobs: 1, runs: 1, users: 1, audit: 1 },
  pageSize: 10,
  reports: [],
  cases: [],
  alerts: [],
  jobs: [],
  runs: [],
  skills: [],
  users: [],
  adminUsers: [],
  auditLogs: [],
  auditTotal: 0,
  noteCaseId: null,
  ownerCaseId: null,
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
const ownerDialog = document.querySelector("#ownerDialog");
const adminRegistrationDialog = document.querySelector("#adminRegistrationDialog");
const toastElement = document.querySelector("#toast");
const adminShell = document.querySelector(".admin-shell");
let drawerReturnFocus = null;

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
  document.querySelector("#adminName").textContent = state.user?.name || "管理员";
}

function logout() {
  sessionStorage.removeItem("mindnook_admin_token");
  state.token = null;
  state.user = null;
  closeDrawer(false);
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
  span.textContent = riskLevelText(level);
  return span;
}

function riskLevelText(level) {
  return RISK_LEVEL_LABELS[level] || level || "未评估";
}

function auditActionLabel(action) {
  return AUDIT_ACTION_LABELS[action] || action || "—";
}

function auditResourceLabel(resource) {
  return AUDIT_RESOURCE_LABELS[resource] || resource || "—";
}

function stepNameLabel(name) {
  return STEP_NAME_LABELS[name] || name || "未知步骤";
}

function skillNameLabel(name) {
  return SKILL_NAME_LABELS[name] || name || "未知技能";
}

function localizedDescription(value) {
  return (value || "—")
    .replace(/\bCONSULT\b/g, "需要关注")
    .replace(/\bRISK\b/g, "高风险")
    .replace(/\bCHAT\b/g, "普通交流")
    .replace(/\s+(需要关注|高风险|普通交流)\s+/g, "$1");
}

function providerLabel(provider) {
  return {
    bailian: "百炼",
    mock: "模拟服务",
    openai_compatible: "OpenAI 兼容服务",
    "openai-compatible": "OpenAI 兼容服务",
  }[provider] || provider || "未知服务";
}

function channelLabel(channel) {
  return { log: "系统日志", console: "系统日志", email: "邮件", smtp: "邮件" }[channel] || channel || "—";
}

function userStatusLabel(status) {
  return { active: "有效", inactive: "停用", disabled: "已停用", pending: "待激活" }[status] || status || "—";
}

function errorText(value) {
  if (!value) return "无";
  const exact = {
    "report not found": "未找到对应报告",
    "unsupported tool job type": "不支持的后台任务类型",
    "worker restarted or running lease expired": "工作进程已重启或任务租约已过期",
    "deferred by alert rate limiter": "因预警发送频率限制延后执行",
  };
  return exact[value] || value;
}

function modelErrorText(value) {
  if (!value) return "";
  const exact = {
    UNSUPPORTED_PURPOSE: "不支持的模型调用目的",
    MODEL_API_KEY_MISSING: "模型服务密钥未配置",
    MODEL_NETWORK_ERROR: "模型服务网络异常",
    MODEL_RESPONSE_INVALID: "模型返回内容无效",
    MODEL_STREAM_INVALID: "模型流式返回内容无效",
    MODEL_UNKNOWN_ERROR: "未知模型服务异常",
    AgentBudgetExceeded: "Agent 运行预算或时间已超限",
  };
  if (exact[value]) return exact[value];
  if (value.startsWith("MODEL_HTTP_")) {
    return `模型服务返回 HTTP ${value.slice("MODEL_HTTP_".length)} 错误`;
  }
  return "系统运行异常";
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

function searchValue(selector) {
  return document.querySelector(selector)?.value.trim().toLocaleLowerCase("zh-CN") || "";
}

function matchesSearch(query, ...values) {
  if (!query) return true;
  return values.some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(query));
}

function isWithinDays(value, days) {
  if (!days) return true;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) && timestamp >= Date.now() - Number(days) * 86400000;
}

function updateFilterSummary(selector, visible, total, noun) {
  document.querySelector(selector).textContent = visible === total
    ? `共 ${total} 条${noun}`
    : `显示 ${visible} / ${total} 条${noun}`;
}

function recordMeta(label, value) {
  const item = element("span", "record-meta-item");
  item.append(element("small", "", label), element("strong", "", value || "—"));
  return item;
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
  document.querySelector("#lastUpdatedAt").textContent = "正在更新…";
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
  refreshButton.classList.add("is-loading");
  setPageStatus("正在更新本页数据…", "loading");
  try {
    if (state.route === "overview") await loadOverview();
    if (state.route === "reports") await loadReports();
    if (state.route === "cases") await loadCases();
    if (state.route === "alerts") await loadAlerts();
    if (state.route === "operations") await loadOperationTab();
    if (state.route === "audit") await loadAuditLogs(state.pages.audit);
    if (state.route === "users") await loadUsers();
    setPageStatus("");
    document.querySelector("#lastUpdatedAt").textContent = `已更新 ${new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date())}`;
  } catch (error) {
    setPageStatus(error.message, "error");
    document.querySelector("#lastUpdatedAt").textContent = "更新失败";
  } finally {
    refreshButton.disabled = false;
    refreshButton.classList.remove("is-loading");
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
  const response = await api("/api/v1/admin/reports");
  state.reports = await response.json();
  state.pages.reports = 1;
  renderReports();
}

function filteredReports() {
  const risk = document.querySelector("#riskFilter").value;
  const days = document.querySelector("#reportDateFilter").value;
  const query = searchValue("#reportSearch");
  return state.reports.filter((report) => (
    (!risk || report.risk_level === risk)
    && isWithinDays(report.created_at, days)
    && matchesSearch(query, report.student, report.emotion_summary, report.reason_summary)
  ));
}

function renderReports() {
  const filtered = filteredReports();
  const result = paginate(filtered, "reports");
  const list = document.querySelector("#reportRows");
  list.replaceChildren();
  result.items.forEach((report) => {
    const card = element("article", `record-card report-record${report.risk_level === "RISK" ? " is-priority" : ""}`);
    const main = element("div", "record-main");
    const heading = element("div", "record-heading");
    heading.append(element("strong", "record-title", report.student), riskBadge(report.risk_level));
    main.append(
      heading,
      element("p", "record-summary", report.emotion_summary || "暂无情绪摘要"),
      element("p", "record-description", truncate(report.reason_summary, 140)),
    );
    const meta = element("div", "record-meta");
    meta.append(recordMeta("生成时间", formatDate(report.created_at)));
    const button = element("button", "small-btn", "查看详情");
    button.type = "button";
    button.addEventListener("click", () => requestReportContext(report));
    card.append(main, meta, button);
    list.appendChild(card);
  });
  if (!result.items.length) list.appendChild(empty("没有找到符合当前条件的报告。"));
  updateFilterSummary("#reportFilterSummary", filtered.length, state.reports.length, "报告");
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
    appendFact(facts, "风险等级", riskLevelText(report.risk_level));
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
  const [caseResponse, ownerResponse] = await Promise.all([
    api("/api/v1/admin/cases"),
    api("/api/v1/admin/users?role=admin"),
  ]);
  [state.cases, state.adminUsers] = await Promise.all([
    caseResponse.json(),
    ownerResponse.json(),
  ]);
  state.pages.cases = 1;
  renderCases();
}

function renderCases() {
  const status = document.querySelector("#caseStatusFilter").value;
  const query = searchValue("#caseSearch");
  const filtered = state.cases.filter((item) => (
    (!status || item.status === status)
    && matchesSearch(
      query,
      item.handoff_summary,
      item.note,
      item.owner?.name,
      item.owner?.institutional_id,
    )
  ));
  const result = paginate(filtered, "cases");
  const list = document.querySelector("#caseRows");
  list.replaceChildren();
  result.items.forEach((item) => {
    const card = element("article", `record-card case-record${!item.owner && item.status !== "closed" ? " needs-owner" : ""}`);
    const main = element("div", "record-main");
    const heading = element("div", "record-heading");
    heading.append(riskBadge(item.risk_level), caseStatusBadge(item.status));
    if (!item.owner && item.status !== "closed") heading.appendChild(element("span", "attention-chip", "待分配"));
    main.append(heading, element("p", "record-summary", truncate(item.handoff_summary, 150)));
    if (item.note) main.appendChild(element("p", "record-description", `最近备注：${truncate(item.note, 100)}`));
    const meta = element("div", "record-meta");
    meta.append(
      recordMeta("负责人", item.owner ? `${item.owner.name}（${item.owner.institutional_id}）` : "未分配"),
      recordMeta("创建时间", formatDate(item.created_at)),
    );
    const actions = element("div", "record-actions");
    const history = element("button", "small-btn", "详情");
    history.type = "button";
    history.addEventListener("click", () => viewCaseHistory(item));
    actions.appendChild(history);
    if (!["following", "closed"].includes(item.status)) {
      const follow = element("button", "small-btn primary-action-btn", "开始跟进");
      follow.type = "button";
      follow.addEventListener("click", () => updateCaseStatus(item, "following"));
      actions.appendChild(follow);
    }
    if (item.status === "closed") {
      const reopen = element("button", "small-btn primary-action-btn", "重新打开");
      reopen.type = "button";
      reopen.addEventListener("click", () => updateCaseStatus(item, "following"));
      actions.appendChild(reopen);
    }

    const menu = document.createElement("details");
    menu.className = "action-menu";
    const menuSummary = element("summary", "small-btn", "更多操作");
    const menuPanel = element("div", "action-menu-panel");
    const note = element("button", "small-btn", "添加备注");
    note.type = "button";
    note.addEventListener("click", () => openNoteDialog(item));
    menuPanel.appendChild(note);
    if (item.status !== "closed") {
      const assign = element("button", "small-btn", item.owner ? "转交负责人" : "分配负责人");
      assign.type = "button";
      assign.addEventListener("click", () => openOwnerDialog(item));
      menuPanel.appendChild(assign);
      const close = element("button", "small-btn danger-btn", "关闭");
      close.type = "button";
      close.addEventListener("click", () => updateCaseStatus(item, "closed"));
      menuPanel.appendChild(close);
    }
    menu.append(menuSummary, menuPanel);
    actions.appendChild(menu);
    card.append(main, meta, actions);
    list.appendChild(card);
  });
  if (!result.items.length) list.appendChild(empty("没有找到符合当前条件的风险个案。"));
  updateFilterSummary("#caseFilterSummary", filtered.length, state.cases.length, "个案");
  document.querySelectorAll("[data-case-status]").forEach((button) => {
    const buttonStatus = button.dataset.caseStatus;
    const count = buttonStatus
      ? state.cases.filter((item) => item.status === buttonStatus).length
      : state.cases.length;
    const label = {
      "": "全部",
      pending: "待处理",
      notified: "已通知",
      assigned: "已分配",
      following: "跟进中",
      closed: "已关闭",
    }[buttonStatus];
    button.textContent = `${label} ${count}`;
    button.classList.toggle("active", buttonStatus === status);
  });
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
  if (item.status === "closed" && status === "following") {
    const confirmed = await confirmAction(
      "重新打开风险个案",
      "重新打开后，个案将回到跟进中状态，并继续保留原有处理记录。",
      "确认重新打开",
    );
    if (!confirmed) return;
  }
  try {
    await api(`/api/v1/admin/cases/${item.id}/transitions`, {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    toast(
      status === "closed"
        ? "个案已关闭"
        : item.status === "closed"
          ? "个案已重新打开"
          : "个案已进入跟进状态",
    );
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
    appendFact(facts, "风险等级", riskLevelText(item.risk_level));
    appendFact(facts, "负责人", item.owner ? `${item.owner.name}（${item.owner.institutional_id}）` : "未分配");
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
      if (event.actor) entry.appendChild(element("span", "muted-text", `操作人：${event.actor.name}（${event.actor.institutional_id}）`));
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
  return { created: "创建个案", note_added: "添加备注", status_changed: "更新状态", owner_assigned: "分配负责人" }[type] || type;
}

function openNoteDialog(item) {
  state.noteCaseId = item.id;
  document.querySelector("#caseNote").value = item.note || "";
  document.querySelector("#noteError").textContent = "";
  noteDialog.showModal();
  document.querySelector("#caseNote").focus();
}

function openOwnerDialog(item) {
  state.ownerCaseId = item.id;
  const select = document.querySelector("#caseOwner");
  select.replaceChildren();
  state.adminUsers.forEach((user) => {
    const option = document.createElement("option");
    option.value = user.id;
    option.textContent = `${user.name}（${user.institutional_id}）`;
    option.selected = item.owner?.id === user.id;
    select.appendChild(option);
  });
  document.querySelector("#ownerError").textContent = "";
  document.querySelector("#ownerDialog").showModal();
  select.focus();
}

async function loadAlerts() {
  const response = await api("/api/v1/admin/alerts");
  state.alerts = await response.json();
  state.pages.alerts = 1;
  renderAlerts();
}

function renderAlerts() {
  const status = document.querySelector("#alertStatusFilter").value;
  const query = searchValue("#alertSearch");
  const filtered = state.alerts.filter((item) => (
    (!status || item.status === status)
    && matchesSearch(query, channelLabel(item.channel), item.summary)
  ));
  const result = paginate(filtered, "alerts");
  const list = document.querySelector("#alertRows");
  list.replaceChildren();
  result.items.forEach((item) => {
    const card = element("article", `record-card alert-record${item.status === "failed" ? " is-priority" : ""}`);
    const marker = element("span", `channel-mark channel-${item.channel || "unknown"}`, item.channel === "email" || item.channel === "smtp" ? "邮" : "记");
    const main = element("div", "record-main");
    const heading = element("div", "record-heading");
    heading.append(
      element("strong", "record-title", channelLabel(item.channel)),
      statusBadge(item.status, { sent: "已发送", failed: "发送失败", pending: "待发送" }),
    );
    main.append(heading, element("p", "record-summary", item.summary || "暂无预警摘要"));
    const meta = element("div", "record-meta");
    meta.append(recordMeta("创建时间", formatDate(item.created_at)));
    card.append(marker, main, meta);
    list.appendChild(card);
  });
  if (!result.items.length) list.appendChild(empty("没有找到符合当前条件的预警记录。"));
  updateFilterSummary("#alertFilterSummary", filtered.length, state.alerts.length, "预警");
  renderPagination("#alertPagination", "alerts", result, renderAlerts);
}

function auditQuery(includePage = true) {
  const params = new URLSearchParams();
  const action = document.querySelector("#auditActionFilter").value.trim();
  const resource = document.querySelector("#auditResourceFilter").value.trim();
  const actor = document.querySelector("#auditActorFilter").value.trim();
  if (action) params.set("action", action);
  if (resource) params.set("resource_type", resource);
  if (actor) params.set("actor", actor);
  if (includePage) {
    params.set("page", String(state.pages.audit));
    params.set("page_size", String(state.pageSize));
  }
  return params;
}

async function loadAuditLogs(page = 1) {
  state.pages.audit = page;
  const response = await api(`/api/v1/admin/audit-logs?${auditQuery()}`);
  const payload = await response.json();
  state.auditLogs = payload.items;
  state.auditTotal = payload.total;
  renderAuditLogs();
}

function renderAuditLogs() {
  const body = document.querySelector("#auditRows");
  body.replaceChildren();
  state.auditLogs.forEach((item) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "nowrap", formatDate(item.created_at)));
    row.appendChild(element("td", "cell-strong", `${item.actor.name}（${item.actor.institutional_id}）`));
    row.appendChild(element("td", "", item.action_label || auditActionLabel(item.action)));
    row.appendChild(element("td", "", `${item.resource_type_label || auditResourceLabel(item.resource_type)}${item.resource_id ? ` · ${item.resource_id}` : ""}`));
    row.appendChild(element("td", "summary-cell", item.detail_label || item.detail || "—"));
    body.appendChild(row);
  });
  if (!state.auditLogs.length) body.appendChild(empty("暂无符合条件的审计记录", 5));

  const pageCount = Math.max(1, Math.ceil(state.auditTotal / state.pageSize));
  const container = document.querySelector("#auditPagination");
  container.replaceChildren();
  const summary = element("span", "pagination-summary", `第 ${state.pages.audit} / ${pageCount} 页 · 共 ${state.auditTotal} 条`);
  const actions = element("div", "pagination-actions");
  const previous = element("button", "small-btn", "上一页");
  previous.type = "button";
  previous.disabled = state.pages.audit <= 1;
  previous.addEventListener("click", () => loadAuditLogs(state.pages.audit - 1));
  const next = element("button", "small-btn", "下一页");
  next.type = "button";
  next.disabled = state.pages.audit >= pageCount;
  next.addEventListener("click", () => loadAuditLogs(state.pages.audit + 1));
  actions.append(previous, next);
  container.append(summary, actions);
}

async function loadUsers() {
  const role = document.querySelector("#userRoleFilter").value;
  const query = role ? `?role=${encodeURIComponent(role)}` : "";
  const response = await api(`/api/v1/admin/users${query}`);
  state.users = await response.json();
  state.pages.users = 1;
  renderUsers();
}

function renderUsers() {
  const query = searchValue("#userSearch");
  const filtered = state.users.filter((user) => matchesSearch(query, user.institutional_id, user.name));
  const result = paginate(filtered, "users");
  const body = document.querySelector("#userRows");
  body.replaceChildren();
  result.items.forEach((user) => {
    const row = document.createElement("tr");
    row.appendChild(element("td", "cell-strong", user.institutional_id));
    row.appendChild(element("td", "", user.name));
    row.appendChild(element("td", "", user.role === "admin" ? "管理员" : "学生"));
    row.appendChild(element("td", "", userStatusLabel(user.status)));
    row.appendChild(element("td", "nowrap", formatDate(user.created_at)));
    body.appendChild(row);
  });
  if (!result.items.length) body.appendChild(empty("没有找到符合当前条件的用户", 5));
  renderPagination("#userPagination", "users", result, renderUsers);
}

async function downloadExport(type, params = new URLSearchParams()) {
  const caseStatus = document.querySelector("#caseStatusFilter").value;
  const reportRisk = document.querySelector("#riskFilter").value;
  const exportMeta = {
    reports: {
      title: "导出心理支持报告",
      count: reportRisk
        ? state.reports.filter((item) => item.risk_level === reportRisk).length
        : state.reports.length,
      warning: "文件将完整包含学生原始表达等敏感原文，并按当前风险等级导出。",
    },
    cases: {
      title: "导出风险个案",
      count: caseStatus
        ? state.cases.filter((item) => item.status === caseStatus).length
        : state.cases.length,
      warning: "文件将完整包含学生原始表达和个案跟进信息。",
    },
    "audit-logs": {
      title: "导出审计日志",
      count: state.auditTotal,
      warning: "文件将包含操作人和敏感操作明细。",
    },
  }[type];
  if (!exportMeta) return;
  const confirmed = await confirmAction(
    exportMeta.title,
    `将按当前筛选条件导出 ${exportMeta.count} 条记录。${exportMeta.warning}请确保只在必要的工作场景中使用并妥善保管。`,
    "确认导出",
  );
  if (!confirmed) return;
  try {
    const query = params.toString();
    const response = await api(`/api/v1/admin/exports/${type}.xlsx${query ? `?${query}` : ""}`);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const matched = disposition.match(/filename="([^"]+)"/);
    const filename = matched?.[1] || `mindnook_${type}.xlsx`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    toast("Excel 已导出，操作已记入审计中心");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadOperationTab() {
  if (state.operationTab === "overview") await loadOperationOverview();
  else if (state.operationTab === "jobs") await loadJobs();
  else if (state.operationTab === "runs") await loadAgentRuns();
  else await loadSkills();
}

async function switchOperationTab(tab) {
  state.operationTab = ["overview", "jobs", "runs", "skills"].includes(tab) ? tab : "overview";
  document.querySelectorAll("[data-operation-tab]").forEach((button) => {
    const active = button.dataset.operationTab === state.operationTab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelector("#operationOverviewPanel").classList.toggle("hidden", state.operationTab !== "overview");
  document.querySelector("#jobsPanel").classList.toggle("hidden", state.operationTab !== "jobs");
  document.querySelector("#runsPanel").classList.toggle("hidden", state.operationTab !== "runs");
  document.querySelector("#skillsPanel").classList.toggle("hidden", state.operationTab !== "skills");
  await loadCurrentPage();
}

async function loadOperationOverview() {
  const [jobsResponse, runsResponse, skillsResponse] = await Promise.all([
    api("/api/v1/admin/job-groups"),
    api("/api/v1/admin/agent-runs"),
    api("/api/v1/admin/skills"),
  ]);
  [state.jobs, state.runs, state.skills] = await Promise.all([
    jobsResponse.json(),
    runsResponse.json(),
    skillsResponse.json(),
  ]);
  renderOperationOverview();
}

function renderOperationOverview() {
  const failedGroups = state.jobs.filter((item) => item.status === "DEAD");
  const failedRuns = state.runs.filter((item) => item.status === "failed");
  const degradedRuns = state.runs.filter((item) => item.degraded);
  const failedSkills = state.skills.filter((item) => item.status === "FAILED");
  const warningSkills = state.skills.filter((item) => item.status === "WARN");
  const issueCount = failedGroups.length + failedRuns.length + failedSkills.length;
  const noticeCount = degradedRuns.length + warningSkills.length;
  const banner = document.querySelector("#operationHealthBanner");
  banner.className = `operation-health-banner${issueCount ? " has-issues" : noticeCount ? " has-warnings" : ""}`;
  banner.replaceChildren();
  const message = element("div", "");
  message.append(
    element(
      "strong",
      "",
      issueCount
        ? `有 ${issueCount} 项需要处理`
        : noticeCount
          ? `当前没有失败，有 ${noticeCount} 项运行提醒`
          : "当前未发现需要处理的异常",
    ),
    element(
      "p",
      "",
      issueCount
        ? "请优先处理失败流程或不可用技能。等待和执行中的后台任务不会在这里展示。"
        : noticeCount
          ? "部分 Agent 使用了安全降级，或技能存在非阻断性警告，可在下方查看详情。"
          : "后台流程、Agent 运行和技能资产当前均没有需要管理员处理的异常。",
    ),
  );
  banner.appendChild(message);

  document.querySelector("#operationJobSummary").textContent = failedGroups.length
    ? `${failedGroups.length} 项失败`
    : `${state.jobs.length} 项已结束`;
  document.querySelector("#operationRunSummary").textContent = failedRuns.length
    ? `${failedRuns.length} 次失败`
    : degradedRuns.length
      ? `${degradedRuns.length} 次降级`
      : "最近运行正常";
  document.querySelector("#operationSkillSummary").textContent = failedSkills.length
    ? `${failedSkills.length} 项不可用`
    : warningSkills.length
      ? `${warningSkills.length} 项警告`
      : `${state.skills.length} 项就绪`;

  const attention = document.querySelector("#operationAttentionList");
  attention.replaceChildren();
  failedGroups.slice(0, 3).forEach((group) => {
    attention.appendChild(operationActivityItem(
      "后台处理失败",
      `${jobGroupSummary(group)} · ${truncate(errorText(group.last_error), 72)}`,
      group.finished_at,
      "danger",
      () => switchOperationTab("jobs"),
    ));
  });
  failedRuns.slice(0, 3).forEach((run) => {
    attention.appendChild(operationActivityItem(
      "Agent 运行失败",
      `${riskLevelText(run.risk_level)} · ${run.total_latency_ms} 毫秒`,
      run.created_at,
      "danger",
      () => viewAgentRun(run),
    ));
  });
  failedSkills.slice(0, 3).forEach((skill) => {
    attention.appendChild(operationActivityItem(
      `${skillNameLabel(skill.name)}不可用`,
      skill.issues.join("；") || "技能校验失败",
      null,
      "danger",
      () => switchOperationTab("skills"),
    ));
  });
  if (!attention.children.length) attention.appendChild(empty("当前没有需要处理的问题。"));

  const recent = document.querySelector("#operationRecentList");
  recent.replaceChildren();
  const activities = [
    ...state.jobs.map((group) => ({
      title: group.status === "DEAD" ? "后台处理失败" : "后台处理完成",
      detail: jobGroupSummary(group),
      time: group.finished_at,
      tone: group.status === "DEAD" ? "danger" : "success",
      action: () => viewJobGroup(group),
    })),
    ...state.runs.map((run) => ({
      title: run.degraded ? "Agent 使用安全降级" : run.status === "failed" ? "Agent 运行失败" : "Agent 运行完成",
      detail: `${riskLevelText(run.risk_level)} · ${run.total_latency_ms} 毫秒`,
      time: run.created_at,
      tone: run.degraded || run.status === "failed" ? "warning" : "success",
      action: () => viewAgentRun(run),
    })),
  ].sort((left, right) => new Date(right.time || 0) - new Date(left.time || 0));
  activities.slice(0, 5).forEach((item) => {
    recent.appendChild(operationActivityItem(item.title, item.detail, item.time, item.tone, item.action));
  });
  if (!recent.children.length) recent.appendChild(empty("暂无运行记录。"));
}

function operationActivityItem(title, detail, time, tone, action) {
  const button = element("button", `operation-activity-item tone-${tone}`);
  button.type = "button";
  const copy = element("span", "operation-activity-copy");
  copy.append(element("strong", "", title), element("small", "", detail));
  button.append(copy, element("time", "", time ? formatDate(time) : "查看详情"));
  button.addEventListener("click", action);
  return button;
}

async function loadJobs() {
  const response = await api("/api/v1/admin/job-groups");
  state.jobs = await response.json();
  state.pages.jobs = 1;
  renderJobs();
}

function renderJobs() {
  const typeScoped = state.jobs.filter((item) => !state.jobType || jobGroupType(item) === state.jobType);
  const filtered = typeScoped
    .filter((item) => !state.jobStatus || item.status === state.jobStatus)
    .sort((left, right) => {
      if (left.status !== right.status) return left.status === "DEAD" ? -1 : 1;
      return new Date(right.finished_at || 0) - new Date(left.finished_at || 0);
    });
  const result = paginate(filtered, "jobs");
  const list = document.querySelector("#jobGroupList");
  list.replaceChildren();
  result.items.forEach((item) => list.appendChild(buildJobGroupCard(item)));
  if (!result.items.length) list.appendChild(empty("暂无符合条件的处理记录。"));
  document.querySelectorAll("[data-job-status]").forEach((button) => {
    const status = button.dataset.jobStatus;
    const count = status ? typeScoped.filter((item) => item.status === status).length : typeScoped.length;
    const label = status === "DEAD" ? "失败" : status === "SUCCESS" ? "已完成" : "全部";
    button.textContent = `${label} ${count}`;
    button.classList.toggle("active", status === state.jobStatus);
  });
  renderPagination("#jobPagination", "jobs", result, renderJobs);
}

function jobGroupSummary(group) {
  const completed = group.steps.filter((step) => step.status === "SUCCESS").length;
  const failed = group.steps.filter((step) => step.status === "DEAD").length;
  return failed
    ? `${completed} 个步骤完成，${failed} 个步骤失败`
    : `${group.steps.length} 个步骤全部完成`;
}

function jobGroupTitle(group) {
  const type = jobGroupType(group);
  if (type === "RISK_FOLLOWUP") return "高风险跟进流程";
  if (type === "EXCEL_REPORT") return "心理报告写入流程";
  return "后台处理流程";
}

function jobGroupType(group) {
  const types = new Set(group.steps.map((step) => String(step.job_type || "").toUpperCase()));
  if (types.has("CASE_CREATE") || types.has("ALERT_SEND")) return "RISK_FOLLOWUP";
  if (types.has("EXCEL_REPORT")) return "EXCEL_REPORT";
  return "OTHER";
}

function buildJobGroupCard(group) {
  const card = element("article", `job-group-card${group.status === "DEAD" ? " failed" : ""}`);
  const header = element("div", "job-group-header");
  const heading = element("div", "");
  heading.append(
    element("strong", "", jobGroupTitle(group)),
    element("p", "muted-text", `报告 ${group.report_id} · ${formatDate(group.finished_at)}`),
  );
  header.append(
    heading,
    statusBadge(group.status, { succeeded: "全部完成", dead_letter: "处理失败" }),
  );
  card.appendChild(header);
  const steps = element("ol", "job-step-list");
  group.steps.forEach((step) => {
    const row = element("li", `job-step job-step-${step.status.toLowerCase()}`);
    const marker = element("span", "job-step-marker", step.status === "SUCCESS" ? "✓" : "!");
    const copy = element("div", "job-step-copy");
    copy.append(
      element("strong", "", jobTypeLabel(step.job_type)),
      element("small", "", step.status === "SUCCESS" ? "已完成" : `失败 ${step.attempt_count} 次`),
    );
    row.append(marker, copy);
    steps.appendChild(row);
  });
  card.appendChild(steps);
  if (group.last_error) card.appendChild(element("p", "job-group-error", truncate(errorText(group.last_error), 120)));
  const actions = element("div", "job-group-actions");
  const detailButton = element("button", "small-btn", "查看技术详情");
  detailButton.type = "button";
  detailButton.addEventListener("click", () => viewJobGroup(group));
  actions.appendChild(detailButton);
  if (group.status === "DEAD") {
    const retryButton = element("button", "small-btn primary-action-btn", "重新执行失败步骤");
    retryButton.type = "button";
    retryButton.addEventListener("click", () => retryJobGroup(group, retryButton));
    actions.appendChild(retryButton);
  }
  card.appendChild(actions);
  return card;
}

function viewJobGroup(group) {
  const content = document.createDocumentFragment();
  const facts = element("dl", "detail-facts");
  appendFact(facts, "报告 ID", group.report_id);
  appendFact(facts, "处理结果", group.status === "DEAD" ? "处理失败" : "全部完成");
  appendFact(facts, "累计尝试", `${group.attempt_count} 次`);
  appendFact(facts, "结束时间", formatDate(group.finished_at));
  content.appendChild(facts);
  const section = element("section", "detail-section");
  section.appendChild(element("h3", "", "处理步骤"));
  const list = element("ol", "step-list");
  group.steps.forEach((step, index) => {
    const item = document.createElement("li");
    const head = element("div", "step-head");
    head.append(
      element("strong", "", `${index + 1}. ${jobTypeLabel(step.job_type)}`),
      statusBadge(step.status, { succeeded: "已完成", dead_letter: "失败" }),
    );
    item.appendChild(head);
    item.appendChild(element("p", "muted-text", `尝试 ${step.attempt_count}/${step.max_attempts} 次 · ${formatDate(step.finished_at)}`));
    if (step.last_error) item.appendChild(element("p", "error-text", errorText(step.last_error)));
    list.appendChild(item);
  });
  section.appendChild(list);
  content.appendChild(section);
  openDrawer("后台处理详情", content, "技术详情");
}

async function retryJobGroup(group, button) {
  const failedSteps = group.steps
    .filter((step) => step.status === "DEAD")
    .map((step) => jobTypeLabel(step.job_type));
  const confirmed = await confirmAction(
    "重新执行失败步骤",
    `将重新执行：${failedSteps.join("、")}。已经成功的步骤不会重复执行，本次操作会写入审计日志。`,
    "确认重新执行",
  );
  if (!confirmed) return;
  button.disabled = true;
  try {
    await api(`/api/v1/admin/job-groups/${group.report_id}/retry`, { method: "POST" });
    toast("失败步骤已重新提交执行");
    await loadJobs();
  } catch (error) {
    toast(error.message, "error");
    button.disabled = false;
  }
}

function jobTypeLabel(type) {
  type = {
    EXCEL_REPORT: "excel_report",
    CASE_CREATE: "case_create",
    ALERT_SEND: "alert_send",
  }[type] || type;
  return {
    excel_report: "写入心理报告",
    case_create: "创建风险个案",
    alert_send: "发送风险预警",
  }[type] || type;
}

async function loadSkills() {
  const response = await api("/api/v1/admin/skills");
  state.skills = await response.json();
  renderSkills();
}

function renderSkills() {
  const body = document.querySelector("#skillRows");
  body.replaceChildren();
  const failed = state.skills.filter((skill) => skill.status === "FAILED").length;
  const warnings = state.skills.filter((skill) => skill.status === "WARN").length;
  const ready = state.skills.filter((skill) => skill.status === "READY").length;
  const summary = document.querySelector("#skillSummary");
  summary.className = `skill-summary${failed ? " has-issues" : ""}`;
  summary.textContent = failed
    ? `${failed} 个技能不可用，建议先处理校验问题。`
    : warnings
      ? `${ready} 个技能就绪，${warnings} 个技能存在警告。`
      : `${ready} 个技能全部就绪。`;
  state.skills.forEach((skill) => {
    const card = element("article", "skill-status-card");
    const head = element("div", "skill-status-head");
    head.append(element("strong", "", skillNameLabel(skill.name)), statusBadge(skill.status, {
      READY: "就绪",
      WARN: "有警告",
      FAILED: "校验失败",
    }));
    card.append(
      head,
      element("p", "skill-description", localizedDescription(skill.description)),
      element("p", skill.issues.length ? "skill-issue" : "skill-valid", skill.issues.join("；") || "结构和工作流校验通过"),
    );
    body.appendChild(card);
  });
  if (!state.skills.length) body.appendChild(empty("未发现 Skill 资产"));
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
    const card = element("article", "operation-run-card");
    const main = element("div", "operation-run-main");
    const heading = element("div", "operation-run-heading");
    heading.append(
      riskBadge(run.risk_level),
      element("strong", "", run.degraded ? "安全降级回复" : `${riskLevelText(run.risk_level)}对话评估`),
    );
    main.append(
      heading,
      element("p", "muted-text", `${providerLabel(run.provider)} · 模型调用 ${run.model_calls} 次 · ${formatDate(run.created_at)}`),
    );
    const resultBlock = element("div", "operation-run-result");
    resultBlock.append(statusBadge(run.degraded ? "degraded" : run.status, {
      succeeded: "正常",
      completed: "正常",
      failed: "失败",
      degraded: "已降级",
    }), element("strong", "", `${run.total_latency_ms} 毫秒`));
    const button = element("button", "small-btn", "查看运行");
    button.type = "button";
    button.addEventListener("click", () => viewAgentRun(run));
    card.append(main, resultBlock, button);
    body.appendChild(card);
  });
  if (!result.items.length) body.appendChild(empty("暂无 Agent 运行记录"));
  renderPagination("#runPagination", "runs", result, renderAgentRuns);
}

function buildRiskComparison(decisions, run) {
  const byStage = (stage) => decisions.find((item) => item.stage === stage);
  const understanding = byStage("understanding");
  const hardRule = byStage("hard_rule");
  const finalDecision = byStage("run_final") || byStage("safety") || {
    risk_level: run.risk_level,
  };
  const section = element("section", "detail-section risk-comparison-section");
  section.appendChild(element("h3", "", "风险判断摘要"));
  const grid = element("div", "risk-comparison");
  [
    ["Understanding 初步判断", understanding],
    ["固定规则下限", hardRule],
    ["运行最终等级", finalDecision],
  ].forEach(([label, decision]) => {
    const card = element("article", "risk-decision-card");
    card.appendChild(element("span", "risk-decision-label", label));
    card.appendChild(riskBadge(decision?.risk_level));
    if (decision?.confidence !== null && decision?.confidence !== undefined) {
      card.appendChild(
        element("span", "muted-text", `置信度 ${Number(decision.confidence).toFixed(2)}`),
      );
    }
    if (decision?.reason) {
      card.appendChild(element("p", "", truncate(decision.reason, 110)));
    }
    grid.appendChild(card);
  });
  section.appendChild(grid);
  if (
    understanding?.risk_level &&
    finalDecision?.risk_level &&
    understanding.risk_level !== finalDecision.risk_level
  ) {
    section.appendChild(
      detailNotice(
        `等级发生变化：Understanding 初步判断为“${riskLevelText(understanding.risk_level)}”，运行最终采纳“${riskLevelText(finalDecision.risk_level)}”。`,
      ),
    );
  }
  return section;
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
    appendFact(facts, "风险等级", riskLevelText(run.risk_level));
    appendFact(facts, "模型服务", providerLabel(run.provider));
    appendFact(facts, "模型调用", `${run.model_calls} 次`);
    appendFact(facts, "总耗时", `${run.total_latency_ms} ms`);
    appendFact(facts, "协作事件", `${trace.collaboration_events.length} 条`);
    appendFact(facts, "任务数量", `${trace.tasks.length} 个`);
    content.appendChild(facts);
    if (trace.trace_notice) content.appendChild(detailNotice(trace.trace_notice));
    content.appendChild(buildRiskComparison(trace.risk_decisions || [], run));
    content.appendChild(detailSection("脱敏输入", trace.redacted_input || "—"));
    content.appendChild(detailSection("记忆摘要", trace.memoryBrief || "本轮没有历史摘要"));

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
    stepSection.appendChild(element("h3", "", "Agent 执行步骤"));
    const stepList = element("ol", "step-list");
    steps.forEach((step) => {
      const item = document.createElement("li");
      const head = element("div", "step-head");
      head.append(element("strong", "", `${step.sequence}. ${stepNameLabel(step.step_name)}`));
      head.appendChild(statusBadge(step.status, { succeeded: "成功", failed: "失败", skipped: "跳过" }));
      item.appendChild(head);
      item.appendChild(element("p", "", `${step.model_name || "规则引擎"} · ${step.latency_ms} 毫秒 · 输入/输出词元 ${step.input_tokens || 0}/${step.output_tokens || 0}`));
      if (step.summary) item.appendChild(element("p", "step-summary", step.summary));
      if (step.error_code) item.appendChild(element("p", "error-text", modelErrorText(step.error_code)));
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
  drawerReturnFocus =
    document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.querySelector("#drawerTitle").textContent = title;
  document.querySelector("#drawerEyebrow").textContent = eyebrow;
  drawerContent.replaceChildren(content);
  detailDrawer.classList.add("open");
  detailDrawer.setAttribute("aria-hidden", "false");
  document.body.classList.add("drawer-open");
  adminShell.inert = true;
  drawerPanel.focus();
}

function closeDrawer(restoreFocus = true) {
  if (!detailDrawer.classList.contains("open")) return;
  detailDrawer.classList.remove("open");
  detailDrawer.setAttribute("aria-hidden", "true");
  document.body.classList.remove("drawer-open");
  adminShell.inert = false;
  const returnTarget = drawerReturnFocus;
  drawerReturnFocus = null;
  if (restoreFocus && returnTarget?.isConnected) returnTarget.focus();
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
        institutional_id: loginForm.institutional_id.value.trim(),
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
document.querySelector("#riskFilter").addEventListener("change", () => {
  state.pages.reports = 1;
  renderReports();
});
document.querySelector("#reportDateFilter").addEventListener("change", () => {
  state.pages.reports = 1;
  renderReports();
});
document.querySelector("#reportSearch").addEventListener("input", () => {
  state.pages.reports = 1;
  renderReports();
});
document.querySelector("#exportReportsButton").addEventListener("click", () => {
  const params = new URLSearchParams();
  const risk = document.querySelector("#riskFilter").value;
  if (risk) params.set("risk_level", risk);
  downloadExport("reports", params);
});
document.querySelector("#caseStatusFilter").addEventListener("change", () => {
  state.pages.cases = 1;
  renderCases();
});
document.querySelectorAll("[data-case-status]").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelector("#caseStatusFilter").value = button.dataset.caseStatus;
    state.pages.cases = 1;
    renderCases();
  });
});
document.querySelector("#caseSearch").addEventListener("input", () => {
  state.pages.cases = 1;
  renderCases();
});
document.querySelector("#exportCasesButton").addEventListener("click", () => {
  const params = new URLSearchParams();
  const caseStatus = document.querySelector("#caseStatusFilter").value;
  if (caseStatus) params.set("case_status", caseStatus);
  downloadExport("cases", params);
});
document.querySelector("#alertStatusFilter").addEventListener("change", () => {
  state.pages.alerts = 1;
  renderAlerts();
});
document.querySelector("#alertSearch").addEventListener("input", () => {
  state.pages.alerts = 1;
  renderAlerts();
});
document.querySelectorAll("[data-job-status]").forEach((button) => {
  button.addEventListener("click", () => {
    state.jobStatus = button.dataset.jobStatus;
    state.pages.jobs = 1;
    renderJobs();
  });
});
document.querySelector("#auditSearchButton").addEventListener("click", () => loadAuditLogs(1));
document.querySelector("#auditResetButton").addEventListener("click", () => {
  document.querySelector("#auditActionFilter").value = "";
  document.querySelector("#auditResourceFilter").value = "";
  document.querySelector("#auditActorFilter").value = "";
  loadAuditLogs(1);
});
document.querySelector("#exportAuditButton").addEventListener("click", () => downloadExport("audit-logs", auditQuery(false)));
document.querySelector("#userRoleFilter").addEventListener("change", loadUsers);
document.querySelector("#userSearch").addEventListener("input", () => {
  state.pages.users = 1;
  renderUsers();
});
document.querySelector("#jobTypeFilter").addEventListener("change", (event) => {
  state.jobType = event.currentTarget.value;
  state.pages.jobs = 1;
  renderJobs();
});
document.querySelectorAll("[data-operation-tab]").forEach((button) => {
  button.addEventListener("click", () => switchOperationTab(button.dataset.operationTab));
});
document.querySelectorAll("[data-operation-nav]").forEach((button) => {
  button.addEventListener("click", () => switchOperationTab(button.dataset.operationNav));
});
document.querySelectorAll("[data-nav]").forEach((button) => {
  button.addEventListener("click", () => {
    if (button.dataset.risk) document.querySelector("#riskFilter").value = button.dataset.risk;
    if (button.dataset.nav === "operations") state.operationTab = "overview";
    navigate(button.dataset.nav);
  });
});
document.querySelector("#openMenuButton").addEventListener("click", openMobileMenu);
document.querySelector("#closeMenuButton").addEventListener("click", closeMobileMenu);
document.querySelector("#sidebarBackdrop").addEventListener("click", closeMobileMenu);
document.querySelector("#closeDrawerButton").addEventListener("click", closeDrawer);
document.querySelector("#drawerBackdrop").addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (!detailDrawer.classList.contains("open")) return;
  if (event.key === "Escape") {
    closeDrawer();
    return;
  }
  if (event.key !== "Tab") return;
  const focusable = [...drawerPanel.querySelectorAll(
    'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )].filter((item) => !item.classList.contains("hidden"));
  if (!focusable.length) {
    event.preventDefault();
    drawerPanel.focus();
    return;
  }
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (
    event.shiftKey &&
    (document.activeElement === first || document.activeElement === drawerPanel)
  ) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});

document.querySelector("#cancelNoteButton").addEventListener("click", () => noteDialog.close());
document.querySelector("#cancelOwnerButton").addEventListener("click", () => ownerDialog.close());
document.querySelector("#openAdminRegistrationButton").addEventListener("click", () => {
  document.querySelector("#adminRegistrationForm").reset();
  document.querySelector("#adminRegistrationError").textContent = "";
  adminRegistrationDialog.showModal();
});
document.querySelector("#cancelAdminRegistrationButton").addEventListener("click", () => adminRegistrationDialog.close());
document.querySelector("#ownerForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = event.currentTarget.querySelector("button[type='submit']");
  submit.disabled = true;
  document.querySelector("#ownerError").textContent = "";
  try {
    await api(`/api/v1/admin/cases/${state.ownerCaseId}/assign`, {
      method: "POST",
      body: JSON.stringify({ owner_id: document.querySelector("#caseOwner").value }),
    });
    ownerDialog.close();
    toast("个案负责人已更新");
    await loadCases();
  } catch (error) {
    document.querySelector("#ownerError").textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#adminRegistrationForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const errorElement = document.querySelector("#adminRegistrationError");
  errorElement.textContent = "";
  if (form.password.value !== form.password_confirm.value) {
    errorElement.textContent = "两次输入的密码不一致。";
    return;
  }
  const submit = form.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    await api("/api/v1/admin/users/admins", {
      method: "POST",
      body: JSON.stringify({
        institutional_id: form.institutional_id.value.trim(),
        name: form.elements.namedItem("name").value.trim(),
        password: form.password.value,
      }),
    });
    form.reset();
    adminRegistrationDialog.close();
    toast("管理员账号已创建");
    await loadUsers();
  } catch (error) {
    errorElement.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});
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
