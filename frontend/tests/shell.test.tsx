import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { MODULES } from "../src/app/registry";
import { Shell } from "../src/app/Shell";
import { useAppStore } from "../src/app/store";

describe("Module registry and shell", () => {
  it("discovers all core module manifests dynamically (plus dev gallery in dev mode)", () => {
    const ids = MODULES.map((m) => m.id);
    expect(ids).toEqual([
      "mission-control",
      "plan-studio",
      "plan-builder",
      "runs-history",
      "engines-quota",
      "gallery",
      "settings",
    ]);
  });

  it("renders shell navigation, opens command palette with Ctrl+K, and switches theme", async () => {
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/?scenario=idle"]}>
        <Shell />
      </MemoryRouter>,
    );

    expect(await screen.findByRole("heading", { name: "Mission Control" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Plan Builder/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Settings/i })).toBeInTheDocument();

    // Open command palette via keyboard shortcut
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog", { name: "Command palette" })).not.toBeInTheDocument();

    // Navigate to Settings and switch theme to light
    await user.click(screen.getByRole("link", { name: /Settings/i }));
    expect(await screen.findByRole("heading", { name: "Settings" })).toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: "Light" }));
    await waitFor(() => {
      expect(useAppStore.getState().resolvedTheme).toBe("light");
      expect(document.documentElement.dataset.theme).toBe("light");
    });

    // Reset theme back to dark for clean state
    await user.click(screen.getByRole("radio", { name: "Dark" }));
    await waitFor(() => {
      expect(document.documentElement.dataset.theme).toBe("dark");
    });
  });
});
