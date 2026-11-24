import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import {
  Button,
  Checkbox,
  CodeEditor,
  ConfirmDialog,
  EngineCard,
  GlassCard,
  PhaseTimeline,
  ProgressBar,
  ProgressRing,
  SegmentedControl,
  Select,
  Skeleton,
  StatTile,
  StatusPill,
  StepFeed,
  Tabs,
  TextArea,
  TextField,
  Toggle,
  Tooltip,
  type StatusPillState,
} from "../src/design";

describe("Design-system components", () => {
  it("Button loading keeps label in DOM to preserve width and sets aria-busy", () => {
    const { rerender } = render(<Button>Start overnight run</Button>);
    const button = screen.getByRole("button", { name: "Start overnight run" });
    expect(button).not.toBeDisabled();
    expect(screen.getByTestId("button-content")).not.toHaveClass("invisible");

    rerender(<Button loading>Start overnight run</Button>);
    const loadingBtn = screen.getByRole("button");
    expect(loadingBtn).toBeDisabled();
    expect(loadingBtn).toHaveAttribute("aria-busy", "true");
    // Original label stays rendered (invisible) so layout width does not shrink
    const content = screen.getByTestId("button-content");
    expect(content).toHaveClass("invisible");
    expect(content).toHaveTextContent("Start overnight run");
    expect(screen.getByTestId("button-spinner")).toBeInTheDocument();
  });

  it("Toggle supports keyboard Space and Enter and exposes switch semantics", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [checked, setChecked] = useState(false);
      return (
        <Toggle
          label="Auto-merge green PRs"
          helperText="Only low-risk pull requests"
          checked={checked}
          onChange={setChecked}
        />
      );
    }

    render(<Harness />);
    const sw = screen.getByRole("switch", { name: "Auto-merge green PRs" });
    expect(sw).toHaveAttribute("aria-checked", "false");

    sw.focus();
    await user.keyboard(" ");
    expect(sw).toHaveAttribute("aria-checked", "true");

    await user.keyboard("{Enter}");
    expect(sw).toHaveAttribute("aria-checked", "false");
  });

  it("Select supports full keyboard navigation and skips disabled options", async () => {
    const user = userEvent.setup();
    const options = [
      { value: "agy", label: "Antigravity" },
      { value: "gemini", label: "Gemini CLI", disabled: true },
      { value: "codex", label: "Codex" },
    ] as const;

    function Harness() {
      const [val, setVal] = useState("agy");
      return <Select label="Engine" value={val} options={options} onChange={setVal} />;
    }

    render(<Harness />);
    const trigger = screen.getByRole("combobox", { name: "Engine" });
    trigger.focus();

    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("listbox", { name: "Engine" })).toBeInTheDocument();

    // ArrowDown skips disabled Gemini CLI and lands on Codex
    await user.keyboard("{ArrowDown}{Enter}");
    expect(trigger).toHaveTextContent("Codex");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();

    // Escape closes without changing selection
    await user.keyboard("{ArrowDown}{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveTextContent("Codex");
  });

  it("ConfirmDialog traps Tab and Shift+Tab focus, closes on Escape, and restores focus", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();

    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <div>
          <Button onClick={() => setOpen(true)}>Open destructive action</Button>
          <ConfirmDialog
            open={open}
            title="Park phase?"
            description="Leaves the PR open and moves on."
            consequence="No commits will be lost."
            confirmLabel="Park phase"
            cancelLabel="Keep running"
            onConfirm={() => {
              onConfirm();
              setOpen(false);
            }}
            onCancel={() => setOpen(false)}
          />
        </div>
      );
    }

    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "Open destructive action" });
    await user.click(trigger);

    const dialog = screen.getByRole("alertdialog", { name: "Park phase?" });
    expect(dialog).toBeInTheDocument();
    expect(within(dialog).getByText("No commits will be lost.")).toBeInTheDocument();

    const cancelBtn = within(dialog).getByRole("button", { name: "Keep running" });
    const confirmBtn = within(dialog).getByRole("button", { name: "Park phase" });
    expect(cancelBtn).toHaveFocus();

    // Tab moves to confirm, then wraps back to cancel
    await user.tab();
    expect(confirmBtn).toHaveFocus();
    await user.tab();
    expect(cancelBtn).toHaveFocus();

    // Shift+Tab wraps back to confirm
    await user.tab({ shift: true });
    expect(confirmBtn).toHaveFocus();

    // Escape closes dialog and restores focus to trigger
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("ProgressRing exposes accessible aria progress values and clamps bounds", () => {
    const { rerender } = render(
      <ProgressRing value={68} max={100} label="Overall progress" caption="4 of 6 phases" />,
    );
    const bar = screen.getByRole("progressbar", { name: "Overall progress" });
    expect(bar).toHaveAttribute("aria-valuemin", "0");
    expect(bar).toHaveAttribute("aria-valuemax", "100");
    expect(bar).toHaveAttribute("aria-valuenow", "68");
    expect(bar).toHaveAttribute("aria-valuetext", "68%");
    expect(bar).toHaveTextContent("68%");

    rerender(<ProgressRing value={140} max={100} label="Overall progress" />);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });

  it("StatusPill never relies on colour alone across all six states", () => {
    const states: StatusPillState[] = ["idle", "running", "waiting", "cooling", "failed", "done"];
    render(
      <div>
        {states.map((s) => (
          <StatusPill key={s} state={s} />
        ))}
      </div>,
    );

    const pills = screen.getAllByRole("status");
    expect(pills).toHaveLength(states.length);
    for (const pill of pills) {
      expect(pill.querySelector("svg")).not.toBeNull();
      expect(pill.textContent?.trim().length).toBeGreaterThan(0);
    }
  });

  it("StepFeed expands raw log lines when Details is clicked", async () => {
    const user = userEvent.setup();
    render(
      <StepFeed
        items={[
          {
            id: "1",
            message: "Checks passed",
            relativeTime: "2m ago",
            status: "success",
            rawLog: "pytest -q -> 42 passed",
          },
        ]}
      />,
    );

    expect(screen.queryByText("pytest -q -> 42 passed")).not.toBeInTheDocument();
    const detailsBtn = screen.getByRole("button", { name: "Details" });
    await user.click(detailsBtn);
    expect(detailsBtn).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("pytest -q -> 42 passed")).toBeInTheDocument();
  });

  it("EngineCard labels quota as estimate and displays cooling time", () => {
    render(
      <EngineCard
        name="Gemini CLI"
        model="gemini-2.5-pro"
        effort="Medium"
        state="cooling"
        coolingUntil="03:10"
        quotaUsedPct={82}
      />,
    );

    expect(screen.getByText("Cooling until 03:10")).toBeInTheDocument();
    expect(screen.getByText(/estimate/i)).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Gemini CLI estimated quota used" })).toHaveAttribute(
      "aria-valuenow",
      "82",
    );
  });

  it("Tabs and SegmentedControl support arrow key navigation", async () => {
    const user = userEvent.setup();
    function Harness() {
      const [tab, setTab] = useState("a");
      const [seg, setSeg] = useState("dark");
      return (
        <div>
          <Tabs
            label="Sections"
            value={tab}
            onChange={setTab}
            items={[
              { id: "a", label: "First", content: <p>Panel A</p> },
              { id: "b", label: "Second", content: <p>Panel B</p> },
            ]}
          />
          <SegmentedControl
            label="Theme"
            value={seg}
            onChange={setSeg}
            options={[
              { value: "dark", label: "Dark" },
              { value: "light", label: "Light" },
            ]}
          />
        </div>
      );
    }

    render(<Harness />);
    const firstTab = screen.getByRole("tab", { name: "First" });
    firstTab.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Second" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Panel B")).toBeInTheDocument();

    const darkRadio = screen.getByRole("radio", { name: "Dark" });
    darkRadio.focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("radio", { name: "Light" })).toHaveAttribute("aria-checked", "true");
  });

  it("renders Checkbox, TextField, TextArea, CodeEditor, ProgressBar, PhaseTimeline, StatTile, Tooltip, Skeleton, and GlassCard states", async () => {
    const user = userEvent.setup();
    const onCodeChange = vi.fn();

    render(
      <div>
        <Checkbox label="Enable release" checked={false} onChange={() => {}} error="Required" />
        <TextField label="Branch" value="main" onChange={() => {}} helperText="Target branch" />
        <TextArea label="Goal" value="Ship v1" onChange={() => {}} showCount maxLength={100} />
        <CodeEditor label="Config" value="{}" onChange={onCodeChange} />
        <ProgressBar
          label="Plan"
          segments={[
            { id: "1", label: "P1", status: "done" },
            { id: "2", label: "P2", status: "running" },
          ]}
        />
        <PhaseTimeline
          items={[
            { id: "1", title: "P1", status: "done" },
            { id: "2", title: "P2", status: "blocked", blockedReason: "Waiting on P1" },
          ]}
        />
        <StatTile label="Merged" value="4" delta="+1" deltaTone="positive" sparkline={[1, 2, 4]} />
        <Tooltip content="Shortcut info">
          <Button>Info</Button>
        </Tooltip>
        <Skeleton lines={2} />
        <GlassCard title="Error state" error="Connection lost" />
      </div>,
    );

    expect(screen.getByText("Required")).toBeInTheDocument();
    expect(screen.getByText("Target branch")).toBeInTheDocument();
    expect(screen.getByText("7/100")).toBeInTheDocument();
    expect(screen.getByText("Waiting on P1")).toBeInTheDocument();
    expect(screen.getByText("Connection lost")).toBeInTheDocument();

    const infoBtn = screen.getByRole("button", { name: "Info" });
    fireEvent.focus(infoBtn);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Shortcut info");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });
});
