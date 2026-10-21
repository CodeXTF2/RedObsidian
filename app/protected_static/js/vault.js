import { PROJECT, apiFetch, escapeHtml, projectUrl, showToast, state } from "./core.js";

export function initImagesPage() {
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
      const response = await fetch(projectUrl("/api/images"));
      const images = await response.json();

      if (!images.length) {
        gallery.innerHTML = '<div class="empty-state">No images in vault. Paste or drag images into notes to add them.</div>';
        return;
      }

      gallery.innerHTML = images.map(img => `
        <div class="image-card" data-url="${escapeHtml(img.url)}" title="Click to view">
          <img src="${escapeHtml(img.url)}" alt="${escapeHtml(img.name || img.filename)}" loading="lazy">
          <div class="image-info">
            <span class="filename" title="${escapeHtml(img.name || img.filename)}${img.uploaded_by ? ` · ${escapeHtml(img.uploaded_by)}` : ""}">${escapeHtml(img.name || img.filename)}</span>
            ${PROJECT.canEdit ? `<button class="delete-image" data-filename="${escapeHtml(img.filename)}">Delete</button>` : ""}
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
          const response = await apiFetch(projectUrl(`/api/images/${encodeURIComponent(btn.dataset.filename)}`), { method: "DELETE" });
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

export function initFilesPage() {
  const fileList = document.querySelector("#file-list");
  if (!fileList) return;

  async function renderFiles() {
    try {
      const response = await fetch(projectUrl("/api/files"));
      const files = await response.json();

      if (!files.length) {
        fileList.innerHTML = '<div class="empty-state">No files in vault. Paste or drag files into notes to add them.</div>';
        return;
      }

      fileList.innerHTML = files.map(file => {
        const size = (file.size / 1024).toFixed(1) + " KB";
        return `
          <div class="image-card file-card" title="Click to copy markdown link" data-url="${escapeHtml(file.url)}" data-name="${escapeHtml(file.name || file.filename)}">
            <div class="file-icon">${escapeHtml(((file.name || file.filename).split(".").pop() || "file").slice(0, 5).toUpperCase())}</div>
            <div class="image-info">
              <span class="filename" title="${escapeHtml(file.name || file.filename)}">${escapeHtml(file.name || file.filename)}</span>
              <span class="filesize">${size}${file.uploaded_by ? ` · ${escapeHtml(file.uploaded_by)}` : ""}</span>
              ${PROJECT.canEdit ? `<button class="delete-file" data-filename="${escapeHtml(file.filename)}">Delete</button>` : ""}
            </div>
          </div>
        `;
      }).join("");

      fileList.querySelectorAll(".file-card").forEach(card => {
        card.addEventListener("click", (e) => {
          if (e.target.classList.contains("delete-file")) return;
          const label = card.dataset.name.replace(/[[\]]/g, "");
          const md = `[Download ${label}](${card.dataset.url})`;
          navigator.clipboard.writeText(md).then(() => showToast("Markdown link copied"));
        });
      });

      fileList.querySelectorAll(".delete-file").forEach(btn => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          if (!confirm("Are you sure you want to delete this file?")) return;
          const response = await apiFetch(projectUrl(`/api/files/${encodeURIComponent(btn.dataset.filename)}`), { method: "DELETE" });
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
