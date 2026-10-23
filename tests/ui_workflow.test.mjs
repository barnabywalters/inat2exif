import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { afterEach, test } from "node:test";
import vm from "node:vm";
import { JSDOM } from "jsdom";

const html = readFileSync(new URL("../src/inat2exif/web/index.html", import.meta.url), "utf8");
const source = readFileSync(new URL("../src/inat2exif/web/app.js", import.meta.url), "utf8");

const windows = [];
afterEach(async () => {
  await new Promise((resolve) => setImmediate(resolve));
  for (const window of windows.splice(0)) window.close();
});

async function app() {
  const dom = new JSDOM(html, { url: "http://localhost:8000/" });
  windows.push(dom.window);
  const { document } = dom.window;
  const elements = new Map([...document.querySelectorAll("[id]")].map((element) => [element.id, element]));
  const originalAddListener = dom.window.EventTarget.prototype.addEventListener;
  dom.window.EventTarget.prototype.addEventListener = function(name, callback, options) {
    this.listeners ??= new Map();
    this.listeners.set(name, callback);
    return originalAddListener.call(this, name, callback, options);
  };
  dom.window.HTMLElement.prototype.scrollIntoView = function() {};
  dom.window.HTMLDialogElement.prototype.showModal = function() { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function() {
    this.open = false;
    this.dispatchEvent(new dom.window.Event("close"));
  };
  const requests = [];
  const navigations = [];
  const alerts = [];
  const timers = new Map();
  let nextTimer = 1;
  let confirmed = true;
  let handler = (path) => {
    if (path === "/api/config") return { username: "observer", path: "/photos" };
    if (path === "/api/session/status") return { running: false, finished: false };
    if (path === "/api/cache") return { caches: [], total_bytes: 0 };
    return {};
  };
  const context = vm.createContext({
    document, window: dom.window, URLSearchParams, location: { assign: (url) => navigations.push(url) },
    console: { error() {} },
    setTimeout: (callback) => { const id = nextTimer++; timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id),
    setInterval: () => 1, clearInterval() {},
    confirm: () => confirmed, alert: (message) => alerts.push(message),
    fetch: async (path, options) => {
      requests.push({ path, options });
      const data = await handler(path, options);
      return { ok: true, headers: { get: () => "application/json" }, json: async () => data };
    },
  });
  vm.runInContext(source, context, { filename: new URL("../src/inat2exif/web/app.js", import.meta.url).pathname });
  await new Promise((resolve) => setImmediate(resolve));
  return {
    elements, document, requests, navigations, alerts, timers,
    run: (code) => vm.runInContext(code, context),
    set: (name, value) => { context[name] = value; },
    handle: (next) => { handler = next; },
    confirm: (value) => { confirmed = value; },
    click: (id, event = {}) => elements.get(id).listeners.get("click")(event),
  };
}

function ready(ui) {
  ui.run(`hasAnalysis = true; analysisConfigSignature = configSignature();
    previewAvailable = true; latestSavePreview = {total: 1, files: [{photo_id: 1}]};
    updateSidebarNavState();`);
}

test("starts in configuration with later workflow stages disabled", async () => {
  const ui = await app();
  assert.equal(ui.elements.get("settings-view").hidden, false);
  assert.equal(ui.elements.get("settings-btn").getAttribute("aria-current"), "step");
  for (const id of ["review-nav-btn", "pending-nav-btn", "saving-nav-btn", "save"]) {
    assert.equal(ui.elements.get(id).disabled, true, id);
  }
});

test("review remains available with no pending changes, and settings edits require reanalysis", async () => {
  const ui = await app();
  ready(ui);
  ui.run("latestSavePreview.total = 0; updateSidebarNavState();");
  assert.equal(ui.elements.get("pending-nav-btn").disabled, false);
  assert.equal(ui.elements.get("save").disabled, true);
  ui.elements.get("image-title-template").value = "Changed template";
  ui.run("updateSidebarNavState()");
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
  assert.match(ui.elements.get("config-status").textContent, /Settings have changed/);
  ui.elements.get("image-title-template").value = "";
  ui.run("savedConfigSignature = 'different saved settings'; updateSidebarNavState()");
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
});

test("running tasks lock other stages and use the correct step", async () => {
  const ui = await app();
  ready(ui);
  ui.run("analyzeRunning = true; beginTaskProgress(); showSettingsView();");
  assert.equal(ui.elements.get("progress-view").hidden, false);
  assert.equal(ui.elements.get("progress-nav-btn").getAttribute("aria-current"), "step");
  assert.equal(ui.elements.get("cancel-analysis").hidden, false);
  ui.run("analyzeRunning = false; saveRunning = true; beginTaskProgress('save');");
  assert.equal(ui.elements.get("saving-nav-btn").getAttribute("aria-current"), "step");
  assert.equal(ui.elements.get("cancel-analysis").hidden, true);
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
});

test("save is gated on a successfully loaded pending-changes review", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending')");
  assert.equal(ui.elements.get("save").disabled, true);
  ui.handle((path) => path === "/api/save-preview" ? { total: 1, files: [{ photo_id: 1 }] }
    : { total: 1, observations: [] });
  await ui.run("loadPendingChanges()");
  assert.equal(ui.elements.get("save").disabled, false);
  ui.handle(() => { throw new Error("offline"); });
  await ui.run("loadPendingChanges()");
  assert.equal(ui.elements.get("save").disabled, true);
  assert.match(ui.elements.get("pending-list").textContent, /Could not load/);
});

test("changed save plans must be reviewed again and are not written", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending'); pendingPlanSignature = JSON.stringify(latestSavePreview)");
  ui.handle(() => ({ total: 2, files: [{ photo_id: 1 }, { photo_id: 2 }] }));
  await ui.click("save");
  assert.equal(ui.requests.some((request) => request.path === "/api/save"), false);
  assert.match(ui.elements.get("workflow-notice").textContent, /review them before saving/);
});

test("successful saving returns to configuration with a completion message", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending'); pendingPlanSignature = JSON.stringify(latestSavePreview)");
  let saved = false;
  ui.handle((path) => {
    if (path === "/api/save-preview") return saved ? { total: 0, files: [] } : { total: 1, files: [{ photo_id: 1 }] };
    if (path === "/api/save") { saved = true; return { saved: 1, failed: 0 }; }
    if (path === "/api/tree") return { total: 0, matched: 0, needs_attention: 0, years: [] };
    return {};
  });
  const saving = ui.click("save");
  await ui.click("save");
  await saving;
  assert.equal(ui.elements.get("settings-view").hidden, false);
  assert.match(ui.elements.get("workflow-notice").textContent, /Sync complete: saved changes to 1/);
  assert.equal(ui.requests.filter((request) => request.path === "/api/save").length, 1);
});

test("partial failures stay on saving results with a route to retry", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending'); pendingPlanSignature = JSON.stringify(latestSavePreview)");
  ui.handle((path) => {
    if (path === "/api/save-preview") return { total: 1, files: [{ photo_id: 1 }] };
    if (path === "/api/save") return { saved: 0, failed: 1, failures: [{ path: "/photos/a.jpg", error: "Read-only file" }] };
    if (path === "/api/tree") return { total: 0, matched: 0, needs_attention: 0, years: [] };
    return {};
  });
  await ui.click("save");
  assert.equal(ui.elements.get("progress-view").hidden, false);
  assert.equal(ui.elements.get("save-failures").hidden, false);
  assert.equal(ui.elements.get("progress-review").hidden, false);
  assert.match(ui.elements.get("workflow-notice").textContent, /1 failed/);
});

test("analysis cancellation returns to configuration without enabling review", async () => {
  const ui = await app();
  ui.run("analyzeRunning = true; beginTaskProgress()");
  ui.handle(() => ({ running: false, finished: true, cancelled: true }));
  await ui.run("pollStatus()");
  assert.equal(ui.elements.get("settings-view").hidden, false);
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
  assert.match(ui.elements.get("workflow-notice").textContent, /Auto-match cancelled/);
});

test("analysis failure returns to configuration and exposes the error", async () => {
  const ui = await app();
  ui.run("analyzeRunning = true; beginTaskProgress()");
  ui.handle(() => ({ running: false, finished: true, error: "Invalid photo folder" }));
  await ui.run("pollStatus()");
  assert.equal(ui.elements.get("settings-view").hidden, false);
  assert.match(ui.elements.get("workflow-notice").textContent, /Invalid photo folder/);
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
});

test("successful empty analysis opens review and allows the no-changes path", async () => {
  const ui = await app();
  ui.run("analyzeRunning = true; beginTaskProgress()");
  ui.handle((path) => {
    if (path === "/api/session/status") return { running: false, finished: true, summary: { total: 0 } };
    if (path === "/api/tree") return { total: 0, years: [] };
    if (path === "/api/save-preview") return { total: 0, files: [] };
    return {};
  });
  await ui.run("pollStatus()");
  assert.equal(ui.elements.get("analysis-view").hidden, false);
  assert.equal(ui.elements.get("pending-nav-btn").disabled, false);
  await ui.run("loadPendingChanges()");
  assert.equal(ui.elements.get("save").disabled, true);
  assert.equal(ui.elements.get("pending-configure").hidden, false);
});

test("analysis saves the current form before starting and preserves extra settings", async () => {
  const ui = await app();
  ui.elements.get("username").value = "new-observer";
  ui.elements.get("image-title-template").value = "{{name}}";
  ui.handle((path, options) => {
    if (path === "/api/config") return options?.method === "PUT"
      ? JSON.parse(options.body) : { username: "old", path: "/photos", extra_option: "keep" };
    if (path === "/api/session/start") return { started: true };
    if (path === "/api/session/status") return { running: true, finished: false };
    return {};
  });
  await ui.elements.get("config-form").listeners.get("submit")({
    preventDefault() {}, submitter: ui.elements.get("analyze"),
  });
  const putIndex = ui.requests.findIndex((request) => request.options?.method === "PUT");
  const startIndex = ui.requests.findIndex((request) => request.path === "/api/session/start");
  assert.ok(putIndex < startIndex);
  const savedConfig = JSON.parse(ui.requests[putIndex].options.body);
  assert.equal(savedConfig.username, "new-observer");
  assert.equal(savedConfig.image_title_template, "{{name}}");
  assert.equal(savedConfig.extra_option, "keep");
  assert.equal(ui.elements.get("progress-view").hidden, false);
});

test("request errors remain on save results without reporting success", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending'); pendingPlanSignature = JSON.stringify(latestSavePreview)");
  ui.handle((path) => {
    if (path === "/api/save") throw new Error("Connection lost");
    return { total: 1, files: [{ photo_id: 1 }] };
  });
  await ui.click("save");
  assert.equal(ui.elements.get("progress-view").hidden, false);
  assert.equal(ui.elements.get("progress-heading").textContent, "Save results");
  assert.match(ui.elements.get("workflow-notice").textContent, /Connection lost/);
  assert.equal(ui.elements.get("progress-review").hidden, false);
});

test("rejected analysis does not certify changed settings against old matches", async () => {
  const ui = await app();
  ready(ui);
  ui.elements.get("username").value = "different-observer";
  ui.handle((path, options) => {
    if (path === "/api/config") return options?.method === "PUT" ? JSON.parse(options.body) : {};
    if (path === "/api/session/start") throw new Error("Invalid photo folder");
    return { running: false };
  });
  await ui.elements.get("config-form").listeners.get("submit")({
    preventDefault() {}, submitter: ui.elements.get("analyze"),
  });
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
  assert.equal(ui.elements.get("save").disabled, true);
  assert.match(ui.elements.get("workflow-notice").textContent, /Invalid photo folder/);
});

test("a refresh error after saving does not misreport successful writes as failed", async () => {
  const ui = await app();
  ready(ui);
  ui.run("setMainView('pending'); pendingPlanSignature = JSON.stringify(latestSavePreview)");
  ui.handle((path) => {
    if (path === "/api/save-preview") return { total: 1, files: [{ photo_id: 1 }] };
    if (path === "/api/save") return { saved: 1, failed: 0 };
    throw new Error("Cannot refresh matches");
  });
  await ui.click("save");
  assert.equal(ui.elements.get("settings-view").hidden, false);
  assert.match(ui.elements.get("workflow-notice").textContent, /Saved 1 file.*could not refresh/);
  assert.doesNotMatch(ui.elements.get("workflow-notice").textContent, /Save did not complete/);
});

test("saving uses indeterminate progress rather than invented file counts", async () => {
  const ui = await app();
  ui.run("renderLocalProgress({phase:'save', message:'Writing metadata', running:true})");
  assert.equal(ui.elements.get("progress-bar-wrap").classList.contains("is-indeterminate"), true);
  assert.equal(ui.elements.get("progress-bar-wrap").hasAttribute("aria-valuenow"), false);
  assert.match(ui.elements.get("progress-detail").textContent, /Per-file progress is not available/);
});

test("new settings round trip through the form and saved payload", async () => {
  const ui = await app();
  const options = {
    username: "observer", path: "/photos", taxonomy_tag_prefix: "",
    write_dates: false, write_people: false, write_taxonomy: false,
    write_locality: false, write_gps: false, write_title: false,
    write_description: false, write_hierarchical_tags: false,
  };
  ui.handle((path, request) => path === "/api/config"
    ? request?.method === "PUT" ? JSON.parse(request.body) : options : {});
  await ui.run("loadConfigIntoForm()");
  assert.equal(ui.elements.get("taxonomy-tag-prefix").value, "");
  for (const id of ["write-dates", "write-people", "write-taxonomy", "write-locality", "write-gps", "write-title", "write-description"]) {
    assert.equal(ui.elements.get(id).checked, false);
  }
  await ui.run("saveConfigFromForm()");
  const payload = JSON.parse(ui.requests.find((request) => request.options?.method === "PUT").options.body);
  for (const [key, value] of Object.entries(options)) assert.equal(payload[key], value);
});

test("continue stays at the bottom initially and is promoted for saved complete configs", async () => {
  const ui = await app();
  const actions = ui.elements.get("config-actions");
  assert.equal(actions.classList.contains("is-promoted"), false);
  ui.handle((path) => path === "/api/config/status" ? { ready: true } : { username: "observer", path: "/photos" });
  await ui.run("loadConfigIntoForm()");
  assert.equal(actions.classList.contains("is-promoted"), true);
  assert.equal(ui.elements.get("config-form").children[0], actions);
});

test("preview sample selection sends the username and selected observation without changing sync settings", async () => {
  const ui = await app();
  const signature = ui.run("configSignature()");
  ui.handle((path) => path.startsWith("/api/template-samples") ? { samples: [
    { obs_id: 42, name: "Bee", reasons: ["Most favourited"], comments_count: 2, faves_count: 4 },
    { obs_id: 43, name: "Wasp", reasons: ["Most recent"], comments_count: 0, faves_count: 0 },
  ] } : { title: "Wasp", caption: "A description" });
  await ui.run("loadPreviewSamples()");
  assert.equal(ui.elements.get("preview-sample").children.length, 3);
  ui.elements.get("preview-sample").value = "43";
  ui.elements.get("preview-sample").listeners.get("change")();
  await ui.run("refreshTemplatePreview()");
  const request = ui.requests.findLast((request) => request.path === "/api/template-preview");
  assert.equal(JSON.parse(request.options.body).sample_obs_id, 43);
  assert.equal(JSON.parse(request.options.body).username, "observer");
  assert.equal(ui.elements.get("image-title-preview").textContent, "Wasp");
  assert.equal(ui.run("configSignature()"), signature);
});

test("failed sample loading keeps the built-in preview available", async () => {
  const ui = await app();
  ui.handle((path) => {
    if (path.startsWith("/api/template-samples")) throw new Error("offline");
    return { title: "Example bee", caption: "Example description" };
  });
  await ui.run("loadPreviewSamples()");
  assert.match(ui.elements.get("preview-samples-status").textContent, /offline.*built-in example/);
  assert.equal(ui.elements.get("preview-sample").value, "");
  await ui.run("refreshTemplatePreview()");
  assert.equal(ui.elements.get("image-title-preview").textContent, "Example bee");
});

test("changing usernames discards in-flight preview sample responses", async () => {
  const ui = await app();
  let finish;
  ui.handle(() => new Promise((resolve) => { finish = resolve; }));
  const loading = ui.run("loadPreviewSamples()");
  ui.elements.get("username").value = "someone-else";
  ui.elements.get("username").listeners.get("input")();
  finish({ samples: [{ obs_id: 42, name: "Stale sample", reasons: [], comments_count: 0, faves_count: 0 }] });
  await loading;
  assert.equal(ui.elements.get("preview-sample").children.length, 1);
  assert.equal(ui.elements.get("preview-sample").value, "");
});

test("folder browser chooses the absolute server path and invalidates old matches", async () => {
  const ui = await app();
  ready(ui);
  ui.handle(() => ({ path: "/photos/new", parent: "/photos", folders: [] }));
  await ui.run("browseFolder('/photos/new')");
  await ui.click("folder-choose");
  assert.equal(ui.elements.get("path").value, "/photos/new");
  assert.equal(ui.elements.get("review-nav-btn").disabled, true);
  assert.equal(ui.elements.get("folder-choose").disabled, false);
});

test("folder errors never leave a stale selection enabled", async () => {
  const ui = await app();
  ui.handle(() => { throw new Error("Permission denied"); });
  await ui.run("browseFolder('/restricted')");
  assert.equal(ui.elements.get("folder-choose").disabled, true);
  assert.match(ui.elements.get("folder-error").textContent, /Permission denied/);
});

test("editing a folder location invalidates the previously browsed selection", async () => {
  const ui = await app();
  ui.handle(() => ({ path: "/photos", parent: "/", folders: [] }));
  await ui.run("browseFolder('/photos')");
  ui.elements.get("folder-location").value = "/other";
  ui.elements.get("folder-location").listeners.get("input")();
  assert.equal(ui.elements.get("folder-choose").disabled, true);
  await ui.click("folder-choose");
  assert.equal(ui.elements.get("path").value, "/photos");
});

test("Escape closes the folder picker", async () => {
  const ui = await app();
  const dialog = ui.elements.get("folder-picker");
  dialog.showModal();
  dialog.listeners.get("keydown")({ key: "Escape", preventDefault() {} });
  assert.equal(dialog.open, false);
});

test("signed-in status shows the verified account and warns before writing private locations", async () => {
  const ui = await app();
  ui.handle(() => ({ connected: true, username: "signed-in-observer", redirect_uri: "http://localhost:8000/api/auth/callback" }));
  await ui.run("loadAuthStatus()");
  assert.equal(ui.elements.get("username").value, "signed-in-observer");
  assert.equal(ui.elements.get("oauth-setup").hidden, true);
  assert.equal(ui.elements.get("oauth-disconnect").hidden, false);
  assert.equal(ui.elements.get("pending-private-warning").hidden, false);
  assert.match(ui.elements.get("auth-status").textContent, /private locations will be used/);
});

test("OAuth credentials only go to the dedicated endpoint and the secret is cleared before redirect", async () => {
  const ui = await app();
  ui.elements.get("oauth-client-id").value = "test-id";
  ui.elements.get("oauth-client-secret").value = "test-secret";
  const previous = ui.requests.length;
  ui.handle(() => ({ url: "https://www.inaturalist.org/oauth/authorize?state=test" }));
  await ui.click("oauth-sign-in");
  const requests = ui.requests.slice(previous);
  assert.equal(requests.length, 1);
  assert.equal(requests[0].path, "/api/auth/start");
  assert.equal(JSON.parse(requests[0].options.body).client_secret, "test-secret");
  assert.equal(ui.elements.get("oauth-client-secret").value, "");
  assert.equal(ui.navigations[0], "https://www.inaturalist.org/oauth/authorize?state=test");
  assert.equal(ui.run("JSON.stringify(formConfig()).includes('secret')"), false);
});

test("disconnect invalidates reviewed matches and returns to public-only configuration", async () => {
  const ui = await app();
  ready(ui);
  ui.run("authConnected = true; pendingPlanSignature = 'old-plan'; previewSamples = [{obs_id: 1}]");
  ui.handle(() => ({ connected: false }));
  await ui.click("oauth-disconnect");
  assert.equal(ui.run("hasAnalysis"), false);
  assert.equal(ui.run("pendingPlanSignature"), null);
  assert.equal(ui.run("previewSamples.length"), 0);
  assert.equal(ui.elements.get("save").disabled, true);
  assert.equal(ui.elements.get("pending-private-warning").hidden, true);
  assert.match(ui.elements.get("workflow-notice").textContent, /Previous matches were cleared/);
});

test("authentication errors are visible and do not silently switch modes", async () => {
  const ui = await app();
  ui.handle(() => { throw new Error("sign-in failed"); });
  await ui.click("oauth-sign-in");
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not start sign-in/);
  assert.equal(ui.navigations.length, 0);
  assert.equal(ui.run("authBusy"), false);
});

test("privacy counts explain uncertainty and include observations skipped for missing timestamps", async () => {
  const ui = await app();
  ui.handle(() => ({
    total: 10, authenticated: false, restricted_locations: 4, obscured_locations: 3, private_locations: 1,
    missing_coordinates: 2, missing_datetime: 3, skipped_photo_observations: 2, restricted_missing_datetime: 1,
  }));
  await ui.click("check-privacy");
  const text = ui.elements.get("privacy-report").textContent;
  assert.match(text, /10 observations checked/);
  assert.match(text, /2 photo observations/);
  assert.match(text, /not guaranteed to recover/);
  assert.equal(text, ui.elements.get("review-privacy-report").textContent);
});

test("privacy requests for a previous username cannot overwrite the current account", async () => {
  const ui = await app();
  let resolve;
  ui.handle(() => new Promise((done) => { resolve = done; }));
  const loading = ui.click("check-privacy");
  ui.elements.get("username").value = "different-observer";
  ui.elements.get("username").listeners.get("input")();
  resolve({ total: 123 });
  await loading;
  assert.doesNotMatch(ui.elements.get("privacy-report").textContent, /123/);
  assert.equal(ui.elements.get("check-privacy").disabled, false);
});

test("remembered application credentials prefill only the client ID and reuse a blank secret", async () => {
  const ui = await app();
  ui.handle(() => ({
    connected: false, credentials_saved: true, client_id: "remembered-client",
    credential_storage_supported: true, credential_storage_path: "/private/oauth-credentials.json",
  }));
  await ui.run("loadAuthStatus()");
  assert.equal(ui.elements.get("oauth-client-id").value, "remembered-client");
  assert.equal(ui.elements.get("oauth-client-secret").value, "");
  assert.equal(ui.elements.get("oauth-remember").checked, true);
  assert.equal(ui.elements.get("oauth-forget").hidden, false);
  assert.match(ui.elements.get("oauth-storage-status").textContent, /Access tokens are not saved/);
  ui.handle(() => ({ url: "https://www.inaturalist.org/oauth/authorize" }));
  await ui.click("oauth-sign-in");
  const request = ui.requests.findLast((entry) => entry.path === "/api/auth/start");
  assert.deepEqual(JSON.parse(request.options.body), {
    client_id: "remembered-client", client_secret: "", remember_credentials: true,
  });
});

test("forgetting saved credentials keeps current sign-in and removes the saved indicator", async () => {
  const ui = await app();
  ui.handle(() => ({
    connected: true, username: "tester", credentials_saved: true, client_id: "remembered-client",
    credential_storage_supported: true,
  }));
  await ui.run("loadAuthStatus()");
  ui.handle(() => ({ credentials_saved: false }));
  await ui.click("oauth-forget");
  assert.equal(ui.run("authConnected"), true);
  assert.equal(ui.elements.get("oauth-forget").hidden, true);
  assert.equal(ui.elements.get("oauth-remember").checked, false);
  assert.equal(ui.elements.get("oauth-client-id").value, "");
  assert.match(ui.elements.get("auth-status").textContent, /Signed in as tester/);
  assert.match(ui.elements.get("workflow-notice").textContent, /current sign-in is unchanged/);
});

test("storage errors remain visible and forgetting failures do not claim success", async () => {
  const ui = await app();
  ui.handle(() => ({ connected: false, credentials_saved: false,
    credential_storage_error: "Cannot read credentials", credential_storage_supported: true }));
  await ui.run("loadAuthStatus()");
  assert.match(ui.elements.get("oauth-storage-status").textContent, /Cannot read credentials/);
  assert.equal(ui.elements.get("oauth-forget").hidden, false);
  ui.handle(() => { throw new Error("Permission denied"); });
  await ui.click("oauth-forget");
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not forget/);
  assert.equal(ui.elements.get("oauth-forget").hidden, false);
});

test("disconnecting does not claim remembered application credentials were forgotten", async () => {
  const ui = await app();
  ui.handle(() => ({ connected: true, username: "tester", credentials_saved: true,
    client_id: "remembered-client", credential_storage_supported: true }));
  await ui.run("loadAuthStatus()");
  ui.handle(() => ({ connected: false }));
  await ui.click("oauth-disconnect");
  assert.equal(ui.run("authConnected"), false);
  assert.equal(ui.elements.get("oauth-remember").checked, true);
  assert.equal(ui.elements.get("oauth-forget").hidden, false);
});

function candidate(id, selected = false, overrides = {}) {
  return { photo_id: id, photo_ids: [id], name: `photo-${id}.jpg`, path: `/photos/photo-${id}.jpg`,
    url: `/api/image/local/${id}`, score: 0.9, selected, ...overrides };
}

function reviewDay(rows) {
  return { year: 2026, month: 6, day: 15, observation_count: rows.length, rows };
}

function reviewRow(obsId, overrides = {}) {
  return { obs_id: obsId, uri: `https://www.inaturalist.org/observations/${obsId}`, time: "10:30:00",
    state: "ambiguous", status: "unmatched", detail: "Needs a manual match",
    observation_photos: [{ photo_id: obsId, url: `/api/image/observation/${obsId}` }],
    candidates: [], ...overrides };
}

function treeData() {
  const counts = { actionable: 1, unmatchable: 1, photoless: 1, fuzzy: 1, exact: 1, tagged: 1 };
  const stats = { actionable_count: 1, matchable_count: 2, status_counts: counts };
  return { total: 6, matched: 5, needs_attention: 1, years: [
    { year: 2025, ...stats, months: [{ month: 12, ...stats, days: [{ day: 31, ...stats }] }] },
    { year: 2026, ...stats, months: [{ month: 6, ...stats, days: [
      { day: 14, matchable_count: 0, actionable_count: 0, status_counts: { photoless: 1 } },
      { day: 15, ...stats },
    ] }] },
  ] };
}

function reviewApi(ui, day, extra = () => undefined) {
  ui.handle((path, options) => extra(path, options) ?? (
    path === "/api/tree" ? treeData() : path.startsWith("/api/day/") ? day
      : path === "/api/save-preview" ? { total: 1, files: [{ photo_id: 1 }] } : {}
  ));
}

test("pending diffs show replacements, removals, GPS fields, and consolidated keyword paths accurately", async () => {
  const ui = await app();
  const tag = { kind: "tags", added: ["taxonomy/Animalia/Bee", "taxonomy/Animalia", "taxonomy",
    "taxonomy/Animalia/Bee", "taxonomy/Plantae/Bee"], removed: ["taxonomy/Old"], delimiter: "/" };
  const diff = [
    { key: "Xmp.dc.title", old: "Old title", new: "New title" },
    { key: "Xmp.dc.description", old: "Old caption", new: "" },
    { key: "Exif.GPSInfo.GPSLatitude", old: null, new: "51/1 30/1 0/1" },
    { key: "Xmp.dwc.emptyField", old: null, new: null },
    { key: "Xmp.dc.subject", ...tag },
    { key: "Xmp.digiKam.TagsList", ...tag },
  ];
  ui.set("pendingDiff", diff);
  const rendered = ui.run("renderPendingDiff(pendingDiff)");
  assert.equal(rendered.querySelectorAll(".pc-field").length, 5);
  assert.equal(rendered.querySelector(".pc-label").textContent, "Title");
  assert.match(rendered.textContent, /Old title.*New title/);
  assert.match(rendered.textContent, /Old caption/);
  assert.match(rendered.textContent, /GPSLatitude|GPS latitude/);
  assert.match(rendered.textContent, /\(empty\)/);
  assert.match(rendered.textContent, /Animalia › Bee/);
  assert.match(rendered.textContent, /Plantae › Bee/);
  assert.match(rendered.textContent, /×2/);
  assert.match(rendered.textContent, /2 parents/);
  assert.equal(rendered.querySelectorAll(".pc-tag-removed").length, 1);
  assert.equal(ui.run("pendingDiffFieldSummary(pendingDiff)"), "Title, Caption, Tags, 2 other fields");
  assert.equal(ui.run("tagDiffSignature({added:['A|B'], removed:[], delimiter:'|'}) === tagDiffSignature({added:['A/B'], removed:[], delimiter:'/'})"), true);
});

test("pending cards preserve file grouping, skipped removals, and navigation back to an observation", async () => {
  const ui = await app();
  ready(ui);
  const diff = [{ key: "Xmp.dc.title", old: "", new: "Bee" }];
  const data = { total: 7, new_count: 4, refresh_count: 1, remove_count: 2, observations: [
    { obs_id: 10, name: "Bombus terrestris", common_name: "Buff-tailed bumblebee", uri: "https://example.org/10",
      year: 2026, month: 6, day: 15, groups: [
        { reason: "new", files: [1, 2, 3, 4].map((photo_id) => ({ photo_id, path: `/photos/bee-${photo_id}.jpg` })), diff },
        { reason: "refresh", files: [{ photo_id: 5, path: "C:\\photos\\bee.jpg" }], diff },
      ] },
    { obs_id: 20, groups: [
      { reason: "remove", skipped: true, files: [{ photo_id: 6, path: "" }], diff: [] },
      { reason: "remove", files: [{ photo_id: 7, path: "/photos/old.jpg" }], diff },
      { reason: "new", files: [], diff: [] },
    ] },
  ] };
  ui.set("pendingData", data);
  ui.run("renderPendingChanges(pendingData)");
  const list = ui.elements.get("pending-list");
  assert.equal(list.querySelectorAll(".pc-obs").length, 2);
  assert.match(list.textContent, /Buff-tailed bumblebee \(Bombus terrestris\)/);
  assert.match(list.textContent, /Skipped: the file has no inat2exif dataset marker/);
  assert.match(list.textContent, /Metadata already matches/);
  assert.equal(list.querySelector(".pc-thumbs-more").textContent, "+1");
  assert.match(ui.elements.get("pending-summary").textContent, /7 files across 2 observations \(4 new, 1 update, 2 remove\)/);
  const day = reviewDay([reviewRow(10)]);
  reviewApi(ui, day);
  await list.querySelector(".pc-obs-name").onclick({ preventDefault() {} });
  assert.equal(ui.elements.get("analysis-view").hidden, false);
  assert.ok(ui.document.querySelector('tr[data-obs-id="10"]').classList.contains("pending-jump-highlight"));
  ui.handle((path) => path.startsWith("/api/observation/") ? { observed_at: "2026-06-15T10:30:00Z" }
    : path === "/api/tree" ? treeData() : path.startsWith("/api/day/") ? day : {});
  await ui.run("navigateToPendingObservation(10, {})");
  assert.ok(ui.requests.some((entry) => entry.path === "/api/observation/10"));
  ui.handle(() => ({ observed_at: "not a date" }));
  await ui.run("navigateToPendingObservation(10, {})");
  assert.equal(ui.elements.get("analysis-view").hidden, false);
});

test("day review renders dirty version groups, reference photos, photoless rows, and sorting controls", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([
    reviewRow(10, { candidates: [candidate(1, true, {
      photo_ids: [1, 2], dirty: true, dirty_photo_ids: [2],
      metadata_diffs: [{ names: ["bee_v1.jpg"], diff: [{ key: "Xmp.dc.title", old: "Old", new: "New" }] },
        { names: ["bee_v2.jpg"], diff: [{ key: "Xmp.dc.title", old: "", new: "New" }] }],
    })] }),
    reviewRow(20, { time: "09:00:00", state: "audio-only", observation_photos: [], candidates: [] }),
    reviewRow(30, { time: "11:00:00", state: "fuzzy", candidates: [candidate(3)] }),
  ]);
  reviewApi(ui, day);
  ui.set("dayData", day);
  ui.run("renderDay(dayData)");
  const wrap = ui.elements.get("day-table-wrap");
  assert.equal(wrap.querySelectorAll("tbody tr").length, 3);
  assert.match(wrap.querySelector(".candidate-dirty-mismatches").textContent, /bee_v1.jpg.*Old.*New.*bee_v2.jpg/);
  assert.match(wrap.querySelector(".photoless-note").textContent, /no photos/);
  assert.equal(wrap.querySelectorAll(".library-search-start").length, 1);
  assert.equal(wrap.querySelectorAll(".image-strip img").length, 2);
  assert.equal(wrap.querySelector(".candidate-card.selected").dataset.photoId, "1");
  assert.equal(ui.run("baselineSelectionByObs.get(10).has(2)"), true);
  ui.document.getElementById("observation-sort").onclick();
  assert.deepEqual([...wrap.querySelectorAll("tbody tr")].map((row) => row.dataset.obsId), ["20", "10", "30"]);
  ui.document.getElementById("observation-sort").onclick();
  assert.deepEqual([...wrap.querySelectorAll("tbody tr")].map((row) => row.dataset.obsId), ["30", "10", "20"]);
  ui.document.getElementById("status-sort").onclick();
  assert.deepEqual([...wrap.querySelectorAll("tbody tr")].map((row) => row.dataset.obsId), ["10", "30", "20"]);
  ui.run("currentRowOrder = []; currentSort = {column:'observation',direction:'desc'}");
  assert.equal(ui.run("applyRowOrder(dayData.rows)[0].obs_id"), 30);
  ui.run("currentRowOrder = [10,999]");
  assert.equal(ui.run("applyRowOrder(dayData.rows).length"), 3);
});

test("manual candidate selection works by keyboard and sends representative IDs for version groups", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([reviewRow(10, { candidates: [candidate(1, false, { photo_ids: [1, 2] })] })]);
  reviewApi(ui, day, (path, options) => {
    if (path.endsWith("/selection")) {
      const selected = JSON.parse(options.body).selected_photo_ids;
      day.rows[0].candidates[0].selected = selected.includes(1);
      return { selected_count: selected.length };
    }
  });
  ui.set("dayData", day);
  ui.run("renderDay(dayData)");
  await ui.document.querySelector(".candidate-card").onkeydown({ key: "Enter", preventDefault() {} });
  assert.ok(ui.document.querySelector(".candidate-card").classList.contains("selected"));
  let updates = ui.requests.filter((entry) => entry.path.endsWith("/selection"));
  assert.deepEqual(JSON.parse(updates[0].options.body).selected_photo_ids, [1]);
  await ui.document.querySelector(".candidate-card").onkeydown({ key: " ", preventDefault() {} });
  assert.equal(ui.document.querySelector(".candidate-card").classList.contains("selected"), false);
  updates = ui.requests.filter((entry) => entry.path.endsWith("/selection"));
  assert.deepEqual(JSON.parse(updates[1].options.body).selected_photo_ids, []);
});

test("moving a conflicting photo needs confirmation and clears only that version group from its previous observation", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([
    reviewRow(10, { candidates: [candidate(1, true, { photo_ids: [1, 2] }), candidate(3, true)] }),
    reviewRow(20, { candidates: [candidate(1, false, { photo_ids: [1, 2] })] }),
  ]);
  reviewApi(ui, day);
  ui.set("dayData", day);
  ui.run("renderDay(dayData)");
  const card = () => ui.document.querySelector('tr[data-obs-id="20"] .candidate-card');
  assert.ok(card().classList.contains("has-conflict"));
  let choosing = card().onclick();
  ui.document.querySelector(".modal-btn-cancel").onclick();
  await choosing;
  assert.equal(ui.requests.some((entry) => entry.path.endsWith("/selection")), false);
  choosing = card().onclick();
  ui.document.querySelector(".modal-btn-confirm").onclick();
  await choosing;
  const updates = ui.requests.filter((entry) => entry.path.endsWith("/selection"));
  assert.deepEqual(updates.map((entry) => [entry.path, JSON.parse(entry.options.body).selected_photo_ids]), [
    ["/api/observation/10/selection", [3]], ["/api/observation/20/selection", [1]],
  ]);
  assert.equal(ui.document.querySelector(".modal-overlay"), null);
});

test("tree navigation opens the current month, marks attention, and refreshes the selected day", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([reviewRow(10)]);
  reviewApi(ui, day);
  ui.set("treeData", treeData());
  ui.run("renderTree(treeData)");
  const tree = ui.elements.get("tree");
  assert.match(tree.firstElementChild.querySelector(".tree-summary-label").textContent, /2026/);
  assert.ok(tree.querySelector(".is-actionable"));
  assert.ok(tree.querySelector(".is-unmatchable-only"));
  assert.ok(tree.querySelector(".tree-stat-photoless"));
  await tree.querySelector(".day-item").onclick();
  assert.ok(tree.querySelector(".day-item.active"));
  assert.equal(ui.elements.get("analysis-view").hidden, false);
  assert.equal(ui.run("currentDay.day"), 15);
  assert.equal(ui.run("detailsBlock('A year').querySelector('summary').textContent"), "A year");
});

test("whole-library search streams reusable cards, removes stale candidates, and accepts a result", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([reviewRow(10)]);
  const found = { ...candidate(1), taken: "2026:06:15 10:30:00", offset: "+0s" };
  const status = { obs_id: 10, running: true, message: "Comparing", total: 10, current: 2, candidates: [found] };
  reviewApi(ui, day, (path) => path.endsWith("/library-search") ? status : undefined);
  ui.set("dayData", day);
  ui.run("renderDay(dayData)");
  await ui.document.querySelector(".library-search-start").onclick();
  const panel = ui.document.querySelector(".library-search-panel");
  assert.equal(panel.querySelector(".ls-message").textContent, "Comparing");
  assert.equal(panel.querySelector("progress").value, 2);
  assert.equal(panel.querySelector(".ls-actions button").textContent, "Stop");
  const card = panel.querySelector(".library-candidate");
  ui.set("searchStatus", { ...status, current: 5, candidates: [{ ...found, score: 0.99 }] });
  ui.run("updateLibrarySearchPanel(searchStatus)");
  assert.equal(panel.querySelector(".library-candidate"), card);
  assert.match(card.textContent, /0.990/);
  await card.querySelector("button").onclick();
  assert.ok(ui.requests.some((entry) => entry.path === "/api/observation/10/library-search/accept"
    && JSON.parse(entry.options.body).photo_id === 1));
  assert.equal(ui.run("librarySearchStatus"), null);
  ui.set("searchStatus", { ...status, running: false, total: 0, candidates: [] });
  ui.run("updateLibrarySearchPanel(searchStatus)");
  const empty = ui.document.querySelector(".library-search-panel");
  assert.equal(empty.querySelectorAll(".library-candidate").length, 0);
  assert.equal(empty.querySelector("progress").hidden, true);
  empty.querySelector(".ls-actions button").onclick();
  assert.equal(ui.document.querySelector(".library-search-panel"), null);
});

test("whole-library search failures remain visible and polls can recover", async () => {
  const ui = await app();
  ready(ui);
  const day = reviewDay([reviewRow(10), reviewRow(20)]);
  reviewApi(ui, day);
  ui.set("dayData", day);
  ui.run("renderDay(dayData); librarySearchStatus = {obs_id:20,running:true}");
  ui.confirm(false);
  await ui.run("startLibrarySearch(10)");
  assert.equal(ui.requests.some((entry) => entry.path.endsWith("/library-search")), false);
  ui.confirm(true);
  ui.handle(() => { throw new Error("Offline"); });
  await ui.run("startLibrarySearch(10)");
  await ui.run("stopLibrarySearch()");
  await ui.run("pollLibrarySearch()");
  await ui.run("acceptLibrarySearchCandidate(10,{photo_id:1})");
  assert.deepEqual(ui.alerts.map((message) => message.split(":")[0]), [
    "Couldn't start the library search", "Couldn't stop the search", "Couldn't use that photo",
  ]);
  assert.ok(ui.timers.size > 0);
  ui.handle(() => ({ obs_id: 10, running: false, message: "Stopped", error: "Offline", total: 0, candidates: [] }));
  await ui.run("pollLibrarySearch()");
  assert.ok(ui.document.querySelector(".library-search-panel").classList.contains("has-error"));
  ui.run("closeLibrarySearchPanel(10)");
  assert.equal(ui.run("librarySearchStatus"), null);
});

test("cache controls display sizes, require confirmation, and surface deletion errors", async () => {
  const ui = await app();
  const info = { cache_dir: "/cache", total_bytes: 2048,
    caches: [{ name: "observations", label: "Observation data", description: "Downloaded observations", size_bytes: 2048 }] };
  ui.set("cacheInfo", info);
  ui.run("renderCacheInfo(cacheInfo)");
  assert.equal(ui.elements.get("cache-total").textContent, "2.0 KB");
  assert.match(ui.elements.get("cache-table-body").textContent, /Observation data.*Downloaded observations/);
  ui.confirm(false);
  await ui.run("clearCache('observations','Observation data')");
  assert.equal(ui.requests.some((entry) => entry.options?.method === "DELETE"), false);
  ui.confirm(true);
  ui.handle(() => ({ ...info, total_bytes: 0, caches: [] }));
  await ui.elements.get("cache-table-body").querySelector("button").listeners.get("click")();
  assert.equal(ui.elements.get("clear-all-caches").disabled, true);
  assert.ok(ui.requests.some((entry) => entry.path === "/api/cache/observations"));
  ui.handle(() => { throw new Error("Permission denied"); });
  await ui.click("clear-all-caches");
  assert.match(ui.alerts[0], /Failed to clear cache/);
  assert.match(ui.elements.get("cache-table-body").textContent, /Could not load cache sizes/);
  assert.equal(ui.run("formatBytes(123456789)"), "118 MB");
});

test("template completions only appear inside open variable expressions and convert array paths", async () => {
  const ui = await app();
  const fields = [
    { path: "comments[].body", kind: "string", description: "Comment text" },
    { path: "name", kind: "string", source: "alias" },
    { path: "user", kind: "object" }, { path: "comments", kind: "array" },
    { path: "faves_count", kind: "integer" }, { path: "captive", kind: "boolean" },
  ];
  ui.set("fields", fields);
  ui.run("templateFieldEntries = fields");
  function completion(text, prefix = "", explicit = true) {
    ui.set("completionContext", {
      state: { doc: { sliceString: (start, end) => text.slice(start, end) } },
      pos: text.length, explicit, matchBefore: () => ({ from: text.length - prefix.length, text: prefix }),
    });
    return ui.run("buildTemplateFieldCompletions(completionContext)");
  }
  assert.equal(completion("Plain text"), null);
  assert.equal(completion("{% for"), null);
  assert.equal(completion("{{ name }}"), null);
  assert.equal(completion("{{ ", "", false), null);
  const matched = completion("{{ comments[0].b", "comments[0].b");
  assert.equal(matched.options.length, 1);
  assert.equal(matched.options[0].apply, "comments[0].body");
  const all = completion("{{ ");
  assert.deepEqual(Array.from(all.options, (entry) => entry.type), ["variable", "variable", "class", "property", "constant", "keyword"]);
  ui.set("field", { description: "Status", enum: ["research", "casual"], source: "alias", format: "date-time" });
  assert.equal(ui.run("fieldCompletionInfo(field)"), "Status\n\nEnum: research, casual\n\nTemplate alias\n\nFormat: date-time");
});

test("template preview response sequencing prevents stale successes and errors overwriting edits", async () => {
  const ui = await app();
  let resolve, reject;
  ui.handle(() => new Promise((done, fail) => { resolve = done; reject = fail; }));
  const first = ui.run("refreshTemplatePreview()");
  ui.run("scheduleTemplatePreview()");
  resolve({ title: "Stale", caption: "Stale" });
  await first;
  assert.doesNotMatch(ui.elements.get("image-title-preview").textContent, /Stale/);
  const failed = ui.run("refreshTemplatePreview()");
  ui.run("scheduleTemplatePreview()");
  reject(new Error("Old failure"));
  await failed;
  assert.doesNotMatch(ui.elements.get("image-title-preview").textContent, /Old failure/);
  ui.handle(() => { throw new Error("Invalid Jinja template"); });
  await ui.run("refreshTemplatePreview()");
  assert.match(ui.elements.get("image-title-preview").textContent, /Invalid Jinja/);
  assert.ok(ui.elements.get("image-title-preview").classList.contains("is-error"));
  ui.handle(() => ({ title: "Bee", caption: "Caption", sample_obs_id: 42 }));
  await ui.run("refreshTemplatePreview()");
  assert.equal(ui.elements.get("image-title-preview").textContent, "Bee");
  assert.equal(ui.elements.get("image-title-preview").classList.contains("is-error"), false);
});

test("template field catalog renders safe suggestions and reports failures without stale entries", async () => {
  const ui = await app();
  ui.handle(() => ({
    source: "schema", sample_obs_id: 42,
    fields: [
      { path: "comments[].body", description: "Comment text", kind: "string" },
      { path: "<img src=x>", description: "Untrusted schema label", kind: "string" },
    ],
  }));
  await ui.run("loadTemplateFieldData()");
  const list = ui.elements.get("template-field-list");
  assert.equal(list.querySelectorAll("button").length, 2);
  assert.equal(list.querySelector("img"), null);
  assert.equal(list.firstElementChild.title, "Comment text");
  assert.match(ui.elements.get("template-sample-meta").textContent, /iNaturalist schema.*obs:42/);
  list.firstElementChild.click();
  assert.equal(ui.elements.get("image-title-template").value, "");
  ui.handle(() => { throw new Error("Schema unavailable"); });
  await ui.run("loadTemplateFieldData()");
  assert.equal(list.childElementCount, 0);
  assert.equal(ui.run("templateFieldEntries.length"), 0);
  assert.match(ui.elements.get("template-sample-meta").textContent, /Autocomplete unavailable: Schema unavailable/);
});

test("saving settings reports errors and auto-match reconnects if the server started despite a lost response", async () => {
  const ui = await app();
  const submit = (submitter) => ui.elements.get("config-form").listeners.get("submit")({
    preventDefault() {}, submitter,
  });
  ui.handle(() => { throw new Error("Connection lost"); });
  await submit();
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not save settings: Connection lost/);
  await submit(ui.elements.get("analyze"));
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not start or reconnect to auto-match/);
  ui.handle((path) => {
    if (path === "/api/session/status") return { running: true };
    throw new Error("Connection lost");
  });
  await submit(ui.elements.get("analyze"));
  assert.equal(ui.elements.get("progress-view").hidden, false);
  assert.equal(ui.elements.get("analyze").disabled, true);
});

test("cancellation and review navigation expose request failures and remain usable for retry", async () => {
  const ui = await app();
  ui.handle(() => { throw new Error("Server unavailable"); });
  await ui.click("cancel-analysis");
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not cancel auto-match/);
  assert.equal(ui.elements.get("cancel-analysis").disabled, false);
  ui.handle(() => ({}));
  await ui.click("cancel-analysis");
  assert.match(ui.elements.get("workflow-notice").textContent, /Cancellation requested/);
  ready(ui);
  ui.run("currentDay = {year:2026,month:6,day:15}");
  ui.handle(() => { throw new Error("Day unavailable"); });
  ui.click("review-nav-btn");
  await new Promise((resolve) => setImmediate(resolve));
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not load matches: Day unavailable/);
});

test("quit is only offered by the launcher and replaces the page after stopping", async () => {
  const ui = await app();
  assert.equal(ui.elements.get("quit-app").hidden, true);
  ui.handle((path) => (path === "/api/app-info" ? { app: "inat2exif", quit_available: true } : {}));
  await ui.run("loadAppInfo()");
  assert.equal(ui.elements.get("quit-app").hidden, false);
  await ui.click("quit-app");
  assert.equal(ui.requests.at(-1).path, "/api/app/quit");
  assert.equal(ui.requests.at(-1).options.method, "POST");
  assert.match(ui.document.body.textContent, /inat2exif has stopped/);
  assert.equal(ui.document.getElementById("settings-view"), null);
});

test("quit is hidden when app info cannot be loaded", async () => {
  const ui = await app();
  ui.elements.get("quit-app").hidden = false;
  ui.handle(() => { throw new Error("offline"); });
  await ui.run("loadAppInfo()");
  assert.equal(ui.elements.get("quit-app").hidden, true);
});

test("quit protects running tasks and unsaved review work", async () => {
  const ui = await app();
  ui.run("analyzeRunning = true");
  await ui.click("quit-app");
  assert.match(ui.elements.get("workflow-notice").textContent, /Cancel or finish/);
  assert.equal(ui.requests.some(({ path }) => path === "/api/app/quit"), false);

  ui.run("analyzeRunning = false");
  ready(ui);
  ui.confirm(false);
  await ui.click("quit-app");
  assert.equal(ui.requests.some(({ path }) => path === "/api/app/quit"), false);

  ui.confirm(true);
  ui.handle((path) => { if (path === "/api/app/quit") throw new Error("Wait for the current save to finish"); return {}; });
  await ui.click("quit-app");
  assert.equal(ui.elements.get("quit-app").disabled, false);
  assert.match(ui.elements.get("workflow-notice").textContent, /Could not quit: Wait for the current save/);
  assert.notEqual(ui.document.getElementById("settings-view"), null);
});
