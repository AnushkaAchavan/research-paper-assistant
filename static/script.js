/* PaperMind frontend: explicit multi-paper selection and safe API handling. */
const API_BASE = "";
const AUTH_TOKEN_KEY = "papermind_token";
const AUTH_USER_KEY = "papermind_user";

let authToken = localStorage.getItem(AUTH_TOKEN_KEY);
let currentUser = null;
try {
  currentUser = JSON.parse(localStorage.getItem(AUTH_USER_KEY) || "null");
} catch {
  localStorage.removeItem(AUTH_USER_KEY);
}

const chatBox = document.getElementById("chatBox");
const emptyState = document.getElementById("emptyState");
const questionInput = document.getElementById("question");
const sendBtn = document.getElementById("sendBtn");
const pdfUpload = document.getElementById("pdfUpload");
const uploadZone = document.getElementById("uploadZone");
const uploadFileName = document.getElementById("uploadFileName");
const processBtn = document.getElementById("processBtn");
const docStatus = document.getElementById("docStatus");
const docStatusText = document.getElementById("docStatusText");
const topbarDocName = document.getElementById("topbarDocName");
const topbarLevel = document.getElementById("topbarLevel");
const levelGroup = document.getElementById("levelGroup");
const modeGroup = document.getElementById("modeGroup");
const sidebar = document.getElementById("sidebar");

let selectedFiles = [];

// Make sure the question box exists
if (!questionInput) {
    console.error("PaperMind: #question textarea was not found.");
} else {
    questionInput.disabled = false;
    questionInput.readOnly = false;
}

function escapeHTML(value) { const node = document.createElement("div"); node.textContent = String(value ?? ""); return node.innerHTML; }
function setDocStatus(state, message) { docStatus.className = `doc-status ${state || ""}`; docStatusText.textContent = message; }
function currentLevel() { return levelGroup.dataset.value || "college student"; }
function currentMode() { return modeGroup.dataset.value || "normal"; }
function selectedPaperElements() { return [...document.querySelectorAll(".paper-item.selected")]; }
function getSelectedPaperIds() { return selectedPaperElements().map(node => Number(node.dataset.paperId)).filter(Number.isInteger); }
function scrollToBottom() { chatBox.scrollTop = chatBox.scrollHeight; }
function hideEmptyState() { if (emptyState) emptyState.style.display = "none"; }
function autoResize() {
    if (!questionInput) return;

    questionInput.style.height = "auto";
    questionInput.style.height =
        `${Math.min(questionInput.scrollHeight, 160)}px`;
}
function wirePillGroup(group, attribute, callback) {
  group.querySelectorAll(".pill").forEach(button => button.addEventListener("click", () => {
    group.querySelectorAll(".pill").forEach(item => item.classList.remove("active"));
    button.classList.add("active"); group.dataset.value = button.dataset[attribute]; callback?.(button);
  }));
}
wirePillGroup(levelGroup, "level", button => { topbarLevel.textContent = `${button.textContent.trim()} level`; });
wirePillGroup(modeGroup, "mode");
document.getElementById("sidebarToggle").addEventListener("click", () => sidebar.classList.toggle("collapsed"));
document.getElementById("newChatBtn").addEventListener("click", () => { chatBox.querySelectorAll(".message-row").forEach(node => node.remove()); emptyState.style.display = "block"; questionInput.value = ""; autoResize(); });

function setSelectedFiles(files) {
  const pdfs = files.filter(file => file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf"));
  selectedFiles = pdfs.slice(0, 3);
  processBtn.disabled = !selectedFiles.length;
  uploadFileName.textContent = selectedFiles.length ? selectedFiles.map(file => file.name).join(", ") : "No files selected";
  if (!pdfs.length) return setDocStatus("error", "Please choose at least one PDF.");
  setDocStatus(pdfs.length > 3 ? "error" : "", pdfs.length > 3 ? "Maximum 3 papers allowed. Using the first 3." : `${selectedFiles.length} paper${selectedFiles.length > 1 ? "s" : ""} ready to process`);
}
uploadZone.addEventListener("click", () => pdfUpload.click());
uploadZone.addEventListener("dragover", event => { event.preventDefault(); uploadZone.classList.add("dragover"); });
uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
uploadZone.addEventListener("drop", event => { event.preventDefault(); uploadZone.classList.remove("dragover"); setSelectedFiles([...event.dataTransfer.files]); });
pdfUpload.addEventListener("change", () => setSelectedFiles([...pdfUpload.files]));

function responseMessage(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  if (Array.isArray(detail?.errors)) return detail.errors.map(item => `${item.filename}: ${item.error}`).join(" ");
  return fallback;
}
async function authFetch(url, options = {}) {
    const headers = new Headers(options.headers || {});

    if (authToken) {
        headers.set("Authorization", `Bearer ${authToken}`);
    }

    const response = await fetch(url, {
        ...options,
        headers
    });

    if (response.status === 401) {
        clearAuth();
        if (window.location.pathname !== "/login") {
            window.location.replace("/login");
        }
    }
    return response;
}

function saveAuth(token, user) {
    authToken = token;
    currentUser = user;

    localStorage.setItem(AUTH_TOKEN_KEY, token);
    localStorage.setItem(AUTH_USER_KEY, JSON.stringify(user));

    updateAuthUI();
}

function clearAuth() {
    authToken = null;
    currentUser = null;

    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(AUTH_USER_KEY);

    updateAuthUI();
}

function updateAuthUI() {
    const authUser = document.getElementById("authUser");
    const logoutBtn = document.getElementById("logoutBtn");

    if (!authUser || !logoutBtn) return;

    if (authToken && currentUser) {
        authUser.textContent = currentUser.email || currentUser.username || "User";
        logoutBtn.hidden = false;
    } else {
        authUser.textContent = "";
        logoutBtn.hidden = true;
    }
}
async function uploadPDF() {
  if (!selectedFiles.length) return setDocStatus("error", "Choose at least one PDF first.");
  const formData = new FormData(); selectedFiles.forEach(file => formData.append("files", file, file.name));
  processBtn.disabled = true; setDocStatus("loading", `Processing ${selectedFiles.length} paper${selectedFiles.length > 1 ? "s" : ""}...`);
  try {
    const response = await authFetch(`${API_BASE}/upload`, {
    method: "POST",
    body: formData
   });
    // A response body is deliberately consumed once only.
    const text = await response.text(); let payload;
    try { payload = text ? JSON.parse(text) : {}; } catch { payload = { detail: text }; }
    if (!response.ok) throw new Error(responseMessage(payload, `Upload failed with HTTP ${response.status}.`));
    const papers = payload.papers || [];
    if (!papers.length) throw new Error("The server did not return uploaded paper details.");
    await loadPreviousPapers(papers.map(paper => paper.paper_id));
    updateSelectedPaperStatus();
    addNotice(`${papers.length} paper${papers.length > 1 ? "s" : ""} processed and selected. You can now ask questions.`);
    (payload.errors || []).forEach(item => addNotice(`${item.filename}: ${item.error}`, true));
    selectedFiles = []; pdfUpload.value = ""; uploadFileName.textContent = "No files selected";
  } catch (error) {
    console.error("Upload failed", error); setDocStatus("error", "Upload failed"); addNotice(error.message || "Error uploading papers.", true);
  } finally { processBtn.disabled = false; }
}
processBtn.addEventListener("click", uploadPDF);

function updateSelectedPaperStatus() {
  const selected = selectedPaperElements();
  if (!selected.length) { setDocStatus("", "No papers selected"); topbarDocName.textContent = "Untitled session"; return; }
  if (selected.length === 1) { const title = selected[0].querySelector(".paper-title").textContent.trim(); setDocStatus("ready", "1 paper selected"); topbarDocName.textContent = title; return; }
  setDocStatus("ready", `${selected.length} papers selected for comparison`); topbarDocName.textContent = `${selected.length} papers`;
}
async function loadExistingPaper(paperId) {
  try {
    const response = await authFetch(`${API_BASE}/load/${paperId}`, {
    method: "POST"
   });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(responseMessage(payload, "Could not load paper."));
    return payload;
  } catch (error) { addNotice(error.message, true); return null; }
}
function togglePaperSelection(element) {
  if (element.classList.contains("selected")) {
    element.classList.remove("selected"); element.setAttribute("aria-selected", "false"); updateSelectedPaperStatus(); return;
  }
  if (selectedPaperElements().length >= 3) return addNotice("You can select a maximum of 3 papers.", true);
  element.classList.add("selected"); element.setAttribute("aria-selected", "true"); updateSelectedPaperStatus();
  loadExistingPaper(Number(element.dataset.paperId));
}
async function loadPreviousPapers(autoSelectIds = []) {
  const papersList = document.getElementById("papersList");
  papersList.innerHTML = '<p class="papers-loading">Loading papers...</p>';

  try {
    const response = await authFetch(`${API_BASE}/papers`);
    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(
        responseMessage(payload, "Unable to load previous papers.")
      );
    }

    papersList.innerHTML = "";

    if (!payload.papers?.length) {
      papersList.innerHTML = '<p class="papers-empty">No previous papers.</p>';
      return;
    }

    payload.papers.forEach(paper => {
      const selected = autoSelectIds.includes(paper.paper_id);

      const element = document.createElement("div");
      element.className = `paper-item${selected ? " selected" : ""}`;

      element.dataset.paperId = paper.paper_id;
      element.setAttribute("aria-selected", String(selected));

      element.innerHTML = `
        <div class="paper-icon">PDF</div>

        <div class="paper-info">
          <div class="paper-title">
            ${escapeHTML(paper.title)}
          </div>

          <div class="paper-date">
            ${escapeHTML(formatDate(paper.upload_date))}
            · ${paper.chunk_count} chunks
          </div>
        </div>

        <button
          class="delete-paper-btn"
          type="button"
          title="Delete paper"
          aria-label="Delete ${escapeHTML(paper.title)}"
        >
          🗑
        </button>
      `;

      // Clicking the paper selects/deselects it
      element.addEventListener("click", () => {
        togglePaperSelection(element);
      });

      // Clicking delete should NOT select the paper
      const deleteButton = element.querySelector(".delete-paper-btn");

      deleteButton.addEventListener("click", async (event) => {
        event.stopPropagation();

        const confirmed = confirm(
          `Delete "${paper.title}"?\n\nThis will permanently remove the paper and its stored chunks.`
        );

        if (!confirmed) return;

        await deletePaper(paper.paper_id, paper.title);
      });

      papersList.appendChild(element);
    });

  } catch (error) {
    papersList.innerHTML =
      '<p class="papers-error">Unable to load previous papers.</p>';

    addNotice(error.message, true);
  }
}

async function deletePaper(paperId, paperTitle) {
  try {
    const response = await authFetch(
       `${API_BASE}/papers/${paperId}`,
      {
          method: "DELETE"
      }
    );

    const payload = await response.json().catch(() => ({}));

    if (!response.ok) {
      throw new Error(
        responseMessage(payload, `Unable to delete "${paperTitle}".`)
      );
    }

    // Remove the paper from the UI
    const paperElement = document.querySelector(
      `.paper-item[data-paper-id="${paperId}"]`
    );

    if (paperElement) {
      paperElement.remove();
    }

    // Update selected-paper state
    if (typeof updateSelectedPaperStatus === "function") {
      updateSelectedPaperStatus();
    }

    // If no papers remain, show the empty state
    const papersList = document.getElementById("papersList");

    if (!papersList.querySelector(".paper-item")) {
      papersList.innerHTML =
        '<p class="papers-empty">No previous papers.</p>';
    }

    addNotice(`"${paperTitle}" deleted successfully.`);

  } catch (error) {
    addNotice(error.message, true);
  }
}
function formatDate(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "" : date.toLocaleDateString(); }

function addNotice(text, isError = false) { const row = document.createElement("div"); row.className = "message-row"; row.innerHTML = `<div class="notice${isError ? " error" : ""}"></div>`; row.firstChild.textContent = text; chatBox.appendChild(row); scrollToBottom(); }
function addUserMessage(text) { const row = document.createElement("div"); row.className = "message-row user"; row.innerHTML = '<div class="bubble-user"></div>'; row.firstChild.textContent = text; chatBox.appendChild(row); scrollToBottom(); }
function addTypingIndicator() { const row = document.createElement("div"); row.className = "message-row"; row.innerHTML = '<div class="assistant-block"><div class="assistant-avatar">P</div><div class="assistant-content"><div class="typing-dots"><span></span><span></span><span></span></div></div></div>'; chatBox.appendChild(row); scrollToBottom(); return row; }
function renderEquations(equations) {
  if (!Array.isArray(equations) || !equations.length) return "<p>Not available in the document.</p>";
  return equations.map((equation, index) => `<div class="equation-card"><div class="equation-header"><span class="equation-number">Equation ${index + 1}</span><span class="equation-page">${escapeHTML(equation.page ? `Page ${equation.page}` : "")}</span></div><h3 class="equation-name">${escapeHTML(equation.name || `Equation ${index + 1}`)}</h3><div class="equation-formula"><span class="math-expression">${escapeHTML(equation.formula || "")}</span></div><div class="equation-explanation">${Array.isArray(equation.explanation) ? `<ol class="equation-steps">${equation.explanation.map(step => `<li>${escapeHTML(step)}</li>`).join("")}</ol>` : `<p>${escapeHTML(equation.explanation || "")}</p>`}</div></div>`).join("");
}
function renderBlock(block) {
  const body = value => `<div class="answer-tab-body">${value}</div>`;
  if (block.type === "heading") return `<div class="answer-tab dynamic-heading">${body(`<h3>${escapeHTML(block.text)}</h3>`)}</div>`;
  if (block.type === "paragraph") return `<div class="answer-tab dynamic-paragraph">${body(`<p>${escapeHTML(block.text)}</p>`)}</div>`;
  if (block.type === "bullets" || block.type === "numbered_list") {
    const tag = block.type === "bullets" ? "ul" : "ol";
    const items = Array.isArray(block.items) ? block.items.map(item => `<li>${escapeHTML(item)}</li>`).join("") : "";
    return items ? `<div class="answer-tab dynamic-list">${body(`<${tag} class="dynamic-list-items">${items}</${tag}>`)}</div>` : "";
  }
  if (block.type === "table") {
    const headers = Array.isArray(block.headers) ? block.headers : [];
    const rows = Array.isArray(block.rows) ? block.rows : [];
    if (!headers.length || !rows.length) return "";
    return `<div class="answer-tab dynamic-table">${body(`<div class="table-scroll"><table><thead><tr>${headers.map(header => `<th>${escapeHTML(header)}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${(Array.isArray(row) ? row : []).map(cell => `<td>${escapeHTML(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`)}</div>`;
  }
  if (block.type === "equation") return `<div class="answer-tab equations">${body(renderEquations([block]))}</div>`;
  return "";
}
function renderEvidence(sources) {
  if (!Array.isArray(sources) || !sources.length) return "";
  const uniqueSources = [...new Map(sources.map(source => {
    const meta = source.metadata || {};
    return [`${meta.paper_id}-${meta.page}-${meta.section}`, meta];
  })).values()];
  return `<details class="source-evidence"><summary>Sources &amp; evidence <span>${uniqueSources.length}</span></summary><ul>${uniqueSources.map(meta => `<li><strong>Paper ${escapeHTML(meta.paper_id)}</strong><span>${escapeHTML(meta.filename || "Unknown paper")} · p. ${escapeHTML(meta.page)} · ${escapeHTML(meta.section || "GENERAL")}</span></li>`).join("")}</ul></details>`;
}
function renderAnswer(answer, sources = []) {
  const blocks = Array.isArray(answer.blocks) ? answer.blocks.map(renderBlock).filter(Boolean) : [];
  const answerBody = blocks.length ? `<div class="answer-sheet dynamic-answer">${blocks.join("")}</div>` : `<pre class="raw-block">${escapeHTML(answer.raw_response || "The response could not be displayed.")}</pre>`;
  const content = `${answerBody}${renderEvidence(sources)}`;
  const row = document.createElement("div"); row.className = "message-row"; row.innerHTML = `<div class="assistant-block"><div class="assistant-avatar">P</div><div class="assistant-content">${content}</div></div>`; chatBox.appendChild(row); renderMath(); scrollToBottom();
}
function renderMath() { if (typeof katex === "undefined") return; document.querySelectorAll(".math-expression").forEach(node => { try { katex.render(node.textContent.trim(), node, { displayMode: true, throwOnError: false }); } catch (error) { console.warn("Equation rendering failed", error); } }); }
async function sendMessage() {
  const question = questionInput.value.trim(), paperIds = getSelectedPaperIds();
  if (!question) return;
  if (!paperIds.length) return addNotice("Select at least one paper before asking a question.", true);
  hideEmptyState(); addUserMessage(question); questionInput.value = ""; autoResize(); sendBtn.disabled = true; const typing = addTypingIndicator();
  try {
    const response = await authFetch(`${API_BASE}/ask`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question, paper_ids: paperIds, level: currentLevel(), mode: currentMode() }) });
    const payload = await response.json().catch(() => ({})); if (!response.ok) throw new Error(responseMessage(payload, "Unable to answer this question."));
    typing.remove(); renderAnswer(payload.answer || { raw_response: "The server returned no answer." }, payload.sources || []);
  } catch (error) { typing.remove(); addNotice(error.message || "Error getting a response.", true); } finally { sendBtn.disabled = false; }
}
sendBtn.addEventListener("click", sendMessage);
questionInput.addEventListener("keydown", event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); sendMessage(); } });
questionInput.addEventListener("input", autoResize);
document.querySelectorAll(".suggestion-chip").forEach(chip => chip.addEventListener("click", () => { questionInput.value = chip.dataset.suggest; autoResize(); sendMessage(); }));
document.addEventListener("DOMContentLoaded", async () => {
    updateAuthUI();

    if (!authToken) {
        window.location.href = "/login";
        return;
    }

    const authenticated = await loadCurrentUser();

    if (!authenticated) {
        window.location.href = "/login";
        return;
    }

    await loadPreviousPapers();
});
async function loadCurrentUser() {
    if (!authToken) {
        return false;
    }

    try {
        const response = await authFetch(`${API_BASE}/auth/me`);

        if (!response.ok) {
            clearAuth();
            return false;
        }

        const payload = await response.json();
        if (!payload.user) {
            clearAuth();
            return false;
        }
        currentUser = payload.user;

        localStorage.setItem(
            AUTH_USER_KEY,
            JSON.stringify(currentUser)
        );

        updateAuthUI();
        return true;

    } catch (error) {
        console.error("Could not load current user:", error);
        clearAuth();
        return false;
    }
}

function logout() {
    clearAuth();
    window.location.replace("/login");
}
document.getElementById("logoutBtn")?.addEventListener("click", logout);
