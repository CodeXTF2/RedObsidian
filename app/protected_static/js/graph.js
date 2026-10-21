import { PROJECT, closeContextMenu, createNode, currentNode, deleteNodeWithUndo, escapeHtml, focusTitleSoon, history, isEditableTarget, nextOrderIndex, openContextMenu, projectUrl, removeFromGraphWithUndo, setPageRenderer, showToast, socket, state } from "./core.js";
import { initEditorSurface } from "./editor.js";

export function initGraphPage() {
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
  const ARROW_LENGTH = 13;
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
    const half = ARROW_LENGTH * 0.6;
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

  // --- Keeping the open node in view ------------------------------------------
  // Opening the panel narrows the canvas, so the view glides (in step with the panel)
  // to centre the opened node in what's left. Closing glides back to the view from
  // before the panel opened.
  const PANEL_EASE_MS = 260;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let viewBeforePanel = null;
  let panelNodeId = null;
  let viewAnimation = null;

  function cancelViewAnimation() {
    if (viewAnimation) cancelAnimationFrame(viewAnimation);
    viewAnimation = null;
  }

  function animateViewTo(target) {
    cancelViewAnimation();
    const from = { x: state.pan.x, y: state.pan.y, zoom: state.zoom };
    const zoomChanges = from.zoom !== target.zoom;
    const finish = () => {
      Object.assign(state.pan, { x: target.x, y: target.y });
      state.zoom = target.zoom;
      if (zoomChanges) renderEdges();
      applyViewport();
    };
    if (reducedMotion.matches) {
      finish();
      return;
    }
    const start = performance.now();
    // Matches the panel's cubic-bezier(0.22, 1, 0.36, 1) closely enough to move as one
    const ease = (t) => 1 - Math.pow(1 - t, 4);
    const step = (now) => {
      const t = Math.min(1, (now - start) / PANEL_EASE_MS);
      if (t >= 1) {
        viewAnimation = null;
        finish();
        return;
      }
      const k = ease(t);
      state.pan.x = from.x + (target.x - from.x) * k;
      state.pan.y = from.y + (target.y - from.y) * k;
      state.zoom = from.zoom + (target.zoom - from.zoom) * k;
      applyViewport();
      viewAnimation = requestAnimationFrame(step);
    };
    viewAnimation = requestAnimationFrame(step);
  }

  // Width the canvas will have once the panel has finished opening
  function canvasWidthWithPanel(layout) {
    const canvasWidth = graphCanvas.getBoundingClientRect().width;
    const sideBySide = getComputedStyle(layout).gridTemplateColumns.trim().split(/\s+/).length > 1;
    if (!sideBySide) return canvasWidth;
    const panelWidth = graphNodePanel.querySelector(".graph-node-panel-inner")?.getBoundingClientRect().width || 0;
    const columnNow = graphNodePanel.getBoundingClientRect().width;
    return Math.max(120, canvasWidth + columnNow - panelWidth);
  }

  function followPanel(layout, node) {
    const openId = node ? node.id : null;
    if (openId === panelNodeId) return;
    const wasOpen = panelNodeId !== null;
    panelNodeId = openId;
    if (node) {
      if (!wasOpen) viewBeforePanel = { x: state.pan.x, y: state.pan.y, zoom: state.zoom };
      const box = nodeRect(node);
      const height = graphCanvas.getBoundingClientRect().height;
      animateViewTo({
        x: canvasWidthWithPanel(layout) / 2 - box.cx * state.zoom,
        y: height / 2 - box.cy * state.zoom,
        zoom: state.zoom,
      });
    } else if (viewBeforePanel) {
      animateViewTo(viewBeforePanel);
      viewBeforePanel = null;
    }
  }

  function renderGraphEditor() {
    const node = currentNode();
    // The panel stays in the DOM so it can animate closed; inert keeps it out of tab order meanwhile
    graphNodePanel.inert = !node;
    const layout = document.querySelector(".graph-layout");
    layout?.classList.toggle("panel-open", !!node);
    if (layout) followPanel(layout, node && isVisible(node) ? node : null);

    // Keep the last colour while closing so the accent doesn't flash
    if (node) {
      graphNodePanel.classList.remove("color-red", "color-orange", "color-yellow", "color-green", "color-blue", "color-pink");
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

    cancelViewAnimation();
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
    cancelViewAnimation();
    const before = screenToWorld(originX, originY);
    state.zoom = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, nextZoom));
    state.pan.x = originX - rect.left - before.x * state.zoom;
    state.pan.y = originY - rect.top - before.y * state.zoom;
    renderEdges();
    applyViewport();
  }

  function setInteraction(next) {
    if (next) cancelViewAnimation();
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
      window.location.href = projectUrl(`/nodes/${nodeId}`);
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
        // A drag selects what moved but leaves the panel alone; only a plain click opens a node
        if (!state.selectedNodeIds.has(done.node.id)) {
          state.selectedNodeIds = new Set(done.moveStart.map((item) => item.id));
          selectEdge(null);
          refreshGraphSelectionStyles(false);
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
  setPageRenderer(renderGraph);
  window.zoomToFit = zoomToFit;
}
