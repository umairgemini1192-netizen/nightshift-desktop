import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Icon, Kbd } from "../design";
import { MODULES } from "./registry";
import { useAppStore } from "./store";

interface PaletteEntry {
  id: string;
  title: string;
  group: string;
  icon: string;
  run: () => void | Promise<unknown>;
}

function fuzzyMatch(query: string, text: string): boolean {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  const target = text.toLowerCase();
  if (target.includes(q)) return true;
  let qi = 0;
  for (let i = 0; i < target.length && qi < q.length; i += 1) {
    if (target[i] === q[qi]) qi += 1;
  }
  return qi === q.length;
}

export function CommandPalette() {
  const open = useAppStore((s) => s.commandPaletteOpen);
  const close = useAppStore((s) => s.closeCommandPalette);
  const toggle = useAppStore((s) => s.toggleCommandPalette);
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const navigate = useNavigate();
  const location = useLocation();
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        toggle();
      } else if (event.key === "Escape" && open) {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, close, toggle]);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  const entries = useMemo<PaletteEntry[]>(() => {
    const navEntries: PaletteEntry[] = MODULES.map((mod) => ({
      id: `nav:${mod.id}`,
      title: `Go to ${mod.title}`,
      group: "Navigation",
      icon: mod.icon,
      run: () => {
        navigate(location.search ? `${mod.route}${location.search}` : mod.route);
      },
    }));

    const cmdEntries: PaletteEntry[] = MODULES.flatMap((mod) =>
      (mod.commands ?? []).map((cmd) => ({
        id: `cmd:${mod.id}:${cmd.id}`,
        title: cmd.title,
        group: mod.title,
        icon: mod.icon,
        run: async () => {
          await cmd.run(bridge);
          pushToast({
            title: cmd.title,
            description: "Command sent to Night-Shift.",
            tone: "info",
          });
        },
      })),
    );

    return [...navEntries, ...cmdEntries];
  }, [bridge, location.search, navigate, pushToast]);

  const filtered = useMemo(
    () => entries.filter((e) => fuzzyMatch(query, `${e.title} ${e.group}`)),
    [entries, query],
  );

  useEffect(() => {
    setSelectedIndex(0);
  }, [query]);

  if (!open) return null;

  const execute = (entry: PaletteEntry | undefined) => {
    if (!entry) return;
    close();
    void entry.run();
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
      className="fixed inset-0 z-50 flex items-start justify-center bg-bg-canvas/75 px-4 pt-16"
      onClick={close}
    >
      <div
        className="ns-glass-strong w-full max-w-readable overflow-hidden rounded-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3 border-b border-border-subtle px-4 py-3">
          <Icon name="Search" size={20} className="text-text-muted shrink-0" />
          <label htmlFor="command-palette-input" className="sr-only">
            Search commands and pages
          </label>
          <input
            ref={inputRef}
            id="command-palette-input"
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setSelectedIndex((i) => (filtered.length ? (i + 1) % filtered.length : 0));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setSelectedIndex((i) =>
                  filtered.length ? (i - 1 + filtered.length) % filtered.length : 0,
                );
              } else if (e.key === "Enter") {
                e.preventDefault();
                execute(filtered[selectedIndex]);
              }
            }}
            placeholder="Type a page or command..."
            className="w-full bg-transparent text-md text-text-primary placeholder:text-text-muted focus:outline-none"
          />
          <Kbd>Esc</Kbd>
        </div>

        <ul
          role="listbox"
          aria-label="Commands"
          className="overflow-y-auto p-2"
          style={{ maxHeight: "16rem" }}
        >
          {filtered.length === 0 ? (
            <li className="px-4 py-6 text-center text-sm text-text-muted">
              No matching commands.
            </li>
          ) : (
            filtered.map((entry, idx) => {
              const selected = idx === selectedIndex;
              return (
                <li key={entry.id} role="option" aria-selected={selected}>
                  <button
                    type="button"
                    onMouseEnter={() => setSelectedIndex(idx)}
                    onClick={() => execute(entry)}
                    className={`flex w-full items-center justify-between gap-3 rounded-md px-3 py-2 text-left text-sm transition-colors duration-fast ${
                      selected
                        ? "bg-accent-primary-soft text-accent-primary"
                        : "text-text-primary hover:bg-surface-raised"
                    }`}
                  >
                    <span className="flex items-center gap-3 min-w-0">
                      <Icon name={entry.icon} size={16} className="shrink-0" />
                      <span className="truncate">{entry.title}</span>
                    </span>
                    <span className="text-xs text-text-muted shrink-0">{entry.group}</span>
                  </button>
                </li>
              );
            })
          )}
        </ul>
      </div>
    </div>
  );
}
