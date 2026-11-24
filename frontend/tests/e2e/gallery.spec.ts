import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREENSHOTS_DIR = path.resolve(__dirname, "../../screenshots");
const DEV_GALLERY_URL = "http://127.0.0.1:4174/gallery";

test.beforeAll(() => {
  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
});

for (const theme of ["dark", "light"] as const) {
  test(`gallery [${theme}] @ 1440x900`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce", colorScheme: theme });

    await page.goto(DEV_GALLERY_URL, { waitUntil: "networkidle" });
    await expect(page.locator("main h1")).toHaveText("Component Gallery");

    const themeRadio = page.getByRole("radio", { name: theme === "dark" ? "Dark" : "Light" });
    await themeRadio.click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);

    await page.evaluate(() => document.fonts.ready);

    // Assert zero horizontal overflow.
    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(
      overflow.scrollWidth,
      `Horizontal overflow on gallery (${theme}): scrollWidth=${overflow.scrollWidth} > clientWidth=${overflow.clientWidth}`,
    ).toBeLessThanOrEqual(overflow.clientWidth);

    // Run axe-core accessibility scan and fail on serious or critical violations.
    const axeResults = await new AxeBuilder({ page }).analyze();
    const blocking = axeResults.violations.filter(
      (v) => v.impact === "serious" || v.impact === "critical",
    );
    expect(
      blocking,
      `Accessibility violations on gallery (${theme}): ${JSON.stringify(
        blocking.map((v) => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length })),
      )}`,
    ).toEqual([]);

    const outPath = path.join(SCREENSHOTS_DIR, `gallery-${theme}-1440x900.png`);
    await page.screenshot({ path: outPath, fullPage: true });
  });
}

test("gallery route is excluded from production builds", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.emulateMedia({ reducedMotion: "reduce", colorScheme: "dark" });

  await page.goto("/gallery", { waitUntil: "networkidle" });
  await expect(page.locator("main h1")).toHaveText("Mission Control");
  await expect(page.getByRole("link", { name: /Component Gallery/i })).toHaveCount(0);
});
