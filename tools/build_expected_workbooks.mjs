import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const args = process.argv.slice(2);
function argument(name) {
  const index = args.indexOf(name);
  if (index < 0 || !args[index + 1]) throw new Error(`missing ${name}`);
  return args[index + 1];
}

const artifactToolPath = argument("--artifact-tool");
const { SpreadsheetFile, Workbook } = await import(pathToFileURL(artifactToolPath).href);
const headers = [
  "source_record_id", "Company", "source", "official_profile_url",
  "listed_legal_name", "listed_address", "listed_phone", "expected_website",
  "website_verified", "expected_email", "email_verified", "expected_phone",
  "phone_verified", "expected_publication", "identity_evidence_urls",
  "contact_evidence_urls", "observed_at", "evidence_content_sha256",
  "reviewer_pass_1", "reviewer_pass_2", "disagreement_reason", "label_status",
];

async function loadJsonl(filePath) {
  const rows = [];
  for (const line of (await fs.readFile(filePath, "utf8")).split(/\r?\n/)) {
    if (line.trim()) rows.push(JSON.parse(line));
  }
  return rows;
}

function rowValues(row) {
  return headers.map((header) => row[header] ?? "");
}

async function writeWorkbook(rows, outputPath, previewPath) {
  const workbook = Workbook.create();
  const sheet = workbook.worksheets.add("Source Review");
  sheet.showGridLines = false;
  sheet.getRangeByIndexes(0, 0, rows.length + 1, headers.length).values = [headers, ...rows.map(rowValues)];
  sheet.getRange("A1:V1").format = {
    fill: "#1F4E78",
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
    borders: { preset: "outside", style: "medium", color: "#1F4E78" },
  };
  sheet.getRange(`A2:V${rows.length + 1}`).format.wrapText = true;
  sheet.getRange(`V2:V${rows.length + 1}`).conditionalFormats.add("containsText", {
    text: "frozen",
    format: { fill: "#E2F0D9", font: { color: "#375623" } },
  });
  sheet.freezePanes.freezeRows(1);
  for (const [column, width] of [["A", 40], ["B", 30], ["C", 20], ["D", 60], ["E", 32], ["F", 34], ["G", 18], ["H", 30], ["J", 30], ["L", 24], ["N", 22], ["O", 60], ["Q", 28], ["S", 22], ["T", 22], ["U", 45], ["V", 14]]) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
  const preview = await workbook.render({ sheetName: "Source Review", autoCrop: "all", scale: 0.45, format: "png" });
  await fs.writeFile(previewPath, new Uint8Array(await preview.arrayBuffer()));
  const xlsx = await SpreadsheetFile.exportXlsx(workbook);
  await xlsx.save(outputPath);
}

const independentRows = await loadJsonl(argument("--independent-evidence"));
if (independentRows.length !== 120 || new Set(independentRows.map((row) => row.source_record_id)).size !== 120) {
  throw new Error("independent expected workbook requires 120 unique rows");
}
const diagnosticRows = await loadJsonl(argument("--diagnostic-evidence"));
if (diagnosticRows.length !== 96 || new Set(diagnosticRows.map((row) => row.source_record_id)).size !== 96) {
  throw new Error("diagnostic expected workbook requires 96 unique rows");
}
const outputDir = path.resolve(argument("--output-dir"));
await fs.mkdir(outputDir, { recursive: true });
await writeWorkbook(
  diagnosticRows,
  path.join(outputDir, "diagnostic_96.xlsx"),
  path.join(outputDir, "diagnostic_96.preview.png"),
);
await writeWorkbook(
  independentRows,
  path.join(outputDir, "independent_120.xlsx"),
  path.join(outputDir, "independent_120.preview.png"),
);
console.log(JSON.stringify({ diagnostic_rows: diagnosticRows.length, independent_rows: independentRows.length, output_dir: outputDir }));
