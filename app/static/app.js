const els = {
  botStatus: document.querySelector("#botStatus"),
  botUser: document.querySelector("#botUser"),
  downloadCount: document.querySelector("#downloadCount"),
  fileCount: document.querySelector("#fileCount"),
  downloadsBody: document.querySelector("#downloadsBody"),
  filesBody: document.querySelector("#filesBody"),
  logsBody: document.querySelector("#logsBody"),
  form: document.querySelector("#settingsForm"),
  loginOverlay: document.querySelector("#loginOverlay"),
  loginForm: document.querySelector("#loginForm"),
  loginPassword: document.querySelector("#loginPassword"),
  loginMessage: document.querySelector("#loginMessage"),
  bootstrapHint: document.querySelector("#bootstrapHint"),
  formMessage: document.querySelector("#formMessage"),
  startBtn: document.querySelector("#startBtn"),
  restartBtn: document.querySelector("#restartBtn"),
  stopBtn: document.querySelector("#stopBtn"),
  resumeBtn: document.querySelector("#resumeBtn"),
  limitOffBtn: document.querySelector("#limitOffBtn"),
  retryAllBtn: document.querySelector("#retryAllBtn"),
  cleanupBtn: document.querySelector("#cleanupBtn"),
  logoutBtn: document.querySelector("#logoutBtn"),
  apiId: document.querySelector("#apiId"),
  apiHash: document.querySelector("#apiHash"),
  botToken: document.querySelector("#botToken"),
  allowedUserIds: document.querySelector("#allowedUserIds"),
  adminUserIds: document.querySelector("#adminUserIds"),
  adminPassword: document.querySelector("#adminPassword"),
  downloadDir: document.querySelector("#downloadDir"),
  imageDownloadDir: document.querySelector("#imageDownloadDir"),
  videoDownloadDir: document.querySelector("#videoDownloadDir"),
  fileDownloadDir: document.querySelector("#fileDownloadDir"),
  sessionDir: document.querySelector("#sessionDir"),
  sessionName: document.querySelector("#sessionName"),
  progressInterval: document.querySelector("#progressInterval"),
  progressStep: document.querySelector("#progressStep"),
  maxAutoRetries: document.querySelector("#maxAutoRetries"),
  queueMaxsize: document.querySelector("#queueMaxsize"),
  historyFlushInterval: document.querySelector("#historyFlushInterval")
};

let firstLoad = true;
let authenticated = false;

function formatBytes(value) {
  if (!Number(value)) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = Number(value);
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return index === 0 ? `${Math.round(size)} B` : `${size.toFixed(1)} ${units[index]}`;
}

function formatDuration(value) {
  if (value === null || value === undefined) return "-";
  const total = Math.max(0, Math.round(Number(value)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) return `${hours}小时${minutes}分钟`;
  if (minutes) return `${minutes}分${seconds}秒`;
  return `${seconds}秒`;
}

function formatTime(value) {
  if (!value) return "-";
  if (typeof value === "number") return new Date(value * 1000).toLocaleString();
  return new Date(value).toLocaleString();
}

function timeMarkup(value) {
  if (!value) return '<span class="muted">-</span>';
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  if (Number.isNaN(date.getTime())) return '<span class="muted">-</span>';
  return `<time class="timestamp" datetime="${escapeHtml(date.toISOString())}"><span>${escapeHtml(date.toLocaleDateString())}</span><span>${escapeHtml(date.toLocaleTimeString([], { hour12: false }))}</span></time>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function errorDetail(data, fallback) {
  if (typeof data?.detail === "string") return data.detail;
  if (Array.isArray(data?.detail)) return data.detail.map((item) => item.msg).join("；");
  return fallback;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (response.status === 401) {
    authenticated = false;
    els.loginOverlay.classList.remove("hidden");
  }
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(errorDetail(data, response.statusText));
  }
  return response.json();
}

async function postJson(url, payload = {}) {
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
}

function statusText(value) {
  return {
    queued: "排队中",
    downloading: "下载中",
    paused: "已暂停",
    retrying: "重试中",
    verifying: "校验中",
    complete: "完成",
    failed: "失败",
    interrupted: "已中断",
    cancelled: "已取消"
  }[value] || value;
}

function categoryText(value) {
  return { images: "图片", videos: "视频", files: "其他" }[value] || value;
}

function previewMarkup(item) {
  const name = item.name || item.file_name || "文件";
  const icon = item.category === "videos" ? "🎞" : item.category === "images" ? "🖼" : "📄";
  const image = item.preview_url
    ? `<img class="preview-image" src="${escapeHtml(item.preview_url)}" alt="${escapeHtml(name)} 的预览图" loading="lazy" />`
    : "";
  const content = `<span class="preview-fallback" aria-hidden="true">${icon}</span>${image}`;
  if (item.url) {
    return `<a class="preview-frame" href="${escapeHtml(item.url)}" title="下载 ${escapeHtml(name)}">${content}</a>`;
  }
  return `<span class="preview-frame">${content}</span>`;
}

function enablePreviewFallbacks(container) {
  container.querySelectorAll("img.preview-image").forEach((image) => {
    image.addEventListener("error", () => image.classList.add("hidden"), { once: true });
  });
}

async function checkAuth() {
  const state = await requestJson("/api/auth/status");
  authenticated = state.authenticated;
  els.loginOverlay.classList.toggle("hidden", authenticated);
  els.bootstrapHint.classList.toggle("hidden", !state.bootstrap_required);
  return state;
}

function fillSettings(settings) {
  if (!firstLoad) return;
  els.apiId.value = settings.api_id || "";
  els.allowedUserIds.value = (settings.allowed_user_ids || []).join(", ");
  els.adminUserIds.value = (settings.admin_user_ids || []).join(", ");
  els.downloadDir.value = settings.download_dir || "/downloads";
  els.imageDownloadDir.value = settings.image_download_dir || "/downloads/images";
  els.videoDownloadDir.value = settings.video_download_dir || "/downloads/videos";
  els.fileDownloadDir.value = settings.file_download_dir || "/downloads/files";
  els.sessionDir.value = settings.session_dir || "/sessions";
  els.sessionName.value = settings.session_name || "media_downloader_bot";
  els.progressInterval.value = settings.progress_interval_seconds || 3;
  els.progressStep.value = settings.progress_percent_step || 5;
  els.maxAutoRetries.value = settings.max_auto_retries ?? 3;
  els.queueMaxsize.value = settings.queue_maxsize || 100;
  els.historyFlushInterval.value = settings.history_flush_interval_seconds || 2;
  if (settings.api_hash_set) els.apiHash.placeholder = "已保存，留空表示不修改";
  if (settings.bot_token_set) els.botToken.placeholder = "已保存，留空表示不修改";
}

function renderDownloads(downloads) {
  const activeStatuses = new Set(["queued", "downloading", "paused", "retrying", "verifying"]);
  const retryableStatuses = new Set(["failed", "interrupted", "cancelled"]);
  els.downloadsBody.innerHTML = downloads.map((item) => {
    const progress = Math.max(0, Math.min(100, Number(item.progress) || 0));
    const sizeText = item.status === "complete"
      ? formatBytes(item.size_bytes || item.total_bytes || item.downloaded_bytes)
      : item.total_bytes
        ? `${formatBytes(item.downloaded_bytes)} / ${formatBytes(item.total_bytes)}`
        : formatBytes(item.size_bytes || item.downloaded_bytes);
    const speedParts = [];
    if (item.speed_bytes_per_second) speedParts.push(`${formatBytes(item.speed_bytes_per_second)}/s`);
    if (item.speed_limit_bytes_per_second) speedParts.push(`限速 ${formatBytes(item.speed_limit_bytes_per_second)}/s`);
    if (item.eta_seconds !== null && activeStatuses.has(item.status)) speedParts.push(`剩余 ${formatDuration(item.eta_seconds)}`);
    if (item.retry_count) speedParts.push(`重试 ${item.retry_count}/${item.max_retries}`);
    let actions = "";
    if (activeStatuses.has(item.status)) {
      actions = `<button class="small danger" data-action="cancel" data-id="${escapeHtml(item.id)}">取消</button>`;
    } else if (retryableStatuses.has(item.status)) {
      actions = `<button class="small" data-action="retry" data-id="${escapeHtml(item.id)}">重试</button>`;
    }
    return `
      <tr>
        <td class="preview-cell">${previewMarkup(item)}</td>
        <td class="status-cell"><span class="badge ${escapeHtml(item.status)}">${escapeHtml(statusText(item.status))}</span></td>
        <td class="file-cell"><div class="file-name" title="${escapeHtml(item.file_name)}">${escapeHtml(item.file_name)}</div><div class="subline">任务 ${escapeHtml(item.id.slice(0, 8))}${item.error ? ` · ${escapeHtml(item.error)}` : ""}</div></td>
        <td class="progress-cell"><div class="progress"><div style="width:${progress}%"></div></div><div class="progress-label">${progress}%</div></td>
        <td class="transfer"><div>${sizeText}</div><div class="subline">${escapeHtml(speedParts.join(" · ") || "-")}</div></td>
        <td>${timeMarkup(item.updated_at)}</td>
        <td class="cell-actions">${actions}</td>
      </tr>`;
  }).join("") || `<tr><td colspan="7" class="muted">暂无下载记录</td></tr>`;
  enablePreviewFallbacks(els.downloadsBody);
}

function renderFiles(files) {
  els.filesBody.innerHTML = files.map((item) => `
    <tr>
      <td class="preview-cell">${previewMarkup(item)}</td>
      <td class="file-cell"><div class="file-name" title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</div></td>
      <td><span class="badge">${escapeHtml(categoryText(item.category))}</span></td>
      <td>${formatBytes(item.size_bytes)}</td>
      <td>${timeMarkup(item.modified_at)}</td>
      <td class="cell-actions"><a href="${escapeHtml(item.url)}">下载</a></td>
    </tr>`).join("") || `<tr><td colspan="6" class="muted">暂无文件</td></tr>`;
  enablePreviewFallbacks(els.filesBody);
}

function renderLogs(logs) {
  els.logsBody.innerHTML = logs.map((item) => `
    <div class="log-row">
      <div class="muted">${formatTime(item.time)}</div>
      <div><span class="badge">${escapeHtml(item.level)}</span></div>
      <div class="log-message">${escapeHtml(item.message)}</div>
    </div>`).join("") || `<div class="muted">暂无日志</div>`;
}

async function refresh() {
  if (!authenticated) return;
  try {
    const state = await requestJson("/api/state");
    const running = state.bot.running;
    const controls = state.bot.controls || {};
    const queueText = `队列 ${state.bot.queue_size || 0}/${state.bot.queue_maxsize || 0}`;
    const limitText = controls.speed_limit_bytes_per_second ? ` · 限速 ${controls.speed_limit_text}` : "";
    const pauseText = controls.paused ? " · 已暂停" : "";
    els.botStatus.innerHTML = running
      ? `<span class="badge running">运行中</span><div class="subline">${queueText}${limitText}${pauseText}</div>`
      : `<span class="badge">已停止</span>`;
    els.botUser.textContent = state.bot.username ? `@${state.bot.username}` : "-";
    els.downloadCount.textContent = state.downloads.length;
    els.fileCount.textContent = state.files.length;
    els.startBtn.disabled = running || !state.settings.ready;
    els.restartBtn.disabled = !state.settings.ready;
    els.stopBtn.disabled = !running;
    els.resumeBtn.disabled = !controls.paused;
    els.limitOffBtn.disabled = !controls.speed_limit_bytes_per_second;
    els.retryAllBtn.disabled = !running || !state.downloads.some((item) => ["failed", "interrupted", "cancelled"].includes(item.status));
    fillSettings(state.settings);
    renderDownloads(state.downloads);
    renderFiles(state.files);
    if (state.bot.last_error) els.formMessage.textContent = state.bot.last_error;
    firstLoad = false;
  } catch (error) {
    if (authenticated) els.formMessage.textContent = error.message;
  }
}

async function refreshLogs() {
  if (!authenticated) return;
  try {
    const data = await requestJson("/api/logs");
    renderLogs(data.logs || []);
  } catch (_) {
    // Authentication handling is centralized in requestJson.
  }
}

els.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.loginMessage.textContent = "正在登录...";
  try {
    await postJson("/api/auth/login", { password: els.loginPassword.value });
    els.loginPassword.value = "";
    els.loginMessage.textContent = "";
    await checkAuth();
    await refresh();
    await refreshLogs();
  } catch (error) {
    els.loginMessage.textContent = error.message;
    els.loginMessage.classList.add("error");
  }
});

els.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  els.formMessage.textContent = "正在验证并保存...";
  const newAdminPassword = els.adminPassword.value;
  const payload = {
    api_id: els.apiId.value.trim() || null,
    api_hash: els.apiHash.value.trim(),
    bot_token: els.botToken.value.trim(),
    allowed_user_ids: els.allowedUserIds.value.trim(),
    admin_user_ids: els.adminUserIds.value.trim(),
    admin_password: newAdminPassword,
    download_dir: els.downloadDir.value.trim() || "/downloads",
    image_download_dir: els.imageDownloadDir.value.trim() || "/downloads/images",
    video_download_dir: els.videoDownloadDir.value.trim() || "/downloads/videos",
    file_download_dir: els.fileDownloadDir.value.trim() || "/downloads/files",
    session_dir: els.sessionDir.value.trim() || "/sessions",
    session_name: els.sessionName.value.trim() || "media_downloader_bot",
    progress_interval_seconds: Number(els.progressInterval.value || 3),
    progress_percent_step: Number(els.progressStep.value || 5),
    max_auto_retries: Number(els.maxAutoRetries.value ?? 3),
    queue_maxsize: Number(els.queueMaxsize.value || 100),
    history_flush_interval_seconds: Number(els.historyFlushInterval.value || 2)
  };
  try {
    await postJson("/api/settings", payload);
    if (newAdminPassword) await postJson("/api/auth/login", { password: newAdminPassword });
    els.apiHash.value = "";
    els.botToken.value = "";
    els.adminPassword.value = "";
    firstLoad = true;
    els.formMessage.textContent = "已保存并验证";
    await refresh();
  } catch (error) {
    els.formMessage.textContent = error.message;
  }
});

for (const [button, url, pending, done] of [
  [els.startBtn, "/api/bot/start", "正在启动...", "已启动"],
  [els.restartBtn, "/api/bot/restart", "正在重启...", "已重启"],
  [els.stopBtn, "/api/bot/stop", "正在停止...", "已停止"],
  [els.resumeBtn, "/api/controls/resume", "正在恢复...", "下载已恢复"]
]) {
  button.addEventListener("click", async () => {
    els.formMessage.textContent = pending;
    try {
      await postJson(url);
      els.formMessage.textContent = done;
      await refresh();
    } catch (error) {
      els.formMessage.textContent = error.message;
    }
  });
}

els.limitOffBtn.addEventListener("click", async () => {
  try {
    await postJson("/api/controls/limit", { megabytes_per_second: null });
    els.formMessage.textContent = "已取消限速";
    await refresh();
  } catch (error) {
    els.formMessage.textContent = error.message;
  }
});

els.retryAllBtn.addEventListener("click", async () => {
  els.retryAllBtn.disabled = true;
  try {
    const result = await postJson("/api/downloads/retry-failed");
    const remaining = result.remaining ? `，仍有 ${result.remaining} 条未入队` : "";
    els.formMessage.textContent = `已将 ${result.queued}/${result.total} 条失败任务重新加入队列${remaining}`;
    await refresh();
  } catch (error) {
    els.formMessage.textContent = error.message;
  } finally {
    els.retryAllBtn.disabled = false;
  }
});

els.cleanupBtn.addEventListener("click", async () => {
  try {
    const result = await postJson("/api/downloads/cleanup");
    els.formMessage.textContent = `已清理 ${result.removed} 条失败/中断记录`;
    await refresh();
  } catch (error) {
    els.formMessage.textContent = error.message;
  }
});

els.downloadsBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  try {
    await postJson(`/api/downloads/${encodeURIComponent(button.dataset.id)}/${button.dataset.action}`);
    await refresh();
  } catch (error) {
    els.formMessage.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

els.logoutBtn.addEventListener("click", async () => {
  await postJson("/api/auth/logout");
  authenticated = false;
  firstLoad = true;
  els.loginOverlay.classList.remove("hidden");
});

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
    tab.classList.add("active");
    const target = tab.dataset.tab;
    document.querySelector("#downloadsTab").classList.toggle("hidden", target !== "downloads");
    document.querySelector("#filesTab").classList.toggle("hidden", target !== "files");
    document.querySelector("#logsTab").classList.toggle("hidden", target !== "logs");
    if (target === "logs") refreshLogs();
  });
});

checkAuth().then(async () => {
  await refresh();
  await refreshLogs();
}).catch((error) => {
  els.loginMessage.textContent = error.message;
});
setInterval(refresh, 3000);
setInterval(refreshLogs, 5000);
