/* =========================================================
   PaperMind — frontend logic
   NOTE: API endpoints, payload shapes, and response parsing
   are unchanged from the original implementation on purpose —
   only presentation/UI has been reworked.
========================================================= */

const API_BASE = "";

/* ---------- element refs ---------- */
const chatBox        = document.getElementById("chatBox");
const emptyState      = document.getElementById("emptyState");
const questionInput  = document.getElementById("question");
const sendBtn        = document.getElementById("sendBtn");

const pdfUpload       = document.getElementById("pdfUpload");
const uploadZone       = document.getElementById("uploadZone");
const uploadFileName  = document.getElementById("uploadFileName");
const processBtn      = document.getElementById("processBtn");
const docStatus        = document.getElementById("docStatus");
const docStatusText    = document.getElementById("docStatusText");
const topbarDocName    = document.getElementById("topbarDocName");
const topbarLevel      = document.getElementById("topbarLevel");

const levelGroup       = document.getElementById("levelGroup");
const modeGroup         = document.getElementById("modeGroup");
const sidebar           = document.getElementById("sidebar");
const sidebarToggle    = document.getElementById("sidebarToggle");
const newChatBtn        = document.getElementById("newChatBtn");

let selectedFile = null;

/* =========================================================
   PILL SELECTORS (level / mode)
========================================================= */
function wirePillGroup(group, dataAttr, onChange) {
  group.querySelectorAll(".pill").forEach(btn => {
    btn.addEventListener("click", () => {
      group.querySelectorAll(".pill").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const value = btn.dataset[dataAttr];
      group.dataset.value = value;
      if (onChange) onChange(value, btn.textContent.trim());
    });
  });
}

wirePillGroup(levelGroup, "level", (value, label) => {
  topbarLevel.textContent = `${label} level`;
});
wirePillGroup(modeGroup, "mode");

function currentLevel() { return levelGroup.dataset.value || "college student"; }
function currentMode()  { return modeGroup.dataset.value || "normal"; }

/* =========================================================
   SIDEBAR TOGGLE (mobile / manual collapse)
========================================================= */
sidebarToggle.addEventListener("click", () => {
  sidebar.classList.toggle("collapsed");
});

newChatBtn.addEventListener("click", () => {
  chatBox.querySelectorAll(".message-row").forEach(el => el.remove());
  if (emptyState) emptyState.style.display = "block";
  questionInput.value = "";
  autoResize();
});

/* =========================================================
   FILE SELECTION (click + drag & drop)
========================================================= */
uploadZone.addEventListener("click", () => pdfUpload.click());

uploadZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  uploadZone.classList.add("dragover");
});
uploadZone.addEventListener("dragleave", () => uploadZone.classList.remove("dragover"));
uploadZone.addEventListener("drop", (e) => {
  e.preventDefault();
  uploadZone.classList.remove("dragover");
  if (e.dataTransfer.files.length) {
    setSelectedFile(e.dataTransfer.files[0]);
  }
});

pdfUpload.addEventListener("change", () => {
  if (pdfUpload.files.length) setSelectedFile(pdfUpload.files[0]);
});

function setSelectedFile(file) {
  if (file.type !== "application/pdf" && !file.name.toLowerCase().endsWith(".pdf")) {
    setDocStatus("error", "Please choose a PDF file.");
    return;
  }
  selectedFile = file;
  uploadFileName.textContent = file.name;
  processBtn.disabled = false;
  setDocStatus("idle", "Ready to process");
}

/* =========================================================
   DOC STATUS HELPERS
========================================================= */
function setDocStatus(state, text) {
  docStatus.classList.remove("ready", "loading", "error");
  if (state === "ready" || state === "loading" || state === "error") {
    docStatus.classList.add(state);
  }
  docStatusText.textContent = text;
}

/* =========================================================
   PDF UPLOAD  (POST /upload — unchanged contract)
========================================================= */
processBtn.addEventListener("click", uploadPDF);

async function uploadPDF() {
  if (!selectedFile) {
    setDocStatus("error", "Choose a PDF first.");
    return;
  }

  const formData = new FormData();
  formData.append("file", selectedFile);

  processBtn.disabled = true;
  setDocStatus("loading", "Processing document…");
  addNotice("Uploading and processing PDF…");

  try {
    const response = await fetch(`${API_BASE}/upload`, {
      method: "POST",
      body: formData
    });

    if (!response.ok) throw new Error("Upload failed");

    setDocStatus("ready", "Document ready");
    topbarDocName.textContent = selectedFile.name;
    addNotice("PDF processed successfully. You can now ask questions about it.");

    // Refresh the Previous Papers list so the newly processed
    // document shows up right away (backend already persisted it
    // to SQL Server as part of /upload).
    loadPreviousPapers();

  } catch (error) {
    console.error("Upload error:", error);
    setDocStatus("error", "Upload failed");
    addNotice("Error uploading PDF. Check that the backend server is running.", true);
  } finally {
    processBtn.disabled = false;
  }
}

/* =========================================================
   SUGGESTION CHIPS
========================================================= */
document.querySelectorAll(".suggestion-chip").forEach(chip => {
  chip.addEventListener("click", () => {
    questionInput.value = chip.dataset.suggest;
    autoResize();
    sendMessage();
  });
});

/* =========================================================
   SEND MESSAGE  (POST /ask — unchanged contract)
========================================================= */
sendBtn.addEventListener("click", sendMessage);
questionInput.addEventListener("keydown", function (e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});
questionInput.addEventListener("input", autoResize);

function autoResize() {
  questionInput.style.height = "auto";
  questionInput.style.height = Math.min(questionInput.scrollHeight, 160) + "px";
}

async function sendMessage() {
  const question = questionInput.value.trim();
  if (!question) return;

  const level = currentLevel();
  const mode = currentMode();

  hideEmptyState();
  addUserMessage(question);
  questionInput.value = "";
  autoResize();

  setLoading(true);
  const typingRow = addTypingIndicator();

  try {
    const response = await fetch(`${API_BASE}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, level, mode })
    });

    if (!response.ok) throw new Error("Server error");

    const data = await response.json();
    typingRow.remove();

    const answer = data.answer || {};
    renderAnswer(answer);

  } catch (error) {
    typingRow.remove();
    addNotice("Error getting a response. Make sure the backend server is running.", true);
    console.error(error);
  }

  setLoading(false);
}

/* =========================================================
   RENDERING — user messages
========================================================= */
function hideEmptyState() {
  if (emptyState) emptyState.style.display = "none";
}

function addUserMessage(text) {
  const row = document.createElement("div");
  row.className = "message-row user";
  row.innerHTML = `<div class="bubble-user"></div>`;
  row.querySelector(".bubble-user").textContent = text;
  chatBox.appendChild(row);
  scrollToBottom();
}

/* =========================================================
   RENDERING — assistant structured answer ("answer sheet")
========================================================= */
function renderAnswer(answer) {
  const row = document.createElement("div");
  row.className = "message-row";

  const tabs = [];

  if (answer.main_idea) {
    tabs.push(`
      <div class="answer-tab idea">
        <div class="answer-tab-label">Main idea</div>
        <div class="answer-tab-body"><p>${escapeHTML(answer.main_idea)}</p></div>
      </div>
    `);
  }

  if (answer.key_concepts && Array.isArray(answer.key_concepts) && answer.key_concepts.length) {
    const items = answer.key_concepts.map(c => {
      if (typeof c === "string") {
        return `<li>${escapeHTML(c)}</li>`;
      }
      const term = c.concept || c.term || "—";
      const explanation = c.explanation || "Not available";
      return `<li><span class="concept-term">${escapeHTML(term)}</span> — ${escapeHTML(explanation)}</li>`;
    }).join("");
    tabs.push(`
      <div class="answer-tab concepts">
        <div class="answer-tab-label">Key concepts</div>
        <div class="answer-tab-body"><ul class="concept-list">${items}</ul></div>
      </div>
    `);
  }

  if (answer.equations) {
    tabs.push(`
        <div class="answer-tab equations">
            <div class="answer-tab-label">Equations</div>
            <div class="answer-tab-body">
                ${renderEquations(answer.equations)}
            </div>
        </div>
    `);
}

  if (answer.real_world_example) {
    tabs.push(`
      <div class="answer-tab example">
        <div class="answer-tab-label">Real-world example</div>
        <div class="answer-tab-body"><p>${escapeHTML(answer.real_world_example)}</p></div>
      </div>
    `);
  }

  if (answer.simple_summary) {
    tabs.push(`
      <div class="answer-tab summary">
        <div class="answer-tab-label">Summary</div>
        <div class="answer-tab-body"><p>${escapeHTML(answer.simple_summary)}</p></div>
      </div>
    `);
  }

  let bodyHTML;
  if (tabs.length) {
    bodyHTML = `<div class="answer-sheet">${tabs.join("")}</div>`;
  } else {
    const raw = answer.raw_response ? answer.raw_response : JSON.stringify(answer, null, 2);
    bodyHTML = `<pre class="raw-block">${escapeHTML(raw)}</pre>`;
  }

  row.innerHTML = `
    <div class="assistant-block">
      <div class="assistant-avatar">P</div>
      <div class="assistant-content">${bodyHTML}</div>
    </div>
  `;

  chatBox.appendChild(row);
  renderMath();
  scrollToBottom();
}

/* =========================================================
   NOTICES / TYPING INDICATOR
========================================================= */
function addNotice(text, isError = false) {
  const row = document.createElement("div");
  row.className = "message-row";
  row.innerHTML = `<div class="notice${isError ? " error" : ""}"></div>`;
  row.querySelector(".notice").textContent = text;
  chatBox.appendChild(row);
  scrollToBottom();
  return row;
}

function addTypingIndicator() {
  const row = document.createElement("div");
  row.className = "message-row";
  row.innerHTML = `
    <div class="assistant-block">
      <div class="assistant-avatar">P</div>
      <div class="assistant-content">
        <div class="typing-dots"><span></span><span></span><span></span></div>
      </div>
    </div>
  `;
  chatBox.appendChild(row);
  scrollToBottom();
  return row;
}

/* =========================================================
   UTILITIES
========================================================= */
function setLoading(isLoading) {
  sendBtn.disabled = isLoading;
}

function scrollToBottom() {
  chatBox.scrollTop = chatBox.scrollHeight;
}

function renderEquations(equations) {
    if (!Array.isArray(equations) || equations.length === 0) {
        return `<p>Not available in the document.</p>`;
    }

    return equations.map((eq, index) => {

        const name = eq.name || `Equation ${index + 1}`;
        const formula = eq.formula || "";
        const explanation = eq.explanation || "";
        const page = eq.page ? `Page ${eq.page}` : "";

        return `
            <div class="equation-card">

                <div class="equation-header">
                    <span class="equation-number">
                        Equation ${index + 1}
                    </span>

                    <span class="equation-page">
                        ${escapeHTML(page)}
                    </span>
                </div>

                <h3 class="equation-name">
                    ${escapeHTML(name)}
                </h3>

                <div class="equation-formula">
                    <span class="math-expression">
                        ${escapeHTML(formula)}
                    </span>
                </div>

                <div class="equation-explanation">
                    ${renderEquationExplanation(explanation)}
                </div>

            </div>
        `;
    }).join("");
}

function renderEquationExplanation(explanation) {

    if (!explanation) {
        return "";
    }

    if (Array.isArray(explanation)) {
        return `
            <ol class="equation-steps">
                ${explanation.map(step => `
                    <li>${escapeHTML(step)}</li>
                `).join("")}
            </ol>
        `;
    }

    return `<p>${escapeHTML(explanation)}</p>`;
}

/* =========================================================
   MATH RENDERING
========================================================= */

function renderMath() {

    if (typeof katex === "undefined") {
        console.warn("KaTeX has not loaded yet.");
        return;
    }

    document.querySelectorAll(".math-expression").forEach(element => {

        const formula = element.textContent.trim();

        if (!formula) return;

        try {

            katex.render(formula, element, {
                displayMode: true,
                throwOnError: false
            });

        } catch (error) {

            console.error("Equation rendering error:", error);

        }

    });
}


/* =========================================================
   HTML ESCAPING
   (single shared helper — used by both the answer renderer
   and the previous-papers list, so there is only one
   escaping implementation in the whole file)
========================================================= */

function escapeHTML(str) {
    const div = document.createElement("div");
    div.textContent = String(str);
    return div.innerHTML;
}

/* ========================================================
    PREVIOUS PAPERS
   ========================================================= */

async function loadPreviousPapers() {
    const papersList = document.getElementById("papersList");

    if (!papersList) {
        return;
    }

    papersList.innerHTML = '<p class="papers-loading">Loading papers...</p>';

    try {
        const response = await fetch(`${API_BASE}/papers`);

        if (!response.ok) {
            throw new Error("Failed to load papers");
        }

        const data = await response.json();

        papersList.innerHTML = "";

        if (!data.papers || data.papers.length === 0) {
            papersList.innerHTML =
                '<p class="papers-empty">No previous papers.</p>';
            return;
        }

        data.papers.forEach(paper => {

            const paperElement = document.createElement("div");

            paperElement.className = "paper-item";

            paperElement.innerHTML = `
                <div class="paper-icon">📄</div>

                <div class="paper-info">
                    <div class="paper-title">
                        ${escapeHTML(paper.title)}
                    </div>

                    <div class="paper-date">
                        ${formatDate(paper.upload_date)}
                    </div>
                </div>
            `;

            paperElement.addEventListener(
                "click",
                () => loadExistingPaper(paper.paper_id, paper.title)
            );

            papersList.appendChild(paperElement);
        });

    } catch (error) {

        console.error("Error loading papers:", error);

        papersList.innerHTML =
            '<p class="papers-error">Unable to load previous papers.</p>';
    }
}

function formatDate(dateString) {
    const date = new Date(dateString);

    if (isNaN(date.getTime())) {
        return "";
    }

    return date.toLocaleDateString();
}

async function loadExistingPaper(paperId, title) {

    try {

        // Show loading status
        setDocStatus("loading", "Loading paper…");

        const response = await fetch(
            `${API_BASE}/load/${paperId}`,
            {
                method: "POST"
            }
        );

        const data = await response.json();

        if (!response.ok) {
            throw new Error(
                data.detail || "Failed to load paper"
            );
        }

        console.log("Paper loaded:", data);

        // Update document name in top bar
        topbarDocName.textContent = title;

        // Update sidebar status
        setDocStatus(
            "ready",
            `Paper ready — ${data.chunks_loaded} chunks loaded`
        );

        // Clear previous conversation so the new paper starts fresh,
        // but the user can ask questions immediately — no re-upload
        // or re-processing is triggered.
        chatBox.querySelectorAll(".message-row").forEach(
            el => el.remove()
        );

        if (emptyState) {
            emptyState.style.display = "block";
        }

        questionInput.value = "";

    } catch (error) {

        console.error("Error loading paper:", error);

        setDocStatus(
            "error",
            "Could not load paper"
        );
    }
}

document.addEventListener(
    "DOMContentLoaded",
    () => {
        loadPreviousPapers();
    }
);