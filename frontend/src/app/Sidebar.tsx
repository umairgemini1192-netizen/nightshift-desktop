import { NavLink, useLocation } from "react-router-dom";
import { Badge, Icon } from "../design";
import type { ModuleManifest, ModuleSection } from "./moduleTypes";
import { getModulesBySection, MODULES } from "./registry";
import { useAppStore } from "./store";

const MAIN_SECTIONS: readonly { id: ModuleSection; label: string }[] = [
  { id: "core", label: "Core" },
  { id: "build", label: "Build" },
  { id: "tools", label: "Tools" },
];

export function Sidebar() {
  const collapsed = useAppStore((s) => s.sidebarCollapsed);
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const snapshot = useAppStore((s) => s.snapshot);
  const location = useLocation();

  const settingsModules = getModulesBySection(MODULES, "settings");

  const renderItem = (mod: ModuleManifest) => {
    const badge = mod.statusBadge?.(snapshot) ?? null;
    const to = location.search ? `${mod.route}${location.search}` : mod.route;

    return (
      <NavLink
        key={mod.id}
        to={to}
        end={mod.route === "/"}
        title={collapsed ? mod.title : undefined}
        className={({ isActive }) =>
          `group flex items-center gap-3 rounded-pill px-3 py-2 text-sm font-medium transition-all duration-fast ease-out ${
            isActive
              ? "bg-accent-primary-soft text-accent-primary shadow-glow-accent"
              : "text-text-secondary hover:bg-surface-raised hover:text-text-primary"
          } ${collapsed ? "justify-center px-2" : "justify-between"}`
        }
      >
        <span className="flex items-center gap-3 min-w-0">
          <Icon name={mod.icon} size={20} className="shrink-0" />
          {!collapsed && <span className="truncate">{mod.title}</span>}
        </span>
        {!collapsed && badge && (
          <Badge tone={badge.tone} className="shrink-0">
            {badge.label}
          </Badge>
        )}
      </NavLink>
    );
  };

  return (
    <aside
      aria-label="Sidebar"
      className={`ns-glass z-10 flex shrink-0 flex-col justify-between border-r border-border-subtle transition-all duration-base ease-out ${
        collapsed ? "w-sidebar-collapsed" : "w-sidebar"
      }`}
    >
      <nav aria-label="Main navigation" className="flex flex-1 flex-col gap-5 overflow-y-auto p-3">
        {MAIN_SECTIONS.map((section) => {
          const items = getModulesBySection(MODULES, section.id);
          if (items.length === 0) return null;
          return (
            <div key={section.id} className="flex flex-col gap-1">
              {!collapsed && (
                <p className="px-3 pb-1 text-xs font-medium uppercase tracking-wider text-text-muted">
                  {section.label}
                </p>
              )}
              {items.map(renderItem)}
            </div>
          );
        })}
      </nav>

      <div className="flex flex-col gap-2 border-t border-border-subtle p-3">
        {settingsModules.map(renderItem)}

        <button
          type="button"
          onClick={toggleSidebar}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          className={`flex items-center gap-3 rounded-md px-3 py-2 text-sm text-text-secondary transition-colors duration-fast hover:bg-surface-raised hover:text-text-primary ${
            collapsed ? "justify-center px-2" : ""
          }`}
        >
          <Icon name={collapsed ? "ChevronRight" : "ChevronLeft"} size={16} />
          {!collapsed && <span>Collapse</span>}
        </button>
      </div>
    </aside>
  );
}
