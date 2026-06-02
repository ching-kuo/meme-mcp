const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const accountRoot = document.querySelector(".account");
const form = document.querySelector("[data-token-form]");
const revokeButton = document.querySelector("[data-revoke-token]");
const submitButton = document.querySelector("[data-token-submit]");
const reveal = document.querySelector("[data-token-reveal]");
const plaintext = document.querySelector("[data-token-plaintext]");
const copyButton = document.querySelector("[data-copy-token]");
const regenerateNote = document.querySelector("[data-regenerate-note]");

// The submit button's label is localized, so "is this a regenerate?" is tracked
// via a data attribute, not by comparing textContent to an English literal. The
// server renders the initial label; seed the mode from data-token-state so the
// first submit reads the right value before any updateStatus() runs.
if (submitButton && accountRoot) {
  submitButton.dataset.mode = accountRoot.dataset.tokenState === "active" ? "regenerate" : "generate";
}

function updateStatus(status) {
  const stateLabel = document.querySelector("[data-token-state-label]");
  stateLabel.textContent = t("js.token.state." + status.state);
  stateLabel.className = `token-badge token-badge--${status.state}`;
  document.querySelector("[data-token-scope]").textContent = status.scope
    ? t("js.token.scope." + status.scope)
    : t("js.token.none");
  document.querySelector("[data-token-expires]").textContent = status.expires_at || t("js.token.none");
  document.querySelector("[data-token-last-used]").textContent =
    status.last_used_at || t("js.token.never");
  const active = status.state === "active";
  submitButton.dataset.mode = active ? "regenerate" : "generate";
  submitButton.textContent = active ? t("js.account.regenerate") : t("js.account.generate");
  regenerateNote.hidden = !active;
  revokeButton.disabled = !active;
}

async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const envelope = await response.json();
  if (!response.ok || !envelope.ok) {
    throw new Error(envelope.error_code || "request_failed");
  }
  return envelope.data;
}

form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (submitButton.dataset.mode === "regenerate") {
    const confirmed = window.confirm(t("js.account.confirm_regenerate"));
    if (!confirmed) return;
  }
  submitButton.disabled = true;
  try {
    const data = await postJson("/account/token", Object.fromEntries(new FormData(form)));
    plaintext.textContent = data.token;
    reveal.hidden = false;
    updateStatus(data.token_status);
  } finally {
    submitButton.disabled = false;
  }
});

revokeButton?.addEventListener("click", async () => {
  const confirmed = window.confirm(t("js.account.confirm_revoke"));
  if (!confirmed) return;
  revokeButton.disabled = true;
  try {
    const data = await postJson("/account/token/revoke");
    reveal.hidden = true;
    plaintext.textContent = "";
    updateStatus(data.token_status);
  } finally {
    revokeButton.disabled = false;
  }
});

copyButton?.addEventListener("click", async () => {
  await navigator.clipboard.writeText(plaintext.textContent || "");
  copyButton.textContent = t("js.copy.done");
  window.setTimeout(() => {
    copyButton.textContent = t("js.copy");
  }, 1500);
});
