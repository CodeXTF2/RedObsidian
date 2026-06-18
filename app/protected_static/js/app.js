const socket = io();

const state = {
  user: null,
  events: [],
  nodes: [],
  edges: [],
  activeNodeId: null,
  expanded: new Set(),
  pan: { x: 0, y: 0 },
  zoom: 1,
};

const history = {
  undoStack: [],
  redoStack: [],
  isUndoing: false,
  lastActionTime: 0,
  push(undoFn, redoFn) {
    if (this.isUndoing) return;
    this.undoStack.push({ undo: undoFn, redo: redoFn });
    this.redoStack = [];
    if (this.undoStack.length > 200) this.undoStack.shift();
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

window.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey) {
    if (e.key.toLowerCase() === "z") {
      e.preventDefault();
      if (e.shiftKey) history.redo();
      else history.undo();
    } else if (e.key.toLowerCase() === "y") {
      e.preventDefault();
      history.redo();
    }
  }
});

const page = document.querySelector("[data-page]")?.dataset.page;
const toast = document.querySelector("#toast");

function showToast(message) {
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => {
    toast.hidden = true;
  }, 2600);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
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
  let inCode = false;
  return lines.map((line) => {
    if (line.startsWith("```")) {
      inCode = !inCode;
      return `<div class="md-line md-code-toggle">${escapeHtml(line) || "<br>"}</div>`;
    }
    if (inCode) {
      return `<div class="md-line md-code">${escapeHtml(line) || "<br>"}</div>`;
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

function toLocalInputValue(date = new Date()) {
  if (!(date instanceof Date)) date = new Date(date);
  const offset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function formatTime(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
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

  let lastSavedState = null;

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

    lastSavedState = {
      id: node.id,
      title: node.title,
      caption: node.caption || "",
      notes: node.notes || "",
      color: node.color || "",
      in_graph: node.in_graph
    };

    if (breadcrumb) breadcrumb.innerHTML = nodePath(node).map((item) => escapeHtml(item.title)).join("<span>/</span>");
    
    const forceUpdate = isNewSelection || (Date.now() - history.lastActionTime < 1000);
    
    if (forceUpdate || document.activeElement !== title) title.value = node.title;
    if (forceUpdate || document.activeElement !== caption) caption.value = node.caption || "";
    if (forceUpdate || document.activeElement !== notes) setEditorMarkdown(notes, node.notes || "", true);
    
    if (nodeFilesBlock) {
      nodeFilesBlock.innerHTML = (node.files || []).map((file, i) => `
        <div class="attached-file">
          <span class="file-icon-small">📄</span>
          <a href="${file.url}" target="_blank" rel="noreferrer">${escapeHtml(file.name)}</a>
          <button type="button" class="delete-attached" data-file-index="${i}">&times;</button>
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

  const save = debounce(() => {
    if (!state.activeNodeId) return;
    const node = state.nodes.find(n => n.id === state.activeNodeId);
    if (!node) return;

    const newState = {
      id: node.id,
      title: title.value,
      caption: caption.value,
      notes: editorText(notes),
      color: node.color || "",
      in_graph: node.in_graph,
    };

    if (lastSavedState && (
      lastSavedState.title !== newState.title || 
      lastSavedState.notes !== newState.notes || 
      lastSavedState.caption !== newState.caption
    )) {
      const prevState = { ...lastSavedState };
      const nextState = { ...newState };
      history.push(
        () => socket.emit("node:update", prevState),
        () => socket.emit("node:update", nextState)
      );
    }
    lastSavedState = { ...newState };

    socket.emit("node:update", newState);
  }, 600);

  const flush = () => {
    if (!lastLoadedNodeId) return;
    const node = state.nodes.find(n => n.id === lastLoadedNodeId);
    if (!node) return;
    save.cancel();
    
    const newState = {
      id: node.id,
      title: title.value,
      caption: caption.value,
      notes: editorText(notes),
      color: node.color || "",
      in_graph: node.in_graph,
    };

    if (lastSavedState && (
      lastSavedState.title !== newState.title || 
      lastSavedState.notes !== newState.notes || 
      lastSavedState.caption !== newState.caption
    )) {
      const prevState = { ...lastSavedState };
      const nextState = { ...newState };
      history.push(
        () => socket.emit("node:update", prevState),
        () => socket.emit("node:update", nextState)
      );
    }
    lastSavedState = { ...newState };

    socket.emit("node:update", newState);
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
      const response = await fetch("/api/upload", { method: "POST", body: formData });
      const payload = await response.json();
      if (payload.url) {
        return { url: payload.url, name: file.name, isImage: file.type.startsWith("image/") };
      }
    } catch (err) {
      showToast("Upload failed");
    }
    return null;
  };

  const insertTextAtCaret = (editor, text, addNewlines = false) => {
    const selection = window.getSelection();
    if (!selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    
    const textToInsert = addNewlines ? `\n${text}\n` : text;
    
    const node = document.createTextNode(textToInsert);
    range.deleteContents();
    range.insertNode(node);
    range.setStartAfter(node);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    
    // Trigger update
    setEditorMarkdown(editor, editorText(editor), true);
    editor.dispatchEvent(new Event("input", { bubbles: true }));
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
      renderEditor();
      save();
    }
  });

  addToGraphBtn?.addEventListener("click", () => {
    const node = currentNode();
    if (node) {
      node.in_graph = true;
      renderEditor();
      save();
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
        const parentId = Number(button.dataset.childId);
        state.expanded.add(parentId);
        socket.emit("node:create", {
          title: "Untitled page",
          caption: "",
          parent_id: parentId,
          in_graph: false,
          order_index: nextOrderIndex(parentId),
          x: 120,
          y: 120,
        });
      });
    });
    nodeList.querySelectorAll(".tree-row").forEach((row) => attachTreeDragHandlers(row, nodeList));

    // Allow dropping to the very bottom to make a root node
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

    const node = currentNode();
    editor.hidden = !node;
    emptyEditor.hidden = !!node;
    if (node) renderEditor();
  }

  addNodeButton.addEventListener("click", () => {
    socket.emit("node:create", {
      title: "Untitled page",
      caption: "",
      parent_id: null,
      in_graph: false,
      order_index: nextOrderIndex(null),
      x: 120,
      y: 120,
    });
  });

  const sidebar = document.querySelector(".vault-sidebar");
  sidebar.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    const row = event.target.closest(".tree-row");
    const nodeId = row ? Number(row.dataset.nodeId) : null;

    contextMenu.style.left = `${event.clientX}px`;
    contextMenu.style.top = `${event.clientY}px`;
    contextMenu.hidden = false;

    menuNewPage.hidden = !!nodeId;
    menuNewChild.hidden = !nodeId;
    menuDeletePage.hidden = !nodeId;

    const cleanup = (e) => {
      if (e && contextMenu.contains(e.target)) return;
      contextMenu.hidden = true;
      document.removeEventListener("pointerdown", cleanup);
    };

    if (nodeId) {
      menuNewChild.onclick = (e) => {
        e.stopPropagation();
        state.expanded.add(nodeId);
        socket.emit("node:create", {
          title: "Untitled page",
          caption: "",
          parent_id: nodeId,
          in_graph: false,
          order_index: nextOrderIndex(nodeId),
          x: 120,
          y: 120,
        });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
      menuDeletePage.onclick = (e) => {
        e.stopPropagation();
        if (typeof window.cancelPendingSave === "function") window.cancelPendingSave();
        socket.emit("node:delete", { id: nodeId });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
    } else {
      menuNewPage.onclick = (e) => {
        e.stopPropagation();
        socket.emit("node:create", {
          title: "Untitled page",
          caption: "",
          parent_id: null,
          in_graph: false,
          order_index: nextOrderIndex(null),
          x: 120,
          y: 120,
        });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
    }

    setTimeout(() => document.addEventListener("pointerdown", cleanup), 10);
  });

  window.renderCurrentPage = renderNotes;
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
  window.renderCurrentPage = initEditorSurface({ fixedNodeId: state.activeNodeId });
}

function initTimelinePage() {
  const timelineForm = document.querySelector("#timeline-form");
  const timelineList = document.querySelector("#timeline-list");
  const eventTitle = document.querySelector("#event-title");
  const eventBody = document.querySelector("#event-body");
  const eventTime = document.querySelector("#event-time");
  const eventTimeNow = document.querySelector("#event-time-now");

  eventBody?.addEventListener("input", () => {
    setEditorMarkdown(eventBody, editorText(eventBody), true);
  });

  eventTimeNow?.addEventListener("click", () => {
    eventTime.value = toLocalInputValue();
  });

  function renderTimeline() {
    const events = [...state.events].sort((a, b) => {
      const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
      return orderDelta || new Date(b.occurred_at) - new Date(a.occurred_at);
    });
    timelineList.innerHTML = events.map((event) => `
      <article class="timeline-item" data-event-id="${event.id}">
        <div class="timeline-drag" draggable="true" title="Drag to reorder">::</div>
        <div class="timeline-edit-fields">
          <input class="timeline-title-input" data-event-title="${event.id}" value="${escapeHtml(event.title)}" maxlength="160">
          <div id="event-body-${event.id}" class="live-markdown-editor timeline-item-body" contenteditable="true" data-placeholder="Caption or notes" data-event-body="${event.id}">${markdownToLiveHtml(event.body || "")}</div>
          <div class="timeline-row-actions">
            <div class="timeline-time-input-group">
              <input class="timeline-time-input" type="datetime-local" data-event-time="${event.id}" value="${toLocalInputValue(event.occurred_at)}">
              <button type="button" class="ghost-button" data-event-now="${event.id}" title="Set to now">Now</button>
            </div>
            <button type="button" class="ghost-button danger" data-event-delete="${event.id}">Delete</button>
            <button type="button" data-event-save="${event.id}">Save</button>
          </div>
        </div>
      </article>
    `).join("");

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
      });
    });

    timelineList.querySelectorAll("[data-event-save]").forEach((button) => {
      button.addEventListener("click", () => {
        const id = Number(button.dataset.eventSave);
        const eventData = state.events.find(e => e.id === id);
        if (!eventData) return;

        const editor = timelineList.querySelector(`[data-event-body="${id}"]`);
        const newState = {
          id,
          title: timelineList.querySelector(`[data-event-title="${id}"]`).value,
          body: editorText(editor),
          occurred_at: new Date(timelineList.querySelector(`[data-event-time="${id}"]`).value).toISOString(),
        };

        const prevState = {
          id: eventData.id,
          title: eventData.title,
          body: eventData.body,
          occurred_at: eventData.occurred_at
        };

        history.push(
          () => socket.emit("timeline:update", prevState),
          () => socket.emit("timeline:update", newState)
        );

        socket.emit("timeline:update", newState);
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
  }
  timelineForm.addEventListener("submit", (event) => {
    event.preventDefault();
    socket.emit("timeline:create", {
      title: eventTitle.value,
      body: editorText(eventBody),
      occurred_at: new Date(eventTime.value).toISOString(),
    });
    timelineForm.reset();
    eventBody.innerHTML = "";
    eventTime.value = toLocalInputValue();
  });
  eventTime.value = toLocalInputValue();
  window.renderCurrentPage = renderTimeline;
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
    return orderDelta || new Date(b.occurred_at) - new Date(a.occurred_at);
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
  const menuDeleteNode = document.querySelector("#menu-delete-node");
  const menuDeleteLink = document.querySelector("#menu-delete-link");
  const graphCanvas = document.querySelector("#graph-canvas");
  const edgeLayer = document.querySelector("#edge-layer");
  const draftLayer = document.querySelector("#draft-layer");
  const nodeLayer = document.querySelector("#node-layer");
  const renderEditor = initEditorSurface();
  let interaction = null;
  let lastPanMoved = false;

  const worldToScreen = (point) => ({ x: point.x * state.zoom + state.pan.x, y: point.y * state.zoom + state.pan.y });
  const screenToWorld = (clientX, clientY) => {
    const rect = graphCanvas.getBoundingClientRect();
    return {
      x: (clientX - rect.left - state.pan.x) / state.zoom,
      y: (clientY - rect.top - state.pan.y) / state.zoom,
    };
  };

  function drawLine(layer, x1, y1, x2, y2, color, width) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    const start = worldToScreen({ x: x1, y: y1 });
    const end = worldToScreen({ x: x2, y: y2 });
    line.setAttribute("x1", start.x);
    line.setAttribute("y1", start.y);
    line.setAttribute("x2", end.x);
    line.setAttribute("y2", end.y);
    line.setAttribute("stroke", color);
    line.setAttribute("stroke-width", width);
    line.setAttribute("stroke-linecap", "round");
    layer.appendChild(line);
  }

  function renderEdges() {
    edgeLayer.innerHTML = "";
    for (const edge of state.edges) {
      const source = state.nodes.find((node) => node.id === edge.source_id);
      const target = state.nodes.find((node) => node.id === edge.target_id);
      if (source && target && source.in_graph !== false && target.in_graph !== false) drawLine(edgeLayer, source.x + 92, source.y + 42, target.x + 92, target.y + 42, "#7d8d9a", 2);
    }
  }

  function positionNodeElement(node) {
    const element = nodeLayer.querySelector(`[data-node-id="${node.id}"]`);
    if (!element) return;
    const point = worldToScreen(node);
    element.style.left = `${point.x}px`;
    element.style.top = `${point.y}px`;
    element.style.transform = `scale(${state.zoom})`;
  }

  function renderGraphEditor() {
    const node = currentNode();
    graphNodePanel.hidden = !node;
    document.querySelector(".graph-layout")?.classList.toggle("panel-open", !!node);
    
    // Clear previous color classes
    graphNodePanel.classList.remove("color-red", "color-orange", "color-yellow", "color-green", "color-blue", "color-pink");
    
    if (node) {
      if (node.color) graphNodePanel.classList.add(`color-${node.color}`);
      renderEditor();
    }
  }

  function renderZoom() {
    zoomLevel.textContent = `${Math.round(state.zoom * 100)}%`;
  }

  function zoomToFit() {
    const nodes = state.nodes.filter((node) => node.in_graph !== false);
    if (!nodes.length) return;

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    nodes.forEach((node) => {
      minX = Math.min(minX, node.x);
      minY = Math.min(minY, node.y);
      maxX = Math.max(maxX, node.x + 184);
      maxY = Math.max(maxY, node.y + 84);
    });

    const padding = 60;
    const width = maxX - minX;
    const height = maxY - minY;
    const rect = graphCanvas.getBoundingClientRect();

    const zoomX = rect.width / (width + padding * 2);
    const zoomY = rect.height / (height + padding * 2);
    state.zoom = Math.max(0.35, Math.min(2.4, Math.min(zoomX, zoomY)));

    state.pan.x = (rect.width - width * state.zoom) / 2 - minX * state.zoom;
    state.pan.y = (rect.height - height * state.zoom) / 2 - minY * state.zoom;

    state.nodes.forEach(positionNodeElement);
    renderEdges();
    renderZoom();
  }

  function setZoom(nextZoom, originClientX = null, originClientY = null) {
    const rect = graphCanvas.getBoundingClientRect();
    const originX = originClientX ?? rect.left + rect.width / 2;
    const originY = originClientY ?? rect.top + rect.height / 2;
    const before = screenToWorld(originX, originY);
    state.zoom = Math.max(0.35, Math.min(2.4, nextZoom));
    state.pan.x = originX - rect.left - before.x * state.zoom;
    state.pan.y = originY - rect.top - before.y * state.zoom;
    state.nodes.forEach(positionNodeElement);
    renderEdges();
    renderZoom();
  }

  function isClickOnEdge(clientX, clientY) {
    const world = screenToWorld(clientX, clientY);
    for (const edge of state.edges) {
      const source = state.nodes.find((node) => node.id === edge.source_id);
      const target = state.nodes.find((node) => node.id === edge.target_id);
      if (!source || !target) continue;
      const x1 = source.x + 92, y1 = source.y + 42;
      const x2 = target.x + 92, y2 = target.y + 42;
      const A = world.x - x1, B = world.y - y1;
      const C = x2 - x1, D = y2 - y1;
      const lenSq = C * C + D * D;
      if (lenSq === 0) continue;
      let t = (A * C + B * D) / lenSq;
      if (t < 0) t = 0;
      else if (t > 1) t = 1;
      const closestX = x1 + t * C;
      const closestY = y1 + t * D;
      if (Math.hypot(world.x - closestX, world.y - closestY) < 10) return edge;
    }
    return null;
  }

  function renderGraph() {
    nodeLayer.innerHTML = "";
    draftLayer.innerHTML = "";
    renderEdges();
    for (const node of state.nodes) {
      if (node.in_graph === false) continue;
      const element = document.createElement("div");
      element.className = `graph-node ${node.id === state.activeNodeId ? "active" : ""} ${node.color ? `color-${node.color}` : ""}`;
      element.dataset.nodeId = node.id;
      element.innerHTML = `
        <button class="node-open" type="button">
          <strong>${escapeHtml(node.title)}</strong>
          <span>${escapeHtml(node.caption || "No caption")}</span>
        </button>
        <button class="node-link-handle" type="button" title="Link"></button>
      `;
      element.querySelector(".node-open").addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        const world = screenToWorld(event.clientX, event.clientY);
        interaction = {
          type: "move",
          node,
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          startWorldX: node.x,
          startWorldY: node.y,
          offsetX: world.x - node.x,
          offsetY: world.y - node.y,
          moved: false,
        };
      });
      element.querySelector(".node-link-handle").addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.stopPropagation();
        interaction = { type: "link", node, pointerId: event.pointerId, fromX: node.x + 92, fromY: node.y + 42 };
      });
      nodeLayer.appendChild(element);
      positionNodeElement(node);
    }
    renderZoom();
    renderGraphEditor();
  }

  closePanelButton?.addEventListener("click", () => {
    state.activeNodeId = null;
    renderGraph();
  });

  graphCanvas.addEventListener("contextmenu", (event) => {
    if (lastPanMoved) {
      event.preventDefault();
      lastPanMoved = false;
      return;
    }
    event.preventDefault();
    const nodeElement = event.target.closest(".graph-node");
    const nodeId = nodeElement ? Number(nodeElement.dataset.nodeId) : null;

    contextMenu.style.left = `${event.clientX}px`;
    contextMenu.style.top = `${event.clientY}px`;
    contextMenu.hidden = false;

    const targetEdge = isClickOnEdge(event.clientX, event.clientY);
    menuNewNode.hidden = !!nodeId;
    menuDeleteNode.hidden = !nodeId;
    menuDeleteLink.hidden = !targetEdge;

    const cleanup = (e) => {
      if (e && contextMenu.contains(e.target)) return;
      contextMenu.hidden = true;
      document.removeEventListener("pointerdown", cleanup);
    };

    if (!nodeId) {
      const world = screenToWorld(event.clientX, event.clientY);
      menuNewNode.onclick = (e) => {
        e.stopPropagation();
        socket.emit("node:create", {
          title: "Untitled page",
          caption: "",
          parent_id: null,
          in_graph: true,
          order_index: nextOrderIndex(null),
          x: Math.round(world.x - 92),
          y: Math.round(world.y - 42),
        });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
    }

    if (nodeId) {
      menuDeleteNode.onclick = (e) => {
        e.stopPropagation();
        if (typeof window.cancelPendingSave === "function") window.cancelPendingSave();
        socket.emit("node:delete", { id: nodeId });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
    }

    if (targetEdge) {
      menuDeleteLink.onclick = (e) => {
        e.stopPropagation();
        const edgeState = { ...targetEdge };
        history.push(
          () => socket.emit("edge:create", { source_id: edgeState.source_id, target_id: edgeState.target_id }),
          () => socket.emit("edge:delete", { id: edgeState.id })
        );
        socket.emit("edge:delete", { id: targetEdge.id });
        contextMenu.hidden = true;
        document.removeEventListener("pointerdown", cleanup);
      };
    }

    setTimeout(() => document.addEventListener("pointerdown", cleanup), 10);
  });

  graphCanvas.addEventListener("pointerdown", (event) => {
    if (event.button === 2) lastPanMoved = false;
    if (event.button !== 2 || event.target.closest(".graph-node")) return;
    event.preventDefault();
    interaction = {
      type: "pan",
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      panX: state.pan.x,
      panY: state.pan.y,
      moved: false,
    };
  });

  document.addEventListener("pointermove", (event) => {
    if (!interaction || event.pointerId !== interaction.pointerId) return;
    if (interaction.type === "move") {
      const world = screenToWorld(event.clientX, event.clientY);
      interaction.node.x = Math.max(-4000, Math.min(4000, world.x - interaction.offsetX));
      interaction.node.y = Math.max(-4000, Math.min(4000, world.y - interaction.offsetY));
      interaction.moved = interaction.moved || Math.hypot(event.clientX - interaction.startX, event.clientY - interaction.startY) > 4;
      positionNodeElement(interaction.node);
      renderEdges();
    }
    if (interaction.type === "link") {
      const world = screenToWorld(event.clientX, event.clientY);
      draftLayer.innerHTML = "";
      drawLine(draftLayer, interaction.fromX, interaction.fromY, world.x, world.y, "#2dd4bf", 3);
    }
    if (interaction.type === "pan") {
      state.pan.x = interaction.panX + event.clientX - interaction.startX;
      state.pan.y = interaction.panY + event.clientY - interaction.startY;
      interaction.moved = interaction.moved || Math.hypot(event.clientX - interaction.startX, event.clientY - interaction.startY) > 5;
      state.nodes.forEach(positionNodeElement);
      renderEdges();
    }
  });

  document.addEventListener("pointerup", (event) => {
    if (!interaction || event.pointerId !== interaction.pointerId) return;
    if (interaction.type === "pan" && interaction.moved) lastPanMoved = true;
    const done = interaction;
    interaction = null;
    draftLayer.innerHTML = "";
    if (done.type === "move") {
      if (done.moved) socket.emit("node:update", { id: done.node.id, x: done.node.x, y: done.node.y });
      else {
        state.activeNodeId = done.node.id;
        renderGraph();
      }
    }
    if (done.type === "link") {
      const targetElement = document.elementsFromPoint(event.clientX, event.clientY)
        .map((element) => element.closest?.(".graph-node"))
        .find((element) => element && Number(element.dataset.nodeId) !== done.node.id);
      const targetId = Number(targetElement?.dataset.nodeId);
      if (targetId) {
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
  });

  addNodeButton.addEventListener("click", () => {
    const rect = graphCanvas.getBoundingClientRect();
    const center = screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
    socket.emit("node:create", {
      title: "Untitled page",
      caption: "",
      parent_id: null,
      in_graph: true,
      order_index: nextOrderIndex(null),
      x: Math.round(center.x - 92),
      y: Math.round(center.y - 42),
    });
  });
  zoomInButton.addEventListener("click", () => setZoom(state.zoom * 1.15));
  zoomOutButton.addEventListener("click", () => setZoom(state.zoom / 1.15));
  zoomFitButton.addEventListener("click", () => zoomToFit());
  graphCanvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    setZoom(state.zoom * (event.deltaY < 0 ? 1.1 : 1 / 1.1), event.clientX, event.clientY);
  }, { passive: false });

  window.addEventListener("resize", renderGraph);
  window.renderCurrentPage = renderGraph;
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
          <img src="${img.url}" loading="lazy">
          <div class="image-info">
            <span class="filename">${escapeHtml(img.filename.slice(0, 12))}...</span>
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
          const response = await fetch(`/api/images/${btn.dataset.filename}`, { method: "DELETE" });
          if (response.ok) {
            showToast("Image deleted");
            renderGallery();
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
              <span class="filename" title="${escapeHtml(file.filename)}">${escapeHtml(file.filename.slice(0, 16))}...</span>
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
          const response = await fetch(`/api/files/${btn.dataset.filename}`, { method: "DELETE" });
          if (response.ok) {
            showToast("File deleted");
            renderFiles();
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
  if (typeof window.renderCurrentPage === "function") window.renderCurrentPage();
}

socket.on("timeline:created", (event) => {
  upsert(state.events, event);
  renderCurrentPage();
});

socket.on("timeline:updated", (event) => {
  upsert(state.events, event);
  renderCurrentPage();
});

socket.on("timeline:deleted", (payload) => {
  state.events = state.events.filter(e => e.id !== payload.id);
  renderCurrentPage();
});

socket.on("node:created", (node) => {
  upsert(state.nodes, node);
  renderCurrentPage();
});

socket.on("node:updated", (node) => {
  upsert(state.nodes, node);
  renderCurrentPage();
});

socket.on("node:deleted", (payload) => {
  state.nodes = state.nodes.filter((node) => node.id !== Number(payload.id));
  state.edges = state.edges.filter((edge) => edge.source_id !== Number(payload.id) && edge.target_id !== Number(payload.id));
  if (state.activeNodeId === Number(payload.id)) state.activeNodeId = null;
  renderCurrentPage();
});

socket.on("edge:created", (edge) => {
  upsert(state.edges, edge);
  renderCurrentPage();
});

socket.on("edge:deleted", (payload) => {
  state.edges = state.edges.filter((edge) => edge.id !== Number(payload.id));
  renderCurrentPage();
});

socket.on("error:message", (payload) => showToast(payload.message));

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
    state.nodes.filter((node) => node.parent_id === null).forEach((node) => state.expanded.add(node.id));
    if (page === "notes" && state.nodes.length) state.activeNodeId = sortedNodes()[0].id;
    renderCurrentPage();
    if (page === "graph" && typeof window.zoomToFit === "function") {
      setTimeout(() => window.zoomToFit(), 50);
    }
  });
