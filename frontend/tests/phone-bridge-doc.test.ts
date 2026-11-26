import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

describe("phone bridge document redraw", () => {
  it("redraws an empty document using supported Body methods", () => {
    const text = {
      setForegroundColor() { return this; },
      setFontSize() { return this; },
      setBold() { return this; },
    };
    const body = {
      clear: vi.fn(),
      setMarginTop: vi.fn(),
      setMarginBottom: vi.fn(),
      setMarginLeft: vi.fn(),
      setMarginRight: vi.fn(),
      appendParagraph: vi.fn(() => ({ setHeading: vi.fn(), editAsText: () => text })),
    };
    const doc = { getBody: () => body, saveAndClose: vi.fn() };
    const context = {
      DocumentApp: { openById: () => doc, ParagraphHeading: { HEADING2: "heading" } },
      props: { getProperty: () => "doc-id" },
    };
    const source = readFileSync("../tools/phone-bridge/Code.gs", "utf8");
    runInNewContext(`${source}\nredrawDoc_(props, {}, 0);`, context);
    expect(body.appendParagraph).toHaveBeenCalledTimes(2);
    expect(doc.saveAndClose).toHaveBeenCalledOnce();
  });
});
