const params = new URLSearchParams(window.location.search);
const teacherKey = params.get("key") || "";
const students = new Map();
const selectedFiles = new Map();
const followEnabled = new Map();
let activeUsername = null;
let eventSource = null;

const rosterElement = document.querySelector("#roster");
const tabsElement = document.querySelector("#student-tabs");
const panelElement = document.querySelector("#student-panel");
const emptyElement = document.querySelector("#empty-state");
const connectionElement = document.querySelector("#connection-state");

function relativeTime(value) {
  if (!value) return "No file changes yet";
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - Number(value)));
  if (seconds < 5) return "just now";
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function statusFor(student) {
  const age = Date.now() - Date.parse(student.last_heartbeat || 0);
  if (!Number.isFinite(age) || age > 45_000) return "offline";
  if (Date.now() / 1000 - Number(student.last_change_epoch || 0) > 120) return "idle";
  return "online";
}

function makeStatus(status) {
  const indicator = document.createElement("span");
  indicator.className = `status-dot ${status}`;
  indicator.setAttribute("aria-label", status);
  return indicator;
}

function sortedStudents() {
  return [...students.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }));
}

function renderOverview() {
  const list = sortedStudents();
  document.querySelector("#student-count").textContent = String(list.length).padStart(2, "0");
  emptyElement.hidden = list.length > 0;
  rosterElement.replaceChildren();
  for (const student of list) {
    const button = document.createElement("button");
    button.className = `roster-item${student.username === activeUsername ? " selected" : ""}`;
    button.type = "button";
    button.addEventListener("click", () => selectStudent(student.username));

    const identity = document.createElement("span");
    identity.className = "roster-identity";
    identity.append(makeStatus(statusFor(student)));
    const name = document.createElement("span");
    name.className = "roster-name";
    name.textContent = student.name;
    identity.append(name);

    const activity = document.createElement("span");
    activity.className = "roster-activity";
    activity.textContent = relativeTime(student.last_change_epoch);
    button.append(identity, activity);
    rosterElement.append(button);
  }
}

function renderTabs() {
  tabsElement.replaceChildren();
  for (const student of sortedStudents()) {
    const tab = document.createElement("button");
    tab.type = "button";
    tab.role = "tab";
    tab.setAttribute("aria-selected", String(student.username === activeUsername));
    tab.className = `student-tab${student.username === activeUsername ? " active" : ""}`;
    tab.append(makeStatus(statusFor(student)));
    const name = document.createElement("span");
    name.textContent = student.name;
    tab.append(name);
    tab.addEventListener("click", () => selectStudent(student.username));
    tabsElement.append(tab);
  }
}

function languageFor(path) {
  const extension = path.split(".").pop().toLowerCase();
  const languages = {
    py: "python", pyw: "python", js: "javascript", mjs: "javascript", cjs: "javascript",
    jsx: "jsx", ts: "typescript", tsx: "tsx", html: "xml", htm: "xml", xml: "xml",
    css: "css", scss: "scss", json: "json", jsonc: "json", md: "markdown", yml: "yaml",
    yaml: "yaml", toml: "toml", sh: "bash", bash: "bash", sql: "sql", java: "java",
    c: "c", h: "c", cpp: "cpp", hpp: "cpp", cs: "csharp", go: "go", rs: "rust",
    rb: "ruby", php: "php", swift: "swift", kt: "kotlin", graphql: "graphql", gql: "graphql",
  };
  return languages[extension] || "plaintext";
}

function buildTree(files, selectedPath) {
  const root = { folders: new Map(), entries: [] };
  for (const [path, file] of Object.entries(files)) {
    const parts = path.split("/");
    let branch = root;
    for (const folder of parts.slice(0, -1)) {
      if (!branch.folders.has(folder)) branch.folders.set(folder, { folders: new Map(), entries: [] });
      branch = branch.folders.get(folder);
    }
    branch.entries.push({ name: parts.at(-1), path, file });
  }

  function renderBranch(branch, parent, depth) {
    for (const [name, child] of [...branch.folders].sort(([a], [b]) => a.localeCompare(b))) {
      const folder = document.createElement("div");
      folder.className = "tree-folder";
      folder.style.setProperty("--depth", depth);
      const label = document.createElement("div");
      label.className = "tree-folder-label";
      label.innerHTML = '<span class="folder-chevron" aria-hidden="true">⌄</span><span class="folder-icon" aria-hidden="true"></span>';
      const folderName = document.createElement("span");
      folderName.textContent = name;
      label.append(folderName);
      folder.append(label);
      renderBranch(child, folder, depth + 1);
      parent.append(folder);
    }
    for (const entry of branch.entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `tree-file${entry.path === selectedPath ? " selected" : ""}`;
      button.style.setProperty("--depth", depth);
      button.title = entry.path;
      const icon = document.createElement("span");
      icon.className = `file-icon ${languageFor(entry.path)}`;
      icon.textContent = entry.name.includes(".") ? entry.name.split(".").pop().slice(0, 2).toUpperCase() : "--";
      const label = document.createElement("span");
      label.className = "tree-file-name";
      label.textContent = entry.name;
      button.append(icon, label);
      if (Date.now() - Date.parse(entry.file.updated_at) < 120_000) {
        const recent = document.createElement("span");
        recent.className = "recent-dot";
        recent.title = "Changed recently";
        button.append(recent);
      }
      button.addEventListener("click", () => {
        selectedFiles.set(activeUsername, entry.path);
        renderPanel();
      });
      parent.append(button);
    }
  }

  const tree = document.createElement("div");
  tree.className = "file-tree";
  renderBranch(root, tree, 0);
  return tree;
}

function renderPanel() {
  const student = students.get(activeUsername);
  if (!student) {
    panelElement.innerHTML = '<div class="panel-empty">Select a student workspace to inspect their files.</div>';
    return;
  }
  const previousPath = panelElement.querySelector(".breadcrumb-file")?.textContent;
  const previousScrollTop = panelElement.querySelector(".code-scroll")?.scrollTop || 0;
  const filePaths = Object.keys(student.files).sort();
  let selectedPath = selectedFiles.get(student.username);
  if (!filePaths.includes(selectedPath)) {
    if (followEnabled.get(student.username) !== false) {
      selectedPath = Object.entries(student.files).sort((a, b) => Date.parse(b[1].updated_at) - Date.parse(a[1].updated_at))[0]?.[0];
    } else {
      selectedPath = filePaths[0] || null;
    }
  }
  selectedFiles.set(student.username, selectedPath);

  const shell = document.createElement("div");
  shell.className = "workspace-shell";
  const sidebar = document.createElement("aside");
  sidebar.className = "file-sidebar";
  const sidebarHead = document.createElement("div");
  sidebarHead.className = "sidebar-head";
  const filesLabel = document.createElement("span");
  filesLabel.textContent = "FILES";
  const fileCount = document.createElement("span");
  fileCount.className = "file-count";
  fileCount.textContent = String(filePaths.length).padStart(2, "0");
  sidebarHead.append(filesLabel, fileCount);
  sidebar.append(sidebarHead, buildTree(student.files, selectedPath));

  const editor = document.createElement("section");
  editor.className = "editor-area";
  const toolbar = document.createElement("div");
  toolbar.className = "editor-toolbar";
  const breadcrumb = document.createElement("div");
  breadcrumb.className = "breadcrumb";
  const username = document.createElement("span");
  username.textContent = student.username;
  breadcrumb.append(username);
  if (selectedPath) {
    const separator = document.createElement("span");
    separator.className = "breadcrumb-separator";
    separator.textContent = "/";
    const fileName = document.createElement("span");
    fileName.className = "breadcrumb-file";
    fileName.textContent = selectedPath;
    breadcrumb.append(separator, fileName);
  }

  const controls = document.createElement("div");
  controls.className = "editor-controls";
  const followLabel = document.createElement("label");
  followLabel.className = "follow-control";
  const follow = document.createElement("input");
  follow.type = "checkbox";
  follow.checked = followEnabled.get(student.username) !== false;
  follow.addEventListener("change", () => followEnabled.set(student.username, follow.checked));
  const switchMark = document.createElement("span");
  switchMark.className = "switch-mark";
  const followText = document.createElement("span");
  followText.textContent = "Follow";
  followLabel.append(follow, switchMark, followText);
  controls.append(followLabel);
  toolbar.append(breadcrumb, controls);

  const editorBody = document.createElement("div");
  editorBody.className = "code-scroll";
  if (selectedPath) {
    const code = document.createElement("code");
    code.className = `language-${languageFor(selectedPath)}`;
    code.textContent = student.files[selectedPath].content;
    const pre = document.createElement("pre");
    pre.append(code);
    editorBody.append(pre);
    if (window.hljs) window.hljs.highlightElement(code);
  } else {
    const noFiles = document.createElement("div");
    noFiles.className = "panel-empty no-files";
    noFiles.textContent = "No shareable source files in this workspace yet.";
    editorBody.append(noFiles);
  }
  if (selectedPath === previousPath) editorBody.scrollTop = previousScrollTop;

  const editorFoot = document.createElement("div");
  editorFoot.className = "editor-foot";
  const currentStatus = statusFor(student);
  const statusText = document.createElement("span");
  statusText.append(makeStatus(currentStatus), document.createTextNode(` ${currentStatus} · updated ${relativeTime(student.last_change_epoch)}`));
  const languageText = document.createElement("span");
  languageText.textContent = selectedPath ? languageFor(selectedPath).toUpperCase() : "PLAIN TEXT";
  editorFoot.append(statusText, languageText);

  editor.append(toolbar, editorBody, editorFoot);
  shell.append(sidebar, editor);
  panelElement.replaceChildren(shell);
}

function renderAll() {
  renderOverview();
  renderTabs();
  renderPanel();
}

function selectStudent(username) {
  if (!students.has(username)) return;
  activeUsername = username;
  renderAll();
}

function acceptStudent(student, changed = []) {
  const previous = students.get(student.username);
  students.set(student.username, student);
  if (!activeUsername) activeUsername = student.username;
  if (followEnabled.get(student.username) !== false) {
    const latestChange = changed
      .filter(path => student.files[path])
      .sort((a, b) => Date.parse(student.files[b].updated_at) - Date.parse(student.files[a].updated_at))[0];
    if (latestChange) selectedFiles.set(student.username, latestChange);
    else if (!selectedFiles.has(student.username)) {
      const mostRecent = Object.entries(student.files).sort((a, b) => Date.parse(b[1].updated_at) - Date.parse(a[1].updated_at))[0];
      if (mostRecent) selectedFiles.set(student.username, mostRecent[0]);
    }
  }
  if (!previous || activeUsername === student.username) renderAll();
  else {
    renderOverview();
    renderTabs();
  }
}

async function connect() {
  if (!teacherKey) {
    connectionElement.textContent = "Teacher key required in page URL";
    return;
  }
  const query = `?key=${encodeURIComponent(teacherKey)}`;
  eventSource?.close();
  eventSource = new EventSource(`/api/events${query}`);
  eventSource.onopen = () => {
    connectionElement.textContent = "Live connection established";
  };
  eventSource.onerror = () => {
    connectionElement.textContent = "Reconnecting to classroom...";
  };
  eventSource.onmessage = message => {
    try {
      const event = JSON.parse(message.data);
      if (event.type === "student") {
        const previous = students.get(event.student.username);
        const files = event.snapshot ? { ...(event.files || {}) } : { ...(previous?.files || {}), ...(event.files || {}) };
        for (const path of event.deleted || []) delete files[path];
        acceptStudent({ ...event.student, files }, event.changed || []);
      }
    } catch {
      connectionElement.textContent = "Received an invalid classroom update";
    }
  };
}

function updateClock() {
  document.querySelector("#clock").textContent = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date());
  renderOverview();
  renderTabs();
}

connect();
updateClock();
window.setInterval(updateClock, 5_000);