const state = {
  token: sessionStorage.getItem("mindnook_student_token"),
  user: null,
  conversations: [],
  activeConversationId: null,
  sending: false,
  abortController: null,
};

const loginView = document.querySelector("#loginView");
const appView = document.querySelector("#appView");
const loginForm = document.querySelector("#loginForm");
const loginError = document.querySelector("#loginError");
const registerForm = document.querySelector("#registerForm");
const registerError = document.querySelector("#registerError");
const conversationList = document.querySelector("#conversationList");
const conversationTitle = document.querySelector("#conversationTitle");
const messages = document.querySelector("#messages");
const welcome = document.querySelector("#welcome");
const composer = document.querySelector("#composer");
const messageInput = document.querySelector("#messageInput");
const sendButton = document.querySelector("#sendButton");
const stopButton = document.querySelector("#stopButton");
const composerStatus = document.querySelector("#composerStatus");
const historyToggleButton = document.querySelector("#historyToggleButton");
const conversationBackdrop = document.querySelector("#conversationBackdrop");
const privacyDialog = document.querySelector("#privacyDialog");

const defaultComposerStatus =
  "Enter 发送 · Shift+Enter 换行 · 回答不构成诊断或治疗建议。";

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) logout();
  return response;
}

function showApp() {
  loginView.classList.add("hidden");
  appView.classList.remove("hidden");
  document.querySelector("#userName").textContent = state.user.name;
}

function logout() {
  state.abortController?.abort();
  sessionStorage.removeItem("mindnook_student_token");
  state.token = null;
  state.user = null;
  state.activeConversationId = null;
  closeConversationHistory();
  appView.classList.add("hidden");
  loginView.classList.remove("hidden");
}

function addMessage(sender, content) {
  welcome?.classList.add("hidden");
  const row = document.createElement("div");
  row.className = `message ${sender}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;
  row.appendChild(bubble);
  messages.appendChild(row);
  messages.scrollTop = messages.scrollHeight;
  return bubble;
}

function renderConversations() {
  conversationList.replaceChildren();
  if (!state.conversations.length) {
    const empty = document.createElement("p");
    empty.className = "conversation-empty";
    empty.textContent = "还没有历史对话，发送第一条消息后会自动保存。";
    conversationList.appendChild(empty);
    return;
  }
  for (const item of state.conversations) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "conversation-item";
    if (item.id === state.activeConversationId) button.classList.add("active");
    button.textContent = item.title;
    button.addEventListener("click", () => selectConversation(item.id));
    conversationList.appendChild(button);
  }
}

async function loadConversations() {
  const response = await api("/api/v1/conversations");
  if (!response.ok) return;
  state.conversations = await response.json();
  if (!state.activeConversationId && state.conversations.length) {
    state.activeConversationId = state.conversations[0].id;
  }
  renderConversations();
  if (state.activeConversationId) await loadMessages();
}

async function createConversation() {
  const response = await api("/api/v1/conversations", { method: "POST" });
  if (!response.ok) throw new Error("暂时无法创建对话，请稍后重试。");
  const conversation = await response.json();
  state.conversations.unshift(conversation);
  state.activeConversationId = conversation.id;
  conversationTitle.textContent = conversation.title;
  renderConversations();
  return conversation;
}

function startNewConversation() {
  if (state.sending) return;
  state.activeConversationId = null;
  conversationTitle.textContent = "新的对话";
  messages.querySelectorAll(".message").forEach((node) => node.remove());
  welcome?.classList.remove("hidden");
  composerStatus.textContent = defaultComposerStatus;
  renderConversations();
  closeConversationHistory();
  messageInput.focus();
}

async function selectConversation(id) {
  if (state.sending) return;
  state.activeConversationId = id;
  renderConversations();
  await loadMessages();
  closeConversationHistory();
}

function openConversationHistory() {
  conversationList.classList.add("mobile-open");
  conversationBackdrop.classList.remove("hidden");
  historyToggleButton.setAttribute("aria-expanded", "true");
  document.body.classList.add("conversation-open");
}

function closeConversationHistory() {
  conversationList.classList.remove("mobile-open");
  conversationBackdrop.classList.add("hidden");
  historyToggleButton.setAttribute("aria-expanded", "false");
  document.body.classList.remove("conversation-open");
}

async function loadMessages() {
  const response = await api(
    `/api/v1/conversations/${state.activeConversationId}/messages`,
  );
  if (!response.ok) return;
  const items = await response.json();
  messages.querySelectorAll(".message").forEach((node) => node.remove());
  welcome?.classList.toggle("hidden", items.length > 0);
  for (const item of items) addMessage(item.sender, item.content);
  const current = state.conversations.find(
    (item) => item.id === state.activeConversationId,
  );
  conversationTitle.textContent = current?.title || "新的对话";
}

function parseSseBlock(block) {
  let event = "message";
  const dataLines = [];
  for (const line of block.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
  }
  if (!dataLines.length) return null;
  return { event, data: JSON.parse(dataLines.join("\n")) };
}

const streamStatusMessages = {
  reasoning: "正在理解你的话…",
  generating: "正在生成回复…",
  checking: "正在进行安全检查…",
};

async function sendMessage(content) {
  state.sending = true;
  state.abortController = new AbortController();
  sendButton.disabled = true;
  messageInput.disabled = true;
  sendButton.classList.add("hidden");
  stopButton.classList.remove("hidden");
  messages.setAttribute("aria-busy", "true");
  composerStatus.textContent = "正在理解你的话…";
  let assistantBubble = null;

  try {
    if (!state.activeConversationId) await createConversation();
    addMessage("student", content);
    assistantBubble = addMessage("assistant", "");
    assistantBubble.classList.add("streaming");
    const response = await api(
      `/api/v1/conversations/${state.activeConversationId}/messages:stream`,
      {
        method: "POST",
        body: JSON.stringify({
          content,
          client_request_id: crypto.randomUUID(),
        }),
        signal: state.abortController.signal,
      },
    );
    if (!response.ok || !response.body) {
      throw new Error("暂时无法获得回复，请稍后重试。");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        const item = parseSseBlock(block);
        if (!item) continue;
        if (item.event === "status") {
          composerStatus.textContent =
            streamStatusMessages[item.data.stage] || "正在处理你的消息…";
        }
        if (item.event === "delta") {
          assistantBubble.classList.remove("streaming");
          assistantBubble.textContent += item.data.text;
          messages.scrollTop = messages.scrollHeight;
        }
        if (item.event === "error") {
          await reader.cancel();
          const streamError = new Error(
            item.data.message || "暂时无法获得回复，请稍后重试。",
          );
          streamError.code = item.data.code;
          streamError.retryable = Boolean(item.data.retryable);
          throw streamError;
        }
        if (item.event === "completed") {
          composerStatus.textContent = defaultComposerStatus;
        }
      }
      if (done) break;
    }
    await loadConversations();
  } catch (error) {
    if (assistantBubble) assistantBubble.classList.remove("streaming");
    if (error.name === "AbortError") {
      if (assistantBubble && !assistantBubble.textContent) {
        assistantBubble.textContent = "已停止生成。";
      }
      composerStatus.textContent = "已停止，你可以继续输入。";
    } else {
      if (assistantBubble) {
        assistantBubble.textContent = error.message || "请求失败，请稍后重试。";
      }
      composerStatus.textContent = "请求失败，你可以重新发送。";
    }
  } finally {
    state.sending = false;
    state.abortController = null;
    sendButton.disabled = false;
    messageInput.disabled = false;
    sendButton.classList.remove("hidden");
    stopButton.classList.add("hidden");
    messages.removeAttribute("aria-busy");
    messageInput.focus();
  }
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
    if (payload.user.role !== "student") throw new Error("该账号不能登录学生端。");
    state.token = payload.access_token;
    state.user = payload.user;
    sessionStorage.setItem("mindnook_student_token", state.token);
    showApp();
    await loadConversations();
    if (!state.conversations.length) startNewConversation();
  } catch (error) {
    loginError.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

document.querySelector("#showRegisterButton").addEventListener("click", () => {
  loginForm.classList.add("hidden");
  registerForm.classList.remove("hidden");
  registerError.textContent = "";
  registerForm.institutional_id.focus();
});

document.querySelector("#showLoginButton").addEventListener("click", () => {
  registerForm.classList.add("hidden");
  loginForm.classList.remove("hidden");
  loginError.textContent = "";
  loginForm.institutional_id.focus();
});

registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  registerError.textContent = "";
  if (registerForm.password.value !== registerForm.password_confirm.value) {
    registerError.textContent = "两次输入的密码不一致。";
    return;
  }
  const submit = registerForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    const response = await fetch("/api/v1/auth/register/student", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        institutional_id: registerForm.institutional_id.value.trim(),
        name: registerForm.elements.namedItem("name").value.trim(),
        password: registerForm.password.value,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "注册失败。");
    state.token = payload.access_token;
    state.user = payload.user;
    sessionStorage.setItem("mindnook_student_token", state.token);
    registerForm.reset();
    showApp();
    await loadConversations();
    if (!state.conversations.length) startNewConversation();
  } catch (error) {
    registerError.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

composer.addEventListener("submit", async (event) => {
  event.preventDefault();
  const content = messageInput.value.trim();
  if (!content || state.sending) return;
  messageInput.value = "";
  resizeMessageInput();
  await sendMessage(content);
});

function resizeMessageInput() {
  messageInput.style.height = "auto";
  messageInput.style.height = `${Math.min(messageInput.scrollHeight, 144)}px`;
}

messageInput.addEventListener("input", resizeMessageInput);

messageInput.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

document.querySelectorAll(".chip").forEach((chip) => {
  chip.addEventListener("click", () => {
    messageInput.value = chip.textContent;
    resizeMessageInput();
    messageInput.focus();
  });
});

document.querySelector("#newChatButton").addEventListener("click", startNewConversation);
document.querySelector("#logoutButton").addEventListener("click", logout);
historyToggleButton.addEventListener("click", () => {
  if (conversationList.classList.contains("mobile-open")) closeConversationHistory();
  else openConversationHistory();
});
conversationBackdrop.addEventListener("click", closeConversationHistory);
stopButton.addEventListener("click", () => state.abortController?.abort());
document.querySelectorAll("[data-privacy-info]").forEach((button) => {
  button.addEventListener("click", () => privacyDialog.showModal());
});
window.addEventListener("resize", () => {
  if (window.innerWidth > 900) closeConversationHistory();
});

async function restoreSession() {
  if (!state.token) return;
  const response = await api("/api/v1/me");
  if (!response.ok) return;
  const user = await response.json();
  if (user.role !== "student") return logout();
  state.user = user;
  showApp();
  await loadConversations();
  if (!state.conversations.length) startNewConversation();
}

restoreSession();
