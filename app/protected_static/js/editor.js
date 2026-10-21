import { PROJECT, apiFetch, currentNode, debounce, editorText, escapeHtml, history, insertTextAtCaret, nodePath, page, projectUrl, renderCurrentPage, safeUrl, setEditorMarkdown, showToast, socket, state } from "./core.js";

export function initEditorSurface({ fixedNodeId = null } = {}) {
  const title = document.querySelector("#node-title");
  const caption = document.querySelector("#node-caption");
  const notes = document.querySelector("#node-notes");
  const colorPicker = document.querySelector("#node-color-picker");
  const addToGraphBtn = document.querySelector("#add-to-graph");
  const breadcrumb = document.querySelector("#breadcrumb");
  const historyLink = document.querySelector("#node-history-link");
  const findingLink = document.querySelector("#node-finding-link");
  const openLink = document.querySelector("#node-open-link");
  let lastLoadedNodeId = null;

  if (!PROJECT.canEdit) {
    [title, caption].forEach((input) => input && (input.readOnly = true));
    if (notes) notes.contentEditable = "false";
  }

  const nodeFilesBlock = document.querySelector("#node-files");
  const editorForm = document.querySelector("#node-editor");
  // Vault/page editors tint their container with the page colour (the graph panel handles its own)
  const tintTarget = editorForm?.closest(".notes-layout > .document-shell, .node-page-layout > .document-shell");
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
    if (historyLink) {
      historyLink.href = projectUrl(`/nodes/${node.id}/history`);
      historyLink.hidden = false;
    }
    if (findingLink) {
      findingLink.href = projectUrl(`/findings/new?from_page=${node.id}`);
      findingLink.hidden = !PROJECT.canEdit || !PROJECT.findingsEnabled;
    }
    if (openLink) {
      openLink.href = projectUrl(`/nodes/${node.id}`);
      openLink.hidden = !!fixedNodeId;
    }

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

    if (tintTarget) {
      tintTarget.classList.remove("color-red", "color-orange", "color-yellow", "color-green", "color-blue", "color-pink");
      if (node.color) tintTarget.classList.add(`color-${node.color}`);
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
      const response = await apiFetch(projectUrl("/api/upload"), { method: "POST", body: formData });
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
