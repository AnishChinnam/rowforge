"use strict";
const $ = (id) => document.getElementById(id);
const state = {
  user: null,
  jobs: [],
  active: null,
  mode: "register",
  uploadKey: null,
  busy: false,
  detailStamp: "",
  previewKind: "cleaned",
  previews: new Map(),
};
const fmt = new Intl.NumberFormat();
const escapeHTML = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const date = (v) =>
  v
    ? new Date(v * 1000).toLocaleString([], {
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
const badge = (status) =>
  `<span class="status ${escapeHTML(status)}">${status === "running" ? "Processing" : escapeHTML(status[0].toUpperCase() + status.slice(1))}</span>`;
async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.method && options.method !== "GET" && state.user)
    headers["X-CSRF-Token"] = state.user.csrf;
  if (options.body && !(options.body instanceof FormData))
    headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.includes("/auth/")) showAuth();
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `Request failed (${response.status}). Check your input and try again.`,
    );
  }
  return data;
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 4500);
}
function showAuth() {
  state.user = null;
  state.active = null;
  state.jobs = [];
  state.previews.clear();
  render();
  $("auth-view").hidden = false;
  $("workspace").hidden = true;
  for (const dialog of document.querySelectorAll("dialog")) dialog.close();
}
function showWorkspace(user) {
  state.user = user;
  $("auth-view").hidden = true;
  $("workspace").hidden = false;
  $("account-email").textContent = user.email;
  refresh();
}
function mode(value) {
  state.mode = value;
  $("register-tab").classList.toggle("active", value === "register");
  $("login-tab").classList.toggle("active", value === "login");
  $("auth-title").textContent =
    value === "register" ? "Welcome to RowForge" : "Good to see you again";
  $("auth-description").textContent =
    value === "register"
      ? "Create an account to start your first pipeline."
      : "Sign in to your data workspace.";
  $("auth-submit").textContent =
    value === "register" ? "Create workspace ↗" : "Sign in ↗";
  $("password").autocomplete =
    value === "register" ? "new-password" : "current-password";
  $("auth-error").textContent = "";
}
$("register-tab").onclick = () => mode("register");
$("login-tab").onclick = () => mode("login");
$("auth-form").onsubmit = async (event) => {
  event.preventDefault();
  $("auth-submit").disabled = true;
  $("auth-error").textContent = "";
  try {
    const user = await api("/api/auth/" + state.mode, {
      method: "POST",
      body: JSON.stringify({
        email: $("email").value,
        password: $("password").value,
      }),
    });
    $("password").value = "";
    showWorkspace(user);
  } catch (e) {
    $("auth-error").textContent = e.message;
  } finally {
    $("auth-submit").disabled = false;
  }
};
$("logout").onclick = async () => {
  try {
    await api("/api/logout", { method: "POST" });
    showAuth();
  } catch (e) {
    toast(e.message);
  }
};
function render() {
  $("nav-count").textContent = state.jobs.length;
  $("jobs-count").textContent = state.jobs.length;
  $("stat-jobs").textContent = state.jobs.length;
  $("stat-active").textContent = state.jobs.filter((j) =>
    ["queued", "running"].includes(j.status),
  ).length;
  $("stat-accepted").textContent = fmt.format(
    state.jobs
      .filter((j) => j.status === "completed")
      .reduce((s, j) => s + j.accepted, 0),
  );
  $("stat-failed").textContent = state.jobs.filter(
    (j) => j.status === "failed",
  ).length;
  const query = $("search").value.toLowerCase(),
    status = $("status-filter").value;
  const filtered = state.jobs.filter(
    (j) =>
      j.filename.toLowerCase().includes(query) &&
      (status === "all" || j.status === status),
  );
  $("empty").hidden = state.jobs.length > 0;
  $("no-matches").hidden = state.jobs.length === 0 || filtered.length > 0;
  $("jobs-body").innerHTML = filtered
    .map(
      (j) =>
        `<tr><td><div class="file-name"><span class="file-symbol">CSV</span><div><strong>${escapeHTML(j.filename)}</strong><small>${(j.input_bytes / 1024).toFixed(1)} KB · ${j.attempts} ${j.attempts === 1 ? "attempt" : "attempts"}</small></div></div></td><td>${badge(j.status)}</td><td>${fmt.format(j.processed)}<small class="muted"> ${j.status === "running" ? "read" : ""}</small></td><td class="muted">${date(j.created_at)}</td><td><button class="row-action" data-job="${j.id}" aria-label="Inspect ${escapeHTML(j.filename)}">↗</button></td></tr>`,
    )
    .join("");
  $("table-count").textContent =
    `${filtered.length} ${filtered.length === 1 ? "pipeline" : "pipelines"}`;
  for (const button of $("jobs-body").querySelectorAll("[data-job]"))
    button.onclick = () => openDetail(button.dataset.job);
}
$("search").oninput = render;
$("status-filter").onchange = render;
async function refresh() {
  if (!state.user || state.busy) return;
  state.busy = true;
  try {
    state.jobs = await api("/api/jobs");
    render();
    $("page-error").hidden = true;
    if (state.active && $("detail-dialog").open) await loadDetail();
  } catch (e) {
    $("page-error").textContent = e.message;
    $("page-error").hidden = false;
  } finally {
    state.busy = false;
  }
}
function newPipeline() {
  state.uploadKey = crypto.randomUUID();
  $("upload-form").reset();
  $("file-label").textContent = "Choose a CSV file";
  $("upload-error").textContent = "";
  $("upload-dialog").showModal();
}
for (const id of ["new-job", "new-nav", "empty-upload"])
  $(id).onclick = newPipeline;
$("overview-nav").onclick = () => {
  $("search").value = "";
  $("status-filter").value = "all";
  render();
};
$("close-upload").onclick = () => $("upload-dialog").close();
$("csv-file").onchange = () => {
  $("file-label").textContent =
    $("csv-file").files[0]?.name || "Choose a CSV file";
  state.uploadKey = crypto.randomUUID();
};
for (const id of [
  "column-mapping",
  "required-cols",
  "email-cols",
  "numeric-cols",
  "trim",
  "dedupe",
])
  $(id).addEventListener(
    "change",
    () => (state.uploadKey = crypto.randomUUID()),
  );
const columns = (id) =>
  $(id)
    .value.split(",")
    .map((s) => s.trim())
    .filter(Boolean);
function mappingRules() {
  const mapping = {};
  for (const line of $("column-mapping").value.split("\n").filter((v) => v.trim())) {
    const parts = line.split("=>").map((v) => v.trim());
    if (parts.length !== 2 || parts.some((v) => !v))
      throw new Error("Use one source => destination mapping per line.");
    if (Object.hasOwn(mapping, parts[0]))
      throw new Error("Each source column can only be mapped once.");
    Object.defineProperty(mapping, parts[0], {value: parts[1], enumerable: true});
  }
  return mapping;
}
async function submitFile(file, rules, key) {
  const body = new FormData();
  body.append("file", file);
  body.append("rules", JSON.stringify(rules));
  return api("/api/jobs", {
    method: "POST",
    headers: { "Idempotency-Key": key },
    body,
  });
}
$("upload-form").onsubmit = async (event) => {
  event.preventDefault();
  const file = $("csv-file").files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    $("upload-error").textContent = "Choose a file smaller than 10 MiB.";
    return;
  }
  $("upload-submit").disabled = true;
  $("upload-error").textContent = "";
  try {
    const job = await submitFile(
      file,
      {
        mapping: mappingRules(),
        required: columns("required-cols"),
        email: columns("email-cols"),
        numeric: columns("numeric-cols"),
        trim: $("trim").checked,
        dedupe: $("dedupe").checked,
      },
      state.uploadKey,
    );
    $("upload-dialog").close();
    toast("Pipeline queued. We’ll keep you updated.");
    await refresh();
    await openDetail(job.id);
  } catch (e) {
    $("upload-error").textContent = e.message;
  } finally {
    $("upload-submit").disabled = false;
  }
};
$("sample-job").onclick = async () => {
  $("sample-job").disabled = true;
  try {
    const response = await fetch("/api/sample");
    if (!response.ok) throw new Error("Sample unavailable.");
    const file = new File([await response.blob()], "sample-customers.csv", {
      type: "text/csv",
    });
    const job = await submitFile(
      file,
      {
        required: ["customer_id", "name", "email"],
        email: ["email"],
        numeric: ["amount"],
        trim: true,
        dedupe: true,
      },
      crypto.randomUUID(),
    );
    toast("Sample pipeline queued with 5,100 input rows.");
    await refresh();
    await openDetail(job.id);
  } catch (e) {
    toast(e.message);
  } finally {
    $("sample-job").disabled = false;
  }
};
async function openDetail(id) {
  state.active = id;
  state.detailStamp = "";
  state.previewKind = "cleaned";
  $("detail-name").textContent = "Loading…";
  $("detail-content").textContent = "";
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  try {
    await loadDetail();
  } catch (e) {
    $("detail-content").textContent = e.message;
  }
}
async function loadDetail() {
  const j = await api("/api/jobs/" + state.active);
  if (j.id !== state.active) return;
  const stamp = JSON.stringify([j.id, j.updated_at, j.status, j.events.length]);
  if (stamp === state.detailStamp) return;
  state.detailStamp = stamp;
  $("detail-name").textContent = j.filename;
  const mappingText = Object.entries(j.rules.mapping || {}).map(([a, b]) => `${a} → ${b}`).join(" · ");
  const ruleText =
    [
      ["Required", j.rules.required],
      ["Email", j.rules.email],
      ["Numeric", j.rules.numeric],
    ]
      .filter(([, v]) => v.length)
      .map(([k, v]) => `${k}: ${v.join(", ")}`)
      .join(" · ") || "No column validation rules";
  $("detail-content").innerHTML =
    `<div class="detail-status">${badge(j.status)}<span class="small muted">${j.attempts} ${j.attempts === 1 ? "attempt" : "attempts"} · ${date(j.created_at)}</span></div><progress value="${j.progress}" max="100" aria-label="Approximate file processing progress"></progress><p class="detail-caption">${j.status === "running" ? "Processing · byte-based progress is approximate" : j.status === "queued" ? "Waiting for a worker. Keep the worker process running." : j.status === "completed" ? "Complete. Every input row has been accounted for." : j.status === "failed" ? "Review the message below. Correct the input if needed." : "Processing stopped. No partial result is published."}</p><div class="detail-grid">${[
      ["Read", j.processed],
      ["Accepted", j.accepted],
      ["Rejected", j.rejected],
      ["Duplicates", j.duplicates],
    ]
      .map(
        ([k, v]) =>
          `<div><strong>${fmt.format(v)}</strong><span>${k}</span></div>`,
      )
      .join(
        "",
      )}</div>${j.error ? `<p class="error banner">${escapeHTML(j.error)}</p>` : ""}<div class="detail-rules">${mappingText ? `Mapping: ${escapeHTML(mappingText)}<br>` : ""}${escapeHTML(ruleText)}<br>${j.rules.trim ? "Trim whitespace" : "Keep whitespace"} · ${j.rules.dedupe ? "Remove identical valid rows" : "Keep duplicate rows"}</div><div class="detail-actions">${j.status === "completed" ? `<a class="primary" href="/api/jobs/${j.id}/download/cleaned">Cleaned CSV ↓</a><a class="secondary" href="/api/jobs/${j.id}/download/errors">Error report ↓</a>` : ""}${["queued", "running"].includes(j.status) ? '<button class="secondary" id="cancel-job">Cancel pipeline</button>' : ""}${["failed", "cancelled"].includes(j.status) ? '<button class="primary" id="retry-job">Retry pipeline ↗</button>' : ""}</div>${j.status === "completed" ? '<section class="preview-section"><div class="preview-heading"><h3>Result preview</h3><div><button id="preview-cleaned" class="preview-tab" type="button">Cleaned rows</button><button id="preview-errors" class="preview-tab" type="button">Rejected rows</button></div></div><div id="result-preview" class="table-scroll"></div><p id="preview-caption" class="detail-caption"></p></section>' : ""}<h3>Activity trail</h3><ul class="timeline">${j.events.map((e) => `<li><div><p>${escapeHTML(e.message)}</p><small>${date(e.created_at)}</small></div></li>`).join("")}</ul><p class="detail-id">Job ${j.id}</p>`;
  if (j.status === "completed") {
    for (const kind of ["cleaned", "errors"])
      $("preview-" + kind).onclick = () => loadPreview(j.id, kind);
    await loadPreview(j.id, state.previewKind);
  }
  for (const action of ["cancel", "retry"]) {
    const button = $(action + "-job");
    if (button)
      button.onclick = async () => {
        button.disabled = true;
        try {
          await api(`/api/jobs/${j.id}/${action}`, { method: "POST" });
          toast(
            action === "cancel" ? "Pipeline cancelled." : "Pipeline requeued.",
          );
          await refresh();
        } catch (e) {
          toast(e.message);
          button.disabled = false;
        }
      };
  }
}
$("close-detail").onclick = () => {
  $("detail-dialog").close();
  state.active = null;
};
$("detail-dialog").addEventListener("close", () => (state.active = null));
setInterval(() => {
  if (!document.hidden) refresh();
}, 2000);
api("/api/me")
  .then(showWorkspace)
  .catch(() => showAuth());

async function loadPreview(id, kind) {
  state.previewKind = kind;
  for (const value of ["cleaned", "errors"]) {
    const button = $("preview-" + value);
    if (button) {
      button.classList.toggle("active", value === kind);
      button.setAttribute("aria-pressed", String(value === kind));
    }
  }
  const area = $("result-preview");
  if (!area) return;
  area.textContent = "Loading preview…";
  try {
    const key = id + ":" + kind;
    if (!state.previews.has(key))
      state.previews.set(key, await api(`/api/jobs/${id}/preview/${kind}`));
    if (
      id !== state.active ||
      state.previewKind !== kind ||
      !$("result-preview")
    )
      return;
    const data = state.previews.get(key);
    $("result-preview").innerHTML = data.rows.length
      ? `<table><thead><tr>${data.columns.map((v) => `<th>${escapeHTML(v)}</th>`).join("")}</tr></thead><tbody>${data.rows.map((row) => `<tr>${row.map((v) => `<td>${escapeHTML(v)}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      : '<p class="detail-caption">No rows in this report.</p>';
    $("preview-caption").textContent =
      `${data.more_rows ? "First 10 rows" : data.rows.length + " rows"} shown${data.more_columns ? " · first 12 columns" : ""}. Long cells are shortened in this preview; download the full CSV for all values.`;
  } catch (e) {
    if ($("result-preview")) $("result-preview").textContent = e.message;
  }
}
