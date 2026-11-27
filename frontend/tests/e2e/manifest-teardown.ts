import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type { ScreenshotManifestEntry } from "./manifest-helper";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SCREENSHOTS_DIR = path.resolve(__dirname, "../../screenshots");

function resolveCommitSha(): string {
  const fromEnv = (process.env.CIRCLE_SHA1 ?? process.env.NIGHTSHIFT_COMMIT_SHA ?? "").trim();
  if (fromEnv) {
    return fromEnv;
  }
  try {
    return execSync("git rev-parse HEAD", { encoding: "utf-8" }).trim();
  } catch {
    return "unknown";
  }
}

export default async function globalTeardown(): Promise<void> {
  const entriesDir = path.join(SCREENSHOTS_DIR, ".manifest-entries");
  if (!fs.existsSync(entriesDir)) {
    return;
  }

  const files = fs
    .readdirSync(entriesDir)
    .filter((f) => f.endsWith(".json"))
    .sort();

  const screenshots: ScreenshotManifestEntry[] = [];
  for (const file of files) {
    try {
      const parsed = JSON.parse(
        fs.readFileSync(path.join(entriesDir, file), "utf-8"),
      ) as ScreenshotManifestEntry;
      screenshots.push(parsed);
    } catch {
      // Ignore malformed temp fragments
    }
  }

  screenshots.sort((a, b) => a.file.localeCompare(b.file));

  const buildId = (
    process.env.CIRCLE_BUILD_NUM ??
    process.env.NIGHTSHIFT_BUILD_ID ??
    "local"
  ).trim();
  const commitSha = resolveCommitSha();

  const manifest = {
    schema_version: 1,
    renderer: "playwright-mockbridge",
    build_id: buildId || "local",
    commit_sha: commitSha,
    screenshots,
  };

  fs.mkdirSync(SCREENSHOTS_DIR, { recursive: true });
  fs.writeFileSync(
    path.join(SCREENSHOTS_DIR, "screenshot-manifest.json"),
    `${JSON.stringify(manifest, null, 2)}\n`,
    "utf-8",
  );
  fs.rmSync(entriesDir, { recursive: true, force: true });
}
