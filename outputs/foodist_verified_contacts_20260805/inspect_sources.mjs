import { FileBlob, SpreadsheetFile } from "@oai/artifact-tool";

const files = [
  "outputs/foodist_review_20260805/contacts_cleaned.xlsx",
  "output/foodist_expo_turkiye_blind_20260804_170123/contacts.xlsx",
  "outputs/foodist_verified_contacts_20260805/contacts.xlsx",
  "outputs/foodist_verified_contacts_20260805/contacts_124_unique.xlsx",
];

for (const file of files) {
  const workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(file));
  const summary = await workbook.inspect({
    kind: "workbook,sheet,table",
    maxChars: 5000,
    tableMaxRows: 5,
    tableMaxCols: 10,
  });
  console.log(file);
  console.log(summary.ndjson);
  if (file.includes("contacts_cleaned")) {
    const sheet = workbook.worksheets.getItemAt(0);
    const values = sheet.getUsedRange(true).values;
    console.log("HEADERS", JSON.stringify(values[0]));
    for (const rowNumber of [3, 4, 5, 24, 33, 38, 49, 54]) {
      console.log(`ROW ${rowNumber}`, JSON.stringify(values[rowNumber - 1]));
    }
    const headers = values[0].map((value) => String(value ?? "").toLowerCase());
    const website = headers.indexOf("website");
    const email = headers.indexOf("email");
    const phone = headers.indexOf("phone");
    const complete = values.slice(1).filter((row) => row[website] && row[email] && row[phone]);
    console.log("COMPLETE", complete.length);
  }
  if (file.includes("foodist_expo_turkiye_blind_20260804_170123")) {
    const values = workbook.worksheets.getItemAt(0).getUsedRange(true).values;
    for (const rowNumber of [3, 4, 5, 33, 49]) {
      console.log(`ORIGINAL BAD ROW ${rowNumber}`, JSON.stringify(values[rowNumber - 1]));
    }
  }
}
