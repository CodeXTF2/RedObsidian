import { PROJECT, caretOffset, editorText, escapeHtml, eventTimeMs, history, insertTextAtCaret, localInputValueToIso, markdownToLiveHtml, restoreCaret, setEditorMarkdown, setPageRenderer, setTimelineSaveIndicator, showToast, socket, state, timelineDrafts, timelineSaveIndicators, toLocalInputValue } from "./core.js";

export function initTimelinePage() {
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
        ${PROJECT.canEdit ? `<div class="timeline-drag" draggable="true" title="Drag to reorder">::</div>` : ""}
        <div class="timeline-edit-fields">
          <input class="timeline-title-input" data-event-title="${event.id}" value="${escapeHtml(event.title)}" maxlength="160" ${PROJECT.canEdit ? "" : "readonly"}>
          <div id="event-body-${event.id}" class="live-markdown-editor timeline-item-body" contenteditable="${PROJECT.canEdit}" data-placeholder="Caption or notes" data-event-body="${event.id}">${markdownToLiveHtml(event.body || "")}</div>
          <div class="timeline-row-actions">
            <div class="timeline-time-input-group">
              <label class="timeline-time-field">
                <span>Start</span>
                <input class="timeline-time-input" type="datetime-local" data-event-time="${event.id}" value="${toLocalInputValue(event.occurred_at)}" ${PROJECT.canEdit ? "" : "disabled"}>
              </label>
              <label class="timeline-time-field">
                <span>End</span>
                <input class="timeline-time-input" type="datetime-local" data-event-end-time="${event.id}" value="${toLocalInputValue(event.ended_at)}" ${PROJECT.canEdit ? "" : "disabled"}>
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
  setPageRenderer(renderTimeline);
}

export function attachTimelineDragHandlers(item, timelineList) {
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

export function timelineDropZone(item, clientY) {
  const rect = item.getBoundingClientRect();
  return clientY - rect.top < rect.height / 2 ? "before" : "after";
}

export function orderedEvents() {
  return [...state.events].sort((a, b) => {
    const orderDelta = (a.order_index ?? 0) - (b.order_index ?? 0);
    return orderDelta || eventTimeMs(b.occurred_at) - eventTimeMs(a.occurred_at);
  });
}

export function moveTimelineEvent(draggedId, targetId, zone) {
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
