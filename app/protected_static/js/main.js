import { PROJECT, page, projectUrl, renderCurrentPage, setTimelineSaveIndicator, showToast, socket, sortedNodes, state, timelineDrafts, timelineSaveIndicators, timelineSaveTimers, upsert } from "./core.js";
import { initGlobalSearch } from "./search.js";
import { initNodePage, initNotesPage } from "./notes.js";
import { initTimelinePage } from "./timeline.js";
import { initGraphPage } from "./graph.js";
import { initFilesPage, initImagesPage } from "./vault.js";

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

// An owner removed us from the project (or our access otherwise ended) while the page was open
socket.on("project:access_revoked", (payload) => {
  if (payload.user_id !== PROJECT.userId) return;
  socket.disconnect();
  alert("Your access to this project was removed.");
  window.location.href = "/projects";
});

socket.on("project:deleted", () => {
  socket.disconnect();
  alert("This project was deleted.");
  window.location.href = "/projects";
});

if (page === "notes") initNotesPage();
if (page === "node") initNodePage();
if (page === "timeline") initTimelinePage();
if (page === "graph") initGraphPage();
if (page === "images") initImagesPage();
if (page === "files") initFilesPage();

fetch(projectUrl("/api/state"))
  .then((response) => {
    // Signed out (or a forced password change / 2FA setup): let the server send us where we need to go
    if (response.redirected || !response.ok) {
      window.location.reload();
      return Promise.reject(new Error("state unavailable"));
    }
    return response.json();
  })
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
