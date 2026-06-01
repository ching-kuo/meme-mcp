const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
const form = document.querySelector("[data-token-form]");
const revokeButton = document.querySelector("[data-revoke-token]");
const submitButton = document.querySelector("[data-token-submit]");
const reveal = document.querySelector("[data-token-reveal]");
const plaintext = document.querySelector("[data-token-plaintext]");
const copyButton = document.querySelector("[data-copy-token]");
const regenerateNote = document.querySelector("[data-regenerate-note]");

function updateStatus(status) {
  const stateLabel = document.querySelector("[data-token-state-label]");
  stateLabel.textContent = status.state;
  stateLabel.className = `token-badge token-badge--${status.state}`;
  document.querySelector("[data-token-scope]").textContent = status.scope || "none";
  document.querySelector("[data-token-expires]").textContent = status.expires_at || "none";
  document.querySelector("[data-token-last-used]").textContent = status.last_used_at || "never";
  const active = status.state === "active";
  submitButton.textContent = active ? "Regenerate" : "Generate";
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
  if (submitButton.textContent === "Regenerate") {
    const confirmed = window.confirm("Regenerate this token? The old token dies immediately.");
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
  const confirmed = window.confirm("Revoke the active token now?");
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
  copyButton.textContent = "Copied";
  window.setTimeout(() => {
    copyButton.textContent = "Copy";
  }, 1500);
});
