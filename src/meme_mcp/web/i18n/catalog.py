"""Bilingual message catalog: the single source of truth for UI copy.

``MESSAGES`` maps a dotted message id to its ``en`` and ``zh-TW`` strings. Keys
prefixed ``js.`` are shipped to the browser in the ``window.I18N`` blob (KTD6);
all other keys are server-render only. Values use **named placeholders only**
(``{count}``, ``{login}``) -- see ``core.lint_placeholders``.

Plural counts use a ``.one`` / ``.other`` pair selected by ``core.plural``;
``zh-TW`` has no plural inflection, so both forms carry the same string.

Translation *correctness* (not just key presence) is gated by a native zh-TW
review before shipping (R-risk2); these values are a faithful first pass.
"""

from __future__ import annotations

MESSAGES: dict[str, dict[str, str]] = {
    # --- shared --------------------------------------------------------------
    "common.signed_in_as": {"en": "Signed in as {login}.", "zh-TW": "已以 {login} 登入。"},
    # --- nav (base.html) -----------------------------------------------------
    "nav.browse": {"en": "Browse", "zh-TW": "瀏覽"},
    "nav.upload": {"en": "Upload", "zh-TW": "上傳"},
    "nav.account": {"en": "Account", "zh-TW": "帳號"},
    "nav.logout": {"en": "Sign out", "zh-TW": "登出"},
    "nav.aria_primary": {"en": "Primary", "zh-TW": "主要導覽"},
    # --- PAT-expiry banner (base.html) ---------------------------------------
    "banner.pat_expiry.one": {
        "en": "Your PAT expires in {count} day.",
        "zh-TW": "您的 PAT 將在 {count} 天後過期。",
    },
    "banner.pat_expiry.other": {
        "en": "Your PAT expires in {count} days.",
        "zh-TW": "您的 PAT 將在 {count} 天後過期。",
    },
    "banner.renew_link": {"en": "Renew it in account settings", "zh-TW": "在帳號設定中更新"},
    "banner.renew_suffix": {"en": "to avoid losing access.", "zh-TW": "以免失去存取權。"},
    # --- landing.html --------------------------------------------------------
    "landing.tagline": {
        "en": "A private meme studio for friends — find the right template, "
        "fill it in, and ship it.",
        "zh-TW": "專為朋友打造的迷因工作室 — 找到合適的範本、填好內容，立刻發出去。",
    },
    "landing.cta.browse": {"en": "Browse templates", "zh-TW": "瀏覽範本"},
    "landing.cta.upload": {"en": "Upload a template", "zh-TW": "上傳範本"},
    "landing.signin_prompt": {
        "en": "Sign in to browse the template library and contribute your own.",
        "zh-TW": "登入即可瀏覽範本庫，並貢獻你自己的範本。",
    },
    "landing.cta.signin": {"en": "Sign in with GitHub", "zh-TW": "使用 GitHub 登入"},
    "landing.cta.signin_google": {"en": "Sign in with Google", "zh-TW": "使用 Google 登入"},
    "landing.mcp.heading": {"en": "For MCP clients", "zh-TW": "給 MCP 用戶端"},
    "landing.mcp.body_prefix": {
        "en": "Connect an agent to the hosted MCP endpoint at",
        "zh-TW": "以 bearer PAT 將代理程式連線到代管的 MCP 端點",
    },
    "landing.mcp.body_suffix": {
        "en": "with a bearer PAT. See the README for client snippets.",
        "zh-TW": "。用戶端程式碼範例請見 README。",
    },
    # --- login.html (provider chooser) ---------------------------------------
    "login.heading": {"en": "Sign in", "zh-TW": "登入"},
    "login.prompt": {
        "en": "Choose how you would like to sign in.",
        "zh-TW": "請選擇登入方式。",
    },
    "login.github": {"en": "Sign in with GitHub", "zh-TW": "使用 GitHub 登入"},
    "login.google": {"en": "Sign in with Google", "zh-TW": "使用 Google 登入"},
    # --- restricted.html -----------------------------------------------------
    "restricted.heading": {"en": "Access restricted", "zh-TW": "存取受限"},
    "restricted.body": {
        "en": "Your account is signed in, but it is not on the allowlist "
        "for this meme-mcp instance, so a session was not created.",
        "zh-TW": "你的帳號已登入，但不在這個 meme-mcp 執行個體的允許清單中，"
        "因此沒有建立工作階段。",
    },
    "restricted.request_prefix": {
        "en": "To request access, ask the operator (",
        "zh-TW": "若要申請存取權，請聯絡管理者（",
    },
    "restricted.request_suffix": {
        "en": ") to add you to the allowlist.",
        "zh-TW": "）將你加入允許清單。",
    },
    "restricted.request_generic": {
        "en": "To request access, contact the operator to be added to the allowlist.",
        "zh-TW": "若要申請存取權，請聯絡管理者將你加入允許清單。",
    },
    "restricted.back": {"en": "Back to browse", "zh-TW": "返回瀏覽"},
    # --- browse.html ---------------------------------------------------------
    "browse.eyebrow": {"en": "Template library", "zh-TW": "範本庫"},
    "browse.match.one": {
        "en": "{count} match for “{query}”",
        "zh-TW": "{count} 筆符合「{query}」",
    },
    "browse.match.other": {
        "en": "{count} matches for “{query}”",
        "zh-TW": "{count} 筆符合「{query}」",
    },
    "browse.count.one": {
        "en": "{count} template ready to render",
        "zh-TW": "{count} 個範本可供使用",
    },
    "browse.count.other": {
        "en": "{count} templates ready to render",
        "zh-TW": "{count} 個範本可供使用",
    },
    "browse.search.placeholder": {"en": "deploy, ci, reaction…", "zh-TW": "deploy、ci、reaction…"},
    "browse.search.label": {"en": "Search templates", "zh-TW": "搜尋範本"},
    "browse.search.submit": {"en": "Search", "zh-TW": "搜尋"},
    "browse.card.alt": {"en": "{name} template", "zh-TW": "{name} 範本"},
    "browse.slot.one": {"en": "{count} slot", "zh-TW": "{count} 個欄位"},
    "browse.slot.other": {"en": "{count} slots", "zh-TW": "{count} 個欄位"},
    "browse.empty.query": {
        "en": "No templates match “{query}”. Try a broader search.",
        "zh-TW": "沒有符合「{query}」的範本。試試更廣泛的搜尋。",
    },
    "browse.empty.none": {"en": "No templates yet.", "zh-TW": "目前還沒有範本。"},
    # --- detail.html ---------------------------------------------------------
    "detail.back": {"en": "Template library", "zh-TW": "範本庫"},
    "detail.attrs.heading": {"en": "Attributes", "zh-TW": "屬性"},
    "detail.attr.emotion": {"en": "Emotion", "zh-TW": "情緒"},
    "detail.attr.usage_context": {"en": "Usage context", "zh-TW": "使用情境"},
    "detail.attr.format": {"en": "Format", "zh-TW": "格式"},
    "detail.attr.library": {"en": "Library", "zh-TW": "來源庫"},
    "detail.attr.slug": {"en": "Slug", "zh-TW": "代稱"},
    "detail.slots.heading": {"en": "Slots", "zh-TW": "文字欄位"},
    "detail.slot.fallback": {"en": "Slot {index}", "zh-TW": "欄位 {index}"},
    "detail.slots.empty": {
        "en": "This template has no editable slots.",
        "zh-TW": "這個範本沒有可編輯的文字欄位。",
    },
    "detail.origin.title": {"en": "Origin", "zh-TW": "出處"},
    "detail.source.title": {"en": "Source", "zh-TW": "來源"},
    "detail.origin.identified_as": {"en": "Identified as", "zh-TW": "辨識為"},
    "detail.origin.source": {"en": "Source", "zh-TW": "來源"},
    "detail.origin.reference": {"en": "Reference", "zh-TW": "參考連結"},
    "detail.fingerprint.summary": {"en": "Fingerprint", "zh-TW": "指紋"},
    "detail.fingerprint.exact": {"en": "Exact hash", "zh-TW": "精確雜湊"},
    "detail.fingerprint.perceptual": {"en": "Perceptual hash", "zh-TW": "感知雜湊"},
    "detail.fingerprint.image_path": {"en": "Image path", "zh-TW": "圖片路徑"},
    # --- account.html --------------------------------------------------------
    "account.eyebrow": {"en": "Account", "zh-TW": "帳號"},
    "account.heading": {"en": "MCP access token", "zh-TW": "MCP 存取權杖"},
    "account.current_token": {"en": "Current token", "zh-TW": "目前的權杖"},
    "account.scope": {"en": "Scope", "zh-TW": "權限範圍"},
    "account.expires": {"en": "Expires", "zh-TW": "到期"},
    "account.last_used": {"en": "Last used", "zh-TW": "上次使用"},
    "account.generate": {"en": "Generate", "zh-TW": "產生"},
    "account.regenerate": {"en": "Regenerate", "zh-TW": "重新產生"},
    "account.generate_heading": {"en": "Generate token", "zh-TW": "產生權杖"},
    "account.regenerate_heading": {"en": "Regenerate token", "zh-TW": "重新產生權杖"},
    "account.expiry_label": {"en": "Expiry", "zh-TW": "到期時間"},
    "account.expiry_days": {"en": "{days} days", "zh-TW": "{days} 天"},
    "account.regenerate_note": {
        "en": "Regenerating kills the old token immediately.",
        "zh-TW": "重新產生會立即作廢舊的權杖。",
    },
    "account.reveal.heading": {"en": "Copy this token now", "zh-TW": "立即複製此權杖"},
    "account.reveal.body": {
        "en": "This plaintext token cannot be shown again after you leave or reload this page.",
        "zh-TW": "離開或重新整理此頁面後，將無法再次顯示此明文權杖。",
    },
    "account.revoke.heading": {"en": "Revoke token", "zh-TW": "撤銷權杖"},
    "account.revoke.body": {
        "en": "Revoking the token does not sign you out of the web session.",
        "zh-TW": "撤銷權杖不會讓你登出網頁工作階段。",
    },
    "account.revoke.button": {"en": "Revoke active token", "zh-TW": "撤銷使用中的權杖"},
    # --- upload.html ---------------------------------------------------------
    "upload.heading": {"en": "Upload a template", "zh-TW": "上傳範本"},
    "upload.steps.aria_label": {"en": "Progress", "zh-TW": "進度"},
    "upload.img.preview_alt": {"en": "Selected image preview", "zh-TW": "已選取的圖片預覽"},
    "upload.noscript": {
        "en": "This page needs JavaScript. The upload flow previews your image, "
        "analyzes it, and lets you edit the proposed metadata entirely in the "
        "browser, so it cannot run with JavaScript disabled.",
        "zh-TW": "此頁面需要 JavaScript。上傳流程會在瀏覽器中預覽圖片、分析圖片，"
        "並讓你編輯建議的描述資料，因此在停用 JavaScript 時無法運作。",
    },
    "upload.helper": {
        "en": "Accepted formats: PNG, JPEG, or WebP, up to 10 MB. After you "
        "analyze, describing the image may take several seconds while the model "
        "inspects it.",
        "zh-TW": "支援格式：PNG、JPEG 或 WebP，上限 10 MB。分析後，模型檢視圖片並"
        "產生描述可能需要幾秒鐘。",
    },
    "upload.step.pick": {"en": "Pick", "zh-TW": "選擇"},
    "upload.step.review": {"en": "Review", "zh-TW": "檢視"},
    "upload.step.done": {"en": "Done", "zh-TW": "完成"},
    "upload.dropzone.idle": {
        "en": "Drag an image here, or click to choose",
        "zh-TW": "將圖片拖曳到這裡，或點擊選擇",
    },
    "upload.dropzone.drop": {"en": "Drop to use this image", "zh-TW": "放開以使用這張圖片"},
    "upload.dropzone.hint": {
        "en": "PNG, JPEG or WebP — up to 10 MB",
        "zh-TW": "PNG、JPEG 或 WebP — 上限 10 MB",
    },
    "upload.identify.toggle": {"en": "Identify this meme online", "zh-TW": "在線上辨識這個迷因"},
    "upload.identify.help": {
        "en": "Sends your image to Google to identify the meme and fill in its "
        "real name and usage. Uncheck for private or original images you do not "
        "want sent off this server.",
        "zh-TW": "會將你的圖片傳送給 Google 以辨識迷因，並填入其真實名稱與用途。"
        "若是不想傳出本伺服器的私人或原創圖片，請取消勾選。",
    },
    "upload.analyze": {"en": "Analyze", "zh-TW": "分析"},
    "upload.analyzing.title": {"en": "Looking at your image…", "zh-TW": "正在檢視你的圖片…"},
    "upload.analyzing.hint": {
        "en": "This usually takes 5–60 seconds.",
        "zh-TW": "通常需要 5–60 秒。",
    },
    "upload.analyzing.rm": {
        "en": "Analyzing the image. This may take several seconds.",
        "zh-TW": "正在分析圖片，可能需要幾秒鐘。",
    },
    "upload.review.heading": {"en": "Proposed metadata", "zh-TW": "建議的描述資料"},
    "upload.disclosure.summary": {"en": "Stored image differs", "zh-TW": "儲存的圖片會有差異"},
    "upload.disclosure.body": {
        "en": "The stored template is EXIF-stripped and re-encoded, so the saved "
        "image may differ slightly from the local preview shown here.",
        "zh-TW": "儲存的範本會移除 EXIF 並重新編碼，因此儲存的圖片可能與這裡顯示的"
        "本機預覽略有不同。",
    },
    "upload.field.name": {"en": "Name", "zh-TW": "名稱"},
    "upload.field.description": {"en": "Description", "zh-TW": "描述"},
    "upload.field.emotion": {"en": "Emotion", "zh-TW": "情緒"},
    "upload.field.usage_context": {"en": "Usage context", "zh-TW": "使用情境"},
    "upload.field.tags": {"en": "Tags (comma-separated)", "zh-TW": "標籤（以逗號分隔）"},
    "upload.field.tags_help": {
        "en": "Separate tags with commas. Do not put a comma inside a single tag.",
        "zh-TW": "以逗號分隔標籤。單一標籤內請勿使用逗號。",
    },
    "upload.field.origin_name": {"en": "Origin name (optional)", "zh-TW": "出處名稱（選填）"},
    "upload.field.origin_url": {
        "en": "Origin source URL (optional)",
        "zh-TW": "出處來源網址（選填）",
    },
    "upload.field.origin_url_help": {
        "en": "Where this meme comes from. Only https links are kept.",
        "zh-TW": "這個迷因的來源。只會保留 https 連結。",
    },
    "upload.slots.legend": {"en": "Slots (read-only)", "zh-TW": "文字欄位（唯讀）"},
    "upload.approve": {"en": "Approve", "zh-TW": "核准"},
    "upload.discard": {"en": "Discard", "zh-TW": "捨棄"},
    "upload.done.browse": {"en": "View it in browse", "zh-TW": "在瀏覽頁面查看"},
    "upload.done.another": {"en": "Upload another", "zh-TW": "再上傳一個"},
    "upload.resume.notice": {
        "en": "A previous upload was left in review. You can discard it and start over.",
        "zh-TW": "先前有一個上傳停留在檢視階段。你可以捨棄它並重新開始。",
    },
    "upload.resume.discard": {"en": "Discard and start over", "zh-TW": "捨棄並重新開始"},
    # --- client JS (shipped in window.I18N) ----------------------------------
    # Account token status enum values, rendered server-side here (account.html)
    # and client-side in account.js (U5) from the same keys, so first paint and
    # AJAX re-render agree.
    "js.token.state.none": {"en": "none", "zh-TW": "無"},
    "js.token.state.active": {"en": "active", "zh-TW": "使用中"},
    "js.token.state.expired": {"en": "expired", "zh-TW": "已過期"},
    "js.token.state.revoked": {"en": "revoked", "zh-TW": "已撤銷"},
    "js.token.scope.read": {"en": "read", "zh-TW": "唯讀"},
    "js.token.scope.readwrite": {"en": "readwrite", "zh-TW": "讀寫"},
    "js.token.none": {"en": "none", "zh-TW": "無"},
    "js.token.never": {"en": "never", "zh-TW": "從未"},
    "js.copy": {"en": "Copy", "zh-TW": "複製"},
    "js.copy.done": {"en": "Copied", "zh-TW": "已複製"},
    # account.js dynamic strings
    "js.account.generate": {"en": "Generate", "zh-TW": "產生"},
    "js.account.regenerate": {"en": "Regenerate", "zh-TW": "重新產生"},
    "js.account.confirm_regenerate": {
        "en": "Regenerate this token? The old token dies immediately.",
        "zh-TW": "要重新產生這個權杖嗎？舊的權杖會立即失效。",
    },
    "js.account.confirm_revoke": {
        "en": "Revoke the active token now?",
        "zh-TW": "要立即撤銷使用中的權杖嗎？",
    },
    # upload.js error contract
    "js.upload.error.rejected": {
        "en": "This image was rejected (size, type, or it looked malformed). "
        "Use a PNG, JPEG, or WebP under 10 MB.",
        "zh-TW": "這張圖片被拒絕（大小、格式，或看起來已損毀）。"
        "請使用 10 MB 以下的 PNG、JPEG 或 WebP。",
    },
    "js.upload.error.duplicate_with_id": {
        "en": "This image already exists as template {id}.",
        "zh-TW": "這張圖片已存在，範本為 {id}。",
    },
    "js.upload.error.duplicate": {
        "en": "This image already exists as a template.",
        "zh-TW": "這張圖片已存在於某個範本中。",
    },
    "js.upload.error.rate_limited": {
        "en": "You have uploaded too many images recently. Try again later.",
        "zh-TW": "你最近上傳的圖片太多了，請稍後再試。",
    },
    "js.upload.error.suspect": {
        "en": "The proposed metadata was flagged as suspect. Review and "
        "acknowledge it before approving.",
        "zh-TW": "建議的描述資料被標記為可疑。請先檢視並確認後再核准。",
    },
    "js.upload.error.name_required": {
        "en": "A name is required, and it cannot be the placeholder default.",
        "zh-TW": "名稱為必填，且不能是預設的佔位文字。",
    },
    "js.upload.error.invalid": {
        "en": "Some fields were invalid: {reasons}.",
        "zh-TW": "部分欄位無效：{reasons}。",
    },
    "js.upload.error.invalid_fallback_reason": {
        "en": "check your input",
        "zh-TW": "請檢查你的輸入",
    },
    "js.upload.error.forbidden": {
        "en": "Your session security check failed. Reload the page and try again.",
        "zh-TW": "你的工作階段安全檢查失敗。請重新整理頁面後再試。",
    },
    "js.upload.error.not_found": {
        "en": "This upload is no longer available. Start over.",
        "zh-TW": "這個上傳已不存在，請重新開始。",
    },
    "js.upload.error.vlm_unavailable": {
        "en": "The description service is unavailable right now, so fields were "
        "left blank. You can still fill them in and approve.",
        "zh-TW": "描述服務目前無法使用，因此欄位留白。你仍可自行填寫並核准。",
    },
    "js.upload.error.generic": {
        "en": "Something went wrong. Please try again.",
        "zh-TW": "發生錯誤，請再試一次。",
    },
    "js.upload.error.too_large": {
        "en": "That file is larger than 10 MB. Choose a smaller image.",
        "zh-TW": "這個檔案大於 10 MB，請選擇較小的圖片。",
    },
    "js.upload.error.timeout": {
        "en": "Analysis timed out in your browser before the server responded. "
        "Check your connection and try again. (This is a client timeout, not "
        "the description service being unavailable.)",
        "zh-TW": "在伺服器回應之前，分析就在你的瀏覽器中逾時了。請檢查連線後再試。"
        "（這是用戶端逾時，不是描述服務無法使用。）",
    },
    "js.upload.error.network": {
        "en": "Could not reach the server. Try again.",
        "zh-TW": "無法連線到伺服器，請再試一次。",
    },
    # upload.js analyze / origin / review status
    "js.upload.analyzing.online_title": {
        "en": "Looking up this meme online…",
        "zh-TW": "正在線上查詢這個迷因…",
    },
    "js.upload.analyzing.online_hint": {
        "en": "Checking the web to identify it, then describing it. This can take "
        "up to a minute.",
        "zh-TW": "正在搜尋網路以辨識它，接著產生描述。這可能需要長達一分鐘。",
    },
    "js.upload.origin.success": {
        "en": "Identified online. Edit if anything looks off.",
        "zh-TW": "已在線上辨識。如有不符請自行編輯。",
    },
    "js.upload.origin.no_match": {
        "en": "Could not confidently identify this meme online. Add its name and "
        "source if you know them.",
        "zh-TW": "無法在線上確定辨識這個迷因。若你知道其名稱與來源，請自行填寫。",
    },
    "js.upload.origin.skipped": {
        "en": "Online identification was not used. Add the meme's origin if you know it.",
        "zh-TW": "未使用線上辨識。若你知道這個迷因的出處，請自行填寫。",
    },
    "js.upload.slots.none": {"en": "No slots proposed.", "zh-TW": "沒有建議的文字欄位。"},
    "js.upload.duplicate_warning": {
        "en": "This looks similar to an existing template ({id}). You can still "
        "approve it if it is genuinely different.",
        "zh-TW": "這與現有範本（{id}）相似。如果確實不同，你仍可核准。",
    },
    "js.upload.suspect_ack": {
        "en": "The proposed metadata was flagged ({flags}). I have reviewed it and "
        "want to approve anyway.",
        "zh-TW": "建議的描述資料被標記（{flags}）。我已檢視並仍要核准。",
    },
    "js.upload.done.saved": {
        "en": 'Saved "{name}" to the library.',
        "zh-TW": "已將「{name}」儲存到範本庫。",
    },
    "js.upload.done.searchable": {
        "en": '"{name}" is now searchable and ready to render.',
        "zh-TW": "「{name}」現在可供搜尋並可用於產生迷因。",
    },
    "js.upload.session_expired": {
        "en": "Session expired. Your edits are still here - log in in a new tab, "
        "come back, and submit again. ",
        "zh-TW": "工作階段已過期。你的編輯仍在這裡 - 請在新分頁登入後返回，再重新送出。",
    },
    "js.upload.session_expired_login": {"en": "Log in", "zh-TW": "登入"},
}
