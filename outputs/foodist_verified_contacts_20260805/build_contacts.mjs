import fs from "node:fs/promises";
import path from "node:path";
import { FileBlob, SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = process.cwd();
const outputDir = path.join(root, "outputs", "foodist_verified_contacts_20260805");
const evidencePaths = [
  "output/foodist_review_live_general_v3_20260805/evidence.jsonl",
  "output/foodist_review_live_general_v4_places_20260805/evidence.jsonl",
  "output/foodist_review_live_general_v6_clean_20260805/evidence.jsonl",
  "output/foodist_review_live_general_v7_scaled_20260805/evidence.jsonl",
  "output/foodist_arch_iter1_identity_seed_20260805/evidence.jsonl",
  "output/foodist_arch_iter6_final_20260805/evidence.jsonl",
];

const configText = await fs.readFile(path.join(root, "config.py"), "utf8");
const excludedBlock = configText.match(/(?:^|\n)EXCLUDED_DOMAINS\s*=\s*\[([\s\S]*?)\n\]/)?.[1] ?? "";
const excludedDomains = new Set(
  [...excludedBlock.matchAll(/"([^"]+)"/g)].map((match) => match[1].toLowerCase()),
);
if (!excludedDomains.has("tradeatlas.com")) {
  throw new Error("EXCLUDED_DOMAINS could not be parsed safely");
}

function host(value) {
  try {
    return new URL(value).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

function isExcluded(value) {
  const domain = host(value);
  return [...excludedDomains].some((blocked) => domain === blocked || domain.endsWith(`.${blocked}`));
}

function turkeyPhone(value) {
  const digits = String(value).replace(/\D/g, "");
  const national = digits.startsWith("90") ? digits.slice(2) : digits.startsWith("0") ? digits.slice(1) : digits;
  if (national.length === 10) {
    return `0${national.slice(0, 3)} ${national.slice(3, 6)} ${national.slice(6, 8)} ${national.slice(8)}`;
  }
  if (national.length === 7) {
    return `0${national.slice(0, 3)} ${national.slice(3, 5)} ${national.slice(5)}`;
  }
  return `0${national}`;
}

const contacts = new Map();
const originalWorkbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load(path.join(root, "output", "foodist_expo_turkiye_blind_20260804_170123", "contacts.xlsx")),
);
const originalValues = originalWorkbook.worksheets.getItemAt(0).getUsedRange(true).values;
const manuallyRejectedCompanies = new Set(
  [3, 4, 5, 33, 49].map((rowNumber) => String(originalValues[rowNumber - 1]?.[0] ?? "")),
);

const cleanedWorkbook = await SpreadsheetFile.importXlsx(
  await FileBlob.load(path.join(root, "outputs", "foodist_review_20260805", "contacts_cleaned.xlsx")),
);
const cleanedValues = cleanedWorkbook.worksheets.getItemAt(0).getUsedRange(true).values;
const cleanedHeaders = cleanedValues[0].map((value) => String(value ?? "").toLowerCase());
const column = (name) => cleanedHeaders.indexOf(name);
for (const row of cleanedValues.slice(1)) {
  const company = String(row[column("company")] ?? "");
  const website = String(row[column("website")] ?? "");
  const email = String(row[column("email")] ?? "");
  const phone = String(row[column("phone")] ?? "");
  if (!website || !email || !phone || manuallyRejectedCompanies.has(company) || isExcluded(website)) continue;
  const key = [host(website), email.toLowerCase(), phone.replace(/\D/g, "")].join("|");
  contacts.set(key, {
    company,
    website,
    email,
    phone: turkeyPhone(phone),
    confidence: String(row[column("confidence")] ?? ""),
    status: String(row[column("status")] ?? ""),
    emailSource: String(row[column("email_source_url")] ?? ""),
    phoneSource: String(row[column("phone_source_url")] ?? ""),
  });
}

for (const relativePath of evidencePaths) {
  const text = await fs.readFile(path.join(root, relativePath), "utf8");
  for (const line of text.split(/\r?\n/).filter(Boolean)) {
    const record = JSON.parse(line);
    const selected = record.selected ?? {};
    if (!selected.website || !selected.email || !selected.phone || isExcluded(selected.website)) continue;
    const key = [host(selected.website), selected.email.toLowerCase(), String(selected.phone).replace(/\D/g, "")].join("|");
    if (!contacts.has(key)) {
      contacts.set(key, {
        company: record.company,
        website: selected.website,
        email: selected.email,
        phone: turkeyPhone(selected.phone),
        confidence: selected.confidence ?? "",
        status: selected.status ?? "",
        emailSource: selected.email_source_url ?? "",
        phoneSource: selected.phone_source_url ?? "",
      });
    }
  }
}

const rows = [...contacts.values()].sort((a, b) => a.company.localeCompare(b.company, "tr"));
const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Contacts");
sheet.showGridLines = false;
sheet.freezePanes.freezeRows(1);

const headers = [["Company", "Website", "Email", "Phone", "Confidence", "Status", "Email Source", "Phone Source"]];
sheet.getRange("A1:H1").values = headers;
sheet.getRange(`A2:H${rows.length + 1}`).values = rows.map((row) => [
  row.company,
  row.website,
  row.email,
  row.phone,
  row.confidence,
  row.status,
  row.emailSource,
  row.phoneSource,
]);

const used = sheet.getRange(`A1:H${rows.length + 1}`);
used.format = {
  font: { name: "Aptos", size: 10, color: "#1F2937" },
  verticalAlignment: "center",
  borders: { insideHorizontal: { style: "thin", color: "#E5E7EB" } },
};
sheet.getRange("A1:H1").format = {
  fill: "#14532D",
  font: { name: "Aptos", size: 10, bold: true, color: "#FFFFFF" },
  verticalAlignment: "center",
  rowHeightPx: 30,
  borders: { bottom: { style: "medium", color: "#14532D" } },
};
sheet.getRange(`A2:A${rows.length + 1}`).format.wrapText = true;
sheet.getRange(`B2:H${rows.length + 1}`).format.wrapText = false;
sheet.getRange(`A2:H${rows.length + 1}`).format.rowHeightPx = 48;
sheet.getRange("A:A").format.columnWidthPx = 330;
sheet.getRange("B:B").format.columnWidthPx = 210;
sheet.getRange("C:C").format.columnWidthPx = 220;
sheet.getRange("D:D").format.columnWidthPx = 125;
sheet.getRange("E:E").format.columnWidthPx = 105;
sheet.getRange("F:F").format.columnWidthPx = 180;
sheet.getRange("G:H").format.columnWidthPx = 300;
sheet.getRange(`D2:D${rows.length + 1}`).format.numberFormat = "@";

const table = sheet.tables.add(`A1:H${rows.length + 1}`, true, "VerifiedContacts");
table.style = "TableStyleMedium4";
table.showFilterButton = true;

const inspection = await workbook.inspect({
  kind: "table",
  range: `Contacts!A1:H${Math.min(rows.length + 1, 8)}`,
  include: "values,formulas",
  tableMaxRows: 8,
  tableMaxCols: 8,
});
console.log(inspection.ndjson);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "final formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Contacts",
  range: `A1:H${Math.min(rows.length + 1, 18)}`,
  scale: 1,
  format: "png",
});
await fs.writeFile(path.join(outputDir, "contacts_preview.png"), new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDir, "contacts_124_domestic_phones.xlsx"));
console.log(JSON.stringify({ rows: rows.length, output: path.join(outputDir, "contacts_124_domestic_phones.xlsx") }));
