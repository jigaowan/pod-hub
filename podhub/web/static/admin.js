document.documentElement.classList.add("js");
document.addEventListener("click", async (event) => {
  const copy = event.target.closest("[data-copy]");
  if (copy) {
    const field = document.getElementById(copy.dataset.copy);
    field.select();
    try {
      await navigator.clipboard.writeText(field.value);
      copy.textContent = "Copied";
      window.setTimeout(() => { copy.textContent = "Copy RSS URL"; }, 1500);
    } catch {
      field.focus();
    }
  }
  const opener = event.target.closest("[data-dialog]");
  if (opener) document.getElementById(opener.dataset.dialog).showModal();
});
document.addEventListener("htmx:configRequest", (event) => {
  const token = document.querySelector('meta[name="csrf-token"]')?.content;
  if (token) event.detail.headers["X-CSRF-Token"] = token;
});
document.addEventListener("htmx:beforeRequest", (event) => {
  if (event.target.id === "overview-panel" && event.target.contains(document.activeElement)) {
    event.preventDefault();
  }
});
