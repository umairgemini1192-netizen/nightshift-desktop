import { Icon, Kbd, StatusPill } from "../design";
import { useAppStore } from "./store";

export function TopBar() {
  const snapshot = useAppStore((s) => s.snapshot);
  const bridge = useAppStore((s) => s.bridge);
  const selectProject = useAppStore((s) => s.selectProject);
  const openCommandPalette = useAppStore((s) => s.openCommandPalette);

  return (
    <header className="pywebview-drag-region ns-glass-strong z-20 flex h-topbar shrink-0 items-center justify-between gap-4 border-b border-border-subtle px-4">
      <div className="flex items-center gap-4 min-w-0">
        <div className="flex items-center gap-2 shrink-0">
          <span className="flex h-8 w-8 items-center justify-center rounded-md bg-accent-primary-soft text-accent-primary">
            <Icon name="Radar" size={20} />
          </span>
          <span className="font-display text-md font-semibold tracking-tight text-text-primary">
            Night-Shift
          </span>
        </div>

        <div className="pywebview-no-drag flex items-center gap-2 min-w-0">
          <label htmlFor="project-switcher" className="sr-only">
            Active project
          </label>
          <div className="relative flex items-center">
            <Icon
              name="FolderGit2"
              size={16}
              className="pointer-events-none absolute left-3 text-text-muted"
            />
            <select
              id="project-switcher"
              value={snapshot.activeProjectId ?? ""}
              onChange={(e) => {
                if (e.target.value) {
                  void selectProject(e.target.value);
                }
              }}
              disabled={snapshot.projects.length === 0}
              className="h-btn-sm appearance-none rounded-md border border-border-subtle bg-surface-raised pl-8 pr-8 text-sm text-text-primary transition-colors duration-fast hover:border-border-strong disabled:opacity-60"
            >
              {snapshot.projects.length === 0 ? (
                <option value="">No project connected</option>
              ) : (
                snapshot.projects.map((project) => (
                  <option key={project.id} value={project.id}>
                    {project.name}
                  </option>
                ))
              )}
            </select>
            <Icon
              name="ChevronDown"
              size={16}
              className="pointer-events-none absolute right-2 text-text-muted"
            />
          </div>
        </div>
      </div>

      <div className="pywebview-no-drag flex items-center gap-3 shrink-0">
        <StatusPill state={snapshot.runState} />

        <button
          type="button"
          onClick={openCommandPalette}
          className="inline-flex h-btn-sm items-center gap-2 rounded-md border border-border-subtle bg-surface-raised px-3 text-sm text-text-secondary transition-colors duration-fast hover:border-border-strong hover:text-text-primary"
        >
          <Icon name="Search" size={16} />
          <span>Commands</span>
          <span aria-hidden="true">
            <Kbd>Ctrl K</Kbd>
          </span>
        </button>

        <div className="flex items-center gap-1 border-l border-border-subtle pl-2">
          <button
            type="button"
            aria-label="Minimize window"
            onClick={() => void bridge.call("window.minimize")}
            className="flex h-btn-sm w-8 items-center justify-center rounded-sm text-text-secondary transition-colors duration-fast hover:bg-accent-primary-soft hover:text-text-primary"
          >
            <Icon name="Minus" size={16} />
          </button>
          <button
            type="button"
            aria-label="Maximize window"
            onClick={() => void bridge.call("window.toggleMaximize")}
            className="flex h-btn-sm w-8 items-center justify-center rounded-sm text-text-secondary transition-colors duration-fast hover:bg-accent-primary-soft hover:text-text-primary"
          >
            <Icon name="Square" size={16} />
          </button>
          <button
            type="button"
            aria-label="Close window"
            onClick={() => void bridge.call("window.close")}
            className="flex h-btn-sm w-8 items-center justify-center rounded-sm text-text-secondary transition-colors duration-fast hover:bg-status-danger hover:text-text-on-accent"
          >
            <Icon name="X" size={16} />
          </button>
        </div>
      </div>
    </header>
  );
}
