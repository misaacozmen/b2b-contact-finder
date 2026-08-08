import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const source = "../../output/foodist_review_live_general_v7_scaled_20260805/evidence.jsonl";
const rows = (await fs.readFile(source, "utf8"))
  .split(/\r?\n/)
  .filter(Boolean)
  .map((line) => JSON.parse(line))
  .filter((item) => !(
    item.selected?.website && item.selected?.email && item.selected?.phone
  ))
  .map((item) => [item.company]);

const workbook = Workbook.create();
const sheet = workbook.worksheets.add("Companies");
sheet.showGridLines = false;
sheet.getRange("A1").values = [["Company"]];
sheet.getRange(`A2:A${rows.length + 1}`).values = rows;
sheet.getRange("A1").format = {
  fill: "#0F766E",
  font: { bold: true, color: "#FFFFFF" },
};
sheet.getRange(`A1:A${rows.length + 1}`).format.columnWidth = 80;
sheet.freezePanes.freezeRows(1);

const inspected = await workbook.inspect({
  kind: "table",
  range: `Companies!A1:A${rows.length + 1}`,
  include: "values,formulas",
  tableMaxRows: 5,
  tableMaxCols: 1,
});
console.log(inspected.ndjson);
const preview = await workbook.render({
  sheetName: "Companies", range: "A1:A8", scale: 1, format: "png",
});
await fs.writeFile("input_preview.png", new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save("input_companies_only.xlsx");
console.log(`rows=${rows.length}`);
