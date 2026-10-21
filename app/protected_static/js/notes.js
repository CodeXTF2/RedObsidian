import { PROJECT, childNodes, createNode, currentNode, deleteNodeWithUndo, escapeHtml, focusTitleSoon, nextOrderIndex, openContextMenu, page, setPageRenderer, socket, sortedNodes, state } from "./core.js";
import { initEditorSurface } from "./editor.js";

export function initNotesPage() {
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
        <div class="tree-row ${active ? "active" : ""} ${node.color ? `color-${node.color}` : ""}" draggable="${PROJECT.canEdit}" style="--depth:${depth}" data-node-id="${node.id}">
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

  setPageRenderer(renderNotes);
}

export function attachTreeDragHandlers(row, nodeList) {
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

export function dropZone(row, clientY) {
  const rect = row.getBoundingClientRect();
  const offset = clientY - rect.top;
  if (offset < rect.height * 0.28) return "before";
  if (offset > rect.height * 0.72) return "after";
  return "into";
}

export function moveTreeNode(draggedId, targetId, zone) {
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

export function initNodePage() {
  state.activeNodeId = Number(document.querySelector("[data-node-id]").dataset.nodeId);
  setPageRenderer(initEditorSurface({ fixedNodeId: state.activeNodeId }));
}
