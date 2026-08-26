"use strict";

(() => {
  const root = document.querySelector("[data-page]");
  if (!root) return;

  const state = {
    csrfToken: null,
    pollTimer: null,
    polling: false,
    pods: [],
    activeSession: null,
  };

  class ApiError extends Error {
    constructor(payload, status, requestId) {
      super(payload?.message || `HTTP ${status}`);
      this.name = "ApiError";
      this.code = payload?.code || "HTTP_ERROR";
      this.context = payload?.context || {};
      this.retryable = Boolean(payload?.retryable);
      this.status = status;
      this.requestId = requestId || "";
    }
  }

  const text = (selector, value) => {
    const target = document.querySelector(selector);
    if (target) target.textContent = value ?? "";
  };

  const clear = (target) => {
    if (target) target.replaceChildren();
  };

  const element = (tag, options = {}) => {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = String(options.text);
    if (options.href) node.setAttribute("href", options.href);
    if (options.type) node.setAttribute("type", options.type);
    return node;
  };

  const appendTextPair = (list, label, value) => {
    list.append(element("dt", { text: label }), element("dd", { text: value ?? "—" }));
  };

  const statusLabel = (value) => {
    const labels = {
      not_started: "未开始",
      active: "进行中",
      completed: "已完成",
      provisioning: "创建中",
      ready: "就绪",
      in_progress: "排障中",
      passed: "已通过",
      resetting: "重置中",
      cleaning: "清理中",
      error: "异常",
      failed: "未通过",
    };
    return labels[value] || value || "未知";
  };

  const statusTone = (value) => {
    if (["completed", "passed", "ready"].includes(value)) return "success";
    if (["active", "in_progress", "provisioning", "resetting", "cleaning"].includes(value)) {
      return "info";
    }
    if (["error", "failed"].includes(value)) return "danger";
    if (value === "not_started") return "neutral";
    return "warning";
  };

  const badge = (value) => {
    return element("span", {
      className: `status-badge ${statusTone(value)}`,
      text: statusLabel(value),
    });
  };

  const parseResponse = async (response) => {
    const requestId = response.headers.get("X-Request-ID") || "";
    const token = response.headers.get("X-CSRF-Token");
    if (token) state.csrfToken = token;
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) throw new ApiError(payload, response.status, requestId);
    return payload;
  };

  const refreshCsrf = async () => {
    const response = await fetch("/health", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    await parseResponse(response);
    if (!state.csrfToken) throw new ApiError({ code: "CSRF_TOKEN_MISSING", message: "无法获取安全令牌。" }, 500, "");
  };

  const api = async (path, options = {}, retryCsrf = true) => {
    const method = (options.method || "GET").toUpperCase();
    const write = !["GET", "HEAD", "OPTIONS"].includes(method);
    if (write && !state.csrfToken) await refreshCsrf();
    const headers = new Headers(options.headers || {});
    headers.set("Accept", "application/json");
    if (options.body !== undefined) headers.set("Content-Type", "application/json");
    if (write && state.csrfToken) headers.set("X-CSRF-Token", state.csrfToken);
    try {
      const response = await fetch(path, {
        ...options,
        method,
        headers,
        credentials: "same-origin",
      });
      return await parseResponse(response);
    } catch (error) {
      if (write && retryCsrf && error instanceof ApiError && error.code === "CSRF_TOKEN_INVALID") {
        state.csrfToken = null;
        await refreshCsrf();
        return api(path, options, false);
      }
      throw error;
    }
  };

  const formatError = (error) => {
    if (!(error instanceof ApiError)) return "KubeLab 无法完成当前操作。";
    const retry = error.retryable ? " 可以稍后重试。" : "";
    const requestId = error.requestId ? `\n请求 ID：${error.requestId}` : "";
    return `${error.message}（${error.code}）${retry}${requestId}`;
  };

  const showPageError = (error) => {
    const target = document.querySelector("#page-error");
    if (!target) return;
    target.textContent = formatError(error);
    target.classList.remove("hidden");
  };

  const clearPageError = () => {
    const target = document.querySelector("#page-error");
    if (!target) return;
    target.textContent = "";
    target.classList.add("hidden");
  };

  const toast = (message) => {
    const region = document.querySelector("#toast-region");
    if (!region) return;
    const item = element("div", { className: "toast", text: message });
    region.append(item);
    window.setTimeout(() => item.remove(), 4200);
  };

  const withBusy = async (button, busyText, action) => {
    if (!button || button.dataset.busy === "true") return null;
    const original = button.textContent;
    button.dataset.busy = "true";
    button.disabled = true;
    button.textContent = busyText;
    try {
      return await action();
    } finally {
      button.dataset.busy = "false";
      button.disabled = false;
      button.textContent = original;
    }
  };

  const renderLabCard = (lab) => {
    const card = element("a", { className: "lab-card", href: `/labs/${encodeURIComponent(lab.id)}` });
    card.append(badge(lab.progress));
    card.append(element("h3", { text: lab.name }));
    card.append(element("p", { text: lab.description }));
    const meta = element("div", { className: "card-meta" });
    meta.append(element("span", { text: `${lab.category} · ${lab.difficulty}` }));
    meta.append(element("span", { text: `${lab.duration_minutes} 分钟` }));
    card.append(meta);
    return card;
  };

  const loadDashboard = async () => {
    const [environmentResult, labsResult, activeResult] = await Promise.allSettled([
      api("/api/v1/environment"),
      api("/api/v1/labs"),
      api("/api/v1/sessions/active"),
    ]);

    if (environmentResult.status === "fulfilled") {
      const environment = environmentResult.value;
      const list = document.querySelector("#environment-details");
      clear(list);
      appendTextPair(list, "支持边界", environment.supported_runtime);
      appendTextPair(list, "当前平台", environment.process_platform);
      appendTextPair(list, "WSL 发行版", environment.wsl_distribution || "未识别");
      appendTextPair(list, "监听地址", `${environment.bind_host}:${environment.port}`);
      const status = document.querySelector("#environment-status");
      status.textContent = "本地运行";
      status.className = "status-badge success";
    } else {
      showPageError(environmentResult.reason);
    }

    if (labsResult.status === "fulfilled") {
      const labs = labsResult.value.labs;
      const completed = labs.filter((lab) => lab.progress === "completed").length;
      const active = labs.filter((lab) => lab.progress === "active").length;
      text("#metric-total", labs.length);
      text("#metric-completed", completed);
      text("#metric-active", active);
      text("#metric-rate", labs.length ? `${Math.round((completed / labs.length) * 100)}%` : "0%");
      const recommendations = document.querySelector("#recommended-labs");
      clear(recommendations);
      const candidates = labs.filter((lab) => lab.progress !== "completed").slice(0, 3);
      if (!candidates.length) {
        recommendations.append(element("p", { className: "quiet-text", text: "所有实验均已完成。" }));
      } else {
        candidates.forEach((lab) => recommendations.append(renderLabCard(lab)));
      }
    } else {
      showPageError(labsResult.reason);
    }

    const activeTarget = document.querySelector("#active-session");
    clear(activeTarget);
    activeTarget.className = "active-session-card";
    if (activeResult.status === "fulfilled") {
      const session = activeResult.value.session;
      activeTarget.append(badge(session.status));
      activeTarget.append(element("h3", { text: session.lab_id }));
      activeTarget.append(element("code", { text: session.namespace }));
      activeTarget.append(
        element("a", {
          className: "button primary",
          href: `/sessions/${encodeURIComponent(session.id)}`,
          text: "进入排障工作台",
        }),
      );
    } else if (activeResult.reason instanceof ApiError && activeResult.reason.status === 404) {
      activeTarget.className = "empty-state";
      activeTarget.append(element("p", { text: "当前没有活动实验。可以从目录选择一个场景开始。" }));
    } else {
      showPageError(activeResult.reason);
      activeTarget.className = "empty-state";
      activeTarget.append(element("p", { text: "无法读取活动 Session。" }));
    }
  };

  const loadLabs = async () => {
    const payload = await api("/api/v1/labs");
    const labs = payload.labs;
    const categoryFilter = document.querySelector("#category-filter");
    const progressFilter = document.querySelector("#progress-filter");
    [...new Set(labs.map((lab) => lab.category))].sort().forEach((category) => {
      const option = element("option", { text: category });
      option.value = category;
      categoryFilter.append(option);
    });
    const render = () => {
      const filtered = labs.filter((lab) => {
        return (!categoryFilter.value || lab.category === categoryFilter.value) &&
          (!progressFilter.value || lab.progress === progressFilter.value);
      });
      const grid = document.querySelector("#labs-grid");
      clear(grid);
      text("#labs-count", `${filtered.length} 个实验`);
      if (!filtered.length) {
        grid.append(element("p", { className: "empty-state", text: "没有符合筛选条件的实验。" }));
      } else {
        filtered.forEach((lab) => grid.append(renderLabCard(lab)));
      }
    };
    categoryFilter.addEventListener("change", render);
    progressFilter.addEventListener("change", render);
    render();
  };

  const loadLabDetail = async () => {
    const labId = root.dataset.labId;
    const detail = await api(`/api/v1/labs/${encodeURIComponent(labId)}`);
    text("#lab-category", detail.lab.category.toUpperCase());
    text("#lab-name", detail.lab.name);
    text("#lab-description", detail.lab.description);
    text("#lab-task", detail.task);
    text("#lab-completion", detail.completion_description);
    const progress = document.querySelector("#lab-progress");
    progress.textContent = statusLabel(detail.lab.progress);
    progress.className = `status-badge ${statusTone(detail.lab.progress)}`;

    const facts = document.querySelector("#lab-facts");
    appendTextPair(facts, "Namespace", detail.namespace);
    appendTextPair(facts, "难度", detail.lab.difficulty);
    appendTextPair(facts, "预计用时", `${detail.lab.duration_minutes} 分钟`);
    appendTextPair(facts, "Kubernetes", detail.kubernetes_requirement);
    appendTextPair(facts, "最低资源", `${detail.minimum_cpu} CPU / ${detail.minimum_memory_mib} MiB`);
    appendTextPair(facts, "提示层级", String(detail.hint_count));

    const tags = document.querySelector("#lab-tags");
    detail.lab.tags.forEach((value) => tags.append(element("span", { className: "tag", text: value })));
    const questions = document.querySelector("#interview-questions");
    detail.interview_questions.forEach((value) => questions.append(element("li", { text: value })));

    const startButton = document.querySelector("#start-lab");
    let activeSession = null;
    try {
      const active = await api("/api/v1/sessions/active");
      activeSession = active.session;
      if (activeSession.lab_id === labId) {
        startButton.textContent = "进入活动实验";
      } else {
        startButton.textContent = "请先完成当前实验";
        startButton.disabled = true;
      }
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error;
    }
    startButton.addEventListener("click", async () => {
      clearPageError();
      if (activeSession?.lab_id === labId) {
        window.location.assign(`/sessions/${encodeURIComponent(activeSession.id)}`);
        return;
      }
      try {
        const started = await withBusy(startButton, "正在创建环境…", () =>
          api(`/api/v1/labs/${encodeURIComponent(labId)}/start`, { method: "POST" }),
        );
        if (started) window.location.assign(`/sessions/${encodeURIComponent(started.id)}`);
      } catch (error) {
        showPageError(error);
      }
    });
  };

  const updateSessionIdentity = (session) => {
    state.activeSession = session;
    text("#session-lab-id", session.lab_id);
    text("#session-namespace", session.namespace);
    const status = document.querySelector("#session-status");
    status.textContent = statusLabel(session.status);
    status.className = `status-badge ${statusTone(session.status)}`;
  };

  const renderRows = (target, rows, columns, emptyMessage) => {
    clear(target);
    if (!rows.length) {
      const row = element("tr");
      const cell = element("td", { text: emptyMessage });
      cell.colSpan = columns.length;
      row.append(cell);
      target.append(row);
      return;
    }
    rows.forEach((item) => {
      const row = element("tr");
      columns.forEach((column) => row.append(element("td", { text: column(item) })));
      target.append(row);
    });
  };

  const updateLogSelectors = (pods) => {
    state.pods = pods;
    const podSelect = document.querySelector("#log-pod");
    const current = podSelect.value;
    clear(podSelect);
    const placeholder = element("option", { text: "选择 Pod" });
    placeholder.value = "";
    podSelect.append(placeholder);
    pods.forEach((pod) => {
      const option = element("option", { text: pod.name });
      option.value = pod.name;
      podSelect.append(option);
    });
    if (pods.some((pod) => pod.name === current)) podSelect.value = current;
    updateContainerOptions();
  };

  const updateContainerOptions = () => {
    const podName = document.querySelector("#log-pod").value;
    const containerSelect = document.querySelector("#log-container");
    const current = containerSelect.value;
    clear(containerSelect);
    const automatic = element("option", { text: "自动选择" });
    automatic.value = "";
    containerSelect.append(automatic);
    const pod = state.pods.find((item) => item.name === podName);
    (pod?.containers || []).forEach((container) => {
      const option = element("option", { text: container.name });
      option.value = container.name;
      containerSelect.append(option);
    });
    if ([...containerSelect.options].some((option) => option.value === current)) {
      containerSelect.value = current;
    }
  };

  const pollResources = async () => {
    if (state.polling || document.hidden || !state.activeSession) return;
    if (["completed"].includes(state.activeSession.status)) return;
    state.polling = true;
    text("#poll-status", "正在刷新…");
    try {
      const payload = await api("/api/v1/sessions/active/resources");
      updateSessionIdentity(payload.session);
      renderRows(
        document.querySelector("#resources-body"),
        payload.resources,
        [(item) => item.kind, (item) => item.name, (item) => item.status || "—"],
        "当前 Namespace 中没有可展示资源。",
      );
      renderRows(
        document.querySelector("#pods-body"),
        payload.pods,
        [
          (item) => item.name,
          (item) => item.phase || "—",
          (item) => (item.ready ? "是" : "否"),
          (item) => String(item.restart_count),
          (item) => item.containers.map((container) => container.name).join(", ") || "—",
        ],
        "当前 Namespace 中没有 Pod。",
      );
      updateLogSelectors(payload.pods);
      text("#poll-status", `已更新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
      clearPageError();
    } catch (error) {
      text("#poll-status", "刷新失败");
      showPageError(error);
    } finally {
      state.polling = false;
    }
  };

  const startPolling = () => {
    stopPolling();
    if (!document.hidden) {
      pollResources();
      state.pollTimer = window.setInterval(pollResources, 2000);
    }
  };

  const stopPolling = () => {
    if (state.pollTimer !== null) window.clearInterval(state.pollTimer);
    state.pollTimer = null;
  };

  const loadEvents = async (button) => {
    const payload = await withBusy(button, "刷新中…", () => api("/api/v1/sessions/active/events"));
    if (!payload) return;
    const target = document.querySelector("#events-list");
    clear(target);
    target.className = "stack-list";
    if (!payload.events.length) {
      target.className = "empty-state";
      target.append(element("p", { text: "当前没有 Events。" }));
      return;
    }
    payload.events.forEach((event) => {
      const item = element("article", { className: "stack-item" });
      const header = element("header");
      header.append(element("strong", { text: event.reason || "Event" }));
      header.append(badge(event.type === "Warning" ? "failed" : "active"));
      item.append(header);
      item.append(element("p", { text: `${event.involved_kind || "资源"}/${event.involved_name || "—"}\n${event.message || ""}` }));
      target.append(item);
    });
  };

  const loadLogs = async (button) => {
    const pod = document.querySelector("#log-pod").value;
    if (!pod) {
      toast("请先选择 Pod。");
      document.querySelector("#log-pod").focus();
      return;
    }
    const params = new URLSearchParams({
      pod,
      previous: String(document.querySelector("#log-previous").checked),
      tail: document.querySelector("#log-tail").value,
    });
    const container = document.querySelector("#log-container").value;
    if (container) params.set("container", container);
    const payload = await withBusy(button, "读取中…", () => api(`/api/v1/sessions/active/logs?${params}`));
    if (!payload) return;
    text("#log-output", payload.content || "日志为空。");
    if (payload.truncated) toast("日志已按安全上限截断。 ");
  };

  const renderVerification = (payload) => {
    const target = document.querySelector("#verification-results");
    clear(target);
    target.className = "stack-list";
    const summary = element("article", { className: "stack-item" });
    const header = element("header");
    header.append(element("strong", { text: "验证结果" }), badge(payload.status));
    summary.append(header);
    summary.append(element("p", { text: `耗时 ${payload.duration_ms} ms` }));
    target.append(summary);
    payload.results.forEach((result) => {
      const item = element("article", { className: "stack-item" });
      const itemHeader = element("header");
      itemHeader.append(element("strong", { text: result.check_id }), badge(result.status));
      item.append(itemHeader, element("p", { text: result.message }));
      target.append(item);
    });
  };

  const renderHint = (payload) => {
    const target = document.querySelector("#hint-result");
    clear(target);
    target.className = "hint-card";
    target.append(element("h4", { text: `提示 ${payload.level}/${payload.total_levels}` }));
    target.append(element("p", { text: payload.content }));
  };

  const fillRetrospective = (payload) => {
    const form = document.querySelector("#retrospective-form");
    const value = payload.retrospective || {};
    [...form.elements].forEach((field) => {
      if (field.name && Object.hasOwn(value, field.name)) field.value = value[field.name] || "";
    });
  };

  const openConfirmation = (operation) => {
    const dialog = document.querySelector("#confirmation-dialog");
    const namespace = state.activeSession.namespace;
    dialog.dataset.operation = operation;
    text("#confirmation-title", operation === "reset" ? "确认重置实验" : "确认清理环境");
    text(
      "#confirmation-copy",
      operation === "reset"
        ? `将删除并重建 ${namespace}，恢复初始故障。`
        : `将安全删除实验 Namespace ${namespace}。`,
    );
    const input = document.querySelector("#namespace-confirmation");
    input.value = "";
    document.querySelector("#confirm-destructive").disabled = true;
    dialog.showModal();
    input.focus();
  };

  const initializeSessionActions = () => {
    document.querySelector("#log-pod").addEventListener("change", updateContainerOptions);
    document.querySelector("#refresh-events").addEventListener("click", async (event) => {
      try { await loadEvents(event.currentTarget); } catch (error) { showPageError(error); }
    });
    document.querySelector("#refresh-logs").addEventListener("click", async (event) => {
      try { await loadLogs(event.currentTarget); } catch (error) { showPageError(error); }
    });
    document.querySelector("#run-verify").addEventListener("click", async (event) => {
      try {
        const payload = await withBusy(event.currentTarget, "验证中…", () => api("/api/v1/sessions/active/verify", { method: "POST" }));
        if (payload) renderVerification(payload);
        await pollResources();
      } catch (error) { showPageError(error); }
    });
    document.querySelector("#request-hint").addEventListener("click", async (event) => {
      try {
        const payload = await withBusy(event.currentTarget, "读取中…", () => api("/api/v1/sessions/active/hint", { method: "POST" }));
        if (payload) renderHint(payload);
        await pollResources();
      } catch (error) { showPageError(error); }
    });
    document.querySelector("#retrospective-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.currentTarget.querySelector("button[type='submit']");
      const payload = Object.fromEntries(new FormData(event.currentTarget));
      try {
        await withBusy(button, "保存中…", () => api("/api/v1/sessions/latest/retrospective", { method: "PUT", body: JSON.stringify(payload) }));
        text("#retrospective-status", "已保存");
        toast("复盘已保存。");
      } catch (error) { showPageError(error); }
    });
    document.querySelector("#reset-session").addEventListener("click", () => openConfirmation("reset"));
    document.querySelector("#cleanup-session").addEventListener("click", () => openConfirmation("cleanup"));

    const input = document.querySelector("#namespace-confirmation");
    input.addEventListener("input", () => {
      document.querySelector("#confirm-destructive").disabled = input.value !== state.activeSession.namespace;
    });
    document.querySelector("#confirmation-dialog").addEventListener("close", async (event) => {
      if (event.currentTarget.returnValue !== "confirm") return;
      const operation = event.currentTarget.dataset.operation;
      const namespace = input.value;
      const trigger = document.querySelector(operation === "reset" ? "#reset-session" : "#cleanup-session");
      try {
        const payload = await withBusy(trigger, operation === "reset" ? "重置中…" : "清理中…", () =>
          api(`/api/v1/sessions/active/${operation}`, { method: "POST", body: JSON.stringify({ namespace }) }),
        );
        if (!payload) return;
        if (operation === "cleanup") {
          stopPolling();
          toast("环境已安全清理。");
          window.setTimeout(() => window.location.assign("/"), 500);
        } else {
          updateSessionIdentity(payload);
          toast("实验已恢复到初始故障。");
          startPolling();
        }
      } catch (error) { showPageError(error); }
    });
  };

  const loadSession = async () => {
    const routeSessionId = root.dataset.sessionId;
    const active = await api("/api/v1/sessions/active");
    if (active.session.id !== routeSessionId) {
      throw new ApiError(
        { code: "SESSION_ID_MISMATCH", message: "当前活动 Session 与页面地址不一致。" },
        409,
        "",
      );
    }
    updateSessionIdentity(active.session);
    const [detail, retrospective] = await Promise.all([
      api(`/api/v1/labs/${encodeURIComponent(active.session.lab_id)}`),
      api("/api/v1/sessions/latest/retrospective"),
    ]);
    text("#session-title", detail.lab.name);
    text("#session-task", detail.task);
    fillRetrospective(retrospective);
    initializeSessionActions();
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        stopPolling();
        text("#poll-status", "页面不可见，已暂停");
      } else {
        startPolling();
      }
    });
    window.addEventListener("beforeunload", stopPolling);
    startPolling();
  };

  const loadProgress = async () => {
    const payload = await api("/api/v1/labs");
    const labs = payload.labs;
    const completed = labs.filter((lab) => lab.progress === "completed");
    const active = labs.filter((lab) => lab.progress === "active");
    text("#progress-total", labs.length);
    text("#progress-completed", completed.length);
    text("#progress-active", active.length);
    text("#progress-rate", labs.length ? `${Math.round((completed.length / labs.length) * 100)}%` : "0%");

    const grouped = new Map();
    labs.forEach((lab) => {
      const value = grouped.get(lab.category) || { total: 0, completed: 0 };
      value.total += 1;
      if (lab.progress === "completed") value.completed += 1;
      grouped.set(lab.category, value);
    });
    const categories = document.querySelector("#category-progress");
    clear(categories);
    [...grouped.entries()].sort().forEach(([category, value]) => {
      const row = element("article", { className: "progress-row" });
      const header = element("header");
      header.append(element("strong", { text: category }), element("span", { text: `${value.completed}/${value.total}` }));
      const progress = element("progress", { className: "progress-track" });
      progress.setAttribute("aria-label", `${category} 完成进度`);
      progress.max = value.total;
      progress.value = value.completed;
      row.append(header, progress);
      categories.append(row);
    });

    const completedTarget = document.querySelector("#completed-labs");
    clear(completedTarget);
    if (!completed.length) {
      completedTarget.className = "empty-state";
      completedTarget.append(element("p", { text: "还没有完成记录。完成第一个实验后会显示在这里。" }));
    } else {
      completed.forEach((lab) => {
        const link = element("a", { className: "stack-item", href: `/labs/${encodeURIComponent(lab.id)}` });
        link.append(element("strong", { text: lab.name }), element("p", { text: `${lab.category} · ${lab.difficulty}` }));
        completedTarget.append(link);
      });
    }
  };

  const boot = async () => {
    try {
      await refreshCsrf();
      const loaders = {
        dashboard: loadDashboard,
        labs: loadLabs,
        "lab-detail": loadLabDetail,
        session: loadSession,
        progress: loadProgress,
      };
      const loader = loaders[root.dataset.page];
      if (loader) await loader();
    } catch (error) {
      showPageError(error);
    }
  };

  boot();
})();
