// End-to-end walk through the web UI on a phone-sized viewport, against a
// server started in simulation mode. Usage (see tests/e2e/run.sh):
//   BASE_URL=http://127.0.0.1:8765 SIM_DIR=/tmp/sim/dev PYTHON=.venv/bin/python node tests/e2e/ui-flow.mjs
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";

// CommonJS resolution honours NODE_PATH, where a global Playwright usually lives.
const { chromium } = createRequire(import.meta.url)("playwright");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:8765";
const SIM = process.env.SIM_DIR;
const PY = process.env.PYTHON ?? "python3";
const SHOTS = process.env.SHOTS_DIR ?? "";
if (!SIM) throw new Error("SIM_DIR is required");

const sim = (...args) => execFileSync(PY, ["-m", "backupbox.simctl", SIM, ...args], { stdio: "pipe" });
const failures = [];
const check = (cond, label) => {
  console.log(`${cond ? "PASS" : "FAIL"} ${label}`);
  if (!cond) failures.push(label);
};

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
const consoleErrors = [];
page.on("pageerror", (err) => consoleErrors.push(String(err)));
page.on("console", (msg) => { if (msg.type() === "error") consoleErrors.push(msg.text()); });
const shot = async (name) => {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${name}.png`, fullPage: true });
};

try {
  await page.goto(BASE);
  await page.getByText("Branchez le SSD de destination").waitFor();
  check(true, "US-01 empty state asks for the SSD");
  await shot("01-empty");

  // A brand-new SSD shows up as an unknown disk and is initialised from the phone.
  sim("ssd", "sda1", "--label", "T7");
  await page.getByRole("button", { name: "Utiliser comme SSD de destination" }).click();
  const projectInput = page.locator("#init-dialog input[name=project]");
  await projectInput.fill("Islande_2026");
  await shot("02-init-dialog");
  await page.getByRole("button", { name: "Initialiser" }).click();
  await page.locator("#destination").getByText("Islande_2026").waitFor();
  check(true, "US-01 SSD initialised with project Islande_2026");
  await shot("03-destination");

  // Unknown GoPro card: countdown, then the user picks another source name.
  sim("card", "sdb1", "--brand", "gopro", "--clips", "4", "--clip-mb", "8", "--serial", "C3471");
  await page.getByText("Sauvegarde vers").first().waitFor();
  check(await page.getByText(/dans \d+ s/).isVisible(), "US-03 countdown visible for an unknown card");
  await shot("04-countdown");
  await page.getByRole("button", { name: "Changer la source" }).click();
  await page.locator("#source-dialog").waitFor({ state: "visible" });
  await page.locator("#source-dialog input[name=new_source]").fill("GoPro Keven");
  await page.getByRole("button", { name: "Enregistrer et démarrer" }).click();
  check(await page.locator("#source-dialog").isVisible(), "US-04 invalid source name keeps the dialog open");
  await page.locator("#source-dialog input[name=new_source]").fill("GoPro_Keven");
  await shot("05-source-dialog");
  await page.getByRole("button", { name: "Enregistrer et démarrer" }).click();

  await page.getByText("Sauvegardée et vérifiée", { exact: false }).first().waitFor({ timeout: 30000 });
  check(true, "US-05 backup completes and is verified");
  const cardText = await page.locator("#cards").innerText();
  check(cardText.includes("GoPro_Keven") || cardText.includes("retirer"), "US-05 card shows result");
  await shot("06-done");

  // History lists the job.
  await page.locator("#history-panel summary").click();
  await page.locator("#history").getByText("GoPro_Keven").first().waitFor();
  check(true, "US-11 history lists the backup");
  await page.locator("#history").getByRole("button", { name: "Détails" }).first().click();
  await page.locator("#detail-dialog").getByText("relecture complète").waitFor();
  check(true, "US-11 details show verification");
  await shot("07-details");
  await page.locator("#detail-dialog").getByRole("button", { name: "Fermer" }).click();

  // Same card again: known, starts without countdown, nothing recopied.
  sim("unplug", "sdb1");
  await page.getByText("Insérez une carte").waitFor();
  sim("card", "sdb2", "--brand", "gopro", "--clips", "0", "--serial", "C3471");
  await page.getByText(/Déjà sauvegardée|Sauvegardée et vérifiée/).first().waitFor({ timeout: 20000 });
  check(true, "US-06 known card handled without countdown");
  await shot("08-known-card");

  // Eject the SSD from the UI.
  await page.getByRole("button", { name: "Éjecter le SSD" }).click();
  await page.getByText("Branchez le SSD de destination").waitFor();
  check(true, "US-07 SSD ejected from the phone");

  check(consoleErrors.length === 0, `no JS errors (${consoleErrors.join(" | ")})`);
} catch (err) {
  console.error(err);
  failures.push(String(err));
  await shot("failure");
} finally {
  await browser.close();
}

if (failures.length) {
  console.error(`${failures.length} failure(s)`);
  process.exit(1);
}
console.log("E2E OK");
