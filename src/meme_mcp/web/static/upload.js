// Single-page /upload client (KTD1). Vanilla JS, no framework or build step.
//
// Flow: choose-or-drop a file -> local preview + size pre-check (shown as a
// file card) -> Analyze (base64 JSON POST with X-CSRF-Token, animated status,
// AbortController timeout) -> edit proposed metadata (read-only slots, KTD10)
// -> Approve -> success link to /browse. A presentational 3-step wayfinder
// (data-current on the root) tracks pick/review/done.
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
    dropzone: root.querySelector("[data-dropzone]"),
    fileInput: root.querySelector("[data-file-input]"),
    filecard: root.querySelector("[data-filecard]"),
    thumbImg: root.querySelector("[data-thumb-img]"),
    previewImg: root.querySelector("[data-preview-img]"),
    pickError: root.querySelector("[data-pick-error]"),
    identifyToggle: root.querySelector("[data-identify-toggle]"),
    analyzeBtn: root.querySelector("[data-analyze-btn]"),
    spinner: root.querySelector("[data-spinner]"),
    analyzingTitle: root.querySelector("[data-analyzing-title]"),
    analyzingHint: root.querySelector("[data-analyzing-hint]"),
    originStatus: root.querySelector("[data-origin-status]"),
    analyzeError: root.querySelector("[data-analyze-error]"),
    reviewStep: root.querySelector('[data-step="review"]'),
    reviewHeading: root.querySelector('[data-step="review"] h2'),
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
    successSub: root.querySelector("[data-success-sub]"),
    browseLink: root.querySelector("[data-browse-link]"),
    resume: root.querySelector("[data-resume]"),
    resumeDiscard: root.querySelector("[data-resume-discard]"),
    steps: Array.prototype.slice.call(root.querySelectorAll(".upload-stepper-item"))
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

  function setText(selector, text) {
    var nodes = root.querySelectorAll(selector);
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].textContent = text;
    }
  }

  function formatBytes(bytes) {
    if (bytes < 1024) {
      return bytes + " B";
    }
    var kb = bytes / 1024;
    if (kb < 1024) {
      return kb.toFixed(kb < 10 ? 1 : 0) + " KB";
    }
    return (kb / 1024).toFixed(1) + " MB";
  }

  // Presentational wayfinder. The root's data-current attribute drives the
  // step-dot styling in CSS; this also moves aria-current so assistive tech
  // announces the active step rather than reading the strip as decoration.
  function setStep(phase) {
    root.dataset.current = phase;
    var activeKey = phase === "pick" ? "pick" : phase === "done" ? "done" : "review";
    els.steps.forEach(function (item) {
      if (item.getAttribute("data-key") === activeKey) {
        item.setAttribute("aria-current", "step");
      } else {
        item.removeAttribute("aria-current");
      }
    });
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
        return t("js.upload.error.rejected");
      case "DUPLICATE_TEMPLATE":
        var dupId = duplicateTemplateId(envelope);
        return dupId
          ? t("js.upload.error.duplicate_with_id", { id: dupId })
          : t("js.upload.error.duplicate");
      case "RATE_LIMITED":
        return t("js.upload.error.rate_limited");
      case "VLM_OUTPUT_SUSPECT":
        return t("js.upload.error.suspect");
      case "INVALID_INPUT":
        if (reasonsText(envelope).indexOf("name_required") !== -1) {
          return t("js.upload.error.name_required");
        }
        return t("js.upload.error.invalid", {
          reasons: reasonsText(envelope) || t("js.upload.error.invalid_fallback_reason")
        });
      case "FORBIDDEN":
        return t("js.upload.error.forbidden");
      case "NOT_FOUND":
        return t("js.upload.error.not_found");
      case "VLM_UNAVAILABLE":
        return t("js.upload.error.vlm_unavailable");
      default:
        return t("js.upload.error.generic");
    }
  }

  // --- file selection + preview -------------------------------------------

  function clearPreview() {
    if (state.previewUrl) {
      URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = null;
    }
  }

  // Accept a File from either the native input change or a drag-drop, run the
  // same size pre-check, and build the local preview. The createObjectURL /
  // revokeObjectURL lifecycle stays single-owner: one URL per selected file,
  // revoked before the next.
  function useFile(file) {
    setMessage(els.pickError, "");
    clearPreview();
    hide(els.filecard);
    state.file = null;
    els.analyzeBtn.disabled = true;

    if (!file) {
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setMessage(els.pickError, t("js.upload.error.too_large"));
      return;
    }
    state.file = file;
    state.previewUrl = URL.createObjectURL(file);
    setText("[data-file-name]", file.name);
    setText("[data-file-size]", formatBytes(file.size));
    setText("[data-file-dims]", "");
    // Attach onload before assigning src so a fast/cached decode cannot resolve
    // before the dimensions handler is registered.
    els.previewImg.onload = function () {
      if (els.previewImg.naturalWidth) {
        setText(
          "[data-file-dims]",
          els.previewImg.naturalWidth + " x " + els.previewImg.naturalHeight
        );
      }
    };
    els.thumbImg.src = state.previewUrl;
    els.previewImg.src = state.previewUrl;
    show(els.filecard);
    els.analyzeBtn.disabled = false;
    setStep("pick");
  }

  function onFileChange() {
    useFile(els.fileInput.files && els.fileInput.files[0]);
  }

  // --- drag and drop -------------------------------------------------------

  function onDragOver(event) {
    event.preventDefault();
    els.dropzone.classList.add("is-dragover");
  }

  function onDragLeave(event) {
    // dragleave fires when crossing into child nodes too; only clear the armed
    // state when the pointer actually leaves the dropzone (or the window).
    if (!els.dropzone.contains(event.relatedTarget)) {
      els.dropzone.classList.remove("is-dragover");
    }
  }

  function onDrop(event) {
    event.preventDefault();
    els.dropzone.classList.remove("is-dragover");
    var transfer = event.dataTransfer;
    var file = transfer && transfer.files && transfer.files[0];
    if (file) {
      useFile(file);
    }
  }

  // Swallow stray drops outside the dropzone. Without these the browser
  // navigates the tab to the dropped file -- abandoning the flow, and mid-review
  // tripping the beforeunload guard and orphaning the pending row. The dropzone
  // is also display:none during analyze/review/done, so the whole window would
  // otherwise be a live (wrong) drop target. Genuine dropzone drops are still
  // handled by onDrop (the document drop guard skips targets inside the zone).
  function isFileDrag(event) {
    var types = event.dataTransfer && event.dataTransfer.types;
    // Only file drags carry the "Files" type. Text/selection drags must pass
    // through untouched so they can still be dropped into the review fields.
    return !!types && Array.prototype.indexOf.call(types, "Files") !== -1;
  }

  function onDocumentDragOver(event) {
    if (isFileDrag(event)) {
      event.preventDefault();
    }
  }

  function onDocumentDrop(event) {
    if (isFileDrag(event) && !els.dropzone.contains(event.target)) {
      event.preventDefault();
    }
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
    // Always send identify_online explicitly so the server's default does not
    // decide for us: checked when the toggle is present and on, false otherwise
    // (including when the feature is off and no toggle renders).
    var identifyOnline = !!(els.identifyToggle && els.identifyToggle.checked);
    setMessage(els.analyzeError, "");
    els.analyzeBtn.disabled = true;
    els.fileInput.disabled = true;
    setStep("analyze");
    if (identifyOnline) {
      // The combined wait is rate-limit + Vision (~8s) + VLM (~60s), so set the
      // extended-wait copy when an online lookup will run.
      setText("[data-analyzing-title]", t("js.upload.analyzing.online_title"));
      setText("[data-analyzing-hint]", t("js.upload.analyzing.online_hint"));
    }
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
          title_hint: file.name,
          identify_online: identifyOnline
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
        setMessage(els.analyzeError, t("js.upload.error.timeout"));
      } else {
        setMessage(els.analyzeError, t("js.upload.error.network"));
      }
    } finally {
      window.clearTimeout(timer);
      hide(els.spinner);
      els.fileInput.disabled = false;
      // Re-enable analyze only if we did not advance to review, and step the
      // wayfinder back to pick so the dropzone reappears.
      if (els.reviewStep.hidden) {
        els.analyzeBtn.disabled = !state.file;
        setStep("pick");
      }
    }
  }

  // --- review --------------------------------------------------------------

  function enterReview(data) {
    var metadata = data.metadata || {};
    field("name").value = metadata.name || "";
    field("description").value = metadata.description || "";
    field("emotion").value = metadata.emotion || "";
    field("usage_context").value = metadata.usage_context || "";
    field("tags").value = Array.isArray(metadata.tags) ? metadata.tags.join(", ") : "";

    var origin = metadata.origin || {};
    field("origin_name").value = origin.name || "";
    field("origin_source_url").value = origin.source_url || "";
    renderOriginStatus(data.reverse_image_status);

    renderSlots(data.slot_definitions || []);
    renderDuplicate(data.duplicate || {});
    renderSuspect(data.suspect_flags || []);

    // Hide the pick controls so a second analyze cannot start (and orphan this
    // pending row) while a review is open; clear any stale approve/done copy.
    hide(els.pickStep);
    setMessage(els.approveError, "");
    hide(els.doneStep);
    show(els.reviewStep);
    setStep("review");
    // Commit the pending id only once the review UI is live: if any render step
    // above threw on malformed data, the failure surfaces without arming the
    // beforeunload guard or resume affordance against an invisible review.
    setPendingId(data.pending_upload_id);
    // Move focus to the new step's heading; the Analyze button it was on is now
    // hidden, which would otherwise strand keyboard/screen-reader focus.
    if (els.reviewHeading) {
      els.reviewHeading.focus();
    }
  }

  // Distinguish "we looked and could not identify it" from "we did not look",
  // so the empty origin fields read honestly (R10). Silent degradation still
  // applies -- no error is shown, just context-appropriate helper copy.
  function renderOriginStatus(status) {
    if (!els.originStatus) {
      return;
    }
    var msg = "";
    if (status === "success") {
      msg = t("js.upload.origin.success");
    } else if (status === "no_match" || status === "low_confidence") {
      msg = t("js.upload.origin.no_match");
    } else if (status === "skipped" || status === "unavailable") {
      msg = t("js.upload.origin.skipped");
    }
    els.originStatus.textContent = msg;
  }

  // Client-side https check, mirroring the server allowlist. UX only -- the
  // server store-sanitize is the authoritative guarantee. Blanks a non-https or
  // unparseable URL before it is sent or could back a link.
  function safeHttpsUrl(value) {
    var url = String(value || "").trim();
    if (!url) {
      return "";
    }
    try {
      return new URL(url).protocol === "https:" ? url : "";
    } catch (err) {
      return "";
    }
  }

  function renderSlots(slots) {
    els.slotList.textContent = "";
    if (!slots.length) {
      var empty = document.createElement("li");
      empty.textContent = t("js.upload.slots.none");
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
        t("js.upload.duplicate_warning", { id: duplicate.template_id })
      );
    } else {
      hide(els.duplicateWarning);
    }
  }

  function renderSuspect(flags) {
    // VLM suspect flags BLOCK approval until acknowledged (R12).
    state.suspectFlags = flags;
    if (flags.length) {
      els.ackLabel.textContent = t("js.upload.suspect_ack", { flags: flags.join(", ") });
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
    var metadata = {
      name: field("name").value.trim(),
      description: field("description").value,
      emotion: field("emotion").value,
      usage_context: field("usage_context").value,
      tags: tags,
      format: "static"
    };
    // Collect origin only when at least one field is filled; omit it entirely
    // (do not send empty strings) so a blank origin leaves no stored block.
    var originName = field("origin_name").value.trim();
    var originUrl = safeHttpsUrl(field("origin_source_url").value);
    if (originName || originUrl) {
      metadata.origin = { name: originName, source_url: originUrl };
    }
    return metadata;
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
      setMessage(els.approveError, t("js.upload.error.network"));
      els.approveBtn.disabled = state.suspectFlags.length ? !els.ackCheckbox.checked : false;
      els.discardBtn.disabled = false;
    }
  }

  function enterDone(name) {
    // The template now references the blob; clear the pending id so the
    // beforeunload guard and resume affordance no longer fire.
    setPendingId(null);
    setMessage(els.successMessage, t("js.upload.done.saved", { name: name }));
    setMessage(els.successSub, t("js.upload.done.searchable", { name: name }));
    els.browseLink.href = "/browse?q=" + encodeURIComponent(name);
    hide(els.reviewStep);
    show(els.doneStep);
    setStep("done");
    // Terminal state: release the last object URL (the preview lives in the
    // now-hidden review step) so it is not held until the tab closes.
    els.previewImg.removeAttribute("src");
    els.thumbImg.removeAttribute("src");
    clearPreview();
    // Move focus to the success message; the Approve button it was on is now
    // hidden. The message carries tabindex="-1" so it can receive focus.
    if (els.successMessage) {
      els.successMessage.focus();
    }
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
    target.textContent = t("js.upload.session_expired");
    var link = document.createElement("a");
    link.href = "/auth/login?next=/upload";
    link.textContent = t("js.upload.session_expired_login");
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
  els.dropzone.addEventListener("dragover", onDragOver);
  els.dropzone.addEventListener("dragleave", onDragLeave);
  els.dropzone.addEventListener("drop", onDrop);
  document.addEventListener("dragover", onDocumentDragOver);
  document.addEventListener("drop", onDocumentDrop);
  els.analyzeBtn.addEventListener("click", onAnalyze);
  els.ackCheckbox.addEventListener("change", onAckChange);
  els.reviewForm.addEventListener("submit", onApprove);
  els.discardBtn.addEventListener("click", onDiscard);
  els.resumeDiscard.addEventListener("click", onResumeDiscard);
  window.addEventListener("beforeunload", onBeforeUnload);

  offerResume();
})();
