import { PROJECT, attachedFileResults, escapeHtml, formatTime, page, projectUrl, resultExcerpt, state } from "./core.js";

export async function loadSearchUploads() {
  const [images, files] = await Promise.all([
    fetch(projectUrl("/api/images")).then((response) => response.ok ? response.json() : []).catch(() => []),
    fetch(projectUrl("/api/files")).then((response) => response.ok ? response.json() : []).catch(() => []),
  ]);
  return { images, files };
}

export function buildSearchIndex({ images = [], files = [] } = {}) {
  const nodeResults = state.nodes.flatMap((node) => [
    {
      type: "Vault",
      title: node.title || "Untitled page",
      detail: node.caption || resultExcerpt(node.notes, ""),
      href: projectUrl(`/nodes/${node.id}`),
      searchText: `${node.title || ""} ${node.caption || ""} ${node.notes || ""}`,
    },
    ...attachedFileResults(node.files, node.title || "Untitled page", "Vault", projectUrl(`/nodes/${node.id}`)),
  ]);

  const timelineResults = state.events.flatMap((event) => [
    {
      type: "Timeline",
      title: event.title || "Untitled event",
      detail: `${formatTime(event.occurred_at)}${event.ended_at ? ` to ${formatTime(event.ended_at)}` : ""} ${resultExcerpt(event.body, "")}`.trim(),
      href: projectUrl(`/timeline#timeline-event-${event.id}`),
      searchText: `${event.title || ""} ${event.body || ""} ${formatTime(event.occurred_at)} ${event.ended_at ? formatTime(event.ended_at) : ""}`,
    },
    ...attachedFileResults(event.files, event.title || "Untitled event", "Timeline", projectUrl(`/timeline#timeline-event-${event.id}`)),
  ]);

  const imageResults = images.map((image) => ({
    type: "Image",
    title: image.filename,
    detail: "Image vault",
    href: image.url || projectUrl("/images"),
    searchText: image.filename,
  }));

  const fileResults = files.map((file) => ({
    type: "File",
    title: file.filename,
    detail: "File vault",
    href: file.url || projectUrl("/files"),
    searchText: file.filename,
  }));

  return [...nodeResults, ...timelineResults, ...imageResults, ...fileResults];
}

export function initGlobalSearch() {
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
