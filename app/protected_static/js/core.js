// Set by app_base.html on every project page: { id, base, canEdit, userId }
export const PROJECT = JSON.parse(document.querySelector("#project-context")?.textContent || "{}");

export function projectUrl(path) {
  return `${PROJECT.base}${path}`;
}

export const socket = io({ query: { project: PROJECT.id } });

// Viewers and archived projects are read-only; drop writes before they reach the server (which refuses them anyway)
const READ_ONLY_SAFE_EVENTS = new Set();
const rawEmit = socket.emit.bind(socket);
socket.emit = (event, ...args) => {
  if (!PROJECT.canEdit && !READ_ONLY_SAFE_EVENTS.has(event)) {
    showToast("You have read-only access to this project.");
    return socket;
  }
  return rawEmit(event, ...args);
};

export const state = {
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

export const timelineSaveIndicators = new Map();
export const timelineSaveTimers = new Map();
export const timelineDrafts = new Map();
// Set by the active page's init function; pages without live content leave it null
export let pageRenderer = null;

export const history = {
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

export function isEditableTarget(target) {
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

export const page = document.querySelector("[data-page]")?.dataset.page;
export const toast = document.querySelector("#toast");

export function hideToast() {
  clearTimeout(showToast.timer);
  toast.classList.remove("is-visible");
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 200);
}

// Optional action renders a button in the toast (e.g. "Undo") and keeps it up a little longer
export function showToast(message, action = null) {
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

export const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

// fetch() for state-changing API calls; Flask-WTF expects the token in this header
export function apiFetch(url, options = {}) {
  return fetch(url, { ...options, headers: { "X-CSRFToken": csrfToken, ...(options.headers || {}) } });
}

export let activeContextMenu = null;

export function closeContextMenu() {
  if (!activeContextMenu) return;
  activeContextMenu.cleanup();
  activeContextMenu = null;
}

export function openContextMenu(menu, clientX, clientY) {
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
export function createNode(payload, onCreated) {
  socket.emit("node:create", { title: "Untitled page", caption: "", parent_id: null, in_graph: false, x: 120, y: 120, ...payload }, (node) => {
    if (!node || !node.id) return;
    upsert(state.nodes, node);
    onCreated?.(node);
  });
}

export function focusTitleSoon() {
  requestAnimationFrame(() => {
    const title = document.querySelector("#node-title");
    if (!title || title.offsetParent === null) return;
    title.focus();
    title.select();
  });
}

// Pages are soft-deleted on the server, so deletes can be undone from the toast or with Ctrl+Z
export function deleteNodeWithUndo(nodeId) {
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

export function removeFromGraphWithUndo(nodeId) {
  const node = state.nodes.find((item) => item.id === nodeId);
  if (!node) return;
  const entry = history.push(
    () => socket.emit("node:update", { id: nodeId, in_graph: true }),
    () => socket.emit("node:update", { id: nodeId, in_graph: false })
  );
  socket.emit("node:update", { id: nodeId, in_graph: false });
  showToast(`Removed "${node.title || "Untitled page"}" from the graph`, { label: "Undo", onClick: () => history.undoEntry(entry) });
}

export function setTimelineSaveIndicator(id, status) {
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
export function renderTimelineSaveIndicator(eventId) {
  const indicator = document.querySelector(`#timeline-event-${eventId} .timeline-save-indicator`);
  if (!indicator) return;
  const status = timelineSaveIndicators.get(eventId);
  indicator.className = `timeline-save-indicator ${status ? `is-${status}` : ""}`;
  indicator.textContent = status === "saving" ? "Saving..." : status === "saved" ? "Saved" : "";
}

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

// Only allow http(s) and same-origin relative URLs in generated links
export function safeUrl(value) {
  const url = String(value ?? "").trim();
  return /^(https?:\/\/|\/(?!\/))/i.test(url) ? url : "#";
}

export function insertTextAtCaret(editor, text, addNewlines = false) {
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

export function upsert(collection, item) {
  const index = collection.findIndex((existing) => existing.id === item.id);
  if (index === -1) collection.push(item);
  else collection[index] = item;
}

export function sortedNodes(nodes = state.nodes) {
  return [...nodes].sort((a, b) => {
    const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
    return orderDelta || a.title.localeCompare(b.title);
  });
}

export function currentNode() {
  return state.nodes.find((node) => node.id === state.activeNodeId);
}

export function childNodes(parentId) {
  return sortedNodes(state.nodes.filter((node) => (node.parent_id || null) === (parentId || null)));
}

export function nextOrderIndex(parentId) {
  const siblings = childNodes(parentId);
  if (!siblings.length) return 0;
  return Math.max(...siblings.map((node) => node.order_index ?? 0)) + 1;
}

export function nodePath(node) {
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

export function inlineMarkdown(value) {
  const escaped = escapeHtml(value);
  return escaped
    .replace(/(!\[([^\]]*)\]\((https?:\/\/[^)\s]+|[\w/.-]+\.\w+)\))/g, '<span class="md-img"><span class="md-img-syntax" contenteditable="false">$1</span><img src="$3" alt="$2"></span>')
    .replace(/(`[^`]+`)/g, "<code>$1</code>")
    .replace(/(\*\*([^*]+)\*\*)/g, "<strong>$1</strong>")
    .replace(/(\*[^*]+\*)/g, "<em>$1</em>");
}

export function markdownToLiveHtml(markdown) {
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

export function editorText(editor) {
  const lines = Array.from(editor.querySelectorAll(".md-line"));
  if (lines.length > 0) {
    return lines.map((line) => line.textContent.replace(/\r/g, "")).join("\n");
  }
  return editor.innerText.replace(/\u00a0/g, "").replace(/\n$/, "");
}

export function caretOffset(root) {
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

export function restoreCaret(root, offset) {
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

export function setEditorMarkdown(editor, markdown, preserveCaret = false) {
  const hasFocus = editor.contains(document.activeElement);
  const offset = (preserveCaret && hasFocus) ? caretOffset(editor) : 0;
  editor.innerHTML = markdownToLiveHtml(markdown);
  if (preserveCaret && hasFocus) restoreCaret(editor, offset);
}

export function debounce(callback, delay) {
  let timer;
  const fn = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => callback(...args), delay);
  };
  fn.cancel = () => clearTimeout(timer);
  return fn;
}

export function parseServerDate(value = new Date()) {
  if (value instanceof Date) return value;
  if (typeof value === "string" && value.trim()) {
    const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
    return new Date(normalized);
  }
  return new Date(value);
}

export function eventTimeMs(value) {
  const time = parseServerDate(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

export function toLocalInputValue(value = new Date()) {
  if (arguments.length > 0 && (value === null || value === "")) return "";
  const date = parseServerDate(value);
  if (Number.isNaN(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

export function localInputValueToIso(value) {
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

export function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(parseServerDate(value));
}

export function resultExcerpt(value, query) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (!text) return "";
  const index = text.toLowerCase().indexOf(query.toLowerCase());
  const start = Math.max(0, index === -1 ? 0 : index - 36);
  const excerpt = text.slice(start, start + 110);
  return `${start > 0 ? "... " : ""}${excerpt}${start + 110 < text.length ? " ..." : ""}`;
}

export function attachedFileResults(files, sourceTitle, sourceType, sourceUrl) {
  return (files || []).map((file) => ({
    type: sourceType === "Timeline" ? "Timeline attachment" : "Vault attachment",
    title: file.name || file.filename || "Attached file",
    detail: sourceTitle,
    href: file.url || sourceUrl,
    searchText: `${file.name || ""} ${file.filename || ""} ${sourceTitle}`,
  }));
}

export function renderCurrentPage() {
  if (typeof pageRenderer === "function") pageRenderer();
}

export function setPageRenderer(renderer) {
  pageRenderer = renderer;
}
