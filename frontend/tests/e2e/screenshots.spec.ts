import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREENSHOTS_DIR = path.resolve(__dirname, "../../screenshots");

const OTHER_MODULES = [
  { id: "plan-builder", route: "/plan-builder" },
  { id: "runs-history", route: "/runs" },
  { id: "engines-quota", route: "/engines" },
  { id: "settings", route: "/settings" },
] as const;

const BASE_SCENARIOS = ["idle", "running", "failure", "empty"] as const;

const MISSION_CONTROL_SCENARIOS = [
  "idle",
  "running",
  "failure",
  "empty",
  "gemini-logged-out",
  "claude-limit",
  "gh-logged-out",
  "handoff",
  "ci-healing",
  "finished",
] as const;

const VIEWPORTS = [
  { name: "1440x900", width: 1440, height: 900, fontScalePct: 100 },
  { name: "1024x700", width: 1024, height: 700, fontScalePct: 100 },
  { name: "1440x900-150pct", width: 1440, height: 900, fontScalePct: 150 },
] as const;

const THEMES = ["dark", "light"] as const;

test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
});

// Mission Control: full coverage across all scenarios, viewports, and both themes
for (const scenario of MISSION_CONTROL_SCENARIOS) {
  for (const vp of VIEWPORTS) {
    for (const theme of THEMES) {
      test(`mission-control [${scenario}] @ ${vp.name} (${theme})`, async ({ page }) => {
        await page.setViewportSize({ width: vp.width, height: vp.height });
        await page.emulateMedia({ reducedMotion: "reduce", colorScheme: theme });

        await page.goto(`/?scenario=${scenario}`, { waitUntil: "networkidle" });
        await page.locator("main h1").waitFor({ state: "visible" });

        await page.evaluate((th) => {
          localStorage.setItem("nightshift.theme", th);
          document.documentElement.dataset.theme = th;
        }, theme);

        if (vp.fontScalePct !== 100) {
          await page.evaluate((pct) => {
            document.documentElement.style.fontSize = `${pct}%`;
          }, vp.fontScalePct);
        }

        await page.evaluate(() => document.fonts.ready);

        // Assert zero horizontal overflow.
        const overflow = await page.evaluate(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        }));
        expect(
          overflow.scrollWidth,
          `Horizontal overflow on mission-control (${scenario}, ${theme}, ${vp.name}): scrollWidth=${overflow.scrollWidth} > clientWidth=${overflow.clientWidth}`,
        ).toBeLessThanOrEqual(overflow.clientWidth);

        // Run axe-core accessibility scan and fail on serious or critical violations.
        const axeResults = await new AxeBuilder({ page }).analyze();
        const blocking = axeResults.violations.filter(
          (v) => v.impact === "serious" || v.impact === "critical",
        );
        expect(
          blocking,
          `Accessibility violations on mission-control (${scenario}, ${theme}, ${vp.name}): ${JSON.stringify(
            blocking.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length })),
          )}`,
        ).toEqual([]);

        const filename =
          theme === "dark"
            ? `mission-control-${scenario}-${vp.name}.png`
            : `mission-control-${scenario}-light-${vp.name}.png`;
        const outPath = path.join(SCREENSHOTS_DIR, filename);
        await page.screenshot({ path: outPath, fullPage: true });
      });
    }
  }
}

// Other modules: primary scenarios across 3 viewports
for (const mod of OTHER_MODULES) {
  for (const scenario of BASE_SCENARIOS) {
    for (const vp of VIEWPORTS) {
      test(`${mod.id} [${scenario}] @ ${vp.name}`, async ({ page }) => {
        await page.setViewportSize({ width: vp.width, height: vp.height });
        await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });

        await page.goto(`${mod.route}?scenario=${scenario}`, { waitUntil: "networkidle" });
        await page.locator("main h1").waitFor({ state: "visible" });

        if (vp.fontScalePct !== 100) {
          await page.evaluate((pct) => {
            document.documentElement.style.fontSize = `${pct}%`;
          }, vp.fontScalePct);
        }

        await page.evaluate(() => document.fonts.ready);

        // Assert zero horizontal overflow.
        const overflow = await page.evaluate(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        }));
        expect(
          overflow.scrollWidth,
          `Horizontal overflow on ${mod.id} (${scenario}, ${vp.name}): scrollWidth=${overflow.scrollWidth} > clientWidth=${overflow.clientWidth}`,
        ).toBeLessThanOrEqual(overflow.clientWidth);

        // Run axe-core accessibility scan and fail on serious or critical violations.
        const axeResults = await new AxeBuilder({ page }).analyze();
        const blocking = axeResults.violations.filter(
          (v) => v.impact === "serious" || v.impact === "critical",
        );
        expect(
          blocking,
          `Accessibility violations on ${mod.id} (${scenario}, ${vp.name}): ${JSON.stringify(
            blocking.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length })),
          )}`,
        ).toEqual([]);

        const outPath = path.join(SCREENSHOTS_DIR, `${mod.id}-${scenario}-${vp.name}.png`);
        await page.screenshot({ path: outPath, fullPage: true });
      });
    }
  }
}

test("interactive shell: command palette and theme switching", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/settings?scenario=idle", { waitUntil: "networkidle" });
  await page.locator("main h1").waitFor({ state: "visible" });

  await page.getByRole("radio", { name: "Light" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "light");

  // Verify light theme also has zero serious/critical axe violations.
  const lightAxe = await new AxeBuilder({ page }).analyze();
  const blockingLight = lightAxe.violations.filter(
    (v) => v.impact === "serious" || v.impact === "critical",
  );
  expect(blockingLight).toEqual([]);

  await page.keyboard.press("Control+k");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Command palette" })).toBeHidden();
});
