/**
 * Cognee RCA Website Application Logic
 * Implements interactive single-page app tabs, releases explorer with colored diffs,
 * and a canvas-based simulated force-directed graph with traversal animations.
 */

// --- Static Data Definitions -------------------------------------------------

const INCIDENTS = {
    "INC-1001": {
        id: "INC-1001",
        lang: "python",
        description: "Checkout requests with a $0 promotional order started failing with 'invalid amount' right after the R1 release.",
        traceback: `Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 32, in process_payment
    raise ValueError("invalid amount")
ValueError: invalid amount`,
        seeds: ["app.api.handle_checkout", "app.payments.process_payment"],
        // Ground truth from tests/fixtures (graph highlighting only) — scores/ranks
        // always come live from /api/analyze, never from here.
        findings: [
            { name: "app.payments.process_payment", file: "app/payments.py", isTrue: true }
        ]
    },
    "INC-1002": {
        id: "INC-1002",
        lang: "python",
        description: "Receipt display amounts started rendering as 'N/A' for large orders shortly after R1; exception surfaces inside process_payment's call into the currency formatter.",
        traceback: `Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 34, in process_payment
    receipt["display_amount"] = format_currency(order["amount"])
ValueError: Unknown format code 'f' for object of type 'str'`,
        seeds: ["app.api.handle_checkout", "app.payments.process_payment"],
        findings: [
            { name: "app.utils.format_currency", file: "app/utils.py", isTrue: true }
        ]
    },
    "INC-1004": {
        id: "INC-1004",
        lang: "python",
        description: "Card validation started rejecting some previously-valid short test card numbers immediately after R1 shipped, well before R2 existed.",
        traceback: `Traceback (most recent call last):
  File "app/api.py", line 9, in handle_checkout
    return process_payment(order)
  File "app/payments.py", line 30, in process_payment
    raise ValueError("invalid card")
ValueError: invalid card`,
        seeds: ["app.api.handle_checkout", "app.payments.process_payment"],
        findings: [
            { name: "app.payments.validate_card", file: "app/payments.py", isTrue: true }
        ]
    },
    "INC-1003": {
        id: "INC-1003",
        lang: "javascript",
        description: "Order submission on the web client intermittently shows a blank price; suspected client-side formatting bug unrelated to backend releases.",
        traceback: `Error: Cannot read properties of undefined (reading 'toFixed')
    at formatPrice (web/helpers.js:4:24)
    at submitOrder (web/client.js:7:20)`,
        seeds: ["web.helpers.formatPrice", "web.client.submitOrder"],
        findings: []  // negative control: nothing in web/* changes in any release
    }
};

const RELEASES = {
    "v1.2.0": {
        tag: "v1.2.0",
        sha: "8f7e6d5c",
        date: "2026-03-01 09:00:00 UTC",
        msg: "R2: apply flat processing fee on charge",
        changes: [
            { symbol: "app.payments.charge_card", file: "app/payments.py", type: "modified" }
        ],
        diffs: {
            "app/payments.py": `@@ -13,4 +13,8 @@ def validate_card(card):
 def charge_card(card, amount):
-    """Charge a validated card for \`amount\` cents."""
-    return {"card": card["number"][-4:], "amount": amount, "status": "charged"}
+    """Charge a validated card for \`amount\` cents.
+
+    R2: applies a flat processing fee before recording the charge.
+    """
+    fee = 30
+    return {"card": card["number"][-4:], "amount": amount + fee, "status": "charged"}`
        }
    },
    "v1.1.0": {
        tag: "v1.1.0",
        sha: "e4f5a6b7",
        date: "2026-02-01 09:00:00 UTC",
        msg: "R1: tighten card validation, add refunds, drop unused retry helper",
        changes: [
            { symbol: "app.payments.process_payment", file: "app/payments.py", type: "modified" },
            { symbol: "app.payments.validate_card", file: "app/payments.py", type: "modified" },
            { symbol: "app.utils.format_currency", file: "app/utils.py", type: "modified" },
            { symbol: "app.payments.refund_payment", file: "app/payments.py", type: "added" },
            { symbol: "app.utils.retry", file: "app/utils.py", type: "deleted" }
        ],
        diffs: {
            "app/payments.py": `@@ -7,4 +7,5 @@ from app.utils import format_currency
 def validate_card(card):
-    """Validate a card dict has the required fields."""
-    return bool(card.get("number")) and bool(card.get("cvv"))
+    """Validate a card dict has the required fields and a plausible length."""
+    number = card.get("number", "")
+    return bool(number) and bool(card.get("cvv")) and len(number) >= 12

@@ -13,14 +14,24 @@ def validate_card(card):
 def charge_card(card, amount):
     """Charge a validated card for \`amount\` cents."""
     return {"card": card["number"][-4:], "amount": amount, "status": "charged"}

+def refund_payment(receipt):
+    """Refund a previously charged payment receipt."""
+    return {"card": receipt["card"], "amount": -receipt["amount"], "status": "refunded"}
+
 def process_payment(order):
-    """Validate and charge the card attached to \`order\`, then persist it."""
+    """Validate and charge the card attached to \`order\`, then persist it.
+
+    R1: now raises on non-positive amounts before attempting to charge.
+    """
     card = order["card"]
     if not validate_card(card):
         raise ValueError("invalid card")
+    if order["amount"] <= 0:
+        raise ValueError("invalid amount")
     receipt = charge_card(card, order["amount"])
     receipt["display_amount"] = format_currency(order["amount"])
     save_record("payments", receipt)
     return receipt`,
            "app/utils.py": `@@ -4,14 +4,3 @@
 def format_currency(cents):
-    """Format integer cents as a dollar string."""
-    return "\${:.2f}".format(cents / 100)
-
-
-def retry(fn, times):
-    """Call fn up to \`times\` times, returning the first success."""
-    last_err = None
-    for _ in range(times):
-        try:
-            return fn()
-        except Exception as err:  # noqa: BLE001 - fixture code
-            last_err = err
-    raise last_err
+    """Format integer cents as a dollar string, now with thousands separators."""
+    return "\${:,.2f}".format(cents / 100)`
        }
    },
    "v1.0.0": {
        tag: "v1.0.0",
        sha: "a1b2c3d4",
        date: "2026-01-01 09:00:00 UTC",
        msg: "initial: fixture payment app (baseline build)",
        changes: [],
        diffs: {
            "app/api.py": `+ """HTTP-ish entry points (fixture app)."""
+ from app.payments import process_payment
+ 
+ def handle_checkout(request):
+     order = request["order"]
+     return process_payment(order)`
        }
    }
};

// Raw code graph nodes & edges representation
const CODE_GRAPH = {
    nodes: [
        // Files
        { id: "app/api.py", name: "app/api.py", type: "file", x: 150, y: 150 },
        { id: "app/payments.py", name: "app/payments.py", type: "file", x: 400, y: 250 },
        { id: "app/utils.py", name: "app/utils.py", type: "file", x: 650, y: 150 },
        { id: "app/db.py", name: "app/db.py", type: "file", x: 400, y: 450 },
        { id: "web/client.js", name: "web/client.js", type: "file", x: 150, y: 350 },
        { id: "web/helpers.js", name: "web/helpers.js", type: "file", x: 150, y: 500 },

        // Functions
        { id: "app.api.handle_checkout", name: "handle_checkout", type: "func", file: "app/api.py", x: 150, y: 80 },
        { id: "app.payments.validate_card", name: "validate_card", type: "func", file: "app/payments.py", x: 300, y: 180 },
        { id: "app.payments.charge_card", name: "charge_card", type: "func", file: "app/payments.py", x: 450, y: 180 },
        { id: "app.payments.refund_payment", name: "refund_payment", type: "func", file: "app/payments.py", x: 520, y: 280 },
        { id: "app.payments.process_payment", name: "process_payment", type: "func", file: "app/payments.py", x: 380, y: 320 },
        { id: "app.utils.format_currency", name: "format_currency", type: "func", file: "app/utils.py", x: 650, y: 80 },
        { id: "app.utils.retry", name: "retry", type: "func", file: "app/utils.py", x: 720, y: 220 },
        { id: "app.db.get_connection", name: "get_connection", type: "func", file: "app/db.py", x: 300, y: 520 },
        { id: "app.db.save_record", name: "save_record", type: "func", file: "app/db.py", x: 500, y: 520 },
        { id: "web.client.submitOrder", name: "submitOrder", type: "func", file: "web/client.js", x: 100, y: 280 },
        { id: "web.helpers.formatPrice", name: "formatPrice", type: "func", file: "web/helpers.js", x: 80, y: 440 },
        { id: "web.helpers.logEvent", name: "logEvent", type: "func", file: "web/helpers.js", x: 220, y: 440 }
    ],
    edges: [
        // Containment edges (File -> Function)
        { source: "app/api.py", target: "app.api.handle_checkout", type: "contain" },
        { source: "app/payments.py", target: "app.payments.validate_card", type: "contain" },
        { source: "app/payments.py", target: "app.payments.charge_card", type: "contain" },
        { source: "app/payments.py", target: "app.payments.refund_payment", type: "contain" },
        { source: "app/payments.py", target: "app.payments.process_payment", type: "contain" },
        { source: "app/utils.py", target: "app.utils.format_currency", type: "contain" },
        { source: "app/utils.py", target: "app.utils.retry", type: "contain" },
        { source: "app/db.py", target: "app.db.get_connection", type: "contain" },
        { source: "app/db.py", target: "app.db.save_record", type: "contain" },
        { source: "web/client.js", target: "web.client.submitOrder", type: "contain" },
        { source: "web/helpers.js", target: "web.helpers.formatPrice", type: "contain" },
        { source: "web/helpers.js", target: "web.helpers.logEvent", type: "contain" },

        // Calls / Imports / Dependencies (Call: Function -> Function, Import: File -> File)
        { source: "app.api.handle_checkout", target: "app.payments.process_payment", type: "call" },
        { source: "app.payments.process_payment", target: "app.payments.validate_card", type: "call" },
        { source: "app.payments.process_payment", target: "app.payments.charge_card", type: "call" },
        { source: "app.payments.process_payment", target: "app.utils.format_currency", type: "call" },
        { source: "app.payments.process_payment", target: "app.db.save_record", type: "call" },
        { source: "app.db.save_record", target: "app.db.get_connection", type: "call" },
        { source: "web.client.submitOrder", target: "web.helpers.formatPrice", type: "call" },
        { source: "web.client.submitOrder", target: "web.helpers.logEvent", type: "call" },

        // Imports (File level)
        { source: "app/api.py", target: "app/payments.py", type: "import" },
        { source: "app/payments.py", target: "app/db.py", type: "import" },
        { source: "app/payments.py", target: "app/utils.py", type: "import" },
        { source: "web/client.js", target: "web/helpers.js", type: "import" }
    ]
};

// --- App State ---------------------------------------------------------------

let currentTab = "playground";
let dbInitialized = false;
let selectedIncidentId = "INC-1001";
let selectedReleaseTag = "v1.1.0";
let selectedReleaseFile = "app/payments.py";
let analysisRunning = false;

// Physics engine properties
let nodes = [];
let edges = [];
let draggingNode = null;
let hoveredNode = null;
let panX = 0;
let panY = 0;
let zoomScale = 1.0;
let isPanning = false;
let startPanX = 0;
let startPanY = 0;

// Traversal Animation States
let animatedVisitedNodes = new Set();
let animatedHops = {}; // node.id -> hop depth
let animatedCandidates = new Set();
let animationStep = 0;
let animationProgress = 0; // 0.0 to 1.0 for transitions

// Canvas Elements
let canvas = null;
let ctx = null;

// --- Initialize -------------------------------------------------------------

window.addEventListener("DOMContentLoaded", () => {
    // Detect if running inside iframe
    const isEmbedded = window.self !== window.top;
    if (isEmbedded) {
        document.body.classList.add("embedded-mode");
    }

    canvas = document.getElementById("graph-canvas");
    ctx = canvas.getContext("2d");
    
    // Fit canvas to parent container
    resizeCanvas();
    window.addEventListener("resize", resizeCanvas);

    // Initialize graph layout nodes
    resetGraphLayout();

    // Set up canvas event listeners
    setupCanvasListeners();

    // Trigger tab display
    switchTab(currentTab);

    // Load live Cognee data on startup
    initializeDashboard();

    // Start force simulation loop
    requestAnimationFrame(updateSimulation);
});

async function initializeDashboard() {
    addLogLine("[backend] Checking Cognee Graph DB context...", "text-muted");
    try {
        let resStatus = await fetch("/api/status");
        let status = await resStatus.json();
        
        if (status.initialized) {
            dbInitialized = true;
            document.getElementById("ingest-repo-section").classList.add("hidden");
            document.getElementById("incident-selector-section").classList.remove("hidden");
            document.getElementById("btn-run-rca").removeAttribute("disabled");
            
            addLogLine(`[backend] Graph database active. Ingested ${status.stats.files} files, ${status.stats.functions} functions, ${status.stats.edges} edges.`, "text-green");
            
            let resInc = await fetch("/api/incidents");
            let incs = await resInc.json();
            incs.forEach(inc => {
                INCIDENTS[inc.incident_id] = {
                    id: inc.incident_id,
                    lang: inc.language,
                    description: inc.description,
                    traceback: inc.stack_trace,
                    seeds: inc.incident_id === "INC-1003" ? ["web.helpers.formatPrice", "web.client.submitOrder"] : ["app.api.handle_checkout", "app.payments.process_payment"],
                    findings: []
                    // verdict comes from the live /api/analyze response — never canned.
                };
            });

            let resRel = await fetch("/api/releases");
            let rels = await resRel.json();
            rels.forEach(rel => {
                RELEASES[rel.tag] = rel;
            });
            
            // Populate dynamic lists
            populateIncidentSelectors();
            populateReleasesList();
            resetGraphLayout();
            
            // Refresh selectors (this will auto-trigger runAnalysis)
            selectIncident(selectedIncidentId);
            showRelease(selectedReleaseTag);
            
            addLogLine("[system] Ready. Real-time API link established.", "text-green");
        } else {
            dbInitialized = false;
            document.getElementById("ingest-repo-section").classList.remove("hidden");
            document.getElementById("incident-selector-section").classList.add("hidden");
            document.getElementById("btn-run-rca").setAttribute("disabled", "true");
            
            addLogLine("[backend] Graph database active but not initialized.", "text-amber");
            addLogLine("[system] Ready. Please ingest the demo repository to construct the Code Knowledge Graph.", "text-amber");
            
            populateIncidentSelectors();
            populateReleasesList();
            resetGraphLayout();
        }
    } catch (err) {
        addLogLine("[backend] Offline — start the engine with:  python dashboard/server.py", "text-amber");
        addLogLine("[backend] You can browse incidents/releases, but findings require the live engine.", "text-amber");
        dbInitialized = true; // allow browsing static inputs (traces/diffs); runAnalysis aborts offline
        document.getElementById("ingest-repo-section").classList.add("hidden");
        document.getElementById("incident-selector-section").classList.remove("hidden");
        document.getElementById("btn-run-rca").removeAttribute("disabled");

        populateIncidentSelectors();
        populateReleasesList();
        resetGraphLayout();
        selectIncident(selectedIncidentId);
        showRelease(selectedReleaseTag);
    }
}

function populateIncidentSelectors() {
    const container = document.getElementById("incident-selector-container");
    if (!container) return;
    
    container.innerHTML = "";
    
    const keys = Object.keys(INCIDENTS);
    if (!dbInitialized || keys.length === 0) {
        container.innerHTML = `<div class="table-placeholder" style="padding: 20px; text-align: center;">No incidents loaded. Ingest the demo repository first.</div>`;
        return;
    }
    
    keys.forEach(incId => {
        const inc = INCIDENTS[incId];
        const option = document.createElement("div");
        option.className = `incident-option ${incId === selectedIncidentId ? "active" : ""}`;
        option.id = `inc-${incId}`;
        option.setAttribute("onclick", `selectIncident('${incId}')`);
        
        const langClass = inc.lang === "javascript" || inc.lang === "js" ? "badge-js" : "badge-python";
        const langName = inc.lang === "javascript" || inc.lang === "js" ? "JS" : "Python";
        
        let shortTitle = inc.description;
        if (incId === "INC-1001") shortTitle = "Checkout failures ($0 order)";
        if (incId === "INC-1002") shortTitle = "Receipt display shows N/A";
        if (incId === "INC-1004") shortTitle = "Chronological R1 validation error";
        if (incId === "INC-1003") shortTitle = "JS blank order price (Control)";
        
        option.innerHTML = `
            <div class="inc-meta">
                <span class="inc-id">${incId}</span>
                <span class="inc-lang ${langClass}">${langName}</span>
            </div>
            <div class="inc-title">${shortTitle}</div>
        `;
        container.appendChild(option);
    });
}

function populateReleasesList() {
    const container = document.getElementById("releases-list-container");
    if (!container) return;
    
    container.innerHTML = "";
    
    const releaseKeys = Object.keys(RELEASES).sort((a, b) => b.localeCompare(a));
    
    if (!dbInitialized || releaseKeys.length === 0) {
        container.innerHTML = `<div class="table-placeholder" style="padding: 20px; text-align: center;">No releases loaded. Ingest the demo repository first.</div>`;
        return;
    }
    
    releaseKeys.forEach((tag) => {
        const rel = RELEASES[tag];
        const shortDate = rel.date ? rel.date.split(" ")[0] : "";
        
        const item = document.createElement("div");
        item.className = `release-item ${tag === selectedReleaseTag ? "active" : ""}`;
        item.id = `rel-${tag}`;
        item.setAttribute("onclick", `showRelease('${tag}')`);
        item.innerHTML = `
            <div class="release-header-row">
                <span class="release-tag-badge">${tag}</span>
                <span class="release-time">${shortDate}</span>
            </div>
            <div class="release-msg">${rel.msg || ""}</div>
        `;
        container.appendChild(item);
    });
}

async function triggerIngestion() {
    const btn = document.getElementById("btn-ingest-repo");
    if (btn.disabled) return;
    
    btn.disabled = true;
    btn.innerHTML = `<span>⏳ Ingesting Repository...</span>`;
    
    const consoleEl = document.getElementById("terminal-console");
    consoleEl.innerHTML = "";
    
    addLogLine("[system] Triggering dynamic codebase ingestion pipeline...", "text-blue");
    
    // Simulate real-time console feedback steps matching backend activity
    setTimeout(() => {
        addLogLine("[backend] Initializing git payment-app repository environment...", "text-muted");
    }, 400);

    setTimeout(() => {
        addLogLine("[backend] Ingesting files: app/api.py, app/payments.py, app/utils.py, app/db.py...", "text-muted");
    }, 1000);

    setTimeout(() => {
        addLogLine("[backend] Running Tree-Sitter AST parsing on source files...", "text-blue");
        addLogLine("[cognee] Indexing nodes into sqlite database graph...", "text-purple");
    }, 2000);

    setTimeout(() => {
        addLogLine("[backend] Synchronizing release diffs for v1.1.0 and v1.2.0...", "text-blue");
    }, 3200);

    try {
        const response = await fetch("/api/init", { method: "POST" });
        if (response.ok) {
            const data = await response.json();
            
            // Wait slightly for final sync logs to render cleanly
            setTimeout(() => {
                addLogLine(`[backend] Cognee graph construction complete!`, "text-green");
                addLogLine(`[backend] Ingested stats: ${data.stats.files} files, ${data.stats.functions} functions, ${data.stats.edges} edges.`, "text-green");
                
                // Refresh dashboard to load selectors
                initializeDashboard();
            }, 4000);
        } else {
            addLogLine("[backend] Failed to construct code graph. Server returned error.", "text-red");
            btn.disabled = false;
            btn.innerHTML = `📂 Ingest Demo Repo`;
        }
    } catch (err) {
        addLogLine(`[error] Ingestion connection failed: ${err.message}`, "text-red");
        btn.disabled = false;
        btn.innerHTML = `📂 Ingest Demo Repo`;
    }
}

// Fit canvas to layout dynamically
function resizeCanvas() {
    const wrapper = document.getElementById("graph-wrapper");
    if (wrapper && canvas) {
        canvas.width = wrapper.clientWidth;
        canvas.height = wrapper.clientHeight;
    }
}

// Reset node coordinates and initialize layout
function resetGraphLayout() {
    if (!dbInitialized) {
        nodes = [];
        edges = [];
        return;
    }
    
    nodes = CODE_GRAPH.nodes.map(n => ({
        ...n,
        vx: 0,
        vy: 0,
        fx: null,
        fy: null
    }));
    
    edges = CODE_GRAPH.edges.map(e => ({
        ...e,
        sourceNode: nodes.find(n => n.id === e.source),
        targetNode: nodes.find(n => n.id === e.target)
    }));

    panX = canvas ? canvas.width / 2 - 400 : 0;
    panY = canvas ? canvas.height / 2 - 300 : 0;
    zoomScale = 0.9;
}

// --- Navigation Tabs --------------------------------------------------------

function switchTab(tabName) {
    currentTab = tabName;
    
    // Manage tab button highlights
    document.querySelectorAll(".nav-item").forEach(btn => {
        btn.classList.remove("active");
    });
    const activeBtn = document.getElementById(`btn-tab-${tabName}`);
    if (activeBtn) activeBtn.classList.add("active");

    // Manage panels visible
    document.querySelectorAll(".tab-panel").forEach(panel => {
        panel.classList.remove("active");
    });
    const activePanel = document.getElementById(`panel-playground`);
    
    // Hide all, show selected
    document.getElementById("panel-playground").style.display = tabName === "playground" ? "flex" : "none";
    document.getElementById("panel-releases").style.display = tabName === "releases" ? "flex" : "none";
    document.getElementById("panel-architecture").style.display = tabName === "architecture" ? "flex" : "none";
    document.getElementById("panel-docs").style.display = tabName === "docs" ? "flex" : "none";

    // Header update
    const titleEl = document.getElementById("page-title");
    const subEl = document.getElementById("page-subtitle");

    if (tabName === "playground") {
        titleEl.innerText = "RCA Diagnostic Console";
        subEl.innerText = "Simulate stack trace ingestion and walk the Blast Radius in real-time";
        // Recalculate canvas size on view change
        setTimeout(resizeCanvas, 50);
    } else if (tabName === "releases") {
        titleEl.innerText = "Git Releases & Code Diffs";
        subEl.innerText = "Inspect release commit records and line-by-line file diffs";
    } else if (tabName === "architecture") {
        titleEl.innerText = "Graph RCA Architecture";
        subEl.innerText = "How Tree-Sitter logical extraction and Cognee edge traversals work";
    } else if (tabName === "docs") {
        titleEl.innerText = "Quickstart Documentation";
        subEl.innerText = "Install cognee[codegraph] and query your codebase structure locally";
    }
}

// --- Incident Selectors -----------------------------------------------------

function selectIncident(incId) {
    if (analysisRunning) return;
    if (!dbInitialized) return;
    
    selectedIncidentId = incId;

    // Highlight active selector
    document.querySelectorAll(".incident-option").forEach(opt => {
        opt.classList.remove("active");
    });
    document.getElementById(`inc-${incId}`).classList.add("active");

    const inc = INCIDENTS[incId];
    
    // Update labels & desc
    document.getElementById("selected-inc-desc").innerText = inc.description;
    document.getElementById("inc-badge").innerText = inc.id;
    document.getElementById("code-traceback").innerText = inc.traceback;

    // Auto-adjust release window select matching test cases
    const winSelect = document.getElementById("param-window");
    if (incId === "INC-1004") {
        winSelect.value = "v1.1.0";
    } else {
        winSelect.value = "all";
    }

    // Reset simulator visual highlights
    clearAnalysisVisuals();
}

async function resetPlayground() {
    clearAnalysisVisuals();
    resetGraphLayout();
    document.getElementById("terminal-console").innerHTML = `<div class="terminal-line"><span class="text-blue">[system]</span> Re-initializing Cognee database...</div>`;
    
    try {
        const response = await fetch("/api/init", { method: "POST" });
        if (response.ok) {
            const data = await response.json();
            addLogLine(`[backend] Database re-initialized: ${data.stats.files} files, ${data.stats.functions} functions, ${data.stats.edges} edges.`, "text-green");
        } else {
            addLogLine("[backend] Failed to re-initialize database.", "text-red");
        }
    } catch (err) {
        addLogLine("[backend] Server not active. Offline reset completed.", "text-amber");
    }
}

async function resetEverything() {
    clearAnalysisVisuals();
    
    // Clear elements back to default uninitialized placeholders
    document.getElementById("code-traceback").innerText = "[No active traceback. Please ingest the demo repository to load incidents.]";
    document.getElementById("inc-badge").innerText = "N/A";
    document.getElementById("selected-inc-desc").innerText = "Select an incident to view details.";
    document.getElementById("findings-table-body").innerHTML = `
        <tr>
            <td colspan="6" class="table-placeholder">No analysis run yet. Click "Run Graph RCA" above.</td>
        </tr>
    `;
    document.getElementById("findings-count").innerText = "0 candidates";
    
    document.getElementById("terminal-console").innerHTML = `<div class="terminal-line"><span class="text-blue">[system]</span> Resetting Cognee Graph Database...</div>`;
    
    try {
        const response = await fetch("/api/reset", { method: "POST" });
        if (response.ok) {
            addLogLine("[backend] Database purged. Returned to clean slate status.", "text-green");
        } else {
            addLogLine("[backend] Failed to clear database.", "text-red");
        }
    } catch (err) {
        addLogLine("[backend] Server not active. Offline reset completed.", "text-amber");
    }
    
    // Re-verify and hydrate UI to uninitialized state
    await initializeDashboard();
}

// Clear visualization indicators
function clearAnalysisVisuals() {
    animatedVisitedNodes.clear();
    animatedCandidates.clear();
    animatedHops = {};
    animationStep = 0;
    animationProgress = 0;
    
    document.getElementById("findings-count").innerText = "0 candidates";
    document.getElementById("findings-table-body").innerHTML = `
        <tr>
            <td colspan="6" class="table-placeholder">No analysis run yet. Click "Run Graph RCA" above.</td>
        </tr>
    `;
    closePlaygroundDiff();
}

// --- Run RCA Engine Live Ingest & Query ---------------------------------------

async function runAnalysis() {
    if (analysisRunning) return;
    
    clearAnalysisVisuals();
    analysisRunning = true;
    
    const inc = INCIDENTS[selectedIncidentId];
    const kHops = parseInt(document.getElementById("param-hops").value);
    const relWindow = document.getElementById("param-window").value;

    const consoleEl = document.getElementById("terminal-console");
    consoleEl.innerHTML = ""; // Clear log

    addLogLine(`[cognee] Contacting local database...`, "text-muted");
    
    let backendFindings = [];
    let backendVerdict = "";
    let isOffline = false;

    // Trigger API request immediately in background
    try {
        const payload = {
            incident_id: selectedIncidentId,
            k_hops: kHops,
            release_window: relWindow
        };
        const response = await fetch("/api/analyze", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        if (response.ok) {
            const result = await response.json();
            backendFindings = result.findings;
            backendVerdict = result.verdict || "";
            addLogLine(`[api] POST /api/analyze completed successfully.`, "text-muted");
        } else {
            isOffline = true;
        }
    } catch (err) {
        isOffline = true;
    }

    if (isOffline) {
        // No canned findings, ever — the demo runs on real pipeline output only.
        addLogLine(`[error] Backend unreachable. Start it with:  python dashboard/server.py`, "text-amber");
        addLogLine(`[error] No analysis performed — findings only come from the live RCA engine.`, "text-amber");
        analysisRunning = false;
        return;
    }

    // Run visually engaging BFS animation steps
    setTimeout(() => {
        addLogLine(`[cognee] Graph store connected (Ladybug SQLite).`, "text-muted");
        addLogLine(`[rca.query] Parsing incident traceback...`, "text-blue");
    }, 450);

    setTimeout(() => {
        addLogLine(`[rca.query] Identified stack trace language: ${inc.lang.toUpperCase()}`, "text-blue");
        addLogLine(`[rca.query] Extracted seed frames:`, "text-blue");
        
        inc.seeds.forEach(s => {
            addLogLine(`  -> matched file/symbol: ${s}`, "text-cyan");
            animatedVisitedNodes.add(s);
            animatedHops[s] = 0;
        });

        animationStep = 1;
        animationProgress = 0;
    }, 1000);

    setTimeout(() => {
        addLogLine(`[rca.query] Traversing call-graph & imports (k_hops = ${kHops})...`, "text-blue");
        
        let traversedNodes = [];
        if (selectedIncidentId === "INC-1003") {
            traversedNodes = [
                { id: "web.helpers.logEvent", hop: 1 },
                { id: "web/client.js", hop: 1 },
                { id: "web/helpers.js", hop: 1 }
            ];
        } else {
            traversedNodes = [
                { id: "app.payments.validate_card", hop: 1 },
                { id: "app.payments.charge_card", hop: 1 },
                { id: "app.utils.format_currency", hop: 1 },
                { id: "app.db.save_record", hop: 1 },
                { id: "app/api.py", hop: 1 },
                { id: "app/payments.py", hop: 1 },
                { id: "app/utils.py", hop: 1 },
                { id: "app/db.py", hop: 1 }
            ];
            if (kHops >= 2) {
                traversedNodes.push(
                    { id: "app.payments.refund_payment", hop: 2 },
                    { id: "app.utils.retry", hop: 2 },
                    { id: "app.db.get_connection", hop: 2 }
                );
            }
        }

        traversedNodes.forEach(item => {
            if (item.hop <= kHops) {
                animatedVisitedNodes.add(item.id);
                animatedHops[item.id] = item.hop;
            }
        });

        animationStep = 2;
        animationProgress = 0;
    }, 2000);

    setTimeout(() => {
        addLogLine(`[rca.query] Comparing traversed nodes against release modifications...`, "text-blue");
        
        backendFindings.forEach(f => {
            animatedCandidates.add(f.name);
            addLogLine(`  -> Found modified symbol: ${f.name} [${f.type} in ${f.release}] (hops=${f.hops})`, "text-amber");
        });

        animationStep = 3;
        animationProgress = 0;
    }, 3200);

    setTimeout(() => {
        addLogLine(`[rca.query] Ingested traceback analyzed successfully. Sorting findings.`, "text-blue");
        
        renderFindingsTable(backendFindings);
        
        addLogLine(`\nIncident Verdict:`, "text-green");
        addLogLine(backendVerdict, "text-green");

        analysisRunning = false;
    }, 4000);
}

function addLogLine(text, className = "") {
    const consoleEl = document.getElementById("terminal-console");
    if (!consoleEl) return;

    const line = document.createElement("div");
    line.className = `terminal-line ${className}`;
    line.innerText = text;
    consoleEl.appendChild(line);
    
    // Auto scroll to bottom
    consoleEl.scrollTop = consoleEl.scrollHeight;
}

function renderFindingsTable(findings) {
    const tbody = document.getElementById("findings-table-body");
    const countEl = document.getElementById("findings-count");

    countEl.innerText = `${findings.length} candidate${findings.length === 1 ? "" : "s"}`;

    if (findings.length === 0) {
        tbody.innerHTML = `
            <tr>
                <td colspan="6" class="table-placeholder">No root-cause candidates found in matching release window & hop boundary.</td>
            </tr>
        `;
        return;
    }

    // Sort by score descending (matches ranks)
    const sorted = [...findings].sort((a, b) => b.score - a.score);

    tbody.innerHTML = "";
    sorted.forEach((f, idx) => {
        const tr = document.createElement("tr");
        if (f.isTrue) {
            tr.className = "row-root-cause";
        }

        tr.onclick = () => selectPlaygroundFinding(f);

        tr.innerHTML = `
            <td>
                <span class="rank-badge">${idx + 1}</span>
            </td>
            <td>
                <span class="symbol-cell">${f.name}</span>
                ${f.isTrue ? `<span class="true-cause-label">★ Root Cause</span>` : ""}
            </td>
            <td>
                <span class="release-tag-badge">${f.release}</span>
            </td>
            <td>${f.hops} hop${f.hops === 1 ? "" : "s"}</td>
            <td>
                <span class="score-value">${f.score.toLocaleString()}</span>
            </td>
            <td>
                <button class="btn-view-diff" onclick="event.stopPropagation(); selectPlaygroundFinding(${JSON.stringify(f).replace(/"/g, '&quot;')})">
                    View Diff
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// Select a finding in table to see its diff
function selectPlaygroundFinding(f) {
    const diffSection = document.getElementById("playground-diff-section");
    const diffTitle = document.getElementById("playground-diff-title");
    const diffCode = document.getElementById("playground-diff-code");

    diffSection.classList.remove("hidden");
    diffTitle.innerText = `${f.file} @ ${f.release} (${f.name})`;

    // Get matching diff
    const rel = RELEASES[f.release];
    if (rel && rel.diffs[f.file]) {
        diffCode.innerHTML = formatDiff(rel.diffs[f.file]);
    } else {
        diffCode.innerText = `// Diff not available for ${f.name} in release ${f.release}`;
    }

    // Scroll table/diff view
    diffSection.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function closePlaygroundDiff() {
    document.getElementById("playground-diff-section").classList.add("hidden");
}

// Format unified diff to colored HTML lines
function formatDiff(diffText) {
    const lines = diffText.split("\n");
    return lines.map(ln => {
        let cls = "";
        let escaped = ln.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        if (ln.startsWith("+")) {
            cls = "diff-line-add";
        } else if (ln.startsWith("-")) {
            cls = "diff-line-del";
        } else if (ln.startsWith("@@")) {
            cls = "diff-line-meta";
        }
        return `<span class="${cls}">${escaped}</span>`;
    }).join("\n");
}

// --- Releases Explorer Tab Logic ---------------------------------------------

function showRelease(tag) {
    if (!dbInitialized) return;
    selectedReleaseTag = tag;
    
    // Highlight release items
    document.querySelectorAll(".release-item").forEach(item => {
        item.classList.remove("active");
    });
    document.getElementById(`rel-${tag}`).classList.add("active");

    const rel = RELEASES[tag];
    
    // Title meta
    document.getElementById("release-detail-title").innerText = `Release ${rel.tag}`;
    document.getElementById("release-detail-sha").innerText = `SHA: ${rel.sha}`;
    document.getElementById("release-detail-date").innerText = rel.date;
    document.getElementById("release-detail-msg").innerText = rel.msg;

    // Build changes tree list
    const changesList = document.getElementById("release-changes-list");
    changesList.innerHTML = "";

    if (rel.changes.length === 0) {
        changesList.innerHTML = `<div class="text-muted italic" style="font-size: 12px; padding: 10px;">No logical symbol changes recorded. Entirely file changes.</div>`;
    } else {
        rel.changes.forEach(ch => {
            const div = document.createElement("div");
            div.className = "change-item";
            if (ch.file === selectedReleaseFile) {
                div.classList.add("active");
            }
            
            div.onclick = () => selectReleaseFileChange(ch.file);

            div.innerHTML = `
                <span class="change-symbol-name" title="${ch.symbol}">${ch.symbol}</span>
                <span class="change-tag change-${ch.type}">${ch.type}</span>
            `;
            changesList.appendChild(div);
        });
    }

    // Load initial diff
    const files = Object.keys(rel.diffs);
    if (files.length > 0) {
        // Auto-select first file if current selected file not in this release
        if (!files.includes(selectedReleaseFile)) {
            selectedReleaseFile = files[0];
        }
        selectReleaseFileChange(selectedReleaseFile);
    } else {
        document.getElementById("diff-file-title").innerText = "No code diffs available";
        document.getElementById("release-diff-code").innerText = "// Baseline full repository ingest. No changes.";
    }
}

function selectReleaseFileChange(filePath) {
    selectedReleaseFile = filePath;
    
    // Manage tree actives
    document.querySelectorAll(".change-item").forEach(item => {
        item.classList.remove("active");
    });
    
    // Find active element matching text and set active
    const items = document.querySelectorAll(".change-item");
    items.forEach(item => {
        const symbolText = item.querySelector(".change-symbol-name").title;
        const rel = RELEASES[selectedReleaseTag];
        const matchingChange = rel.changes.find(ch => ch.symbol === symbolText);
        if (matchingChange && matchingChange.file === filePath) {
            item.classList.add("active");
        }
    });

    document.getElementById("diff-file-title").innerText = `File Diff: ${filePath}`;
    const rel = RELEASES[selectedReleaseTag];
    if (rel && rel.diffs[filePath]) {
        document.getElementById("release-diff-code").innerHTML = formatDiff(rel.diffs[filePath]);
    } else {
        document.getElementById("release-diff-code").innerText = `// Diff details for ${filePath} not available in this mockup.`;
    }
}

// --- Canvas Force-Directed Code Graph Rendering -----------------------------

// Basic 2D physics loop (Repulsion / Attraction / Gravity)
function updateSimulation() {
    if (currentTab === "playground" && canvas) {
        // Only run simulation when playground is open
        const width = canvas.width;
        const height = canvas.height;

        const kRepel = 2200; // Repulsion constant
        const kAttract = 0.06; // Link spring strength
        const kGravity = 0.04; // Center pull strength
        const friction = 0.85;

        // 1. Calculate repulsion forces between all node pairs
        for (let i = 0; i < nodes.length; i++) {
            const n1 = nodes[i];
            if (n1 === draggingNode) continue;
            
            for (let j = i + 1; j < nodes.length; j++) {
                const n2 = nodes[j];
                
                const dx = n2.x - n1.x;
                const dy = n2.y - n1.y;
                let dist = Math.sqrt(dx * dx + dy * dy);
                if (dist === 0) dist = 1;
                
                // Repulsion force
                const force = kRepel / (dist * dist);
                const fx = (dx / dist) * force;
                const fy = (dy / dist) * force;
                
                n1.vx -= fx;
                n1.vy -= fy;
                n2.vx += fx;
                n2.vy += fy;
            }
        }

        // 2. Calculate link attraction forces
        edges.forEach(e => {
            const n1 = e.sourceNode;
            const n2 = e.targetNode;
            if (!n1 || !n2) return;

            const dx = n2.x - n1.x;
            const dy = n2.y - n1.y;
            const dist = Math.sqrt(dx * dx + dy * dy);
            if (dist === 0) return;

            // Spring force
            const force = kAttract * (dist - 120); // target length 120px
            const fx = (dx / dist) * force;
            const fy = (dy / dist) * force;

            if (n1 !== draggingNode) {
                n1.vx += fx;
                n1.vy += fy;
            }
            if (n2 !== draggingNode) {
                n2.vx -= fx;
                n2.vy -= fy;
            }
        });

        // 3. Central gravity and movement updates
        const cx = 400; // Center coordinate
        const cy = 300;
        
        nodes.forEach(n => {
            if (n === draggingNode) return;

            // Pull to center
            n.vx += (cx - n.x) * kGravity;
            n.vy += (cy - n.y) * kGravity;

            // Apply friction & move
            n.x += n.vx;
            n.y += n.vy;
            n.vx *= friction;
            n.vy *= friction;
        });

        // Render everything
        drawGraph();
    }

    requestAnimationFrame(updateSimulation);
}

// Render nodes, links and search wavefronts on Canvas
function drawGraph() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    
    if (!dbInitialized) {
        ctx.save();
        ctx.fillStyle = "#8b949e";
        ctx.font = "14px monospace";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText("[ Code Knowledge Graph Uninitialized ]", canvas.width / 2, canvas.height / 2 - 10);
        ctx.fillStyle = "#484f58";
        ctx.font = "11px monospace";
        ctx.fillText("Ingest the demo repository to construct and visualize the AST graph", canvas.width / 2, canvas.height / 2 + 15);
        ctx.restore();
        return;
    }
    
    ctx.save();
    // Apply panning and zoom
    ctx.translate(panX, panY);
    ctx.scale(zoomScale, zoomScale);

    // 1. Draw Links
    edges.forEach(e => {
        const n1 = e.sourceNode;
        const n2 = e.targetNode;
        if (!n1 || !n2) return;

        ctx.beginPath();
        ctx.moveTo(n1.x, n1.y);
        ctx.lineTo(n2.x, n2.y);
        
        // Link color matching type
        let strokeColor = "#21262d";
        let lineWidth = 1;
        let isDashed = false;

        if (e.type === "call") {
            strokeColor = "rgba(88, 166, 255, 0.25)";
            lineWidth = 1.5;
        } else if (e.type === "import") {
            strokeColor = "rgba(248, 113, 113, 0.25)";
            lineWidth = 1.5;
        } else if (e.type === "contain") {
            strokeColor = "rgba(48, 54, 61, 0.4)";
            isDashed = true;
        }

        // Highlight traversed links during animation
        const hasN1 = animatedVisitedNodes.has(n1.id);
        const hasN2 = animatedVisitedNodes.has(n2.id);
        
        if (animationStep >= 2 && hasN1 && hasN2) {
            const h1 = animatedHops[n1.id] !== undefined ? animatedHops[n1.id] : 99;
            const h2 = animatedHops[n2.id] !== undefined ? animatedHops[n2.id] : 99;
            
            if (h1 <= 2 && h2 <= 2) {
                strokeColor = "#38bdf8";
                lineWidth = 2.5;
                isDashed = false;
            }
        }

        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = lineWidth;
        if (isDashed) {
            ctx.setLineDash([4, 4]);
        } else {
            ctx.setLineDash([]);
        }
        ctx.stroke();
    });
    ctx.setLineDash([]);

    // 2. Draw Nodes
    nodes.forEach(n => {
        const isFile = n.type === "file";
        const radius = isFile ? 14 : 9;
        
        // Visual pulses/animations for traversed nodes
        const isVisited = animatedVisitedNodes.has(n.id);
        const isCandidate = animatedCandidates.has(n.id);
        const inc = INCIDENTS[selectedIncidentId];
        const isTracebackSeed = inc && inc.seeds.includes(n.id);

        // Nodes halo glow
        if (animationStep >= 1 && isTracebackSeed) {
            ctx.beginPath();
            ctx.arc(n.x, n.y, radius + 8, 0, Math.PI * 2);
            ctx.fillStyle = "rgba(56, 189, 248, 0.15)";
            ctx.fill();
        }

        if (animationStep >= 3 && isCandidate) {
            const isTrueCause = inc.findings.find(f => f.name === n.id && f.isTrue);
            ctx.beginPath();
            ctx.arc(n.x, n.y, radius + 10, 0, Math.PI * 2);
            ctx.fillStyle = isTrueCause ? "rgba(63, 185, 80, 0.15)" : "rgba(210, 153, 34, 0.15)";
            ctx.fill();
        }

        ctx.beginPath();
        if (isFile) {
            // Draw square for files
            ctx.rect(n.x - radius, n.y - radius, radius * 2, radius * 2);
        } else {
            // Draw circle for functions
            ctx.arc(n.x, n.y, radius, 0, Math.PI * 2);
        }

        // Color fills
        let fillColor = "#161b22";
        let strokeColor = "#30363d";
        let strokeWidth = 2;

        if (isFile) {
            fillColor = "#0d1117";
            strokeColor = "#38bdf8"; // Cyan file borders
        } else {
            fillColor = "#1f242c";
            strokeColor = "#ab7df6"; // Purple function borders
        }

        // Override color with active search states
        if (animationStep >= 1 && isTracebackSeed) {
            fillColor = "rgba(56, 189, 248, 0.9)"; // Bright blue trace seed
            strokeColor = "#f0f6fc";
            strokeWidth = 3;
        } else if (animationStep >= 2 && isVisited) {
            fillColor = "#1d3244";
            strokeColor = "#38bdf8"; // Visited blast radius nodes
        }

        if (animationStep >= 3 && isCandidate) {
            const isTrueCause = inc.findings.find(f => f.name === n.id && f.isTrue);
            if (isTrueCause) {
                fillColor = "#1b4721";
                strokeColor = "#3fb950"; // True root cause highlight
            } else {
                fillColor = "#443413";
                strokeColor = "#d29922"; // Other candidates in releases
            }
            strokeWidth = 3;
        }

        if (n === hoveredNode) {
            strokeColor = "#ffffff";
            strokeWidth = 3;
        }

        ctx.fillStyle = fillColor;
        ctx.fill();
        ctx.strokeStyle = strokeColor;
        ctx.lineWidth = strokeWidth;
        ctx.stroke();

        // Node labels
        ctx.font = isFile ? "bold 10px var(--font-sans)" : "10px var(--font-mono)";
        ctx.fillStyle = isFile ? "rgba(240, 246, 252, 0.85)" : "rgba(240, 246, 252, 0.65)";
        
        if (animationStep >= 3 && isCandidate) {
            ctx.fillStyle = "#ffffff";
            ctx.font = "bold 11px var(--font-mono)";
        }
        
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillText(n.name, n.x, n.y + radius + 4);
    });

    ctx.restore();
}

// --- Interaction / Drag & Drop -----------------------------------------------

function setupCanvasListeners() {
    canvas.addEventListener("mousedown", e => {
        const rect = canvas.getBoundingClientRect();
        const mX = e.clientX - rect.left;
        const mY = e.clientY - rect.top;

        // Convert mouse screen space to canvas world space
        const worldCoords = screenToWorld(mX, mY);
        
        // Find clicked node
        const clickedNode = findNodeAt(worldCoords.x, worldCoords.y);
        
        if (clickedNode) {
            draggingNode = clickedNode;
            // Lock position
            clickedNode.fx = clickedNode.x;
            clickedNode.fy = clickedNode.y;
        } else {
            // Drag background to pan
            isPanning = true;
            startPanX = mX - panX;
            startPanY = mY - panY;
        }
    });

    canvas.addEventListener("mousemove", e => {
        const rect = canvas.getBoundingClientRect();
        const mX = e.clientX - rect.left;
        const mY = e.clientY - rect.top;

        const worldCoords = screenToWorld(mX, mY);

        if (draggingNode) {
            // Update node coordinate
            draggingNode.x = worldCoords.x;
            draggingNode.y = worldCoords.y;
            draggingNode.fx = worldCoords.x;
            draggingNode.fy = worldCoords.y;
        } else if (isPanning) {
            // Update pan offset
            panX = mX - startPanX;
            panY = mY - startPanY;
        } else {
            // Hover check
            const found = findNodeAt(worldCoords.x, worldCoords.y);
            if (found !== hoveredNode) {
                hoveredNode = found;
                showNodeTooltip(found, mX, mY);
            }
        }
    });

    canvas.addEventListener("mouseup", () => {
        if (draggingNode) {
            // Release node physics lock
            draggingNode.fx = null;
            draggingNode.fy = null;
            draggingNode = null;
        }
        isPanning = false;
    });

    canvas.addEventListener("mouseleave", () => {
        if (draggingNode) {
            draggingNode.fx = null;
            draggingNode.fy = null;
            draggingNode = null;
        }
        isPanning = false;
        hoveredNode = null;
        hideNodeTooltip();
    });

    canvas.addEventListener("wheel", e => {
        e.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const mX = e.clientX - rect.left;
        const mY = e.clientY - rect.top;

        const beforeZoom = screenToWorld(mX, mY);

        // Zoom coefficient
        const zoomIntensity = 0.08;
        if (e.deltaY < 0) {
            zoomScale += zoomScale * zoomIntensity;
        } else {
            zoomScale -= zoomScale * zoomIntensity;
        }

        // Clamp zoom scale
        zoomScale = Math.max(0.4, Math.min(2.5, zoomScale));

        const afterZoom = screenToWorld(mX, mY);
        
        // Correct panning offset to keep mouse point anchored in world space
        panX += (afterZoom.x - beforeZoom.x) * zoomScale;
        panY += (afterZoom.y - beforeZoom.y) * zoomScale;
    });
}

function screenToWorld(sX, sY) {
    return {
        x: (sX - panX) / zoomScale,
        y: (sY - panY) / zoomScale
    };
}

function findNodeAt(x, y) {
    for (let i = nodes.length - 1; i >= 0; i--) {
        const n = nodes[i];
        const radius = n.type === "file" ? 14 : 9;
        const dx = n.x - x;
        const dy = n.y - y;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist <= radius + 5) {
            return n;
        }
    }
    return null;
}

function showNodeTooltip(node, sX, sY) {
    const tooltip = document.getElementById("node-tooltip");
    if (!node || !tooltip) {
        hideNodeTooltip();
        return;
    }

    let typeLabel = node.type === "file" ? "Code File" : "Function Def";
    let detailsHtml = "";

    if (node.type === "file") {
        const functionsInFile = nodes.filter(fn => fn.file === node.id).map(fn => fn.name);
        detailsHtml = `
            <div class="tooltip-row">
                <span class="tooltip-label">Functions:</span>
                <span class="tooltip-val">${functionsInFile.length}</span>
            </div>
            <div class="tooltip-row">
                <span class="tooltip-label">Symbols:</span>
                <span class="tooltip-val" style="font-size: 10px; max-width: 140px; text-align: right; overflow: hidden; text-overflow: ellipsis;">
                    ${functionsInFile.join(", ") || "none"}
                </span>
            </div>
        `;
    } else {
        // Function details
        detailsHtml = `
            <div class="tooltip-row">
                <span class="tooltip-label">Enclosing File:</span>
                <span class="tooltip-val" style="font-size: 10px;">${node.file}</span>
            </div>
        `;
        // Look up releases containing changes to this node
        const relChanges = [];
        Object.keys(RELEASES).forEach(tag => {
            const rel = RELEASES[tag];
            const hasChange = rel.changes.find(ch => ch.symbol === node.id);
            if (hasChange) {
                relChanges.push(`${tag} (${hasChange.type})`);
            }
        });
        if (relChanges.length > 0) {
            detailsHtml += `
                <div class="tooltip-row">
                    <span class="tooltip-label">Modifications:</span>
                    <span class="tooltip-val text-amber" style="font-size: 9px;">${relChanges.join(", ")}</span>
                </div>
            `;
        }
    }

    tooltip.innerHTML = `
        <div class="tooltip-title">${node.id}</div>
        <div class="tooltip-row">
            <span class="tooltip-label">Type:</span>
            <span class="tooltip-val">${typeLabel}</span>
        </div>
        ${detailsHtml}
    `;

    tooltip.style.display = "block";
    tooltip.style.left = `${sX + 15}px`;
    tooltip.style.top = `${sY + 10}px`;
}

function hideNodeTooltip() {
    const tooltip = document.getElementById("node-tooltip");
    if (tooltip) tooltip.style.display = "none";
}
