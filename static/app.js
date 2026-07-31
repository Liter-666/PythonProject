// 缓存页面中会被频繁访问的 DOM 元素，避免每次操作都重新查询。
const messages = document.querySelector("#messages");
const composer = document.querySelector("#composer");
const input = document.querySelector("#message-input");
const sendButton = document.querySelector("#send-button");
const userIdInput = document.querySelector("#user-id");
const threadIdInput = document.querySelector("#thread-id");
const connection = document.querySelector("#connection");
const modelCount = document.querySelector("#model-count");
const messageCount = document.querySelector("#message-count");
const sessionStart = document.querySelector("#session-start");

// busy 用于阻止模型响应期间重复提交消息。
let busy = false;

// 浏览器只记住当前选择的身份和会话；真正的消息与状态仍保存在 PostgreSQL。
const USER_ID_STORAGE_KEY = "agent-memory.user-id";
const THREAD_ID_STORAGE_KEY = "agent-memory.thread-id";

// 为新会话生成独立的 thread_id；旧浏览器没有 randomUUID 时使用时间戳后备。
function createThreadId() {
  const id = crypto.randomUUID ? crypto.randomUUID().slice(0, 8) : Date.now().toString(36);
  return `chat-${id}`;
}

// 页面刷新时恢复上次选择；只有浏览器从未保存过 thread_id 时才创建新会话。
userIdInput.value = localStorage.getItem(USER_ID_STORAGE_KEY) || userIdInput.value;
threadIdInput.value = localStorage.getItem(THREAD_ID_STORAGE_KEY) || createThreadId();
localStorage.setItem(USER_ID_STORAGE_KEY, userIdInput.value);
localStorage.setItem(THREAD_ID_STORAGE_KEY, threadIdInput.value);

// 转义模型或工具返回的 HTML 特殊字符，防止内容被浏览器当成标签执行。
function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// 新消息加入后把聊天区域滚动到底部。
function scrollToBottom() {
  messages.scrollTop = messages.scrollHeight;
}

// 创建用户或助手的普通聊天气泡，并返回对应 DOM 节点。
function addMessage(kind, content) {
  const node = document.createElement("div");
  node.className = `message ${kind}`;
  // 所有动态内容先经过 escapeHtml，降低 XSS 风险。
  node.innerHTML = `
    <div class="avatar">${kind === "user" ? "YOU" : "AI"}</div>
    <div class="bubble">${escapeHtml(content)}</div>
  `;
  messages.appendChild(node);
  scrollToBottom();
  return node;
}

// 把 tool_call/tool_result 事件渲染成可展开的 details 卡片。
function addToolEvent(event) {
  const node = document.createElement("details");
  node.className = "tool-event";
  const isCall = event.kind === "tool_call";
  // 调用事件展示参数，结果事件展示工具返回文本。
  const payload = isCall ? event.args : event.content;
  node.innerHTML = `
    <summary><span class="tool-badge">${isCall ? "CALL" : "RESULT"}</span>${escapeHtml(event.name || "tool")}</summary>
    <pre>${escapeHtml(typeof payload === "string" ? payload : JSON.stringify(payload, null, 2))}</pre>
  `;
  messages.appendChild(node);
  scrollToBottom();
}

// 模型处理期间显示三个跳动圆点，并返回节点供请求结束时删除。
function addTyping() {
  const node = document.createElement("div");
  node.className = "message assistant typing";
  node.innerHTML = `<div class="avatar">AI</div><div class="bubble"><i></i><i></i><i></i></div>`;
  messages.appendChild(node);
  scrollToBottom();
  return node;
}

// 使用后端返回的状态摘要刷新左侧 CustomState 面板。
function updateState(state = {}) {
  modelCount.textContent = state.model_call_count ?? 0;
  messageCount.textContent = state.message_count ?? 0;
  sessionStart.textContent = state.session_start
    ? new Date(state.session_start).toLocaleString("zh-CN")
    : "尚未开始";
}

// 统一控制输入框和发送按钮的启用状态。
function setBusy(value) {
  busy = value;
  sendButton.disabled = value;
  input.disabled = value;
}

// 向 FastAPI 发送一轮聊天请求，并按事件类型渲染后端响应。
async function sendMessage(text) {
  // 去掉首尾空格；空消息或正在请求时直接退出。
  const message = text.trim();
  if (!message || busy) return;

  // 浏览器先立即显示用户消息，再异步请求后端。
  addMessage("user", message);
  input.value = "";
  input.style.height = "auto";
  setBusy(true);
  const typing = addTyping();

  try {
    // thread_id 控制短期 checkpoint，user_id 控制跨会话的长期偏好 namespace。
    const response = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        thread_id: threadIdInput.value.trim(),
        user_id: userIdInput.value.trim(),
      }),
    });
    const data = await response.json();
    typing.remove();

    // 非 2xx 响应转换成异常，由 catch 显示在聊天区。
    if (!response.ok) throw new Error(data.detail || "请求失败");

    // assistant 渲染成气泡，工具调用和结果渲染成详情卡片。
    for (const event of data.events) {
      if (event.kind === "assistant") addMessage("assistant", event.content);
      else addToolEvent(event);
    }
    updateState(data.state);
  } catch (error) {
    // 网络错误或后端错误也作为一条助手消息展示，避免页面无反馈。
    typing.remove();
    addMessage("assistant", `请求失败：${error.message}`);
  } finally {
    setBusy(false);
    input.focus();
  }
}

// 提交表单时阻止浏览器刷新页面，改为执行异步聊天请求。
composer.addEventListener("submit", (event) => {
  event.preventDefault();
  sendMessage(input.value);
});

// Enter 发送；Shift + Enter 保留为输入框换行。
input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});

// 根据内容自动增高输入框，但最大不超过 150px。
input.addEventListener("input", () => {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 150)}px`;
});

// 快捷按钮通过 data-prompt 属性复用同一个 sendMessage 函数。
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => sendMessage(button.dataset.prompt));
});

// 新建 thread 只清空短期对话；相同 user_id 的 Store 长期偏好仍然存在。
document.querySelector("#new-thread").addEventListener("click", () => {
  threadIdInput.value = createThreadId();
  localStorage.setItem(THREAD_ID_STORAGE_KEY, threadIdInput.value);
  messages.innerHTML = "";
  updateState();
  addMessage("assistant", "新会话已经建立。短期状态已清空，但当前用户的长期偏好仍可从 Store 读取。");
});

// 从后端读取当前用户拥有的会话状态；user_id 同时参与服务端归属校验。
async function loadThreadState() {
  try {
    const threadId = threadIdInput.value.trim();
    const userId = userIdInput.value.trim();
    if (!threadId || !userId) {
      updateState();
      return;
    }

    const query = new URLSearchParams({ thread_id: threadId, user_id: userId });
    const response = await fetch(`/api/state?${query.toString()}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "读取会话状态失败");
    updateState(data);
  } catch (_) {
    updateState();
  }
}

// 用户手动修改标识后更新浏览器选择，并尝试恢复对应的服务端状态。
threadIdInput.addEventListener("change", () => {
  localStorage.setItem(THREAD_ID_STORAGE_KEY, threadIdInput.value.trim());
  loadThreadState();
});

userIdInput.addEventListener("change", () => {
  localStorage.setItem(USER_ID_STORAGE_KEY, userIdInput.value.trim());
  loadThreadState();
});

// 页面加载完成后检查后端，并显示当前实际使用的 Embedding 后端。
fetch("/api/health")
  .then(async (response) => {
    if (!response.ok) throw new Error();
    const data = await response.json();
    connection.classList.add("online");
    connection.innerHTML = "<span></span>服务在线";
    document.querySelector("#embedding-label").textContent = `${data.embedding} · 1024维`;
  })
  .catch(() => {
    connection.classList.add("error");
    connection.innerHTML = "<span></span>服务离线";
  });

// 健康检查与状态恢复互不依赖，后端可用时会显示上次会话的状态摘要。
loadThreadState();
