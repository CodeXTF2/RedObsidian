// Small behaviours for server-rendered pages. Inline handlers are blocked by the CSP, so markup opts in with data-* attributes.
(() => {
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  // Dropdown menus built from <details>: close when clicking elsewhere, pressing Escape, or opening another
  document.addEventListener("click", (event) => {
    $$("details.menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.open = false;
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    $$("details.menu[open]").forEach((menu) => {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    });
  });
  $$("details.menu").forEach((menu) => {
    menu.addEventListener("toggle", () => {
      if (menu.open) $$("details.menu[open]").forEach((other) => other !== menu && (other.open = false));
    });
  });

  // data-toggle="#id": show/hide a panel
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest("[data-toggle]");
    if (!trigger) return;
    const target = document.querySelector(trigger.dataset.toggle);
    if (!target) return;
    target.hidden = !target.hidden;
    if (!target.hidden) target.querySelector("input, textarea, select")?.focus();
  });

  // data-confirm on a form: ask before submitting
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("form[data-confirm]");
    if (form && !window.confirm(form.dataset.confirm)) event.preventDefault();
  });

  // data-autosubmit on a select, or data-autosubmit-toggle on a switch: submit its form on change
  document.addEventListener("change", (event) => {
    const control = event.target.closest("select[data-autosubmit], input[data-autosubmit-toggle]");
    if (control) control.form.requestSubmit();
  });

  // data-match + data-enables: enable a button once the typed text matches
  $$("[data-match][data-enables]").forEach((input) => {
    const button = document.querySelector(input.dataset.enables);
    const sync = () => button && (button.disabled = input.value.trim() !== input.dataset.match);
    input.addEventListener("input", sync);
    sync();
  });

  // data-copy: copy text to the clipboard
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-copy]");
    if (!button) return;
    navigator.clipboard?.writeText(button.dataset.copy).then(() => {
      const label = button.textContent;
      button.textContent = "Copied";
      setTimeout(() => (button.textContent = label), 1500);
    });
  });

  // data-print: print the page (report preview)
  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-print]")) window.print();
  });

  // data-filter-list="#list": filter rows of a checkbox list by text
  $$("[data-filter-list]").forEach((input) => {
    const list = document.querySelector(input.dataset.filterList);
    input.addEventListener("input", () => {
      const query = input.value.trim().toLowerCase();
      $$(".check-row", list).forEach((row) => {
        row.hidden = !!query && !row.textContent.toLowerCase().includes(query);
      });
    });
  });

  // data-depends-on="name": dim a settings group while its switch is off
  $$("[data-depends-on]").forEach((group) => {
    const toggle = document.querySelector(`input[name="${group.dataset.dependsOn}"]`);
    if (!toggle) return;
    const sync = () => group.classList.toggle("is-off", !toggle.checked);
    toggle.addEventListener("change", sync);
    sync();
  });

  // Show server UTC times in the viewer's local time zone
  $$("time[data-local-time]").forEach((element) => {
    const date = new Date(element.getAttribute("datetime"));
    if (Number.isNaN(date.getTime())) return;
    element.title = element.textContent;
    element.textContent = date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
  });

  // Live CVSS score/severity while typing a vector on the finding form
  const vectorInput = document.querySelector("[data-cvss-input]");
  if (vectorInput) {
    const output = document.querySelector("[data-cvss-output]");
    const scoreInput = document.querySelector("[data-cvss-score]");
    const severitySelect = document.querySelector("[data-cvss-severity]");
    let timer = null;
    const update = async () => {
      const vector = vectorInput.value.trim();
      if (!vector) {
        output.textContent = "";
        return;
      }
      try {
        const response = await fetch(`${vectorInput.dataset.cvssUrl}?vector=${encodeURIComponent(vector)}`);
        const result = await response.json();
        if (result.score === null) {
          output.textContent = "Not a complete CVSS 3.x vector";
          return;
        }
        output.textContent = `Score ${result.score.toFixed(1)} (${result.severity})`;
        scoreInput.value = result.score.toFixed(1);
        severitySelect.value = result.severity;
      } catch {
        output.textContent = "";
      }
    };
    vectorInput.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(update, 250);
    });
  }
})();
