const socket = io();

const state = {
  user: null,
  events: [],
  nodes: [],
  edges: [],
  activeNodeId: null,
  selectedNodeIds: new Set(),
  expanded: new Set(),
  pan: { x: 0, y: 0 },
  zoom: 1,
};

const timelineSaveIndicators = new Map();
const timelineSaveTimers = new Map();
const timelineDrafts = new Map();
// Set by the active page's init function; pages without live content leave it null
let pageRenderer = null;

const history = {
  undoStack: [],
  redoStack: [],
  isUndoing: false,
  lastActionTime: 0,
  push(undoFn, redoFn) {
    if (this.isUndoing) return null;
    const entry = { undo: undoFn, redo: redoFn };
    this.undoStack.push(entry);
    this.redoStack = [];
    if (this.undoStack.length > 200) this.undoStack.shift();
    return entry;
  },
  // Undo one specific entry (e.g. from a toast's Undo button) even if newer actions exist
  async undoEntry(entry) {
    if (!entry) return;
    if (this.undoStack[this.undoStack.length - 1] === entry) {
      await this.undo();
      return;
    }
    const index = this.undoStack.indexOf(entry);
    if (index === -1) return;
    this.undoStack.splice(index, 1);
    this.isUndoing = true;
    await entry.undo();
    this.isUndoing = false;
    showToast("Undo");
  },
  async undo() {
    const action = this.undoStack.pop();
    if (action) {
      this.isUndoing = true;
      this.lastActionTime = Date.now();
      await action.undo();
      this.redoStack.push(action);
      this.isUndoing = false;
      showToast("Undo");
    }
  },
  async redo() {
    const action = this.redoStack.pop();
    if (action) {
      this.isUndoing = true;
      this.lastActionTime = Date.now();
      await action.redo();
      this.undoStack.push(action);
      this.isUndoing = false;
      showToast("Redo");
    }
  }
};

function isEditableTarget(target) {
  return !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
}

window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    const key = e.key.toLowerCase();
    if (key !== "z" && key !== "y") return;
    // Leave native undo alone in fields that aren't tracked by the app history (search, timeline, ...)
    if (isEditableTarget(e.target) && !e.target.closest("#node-editor")) return;
    e.preventDefault();
    // Commit in-progress typing first so it becomes the entry that gets undone
    if (typeof window.flushPendingSave === "function") window.flushPendingSave();
    if (key === "z" && !e.shiftKey) history.undo();
    else history.redo();
  }
});

const page = document.querySelector("[data-page]")?.dataset.page;
const toast = document.querySelector("#toast");

function hideToast() {
  clearTimeout(showToast.timer);
  toast.classList.remove("is-visible");
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 200);
}

// Optional action renders a button in the toast (e.g. "Undo") and keeps it up a little longer
function showToast(message, action = null) {
  if (!toast) return;
  toast.replaceChildren(document.createTextNode(message));
  toast.classList.toggle("has-action", !!action);
  if (action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "toast-action";
    button.textContent = action.label;
    button.addEventListener("click", () => {
      hideToast();
      action.onClick();
    });
    toast.append(button);
  }
  toast.hidden = false;
  requestAnimationFrame(() => toast.classList.add("is-visible"));
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(hideToast, action ? 6000 : 2600);
}

const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

// fetch() for state-changing API calls; Flask-WTF expects the token in this header
function apiFetch(url, options = {}) {
  return fetch(url, { ...options, headers: { "X-CSRFToken": csrfToken, ...(options.headers || {}) } });
}

let activeContextMenu = null;

function closeContextMenu() {
  if (!activeContextMenu) return;
  activeContextMenu.cleanup();
  activeContextMenu = null;
}

function openContextMenu(menu, clientX, clientY) {
  closeContextMenu();
  menu.hidden = false;
  const rect = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(clientX, window.innerWidth - rect.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(clientY, window.innerHeight - rect.height - 8))}px`;

  const onPointerDown = (event) => {
    if (!menu.contains(event.target)) closeContextMenu();
  };
  const onKeyDown = (event) => {
    if (event.key === "Escape") closeContextMenu();
  };
  // Button onclick handlers run first (target phase), then the menu closes as the click bubbles
  const onClick = (event) => {
    if (event.target.closest("button")) closeContextMenu();
  };
  document.addEventListener("pointerdown", onPointerDown, true);
  document.addEventListener("keydown", onKeyDown);
  window.addEventListener("blur", closeContextMenu);
  window.addEventListener("resize", closeContextMenu);
  menu.addEventListener("click", onClick);

  activeContextMenu = {
    cleanup() {
      menu.hidden = true;
      document.removeEventListener("pointerdown", onPointerDown, true);
      document.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("blur", closeContextMenu);
      window.removeEventListener("resize", closeContextMenu);
      menu.removeEventListener("click", onClick);
    },
  };
}

// Creates a page and selects it once the server confirms (the handler acks with the new node)
function createNode(payload, onCreated) {
  socket.emit("node:create", { title: "Untitled page", caption: "", parent_id: null, in_graph: false, x: 120, y: 120, ...payload }, (node) => {
    if (!node || !node.id) return;
    upsert(state.nodes, node);
    onCreated?.(node);
  });
}

function focusTitleSoon() {
  requestAnimationFrame(() => {
    const title = document.querySelector("#node-title");
    if (!title || title.offsetParent === null) return;
    title.focus();
    title.select();
  });
}

// Pages are soft-deleted on the server, so deletes can be undone from the toast or with Ctrl+Z
function deleteNodeWithUndo(nodeId) {
  const node = state.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  const children = state.nodes.filter((item) => item.parent_id === nodeId).map((item) => item.id);
  if (typeof window.flushPendingSave === "function") window.flushPendingSave();
  const entry = history.push(
    () => socket.emit("node:restore", { id: nodeId, children }),
    () => socket.emit("node:delete", { id: nodeId })
  );
  socket.emit("node:delete", { id: nodeId });
  showToast(`Deleted "${node.title || "Untitled page"}"`, { label: "Undo", onClick: () => history.undoEntry(entry) });
}

function removeFromGraphWithUndo(nodeId) {
  const node = state.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  const entry = history.push(
    () => socket.emit("node:update", { id: nodeId, in_graph: true }),
    () => socket.emit("node:update", { id: nodeId, in_graph: false })
  );
  socket.emit("node:update", { id: nodeId, in_graph: false });
  showToast(`Removed "${node.title || "Untitled page"}" from the graph`, { label: "Undo", onClick: () => history.undoEntry(entry) });
}

function setTimelineSaveIndicator(id, status) {
  const eventId = Number(id);
  if (!eventId) return;
  clearTimeout(timelineSaveTimers.get(eventId));
  timelineSaveTimers.delete(eventId);

  if (!status) {
    timelineSaveIndicators.delete(eventId);
  } else {
    timelineSaveIndicators.set(eventId, status);
    if (status === "saved") {
      timelineSaveTimers.set(eventId, setTimeout(() => {
        timelineSaveIndicators.delete(eventId);
        timelineSaveTimers.delete(eventId);
        renderTimelineSaveIndicator(eventId);
      }, 1800));
    }
  }

  renderTimelineSaveIndicator(eventId);
}

// Updates just the indicator so unrelated items being edited aren't re-rendered
function renderTimelineSaveIndicator(eventId) {
  const indicator = document.querySelector(`#timeline-event-${eventId} .timeline-save-indicator`);
  if (!indicator) return;
  const status = timelineSaveIndicators.get(eventId);
  indicator.className = `timeline-save-indicator ${status ? `is-${status}` : ""}`;
  indicator.textContent = status === "saving" ? "Saving..." : status === "saved" ? "Saved" : "";
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// Only allow http(s) and same-origin relative URLs in generated links
function safeUrl(value) {
  const url = String(value ?? "").trim();
  return /^(https?:\/\/|\/(?!\/))/i.test(url) ? url : "#";
}

function insertTextAtCaret(editor, text, addNewlines = false) {
  const selection = window.getSelection();
  if (!selection.rangeCount || !editor.contains(selection.getRangeAt(0).startContainer)) return;
  const range = selection.getRangeAt(0);

  const node = document.createTextNode(addNewlines ? `\n${text}\n` : text);
  range.deleteContents();
  range.insertNode(node);
  range.setStartAfter(node);
  range.collapse(true);
  selection.removeAllRanges();
  selection.addRange(range);

  setEditorMarkdown(editor, editorText(editor), true);
  editor.dispatchEvent(new Event("input", { bubbles: true }));
}

function upsert(collection, item) {
  const index = collection.findIndex((existing) => existing.id === item.id);
  if (index === -1) collection.push(item);
  else collection[index] = item;
}

function sortedNodes(nodes = state.nodes) {
  return [...nodes].sort((a, b) => {
    const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
    return orderDelta || a.title.localeCompare(b.title);
  });
}

function currentNode() {
  return state.nodes.find((node) => node.id === state.activeNodeId);
}

function childNodes(parentId) {
  return sortedNodes(state.nodes.filter((node) => (node.parent_id || null) === (parentId || null)));
}

function nextOrderIndex(parentId) {
  const siblings = childNodes(parentId);
  if (!siblings.length) return 0;
  return Math.max(...siblings.map((node) => node.order_index ?? 0)) + 1;
}

function nodePath(node) {
  const path = [];
  let cursor = node;
  const seen = new Set();
  while (cursor && !seen.has(cursor.id)) {
    seen.add(cursor.id);
    path.unshift(cursor);
    cursor = state.nodes.find((item) => item.id === cursor.parent_id);
  }
  return path;
}

function inlineMarkdown(value) {
  const escaped = escapeHtml(value);
  return escaped
    .replace(/(!\[([^\]]*)\]\((https?:\/\/[^)\s]+|[\w/.-]+\.\w+)\))/g, '<span class="md-img"><span class="md-img-syntax" contenteditable="false">$1</span><img src="$3" alt="$2"></span>')
    .replace(/(`[^`]+`)/g, "<code>$1</code>")
    .replace(/(\*\*([^*]+)\*\*)/g, "<strong>$1</strong>")
    .replace(/(\*[^*]+\*)/g, "<em>$1</em>");
}

function markdownToLiveHtml(markdown) {
  const lines = String(markdown ?? "").split("\n");
  if (!lines.length || (lines.length === 1 && lines[0] === "")) {
    return '<div class="md-line md-empty"><br></div>';
  }
  let codeFence = null;
  return lines.map((line, index) => {
    const fence = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence && (!codeFence || fence[1][0] === codeFence.marker)) {
      if (!codeFence) {
        codeFence = { marker: fence[1][0], length: fence[1].length };
        return `<div class="md-line md-code md-code-fence md-code-first">${escapeHtml(line) || "<br>"}</div>`;
      }

      if (fence[1].length >= codeFence.length && !fence[2].trim()) {
        codeFence = null;
        return `<div class="md-line md-code md-code-fence md-code-last">${escapeHtml(line) || "<br>"}</div>`;
      }
    }
    if (codeFence) {
      const nextLine = lines[index + 1] || "";
      const nextFence = nextLine.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
      const isBeforeClose = nextFence && nextFence[1][0] === codeFence.marker && nextFence[1].length >= codeFence.length && !nextFence[2].trim();
      return `<div class="md-line md-code ${isBeforeClose ? "md-code-before-last" : "md-code-middle"}">${escapeHtml(line) || "<br>"}</div>`;
    }
    const heading = line.match(/^(#{1,6})(\s.*)?$/);
    if (heading) {
      return `<div class="md-line md-heading md-h${heading[1].length}">${inlineMarkdown(line) || "<br>"}</div>`;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      return `<div class="md-line md-list">${inlineMarkdown(line) || "<br>"}</div>`;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      return `<div class="md-line md-list">${inlineMarkdown(line) || "<br>"}</div>`;
    }
    if (/^\s*>\s?/.test(line)) {
      return `<div class="md-line md-quote">${inlineMarkdown(line) || "<br>"}</div>`;
    }
    return `<div class="md-line">${inlineMarkdown(line) || "<br>"}</div>`;
  }).join("");
}

function editorText(editor) {
  const lines = Array.from(editor.querySelectorAll(".md-line"));
  if (lines.length > 0) {
    return lines.map((line) => line.textContent.replace(/\r/g, "")).join("\n");
  }
  return editor.innerText.replace(/\u00a0/g, "").replace(/\n$/, "");
}

function caretOffset(root) {
  const selection = window.getSelection();
  if (!selection || !selection.rangeCount) return 0;
  const range = selection.getRangeAt(0);
  if (!root.contains(range.startContainer)) return 0;

  let offset = 0;
  const lines = Array.from(root.querySelectorAll(".md-line"));
  if (lines.length === 0) {
    const before = range.cloneRange();
    before.selectNodeContents(root);
    before.setEnd(range.startContainer, range.startOffset);
    return before.toString().length;
  }

  for (const line of lines) {
    if (line.contains(range.startContainer)) {
      const before = range.cloneRange();
      before.setStart(line, 0);
      before.setEnd(range.startContainer, range.startOffset);
      offset += before.toString().length;
      break;
    } else {
      offset += line.textContent.length + 1;
    }
  }
  return offset;
}

function restoreCaret(root, offset) {
  const lines = Array.from(root.querySelectorAll(".md-line"));
  if (lines.length === 0) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let currentOffset = 0;
    let node = walker.nextNode();
    while (node) {
      const nextOffset = currentOffset + node.textContent.length;
      if (offset <= nextOffset) {
        const range = document.createRange();
        const selection = window.getSelection();
        range.setStart(node, Math.max(0, offset - currentOffset));
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
        return;
      }
      currentOffset = nextOffset;
      node = walker.nextNode();
    }
    return;
  }

  let currentOffset = 0;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const lineLength = line.textContent.length;
    const nextOffset = currentOffset + lineLength + 1;

    if (offset <= currentOffset + lineLength || (i === lines.length - 1 && offset <= nextOffset)) {
      const localOffset = Math.max(0, offset - currentOffset);
      const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
      let lineTextOffset = 0;
      let node = walker.nextNode();

      if (!node) {
        const range = document.createRange();
        const selection = window.getSelection();
        range.setStart(line, 0);
        range.collapse(true);
        selection.removeAllRanges();
        selection.addRange(range);
        return;
      }

      while (node) {
        const nodeLength = node.textContent.length;
        if (lineTextOffset + nodeLength >= localOffset) {
          const range = document.createRange();
          const selection = window.getSelection();
          range.setStart(node, localOffset - lineTextOffset);
          range.collapse(true);
          selection.removeAllRanges();
          selection.addRange(range);
          return;
        }
        lineTextOffset += nodeLength;
        node = walker.nextNode();
      }

      const range = document.createRange();
      const selection = window.getSelection();
      range.setStart(line, line.childNodes.length);
      range.collapse(true);
      selection.removeAllRanges();
      selection.addRange(range);
      return;
    }
    currentOffset = nextOffset;
  }

  const range = document.createRange();
  const selection = window.getSelection();
  range.selectNodeContents(root);
  range.collapse(false);
  selection.removeAllRanges();
  selection.addRange(range);
}

function setEditorMarkdown(editor, markdown, preserveCaret = false) {
  const hasFocus = editor.contains(document.activeElement);
  const offset = (preserveCaret && hasFocus) ? caretOffset(editor) : 0;
  editor.innerHTML = markdownToLiveHtml(markdown);
  if (preserveCaret && hasFocus) restoreCaret(editor, offset);
}

function debounce(callback, delay) {
  let timer;
  const fn = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
  fn.cancel = () => clearTimeout(timer);
  return fn;
}

function parseServerDate(value = new Date()) {
  if (value instanceof Date) return value;
  if (typeof value === "string" && value.trim()) {
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
    return new Date(normalized);
  }
  return new Date(value);
}

function eventTimeMs(value) {
  const time = parseServerDate(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function toLocalInputValue(value = new Date()) {
  if (arguments.length > 0 && (value === null || value === "")) return "";
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function localInputValueToIso(value) {
  const match = String(value || "").match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/);
  if (!match) return null;

  const [, year, month, day, hour, minute, second = "0", millisecond = "0"] = match;
  const date = new Date(
    Number(year),
    Number(month) - 1,
    Number(day),
    Number(hour),
    Number(minute),
    Number(second),
    Number(millisecond.padEnd(3, "0"))
  );

  if (
    date.getFullYear() !== Number(year) ||
    date.getMonth() !== Number(month) - 1 ||
    date.getDate() !== Number(day) ||
    date.getHours() !== Number(hour) ||
    date.getMinutes() !== Number(minute) ||
    date.getSeconds() !== Number(second)
  ) {
    return null;
  }

  return date.toISOString();
}

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parseServerDate(value));
}

function resultExcerpt(value, query) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  const start = Math.max(0, index === -1 ? 0 : index - 36);
  const excerpt = text.slice(start, start + 110);
  return `${start > 0 ? "... " : ""}${excerpt}${start + 110 < text.length ? " ..." : ""}`;
}

function attachedFileResults(files, sourceTitle, sourceType, sourceUrl) {
  return (files || []).map((file) => ({
    type: sourceType === "Timeline" ? "Timeline attachment" : "Vault attachment",
    title: file.name || file.filename || "Attached file",
    detail: sourceTitle,
    href: file.url || sourceUrl,
    searchText: `${file.name || ""} ${file.filename || ""} ${sourceTitle}`,
  }));
}

async function loadSearchUploads() {
  const [images, files] = await Promise.all([
    fetch("/api/images").then((response) => response.ok ? response.json() : []).catch(() => []),
    fetch("/api/files").then((response) => response.ok ? response.json() : []).catch(() => []),
  ]);
  return { images, files };
}

function buildSearchIndex({ images = [], files = [] } = {}) {
  const nodeResults = state.nodes.flatMap((node) => [
    {
      type: "Vault",
      title: node.title || "Untitled page",
      detail: node.caption || resultExcerpt(node.notes, ""),
      href: `/nodes/${node.id}`,
      searchText: `${node.title || ""} ${node.caption || ""} ${node.notes || ""}`,
    },
    ...attachedFileResults(node.files, node.title || "Untitled page", "Vault", `/nodes/${node.id}`),
  ]);

  const timelineResults = state.events.flatMap((event) => [
    {
      type: "Timeline",
      title: event.title || "Untitled event",
      detail: `${formatTime(event.occurred_at)}${event.ended_at ? ` to ${formatTime(event.ended_at)}` : ""} ${resultExcerpt(event.body, "")}`.trim(),
      href: `/timeline#timeline-event-${event.id}`,
      searchText: `${event.title || ""} ${event.body || ""} ${formatTime(event.occurred_at)} ${event.ended_at ? formatTime(event.ended_at) : ""}`,
    },
    ...attachedFileResults(event.files, event.title || "Untitled event", "Timeline", `/timeline#timeline-event-${event.id}`),
  ]);

  const imageResults = images.map((image) => ({
    type: "Image",
    title: image.filename,
    detail: "Image vault",
    href: image.url || "/images",
    searchText: image.filename,
  }));

  const fileResults = files.map((file) => ({
    type: "File",
    title: file.filename,
    detail: "File vault",
    href: file.url || "/files",
    searchText: file.filename,
  }));

  return [...nodeResults, ...timelineResults, ...imageResults, ...fileResults];
}

function initGlobalSearch() {
  const input = document.querySelector("#global-search-input");
  const results = document.querySelector("#global-search-results");
  if (!input || !results) return;

  let index = [];
  let uploadsLoaded = false;
  let cachedUploads = { images: [], files: [] };

  const hide = () => {
    results.hidden = true;
  };

  const render = async () => {
    const query = input.value.trim();
    if (!query) {
      hide();
      return;
    }

    if (!uploadsLoaded) {
      uploadsLoaded = true;
      cachedUploads = await loadSearchUploads();
      index = buildSearchIndex(cachedUploads);
    } else {
      index = buildSearchIndex(cachedUploads);
    }

    const normalized = query.toLowerCase();
    const matches = index
      .filter((item) => String(item.searchText || "").toLowerCase().includes(normalized))
      .slice(0, 12);

    results.hidden = false;
    if (!matches.length) {
      results.innerHTML = '<div class="search-empty">No project results</div>';
      return;
    }

    results.innerHTML = matches.map((item) => `
      <a class="search-result" href="${escapeHtml(item.href)}">
        <span class="search-result-title">${escapeHtml(item.title)}</span>
        <span class="search-result-meta">${escapeHtml(item.type)}${item.detail ? ` - ${escapeHtml(resultExcerpt(item.detail, query))}` : ""}</span>
      </a>
    `).join("");
  };

  input.addEventListener("input", render);
  input.addEventListener("focus", render);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      hide();
      input.blur();
    }
    if (event.key === "Enter") {
      const firstResult = results.querySelector(".search-result");
      if (firstResult) {
        event.preventDefault();
        firstResult.click();
      }
    }
  });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".global-search")) hide();
  });
}

function initEditorSurface({ fixedNodeId = null } = {}) {
  const title = document.querySelector("#node-title");
  const caption = document.querySelector("#node-caption");
  const notes = document.querySelector("#node-notes");
  const colorPicker = document.querySelector("#node-color-picker");
  const addToGraphBtn = document.querySelector("#add-to-graph");
  const breadcrumb = document.querySelector("#breadcrumb");
  let lastLoadedNodeId = null;

  const nodeFilesBlock = document.querySelector("#node-files");
  const editorForm = document.querySelector("#node-editor");
  const TEXT_FIELDS = ["title", "caption", "notes"];

  // Last text we know the server has for the loaded node; anything different locally is unsaved
  let lastSavedState = null;
  // Set when someone else changes a field we have unsaved edits in; autosave pauses until resolved
  let conflict = null;

  const conflictBanner = document.createElement("div");
  conflictBanner.className = "edit-conflict";
  conflictBanner.setAttribute("role", "alert");
  conflictBanner.hidden = true;
  conflictBanner.innerHTML = `
    <div class="edit-conflict-text"></div>
    <div class="edit-conflict-actions">
      <button type="button" class="ghost-button" data-conflict="theirs">Load their version</button>
      <button type="button" class="primary-button" data-conflict="mine">Keep mine</button>
    </div>
  `;
  editorForm?.prepend(conflictBanner);

  const localValues = () => ({ title: title.value, caption: caption.value, notes: editorText(notes) });
  const fieldElement = { title, caption, notes };

  function setField(field, value) {
    const element = fieldElement[field];
    if (field === "notes") {
      if (editorText(notes) !== value || !notes.querySelector(".md-line")) setEditorMarkdown(notes, value, true);
      return;
    }
    if (element.value === value) return;
    const focused = document.activeElement === element;
    const caret = focused ? element.selectionStart : null;
    element.value = value;
    if (focused && caret != null) element.setSelectionRange(Math.min(caret, value.length), Math.min(caret, value.length));
  }

  function renderConflict() {
    conflictBanner.hidden = !conflict;
    if (!conflict) return;
    const fields = conflict.fields.map((field) => (field === "notes" ? "the notes" : `the ${field}`)).join(" and ");
    conflictBanner.querySelector(".edit-conflict-text").innerHTML =
      `<strong>${escapeHtml(conflict.by || "Someone")}</strong> changed ${fields} while you were editing. Autosave is paused until you choose which version to keep.`;
  }

  // Called for every node:updated before it is applied to state
  window.detectEditConflict = (remote) => {
    if (!remote.origin || remote.origin === socket.id) return;
    if (!lastSavedState || remote.id !== lastSavedState.id || remote.id !== state.activeNodeId) return;
    const remoteText = { title: remote.title, caption: remote.caption || "", notes: remote.notes || "" };
    if (conflict) {
      conflict.remote = remoteText;
      conflict.by = remote.edited_by;
      renderConflict();
      return;
    }
    const local = localValues();
    const fields = TEXT_FIELDS.filter((field) => (
      local[field] !== lastSavedState[field] && remoteText[field] !== lastSavedState[field] && remoteText[field] !== local[field]
    ));
    if (!fields.length) return;
    save.cancel();
    conflict = { nodeId: remote.id, by: remote.edited_by, fields, remote: remoteText };
    renderConflict();
  };

  conflictBanner.addEventListener("click", (event) => {
    const choice = event.target.closest("[data-conflict]")?.dataset.conflict;
    if (!choice || !conflict) return;
    const { nodeId, remote } = conflict;
    conflict = null;
    renderConflict();
    lastSavedState = { ...(lastSavedState || {}), id: nodeId, ...remote };
    if (choice === "theirs") {
      TEXT_FIELDS.forEach((field) => setField(field, remote[field]));
    } else {
      // Undo after this goes back to their version
      commitText(nodeId);
    }
  });

  function renderEditor() {
    const node = currentNode();
    if (!node) {
      if (lastLoadedNodeId && typeof window.flushPendingSave === "function") {
        window.flushPendingSave();
      }
      lastLoadedNodeId = null;
      lastSavedState = null;
      return;
    }

    const isNewSelection = lastLoadedNodeId !== node.id;
    if (isNewSelection) {
      if (lastLoadedNodeId && typeof window.flushPendingSave === "function") {
        window.flushPendingSave();
      }
      lastLoadedNodeId = node.id;
    }

    if (breadcrumb) breadcrumb.innerHTML = nodePath(node).map((item) => escapeHtml(item.title)).join("<span>/</span>");

    // Undo/redo results always win; otherwise fields with unsaved local edits are left alone
    const forceUpdate = isNewSelection || (Date.now() - history.lastActionTime < 1000);
    const inConflict = conflict && conflict.nodeId === node.id;
    const previous = !isNewSelection && lastSavedState && lastSavedState.id === node.id ? lastSavedState : null;
    const local = localValues();
    const serverText = { title: node.title, caption: node.caption || "", notes: node.notes || "" };
    if (!inConflict) {
      TEXT_FIELDS.forEach((field) => {
        const dirty = previous && local[field] !== previous[field];
        if (forceUpdate || !dirty) setField(field, serverText[field]);
      });
      lastSavedState = { id: node.id, ...serverText };
    }
    renderConflict();

    if (nodeFilesBlock) {
      nodeFilesBlock.innerHTML = (node.files || []).map((file, i) => `
        <div class="attached-file">
          <span class="file-icon-small">📄</span>
          <a href="${escapeHtml(safeUrl(file.url))}" target="_blank" rel="noreferrer">${escapeHtml(file.name)}</a>
          <button type="button" class="delete-attached" data-file-index="${i}" title="Remove attachment">&times;</button>
        </div>
      `).join("");

      nodeFilesBlock.querySelectorAll(".delete-attached").forEach(btn => {
        btn.addEventListener("click", () => {
          node.files.splice(Number(btn.dataset.fileIndex), 1);
          renderEditor();
          socket.emit("node:update", { id: node.id, files: node.files });
        });
      });
    }

    if (colorPicker) {
      colorPicker.querySelectorAll(".color-swatch").forEach((swatch) => {
        swatch.classList.toggle("active", (swatch.dataset.color || "") === (node.color || ""));
      });
    }

    if (addToGraphBtn) {
      addToGraphBtn.hidden = !!node.in_graph;
    }

    if (fixedNodeId) document.title = `${node.title} - RedObsidian`;
  }

  // Sends the text fields of the editor for nodeId, only if they changed since the last save
  const commitText = (nodeId) => {
    const node = state.nodes.find(n => n.id === nodeId);
    if (!node || (conflict && conflict.nodeId === nodeId)) return;

    const newState = {
      id: node.id,
      title: title.value,
      caption: caption.value,
      notes: editorText(notes),
    };
    const prevState = lastSavedState && lastSavedState.id === node.id ? lastSavedState : null;
    if (prevState && prevState.title === newState.title && prevState.notes === newState.notes && prevState.caption === newState.caption) {
      return;
    }

    if (prevState) {
      const undoState = { id: prevState.id, title: prevState.title, caption: prevState.caption, notes: prevState.notes };
      const redoState = { ...newState };
      history.push(
        () => socket.emit("node:update", undoState),
        () => socket.emit("node:update", redoState)
      );
    }
    lastSavedState = { ...(prevState || {}), ...newState };
    socket.emit("node:update", newState);
  };

  const save = debounce(() => {
    if (state.activeNodeId) commitText(state.activeNodeId);
  }, 600);

  const flush = () => {
    save.cancel();
    if (!lastLoadedNodeId) return;
    if (conflict && conflict.nodeId === lastLoadedNodeId) {
      // Leaving with an unresolved conflict keeps their version, but offers to save ours
      const mine = { id: conflict.nodeId, ...localValues() };
      const pageTitle = conflict.remote.title || "Untitled page";
      conflict = null;
      renderConflict();
      showToast(`Your conflicting edits to "${pageTitle}" were not saved`, {
        label: "Save mine",
        onClick: () => socket.emit("node:update", mine),
      });
      return;
    }
    commitText(lastLoadedNodeId);
  };

  window.cancelPendingSave = () => save.cancel();
  window.flushPendingSave = flush;

  document.addEventListener("click", (event) => {
    const link = event.target.closest(".live-markdown-editor a");
    if (!link) return;

    // Standard behavior: single click opens link
    // "Alt" key behavior: hold Alt to ignore link and edit its text/markdown
    if (!event.altKey) {
      event.preventDefault();
      event.stopPropagation();
      window.open(link.href, "_blank", "noreferrer");
    }
  });

  [title, caption, notes].forEach((input) => {
    input?.addEventListener("input", () => {
      if (input === notes) setEditorMarkdown(notes, editorText(notes), true);
      save();
    });
  });

  const uploadFile = async (file) => {
    const formData = new FormData();
    formData.append("file", file);
    try {
      const response = await apiFetch("/api/upload", { method: "POST", body: formData });
      const payload = await response.json();
      if (payload.url) {
        return { url: payload.url, name: file.name, isImage: file.type.startsWith("image/") };
      }
      showToast(payload.error || "Upload failed");
      return null;
    } catch (err) {
      showToast("Upload failed");
    }
    return null;
  };

  document.addEventListener("paste", async (event) => {
    const editor = event.target.closest(".live-markdown-editor");
    if (!editor) return;

    const items = event.clipboardData?.items;
    let hasFile = false;

    if (items) {
      for (const item of items) {
        if (item.kind === "file") {
          hasFile = true;
          event.preventDefault();
          const file = item.getAsFile();
          const result = await uploadFile(file);
          if (result) {
            if (result.isImage) {
              insertTextAtCaret(editor, `![${result.name}](${result.url})`, true);
            } else if (editor === notes) {
              if (typeof window.flushPendingSave === "function") window.flushPendingSave();
              const node = currentNode();
              if (node) {
                node.files = node.files || [];
                node.files.push({ name: result.name, url: result.url });
                renderEditor();
                socket.emit("node:update", { id: node.id, files: node.files });
              }
            }
          }
        }
      }
    }

    // Handle plain text paste if no files were found
    if (!hasFile) {
      const text = event.clipboardData?.getData("text/plain");
      if (text) {
        event.preventDefault();
        insertTextAtCaret(editor, text, false);
      }
    }
  });

  notes?.addEventListener("drop", async (event) => {
    const files = event.dataTransfer?.files;
    if (files && files.length) {
      event.preventDefault();
      // Put the caret where the files were dropped so images land there
      const dropRange = document.caretRangeFromPoint?.(event.clientX, event.clientY);
      if (dropRange && notes.contains(dropRange.startContainer)) {
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(dropRange);
      } else {
        notes.focus();
      }
      for (const file of files) {
        const result = await uploadFile(file);
        if (result) {
          if (result.isImage) {
            insertTextAtCaret(notes, `![${result.name}](${result.url})`);
          } else {
            if (typeof window.flushPendingSave === "function") window.flushPendingSave();
            const node = currentNode();
            if (node) {
              node.files = node.files || [];
              node.files.push({ name: result.name, url: result.url });
              renderEditor();
              socket.emit("node:update", { id: node.id, files: node.files });
            }
          }
        }
      }
    }
  });

  colorPicker?.addEventListener("click", (event) => {
    const swatch = event.target.closest(".color-swatch");
    if (!swatch) return;
    const node = currentNode();
    if (node) {
      node.color = swatch.dataset.color || "";
      renderCurrentPage();
      socket.emit("node:update", { id: node.id, color: node.color });
    }
  });

  addToGraphBtn?.addEventListener("click", () => {
    const node = currentNode();
    if (node) {
      node.in_graph = true;
      renderEditor();
      socket.emit("node:update", { id: node.id, in_graph: true });
      showToast("Added to graph");
    }
  });

  return renderEditor;
}

function initNotesPage() {
  const nodeList = document.querySelector("#node-list");
  const addNodeButton = document.querySelector("#add-node");
  const editor = document.querySelector("#node-editor");
  const emptyEditor = document.querySelector("#empty-editor");
  const contextMenu = document.querySelector("#notes-context-menu");
  const menuNewPage = document.querySelector("#menu-new-page");
  const menuNewChild = document.querySelector("#menu-new-child");
  const menuDeletePage = document.querySelector("#menu-delete-page");
  const renderEditor = initEditorSurface();

  function renderTree(parentId = null, depth = 0) {
    return childNodes(parentId).map((node) => {
      const children = childNodes(node.id);
      const expanded = state.expanded.has(node.id);
      const active = node.id === state.activeNodeId;
      return `
        <div class="tree-row ${active ? "active" : ""} ${node.color ? `color-${node.color}` : ""}" draggable="true" style="--depth:${depth}" data-node-id="${node.id}">
          <button class="tree-toggle ${expanded ? "expanded" : ""}" type="button" data-toggle-id="${node.id}">
            ${children.length ? '<span class="toggle-icon"></span>' : ""}
          </button>
          <button class="tree-title" type="button" data-select-id="${node.id}">${escapeHtml(node.title)}</button>
          <button class="tree-add" type="button" title="New child page" data-child-id="${node.id}">+</button>
        </div>
        ${children.length && expanded ? renderTree(node.id, depth + 1) : ""}
      `;
    }).join("");
  }

  function renderNotes() {
    if (!state.activeNodeId && state.nodes.length) {
      state.activeNodeId = sortedNodes()[0].id;
    }
    nodeList.innerHTML = renderTree() || '<div class="empty-tree">No pages</div>';
    nodeList.querySelectorAll("[data-select-id]").forEach((button) => {
      button.addEventListener("click", () => {
        state.activeNodeId = Number(button.dataset.selectId);
        state.expanded.add(state.activeNodeId);
        renderNotes();
      });
    });
    nodeList.querySelectorAll("[data-toggle-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        const id = Number(button.dataset.toggleId);
        if (state.expanded.has(id)) state.expanded.delete(id);
        else state.expanded.add(id);
        renderNotes();
      });
    });
    nodeList.querySelectorAll("[data-child-id]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        createPage(Number(button.dataset.childId));
      });
    });
    nodeList.querySelectorAll(".tree-row").forEach((row) => attachTreeDragHandlers(row, nodeList));

    const node = currentNode();
    editor.hidden = !node;
    emptyEditor.hidden = !!node;
    if (node) renderEditor();
  }

  function createPage(parentId = null) {
    if (parentId) state.expanded.add(parentId);
    createNode({ parent_id: parentId, order_index: nextOrderIndex(parentId) }, (node) => {
      state.activeNodeId = node.id;
      renderNotes();
      focusTitleSoon();
    });
  }

  // Allow dropping on empty space below the tree to make a root node
  nodeList.addEventListener("dragover", (event) => {
    // Only handle drops on the container itself, not on the rows
    if (event.target.closest(".tree-row")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    nodeList.classList.add("drop-root");
  });
  nodeList.addEventListener("dragleave", (event) => {
    if (event.target.closest(".tree-row")) return;
    nodeList.classList.remove("drop-root");
  });
  nodeList.addEventListener("drop", (event) => {
    nodeList.classList.remove("drop-root");
    if (event.target.closest(".tree-row")) return; // Handled by row listener
    event.preventDefault();
    const draggedId = Number(event.dataTransfer.getData("text/plain"));
    if (!draggedId) return;
    socket.emit("node:update", { id: draggedId, parent_id: null, order_index: nextOrderIndex(null) });
  });

  addNodeButton.addEventListener("click", () => createPage(null));

  const sidebar = document.querySelector(".vault-sidebar");
  sidebar.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const row = event.target.closest(".tree-row");
    const nodeId = row ? Number(row.dataset.nodeId) : null;

    menuNewPage.hidden = !!nodeId;
    menuNewChild.hidden = !nodeId;
    menuDeletePage.hidden = !nodeId;

    if (nodeId) {
      menuNewChild.onclick = () => createPage(nodeId);
      menuDeletePage.onclick = () => deleteNodeWithUndo(nodeId);
    } else {
      menuNewPage.onclick = () => createPage(null);
    }

    openContextMenu(contextMenu, event.clientX, event.clientY);
  });

  pageRenderer = renderNotes;
}

function attachTreeDragHandlers(row, nodeList) {
  row.addEventListener("dragstart", (event) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", row.dataset.nodeId);
    row.classList.add("dragging");
  });
  row.addEventListener("dragend", () => {
    row.classList.remove("dragging");
    nodeList.querySelectorAll(".tree-row").forEach((item) => item.classList.remove("drop-before", "drop-after", "drop-into"));
  });
  row.addEventListener("dragover", (event) => {
    event.preventDefault();
    const zone = dropZone(row, event.clientY);
    nodeList.querySelectorAll(".tree-row").forEach((item) => item.classList.remove("drop-before", "drop-after", "drop-into"));
    row.classList.add(`drop-${zone}`);
  });
  row.addEventListener("drop", (event) => {
    event.preventDefault();
    const draggedId = Number(event.dataTransfer.getData("text/plain"));
    const targetId = Number(row.dataset.nodeId);
    moveTreeNode(draggedId, targetId, dropZone(row, event.clientY));
  });
}

function dropZone(row, clientY) {
  const rect = row.getBoundingClientRect();
  const offset = clientY - rect.top;
  if (offset < rect.height * 0.28) return "before";
  if (offset > rect.height * 0.72) return "after";
  return "into";
}

function moveTreeNode(draggedId, targetId, zone) {
  if (!draggedId || !targetId || draggedId === targetId) return;
  const dragged = state.nodes.find((node) => node.id === draggedId);
  const target = state.nodes.find((node) => node.id === targetId);
  if (!dragged || !target) return;
  if (zone === "into") {
    state.expanded.add(target.id);
    socket.emit("node:update", { id: dragged.id, parent_id: target.id, order_index: nextOrderIndex(target.id) });
    return;
  }

  const siblings = childNodes(target.parent_id).filter((node) => node.id !== dragged.id);
  const targetIndex = siblings.findIndex((node) => node.id === target.id);
  const prev = zone === "before" ? siblings[targetIndex - 1] : target;
  const next = zone === "before" ? target : siblings[targetIndex + 1];
  let orderIndex = 0;
  if (prev && next) orderIndex = ((prev.order_index ?? 0) + (next.order_index ?? 0)) / 2;
  else if (prev) orderIndex = (prev.order_index ?? 0) + 1;
  else if (next) orderIndex = (next.order_index ?? 0) - 1;
  socket.emit("node:update", { id: dragged.id, parent_id: target.parent_id || null, order_index: orderIndex });
}

function initNodePage() {
  state.activeNodeId = Number(document.querySelector("[data-node-id]").dataset.nodeId);
  pageRenderer = initEditorSurface({ fixedNodeId: state.activeNodeId });
}

function initTimelinePage() {
  const timelineForm = document.querySelector("#timeline-form");
  const timelineList = document.querySelector("#timeline-list");
  const eventTitle = document.querySelector("#event-title");
  const eventBody = document.querySelector("#event-body");
  const eventTime = document.querySelector("#event-time");
  const eventEndTime = document.querySelector("#event-end-time");
  const eventTimeNow = document.querySelector("#event-time-now");
  const timelineAutoSort = document.querySelector("#timeline-auto-sort");

  eventBody?.addEventListener("input", () => {
    setEditorMarkdown(eventBody, editorText(eventBody), true);
  });

  eventTimeNow?.addEventListener("click", () => {
    eventTime.value = toLocalInputValue();
  });

  timelineAutoSort?.addEventListener("click", () => {
    if (!confirm("Re-apply automatic timeline sorting by date and time? This will discard all manual rearrangements.")) return;
    socket.emit("timeline:auto_sort");
  });

  let scrolledToHash = false;

  function captureTimelineFocus() {
    const active = document.activeElement;
    if (!active || !timelineList.contains(active)) return null;
    const attribute = ["data-event-title", "data-event-body", "data-event-time", "data-event-end-time"].find((name) => active.hasAttribute(name));
    if (!attribute) return null;
    return {
      selector: `[${attribute}="${active.getAttribute(attribute)}"]`,
      caret: active.isContentEditable ? caretOffset(active) : active.selectionStart,
    };
  }

  function restoreTimelineFocus(focus) {
    const element = focus && timelineList.querySelector(focus.selector);
    if (!element) return;
    element.focus({ preventScroll: true });
    if (element.isContentEditable) restoreCaret(element, focus.caret);
    else if (focus.caret != null && element.type === "text") element.setSelectionRange(focus.caret, focus.caret);
  }

  // Re-apply unsaved edits so re-renders triggered by other events don't discard them
  function applyTimelineDrafts() {
    timelineDrafts.forEach((draft, id) => {
      const item = timelineList.querySelector(`#timeline-event-${id}`);
      if (!item) {
        timelineDrafts.delete(id);
        return;
      }
      item.querySelector(`[data-event-title="${id}"]`).value = draft.title;
      setEditorMarkdown(item.querySelector(`[data-event-body="${id}"]`), draft.body);
      item.querySelector(`[data-event-time="${id}"]`).value = draft.start;
      item.querySelector(`[data-event-end-time="${id}"]`).value = draft.end;
      item.classList.add("is-dirty");
      if (draft.conflictBy) {
        item.classList.add("is-conflict");
        const indicator = item.querySelector(".timeline-save-indicator");
        indicator.className = "timeline-save-indicator is-conflict";
        indicator.textContent = `${draft.conflictBy} changed this event. Saving will overwrite their changes.`;
      }
    });
  }

  function recordTimelineDraft(id) {
    const item = timelineList.querySelector(`#timeline-event-${id}`);
    if (!item) return;
    timelineDrafts.set(id, {
      conflictBy: timelineDrafts.get(id)?.conflictBy,
      title: item.querySelector(`[data-event-title="${id}"]`).value,
      body: editorText(item.querySelector(`[data-event-body="${id}"]`)),
      start: item.querySelector(`[data-event-time="${id}"]`).value,
      end: item.querySelector(`[data-event-end-time="${id}"]`).value,
    });
    item.classList.add("is-dirty");
  }

  timelineList.addEventListener("input", (event) => {
    const item = event.target.closest(".timeline-item");
    if (item) recordTimelineDraft(Number(item.dataset.eventId));
  });
  timelineList.addEventListener("change", (event) => {
    const item = event.target.closest(".timeline-item");
    if (item) recordTimelineDraft(Number(item.dataset.eventId));
  });

  document.addEventListener("paste", (event) => {
    const editor = event.target.closest?.(".live-markdown-editor");
    if (!editor) return;
    const text = event.clipboardData?.getData("text/plain");
    event.preventDefault();
    if (text) insertTextAtCaret(editor, text);
  });

  function renderTimeline() {
    const focus = captureTimelineFocus();
    const events = [...state.events].sort((a, b) => {
      const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
      return orderDelta || eventTimeMs(b.occurred_at) - eventTimeMs(a.occurred_at);
    });
    timelineList.innerHTML = events.length ? events.map((event) => {
      const saveStatus = timelineSaveIndicators.get(event.id);
      const saveLabel = saveStatus === "saving" ? "Saving..." : saveStatus === "saved" ? "Saved" : "";
      return `
      <article id="timeline-event-${event.id}" class="timeline-item" data-event-id="${event.id}">
        <div class="timeline-drag" draggable="true" title="Drag to reorder">::</div>
        <div class="timeline-edit-fields">
          <input class="timeline-title-input" data-event-title="${event.id}" value="${escapeHtml(event.title)}" maxlength="160">
          <div id="event-body-${event.id}" class="live-markdown-editor timeline-item-body" contenteditable="true" data-placeholder="Caption or notes" data-event-body="${event.id}">${markdownToLiveHtml(event.body || "")}</div>
          <div class="timeline-row-actions">
            <div class="timeline-time-input-group">
              <label class="timeline-time-field">
                <span>Start</span>
                <input class="timeline-time-input" type="datetime-local" data-event-time="${event.id}" value="${toLocalInputValue(event.occurred_at)}">
              </label>
              <label class="timeline-time-field">
                <span>End</span>
                <input class="timeline-time-input" type="datetime-local" data-event-end-time="${event.id}" value="${toLocalInputValue(event.ended_at)}">
              </label>
              <button type="button" class="ghost-button" data-event-now="${event.id}" title="Set to now">Now</button>
            </div>
            <button type="button" class="ghost-button danger" data-event-delete="${event.id}">Delete</button>
            <button type="button" data-event-save="${event.id}">Save</button>
            <span class="timeline-save-indicator ${saveStatus ? `is-${saveStatus}` : ""}" aria-live="polite">${saveLabel}</span>
          </div>
        </div>
      </article>
    `;
    }).join("") : '<div class="empty-state">No events yet. Log the first one on the left.</div>';

    timelineList.querySelectorAll("[data-event-body]").forEach((editor) => {
      editor.addEventListener("input", () => {
        setEditorMarkdown(editor, editorText(editor), true);
      });
    });

    timelineList.querySelectorAll("[data-event-now]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = button.dataset.eventNow;
        const input = timelineList.querySelector(`[data-event-time="${id}"]`);
        if (input) input.value = toLocalInputValue();
        recordTimelineDraft(Number(id));
      });
    });

    timelineList.querySelectorAll("[data-event-save]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = Number(button.dataset.eventSave);
        const eventData = state.events.find(e => e.id === id);
        if (!eventData) return;

        const editor = timelineList.querySelector(`[data-event-body="${id}"]`);
        const timeValue = timelineList.querySelector(`[data-event-time="${id}"]`).value;
        const endTimeValue = timelineList.querySelector(`[data-event-end-time="${id}"]`).value;
        const occurredAt = localInputValueToIso(timeValue);
        if (!occurredAt) {
          showToast("Timeline date and time is invalid.");
          return;
        }
        const endedAt = endTimeValue ? localInputValueToIso(endTimeValue) : null;
        if (endTimeValue && !endedAt) {
          showToast("Timeline end date and time is invalid.");
          return;
        }
        if (endedAt && new Date(endedAt) < new Date(occurredAt)) {
          showToast("Timeline end date and time must be after the start.");
          return;
        }
        const titleValue = timelineList.querySelector(`[data-event-title="${id}"]`).value;
        if (!titleValue.trim()) {
          showToast("Timeline title is required.");
          return;
        }
        const newState = {
          id,
          title: titleValue,
          body: editorText(editor),
          occurred_at: occurredAt,
          ended_at: endedAt,
        };

        const prevState = {
          id: eventData.id,
          title: eventData.title,
          body: eventData.body,
          occurred_at: eventData.occurred_at,
          ended_at: eventData.ended_at || null,
        };

        history.push(
          () => socket.emit("timeline:update", prevState),
          () => socket.emit("timeline:update", newState)
        );

        socket.emit("timeline:update", newState);
        setTimelineSaveIndicator(id, "saving");
      });
    });

    timelineList.querySelectorAll("[data-event-delete]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = Number(button.dataset.eventDelete);
        if (confirm("Are you sure you want to delete this timeline event?")) {
          socket.emit("timeline:delete", { id });
        }
      });
    });

    timelineList.querySelectorAll(".timeline-item").forEach((item) => attachTimelineDragHandlers(item, timelineList));
    applyTimelineDrafts();
    restoreTimelineFocus(focus);
    if (!scrolledToHash && location.hash.startsWith("#timeline-event-")) {
      const target = timelineList.querySelector(location.hash);
      if (target) {
        scrolledToHash = true;
        target.scrollIntoView({ block: "center" });
        target.classList.add("is-highlighted");
      }
    }
  }
  timelineForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const timeValue = eventTime.value;
    const endTimeValue = eventEndTime.value;
    const occurredAt = localInputValueToIso(timeValue);
    if (!occurredAt) {
      showToast("Timeline date and time is invalid.");
      return;
    }
    const endedAt = endTimeValue ? localInputValueToIso(endTimeValue) : null;
    if (endTimeValue && !endedAt) {
      showToast("Timeline end date and time is invalid.");
      return;
    }
    if (endedAt && new Date(endedAt) < new Date(occurredAt)) {
      showToast("Timeline end date and time must be after the start.");
      return;
    }
    socket.emit("timeline:create", {
      title: eventTitle.value,
      body: editorText(eventBody),
      occurred_at: occurredAt,
      ended_at: endedAt,
    });
    timelineForm.reset();
    eventBody.innerHTML = "";
    eventTime.value = toLocalInputValue();
  });
  eventTime.value = toLocalInputValue();
  pageRenderer = renderTimeline;
}

function attachTimelineDragHandlers(item, timelineList) {
  item.addEventListener("dragstart", (event) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", item.dataset.eventId);
    item.classList.add("dragging");
  });
  item.addEventListener("dragend", () => {
    item.classList.remove("dragging");
    timelineList.querySelectorAll(".timeline-item").forEach((row) => row.classList.remove("drop-before", "drop-after"));
  });
  item.addEventListener("dragover", (event) => {
    event.preventDefault();
    const zone = timelineDropZone(item, event.clientY);
    timelineList.querySelectorAll(".timeline-item").forEach((row) => row.classList.remove("drop-before", "drop-after"));
    item.classList.add(`drop-${zone}`);
  });
  item.addEventListener("drop", (event) => {
    event.preventDefault();
    const draggedId = Number(event.dataTransfer.getData("text/plain"));
    const targetId = Number(item.dataset.eventId);
    moveTimelineEvent(draggedId, targetId, timelineDropZone(item, event.clientY));
  });
}

function timelineDropZone(item, clientY) {
  const rect = item.getBoundingClientRect();
  return clientY - rect.top < rect.height / 2 ? "before" : "after";
}

function orderedEvents() {
  return [...state.events].sort((a, b) => {
    const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
    return orderDelta || eventTimeMs(b.occurred_at) - eventTimeMs(a.occurred_at);
  });
}

function moveTimelineEvent(draggedId, targetId, zone) {
  if (!draggedId || !targetId || draggedId === targetId) return;
  const events = orderedEvents().filter((event) => event.id !== draggedId);
  const dragged = state.events.find((event) => event.id === draggedId);
  const targetIndex = events.findIndex((event) => event.id === targetId);
  if (!dragged || targetIndex === -1) return;

  const prev = zone === "before" ? events[targetIndex - 1] : events[targetIndex];
  const next = zone === "before" ? events[targetIndex] : events[targetIndex + 1];
  let orderIndex = 0;
  if (prev && next) orderIndex = ((prev.order_index ?? 0) + (next.order_index ?? 0)) / 2;
  else if (prev) orderIndex = (prev.order_index ?? 0) + 1;
  else if (next) orderIndex = (next.order_index ?? 0) - 1;
  socket.emit("timeline:reorder", { id: dragged.id, order_index: orderIndex });
}

function initGraphPage() {
  const addNodeButton = document.querySelector("#add-node");
  const zoomInButton = document.querySelector("#zoom-in");
  const zoomOutButton = document.querySelector("#zoom-out");
  const zoomFitButton = document.querySelector("#zoom-fit");
  const zoomLevel = document.querySelector("#zoom-level");
  const graphNodePanel = document.querySelector("#graph-node-panel");
  const closePanelButton = document.querySelector("#close-panel");
  const contextMenu = document.querySelector("#graph-context-menu");
  const menuNewNode = document.querySelector("#menu-new-node");
  const menuOpenNode = document.querySelector("#menu-open-node");
  const menuRemoveNode = document.querySelector("#menu-remove-node");
  const menuDeleteNode = document.querySelector("#menu-delete-node");
  const menuResetLink = document.querySelector("#menu-reset-link");
  const menuDeleteLink = document.querySelector("#menu-delete-link");
  const graphCanvas = document.querySelector("#graph-canvas");
  const graphEmpty = document.querySelector("#graph-empty");
  const edgeLayer = document.querySelector("#edge-layer");
  const draftLayer = document.querySelector("#draft-layer");
  const nodeLayer = document.querySelector("#node-layer");
  const selectionBox = document.createElement("div");
  const renderEditor = initEditorSurface();

  const SVG_NS = "http://www.w3.org/2000/svg";
  const NODE_WIDTH = 184;
  const NODE_HEIGHT = 84;
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 2.5;
  const ARROW_LENGTH = 10;
  const CORNER_RADIUS = 10;
  const JOINT_SNAP_PX = 8;

  // Edges and nodes live in "world" coordinates inside layers that share one pan/zoom
  // transform, so what you see is exactly what you can click at any zoom level.
  const edgeWorld = document.createElementNS(SVG_NS, "g");
  const draftWorld = document.createElementNS(SVG_NS, "g");
  edgeLayer.appendChild(edgeWorld);
  draftLayer.appendChild(draftWorld);

  const nodeSizes = new Map();
  const edgeElements = new Map();
  let interaction = null;
  let lastPanMoved = false;
  let selectedEdgeId = null;
  selectionBox.className = "graph-selection-box";
  selectionBox.hidden = true;
  graphCanvas.appendChild(selectionBox);

  const screenToWorld = (clientX, clientY) => {
    const rect = graphCanvas.getBoundingClientRect();
    return {
      x: (clientX - rect.left - state.pan.x) / state.zoom,
      y: (clientY - rect.top - state.pan.y) / state.zoom,
    };
  };

  const findNode = (id) => state.nodes.find((node) => node.id === id);
  const findEdge = (id) => state.edges.find((edge) => edge.id === id);
  const isVisible = (node) => !!node && node.in_graph !== false;

  function svg(tag, className) {
    const element = document.createElementNS(SVG_NS, tag);
    if (className) element.setAttribute("class", className);
    return element;
  }

  function applyViewport() {
    const { x, y } = state.pan;
    nodeLayer.style.transform = `translate(${x}px, ${y}px) scale(${state.zoom})`;
    const svgTransform = `translate(${x} ${y}) scale(${state.zoom})`;
    edgeWorld.setAttribute("transform", svgTransform);
    draftWorld.setAttribute("transform", svgTransform);
    // Keep the dot grid attached to the world, but never denser than ~11px
    let gridSize = 22 * state.zoom;
    while (gridSize < 11) gridSize *= 2;
    graphCanvas.style.backgroundSize = `${gridSize}px ${gridSize}px`;
    graphCanvas.style.backgroundPosition = `${x}px ${y}px`;
    zoomLevel.textContent = `${Math.round(state.zoom * 100)}%`;
  }

  function nodeRect(node) {
    const size = nodeSizes.get(node.id) || { w: NODE_WIDTH, h: NODE_HEIGHT };
    return {
      left: node.x,
      top: node.y,
      right: node.x + size.w,
      bottom: node.y + size.h,
      cx: node.x + size.w / 2,
      cy: node.y + size.h / 2,
    };
  }

  // Elbow routing in the style of the MITRE Attack Flow Builder: the line leaves the
  // source, runs to the joint's column (or row), turns, and enters the target. The joint
  // sits in the middle of that crossing segment and slides along one axis.
  function edgeRoute(edge, source, target) {
    const s = nodeRect(source);
    const t = nodeRect(target);
    const gapX = Math.max(t.left - s.right, s.left - t.right);
    const gapY = Math.max(t.top - s.bottom, s.top - t.bottom);
    if (gapX >= gapY) return elbowRoute(s, t, edge.joint_x);

    // Vertical elbow: route in transposed space and swap the axes back
    const flipRect = (r) => ({ left: r.top, right: r.bottom, top: r.left, bottom: r.right, cx: r.cy, cy: r.cx });
    const flipPoint = (p) => ({ x: p.y, y: p.x });
    const route = elbowRoute(flipRect(s), flipRect(t), edge.joint_y);
    return {
      axis: "y",
      points: route.points.map(flipPoint),
      mid: route.mid.map(flipPoint),
      joint: flipPoint(route.joint),
      endDir: flipPoint(route.endDir),
    };
  }

  // Horizontal elbow through column jx. If the column passes over a node, the line
  // leaves/enters through that node's top or bottom instead of running underneath it.
  function elbowRoute(s, t, jointX) {
    const jx = jointX ?? (t.cx >= s.cx ? (s.right + t.left) / 2 : (s.left + t.right) / 2);
    const overSource = jx > s.left && jx < s.right && (t.cy < s.top || t.cy > s.bottom);
    const overTarget = jx > t.left && jx < t.right && (s.cy < t.top || s.cy > t.bottom);
    const points = [];
    const runStart = { x: jx, y: overSource ? (t.cy > s.cy ? s.bottom : s.top) : s.cy };
    if (!overSource) points.push({ x: jx >= s.cx ? s.right : s.left, y: s.cy });
    points.push(runStart);

    let runEnd;
    let endDir;
    if (overTarget) {
      const down = s.cy < t.cy;
      runEnd = { x: jx, y: down ? t.top : t.bottom };
      points.push(runEnd);
      endDir = { x: 0, y: down ? 1 : -1 };
    } else {
      const fromLeft = jx < t.cx;
      runEnd = { x: jx, y: t.cy };
      points.push(runEnd, { x: fromLeft ? t.left : t.right, y: t.cy });
      endDir = { x: fromLeft ? 1 : -1, y: 0 };
    }
    return {
      axis: "x",
      points,
      mid: [runStart, runEnd],
      joint: { x: jx, y: (runStart.y + runEnd.y) / 2 },
      endDir,
    };
  }

  function roundedPath(points, radius = CORNER_RADIUS) {
    const pts = [];
    for (const point of points) {
      const last = pts[pts.length - 1];
      if (!last || Math.hypot(point.x - last.x, point.y - last.y) > 0.5) pts.push(point);
    }
    if (pts.length < 2) return "";
    let d = `M ${pts[0].x} ${pts[0].y}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const prev = pts[i - 1];
      const cur = pts[i];
      const next = pts[i + 1];
      const inLength = Math.hypot(cur.x - prev.x, cur.y - prev.y);
      const outLength = Math.hypot(next.x - cur.x, next.y - cur.y);
      const r = Math.min(radius, inLength / 2, outLength / 2);
      const a = { x: cur.x - ((cur.x - prev.x) / inLength) * r, y: cur.y - ((cur.y - prev.y) / inLength) * r };
      const b = { x: cur.x + ((next.x - cur.x) / outLength) * r, y: cur.y + ((next.y - cur.y) / outLength) * r };
      d += ` L ${a.x} ${a.y} Q ${cur.x} ${cur.y} ${b.x} ${b.y}`;
    }
    const end = pts[pts.length - 1];
    return `${d} L ${end.x} ${end.y}`;
  }

  function arrowPath(tip, dir) {
    const base = { x: tip.x - dir.x * ARROW_LENGTH, y: tip.y - dir.y * ARROW_LENGTH };
    const half = ARROW_LENGTH * 0.55;
    return `M ${tip.x} ${tip.y} L ${base.x - dir.y * half} ${base.y + dir.x * half} L ${base.x + dir.y * half} ${base.y - dir.x * half} Z`;
  }

  function edgeElementsFor(edge) {
    let parts = edgeElements.get(edge.id);
    if (parts) return parts;
    const group = svg("g", "graph-edge");
    group.dataset.edgeId = edge.id;
    parts = {
      group,
      hit: svg("path", "edge-hit"),
      line: svg("path", "edge-line"),
      arrow: svg("path", "edge-arrow"),
      midHit: svg("path", "edge-mid-hit"),
      joint: svg("circle", "edge-joint"),
    };
    group.append(parts.hit, parts.line, parts.arrow, parts.midHit, parts.joint);
    edgeWorld.appendChild(group);
    edgeElements.set(edge.id, parts);
    return parts;
  }

  function renderEdges() {
    const seen = new Set();
    for (const edge of state.edges) {
      const source = findNode(edge.source_id);
      const target = findNode(edge.target_id);
      if (!isVisible(source) || !isVisible(target)) continue;
      seen.add(edge.id);

      const route = edgeRoute(edge, source, target);
      const tip = route.points[route.points.length - 1];
      const linePoints = [...route.points.slice(0, -1), { x: tip.x - route.endDir.x * (ARROW_LENGTH - 1), y: tip.y - route.endDir.y * (ARROW_LENGTH - 1) }];
      const d = roundedPath(linePoints);
      const parts = edgeElementsFor(edge);
      parts.group.classList.toggle("selected", edge.id === selectedEdgeId);
      parts.group.classList.toggle("has-joint", edge.joint_x != null || edge.joint_y != null);
      parts.group.dataset.axis = route.axis;
      parts.hit.setAttribute("d", d);
      parts.line.setAttribute("d", d);
      parts.arrow.setAttribute("d", arrowPath(tip, route.endDir));
      parts.midHit.setAttribute("d", `M ${route.mid[0].x} ${route.mid[0].y} L ${route.mid[1].x} ${route.mid[1].y}`);
      parts.joint.setAttribute("cx", route.joint.x);
      parts.joint.setAttribute("cy", route.joint.y);
      parts.joint.setAttribute("r", 5.5 / state.zoom);
    }
    edgeElements.forEach((parts, id) => {
      if (seen.has(id)) return;
      parts.group.remove();
      edgeElements.delete(id);
    });
  }

  function positionNodeElement(node) {
    const element = nodeLayer.querySelector(`[data-node-id="${node.id}"]`);
    if (!element) return;
    element.style.left = `${node.x}px`;
    element.style.top = `${node.y}px`;
  }

  function renderGraphEditor() {
    const node = currentNode();
    graphNodePanel.hidden = !node;
    document.querySelector(".graph-layout")?.classList.toggle("panel-open", !!node);

    // Clear previous color classes
    graphNodePanel.classList.remove("color-red", "color-orange", "color-yellow", "color-green", "color-blue", "color-pink");

    if (node) {
      if (node.color) graphNodePanel.classList.add(`color-${node.color}`);
    }
    // Always call so a deselected node's pending edits get flushed
    renderEditor();
  }

  function setGraphSelection(ids, activeId = null) {
    state.selectedNodeIds = new Set(ids);
    state.activeNodeId = activeId ?? ids[ids.length - 1] ?? null;
    if (ids.length) selectEdge(null);
  }

  function toggleGraphSelection(nodeId) {
    if (state.selectedNodeIds.has(nodeId)) {
      state.selectedNodeIds.delete(nodeId);
      if (state.activeNodeId === nodeId) {
        const selectedIds = [...state.selectedNodeIds];
        state.activeNodeId = selectedIds[selectedIds.length - 1] ?? null;
      }
    } else {
      state.selectedNodeIds.add(nodeId);
      state.activeNodeId = nodeId;
      selectEdge(null);
    }
  }

  function selectEdge(edgeId) {
    if (selectedEdgeId === edgeId) return;
    selectedEdgeId = edgeId;
    edgeElements.forEach((parts, id) => parts.group.classList.toggle("selected", id === edgeId));
  }

  function refreshGraphSelectionStyles(updateEditor = true) {
    nodeLayer.querySelectorAll(".graph-node").forEach((element) => {
      const nodeId = Number(element.dataset.nodeId);
      element.classList.toggle("selected", state.selectedNodeIds.has(nodeId));
      element.classList.toggle("active", nodeId === state.activeNodeId);
    });
    if (updateEditor) renderGraphEditor();
  }

  function selectionRect(startX, startY, endX, endY) {
    return {
      left: Math.min(startX, endX),
      top: Math.min(startY, endY),
      right: Math.max(startX, endX),
      bottom: Math.max(startY, endY),
    };
  }

  function updateSelectionBox(rect) {
    const canvasRect = graphCanvas.getBoundingClientRect();
    selectionBox.hidden = false;
    selectionBox.style.left = `${rect.left - canvasRect.left}px`;
    selectionBox.style.top = `${rect.top - canvasRect.top}px`;
    selectionBox.style.width = `${rect.right - rect.left}px`;
    selectionBox.style.height = `${rect.bottom - rect.top}px`;
  }

  function rectsIntersect(a, b) {
    return a.left <= b.right && a.right >= b.left && a.top <= b.bottom && a.bottom >= b.top;
  }

  function nodeIdsInSelectionRect(rect) {
    return Array.from(nodeLayer.querySelectorAll(".graph-node"))
      .filter((element) => rectsIntersect(rect, element.getBoundingClientRect()))
      .map((element) => Number(element.dataset.nodeId));
  }

  function zoomToFit() {
    const nodes = state.nodes.filter(isVisible);
    const rect = graphCanvas.getBoundingClientRect();
    if (!nodes.length || !rect.width || !rect.height) return;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach((node) => {
      const box = nodeRect(node);
      minX = Math.min(minX, box.left);
      minY = Math.min(minY, box.top);
      maxX = Math.max(maxX, box.right);
      maxY = Math.max(maxY, box.bottom);
    });

    const padding = 60;
    const width = maxX - minX;
    const height = maxY - minY;

    const zoomX = rect.width / (width + padding * 2);
    const zoomY = rect.height / (height + padding * 2);
    state.zoom = Math.max(MIN_ZOOM, Math.min(1.5, Math.min(zoomX, zoomY)));

    state.pan.x = (rect.width - width * state.zoom) / 2 - minX * state.zoom;
    state.pan.y = (rect.height - height * state.zoom) / 2 - minY * state.zoom;

    renderEdges();
    applyViewport();
  }

  function setZoom(nextZoom, originClientX = null, originClientY = null) {
    const rect = graphCanvas.getBoundingClientRect();
    const originX = originClientX ?? rect.left + rect.width / 2;
    const originY = originClientY ?? rect.top + rect.height / 2;
    const before = screenToWorld(originX, originY);
    state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, nextZoom));
    state.pan.x = originX - rect.left - before.x * state.zoom;
    state.pan.y = originY - rect.top - before.y * state.zoom;
    renderEdges();
    applyViewport();
  }

  function setInteraction(next) {
    interaction = next;
    graphCanvas.dataset.interaction = next ? next.type : "";
  }

  function startNodeInteraction(event, node) {
    if (event.button !== 0) return;
    event.preventDefault();
    closeContextMenu();
    const world = screenToWorld(event.clientX, event.clientY);
    const isMultiSelect = event.ctrlKey || event.metaKey || event.shiftKey;
    const wasSelected = state.selectedNodeIds.has(node.id);
    let moveIds = wasSelected ? [...state.selectedNodeIds] : [node.id];
    if (!wasSelected && isMultiSelect) moveIds = [...state.selectedNodeIds, node.id];
    const moveStart = moveIds
      .map(findNode)
      .filter(Boolean)
      .map((item) => ({ id: item.id, x: item.x, y: item.y }));
    // Lines whose both ends move together carry their joint along
    const movingIds = new Set(moveStart.map((item) => item.id));
    const jointStart = state.edges
      .filter((edge) => movingIds.has(edge.source_id) && movingIds.has(edge.target_id) && (edge.joint_x != null || edge.joint_y != null))
      .map((edge) => ({ id: edge.id, joint_x: edge.joint_x, joint_y: edge.joint_y }));
    setInteraction({
      type: "move",
      node,
      isMultiSelect,
      wasSelected,
      moveStart,
      jointStart,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      anchorStartX: node.x,
      anchorStartY: node.y,
      offsetX: world.x - node.x,
      offsetY: world.y - node.y,
      moved: false,
    });
  }

  function startLinkInteraction(event, node, side) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    closeContextMenu();
    setInteraction({ type: "link", node, side, pointerId: event.pointerId, targetId: null });
  }

  function linkTargetAt(clientX, clientY, sourceId) {
    const element = document.elementsFromPoint(clientX, clientY)
      .map((item) => item.closest?.(".graph-node"))
      .find((item) => item && Number(item.dataset.nodeId) !== sourceId);
    return element ? Number(element.dataset.nodeId) : null;
  }

  function renderLinkDraft(source, side, world, targetId) {
    draftWorld.innerHTML = "";
    const s = nodeRect(source);
    const target = targetId ? findNode(targetId) : null;
    let points;
    let endDir;
    if (target) {
      const route = edgeRoute({ joint_x: null, joint_y: null }, source, target);
      points = route.points;
      endDir = route.endDir;
    } else {
      // Leave from the anchor that was grabbed, then turn once toward the pointer
      const anchors = {
        left: { x: s.left, y: s.cy },
        right: { x: s.right, y: s.cy },
        top: { x: s.cx, y: s.top },
        bottom: { x: s.cx, y: s.bottom },
      };
      const start = anchors[side] || anchors.right;
      const horizontalFirst = side !== "top" && side !== "bottom";
      const corner = horizontalFirst ? { x: world.x, y: start.y } : { x: start.x, y: world.y };
      points = [start, corner, { x: world.x, y: world.y }];
      endDir = horizontalFirst
        ? { x: 0, y: world.y >= start.y ? 1 : -1 }
        : { x: world.x >= start.x ? 1 : -1, y: 0 };
      const lastLeg = horizontalFirst ? Math.abs(world.y - start.y) : Math.abs(world.x - start.x);
      if (lastLeg < ARROW_LENGTH) {
        points = [start, { x: world.x, y: world.y }];
        endDir = horizontalFirst ? { x: world.x >= start.x ? 1 : -1, y: 0 } : { x: 0, y: world.y >= start.y ? 1 : -1 };
      }
    }
    const tip = points[points.length - 1];
    const line = svg("path", "edge-draft");
    line.setAttribute("d", roundedPath([...points.slice(0, -1), { x: tip.x - endDir.x * (ARROW_LENGTH - 1), y: tip.y - endDir.y * (ARROW_LENGTH - 1) }]));
    const arrow = svg("path", "edge-draft-arrow");
    arrow.setAttribute("d", arrowPath(tip, endDir));
    draftWorld.append(line, arrow);
  }

  function setLinkTargetHighlight(targetId) {
    nodeLayer.querySelectorAll(".graph-node.link-target").forEach((element) => element.classList.remove("link-target"));
    if (targetId) nodeLayer.querySelector(`[data-node-id="${targetId}"]`)?.classList.add("link-target");
  }

  function updateEdgeJoint(edge, jointX, jointY, previous) {
    const next = { id: edge.id, joint_x: jointX, joint_y: jointY };
    const before = { id: edge.id, joint_x: previous.joint_x ?? null, joint_y: previous.joint_y ?? null };
    edge.joint_x = jointX;
    edge.joint_y = jointY;
    renderEdges();
    history.push(
      () => socket.emit("edge:update", before),
      () => socket.emit("edge:update", next)
    );
    socket.emit("edge:update", next);
  }

  function resetEdgeJoint(edge) {
    if (edge.joint_x == null && edge.joint_y == null) return;
    updateEdgeJoint(edge, null, null, { joint_x: edge.joint_x, joint_y: edge.joint_y });
  }

  function deleteEdge(edge) {
    const edgeState = { ...edge };
    history.push(
      () => socket.emit("edge:create", { source_id: edgeState.source_id, target_id: edgeState.target_id, joint_x: edgeState.joint_x, joint_y: edgeState.joint_y }),
      () => {
        const current = state.edges.find((item) => item.source_id === edgeState.source_id && item.target_id === edgeState.target_id);
        if (current) socket.emit("edge:delete", { id: current.id });
      }
    );
    socket.emit("edge:delete", { id: edge.id });
    if (selectedEdgeId === edge.id) selectEdge(null);
  }

  function createGraphNode(world) {
    createNode({
      in_graph: true,
      order_index: nextOrderIndex(null),
      x: Math.round(world.x - NODE_WIDTH / 2),
      y: Math.round(world.y - NODE_HEIGHT / 2),
    }, (node) => {
      setGraphSelection([node.id], node.id);
      renderGraph();
      focusTitleSoon();
    });
  }

  function renderGraph() {
    nodeLayer.innerHTML = "";
    const visibleNodes = state.nodes.filter(isVisible);
    for (const node of visibleNodes) {
      const element = document.createElement("div");
      const selected = state.selectedNodeIds.has(node.id);
      element.className = `graph-node ${selected ? "selected" : ""} ${node.id === state.activeNodeId ? "active" : ""} ${node.color ? `color-${node.color}` : ""}`;
      element.dataset.nodeId = node.id;
      element.innerHTML = `
        <div class="node-open">
          <strong>${escapeHtml(node.title)}</strong>
          ${node.caption ? `<span>${escapeHtml(node.caption)}</span>` : ""}
        </div>
        ${["top", "right", "bottom", "left"].map((side) => `<button class="node-link-handle" data-side="${side}" type="button" title="Drag to link to another node" aria-label="Link from ${side}"></button>`).join("")}
      `;
      element.addEventListener("pointerdown", (event) => startNodeInteraction(event, node));
      element.querySelectorAll(".node-link-handle").forEach((handle) => {
        handle.addEventListener("pointerdown", (event) => startLinkInteraction(event, node, handle.dataset.side));
      });
      nodeLayer.appendChild(element);
      positionNodeElement(node);
    }
    // Measure real (unscaled) sizes so lines attach to the visible border of each node
    nodeSizes.clear();
    nodeLayer.querySelectorAll(".graph-node").forEach((element) => {
      nodeSizes.set(Number(element.dataset.nodeId), { w: element.offsetWidth, h: element.offsetHeight });
    });
    if (selectedEdgeId && !findEdge(selectedEdgeId)) selectedEdgeId = null;
    if (graphEmpty) graphEmpty.hidden = visibleNodes.length > 0;
    renderEdges();
    applyViewport();
    renderGraphEditor();
  }

  closePanelButton?.addEventListener("click", () => {
    state.activeNodeId = null;
    state.selectedNodeIds.clear();
    renderGraph();
  });

  graphCanvas.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    if (lastPanMoved) {
      lastPanMoved = false;
      return;
    }
    const nodeElement = event.target.closest(".graph-node");
    const nodeId = nodeElement ? Number(nodeElement.dataset.nodeId) : null;
    const edgeElement = nodeId ? null : event.target.closest(".graph-edge");
    const edge = edgeElement ? findEdge(Number(edgeElement.dataset.edgeId)) : null;
    if (edge) selectEdge(edge.id);

    menuNewNode.hidden = !!(nodeId || edge);
    menuOpenNode.hidden = !nodeId;
    menuRemoveNode.hidden = !nodeId;
    menuDeleteNode.hidden = !nodeId;
    menuResetLink.hidden = !edge || (edge.joint_x == null && edge.joint_y == null);
    menuDeleteLink.hidden = !edge;

    const world = screenToWorld(event.clientX, event.clientY);
    menuNewNode.onclick = () => createGraphNode(world);
    menuOpenNode.onclick = () => {
      if (typeof window.flushPendingSave === "function") window.flushPendingSave();
      window.location.href = `/nodes/${nodeId}`;
    };
    const deselect = () => {
      state.selectedNodeIds.delete(nodeId);
      if (state.activeNodeId === nodeId) state.activeNodeId = null;
    };
    menuRemoveNode.onclick = () => {
      deselect();
      removeFromGraphWithUndo(nodeId);
    };
    menuDeleteNode.onclick = () => {
      deselect();
      deleteNodeWithUndo(nodeId);
    };
    menuResetLink.onclick = () => edge && resetEdgeJoint(edge);
    menuDeleteLink.onclick = () => edge && deleteEdge(edge);

    openContextMenu(contextMenu, event.clientX, event.clientY);
  });

  graphCanvas.addEventListener("pointerdown", (event) => {
    if (event.target.closest(".graph-node")) return; // Nodes handle their own pointerdown
    if (document.activeElement && graphNodePanel.contains(document.activeElement)) document.activeElement.blur();

    // Right or middle button pans (a right click without movement still opens the context menu)
    if (event.button === 1 || event.button === 2) {
      if (event.button === 2) lastPanMoved = false;
      event.preventDefault();
      setInteraction({
        type: "pan",
        pointerId: event.pointerId,
        startX: event.clientX,
        startY: event.clientY,
        panX: state.pan.x,
        panY: state.pan.y,
        moved: false,
      });
      return;
    }
    if (event.button !== 0) return;
    event.preventDefault();

    const edgeElement = event.target.closest(".graph-edge");
    if (edgeElement) {
      const edge = findEdge(Number(edgeElement.dataset.edgeId));
      if (!edge) return;
      if (state.selectedNodeIds.size || state.activeNodeId) {
        setGraphSelection([], null);
        refreshGraphSelectionStyles();
      }
      selectEdge(edge.id);
      // Dragging the joint (or the segment it sits on) bends the line
      if (event.target.closest(".edge-joint, .edge-mid-hit")) {
        const source = findNode(edge.source_id);
        const target = findNode(edge.target_id);
        const route = edgeRoute(edge, source, target);
        const world = screenToWorld(event.clientX, event.clientY);
        setInteraction({
          type: "joint",
          edgeId: edge.id,
          axis: route.axis,
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          grab: route.axis === "x" ? world.x - route.joint.x : world.y - route.joint.y,
          before: { joint_x: edge.joint_x, joint_y: edge.joint_y },
          moved: false,
        });
      }
      return;
    }

    selectEdge(null);
    setInteraction({
      type: "select",
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      additive: event.ctrlKey || event.metaKey || event.shiftKey,
      baseIds: [...state.selectedNodeIds],
      moved: false,
    });
  });

  graphCanvas.addEventListener("dblclick", (event) => {
    const edgeElement = event.target.closest(".edge-joint, .edge-mid-hit") && event.target.closest(".graph-edge");
    if (edgeElement) {
      const edge = findEdge(Number(edgeElement.dataset.edgeId));
      if (edge) resetEdgeJoint(edge);
      return;
    }
    if (!event.target.closest(".graph-node, .graph-edge")) createGraphNode(screenToWorld(event.clientX, event.clientY));
  });

  document.addEventListener("pointermove", (event) => {
    if (!interaction || event.pointerId !== interaction.pointerId) return;
    const distance = Math.hypot(event.clientX - interaction.startX, event.clientY - interaction.startY);
    if (interaction.type === "move") {
      const world = screenToWorld(event.clientX, event.clientY);
      const nextX = Math.max(-4000, Math.min(4000, world.x - interaction.offsetX));
      const nextY = Math.max(-4000, Math.min(4000, world.y - interaction.offsetY));
      const deltaX = nextX - interaction.anchorStartX;
      const deltaY = nextY - interaction.anchorStartY;
      if (!interaction.moved && distance <= 4) return;
      interaction.moved = true;
      interaction.moveStart.forEach((start) => {
        const movingNode = findNode(start.id);
        if (!movingNode) return;
        movingNode.x = Math.max(-4000, Math.min(4000, start.x + deltaX));
        movingNode.y = Math.max(-4000, Math.min(4000, start.y + deltaY));
        positionNodeElement(movingNode);
      });
      interaction.jointStart.forEach((start) => {
        const edge = findEdge(start.id);
        if (!edge) return;
        edge.joint_x = start.joint_x == null ? null : start.joint_x + deltaX;
        edge.joint_y = start.joint_y == null ? null : start.joint_y + deltaY;
      });
      renderEdges();
    }
    if (interaction.type === "joint") {
      if (!interaction.moved && distance <= 3) return;
      interaction.moved = true;
      const edge = findEdge(interaction.edgeId);
      const source = edge && findNode(edge.source_id);
      const target = edge && findNode(edge.target_id);
      if (!source || !target) return;
      const world = screenToWorld(event.clientX, event.clientY);
      const key = interaction.axis === "x" ? "joint_x" : "joint_y";
      const value = (interaction.axis === "x" ? world.x : world.y) - interaction.grab;
      // Snap back to the automatic (centered) position when close to it
      const automatic = edgeRoute({ joint_x: null, joint_y: null }, source, target);
      const automaticValue = automatic.axis === interaction.axis ? automatic.joint[interaction.axis] : null;
      edge[key] = automaticValue != null && Math.abs(value - automaticValue) * state.zoom < JOINT_SNAP_PX ? null : Math.round(value);
      renderEdges();
    }
    if (interaction.type === "link") {
      const world = screenToWorld(event.clientX, event.clientY);
      interaction.targetId = linkTargetAt(event.clientX, event.clientY, interaction.node.id);
      setLinkTargetHighlight(interaction.targetId);
      renderLinkDraft(interaction.node, interaction.side, world, interaction.targetId);
    }
    if (interaction.type === "pan") {
      state.pan.x = interaction.panX + event.clientX - interaction.startX;
      state.pan.y = interaction.panY + event.clientY - interaction.startY;
      interaction.moved = interaction.moved || distance > 5;
      applyViewport();
    }
    if (interaction.type === "select") {
      const rect = selectionRect(interaction.startX, interaction.startY, event.clientX, event.clientY);
      interaction.moved = interaction.moved || distance > 4;
      if (!interaction.moved) return;
      updateSelectionBox(rect);
      const selectedIds = nodeIdsInSelectionRect(rect);
      const nextIds = interaction.additive ? [...new Set([...interaction.baseIds, ...selectedIds])] : selectedIds;
      setGraphSelection(nextIds, nextIds[nextIds.length - 1] ?? null);
      refreshGraphSelectionStyles(false);
    }
  });

  function finishInteraction(event, cancelled = false) {
    if (!interaction || (event && event.pointerId !== interaction.pointerId)) return;
    if (interaction.type === "pan" && interaction.moved) lastPanMoved = true;
    const done = interaction;
    setInteraction(null);
    draftWorld.innerHTML = "";
    setLinkTargetHighlight(null);
    selectionBox.hidden = true;

    if (done.type === "move") {
      if (done.moved && cancelled) {
        done.moveStart.forEach((start) => {
          const movedNode = findNode(start.id);
          if (!movedNode) return;
          movedNode.x = start.x;
          movedNode.y = start.y;
          positionNodeElement(movedNode);
        });
        done.jointStart.forEach((start) => {
          const edge = findEdge(start.id);
          if (edge) Object.assign(edge, { joint_x: start.joint_x, joint_y: start.joint_y });
        });
        renderEdges();
      } else if (done.moved) {
        if (!state.selectedNodeIds.has(done.node.id)) {
          setGraphSelection(done.moveStart.map((item) => item.id), done.node.id);
          refreshGraphSelectionStyles();
        }
        const before = done.moveStart.map((start) => ({ ...start }));
        const after = done.moveStart.map((start) => {
          const movedNode = findNode(start.id);
          return movedNode ? { id: movedNode.id, x: movedNode.x, y: movedNode.y } : null;
        }).filter(Boolean);
        const jointsBefore = done.jointStart.map((start) => ({ ...start }));
        const jointsAfter = done.jointStart.map((start) => {
          const edge = findEdge(start.id);
          return edge ? { id: edge.id, joint_x: edge.joint_x, joint_y: edge.joint_y } : null;
        }).filter(Boolean);
        const emitAll = (positions, joints) => {
          positions.forEach((position) => socket.emit("node:update", position));
          joints.forEach((joint) => socket.emit("edge:update", joint));
        };
        history.push(() => emitAll(before, jointsBefore), () => emitAll(after, jointsAfter));
        emitAll(after, jointsAfter);
      } else if (!cancelled) {
        if (done.isMultiSelect) {
          toggleGraphSelection(done.node.id);
        } else {
          setGraphSelection([done.node.id], done.node.id);
        }
        refreshGraphSelectionStyles();
      }
    }
    if (done.type === "joint" && done.moved) {
      const edge = findEdge(done.edgeId);
      if (edge) {
        if (cancelled) {
          edge.joint_x = done.before.joint_x;
          edge.joint_y = done.before.joint_y;
          renderEdges();
        } else {
          updateEdgeJoint(edge, edge.joint_x ?? null, edge.joint_y ?? null, done.before);
        }
      }
    }
    if (done.type === "link" && !cancelled) {
      const targetId = event ? linkTargetAt(event.clientX, event.clientY, done.node.id) : null;
      if (targetId) {
        const exists = state.edges.some((edge) => edge.source_id === done.node.id && edge.target_id === targetId);
        if (exists) {
          showToast("Those nodes are already linked.");
        } else {
          const edgeData = { source_id: done.node.id, target_id: targetId };
          history.push(
            () => {
              const edge = state.edges.find(e => e.source_id === edgeData.source_id && e.target_id === edgeData.target_id);
              if (edge) socket.emit("edge:delete", { id: edge.id });
            },
            () => socket.emit("edge:create", edgeData)
          );
          socket.emit("edge:create", edgeData);
        }
      }
    }
    if (done.type === "select") {
      if (!done.moved && !done.additive) {
        setGraphSelection([], null);
      }
      refreshGraphSelectionStyles();
    }
  }

  document.addEventListener("pointerup", (event) => finishInteraction(event));
  document.addEventListener("pointercancel", (event) => finishInteraction(event, true));

  document.addEventListener("keydown", (event) => {
    if (isEditableTarget(event.target)) return;
    if (event.key === "Escape") {
      if (interaction) {
        finishInteraction(null, true);
        return;
      }
      selectEdge(null);
      if (state.selectedNodeIds.size || state.activeNodeId) {
        setGraphSelection([], null);
        refreshGraphSelectionStyles();
      }
    }
    if ((event.key === "Delete" || event.key === "Backspace") && selectedEdgeId) {
      const edge = findEdge(selectedEdgeId);
      if (!edge) return;
      event.preventDefault();
      deleteEdge(edge);
    }
  });

  addNodeButton.addEventListener("click", () => {
    const rect = graphCanvas.getBoundingClientRect();
    createGraphNode(screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2));
  });
  zoomInButton.addEventListener("click", () => setZoom(state.zoom * 1.2));
  zoomOutButton.addEventListener("click", () => setZoom(state.zoom / 1.2));
  zoomFitButton.addEventListener("click", () => zoomToFit());
  graphCanvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    closeContextMenu();
    // Proportional zoom: smooth on trackpads, ~15% per notch on a mouse wheel
    const delta = event.deltaMode === 1 ? event.deltaY * 33 : event.deltaY;
    setZoom(state.zoom * Math.exp(-Math.max(-100, Math.min(100, delta)) * 0.0015), event.clientX, event.clientY);
  }, { passive: false });

  applyViewport();
  window.addEventListener("resize", renderGraph);
  pageRenderer = renderGraph;
  window.zoomToFit = zoomToFit;
}

function initImagesPage() {
  const gallery = document.querySelector("#image-gallery");
  const viewer = document.querySelector("#image-viewer");
  const viewerImg = document.querySelector("#viewer-img");
  const viewerClose = document.querySelector(".viewer-close");
  const viewerCopyMd = document.querySelector("#viewer-copy-md");
  const backdrop = document.querySelector(".viewer-backdrop");

  if (!gallery) return;

  let currentImgUrl = "";

  async function renderGallery() {
    try {
      const response = await fetch("/api/images");
      const images = await response.json();

      if (!images.length) {
        gallery.innerHTML = '<div class="empty-state">No images in vault. Paste or drag images into notes to add them.</div>';
        return;
      }

      gallery.innerHTML = images.map(img => `
        <div class="image-card" data-url="${img.url}" title="Click to view">
          <img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.filename)}" loading="lazy">
          <div class="image-info">
            <span class="filename" title="${escapeHtml(img.filename)}">${escapeHtml(img.filename)}</span>
            <button class="delete-image" data-filename="${img.filename}">Delete</button>
          </div>
        </div>
      `).join("");

      gallery.querySelectorAll(".image-card").forEach(card => {
        card.addEventListener("click", (e) => {
          if (e.target.classList.contains("delete-image")) return;
          currentImgUrl = card.dataset.url;
          viewerImg.src = currentImgUrl;
          viewer.hidden = false;
        });
      });

      gallery.querySelectorAll(".delete-image").forEach(btn => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm("Are you sure you want to delete this image from the vault? This cannot be undone.")) return;
          const response = await apiFetch(`/api/images/${encodeURIComponent(btn.dataset.filename)}`, { method: "DELETE" });
          if (response.ok) {
            showToast("Image deleted");
            renderGallery();
          } else {
            const payload = await response.json().catch(() => ({}));
            showToast(payload.error || "Delete failed");
          }
        });
      });
    } catch (err) {
      gallery.innerHTML = '<div class="error">Failed to load images.</div>';
    }
  }

  const closeViewer = () => {
    viewer.hidden = true;
    viewerImg.src = "";
  };

  viewerClose?.addEventListener("click", closeViewer);
  backdrop?.addEventListener("click", closeViewer);

  viewerCopyMd?.addEventListener("click", () => {
    const md = `![image](${currentImgUrl})`;
    navigator.clipboard.writeText(md).then(() => showToast("Markdown copied to clipboard"));
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !viewer.hidden) closeViewer();
  });

  renderGallery();
}

function initFilesPage() {
  const fileList = document.querySelector("#file-list");
  if (!fileList) return;

  async function renderFiles() {
    try {
      const response = await fetch("/api/files");
      const files = await response.json();

      if (!files.length) {
        fileList.innerHTML = '<div class="empty-state">No files in vault. Paste or drag files into notes to add them.</div>';
        return;
      }

      fileList.innerHTML = files.map(file => {
        const size = (file.size / 1024).toFixed(1) + " KB";
        return `
          <div class="image-card file-card" title="Click to copy markdown link" data-url="${file.url}" data-filename="${file.filename}">
            <div class="file-icon">File</div>
            <div class="image-info">
              <span class="filename" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename)}</span>
              <span class="filesize">${size}</span>
              <button class="delete-file" data-filename="${file.filename}">Delete</button>
            </div>
          </div>
        `;
      }).join("");

      fileList.querySelectorAll(".file-card").forEach(card => {
        card.addEventListener("click", (e) => {
          if (e.target.classList.contains("delete-file")) return;
          const md = `[Download ${card.dataset.filename}](${card.dataset.url})`;
          navigator.clipboard.writeText(md).then(() => showToast("Markdown link copied"));
        });
      });

      fileList.querySelectorAll(".delete-file").forEach(btn => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm("Are you sure you want to delete this file?")) return;
          const response = await apiFetch(`/api/files/${encodeURIComponent(btn.dataset.filename)}`, { method: "DELETE" });
          if (response.ok) {
            showToast("File deleted");
            renderFiles();
          } else {
            const payload = await response.json().catch(() => ({}));
            showToast(payload.error || "Delete failed");
          }
        });
      });
    } catch (err) {
      fileList.innerHTML = '<div class="error">Failed to load files.</div>';
    }
  }

  renderFiles();
}

function renderCurrentPage() {
  if (typeof pageRenderer === "function") pageRenderer();
}

socket.on("timeline:created", (event) => {
  upsert(state.events, event);
  renderCurrentPage();
});

socket.on("timeline:updated", (event) => {
  const draft = timelineDrafts.get(event.id);
  if (draft && event.origin && event.origin !== socket.id) draft.conflictBy = event.edited_by || "Someone";
  upsert(state.events, event);
  const savedByMe = timelineSaveIndicators.get(event.id) === "saving";
  if (savedByMe) timelineDrafts.delete(event.id);
  renderCurrentPage();
  if (savedByMe) setTimelineSaveIndicator(event.id, "saved");
});

socket.on("timeline:deleted", (payload) => {
  state.events = state.events.filter(e => e.id !== payload.id);
  timelineDrafts.delete(payload.id);
  renderCurrentPage();
});

socket.on("timeline:reset", (payload) => {
  state.events = payload.events || [];
  timelineSaveIndicators.clear();
  timelineSaveTimers.forEach((timer) => clearTimeout(timer));
  timelineSaveTimers.clear();
  renderCurrentPage();
  showToast("Timeline auto sorted");
});

socket.on("node:created", (node) => {
  upsert(state.nodes, node);
  renderCurrentPage();
});

socket.on("node:updated", (node) => {
  if (typeof window.detectEditConflict === "function") window.detectEditConflict(node);
  upsert(state.nodes, node);
  renderCurrentPage();
});

socket.on("node:deleted", (payload) => {
  state.nodes = state.nodes.filter((node) => node.id !== Number(payload.id));
  state.edges = state.edges.filter((edge) => edge.source_id !== Number(payload.id) && edge.target_id !== Number(payload.id));
  state.selectedNodeIds.delete(Number(payload.id));
  if (state.activeNodeId === Number(payload.id)) state.activeNodeId = null;
  renderCurrentPage();
});

socket.on("node:restored", ({ node, children = [], edges = [] }) => {
  upsert(state.nodes, node);
  children.forEach((child) => upsert(state.nodes, child));
  edges.forEach((edge) => upsert(state.edges, edge));
  renderCurrentPage();
});

socket.on("edge:created", (edge) => {
  upsert(state.edges, edge);
  renderCurrentPage();
});

socket.on("edge:updated", (edge) => {
  upsert(state.edges, edge);
  renderCurrentPage();
});

socket.on("edge:deleted", (payload) => {
  state.edges = state.edges.filter((edge) => edge.id !== Number(payload.id));
  renderCurrentPage();
});

socket.on("error:message", (payload) => {
  showToast(payload.message);
  // A failed timeline save never gets an "updated" echo; don't leave "Saving..." stuck
  timelineSaveIndicators.forEach((status, id) => {
    if (status === "saving") setTimelineSaveIndicator(id, null);
  });
});

socket.on("connect_error", () => showToast("Connection lost. Retrying..."));

if (page === "notes") initNotesPage();
if (page === "node") initNodePage();
if (page === "timeline") initTimelinePage();
if (page === "graph") initGraphPage();
if (page === "images") initImagesPage();
if (page === "files") initFilesPage();

fetch("/api/state")
  .then((response) => response.json())
  .then((payload) => {
    state.user = payload.user;
    state.events = payload.events;
    state.nodes = payload.nodes;
    state.edges = payload.edges;
    initGlobalSearch();
    state.nodes.filter((node) => node.parent_id === null).forEach((node) => state.expanded.add(node.id));
    if (page === "notes" && state.nodes.length) state.activeNodeId = sortedNodes()[0].id;
    renderCurrentPage();
    if (page === "graph" && typeof window.zoomToFit === "function") {
      setTimeout(() => window.zoomToFit(), 50);
    }
  });
