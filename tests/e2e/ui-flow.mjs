// End-to-end walk through the web UI on a phone-sized viewport, against a
// server started in simulation mode with a Pi buffer. It replays the travel day:
// drone in the field (no SSD) -> GoPro on its cable -> SSD at the Airbnb.
// Usage: tests/e2e/run.sh [screenshots-dir]
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
const avata = () => sim("card", "sdc1", "--brand", "dji", "--clips", "3", "--clip-mb", "6", "--sidecars",
  "--usb-vendor", "DJI", "--usb-model", "Avata 2", "--usb-serial", "1581F6AVATA");
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
const quick = page.locator("#quick");
const buffer = page.locator("#buffer");

try {
  await page.goto(BASE);
  await page.getByText("Pas de SSD branché").waitFor();
  check(true, "US-20 no SSD: the Pi buffer is announced");
  await shot("01-field-empty");

  // Morning, in the field: the Avata 2 on its USB cable, no SSD.
  avata();
  await quick.getByText("DJI Avata 2").waitFor();
  await quick.getByText("3 vidéos").waitFor();
  check(await quick.getByText("Tampon du Pi").first().isVisible(), "US-19/20 drone recognised, target = Pi buffer");
  await quick.getByText("Vidéos seulement").click();
  await quick.getByRole("button", { name: /Transférer les vidéos/ }).waitFor();
  await shot("02-drone-quick-action");
  await quick.getByRole("button", { name: /Transférer les vidéos/ }).click();
  await page.locator("#cards").getByText("Sauvegardé et vérifié").first().waitFor({ timeout: 30000 });
  check(true, "US-23 videos-only transfer to the buffer verified");
  await buffer.getByText("en attente du SSD").waitFor();
  check(await buffer.getByText("DJI_Avata_2").isVisible(), "US-20 buffer lists what waits for the SSD");
  await shot("03-drone-in-buffer");
  sim("unplug", "sdc1");

  // Midday: a GoPro over MTP, new device -> name it.
  sim("card", "mtp1", "--brand", "gopro", "--clips", "2", "--clip-mb", "4", "--mtp",
    "--usb-model", "GoPro HERO12 Black", "--usb-serial", "C3471");
  await quick.getByText("GoPro HERO12 Black").waitFor();
  check(await quick.getByText(/Démarrage automatique dans/).isVisible(), "US-03 countdown for a new device");
  await quick.getByRole("button", { name: "Changer le nom" }).click();
  await page.locator("#source-dialog").waitFor({ state: "visible" });
  await page.locator("#source-dialog input[name=new_source]").fill("GoPro Keven");
  await page.getByRole("button", { name: "Enregistrer et démarrer" }).click();
  check(await page.locator("#source-dialog").isVisible(), "US-04 invalid name keeps the dialog open");
  await page.locator("#source-dialog input[name=new_source]").fill("GoPro_Keven");
  await page.getByRole("button", { name: "Enregistrer et démarrer" }).click();
  await page.locator("#cards").getByText("GoPro_Keven").first().waitFor({ timeout: 30000 });
  check(true, "US-19 GoPro over MTP backed up under its new name");
  sim("unplug", "mtp1");

  // Evening at the Airbnb: a brand-new SSD.
  sim("ssd", "sda1", "--label", "T7");
  await page.getByRole("button", { name: "Utiliser comme SSD de destination" }).click();
  await page.locator("#init-dialog input[name=project]").fill("Islande_2026");
  await page.getByRole("button", { name: "Initialiser" }).click();
  await page.locator("#destination").getByText("Islande_2026").waitFor();
  await buffer.getByText("Rien en attente du SSD").waitFor({ timeout: 30000 });
  check(true, "US-22 buffer drained to the SSD automatically");
  await shot("04-evening-drained");

  // History: the drain is listed.
  await page.locator("#history-panel summary").click();
  await page.locator("#history").getByText("Tampon → SSD").first().waitFor();
  check(true, "US-11 history lists the drain");
  await page.locator("#history").getByRole("button", { name: "Détails" }).first().click();
  await page.locator("#detail-dialog").getByText("relecture complète").waitFor();
  await page.locator("#detail-dialog").getByRole("button", { name: "Fermer" }).click();

  // Next morning: same drone, not formatted -> nothing copied twice.
  avata();
  await page.getByText(/Déjà sauvegardé/).first().waitFor({ timeout: 20000 });
  check(true, "US-06/19 known drone: nothing copied twice");
  await shot("05-known-drone");

  // Eject the SSD from the phone.
  await page.getByRole("button", { name: "Éjecter le SSD" }).click();
  await page.getByText("Pas de SSD branché").waitFor();
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
