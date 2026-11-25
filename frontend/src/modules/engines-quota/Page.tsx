import { useCallback, useEffect, useState } from "react";
import {
  Badge,
  Button,
  EngineCard,
  type EngineAvailability,
  GlassCard,
  Icon,
  PageHeader,
  Select,
} from "../../design";
import type { EngineItem, RouterDecision } from "../../bridge/types";
import { useAppStore } from "../../app/store";

const EFFORT_OPTIONS = [
  { value: "low", label: "Low effort" },
  { value: "medium", label: "Medium effort (default)" },
  { value: "high", label: "High effort" },
];

const LADDER_CATEGORIES = [
  { id: "backend", label: "Backend implementation" },
  { id: "ui", label: "UI & frontend design" },
  { id: "review", label: "Risk-based review gate" },
  { id: "ci_fix", label: "CI self-healing rounds" },
] as const;

type CategoryId = (typeof LADDER_CATEGORIES)[number]["id"];

interface LedgerChartItem {
  runner: string;
  name: string;
  tokens: number;
  costEstimate: string;
  pct: number;
}

export default function EnginesQuotaPage() {
  const bridge = useAppStore((s) => s.bridge);
  const pushToast = useAppStore((s) => s.pushToast);

  const [engines, setEngines] = useState<EngineItem[]>([]);
  const [ladders, setLadders] = useState<Record<string, string[]>>({
    backend: ["agy", "gemini", "codex", "claude"],
    ui: ["agy", "gemini", "claude"],
    review: ["gemini", "codex", "claude"],
    ci_fix: ["codex", "gemini", "claude"],
  });
  const [activeCategory, setActiveCategory] = useState<CategoryId>("backend");
  const [pinnedNote, setPinnedNote] = useState(
    "Claude is pinned last due to token cost; reserved for complex failovers.",
  );
  const [decisions, setDecisions] = useState<RouterDecision[]>([]);
  const [chartData, setChartData] = useState<LedgerChartItem[]>([]);
  const [loading, setLoading] = useState(false);

  const loadData = useCallback(async () => {
    try {
      setLoading(true);
      const [engRes, ladderRes, ledgerRes] = await Promise.all([
        bridge.call<EngineItem[]>("engines.list"),
        bridge.call<{ ladders: Record<string, string[]>; pinnedNote: string }>("engines.getLadders"),
        bridge.call<{ routerDecisions: RouterDecision[]; chartData: LedgerChartItem[] }>("engines.getLedger"),
      ]);

      setEngines(engRes ?? []);
      if (ladderRes?.ladders) {
        setLadders(ladderRes.ladders);
      }
      if (ladderRes?.pinnedNote) {
        setPinnedNote(ladderRes.pinnedNote);
      }
      setDecisions(ledgerRes?.routerDecisions ?? []);
      setChartData(ledgerRes?.chartData ?? []);
    } catch (err) {
      pushToast({
        title: "Could not load engines",
        description: String(err),
        tone: "danger",
      });
    } finally {
      setLoading(false);
    }
  }, [bridge, pushToast]);

  useEffect(() => {
    void loadData();
  }, [loadData]);

  const handleCheckLogins = async () => {
    try {
      await bridge.call("engines.checkLogins");
      pushToast({
        title: "Logins verified",
        description: "CLI authentication tokens and readiness refreshed.",
        tone: "success",
      });
      void loadData();
    } catch (err) {
      pushToast({
        title: "Login check failed",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const handleUpdateEffort = async (id: string, effort: "low" | "medium" | "high") => {
    setEngines((prev) =>
      prev.map((eng) => (eng.id === id ? { ...eng, effort } : eng)),
    );
    try {
      await bridge.call("engines.update", { id, effort });
      pushToast({
        title: "Effort level updated",
        description: `Set ${id} effort level to ${effort}.`,
        tone: "info",
      });
    } catch (err) {
      pushToast({
        title: "Could not update effort",
        description: String(err),
        tone: "danger",
      });
    }
  };

  const moveLadderEngine = (category: string, fromIndex: number, toIndex: number) => {
    const list = [...(ladders[category] ?? [])];
    if (fromIndex < 0 || toIndex < 0 || fromIndex >= list.length || toIndex >= list.length) {
      return;
    }

    // Rule: Claude cannot be moved above the last position
    if (list[fromIndex] === "claude" && toIndex < list.length - 1) {
      pushToast({
        title: "Claude must stay last",
        description: pinnedNote,
        tone: "warning",
      });
      return;
    }

    const [item] = list.splice(fromIndex, 1);
    list.splice(toIndex, 0, item);

    // Guarantee Claude stays at the end
    const sanitized = list.filter((r) => r !== "claude");
    sanitized.push("claude");

    const updated = { ...ladders, [category]: sanitized };
    setLadders(updated);
    void bridge.call("engines.setLadders", { ladders: updated });
  };

  const engineNameMap: Record<string, string> = {
    agy: "Antigravity",
    gemini: "Gemini CLI",
    codex: "Codex CLI",
    claude: "Claude Code",
  };

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="AI Engines & Quota"
        description="Manage engine tiers, login health, cooldowns, and estimated budget usage."
        action={
          <Button
            variant="secondary"
            icon="RefreshCw"
            loading={loading}
            onClick={() => void handleCheckLogins()}
          >
            Check tool logins
          </Button>
        }
      />

      {/* EngineCard Grid: 4 core engines */}
      <section aria-labelledby="engines-heading" className="flex flex-col gap-3">
        <h2 id="engines-heading" className="text-sm font-semibold uppercase tracking-wider text-text-secondary">
          Available AI Engines
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {engines.map((eng) => (
            <div key={eng.id} className="flex flex-col gap-3">
              <EngineCard
                name={eng.name}
                model={eng.model}
                effort={eng.effort || "medium"}
                state={eng.state as EngineAvailability}
                coolingUntil={eng.coolingUntil ?? undefined}
                quotaUsedPct={eng.quotaUsedPct}
              />

              {/* Effort Selector per engine - Default medium */}
              <div className="ns-glass flex flex-col gap-2 rounded-lg p-3">
                <Select
                  id={`effort-${eng.id}`}
                  label={`Effort level for ${eng.name}`}
                  value={eng.effort || "medium"}
                  onChange={(val) =>
                    void handleUpdateEffort(eng.id, val as "low" | "medium" | "high")
                  }
                  options={EFFORT_OPTIONS}
                  className="text-xs"
                />

                {/* Skill Score Chips */}
                {eng.skills && (
                  <div className="mt-1 flex flex-wrap items-center gap-1">
                    {Object.entries(eng.skills).map(([skill, score]) => (
                      <span
                        key={skill}
                        className="rounded bg-surface-elevated px-1.5 py-0.5 text-xs text-text-muted"
                      >
                        {skill}: <strong className="text-text-primary">{score}</strong>
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
        {/* Failover Ladder Editor per category */}
        <section aria-labelledby="ladder-heading" className="lg:col-span-6">
          <GlassCard title="Category Failover Ladders" icon="Layers">
            {/* Category tabs */}
            <div className="flex flex-wrap gap-1 border-b border-border-subtle pb-3">
              {LADDER_CATEGORIES.map((cat) => {
                const isActive = activeCategory === cat.id;
                return (
                  <button
                    key={cat.id}
                    type="button"
                    onClick={() => setActiveCategory(cat.id)}
                    className={`rounded-md px-3 py-1.5 text-xs font-medium transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-focus-ring ${
                      isActive
                        ? "bg-accent-primary-soft text-accent-primary font-semibold"
                        : "text-text-secondary hover:bg-surface-elevated hover:text-text-primary"
                    }`}
                  >
                    {cat.label}
                  </button>
                );
              })}
            </div>

            {/* Explanation Note for Claude pinned last */}
            <div className="mt-3 flex items-start gap-2 rounded-lg border border-status-warning/40 bg-status-warning/10 p-3 text-xs text-text-primary">
              <Icon name="Info" size={16} className="mt-0.5 shrink-0 text-status-warning" />
              <span>{pinnedNote}</span>
            </div>

            {/* Ladder reorder list */}
            <div className="mt-3 flex flex-col gap-2">
              {(ladders[activeCategory] ?? []).map((runnerKey, idx, arr) => {
                const isClaude = runnerKey === "claude";
                return (
                  <div
                    key={runnerKey}
                    className="ns-glass flex items-center justify-between rounded-lg p-2.5 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono font-semibold text-text-muted">
                        #{idx + 1}
                      </span>
                      <span className="font-medium text-text-primary">
                        {engineNameMap[runnerKey] || runnerKey}
                      </span>
                      {isClaude && <Badge tone="warning">Pinned Last</Badge>}
                    </div>

                    {/* Up / Down buttons */}
                    <div className="flex items-center gap-1">
                      <button
                        type="button"
                        disabled={idx === 0 || isClaude}
                        onClick={() => moveLadderEngine(activeCategory, idx, idx - 1)}
                        aria-label={`Move ${runnerKey} up`}
                        className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-20"
                      >
                        <Icon name="ChevronUp" size={16} />
                      </button>
                      <button
                        type="button"
                        disabled={idx === arr.length - 1 || isClaude}
                        onClick={() => moveLadderEngine(activeCategory, idx, idx + 1)}
                        aria-label={`Move ${runnerKey} down`}
                        className="rounded p-1 text-text-muted hover:text-text-primary disabled:opacity-20"
                      >
                        <Icon name="ChevronDown" size={16} />
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </GlassCard>
        </section>

        {/* Router decisions list */}
        <section aria-labelledby="router-heading" className="lg:col-span-6">
          <GlassCard title="Router Decisions" icon="Radar">
            <p className="text-xs text-text-secondary">
              Real-time engine selections made by the failover router tonight.
            </p>
            <div className="mt-3 flex flex-col gap-2">
              {decisions.length > 0 ? (
                decisions.map((dec) => (
                  <div
                    key={dec.id}
                    className="flex flex-col gap-1 rounded bg-surface-elevated/40 p-2.5 text-xs"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="font-semibold text-text-primary">{dec.step}</span>
                      <span className="font-mono text-text-muted">{dec.timestamp}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <Badge tone="info">{dec.chosen}</Badge>
                      <span className="text-text-secondary line-clamp-1">{dec.reason}</span>
                    </div>
                  </div>
                ))
              ) : (
                <p className="text-xs text-text-muted">No routing decisions recorded yet tonight.</p>
              )}
            </div>
          </GlassCard>
        </section>
      </div>

      {/* Quota & Token Ledger - Labelled Estimate */}
      <section aria-labelledby="ledger-heading">
        <GlassCard
          title="Quota & Token Ledger (estimates)"
          icon="Cpu"
          footer={
            <p className="text-xs text-text-muted">
              Note: Token counts and cost calculations are estimates based on model metadata and CLI output headers.
            </p>
          }
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {chartData.map((item) => (
              <div
                key={item.runner}
                className="flex flex-col gap-2 rounded-lg bg-surface-elevated/30 p-3"
              >
                <div className="flex items-center justify-between">
                  <span className="font-medium text-text-primary">{item.name}</span>
                  <Badge tone={item.pct >= 90 ? "danger" : item.pct >= 70 ? "warning" : "info"}>
                    {item.pct}% tonight (est.)
                  </Badge>
                </div>

                <div className="flex items-baseline justify-between">
                  <span className="font-mono text-sm font-semibold text-text-primary ns-tabular">
                    {item.tokens.toLocaleString()} tokens
                  </span>
                  <span className="text-xs text-text-secondary">{item.costEstimate}</span>
                </div>

                {/* Progress track */}
                <div className="h-2 w-full overflow-hidden rounded-full bg-surface-raised">
                  <div
                    className={`h-full transition-all ${
                      item.pct >= 90
                        ? "bg-status-danger"
                        : item.pct >= 70
                          ? "bg-status-warning"
                          : "bg-accent-primary"
                    }`}
                    style={{ width: `${Math.min(item.pct, 100)}%` }}
                  />
                </div>
              </div>
            ))}
          </div>
        </GlassCard>
      </section>
    </div>
  );
}
