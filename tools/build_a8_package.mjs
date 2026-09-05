import fs from "node:fs/promises";
import crypto from "node:crypto";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
function argument(name) {
  const index = args.indexOf(name);
  if (index < 0 || !args[index + 1]) throw new Error(`missing ${name}`);
  return args[index + 1];
}

const payload = JSON.parse(await fs.readFile(argument("--payload"), "utf8"));
const outputRoot = path.resolve(argument("--output-root"));
const artifactToolPath = argument("--artifact-tool");
const { SpreadsheetFile, Workbook } = await import(pathToFileURL(artifactToolPath).href);

const reviewHeaders = [
  "source_record_id", "Company", "source", "official_profile_url",
  "listed_legal_name", "listed_address", "listed_phone", "expected_website",
  "website_verified", "expected_email", "email_verified", "expected_phone",
  "phone_verified", "expected_publication", "identity_evidence_urls",
  "contact_evidence_urls", "observed_at", "evidence_content_sha256",
  "reviewer_pass_1", "reviewer_pass_2", "disagreement_reason", "label_status",
];

const queueRows = payload.diagnostic_rows.map((row) => [
  row.source_record_id,
  row.Company,
  row.source,
  row.official_profile_url,
  row.listed_legal_name,
  row.listed_address,
  row.listed_phone,
  "",
  "unknown",
  "",
  "unknown",
  "",
  "unknown",
  "unknown",
  [row.official_profile_url, row.listing_url].filter(Boolean).join("; "),
  "",
  "2026-09-01",
  "",
  "pending_manual_source_review",
  "pending_manual_source_review",
  "two independent source-review passes pending; labels remain unknown",
  "unknown",
]);

const manifest = {
  schema_version: 2,
  benchmark: "A8",
  status: "BLOCKED_PREFLIGHT",
  selection_algorithm: payload.selection_algorithm,
  input_sha256: payload.input_sha256,
  review_count: payload.review_count,
  diagnostic_count: queueRows.length,
  diagnostic_counts_by_source: { texhibition_2026: 48, zuchex_2026: 48 },
  review_source_record_ids: payload.review_source_record_ids,
  sets: [
    {
      role: "diagnostic_96_review_queue",
      expected: "diagnostic_96_review_queue.xlsx",
      actual: null,
      status: "selection_only_labels_pending",
    },
    {
      role: "independent_120",
      expected: null,
      actual: null,
      status: "blocked",
      blocker: payload.independent_research.blocker,
    },
    {
      role: "actual_diagnostic_96",
      expected: null,
      actual: null,
      status: "not_run",
      blocker: "R6 exact micromamba runtime is not installed on this host.",
    },
    {
      role: "actual_independent_120",
      expected: null,
      actual: null,
      status: "not_run",
      blocker: "Independent expected labels are blocked before actual execution.",
    },
  ],
  official_sources: {
    texhibition: "https://www.texhibitionist.com/en/exhibitors?v=1",
    zuchex: "https://www.zuchex.com/tr/ziyaretci/Katilimci-Listesi-2026.html",
    hometex: payload.independent_research.hometex_listing_url,
    ambiente: payload.independent_research.ambiente_search_url,
  },
  constraints: {
    paid_provider_calls: 0,
    live_application_run: false,
    prior_893_modified: false,
    source_record_id_join_only: true,
  },
};
const manifestText = JSON.stringify(manifest, null, 2) + "\n";
const manifestHash = crypto.createHash("sha256").update(manifestText).digest("hex");
const packageDir = path.join(outputRoot, `a8_benchmark_${manifestHash}`);
await fs.mkdir(path.join(packageDir, "evidence"), { recursive: true });
await fs.mkdir(path.join(packageDir, "reports"), { recursive: true });
await fs.mkdir(path.join(packageDir, "actual"), { recursive: true });
await fs.mkdir(path.join(packageDir, "quality"), { recursive: true });

await fs.writeFile(path.join(packageDir, "a8_manifest.json"), manifestText, "utf8");
await fs.writeFile(
  path.join(packageDir, "selection_manifest.json"),
  JSON.stringify({
    schema_version: 2,
    manifest_sha256: manifestHash,
    review_source_record_ids: payload.review_source_record_ids,
    diagnostic_rows: payload.diagnostic_rows,
    selection_counts: payload.selection_counts,
    selection_algorithm: payload.selection_algorithm,
  }, null, 2) + "\n",
  "utf8",
);
await fs.writeFile(
  path.join(packageDir, "evidence", "source_selection_evidence.jsonl"),
  payload.diagnostic_rows.map((row) => JSON.stringify({
    source_record_id: row.source_record_id,
    source: row.source,
    official_profile_url: row.official_profile_url,
    listing_url: row.listing_url,
    selection_group: row.selection_group,
    selection_score: row.selection_score,
    selection_status: row.selection_status,
    selection_reason: row.selection_reason,
    label_status: "unknown",
  })).join("\n") + "\n",
  "utf8",
);

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Source Review");
sheet.showGridLines = false;
sheet.getRangeByIndexes(0, 0, queueRows.length + 1, reviewHeaders.length).values = [reviewHeaders, ...queueRows];
sheet.getRange("A1:V1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
  borders: { preset: "outside", style: "medium", color: "#1F4E78" },
};
sheet.getRange(`A2:V${queueRows.length + 1}`).format.wrapText = true;
sheet.getRange(`V2:V${queueRows.length + 1}`).conditionalFormats.add("containsText", {
  text: "unknown",
  format: { fill: "#FFF2CC", font: { color: "#7F6000" } },
});
sheet.freezePanes.freezeRows(1);
sheet.getRange(`A1:V${queueRows.length + 1}`).format.autofitColumns();
for (const [column, width] of [["A", 34], ["B", 28], ["C", 20], ["D", 48], ["E", 30], ["F", 34], ["G", 18], ["H", 24], ["J", 24], ["L", 24], ["N", 18], ["O", 52], ["Q", 16], ["S", 28], ["T", 28], ["U", 54], ["V", 14]]) {
  sheet.getRange(`${column}:${column}`).format.columnWidth = width;
}
const preview = await workbook.render({ sheetName: "Source Review", autoCrop: "all", scale: 0.5, format: "png" });
await fs.writeFile(path.join(packageDir, "diagnostic_96_review_queue_preview.png"), new Uint8Array(await preview.arrayBuffer()));
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(path.join(packageDir, "diagnostic_96_review_queue.xlsx"));

const gateReport = {
  schema_version: 2,
  benchmark: "A8",
  manifest_sha256: manifestHash,
  release_gate: "BLOCKED",
  diagnostic: {
    review_ids_planned: payload.review_count,
    selected_rows: queueRows.length,
    selected_by_source: { texhibition_2026: 48, zuchex_2026: 48 },
    labels: "unknown_pending_manual_source_review",
  },
  independent: {
    required: 120,
    available: 0,
    hometex_official_listing_observed: true,
    ambiente_official_search_observed: true,
    blocker: payload.independent_research.blocker,
  },
  actual: {
    diagnostic_96: "not_run",
    independent_120: "not_run",
    provider_calls: 0,
    paid_calls: 0,
  },
  blockers: [
    "R6 exact micromamba Python 3.14.7 / SQLite 3.53.4 runtime unavailable.",
    "Independent Ambiente official directory did not expose stable public exhibitor IDs in the offline research surface.",
    "Two independent source-review passes and actual child runs remain pending; no release-quality labels are asserted.",
  ],
};
await fs.writeFile(path.join(packageDir, "reports", "a8_release_gate.json"), JSON.stringify(gateReport, null, 2) + "\n", "utf8");
await fs.writeFile(path.join(packageDir, "reports", "independent_120_blocker.json"), JSON.stringify({
  required: 120,
  available: 0,
  source: "Ambiente 2026 Exhibitors & Products",
  official_url: payload.independent_research.ambiente_search_url,
  blocker: payload.independent_research.blocker,
  action: "Resolve the official dynamic directory and stable official IDs; do not substitute another fair.",
}, null, 2) + "\n", "utf8");
await fs.writeFile(path.join(packageDir, "actual", "actual_runs.json"), JSON.stringify({
  status: "not_run",
  reason: "R6 runtime prerequisite unavailable; paid/live calls were not made.",
  provider_calls: 0,
  physical_http_requests: 0,
}, null, 2) + "\n", "utf8");
await fs.writeFile(path.join(packageDir, "quality", "quality_report.json"), JSON.stringify({
  status: "not_evaluated",
  reason: "Source labels are pending manual two-pass review and actual child outputs do not exist.",
  false_publication: null,
  published_unknown_identity: null,
}, null, 2) + "\n", "utf8");
console.log(JSON.stringify({ package_dir: packageDir, manifest_sha256: manifestHash, diagnostic_count: queueRows.length }));
