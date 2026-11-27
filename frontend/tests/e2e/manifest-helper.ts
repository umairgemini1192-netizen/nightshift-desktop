import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export interface ScreenshotManifestEntry {
  file: string;
  viewport_or_device: string;
  scale: string;
  axe_violations: number;
  overflow: boolean;
  sha256: string;
}

export function recordScreenshotManifestEntry(params: {
  screenshotsDir: string;
  filename: string;
  viewportOrDevice: string;
  scale: string;
  axeViolations: number;
  overflow: boolean;
}): void {
  const pngPath = path.join(params.screenshotsDir, params.filename);
  const bytes = fs.readFileSync(pngPath);
  const sha256 = crypto.createHash("sha256").update(bytes).digest("hex");

  const entry: ScreenshotManifestEntry = {
    file: params.filename,
    viewport_or_device: params.viewportOrDevice,
    scale: params.scale,
    axe_violations: params.axeViolations,
    overflow: params.overflow,
    sha256,
  };

  const entriesDir = path.join(params.screenshotsDir, ".manifest-entries");
  fs.mkdirSync(entriesDir, { recursive: true });
  const safeName = params.filename.replace(/[^a-zA-Z0-9._-]/g, "_");
  fs.writeFileSync(
    path.join(entriesDir, `${safeName}.json`),
    `${JSON.stringify(entry)}\n`,
    "utf-8",
  );
}
