// Single-page /upload client (KTD1). Vanilla JS, no framework or build step.
//
// Flow: choose file -> local preview + size pre-check -> Analyze (base64 JSON
// POST with X-CSRF-Token, spinner, AbortController timeout) -> edit proposed
// metadata (read-only slots, KTD10) -> Approve -> success link to /browse.
//
// Two distinct warn states are surfaced separately (R9/R12): a near-duplicate
// (duplicate.action === "warn") is a NON-BLOCKING banner naming the nearest
// template with Approve still enabled, while VLM suspect flags require an
// explicit acknowledgment checkbox before Approve is enabled.
//
// The server is always authoritative; client-side checks only fail fast. The
// error contract below maps each error_code envelope to inline copy.

(function () {
  "use strict";

  var MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  // Client abort budget. The server VLM timeout is 60s; this allows headroom
  // for the surrounding round trip so a client-side abort is distinguishable
  // from a server-reported VLM timeout/unavailable (which arrives as an
  // envelope, not an abort).
  var CLIENT_TIMEOUT_MS = 75 * 1000;
  var STORAGE_KEY = "meme_upload_pending_id";

  var root = document.querySelector("[data-upload-root]");
  if (!root) {
    return;
  }

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  var els = {
    pickStep: root.querySelector('[data-step="pick"]'),
    fileInput: root.querySelector("[data-file-input]"),
    preview: root.querySelector("[data-preview]"),
    previewImg: root.querySelector("[data-preview-img]"),
    pickError: root.querySelector("[data-pick-error]"),
    analyzeBtn: root.querySelector("[data-analyze-btn]"),
    spinner: root.querySelector("[data-spinner]"),
    analyzeError: root.querySelector("[data-analyze-error]"),
    reviewStep: root.querySelector('[data-step="review"]'),
    duplicateWarning: root.querySelector("[data-duplicate-warning]"),
    reviewForm: root.querySelector("[data-review-form]"),
    slotList: root.querySelector("[data-slot-list]"),
    ackRow: root.querySelector("[data-ack-row]"),
    ackCheckbox: root.querySelector("[data-ack-checkbox]"),
    ackLabel: root.querySelector("[data-ack-label]"),
    approveError: root.querySelector("[data-approve-error]"),
    approveBtn: root.querySelector("[data-approve-btn]"),
    discardBtn: root.querySelector("[data-discard-btn]"),
    doneStep: root.querySelector('[data-step="done"]'),
    successMessage: root.querySelector("[data-success-message]"),
    browseLink: root.querySelector("[data-browse-link]"),
    resume: root.querySelector("[data-resume]"),
    resumeDiscard: root.querySelector("[data-resume-discard]")
  };

  var state = {
    file: null,
    previewUrl: null,
    pendingId: null,
    suspectFlags: []
  };

  // --- DOM helpers ---------------------------------------------------------

  function show(el) {
    if (el) {
      el.hidden = false;
    }
  }

  function hide(el) {
    if (el) {
      el.hidden = true;
    }
  }

  function setMessage(el, text) {
    if (!el) {
      return;
    }
    el.textContent = text;
    if (text) {
      show(el);
    } else {
      hide(el);
    }
  }

  function field(name) {
    return els.reviewForm.querySelector('[data-field="' + name + '"]');
  }

  // --- pending-id bookkeeping ---------------------------------------------

  function setPendingId(id) {
    state.pendingId = id;
    if (id) {
      try {
        window.sessionStorage.setItem(STORAGE_KEY, id);
      } catch (err) {
        // sessionStorage may be unavailable (private mode); the in-memory id
        // still drives the beforeunload guard.
      }
    } else {
      try {
        window.sessionStorage.removeItem(STORAGE_KEY);
      } catch (err) {
        // ignore
      }
    }
  }

  // --- error contract ------------------------------------------------------
  //
  // Maps an error envelope to human copy. session-expired (401) is handled by
  // the caller so it can preserve the edited fields; everything else routes
  // through here. See errors.py for the authoritative codes/statuses.

  function reasonsText(envelope) {
    var errors = envelope && envelope.errors;
    if (!Array.isArray(errors) || errors.length === 0) {
      return "";
    }
    return errors
      .map(function (e) {
        return e && e.reason ? String(e.reason) : "";
      })
      .filter(Boolean)
      .join(", ");
  }

  function duplicateTemplateId(envelope) {
    var reasons = reasonsText(envelope);
    var match = reasons.match(/duplicate:(\S+)/);
    return match ? match[1] : "";
  }

  function describeError(envelope) {
    var code = envelope && envelope.error_code;
    switch (code) {
      case "UPLOAD_REJECTED":
        return (
          "This image was rejected (size, type, or it looked malformed). " +
          "Use a PNG, JPEG, or WebP under 10 MB."
        );
      case "DUPLICATE_TEMPLATE":
        var dupId = duplicateTemplateId(envelope);
        return dupId
          ? "This image already exists as template " + dupId + "."
          : "This image already exists as a template.";
      case "RATE_LIMITED":
        return "You have uploaded too many images recently. Try again later.";
      case "VLM_OUTPUT_SUSPECT":
        return (
          "The proposed metadata was flagged as suspect. Review and " +
          "acknowledge it before approving."
        );
      case "INVALID_INPUT":
        if (reasonsText(envelope).indexOf("name_required") !== -1) {
          return "A name is required, and it cannot be the placeholder default.";
        }
        return "Some fields were invalid: " + (reasonsText(envelope) || "check your input") + ".";
      case "FORBIDDEN":
        return "Your session security check failed. Reload the page and try again.";
      case "NOT_FOUND":
        return "This upload is no longer available. Start over.";
      case "VLM_UNAVAILABLE":
        return (
          "The description service is unavailable right now, so fields were " +
          "left blank. You can still fill them in and approve."
        );
      default:
        return "Something went wrong. Please try again.";
    }
  }

  // --- file selection + preview -------------------------------------------

  function clearPreview() {
    if (state.previewUrl) {
      URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = null;
    }
  }

  function onFileChange() {
    setMessage(els.pickError, "");
    clearPreview();
    hide(els.preview);
    state.file = null;
    els.analyzeBtn.disabled = true;

    var file = els.fileInput.files && els.fileInput.files[0];
    if (!file) {
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setMessage(
        els.pickError,
        "That file is larger than 10 MB. Choose a smaller image."
      );
      return;
    }
    state.file = file;
    state.previewUrl = URL.createObjectURL(file);
    els.previewImg.src = state.previewUrl;
    show(els.preview);
    els.analyzeBtn.disabled = false;
  }

  function fileToBase64(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        // result is "data:<mime>;base64,<payload>"; strip the prefix.
        var result = String(reader.result);
        var comma = result.indexOf(",");
        resolve(comma >= 0 ? result.slice(comma + 1) : result);
      };
      reader.onerror = function () {
        reject(new Error("read_failed"));
      };
      reader.readAsDataURL(file);
    });
  }

  // --- analyze -------------------------------------------------------------

  async function onAnalyze() {
    // Snapshot the file once: state.file can change across the awaits below if the
    // user re-picks mid-flight, which would otherwise send old bytes with the new
    // file's name/mime.
    var file = state.file;
    if (!file) {
      return;
    }
    setMessage(els.analyzeError, "");
    els.analyzeBtn.disabled = true;
    els.fileInput.disabled = true;
    show(els.spinner);

    var controller = new AbortController();
    var timer = window.setTimeout(function () {
      controller.abort();
    }, CLIENT_TIMEOUT_MS);

    try {
      var contentBase64 = await fileToBase64(file);
      var response = await fetch("/upload/analyze", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken
        },
        body: JSON.stringify({
          filename: file.name,
          mime: file.type,
          content_base64: contentBase64,
          title_hint: file.name
        }),
        signal: controller.signal
      });
      var envelope = await response.json();
      if (!response.ok || !envelope.ok) {
        if (response.status === 401) {
          showSessionExpired(els.analyzeError);
        } else {
          setMessage(els.analyzeError, describeError(envelope));
        }
        return;
      }
      enterReview(envelope.data);
    } catch (err) {
      if (err && err.name === "AbortError") {
        setMessage(
          els.analyzeError,
          "Analysis timed out in your browser before the server responded. " +
            "Check your connection and try again. (This is a client timeout, " +
            "not the description service being unavailable.)"
        );
      } else {
        setMessage(els.analyzeError, "Could not reach the server. Try again.");
      }
    } finally {
      window.clearTimeout(timer);
      hide(els.spinner);
      els.fileInput.disabled = false;
      // Re-enable analyze only if we did not advance to review.
      if (els.reviewStep.hidden) {
        els.analyzeBtn.disabled = !state.file;
      }
    }
  }

  // --- review --------------------------------------------------------------

  function enterReview(data) {
    setPendingId(data.pending_upload_id);
    var metadata = data.metadata || {};
    field("name").value = metadata.name || "";
    field("description").value = metadata.description || "";
    field("emotion").value = metadata.emotion || "";
    field("usage_context").value = metadata.usage_context || "";
    field("tags").value = Array.isArray(metadata.tags) ? metadata.tags.join(", ") : "";

    renderSlots(data.slot_definitions || []);
    renderDuplicate(data.duplicate || {});
    renderSuspect(data.suspect_flags || []);

    // Hide the pick controls so a second analyze cannot start (and orphan this
    // pending row) while a review is open; clear any stale approve/done copy.
    hide(els.pickStep);
    setMessage(els.approveError, "");
    hide(els.doneStep);
    show(els.reviewStep);
  }

  function renderSlots(slots) {
    els.slotList.textContent = "";
    if (!slots.length) {
      var empty = document.createElement("li");
      empty.textContent = "No slots proposed.";
      els.slotList.appendChild(empty);
      return;
    }
    slots.forEach(function (slot) {
      var item = document.createElement("li");
      var name = slot && slot.name ? slot.name : "slot";
      var position = slot && slot.position ? slot.position : "";
      item.textContent = position ? name + " (" + position + ")" : name;
      els.slotList.appendChild(item);
    });
  }

  function renderDuplicate(duplicate) {
    // Near-duplicate: non-blocking. Name the nearest template; Approve stays on.
    if (duplicate.action === "warn" && duplicate.template_id) {
      setMessage(
        els.duplicateWarning,
        "This looks similar to an existing template (" +
          duplicate.template_id +
          "). You can still approve it if it is genuinely different."
      );
    } else {
      hide(els.duplicateWarning);
    }
  }

  function renderSuspect(flags) {
    // VLM suspect flags BLOCK approval until acknowledged (R12).
    state.suspectFlags = flags;
    if (flags.length) {
      els.ackLabel.textContent =
        "The proposed metadata was flagged (" +
        flags.join(", ") +
        "). I have reviewed it and want to approve anyway.";
      show(els.ackRow);
      els.ackCheckbox.checked = false;
      els.approveBtn.disabled = true;
    } else {
      hide(els.ackRow);
      els.approveBtn.disabled = false;
    }
  }

  function onAckChange() {
    els.approveBtn.disabled = !els.ackCheckbox.checked;
  }

  // --- approve -------------------------------------------------------------

  function collectMetadata() {
    var tags = field("tags")
      .value.split(",")
      .map(function (t) {
        return t.trim();
      })
      .filter(Boolean);
    return {
      name: field("name").value.trim(),
      description: field("description").value,
      emotion: field("emotion").value,
      usage_context: field("usage_context").value,
      tags: tags,
      format: "static"
    };
  }

  async function onApprove(event) {
    event.preventDefault();
    if (!state.pendingId) {
      return;
    }
    setMessage(els.approveError, "");
    els.approveBtn.disabled = true;
    // Disable Discard too, so a click during the in-flight approve cannot race the
    // two server operations on the same pending row.
    els.discardBtn.disabled = true;

    try {
      var metadata = collectMetadata();
      var response = await fetch("/upload/approve/" + encodeURIComponent(state.pendingId), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken
        },
        body: JSON.stringify({
          metadata: metadata,
          ack_suspect: state.suspectFlags.length ? els.ackCheckbox.checked : false
        })
      });
      var envelope = await response.json();
      if (!response.ok || !envelope.ok) {
        if (response.status === 401) {
          // Preserve edited fields in the DOM so the friend can re-auth in a
          // new tab and resubmit (error contract: 401 mid-review).
          showSessionExpired(els.approveError);
        } else {
          setMessage(els.approveError, describeError(envelope));
        }
        els.approveBtn.disabled = state.suspectFlags.length ? !els.ackCheckbox.checked : false;
        els.discardBtn.disabled = false;
        return;
      }
      enterDone(metadata.name);
    } catch (err) {
      setMessage(els.approveError, "Could not reach the server. Try again.");
      els.approveBtn.disabled = state.suspectFlags.length ? !els.ackCheckbox.checked : false;
      els.discardBtn.disabled = false;
    }
  }

  function enterDone(name) {
    // The template now references the blob; clear the pending id so the
    // beforeunload guard and resume affordance no longer fire.
    setPendingId(null);
    setMessage(els.successMessage, 'Saved "' + name + '" to the library.');
    els.browseLink.href = "/browse?q=" + encodeURIComponent(name);
    hide(els.reviewStep);
    show(els.doneStep);
  }

  // --- discard -------------------------------------------------------------

  async function discardPending(id) {
    if (!id) {
      return true;
    }
    try {
      await fetch("/upload/discard/" + encodeURIComponent(id), {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken }
      });
      return true;
    } catch (err) {
      return false;
    }
  }

  async function onDiscard() {
    var id = state.pendingId;
    setPendingId(null);
    await discardPending(id);
    window.location.href = "/upload";
  }

  async function onResumeDiscard() {
    var id = state.pendingId;
    setPendingId(null);
    hide(els.resume);
    await discardPending(id);
  }

  // --- session-expired -----------------------------------------------------

  function showSessionExpired(target) {
    // Field values stay in the DOM; only an inline message + login link are
    // injected so re-auth + resubmit is possible (error contract).
    if (!target) {
      return;
    }
    target.textContent = "Session expired - log in again. ";
    var link = document.createElement("a");
    link.href = "/auth/login?next=/upload";
    link.textContent = "Log in";
    target.appendChild(link);
    show(target);
  }

  // --- resume-on-return ----------------------------------------------------

  function offerResume() {
    var stored = null;
    try {
      stored = window.sessionStorage.getItem(STORAGE_KEY);
    } catch (err) {
      stored = null;
    }
    if (stored) {
      state.pendingId = stored;
      show(els.resume);
    }
  }

  // --- wiring --------------------------------------------------------------

  function onBeforeUnload(event) {
    if (state.pendingId && els.doneStep.hidden) {
      event.preventDefault();
      event.returnValue = "";
      return "";
    }
    return undefined;
  }

  els.fileInput.addEventListener("change", onFileChange);
  els.analyzeBtn.addEventListener("click", onAnalyze);
  els.ackCheckbox.addEventListener("change", onAckChange);
  els.reviewForm.addEventListener("submit", onApprove);
  els.discardBtn.addEventListener("click", onDiscard);
  els.resumeDiscard.addEventListener("click", onResumeDiscard);
  window.addEventListener("beforeunload", onBeforeUnload);

  offerResume();
})();
