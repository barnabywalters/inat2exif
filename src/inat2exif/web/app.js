const treeEl = document.getElementById("tree");
const summaryEl = document.getElementById("summary");
const dayMetaEl = document.getElementById("day-meta");
const dayTableWrapEl = document.getElementById("day-table-wrap");
const settingsViewEl = document.getElementById("settings-view");
const analysisViewEl = document.getElementById("analysis-view");
const progressViewEl = document.getElementById("progress-view");
const pendingViewEl = document.getElementById("pending-view");

const configForm = document.getElementById("config-form");
const settingsBtn = document.getElementById("settings-btn");
const progressNavBtn = document.getElementById("progress-nav-btn");
const pendingNavBtn = document.getElementById("pending-nav-btn");
const reviewNavBtn = document.getElementById("review-nav-btn");
const savingNavBtn = document.getElementById("saving-nav-btn");
const reviewNextBtn = document.getElementById("review-next");
const cancelAnalysisBtn = document.getElementById("cancel-analysis");
const progressReviewBtn = document.getElementById("progress-review");
const progressConfigureBtn = document.getElementById("progress-configure");
const pendingConfigureBtn = document.getElementById("pending-configure");
const workflowNotice = document.getElementById("workflow-notice");
const saveSettingsBtn = document.getElementById("save-settings");
const analyzeBtn = document.getElementById("analyze");
const saveBtn = document.getElementById("save");
const usernameInput = document.getElementById("username");
const pathInput = document.getElementById("path");
const ignoreHiddenFilesInput = document.getElementById("ignore-hidden-files");
const writeHierarchicalTagsInput = document.getElementById("write-hierarchical-tags");
const imageTitleTemplateInput = document.getElementById("image-title-template");
const imageCaptionTemplateInput = document.getElementById("image-caption-template");
const imageTitleTemplateEditorEl = document.getElementById("image-title-template-editor");
const imageCaptionTemplateEditorEl = document.getElementById("image-caption-template-editor");
const imageTitlePreviewEl = document.getElementById("image-title-preview");
const imageCaptionPreviewEl = document.getElementById("image-caption-preview");
const templateSampleMetaEl = document.getElementById("template-sample-meta");
const templateFieldListEl = document.getElementById("template-field-list");
const progressHead = document.getElementById("progress-head");
const progressBar = document.getElementById("progress-bar");
const progressDetail = document.getElementById("progress-detail");
const progressLog = document.getElementById("progress-log");
const cacheTableBody = document.getElementById("cache-table-body");
const cacheTotalEl = document.getElementById("cache-total");
const cacheDirEl = document.getElementById("cache-dir");
const clearAllCachesBtn = document.getElementById("clear-all-caches");
const metadataToggleInputs = new Map([
  ["write_dates", "write-dates"], ["write_people", "write-people"],
  ["write_taxonomy", "write-taxonomy"], ["write_locality", "write-locality"],
  ["write_gps", "write-gps"], ["write_title", "write-title"],
  ["write_description", "write-description"],
].map(([key, id]) => [key, document.getElementById(id)]));
const taxonomyTagPrefixInput = document.getElementById("taxonomy-tag-prefix");
const previewSampleSelect = document.getElementById("preview-sample");
const previewSamplesStatus = document.getElementById("preview-samples-status");
const previewSampleLink = document.getElementById("preview-sample-link");
const configActions = document.getElementById("config-actions");
const folderPicker = document.getElementById("folder-picker");
const folderLocationInput = document.getElementById("folder-location");
const folderChooseBtn = document.getElementById("folder-choose");
const folderParentBtn = document.getElementById("folder-parent");

let currentDay = null;
let analyzeRunning = false;
let saveRunning = false;
let statusTimer = null;
let currentDayRows = [];
let currentRowOrder = [];
let currentSort = { column: "status", direction: "asc" };
let baselineSelectionByObs = new Map();
let currentSelectionByObs = new Map();
let photoToObsMap = new Map(); // photo_id -> Set of obs_ids where it's selected
let latestSavePreview = { total: 0, refresh_count: 0, new_count: 0, files: [] };
let codeMirrorModulesPromise = null;
let templatePreviewTimer = null;
let templatePreviewRequestId = 0;
let templateFieldEntries = [];
let activeTemplateEditorKey = "title";
let activeMainView = "settings";
let hasAnalysis = false;
let analysisConfigSignature = null;
let savedConfigSignature = null;
let progressTask = "analysis";
let saveOutcome = null;
let previewAvailable = false;
let pendingPlanSignature = null;
let startingAnalysis = false;
let preparingSave = false;
let lastTree = null;
let pollingStatus = false;
let previewSamplesTimer = null;
let previewSamplesRequestId = 0;
let previewSamplesUsername = "";
let previewSamples = [];
let folderRequestId = 0;
let selectedFolder = null;
let parentFolder = null;
let authConnected = false;
let authStorageStatus = {};
let authBusy = false;
let privacyRequestId = 0;
const templateEditors = new Map();

const TEMPLATE_EDITOR_SPECS = {
  title: {
    key: "title",
    label: "title",
    textarea: imageTitleTemplateInput,
    shell: imageTitleTemplateEditorEl,
    preview: imageTitlePreviewEl,
  },
  caption: {
    key: "caption",
    label: "description",
    textarea: imageCaptionTemplateInput,
    shell: imageCaptionTemplateEditorEl,
    preview: imageCaptionPreviewEl,
  },
};

function anyTaskRunning() {
  return analyzeRunning || saveRunning || startingAnalysis || preparingSave || authBusy;
}

function showNotice(message, isError = false) {
  workflowNotice.textContent = message;
  workflowNotice.hidden = !message;
  workflowNotice.classList.toggle("is-error", isError);
}

function formConfig() {
  return {
    username: usernameInput.value.trim(),
    path: pathInput.value.trim(),
    ignore_hidden_files: ignoreHiddenFilesInput.checked,
    write_hierarchical_tags: writeHierarchicalTagsInput.checked,
    taxonomy_tag_prefix: taxonomyTagPrefixInput.value.trim(),
    ...Object.fromEntries([...metadataToggleInputs].map(([key, input]) => [key, input.checked])),
    image_title_template: getTemplateEditorValue("title"),
    image_caption_template: getTemplateEditorValue("caption"),
  };
}

function configSignature() {
  return JSON.stringify(formConfig());
}

function needsNewAnalysis() {
  return hasAnalysis && (analysisConfigSignature !== configSignature() || analysisConfigSignature !== savedConfigSignature);
}

function updateSidebarNavState() {
  const busy = anyTaskRunning();
  const reviewEnabled = hasAnalysis && !needsNewAnalysis() && !busy;
  const step = activeMainView === "settings" ? 1 : activeMainView === "analysis" ? 3
    : activeMainView === "pending" ? 4 : progressTask === "save" ? 5 : 2;
  const navButtons = [settingsBtn, progressNavBtn, reviewNavBtn, pendingNavBtn, savingNavBtn];
  navButtons.forEach((button, index) => {
    const active = step === index + 1;
    button.classList.toggle("is-active", active);
    button.classList.toggle("is-complete", hasAnalysis && !needsNewAnalysis() && index < step - 1);
    if (active) button.setAttribute("aria-current", "step");
    else button.removeAttribute("aria-current");
  });
  settingsBtn.disabled = busy;
  progressNavBtn.disabled = !analyzeRunning;
  reviewNavBtn.disabled = !reviewEnabled;
  pendingNavBtn.disabled = !reviewEnabled;
  savingNavBtn.disabled = analyzeRunning || startingAnalysis || preparingSave || (!saveRunning && !saveOutcome);
  reviewNextBtn.disabled = !reviewEnabled || !previewAvailable;
  reviewNextBtn.textContent = latestSavePreview.total > 0
    ? `Review changes to ${latestSavePreview.total} file${latestSavePreview.total === 1 ? "" : "s"}` : "Continue to pending changes";
  saveBtn.disabled = !reviewEnabled || !previewAvailable || !pendingPlanSignature || latestSavePreview.total === 0;
  analyzeBtn.disabled = busy;
  saveSettingsBtn.disabled = busy;
  configForm.inert = busy;
  cancelAnalysisBtn.hidden = !analyzeRunning;
  progressConfigureBtn.hidden = busy;
  progressReviewBtn.hidden = busy || !hasAnalysis || needsNewAnalysis() || progressTask !== "save";
  document.getElementById("review-sidebar").hidden = activeMainView !== "analysis";
  pendingConfigureBtn.hidden = latestSavePreview.total > 0;
  document.getElementById("config-status").textContent = needsNewAnalysis()
    ? "Settings have changed. Run auto-match again to update matches and pending changes before saving."
    : "Ready? Save your settings and start auto-matching. No image metadata will be written yet.";
  taxonomyTagPrefixInput.disabled = !writeHierarchicalTagsInput.checked;
}

function setMainView(view) {
  if (anyTaskRunning() && view !== "progress") return;
  const normalized = view === "analysis" || view === "progress" || view === "pending" ? view : "settings";
  const changed = activeMainView !== normalized;
  activeMainView = normalized;

  settingsViewEl.hidden = normalized !== "settings";
  analysisViewEl.hidden = normalized !== "analysis";
  progressViewEl.hidden = normalized !== "progress";
  pendingViewEl.hidden = normalized !== "pending";
  updateSidebarNavState();
  if (changed) {
    document.querySelector(".content").scrollTop = 0;
    const heading = document.querySelector(`#${normalized === "analysis" ? "review" : normalized === "settings" ? "settings" : normalized === "pending" ? "pending" : "progress"}-heading`);
    heading.focus({ preventScroll: true });
  }
}

function showSettingsView() {
  setMainView("settings");
  loadCacheInfo();
}

function showAnalysisView() {
  setMainView("analysis");
}

function showProgressView() {
  setMainView("progress");
}

function showPendingView() {
  if (!hasAnalysis || needsNewAnalysis() || anyTaskRunning()) return;
  pendingPlanSignature = null;
  setMainView("pending");
  void loadPendingChanges();
}

function highlightObservationRow(obsId) {
  const row = dayTableWrapEl.querySelector(`tr[data-obs-id="${obsId}"]`);
  if (!row) {
    return;
  }
  row.classList.add("pending-jump-highlight");
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  window.setTimeout(() => {
    row.classList.remove("pending-jump-highlight");
  }, 1800);
}

async function navigateToPendingObservation(obsId, fileEntry) {
  let year = Number(fileEntry?.year);
  let month = Number(fileEntry?.month);
  let day = Number(fileEntry?.day);

  if (!Number.isInteger(year) || !Number.isInteger(month) || !Number.isInteger(day)) {
    const detail = await api(`/api/observation/${obsId}`);
    const observedAt = new Date(detail?.observed_at || "");
    if (!Number.isNaN(observedAt.getTime())) {
      year = observedAt.getFullYear();
      month = observedAt.getMonth() + 1;
      day = observedAt.getDate();
    }
  }

  if (Number.isInteger(year) && Number.isInteger(month) && Number.isInteger(day)) {
    await loadDay(year, month, day);
    highlightObservationRow(obsId);
    return;
  }

  showAnalysisView();
}

function beginTaskProgress(task = "analysis") {
  progressTask = task;
  const saved = task === "save" && !saveRunning && saveOutcome;
  document.getElementById("progress-step").textContent = task === "save" ? "Step 5 of 5" : "Step 2 of 5";
  document.getElementById("progress-heading").textContent = saved ? "Save results" : task === "save" ? "Saving metadata" : "Auto-matching your observations";
  document.getElementById("progress-help").textContent = saved
    ? "Review the save result below. You can review remaining changes to retry, or return to configuration to set up another sync."
    : task === "save"
    ? "Writing the reviewed metadata to your image files. Keep this tab open until saving finishes. You will return to configuration when all files are saved."
    : "Finding local photos and checking their metadata. No image files are being changed. You will review the matches next.";
  showProgressView();
}

function toSelectionSetFromRow(row) {
  const selected = new Set();
  for (const candidate of row.candidates || []) {
    if (!candidate.selected) {
      continue;
    }
    for (const photoId of candidate.photo_ids || [candidate.photo_id]) {
      selected.add(Number(photoId));
    }
  }
  return selected;
}

function applySavePreviewToButton(preview) {
  const total = Number(preview?.total || 0);
  const label = total === 0 ? "No file changes to save" : `Save changes to ${total} file${total === 1 ? "" : "s"}`;
  saveBtn.textContent = label;
  return total;
}

function updateSaveButtonLabel() {
  return applySavePreviewToButton(latestSavePreview);
}

async function refreshSavePreview() {
  try {
    latestSavePreview = await api("/api/save-preview");
    previewAvailable = true;
  } catch (error) {
    previewAvailable = false;
    pendingPlanSignature = null;
    showNotice(`Could not check pending changes: ${error.message}. Return to Review changes to try again.`, true);
  }
  updateSidebarNavState();
  return applySavePreviewToButton(latestSavePreview);
}

function buildPhotoToObsMap() {
  photoToObsMap = new Map();
  for (const [obsId, photoIds] of currentSelectionByObs.entries()) {
    for (const photoId of photoIds) {
      if (!photoToObsMap.has(photoId)) {
        photoToObsMap.set(photoId, new Set());
      }
      photoToObsMap.get(photoId).add(obsId);
    }
  }
}

function getPhotosInConflict(photoId) {
  // Returns Set of obs_ids this photo is selected in
  return photoToObsMap.get(photoId) || new Set();
}

function syncSelectionMapsFromRows(rows) {
  for (const row of rows || []) {
    const selectionSet = toSelectionSetFromRow(row);
    currentSelectionByObs.set(row.obs_id, selectionSet);
    if (!baselineSelectionByObs.has(row.obs_id)) {
      // Baseline should only include photos that were pre-tagged (dirty).
      // Auto-matched untagged photos are NOT in baseline, so they show as "new selections".
      const baseline = new Set();
      for (const candidate of row.candidates || []) {
        for (const dirtyPhotoId of candidate.dirty_photo_ids || []) {
          baseline.add(Number(dirtyPhotoId));
        }
      }
      baselineSelectionByObs.set(row.obs_id, baseline);
    }
  }
  buildPhotoToObsMap();
  void refreshSavePreview();
}

async function initializeSaveTrackingFromServer() {
  const state = await api("/api/selection-state");
  baselineSelectionByObs = new Map();
  currentSelectionByObs = new Map();

  for (const row of state.rows || []) {
    const obsId = Number(row.obs_id);
    const selected = new Set((row.selected_photo_ids || []).map((id) => Number(id)));
    const dirty = new Set((row.dirty_photo_ids || []).map((id) => Number(id)));
    currentSelectionByObs.set(obsId, selected);
    baselineSelectionByObs.set(obsId, dirty);
  }

  buildPhotoToObsMap();
  await refreshSavePreview();
}

function resetSaveTracking() {
  baselineSelectionByObs = new Map();
  currentSelectionByObs = new Map();
  photoToObsMap = new Map();
  latestSavePreview = { total: 0, refresh_count: 0, new_count: 0, files: [] };
  previewAvailable = false;
  pendingPlanSignature = null;
  updateSaveButtonLabel();
  updateSidebarNavState();
}

const PENDING_FIELD_LABELS = {
  "Xmp.dc.title": "Title",
  "Xmp.dc.description": "Caption",
  "Xmp.lr.hierarchicalSubject": "Lightroom keywords",
  "Xmp.dc.subject": "Keywords",
  "Xmp.digiKam.TagsList": "digiKam tags",
  "Xmp.dcterms.references": "References",
  "Xmp.dwc.inaturalistLogin": "iNaturalist login",
};

const PENDING_PRIMARY_FIELDS = ["Xmp.dc.title", "Xmp.dc.description"];

function pendingFieldLabel(key) {
  if (PENDING_FIELD_LABELS[key]) {
    return PENDING_FIELD_LABELS[key];
  }
  const local = String(key).split(".").pop() || String(key);
  const words = local.replace(/([a-z])([A-Z])/g, "$1 $2").split(" ");
  return words
    .map((word, index) => {
      if (index === 0) return word.charAt(0).toUpperCase() + word.slice(1);
      return word === word.toUpperCase() ? word : word.toLowerCase();
    })
    .join(" ");
}

function makeEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// Collapse hierarchical tags into their deepest entries: "taxonomy|A|B" plus its
// ancestors "taxonomy|A" and "taxonomy" become one chip labelled "B".
function summarizeTagList(tags, delimiter) {
  const counts = new Map();
  for (const tag of tags) counts.set(String(tag), (counts.get(String(tag)) || 0) + 1);
  const paths = Array.from(counts, ([tag, count]) => ({ tag, count, segments: tag.split(delimiter).map((s) => s.trim()) }));
  const isPrefix = (a, b) => a.segments.length < b.segments.length && a.segments.every((seg, i) => seg === b.segments[i]);
  const leaves = paths.filter((p) => !paths.some((other) => isPrefix(p, other)));
  const items = leaves.map((leaf) => ({
    leaf,
    ancestors: paths.filter((p) => isPrefix(p, leaf)),
  }));
  // Show enough trailing segments to tell apart leaves with the same name.
  const labelFor = (item, depth) => item.leaf.segments.slice(-depth).join(" › ");
  for (const item of items) {
    let depth = 1;
    while (
      depth < item.leaf.segments.length &&
      items.some((other) => other !== item && labelFor(other, depth) === labelFor(item, depth))
    ) {
      depth += 1;
    }
    item.label = labelFor(item, depth);
  }
  return items;
}

function renderTagChips(container, tags, delimiter, kind) {
  for (const item of summarizeTagList(tags, delimiter)) {
    const chip = makeEl("span", `pc-tag pc-tag-${kind}`);
    chip.appendChild(makeEl("span", "pc-tag-sign", kind === "added" ? "+" : "−"));
    chip.appendChild(makeEl("span", "pc-tag-name", item.label));
    if (item.leaf.count > 1) {
      chip.appendChild(makeEl("span", "pc-tag-extra", `×${item.leaf.count}`));
    }
    if (item.ancestors.length > 0) {
      chip.appendChild(makeEl("span", "pc-tag-extra", `& ${item.ancestors.length} parent${item.ancestors.length === 1 ? "" : "s"}`));
    }
    const fullPaths = [item.leaf.tag, ...item.ancestors.map((a) => a.tag)];
    chip.title = fullPaths.join("\n");
    container.appendChild(chip);
  }
}

function tagDiffSignature(entry) {
  const split = (list) => (list || []).map((tag) => String(tag).split(entry.delimiter).map((s) => s.trim()).join("\u0000")).sort();
  return JSON.stringify([split(entry.added), split(entry.removed)]);
}

function renderPendingFieldRow(label, keys, valueNode) {
  const row = makeEl("div", "pc-field");
  const labelEl = makeEl("span", "pc-label", label);
  labelEl.title = keys.join("\n");
  row.append(labelEl, valueNode);
  return row;
}

function renderValueChange(entry) {
  const value = makeEl("span", "pc-value");
  const hasOld = entry.old !== null && entry.old !== undefined && entry.old !== "";
  const hasNew = entry.new !== null && entry.new !== undefined && entry.new !== "";
  if (hasOld) {
    const oldEl = makeEl("span", "pc-old", entry.old);
    oldEl.title = `Current: ${entry.old}`;
    value.appendChild(oldEl);
  }
  if (hasOld && hasNew) {
    value.appendChild(makeEl("span", "pc-arrow", "→"));
  }
  if (hasNew) {
    const newEl = makeEl("span", "pc-new", entry.new);
    newEl.title = `New: ${entry.new}`;
    value.appendChild(newEl);
  }
  if (!hasOld && !hasNew) {
    value.appendChild(makeEl("span", "status-text", "(empty)"));
  }
  return value;
}

function renderTagGroup(entries) {
  const value = makeEl("span", "pc-value pc-tags");
  const first = entries[0];
  renderTagChips(value, first.removed || [], first.delimiter, "removed");
  renderTagChips(value, first.added || [], first.delimiter, "added");
  const keys = entries.map((entry) => entry.key);
  const label = entries.length === 1 ? pendingFieldLabel(first.key) : "Tags";
  return renderPendingFieldRow(label, keys, value);
}

function renderPendingDiff(diff) {
  const wrap = makeEl("div", "pc-diff");
  const byKey = new Map(diff.map((entry) => [entry.key, entry]));

  for (const key of PENDING_PRIMARY_FIELDS) {
    const entry = byKey.get(key);
    if (entry) {
      wrap.appendChild(renderPendingFieldRow(pendingFieldLabel(key), [key], renderValueChange(entry)));
    }
  }

  // The three tag fields usually change identically; show them as one "Tags" row.
  const tagGroups = new Map();
  for (const entry of diff) {
    if (entry.kind !== "tags") continue;
    const signature = tagDiffSignature(entry);
    if (!tagGroups.has(signature)) tagGroups.set(signature, []);
    tagGroups.get(signature).push(entry);
  }
  for (const entries of tagGroups.values()) {
    wrap.appendChild(renderTagGroup(entries));
  }

  const others = diff
    .filter((entry) => entry.kind !== "tags" && !PENDING_PRIMARY_FIELDS.includes(entry.key))
    .sort((a, b) => pendingFieldLabel(a.key).localeCompare(pendingFieldLabel(b.key)));
  for (const entry of others) {
    wrap.appendChild(renderPendingFieldRow(pendingFieldLabel(entry.key), [entry.key], renderValueChange(entry)));
  }
  return wrap;
}

function pendingDiffFieldSummary(diff) {
  const hasTags = diff.some((entry) => entry.kind === "tags");
  const labels = [
    ...PENDING_PRIMARY_FIELDS.filter((key) => diff.some((entry) => entry.key === key)).map(pendingFieldLabel),
    ...(hasTags ? ["Tags"] : []),
  ];
  const otherCount = diff.filter((entry) => entry.kind !== "tags" && !PENDING_PRIMARY_FIELDS.includes(entry.key)).length;
  if (otherCount > 0) labels.push(`${otherCount} other field${otherCount === 1 ? "" : "s"}`);
  return labels.join(", ");
}

const PENDING_REASON_LABELS = { new: "New", refresh: "Update", remove: "Remove" };

function fileBaseName(path) {
  return String(path || "").split(/[\\/]/).pop();
}

function renderPendingGroup(group) {
  const groupEl = makeEl("div", "pc-group");

  const thumbs = makeEl("div", "pc-thumbs");
  const maxThumbs = 3;
  for (const file of group.files.slice(0, maxThumbs)) {
    const img = document.createElement("img");
    img.src = `/api/image/local/${file.photo_id}?t=${Date.now()}`;
    img.loading = "lazy";
    img.alt = fileBaseName(file.path);
    img.title = file.path;
    thumbs.appendChild(img);
  }
  if (group.files.length > maxThumbs) {
    thumbs.appendChild(makeEl("span", "pc-thumbs-more", `+${group.files.length - maxThumbs}`));
  }

  const body = makeEl("div", "pc-group-body");
  const head = makeEl("div", "pc-group-head");
  head.appendChild(makeEl("span", `pending-change-reason pending-change-${group.reason}`, PENDING_REASON_LABELS[group.reason] || group.reason));
  const fileNames = makeEl("span", "pc-files");
  group.files.forEach((file, index) => {
    if (index > 0) fileNames.appendChild(document.createTextNode(", "));
    const name = makeEl("span", "pc-file", fileBaseName(file.path) || `photo ${file.photo_id}`);
    name.title = file.path;
    fileNames.appendChild(name);
  });
  head.appendChild(fileNames);
  body.appendChild(head);

  if (group.skipped) {
    body.appendChild(makeEl("div", "status-text", "Skipped: the file has no inat2exif dataset marker, so nothing will be removed."));
  } else if (group.diff.length === 0) {
    body.appendChild(makeEl("div", "status-text", "Metadata already matches; the observation link will be written."));
  } else if (group.reason === "new" || group.reason === "remove") {
    // Every field is being written (or removed), so the list itself is not news; keep it folded.
    const details = makeEl("details", "pc-more");
    const verb = group.reason === "new" ? "Writes" : "Removes";
    details.appendChild(makeEl("summary", null, `${verb} ${pendingDiffFieldSummary(group.diff)}`));
    details.appendChild(renderPendingDiff(group.diff));
    body.appendChild(details);
  } else {
    body.appendChild(renderPendingDiff(group.diff));
  }

  groupEl.append(thumbs, body);
  return groupEl;
}

function renderPendingObservation(obs) {
  const card = makeEl("article", "pc-obs");
  const header = makeEl("div", "pc-obs-head");
  const link = makeEl("a", "pc-obs-name");
  link.href = "#";
  const name = obs.common_name && obs.name ? `${obs.common_name} (${obs.name})` : obs.common_name || obs.name || `Observation ${obs.obs_id}`;
  link.textContent = name;
  link.title = "Show this observation in the day view";
  link.onclick = async (event) => {
    event.preventDefault();
    await navigateToPendingObservation(obs.obs_id, obs);
  };
  header.appendChild(link);
  const meta = makeEl("span", "pc-obs-meta");
  if (obs.year && obs.month && obs.day) {
    meta.appendChild(document.createTextNode(`${obs.year}-${String(obs.month).padStart(2, "0")}-${String(obs.day).padStart(2, "0")} · `));
  }
  if (obs.uri) {
    const external = makeEl("a", null, `obs ${obs.obs_id} ↗`);
    external.href = obs.uri;
    external.target = "_blank";
    external.rel = "noopener";
    meta.appendChild(external);
  } else {
    meta.appendChild(document.createTextNode(`obs ${obs.obs_id}`));
  }
  header.appendChild(meta);
  card.appendChild(header);

  for (const group of obs.groups || []) {
    card.appendChild(renderPendingGroup(group));
  }
  return card;
}

let pendingChangesRequestSeq = 0;

async function loadPendingChanges() {
  const summaryEl = document.getElementById("pending-summary");
  const listEl = document.getElementById("pending-list");
  const requestSeq = ++pendingChangesRequestSeq;
  pendingPlanSignature = null;
  updateSidebarNavState();
  if (!listEl.hasChildNodes()) {
    listEl.textContent = "Loading pending changes…";
  }
  let data;
  try {
    await refreshSavePreview();
    if (!previewAvailable) throw new Error("The save plan is unavailable. Please try again.");
    data = await api("/api/pending-changes");
  } catch (error) {
    if (requestSeq === pendingChangesRequestSeq) {
      summaryEl.textContent = "";
      listEl.textContent = `Could not load pending changes: ${error.message || error}`;
      updateSidebarNavState();
    }
    return;
  }
  if (requestSeq !== pendingChangesRequestSeq) {
    return;
  }
  renderPendingChanges(data);
  pendingPlanSignature = JSON.stringify(latestSavePreview);
  updateSidebarNavState();
}

function renderPendingChanges(data) {
  const summaryEl = document.getElementById("pending-summary");
  const listEl = document.getElementById("pending-list");
  const { total = 0, refresh_count = 0, new_count = 0, remove_count = 0, observations = [] } = data || {};

  const parts = [];
  if (new_count > 0) parts.push(`${new_count} new`);
  if (refresh_count > 0) parts.push(`${refresh_count} update`);
  if (remove_count > 0) parts.push(`${remove_count} remove`);
  const summary = parts.length > 0 ? parts.join(", ") : "No changes";
  const obsCount = observations.length;
  summaryEl.textContent = `${total} file${total !== 1 ? "s" : ""} across ${obsCount} observation${obsCount !== 1 ? "s" : ""} (${summary})`;

  listEl.innerHTML = "";
  if (observations.length === 0) {
    listEl.textContent = "No files need to be changed. You can go back to check your matches, or return to configuration to adjust your settings.";
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const obs of observations) {
    fragment.appendChild(renderPendingObservation(obs));
  }
  listEl.appendChild(fragment);
}

function showConflictConfirmation(photoName, obsId, conflictingObsIds) {
  return new Promise((resolve) => {
    const modal = document.createElement("div");
    modal.className = "modal-overlay";
    
    const content = document.createElement("div");
    content.className = "modal-content";
    
    const title = document.createElement("h2");
    title.textContent = "Photo Already Selected";
    
    const message = document.createElement("p");
    const conflictList = Array.from(conflictingObsIds).join(", ");
    message.innerHTML = `<strong>${photoName}</strong> is already selected for observation(s) <strong>${conflictList}</strong>.<br><br>If you select it for obs:${obsId}, it will be deselected from the other observation(s). Continue?`;
    
    const buttonContainer = document.createElement("div");
    buttonContainer.className = "modal-buttons";
    
    const confirmBtn = document.createElement("button");
    confirmBtn.textContent = "Yes, move to this observation";
    confirmBtn.className = "modal-btn-confirm";
    confirmBtn.onclick = () => {
      document.body.removeChild(modal);
      resolve(true);
    };
    
    const cancelBtn = document.createElement("button");
    cancelBtn.textContent = "Cancel";
    cancelBtn.className = "modal-btn-cancel";
    cancelBtn.onclick = () => {
      document.body.removeChild(modal);
      resolve(false);
    };
    
    buttonContainer.append(confirmBtn, cancelBtn);
    content.append(title, message, buttonContainer);
    modal.appendChild(content);
    document.body.appendChild(modal);
  });
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" ? JSON.stringify(data.detail ?? data) : data;
    throw new Error(detail);
  }
  return data;
}

async function loadCodeMirrorModules() {
  if (!codeMirrorModulesPromise) {
    codeMirrorModulesPromise = import("/web/vendor/codemirror.js").then(({
      state: stateModule,
      view: viewModule,
      language: languageModule,
      commands: commandsModule,
      search: searchModule,
      autocomplete: autocompleteModule,
      lint: lintModule,
      jinja: jinjaModule,
    }) => ({
      EditorState: stateModule.EditorState,
      EditorView: viewModule.EditorView,
      keymap: viewModule.keymap,
      basicSetup: [
        viewModule.lineNumbers(),
        viewModule.highlightActiveLineGutter(),
        viewModule.highlightSpecialChars(),
        commandsModule.history(),
        languageModule.foldGutter(),
        viewModule.drawSelection(),
        viewModule.dropCursor(),
        stateModule.EditorState.allowMultipleSelections.of(true),
        languageModule.indentOnInput(),
        languageModule.syntaxHighlighting(languageModule.defaultHighlightStyle, { fallback: true }),
        languageModule.bracketMatching(),
        autocompleteModule.closeBrackets(),
        viewModule.rectangularSelection(),
        viewModule.crosshairCursor(),
        viewModule.highlightActiveLine(),
        searchModule.highlightSelectionMatches(),
        viewModule.keymap.of([
          ...autocompleteModule.closeBracketsKeymap,
          ...commandsModule.defaultKeymap,
          ...searchModule.searchKeymap,
          ...commandsModule.historyKeymap,
          ...languageModule.foldKeymap,
          ...autocompleteModule.completionKeymap,
          ...lintModule.lintKeymap,
        ]),
      ],
      autocompletion: autocompleteModule.autocompletion,
      startCompletion: autocompleteModule.startCompletion,
      jinja: jinjaModule.jinja,
    }));
  }
  return codeMirrorModulesPromise;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function templatePathToReference(path) {
  return String(path || "").replaceAll("[]", "[0]");
}

function getTemplateEditorValue(key) {
  const editor = templateEditors.get(key);
  if (editor) {
    return editor.view.state.doc.toString();
  }
  return TEMPLATE_EDITOR_SPECS[key]?.textarea?.value || "";
}

function setTemplateEditorValue(key, value) {
  const nextValue = String(value || "");
  const editor = templateEditors.get(key);
  if (editor) {
    const currentValue = editor.view.state.doc.toString();
    if (currentValue !== nextValue) {
      editor.view.dispatch({
        changes: { from: 0, to: currentValue.length, insert: nextValue },
      });
    }
  }
  if (TEMPLATE_EDITOR_SPECS[key]?.textarea) {
    TEMPLATE_EDITOR_SPECS[key].textarea.value = nextValue;
  }
}

function setPreviewState(element, text, { isError = false } = {}) {
  element.classList.toggle("is-error", isError);
  element.textContent = text;
}

function setTemplateFieldMeta(payload) {
  const fragments = [];
  if (payload?.source) {
    fragments.push(payload.source === "schema" ? "Suggestions from iNaturalist schema" : `Suggestions from ${payload.source}`);
  }
  if (payload?.sample_obs_id) {
    fragments.push(`Preview sample obs:${payload.sample_obs_id}`);
  }
  templateSampleMetaEl.textContent = fragments.join(" | ");
}

function fieldCompletionInfo(entry) {
  const parts = [];
  if (entry.description) {
    parts.push(entry.description);
  }
  if (entry.enum?.length) {
    parts.push(`Enum: ${entry.enum.join(", ")}`);
  }
  if (entry.source === "alias") {
    parts.push("Template alias");
  }
  if (entry.format) {
    parts.push(`Format: ${entry.format}`);
  }
  return parts.join("\n\n");
}

function fieldCompletionType(kind) {
  const normalized = String(kind || "").toLowerCase();
  if (normalized === "object") {
    return "class";
  }
  if (normalized === "array" || normalized === "list") {
    return "property";
  }
  if (normalized === "integer" || normalized === "number" || normalized === "float") {
    return "constant";
  }
  if (normalized === "boolean") {
    return "keyword";
  }
  return "variable";
}

function findOpenTemplateDelimiter(state, position) {
  const before = state.doc.sliceString(0, position);
  const lastVariableOpen = before.lastIndexOf("{{");
  const lastBlockOpen = before.lastIndexOf("{%" );
  const lastVariableClose = before.lastIndexOf("}}");
  const lastBlockClose = before.lastIndexOf("%}");
  const lastOpenIndex = Math.max(lastVariableOpen, lastBlockOpen);
  const lastCloseIndex = Math.max(lastVariableClose, lastBlockClose);

  if (lastOpenIndex <= lastCloseIndex) {
    return null;
  }
  return lastOpenIndex === lastVariableOpen ? "{{" : "{%";
}

function buildTemplateFieldCompletions(context) {
  const delimiter = findOpenTemplateDelimiter(context.state, context.pos);
  if (delimiter !== "{{") {
    return null;
  }

  const token = context.matchBefore(/[\w.[\]]*/);
  const from = token ? token.from : context.pos;
  const prefix = token ? token.text : "";
  if (!context.explicit && !prefix) {
    return null;
  }

  const normalizedPrefix = prefix.toLowerCase();
  const options = templateFieldEntries
    .filter((entry) => {
      const path = String(entry.path || "");
      const reference = templatePathToReference(path);
      return !normalizedPrefix
        || path.toLowerCase().startsWith(normalizedPrefix)
        || reference.toLowerCase().startsWith(normalizedPrefix);
    })
    .slice(0, 100)
    .map((entry) => ({
      label: entry.path,
      type: fieldCompletionType(entry.kind),
      detail: entry.source === "alias" ? "alias" : String(entry.kind || "field"),
      info: fieldCompletionInfo(entry),
      apply: templatePathToReference(entry.path),
    }));

  return {
    from,
    options,
    validFor: /[\w.[\]]*/,
  };
}

function insertTemplateFieldReference(editorKey, path) {
  const editor = templateEditors.get(editorKey) || templateEditors.get("title");
  if (!editor) {
    return;
  }

  const reference = templatePathToReference(path);
  const selection = editor.view.state.selection.main;
  const delimiter = findOpenTemplateDelimiter(editor.view.state, selection.from);
  const insertion = delimiter === "{{" ? reference : `{{ ${reference} }}`;

  editor.view.dispatch({
    changes: { from: selection.from, to: selection.to, insert: insertion },
    selection: { anchor: selection.from + insertion.length },
  });
  editor.view.focus();
}

function renderTemplateFieldList(fields) {
  templateFieldListEl.innerHTML = "";
  const fragment = document.createDocumentFragment();

  for (const entry of fields) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "template-field-chip";
    button.textContent = entry.path;
    button.title = fieldCompletionInfo(entry) || String(entry.path);
    button.onclick = () => insertTemplateFieldReference(activeTemplateEditorKey, entry.path);
    fragment.appendChild(button);
  }

  templateFieldListEl.appendChild(fragment);
}

async function loadTemplateFieldData() {
  try {
    const payload = await api("/api/template-fields");
    templateFieldEntries = Array.isArray(payload?.fields) ? payload.fields : [];
    setTemplateFieldMeta(payload);
    renderTemplateFieldList(templateFieldEntries);
  } catch (error) {
    templateFieldEntries = [];
    templateSampleMetaEl.textContent = `Autocomplete unavailable: ${error.message}`;
    templateFieldListEl.innerHTML = "";
  }
}

function schedulePreviewSamples() {
  clearTimeout(previewSamplesTimer);
  ++previewSamplesRequestId;
  previewSamplesUsername = "";
  previewSamples = [];
  const fallback = makeEl("option", "", "Built-in example (illustrative data)");
  fallback.value = "";
  previewSampleSelect.replaceChildren(fallback);
  previewSampleSelect.value = "";
  previewSampleLink.hidden = true;
  scheduleTemplatePreview();
  if (!usernameInput.value.trim()) {
    previewSamplesStatus.textContent = "The built-in example is ready to use without a network connection.";
    return;
  }
  previewSamplesStatus.textContent = "Loading preview observations for this username...";
  previewSamplesTimer = setTimeout(() => { void loadPreviewSamples(); }, 700);
}

function renderPrivacyReport(report) {
  const mode = report.authenticated ? "Signed-in data" : "Public data";
  const text = `${mode}: ${report.total} observations checked. `
    + `${report.restricted_locations} have location privacy restrictions `
    + `(${report.obscured_locations} obscured, ${report.private_locations} private); `
    + `${report.missing_coordinates} have no usable coordinates. `
    + `${report.missing_datetime} have no usable capture timestamp, including `
    + `${report.skipped_photo_observations} photo observations that cannot enter time-based auto-matching. `
    + `Location restrictions plus missing timestamps: ${report.restricted_missing_datetime}. `
    + (report.authenticated ? `${report.private_locations_available} private locations are available to this account. `
      : "Signing in can reveal your own restricted locations. ")
    + "Missing dates or coordinates can also be genuinely absent; sign-in is not guaranteed to recover them. Counts overlap.";
  document.getElementById("privacy-report").textContent = text;
  document.getElementById("review-privacy-report").textContent = text;
}

function renderAuthStatus(status) {
  if ("credentials_saved" in status) authStorageStatus = status;
  authConnected = Boolean(status.connected);
  document.getElementById("auth-status").textContent = status.error
    ? `Sign-in did not complete: ${status.error}`
    : authConnected ? `Signed in as ${status.username}. Your own private locations will be used.`
      : status.available === false ? "Public-only mode. To sign in, open this app through localhost on the computer running it."
        : "Public-only mode. Sign-in is optional.";
  document.getElementById("oauth-callback").value = status.redirect_uri || "";
  document.getElementById("oauth-disconnect").hidden = !authConnected;
  document.getElementById("oauth-setup").hidden = authConnected || status.available === false;
  document.getElementById("oauth-disconnect").disabled = status.available === false;
  document.getElementById("pending-private-warning").hidden = !authConnected;
  const savedCredentials = Boolean(authStorageStatus.credentials_saved);
  document.getElementById("oauth-remember").checked = savedCredentials;
  document.getElementById("oauth-remember").disabled = authStorageStatus.credential_storage_supported === false;
  document.getElementById("oauth-forget").hidden = !savedCredentials && !authStorageStatus.credential_storage_error;
  document.getElementById("oauth-forget").disabled = status.available === false;
  if (savedCredentials) document.getElementById("oauth-client-id").value = authStorageStatus.client_id;
  document.getElementById("oauth-client-secret").placeholder = savedCredentials ? "Saved on this computer; leave blank to reuse" : "";
  document.getElementById("oauth-storage-status").textContent = authStorageStatus.credential_storage_error
    ? `Saved credentials could not be loaded: ${authStorageStatus.credential_storage_error}`
    : authStorageStatus.credential_storage_supported === false
      ? "Remembering credentials is unavailable on this platform. Use a password manager or enter them for each run."
      : savedCredentials
        ? `Application credentials remembered in ${authStorageStatus.credential_storage_path}. Access tokens are not saved.`
        : "Application credentials are not remembered.";
  if (authConnected && status.username) usernameInput.value = status.username;
}

async function loadAuthStatus() {
  renderAuthStatus(await api("/api/auth/status"));
}

document.getElementById("oauth-sign-in").addEventListener("click", async () => {
  if (anyTaskRunning()) return;
  authBusy = true;
  updateSidebarNavState();
  try {
    const result = await api("/api/auth/start", {
      method: "POST",
      body: JSON.stringify({
        client_id: document.getElementById("oauth-client-id").value.trim(),
        client_secret: document.getElementById("oauth-client-secret").value.trim(),
        remember_credentials: document.getElementById("oauth-remember").checked,
      }),
    });
    document.getElementById("oauth-client-secret").value = "";
    location.assign(result.url);
  } catch (error) {
    showNotice(`Could not start sign-in: ${error.message}`, true);
  } finally {
    authBusy = false;
    updateSidebarNavState();
  }
});

document.getElementById("oauth-forget").addEventListener("click", async () => {
  if (anyTaskRunning()) return;
  authBusy = true;
  updateSidebarNavState();
  try {
    await api("/api/auth/forget-credentials", { method: "POST" });
    document.getElementById("oauth-client-secret").value = "";
    document.getElementById("oauth-client-id").value = "";
    authStorageStatus = { ...authStorageStatus, credentials_saved: false, client_id: "", credential_storage_error: null };
    renderAuthStatus({ ...authStorageStatus, connected: authConnected, redirect_uri: document.getElementById("oauth-callback").value });
    showNotice("Saved application credentials were forgotten. Your current sign-in is unchanged; disconnect separately to return to public-only mode.");
  } catch (error) {
    showNotice(`Could not forget application credentials: ${error.message}`, true);
  } finally {
    authBusy = false;
    updateSidebarNavState();
  }
});

document.getElementById("oauth-disconnect").addEventListener("click", async () => {
  if (anyTaskRunning()) return;
  authBusy = true;
  updateSidebarNavState();
  try {
    await api("/api/auth/disconnect", { method: "POST" });
    hasAnalysis = false;
    analysisConfigSignature = null;
    previewAvailable = false;
    pendingPlanSignature = null;
    lastTree = null;
    treeEl.replaceChildren();
    setSummary(null);
    ++privacyRequestId;
    document.getElementById("privacy-report").textContent = "Public-only mode. Run the check or auto-match again for updated statistics.";
    document.getElementById("review-privacy-report").textContent = "";
    renderAuthStatus({ connected: false, redirect_uri: document.getElementById("oauth-callback").value });
    schedulePreviewSamples();
    showSettingsView();
    showNotice("Disconnected. Previous matches were cleared. Auto-match again to use only public data. To revoke this application's authorization completely, remove it in your iNaturalist account settings.");
  } catch (error) {
    showNotice(`Could not disconnect: ${error.message}`, true);
  } finally {
    authBusy = false;
    updateSidebarNavState();
  }
});

document.getElementById("check-privacy").addEventListener("click", async () => {
  const username = usernameInput.value.trim();
  if (!username) {
    showNotice("Enter an iNaturalist username before checking public data.", true);
    return;
  }
  const requestId = ++privacyRequestId;
  document.getElementById("check-privacy").disabled = true;
  document.getElementById("privacy-report").textContent = "Checking all public observations; this may take a while...";
  try {
    const report = await api(`/api/privacy-report?username=${encodeURIComponent(username)}`);
    if (requestId === privacyRequestId && usernameInput.value.trim() === username) renderPrivacyReport(report);
  } catch (error) {
    if (requestId === privacyRequestId) document.getElementById("privacy-report").textContent = `Could not check public data: ${error.message}`;
  } finally {
    if (requestId === privacyRequestId) document.getElementById("check-privacy").disabled = false;
  }
});

async function loadPreviewSamples() {
  clearTimeout(previewSamplesTimer);
  const username = usernameInput.value.trim();
  if (!username) {
    schedulePreviewSamples();
    return;
  }
  const requestId = ++previewSamplesRequestId;
  previewSamplesStatus.textContent = `Loading preview observations for ${username}...`;
  try {
    const payload = await api(`/api/template-samples?username=${encodeURIComponent(username)}`);
    if (requestId !== previewSamplesRequestId || username !== usernameInput.value.trim()) return;
    previewSamples = payload.samples || [];
    previewSamplesUsername = username;
    const fallback = makeEl("option", "", "Built-in example (illustrative data)");
    fallback.value = "";
    previewSampleSelect.replaceChildren(fallback, ...previewSamples.map((sample) => {
      const option = makeEl("option", "", `${sample.name} — ${sample.reasons.join(", ")} (${sample.comments_count} comments, ${sample.faves_count} favourites)`);
      option.value = String(sample.obs_id);
      return option;
    }));
    previewSampleSelect.value = previewSamples.length ? String(previewSamples[0].obs_id) : "";
    previewSamplesStatus.textContent = previewSamples.length
      ? `Loaded ${previewSamples.length} observation${previewSamples.length === 1 ? "" : "s"} for ${username}. Commented examples are selected from the latest 100; favourites are selected across the account. Duplicate observations appear once.`
      : `No public observations found for ${username}. Check the username, or use the built-in example.`;
    updatePreviewSample();
  } catch (error) {
    if (requestId !== previewSamplesRequestId) return;
    previewSamplesStatus.textContent = `Could not load your observations: ${error.message}. The built-in example is still available; use Load my preview observations to retry.`;
    previewSampleSelect.value = "";
    updatePreviewSample();
  }
}

function updatePreviewSample() {
  const sample = previewSamples.find((entry) => String(entry.obs_id) === previewSampleSelect.value);
  previewSampleLink.hidden = !sample;
  if (sample) previewSampleLink.href = `https://www.inaturalist.org/observations/${sample.obs_id}`;
  scheduleTemplatePreview();
}

async function browseFolder(path) {
  const requestId = ++folderRequestId;
  folderChooseBtn.disabled = true;
  folderParentBtn.disabled = true;
  selectedFolder = null;
  const list = document.getElementById("folder-list");
  const errorEl = document.getElementById("folder-error");
  list.replaceChildren();
  errorEl.textContent = "Loading folders...";
  try {
    const query = new URLSearchParams({ path, show_hidden: String(!ignoreHiddenFilesInput.checked) });
    const data = await api(`/api/folders?${query}`);
    if (requestId !== folderRequestId) return;
    selectedFolder = data.path;
    parentFolder = data.parent;
    folderLocationInput.value = data.path;
    folderParentBtn.disabled = !data.parent;
    folderChooseBtn.disabled = false;
    errorEl.textContent = data.folders.length ? "" : "No subfolders. You can use this folder.";
    list.replaceChildren(...data.folders.map((folder) => {
      const button = makeEl("button", "secondary", folder.name);
      button.type = "button";
      button.addEventListener("click", () => { void browseFolder(folder.path); });
      return button;
    }));
  } catch (error) {
    if (requestId === folderRequestId) errorEl.textContent = `Could not open folder: ${error.message}. Enter another path or use Home folder.`;
  }
}

usernameInput.addEventListener("input", () => {
  ++privacyRequestId;
  document.getElementById("check-privacy").disabled = false;
  document.getElementById("privacy-report").textContent = "Username changed. Check public data again for this account.";
  schedulePreviewSamples();
});
previewSampleSelect.addEventListener("change", updatePreviewSample);
document.getElementById("reload-preview-samples").addEventListener("click", () => { void loadPreviewSamples(); });
for (const spec of Object.values(TEMPLATE_EDITOR_SPECS)) {
  spec.textarea.addEventListener("input", scheduleTemplatePreview);
}
document.getElementById("browse-folder").addEventListener("click", () => {
  folderPicker.showModal();
  folderLocationInput.value = pathInput.value;
  void browseFolder(pathInput.value);
});
document.getElementById("folder-location-form").addEventListener("submit", (event) => {
  event.preventDefault();
  void browseFolder(folderLocationInput.value);
});
folderParentBtn.addEventListener("click", () => { if (parentFolder) void browseFolder(parentFolder); });
document.getElementById("folder-home").addEventListener("click", () => { void browseFolder(""); });
document.getElementById("folder-cancel").addEventListener("click", () => folderPicker.close());
folderPicker.addEventListener("close", () => { ++folderRequestId; });
folderPicker.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    folderPicker.close();
  }
});
folderLocationInput.addEventListener("input", () => {
  ++folderRequestId;
  selectedFolder = null;
  folderChooseBtn.disabled = true;
  document.getElementById("folder-error").textContent = "Press Go to open this path before selecting it.";
});
folderChooseBtn.addEventListener("click", () => {
  if (!selectedFolder) return;
  pathInput.value = selectedFolder;
  updateSidebarNavState();
  folderPicker.close();
});

function scheduleTemplatePreview() {
  ++templatePreviewRequestId;
  if (templatePreviewTimer) {
    clearTimeout(templatePreviewTimer);
  }
  templatePreviewTimer = setTimeout(() => {
    void refreshTemplatePreview();
  }, 200);
}

async function refreshTemplatePreview() {
  const requestId = ++templatePreviewRequestId;
  const payload = {
    title_template: getTemplateEditorValue("title"),
    caption_template: getTemplateEditorValue("caption"),
    username: previewSamplesUsername,
    sample_obs_id: previewSampleSelect.value ? Number(previewSampleSelect.value) : null,
  };

  try {
    const preview = await api("/api/template-preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (requestId !== templatePreviewRequestId) {
      return;
    }
    setPreviewState(imageTitlePreviewEl, preview.title || " ");
    setPreviewState(imageCaptionPreviewEl, preview.caption || " ");
    if (preview.sample_obs_id) {
      const existing = templateSampleMetaEl.textContent ? `${templateSampleMetaEl.textContent} | ` : "";
      if (!templateSampleMetaEl.textContent.includes(`obs:${preview.sample_obs_id}`)) {
        templateSampleMetaEl.textContent = `${existing}Preview sample obs:${preview.sample_obs_id}`;
      }
    }
  } catch (error) {
    if (requestId !== templatePreviewRequestId) {
      return;
    }
    const message = error.message || "Preview unavailable";
    setPreviewState(imageTitlePreviewEl, message, { isError: true });
    setPreviewState(imageCaptionPreviewEl, message, { isError: true });
  }
}

async function initializeTemplateEditors() {
  if (templateEditors.size > 0) {
    await loadTemplateFieldData();
    scheduleTemplatePreview();
    return;
  }

  try {
    const {
      EditorState,
      EditorView,
      keymap,
      basicSetup,
      autocompletion,
      startCompletion,
      jinja,
    } = await loadCodeMirrorModules();

    Object.values(TEMPLATE_EDITOR_SPECS).forEach((spec) => {
      const extensions = [
        basicSetup,
        jinja(),
        EditorView.lineWrapping,
        EditorView.contentAttributes.of({ "aria-label": `Image ${spec.label} template` }),
        autocompletion({ override: [buildTemplateFieldCompletions] }),
        keymap.of([
          { key: "Ctrl-Space", run: startCompletion },
          { key: "Cmd-Space", run: startCompletion },
        ]),
        EditorView.updateListener.of((update) => {
          if (update.docChanged) {
            spec.textarea.value = update.state.doc.toString();
            scheduleTemplatePreview();
            updateSidebarNavState();
          }
          if (update.focusChanged && update.view.hasFocus) {
            activeTemplateEditorKey = spec.key;
          }
        }),
      ];

      const view = new EditorView({
        state: EditorState.create({
          doc: spec.textarea.value,
          extensions,
        }),
        parent: spec.shell,
      });

      spec.textarea.classList.add("is-enhanced");
      spec.shell.hidden = false;
      templateEditors.set(spec.key, { ...spec, view });
    });

    await loadTemplateFieldData();
    scheduleTemplatePreview();
  } catch (error) {
    console.error("Failed to initialize template editors", error);
    templateSampleMetaEl.textContent = `Rich editor unavailable: ${error.message}`;
  }
}

/** Fetch the latest config from the server, reading fresh from disk. */
async function getConfig() {
  return api("/api/config");
}

/**
 * Replace the server-side config with the supplied object and return the saved value.
 * Always GET the current config first, modify it, then call updateConfig with the full object.
 */
async function updateConfig(config) {
  return api("/api/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
}

function applyConfigToInputs(config) {
  usernameInput.value = String(config?.username || "");
  pathInput.value = String(config?.path || "");
  ignoreHiddenFilesInput.checked = config?.ignore_hidden_files !== false;
  writeHierarchicalTagsInput.checked = config?.write_hierarchical_tags !== false;
  taxonomyTagPrefixInput.value = config?.taxonomy_tag_prefix ?? "taxonomy";
  for (const [key, input] of metadataToggleInputs) input.checked = config?.[key] !== false;
  setTemplateEditorValue("title", String(config?.image_title_template || ""));
  setTemplateEditorValue("caption", String(config?.image_caption_template || ""));
  savedConfigSignature = configSignature();
  scheduleTemplatePreview();
  updateSidebarNavState();
}

function setSummary(summary) {
  if (!summary) {
    summaryEl.textContent = "No auto-match results loaded.";
    return;
  }
  summaryEl.textContent = `Total ${summary.total} | Matched ${summary.matched} | Needs attention ${summary.needs_attention}`;
  if (summary.privacy) renderPrivacyReport(summary.privacy);
  document.getElementById("review-summary").textContent = summary.total === 0
    ? "No observations with usable capture timestamps were found. Check the data statistics below, username and photo folder before trying again."
    : `${summary.total} observation${summary.total === 1 ? "" : "s"}: ${summary.matched} matched, ${summary.needs_attention} need attention. You do not have to match every observation to continue.`;
}

function statusListMarkup(container) {
  const labels = {
    actionable: { short: "act.", title: "actionable" },
    unmatchable: { short: "unm.", title: "unmatchable" },
    photoless: { short: "aud.", title: "audio only / no photos" },
    fuzzy: { short: "fz.", title: "fuzzy" },
    exact: { short: "ex.", title: "exact" },
    tagged: { short: "tg.", title: "tagged" },
  };

  return Object.entries(labels)
    .filter(([key]) => Number(container.status_counts?.[key] || 0) > 0)
    .map(
      ([key, label]) =>
        `<span class="tree-stat tree-stat-${key}" title="${label.title}"><span class="tree-stat-count">${container.status_counts[key]}</span> ${label.short}</span>`,
    )
    .join("");
}

function buildTreeSummary(labelText, container) {
  const wrapper = document.createElement("div");
  wrapper.className = `tree-summary${container.actionable_count > 0 ? " is-actionable" : ""}${container.matchable_count === 0 ? " is-unmatchable-only" : ""}`;

  const label = document.createElement("span");
  label.className = "tree-summary-label";
  label.textContent = labelText;

  const counts = document.createElement("span");
  counts.className = "tree-summary-counts";
  counts.innerHTML = statusListMarkup(container);

  wrapper.append(label, counts);
  return wrapper;
}

function setAnalyzeButtonState(running) {
  analyzeRunning = running;
  updateSidebarNavState();
}

function renderLocalProgress({ phase, message, current, total, events = [], running = false }) {
  const safePhase = String(phase || "task");
  const safeMessage = String(message || "Working");
  const safeCurrent = Number(current || 0);
  const safeTotal = Number(total || 0);
  const pct = safeTotal > 0 ? Math.min(100, Math.round((safeCurrent / safeTotal) * 100)) : 0;

  progressHead.textContent = `${safePhase.toUpperCase()} - ${safeMessage}`;
  progressBar.style.width = `${pct}%`;
  const progressWrap = document.getElementById("progress-bar-wrap");
  progressWrap.classList.toggle("is-indeterminate", running && safeTotal === 0);
  if (running && safeTotal === 0) progressWrap.removeAttribute("aria-valuenow");
  else progressWrap.setAttribute("aria-valuenow", String(pct));
  progressDetail.textContent = safeTotal > 0
    ? `Progress: ${safeCurrent}/${safeTotal} (${pct}%)`
    : running ? "Working. Per-file progress is not available; results will appear when saving finishes." : "Task finished.";

  progressLog.replaceChildren(...events.slice(-40).map((entry) =>
    makeEl("div", "", `[${entry.time}] ${entry.phase}: ${entry.message}`)));
  progressLog.scrollTop = progressLog.scrollHeight;
}

function renderStatus(status) {
  setAnalyzeButtonState(Boolean(status?.running));
  updateSidebarNavState();

  if (status?.running) {
    beginTaskProgress();
  }

  const displayMessage = (value) => String(value).replace(/\banalysis\b/gi, "auto-match").replace(/\banalyzing\b/gi, "auto-matching").replace(/\banalyze\b/gi, "auto-match");
  const phase = displayMessage(status?.phase || "idle");
  const message = displayMessage(status?.message || "Idle");
  progressHead.textContent = `${phase.toUpperCase()} - ${message}`;

  const current = Number(status?.current || 0);
  const total = Number(status?.total || 0);
  let pct = 0;
  if (total > 0) {
    pct = Math.min(100, Math.round((current / total) * 100));
  } else if (status?.running) {
    pct = 20;
  }
  progressBar.style.width = `${pct}%`;
  const progressWrap = document.getElementById("progress-bar-wrap");
  progressWrap.classList.toggle("is-indeterminate", Boolean(status?.running) && total === 0);
  if (status?.running && total === 0) progressWrap.removeAttribute("aria-valuenow");
  else progressWrap.setAttribute("aria-valuenow", String(pct));
  progressDetail.textContent = total > 0 ? `Progress: ${current}/${total} (${pct}%)` : "Progress: working";

  const events = status?.events || [];
  progressLog.replaceChildren(...events.slice(-40).map((entry) =>
    makeEl("div", "", `[${entry.time}] ${displayMessage(entry.phase)}: ${displayMessage(entry.message)}`)));
  progressLog.scrollTop = progressLog.scrollHeight;
}

async function pollStatus() {
  if (pollingStatus) return;
  pollingStatus = true;
  try {
    const status = await api("/api/session/status");
    renderStatus(status);

    if (!status.running && status.finished) {
      if (status.summary && !status.error && !status.cancelled) {
        hasAnalysis = true;
        if (analysisConfigSignature === null) analysisConfigSignature = configSignature();
        setSummary(status.summary);
        await loadTree();
        await initializeSaveTrackingFromServer();
        if (previewAvailable) showNotice("Auto-match complete. Check the selected photos, then continue to review the pending metadata changes.");
        await openMatchReview();
      }
      if (status.error) {
        hasAnalysis = false;
        showNotice(`Auto-match failed: ${status.error}. Check your configuration and try again.`, true);
        showSettingsView();
      }
      if (status.cancelled) {
        hasAnalysis = false;
        showNotice("Auto-match cancelled. No image files were changed. You can adjust the configuration and try again.");
        showSettingsView();
      }
      stopStatusPolling();
    }
  } finally {
    pollingStatus = false;
    updateSidebarNavState();
  }
}

function startStatusPolling() {
  stopStatusPolling();
  statusTimer = setInterval(async () => {
    try {
      await pollStatus();
    } catch (error) {
      showNotice(`Could not update auto-match progress: ${error.message}. Reconnecting automatically...`, true);
    }
  }, 600);
}

function stopStatusPolling() {
  if (statusTimer) {
    clearInterval(statusTimer);
    statusTimer = null;
  }
}

function renderTree(tree) {
  lastTree = tree;
  treeEl.innerHTML = "";
  setSummary(tree);

  // Reverse years to show most recent first
  const yearsReversed = [...tree.years].reverse();
  let isFirstMonth = true;

  for (const year of yearsReversed) {
    const yearHasCurrentDay = Boolean(currentDay && currentDay.year === year.year);
    const yearEl = detailsBlock(buildTreeSummary(String(year.year), year));
    yearEl.classList.toggle("is-unmatchable-only", year.matchable_count === 0);
    yearEl.classList.toggle("is-actionable", year.actionable_count > 0);
    let yearShouldOpen = yearHasCurrentDay;

    // Reverse months within year to show most recent first
    const monthsReversed = [...year.months].reverse();

    for (const month of monthsReversed) {
      const monthHasCurrentDay = Boolean(currentDay && currentDay.year === year.year && currentDay.month === month.month);
      const monthEl = detailsBlock(buildTreeSummary(String(month.month).padStart(2, "0"), month));
      monthEl.classList.toggle("is-unmatchable-only", month.matchable_count === 0);
      monthEl.classList.toggle("is-actionable", month.actionable_count > 0);
      // Open if contains current day, or if this is the first (most recent) month
      const shouldOpenMonth = monthHasCurrentDay || isFirstMonth;
      monthEl.open = shouldOpenMonth;
      if (shouldOpenMonth) {
        yearShouldOpen = true;
      }
      isFirstMonth = false;
      const daysList = document.createElement("div");
      daysList.className = "obs-list";

      // Reverse days within month to show most recent first
      const daysReversed = [...month.days].reverse();

      for (const day of daysReversed) {
        const btn = document.createElement("button");
        btn.className = "day-item";
        const dayKey = `${year.year}-${month.month}-${day.day}`;
        if (currentDay && dayKey === `${currentDay.year}-${currentDay.month}-${currentDay.day}`) {
          btn.classList.add("active");
        }
        if (day.actionable_count > 0) {
          btn.classList.add("is-actionable");
        }
        if (day.matchable_count === 0) {
          btn.classList.add("is-unmatchable-only");
        }
        btn.appendChild(buildTreeSummary(String(day.day).padStart(2, "0"), day));
        btn.onclick = () => loadDay(year.year, month.month, day.day);
        daysList.appendChild(btn);
      }

      monthEl.appendChild(daysList);
      yearEl.appendChild(monthEl);
    }

    yearEl.open = yearShouldOpen;
    treeEl.appendChild(yearEl);
  }
}

function detailsBlock(summaryContent) {
  const details = document.createElement("details");
  details.className = "tree-group";
  details.open = false;
  const summary = document.createElement("summary");
  if (typeof summaryContent === "string") {
    summary.textContent = summaryContent;
  } else {
    summary.appendChild(summaryContent);
  }
  details.appendChild(summary);
  return details;
}

function statePriority(state) {
  const order = {
    ambiguous: 0,
    fuzzy: 1,
    "exact-untagged": 2,
    tagged: 3,
    "audio-only": 4,
    "no-photos": 4,
  };
  return order[state] ?? 99;
}

function chronologicalRows(rows) {
  const copied = [...rows];
  copied.sort((left, right) => left.time.localeCompare(right.time));
  return copied;
}

function statusSortedRows(rows, direction) {
  const copied = [...rows];
  copied.sort((left, right) => {
    const leftPriority = statePriority(left.state);
    const rightPriority = statePriority(right.state);
    if (leftPriority !== rightPriority) {
      return direction === "asc"
        ? leftPriority - rightPriority
        : rightPriority - leftPriority;
    }
    return left.time.localeCompare(right.time);
  });
  return copied;
}

function applyRowOrder(rows) {
  if (!currentRowOrder.length) {
    if (currentSort.column === "observation") {
      const ordered = chronologicalRows(rows);
      if (currentSort.direction === "desc") {
        ordered.reverse();
      }
      return ordered;
    }
    return statusSortedRows(rows, currentSort.direction || "asc");
  }

  const byObsId = new Map(rows.map((row) => [row.obs_id, row]));
  const ordered = [];

  for (const obsId of currentRowOrder) {
    const row = byObsId.get(obsId);
    if (row) {
      ordered.push(row);
      byObsId.delete(obsId);
    }
  }

  for (const row of rows) {
    if (byObsId.has(row.obs_id)) {
      ordered.push(row);
      byObsId.delete(row.obs_id);
    }
  }

  return ordered;
}

function updateRowOrder(column) {
  if (column === "observation") {
    currentSort = {
      column,
      direction: currentSort.column === column && currentSort.direction === "asc" ? "desc" : "asc",
    };
    const ordered = chronologicalRows(currentDayRows);
    if (currentSort.direction === "desc") {
      ordered.reverse();
    }
    currentRowOrder = ordered.map((row) => row.obs_id);
    return;
  }

  if (column === "status") {
    currentSort = {
      column,
      direction: currentSort.column === column && currentSort.direction === "asc" ? "desc" : "asc",
    };
    const ordered = statusSortedRows(currentDayRows, currentSort.direction);
    currentRowOrder = ordered.map((row) => row.obs_id);
  }
}

const PHOTOLESS_STATES = new Set(["audio-only", "no-photos"]);
const STATE_TITLES = {
  "audio-only": "Audio-only observation: it has no photos, so there's nothing to match",
  "no-photos": "This observation has no photos, so there's nothing to match",
};

let librarySearchStatus = null;
let librarySearchTimer = null;

function canSearchLibrary(row) {
  return (
    row.observation_photos.length > 0 &&
    !PHOTOLESS_STATES.has(row.state) &&
    row.state !== "tagged" &&
    !row.candidates.some((candidate) => candidate.selected)
  );
}

function libraryPanelFor(obsId, create) {
  const tr = dayTableWrapEl.querySelector(`tr[data-obs-id="${obsId}"]`);
  if (!tr) {
    return null;
  }
  let panel = tr.querySelector(".library-search-panel");
  if (!panel && create) {
    panel = document.createElement("div");
    panel.className = "library-search-panel";
    panel.innerHTML = `
      <div class="ls-header">
        <strong>Whole-library search</strong>
        <span class="ls-actions"></span>
      </div>
      <div class="ls-message status-text"></div>
      <progress class="ls-progress" max="1" value="0"></progress>
      <div class="ls-candidates candidate-strip"></div>`;
    panel.cardsById = new Map();
    tr.querySelector("td:last-child").appendChild(panel);
  }
  return panel;
}

async function startLibrarySearch(obsId) {
  if (librarySearchStatus?.running && librarySearchStatus.obs_id !== obsId) {
    if (!confirm(`A library search for obs:${librarySearchStatus.obs_id} is still running. Stop it and search for obs:${obsId} instead?`)) {
      return;
    }
  }
  try {
    const status = await api(`/api/observation/${obsId}/library-search`, { method: "POST" });
    if (librarySearchStatus && librarySearchStatus.obs_id !== obsId) {
      libraryPanelFor(librarySearchStatus.obs_id, false)?.remove();
    }
    librarySearchStatus = status;
    updateLibrarySearchPanel(status);
    scheduleLibrarySearchPoll();
  } catch (error) {
    alert(`Couldn't start the library search: ${error.message}`);
  }
}

function scheduleLibrarySearchPoll() {
  clearTimeout(librarySearchTimer);
  librarySearchTimer = setTimeout(pollLibrarySearch, 1000);
}

async function pollLibrarySearch() {
  librarySearchTimer = null;
  try {
    librarySearchStatus = await api("/api/library-search");
  } catch (error) {
    console.error("Library search poll failed", error);
    scheduleLibrarySearchPoll();
    return;
  }
  updateLibrarySearchPanel(librarySearchStatus);
  if (librarySearchStatus.running) {
    scheduleLibrarySearchPoll();
  }
}

async function stopLibrarySearch() {
  try {
    librarySearchStatus = await api("/api/library-search/cancel", { method: "POST" });
  } catch (error) {
    alert(`Couldn't stop the search: ${error.message}`);
  }
  scheduleLibrarySearchPoll();
}

function closeLibrarySearchPanel(obsId) {
  libraryPanelFor(obsId, false)?.remove();
  if (librarySearchStatus?.obs_id === obsId && !librarySearchStatus.running) {
    librarySearchStatus = null;
  }
}

async function acceptLibrarySearchCandidate(obsId, candidate) {
  try {
    await api(`/api/observation/${obsId}/library-search/accept`, {
      method: "POST",
      body: JSON.stringify({ photo_id: candidate.photo_id }),
    });
  } catch (error) {
    alert(`Couldn't use that photo: ${error.message}`);
    return;
  }
  clearTimeout(librarySearchTimer);
  librarySearchTimer = null;
  librarySearchStatus = null;
  await refreshCurrentDay();
}

function libraryCandidateCard(obsId, candidate) {
  const card = document.createElement("div");
  card.className = "candidate-card library-candidate";
  const img = document.createElement("img");
  img.src = candidate.url;
  img.loading = "lazy";
  const meta = document.createElement("div");
  meta.className = "candidate-meta";
  const useBtn = document.createElement("button");
  useBtn.type = "button";
  useBtn.textContent = "Use this photo";
  useBtn.onclick = () => acceptLibrarySearchCandidate(obsId, card.candidate);
  card.append(img, meta, useBtn);
  return card;
}

function fillLibraryCandidateMeta(card, candidate) {
  card.candidate = candidate;
  const meta = card.querySelector(".candidate-meta");
  meta.innerHTML = "";
  meta.appendChild(makeEl("div", "", candidate.name));
  meta.appendChild(makeEl("div", "status-text", `score=${candidate.score.toFixed(3)}`));
  meta.appendChild(makeEl("div", "status-text", `${candidate.taken || "no date"} (${candidate.offset})`));
  const pathEl = makeEl("div", "status-text ls-path", candidate.path);
  pathEl.title = candidate.path;
  meta.appendChild(pathEl);
}

function updateLibrarySearchPanel(status) {
  if (status.obs_id == null) {
    return;
  }
  const panel = libraryPanelFor(status.obs_id, true);
  if (!panel) {
    return;
  }
  panel.classList.toggle("is-running", Boolean(status.running));
  panel.classList.toggle("has-error", Boolean(status.error));
  panel.querySelector(".ls-message").textContent = status.message || "";
  const progress = panel.querySelector(".ls-progress");
  if (status.total > 0) {
    progress.max = status.total;
    progress.value = status.current;
  } else {
    progress.removeAttribute("value");
  }
  progress.hidden = !status.running;

  const actions = panel.querySelector(".ls-actions");
  actions.innerHTML = "";
  const actionBtn = document.createElement("button");
  actionBtn.type = "button";
  actionBtn.className = "secondary";
  if (status.running) {
    actionBtn.textContent = "Stop";
    actionBtn.onclick = stopLibrarySearch;
  } else {
    actionBtn.textContent = "Close";
    actionBtn.onclick = () => closeLibrarySearchPanel(status.obs_id);
  }
  actions.appendChild(actionBtn);

  // Reuse cards across polls so thumbnails don't reload and flicker.
  const strip = panel.querySelector(".ls-candidates");
  const seen = new Set();
  for (const candidate of status.candidates) {
    let card = panel.cardsById.get(candidate.photo_id);
    if (!card) {
      card = libraryCandidateCard(status.obs_id, candidate);
      panel.cardsById.set(candidate.photo_id, card);
    }
    fillLibraryCandidateMeta(card, candidate);
    strip.appendChild(card);
    seen.add(candidate.photo_id);
  }
  for (const [photoId, card] of panel.cardsById) {
    if (!seen.has(photoId)) {
      card.remove();
      panel.cardsById.delete(photoId);
    }
  }
}

function renderDay(data) {
  currentDay = { year: data.year, month: data.month, day: data.day };
  currentDayRows = data.rows;
  syncSelectionMapsFromRows(data.rows);
  if (!currentRowOrder.length) {
    currentRowOrder = statusSortedRows(data.rows, currentSort.direction || "asc").map((row) => row.obs_id);
  }
  dayMetaEl.innerHTML = `<strong>${data.year}-${String(data.month).padStart(2, "0")}-${String(data.day).padStart(2, "0")}</strong> | observations: ${data.observation_count}`;

  if (data.rows.length === 0) {
    dayTableWrapEl.textContent = "No observations for this day.";
    return;
  }

  const table = document.createElement("table");
  table.className = "day-table";
  table.innerHTML = "<thead><tr><th><button type=\"button\" class=\"sort-button\" id=\"observation-sort\">Observation</button></th><th><button type=\"button\" class=\"sort-button\" id=\"status-sort\">Status</button></th><th>Reference image(s)</th><th>Candidate local image(s)</th></tr></thead>";

  const tbody = document.createElement("tbody");
  for (const row of applyRowOrder(data.rows)) {
    const tr = document.createElement("tr");
    tr.className = `state-${row.state}`;
    tr.dataset.obsId = String(row.obs_id);

    const tdObs = document.createElement("td");
    tdObs.innerHTML = `<div><a href="${row.uri}" target="_blank" rel="noreferrer">obs:${row.obs_id}</a> at ${row.time}</div>`;

    const tdStatus = document.createElement("td");
    const stateTitle = STATE_TITLES[row.state] || "";
    tdStatus.innerHTML = `<div><span class="obs-state state-${row.state}" title="${escapeHtml(stateTitle)}">${row.state}</span></div>${row.status !== row.state ? `<div class="status-text">${escapeHtml(row.status)}</div>` : ""}`;
    if (row.detail) {
      tdStatus.appendChild(makeEl("div", "status-detail", row.detail));
    }

    const tdRef = document.createElement("td");
    const refStrip = document.createElement("div");
    refStrip.className = "image-strip";
    for (const photo of row.observation_photos) {
      const img = document.createElement("img");
      img.src = `${photo.url}?t=${Date.now()}`;
      img.loading = "lazy";
      refStrip.appendChild(img);
    }
    tdRef.appendChild(refStrip);

    const tdCandidates = document.createElement("td");
    const candidateStrip = document.createElement("div");
    candidateStrip.className = "candidate-strip";

    for (const candidate of row.candidates) {
      const card = document.createElement("div");
      card.className = "candidate-card";
      if (candidate.selected) {
        card.classList.add("selected");
      }
      card.dataset.photoId = String(candidate.photo_id);
      
      // Check if this photo is selected in other observations
      const photoId = Number(candidate.photo_id);
      const conflictingObsIds = Array.from(getPhotosInConflict(photoId)).filter(id => id !== row.obs_id);
      if (conflictingObsIds.length > 0 && !candidate.selected) {
        card.classList.add("has-conflict");
        card.setAttribute("title", `Already selected in obs:${conflictingObsIds.join(", obs:")}`);  
      }
      card.tabIndex = 0;

      const img = document.createElement("img");
      img.src = `${candidate.url}?t=${Date.now()}`;
      img.loading = "lazy";

      const meta = document.createElement("div");
      meta.className = "candidate-meta";
      meta.innerHTML = `<div>${candidate.name}</div><div class="status-text">score=${candidate.score.toFixed(3)}</div><div class="status-text">${candidate.path}</div>`;

      const selectionText = document.createElement("div");
      selectionText.className = "status-text";
      selectionText.textContent = candidate.selected ? "Selected" : "Not selected";
      meta.appendChild(selectionText);
      
      // Add dirty status indicator with metadata mismatches
      if (candidate.dirty || (candidate.metadata_diffs && candidate.metadata_diffs.length > 0)) {
        const dirtyIndicator = document.createElement("details");
        dirtyIndicator.className = "candidate-dirty-details";
        const summary = document.createElement("summary");
        summary.className = "candidate-dirty-summary";
        summary.textContent = `⚠️ Re-tagging needed`;
        dirtyIndicator.appendChild(summary);

        const diffs = candidate.metadata_diffs || [];
        if (diffs.length > 0) {
          const details = document.createElement("div");
          details.className = "candidate-dirty-mismatches";
          for (const entry of diffs) {
            if (diffs.length > 1) {
              details.appendChild(makeEl("div", "pc-files", entry.names.join(", ")));
            }
            details.appendChild(renderPendingDiff(entry.diff));
          }
          dirtyIndicator.appendChild(details);
        }
        meta.appendChild(dirtyIndicator);
      }

      const toggleSelection = async () => {
        const photoId = Number(card.dataset.photoId);
        const isSelecting = !card.classList.contains("selected");
        
        if (isSelecting) {
          const conflictingObsIds = getPhotosInConflict(photoId);
          if (conflictingObsIds.size > 0) {
            const confirmed = await showConflictConfirmation(candidate.name, row.obs_id, conflictingObsIds);
            if (!confirmed) {
              return;
            }
            // Deselect from conflicting observations (including all version siblings)
            for (const conflictObsId of conflictingObsIds) {
              const currentFor = currentSelectionByObs.get(conflictObsId) || new Set();
              // Remove all photo IDs in this version group from the conflict observation
              for (const pid of candidate.photo_ids) {
                currentFor.delete(pid);
              }
              currentSelectionByObs.set(conflictObsId, currentFor);
              await api(`/api/observation/${conflictObsId}/selection`, {
                method: "POST",
                body: JSON.stringify({ selected_photo_ids: Array.from(currentFor) }),
              });
            }
          }
        }
        
        card.classList.toggle("selected");
        selectionText.textContent = card.classList.contains("selected") ? "Selected" : "Not selected";
        await saveRowSelection(tr, row.obs_id);
      };

      card.onclick = async () => {
        await toggleSelection();
      };
      card.onkeydown = async (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          await toggleSelection();
        }
      };

      card.append(img, meta);
      candidateStrip.appendChild(card);
    }

    tr.append(tdObs, tdStatus, tdRef, tdCandidates);
    if (PHOTOLESS_STATES.has(row.state)) {
      tdCandidates.appendChild(makeEl("div", "photoless-note", STATE_TITLES[row.state]));
    }
    tdCandidates.appendChild(candidateStrip);
    if (canSearchLibrary(row)) {
      const searchBtn = document.createElement("button");
      searchBtn.type = "button";
      searchBtn.className = "secondary library-search-start";
      searchBtn.textContent = "Search whole library";
      searchBtn.title = "Compare this observation's photos with every photo in your library, most likely first";
      searchBtn.onclick = () => startLibrarySearch(row.obs_id);
      tdCandidates.appendChild(searchBtn);
    }
    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  dayTableWrapEl.innerHTML = "";
  dayTableWrapEl.appendChild(table);
  if (librarySearchStatus && librarySearchStatus.obs_id != null) {
    updateLibrarySearchPanel(librarySearchStatus);
  }

  const observationSortButton = document.getElementById("observation-sort");
  if (observationSortButton) {
    const observationArrow = currentSort.column === "observation" ? (currentSort.direction === "asc" ? " ↑" : " ↓") : "";
    observationSortButton.textContent = `Observation${observationArrow}`;
    observationSortButton.onclick = () => {
      updateRowOrder("observation");
      renderDay({ ...data, rows: currentDayRows });
    };
  }

  const sortButton = document.getElementById("status-sort");
  if (sortButton) {
    const statusArrow = currentSort.column === "status" ? (currentSort.direction === "asc" ? " ↑" : " ↓") : "";
    sortButton.textContent = `Status${statusArrow}`;
    sortButton.onclick = () => {
      updateRowOrder("status");
      renderDay({ ...data, rows: currentDayRows });
    };
  }
}

async function loadTree() {
  try {
    const tree = await api("/api/tree");
    renderTree(tree);
    return tree;
  } catch (error) {
    if (error.message.includes("No analysis loaded. Start a session first.")) {
      treeEl.innerHTML = "";
      setSummary(null);
      return null;
    }
    throw error;
  }
}

async function loadConfigIntoForm() {
  const [config, status] = await Promise.all([getConfig(), api("/api/config/status")]);
  applyConfigToInputs(config);
  promoteConfigActions(status.ready);
  schedulePreviewSamples();
  return config;
}

function promoteConfigActions(ready) {
  configActions.classList.toggle("is-promoted", Boolean(ready));
  if (ready) configForm.insertBefore(configActions, configForm.firstElementChild);
}

async function saveConfigFromForm() {
  const current = await getConfig();
  const next = {
    ...current,
    ...formConfig(),
  };
  const saved = await updateConfig(next);
  applyConfigToInputs(saved);
  promoteConfigActions(Boolean(saved.username?.trim() && saved.path?.trim()));
  return saved;
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes) || 0;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(value >= 100 ? 0 : 1)} ${units[unit]}`;
}

function renderCacheInfo(info) {
  cacheDirEl.textContent = info.cache_dir ? `Location: ${info.cache_dir}` : "";
  cacheTotalEl.textContent = formatBytes(info.total_bytes);
  clearAllCachesBtn.disabled = !info.total_bytes;
  cacheTableBody.replaceChildren(
    ...(info.caches || []).map((cache) => {
      const row = document.createElement("tr");
      const nameCell = document.createElement("td");
      const label = document.createElement("strong");
      label.textContent = cache.label;
      const description = document.createElement("span");
      description.className = "cache-description";
      description.textContent = cache.description;
      nameCell.append(label, description);

      const sizeCell = document.createElement("td");
      sizeCell.className = "cache-size";
      sizeCell.textContent = formatBytes(cache.size_bytes);

      const actionCell = document.createElement("td");
      const clearBtn = document.createElement("button");
      clearBtn.type = "button";
      clearBtn.className = "secondary";
      clearBtn.textContent = "Clear";
      clearBtn.disabled = !cache.size_bytes;
      clearBtn.addEventListener("click", () => clearCache(cache.name, cache.label));
      actionCell.append(clearBtn);

      row.append(nameCell, sizeCell, actionCell);
      return row;
    }),
  );
}

async function loadCacheInfo() {
  try {
    renderCacheInfo(await api("/api/cache"));
  } catch (error) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "status-text";
    cell.textContent = `Could not load cache sizes: ${error.message}`;
    row.append(cell);
    cacheTableBody.replaceChildren(row);
  }
}

async function clearCache(name, label) {
  const prompt = name === "all" ? "Clear all caches?" : `Clear the ${label.toLowerCase()} cache?`;
  if (!confirm(`${prompt} The data will be rebuilt the next time it is needed.`)) {
    return;
  }
  try {
    renderCacheInfo(await api(`/api/cache/${encodeURIComponent(name)}`, { method: "DELETE" }));
  } catch (error) {
    alert(`Failed to clear cache: ${error.message}`);
    await loadCacheInfo();
  }
}

clearAllCachesBtn.addEventListener("click", () => clearCache("all", "all"));

async function startAnalyzeFromConfig() {
  await api("/api/session/start", { method: "POST" });
  analysisConfigSignature = configSignature();
  saveOutcome = null;
  document.getElementById("save-failures").hidden = true;
  startingAnalysis = false;
  hasAnalysis = false;
  setAnalyzeButtonState(true);
  beginTaskProgress();
  resetSaveTracking();
  currentDay = null;
  currentDayRows = [];
  currentRowOrder = [];
  currentSort = { column: "status", direction: "asc" };
  dayMetaEl.textContent = "Select a day from the sidebar.";
  dayTableWrapEl.innerHTML = "";
  startStatusPolling();
  await pollStatus();
}

async function openMatchReview() {
  if (!hasAnalysis || needsNewAnalysis() || anyTaskRunning()) return;
  if (currentDay) {
    await loadDay(currentDay.year, currentDay.month, currentDay.day);
    return;
  }
  const days = (lastTree?.years || []).slice().reverse().flatMap((year) =>
    year.months.slice().reverse().flatMap((month) =>
      month.days.slice().reverse().map((day) => ({ year: year.year, month: month.month, ...day }))));
  const first = days.find((day) => day.actionable_count > 0) || days[0];
  if (first) await loadDay(first.year, first.month, first.day);
  else {
    showAnalysisView();
    dayMetaEl.textContent = "No observations to review. Return to configuration to check your settings.";
  }
}

async function loadDay(year, month, day) {
  const isSameDay = currentDay && currentDay.year === year && currentDay.month === month && currentDay.day === day;
  const data = await api(`/api/day/${year}/${month}/${day}`);
  if (!isSameDay) {
    currentRowOrder = [];
    currentSort = { column: "status", direction: "asc" };
  }
  showAnalysisView();
  renderDay(data);
  await loadTree();
}

async function saveRowSelection(rowElement, obsId) {
  const selected = [];
  const cards = rowElement.querySelectorAll(".candidate-card[data-photo-id]");
  cards.forEach((card) => {
    if (card.classList.contains("selected")) {
      selected.push(Number(card.dataset.photoId));
    }
  });

  await api(`/api/observation/${obsId}/selection`, {
    method: "POST",
    body: JSON.stringify({ selected_photo_ids: selected }),
  });
  await refreshCurrentDay();
}

async function refreshCurrentDay() {
  await loadTree();
  if (currentDay) {
    const data = await api(`/api/day/${currentDay.year}/${currentDay.month}/${currentDay.day}`);
    renderDay(data);
  }
}

configForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (anyTaskRunning()) return;
  const analyze = event.submitter === analyzeBtn;
  if (analyze && hasAnalysis && latestSavePreview.total > 0
    && !confirm("Starting auto-match again replaces your current match selections. No pending changes will be written. Continue?")) return;
  startingAnalysis = true;
  updateSidebarNavState();
  showNotice("");
  try {
    await saveConfigFromForm();
    if (analyze) await startAnalyzeFromConfig();
    else showNotice("Settings saved. Choose Save settings and auto-match when you are ready to find matches.");
  } catch (error) {
    showNotice(`Could not ${analyze ? "start auto-match" : "save settings"}: ${error.message}`, true);
    if (analyze) {
      try {
        const status = await api("/api/session/status");
        if (status.running) {
          startingAnalysis = false;
          hasAnalysis = false;
          analysisConfigSignature = configSignature();
          resetSaveTracking();
          setAnalyzeButtonState(true);
          beginTaskProgress();
          startStatusPolling();
        }
      } catch (statusError) {
        showNotice(`Could not start or reconnect to auto-match: ${error.message}. ${statusError.message}`, true);
      }
    }
  } finally {
    startingAnalysis = false;
    updateSidebarNavState();
  }
});

configForm.addEventListener("input", updateSidebarNavState);
settingsBtn.addEventListener("click", showSettingsView);

progressNavBtn.addEventListener("click", () => {
  if (!anyTaskRunning()) {
    return;
  }
  showProgressView();
});

pendingNavBtn.addEventListener("click", () => {
  showPendingView();
});

reviewNavBtn.addEventListener("click", () => {
  void openMatchReview().catch((error) => showNotice(`Could not load matches: ${error.message}`, true));
});
reviewNextBtn.addEventListener("click", showPendingView);
document.getElementById("pending-back").addEventListener("click", () => {
  void openMatchReview().catch((error) => showNotice(`Could not load matches: ${error.message}`, true));
});
pendingConfigureBtn.addEventListener("click", showSettingsView);
progressConfigureBtn.addEventListener("click", showSettingsView);
progressReviewBtn.addEventListener("click", showPendingView);
savingNavBtn.addEventListener("click", () => beginTaskProgress("save"));
cancelAnalysisBtn.addEventListener("click", async () => {
  cancelAnalysisBtn.disabled = true;
  try {
    await api("/api/session/cancel", { method: "POST" });
    showNotice("Cancellation requested. Waiting for the current auto-match operation to stop...");
  } catch (error) {
    showNotice(`Could not cancel auto-match: ${error.message}`, true);
  } finally {
    cancelAnalysisBtn.disabled = false;
  }
});

saveBtn.addEventListener("click", async () => {
  if (anyTaskRunning() || activeMainView !== "pending" || !pendingPlanSignature || needsNewAnalysis()) return;
  const reviewedPlan = pendingPlanSignature;
  preparingSave = true;
  updateSidebarNavState();
  const outstanding = await refreshSavePreview();
  if (!previewAvailable || JSON.stringify(latestSavePreview) !== reviewedPlan || outstanding === 0) {
    preparingSave = false;
    showNotice("The pending changes have been updated. Please review them before saving.");
    showPendingView();
    return;
  }
  preparingSave = false;
  saveRunning = true;
  saveOutcome = null;
  showNotice("");
  document.getElementById("save-failures").hidden = true;
  beginTaskProgress("save");
  renderLocalProgress({
    phase: "save",
    message: `Writing metadata to ${outstanding} files...`,
    running: true,
  });
  try {
    const result = await api("/api/save", { method: "POST" });
    saveOutcome = result;
    renderLocalProgress({
      phase: "save",
      message: `Saved ${result.saved} file${result.saved === 1 ? "" : "s"}${result.failed ? `; ${result.failed} failed` : ""}`,
      current: result.saved + result.failed,
      total: outstanding,
    });
    if (result.failed) {
      const failuresEl = document.getElementById("save-failures");
      failuresEl.replaceChildren(makeEl("p", "", `${result.failed} file${result.failed === 1 ? "" : "s"} could not be saved. Successful writes have been kept. Review the remaining changes to retry.`),
        ...(result.failures || []).map((failure) => makeEl("p", "", `${failure.path}: ${failure.error}`)));
      failuresEl.hidden = false;
      showNotice(`Saved ${result.saved} file${result.saved === 1 ? "" : "s"}; ${result.failed} failed. Review the save errors in step 5, then review remaining changes to retry.`, true);
    } else {
      showNotice(`Sync complete: saved changes to ${result.saved} file${result.saved === 1 ? "" : "s"}. You can close the app, or adjust the configuration and auto-match again (for example, to refine your title and description templates).`);
    }
    for (const [obsId, currentSet] of currentSelectionByObs.entries()) {
      baselineSelectionByObs.set(obsId, new Set(currentSet));
    }
    await refreshSavePreview();
    try {
      await loadTree();
    } catch (error) {
      showNotice(`Saved ${result.saved} file${result.saved === 1 ? "" : "s"}${result.failed ? `; ${result.failed} failed` : ""}, but could not refresh the match list: ${error.message}. Reload the app before continuing.`, true);
      hasAnalysis = false;
    }
  } catch (error) {
    saveOutcome = { error: error.message };
    renderLocalProgress({ phase: "save", message: "Save did not complete" });
    await refreshSavePreview();
    showNotice(`Save did not complete: ${error.message}. Some files may already have been written. ${previewAvailable
      ? "Review remaining changes before retrying."
      : "Pending changes could not be reloaded either; return to Review changes to try again."}`, true);
  } finally {
    saveRunning = false;
    beginTaskProgress("save");
    updateSidebarNavState();
    if (saveOutcome && !saveOutcome.error && !saveOutcome.failed) showSettingsView();
  }
});

const quitAppBtn = document.getElementById("quit-app");

async function loadAppInfo() {
  try {
    const info = await api("/api/app-info");
    quitAppBtn.hidden = !info.quit_available;
  } catch (error) {
    quitAppBtn.hidden = true;
  }
}

quitAppBtn.addEventListener("click", async () => {
  if (anyTaskRunning()) {
    showNotice("Cancel or finish the current task before quitting.", true);
    return;
  }
  if (hasAnalysis && latestSavePreview.total > 0
    && !confirm("Your reviewed matches have not been saved to your files and will be lost. Quit anyway?")) return;
  quitAppBtn.disabled = true;
  try {
    await api("/api/app/quit", { method: "POST" });
  } catch (error) {
    quitAppBtn.disabled = false;
    showNotice(`Could not quit: ${error.message}`, true);
    return;
  }
  stopStatusPolling();
  clearTimeout(librarySearchTimer);
  const stopped = makeEl("main", "app-stopped");
  stopped.append(
    makeEl("h1", null, "inat2exif has stopped"),
    makeEl("p", null, "You can close this tab. Start inat2exif again to continue working."),
  );
  document.body.replaceChildren(stopped);
});

(async () => {
  try {
    void loadAppInfo();
    showSettingsView();
    await loadConfigIntoForm();
    await loadAuthStatus();
    schedulePreviewSamples();
    void initializeTemplateEditors();
    await pollStatus();
    if (analyzeRunning) startStatusPolling();
  } catch (error) {
    console.error("Application initialization failed", error);
    setSummary(null);
    showNotice(`Could not load the application: ${error.message}. Reload to try again.`, true);
  }
})();
