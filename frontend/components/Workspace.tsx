"use client";

import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import Image from "next/image";
import { useWorkspaceSocket } from "@/lib/useWorkspaceSocket";
import { buildHub, type HubSelection } from "@/lib/graph";
import { useOrchestration } from "@/lib/useOrchestration";
import { useAuth } from "@/lib/useAuth";
import { ConnectionStatus } from "./ConnectionStatus";
import { AuthBar } from "./AuthBar";
import { ChainBadge } from "./ChainBadge";
import { BudgetStrip } from "./BudgetStrip";
import { EventFeed } from "./EventFeed";
import { AskBob } from "./AskBob";
import { AgentRail } from "./AgentRail";
import { PluginsPanel } from "./PluginsPanel";
import { ProvidersPanel } from "./ProvidersPanel";
import { Inspector } from "./Inspector";
import { OutputPanel } from "./OutputPanel";
import type { CanvasPluginPanel, PluginId } from "@/lib/plugins";

// React Flow measures the DOM, so it only mounts on the client.
const HubCanvas = dynamic(() => import("./canvas/HubCanvas").then((m) => m.HubCanvas), {
  ssr: false,
  loading: () => (
    <div className="flex h-full min-h-[560px] w-full items-center justify-center rounded-xl bg-ink-900 text-sm text-muted">
      Loading canvas…
    </div>
  ),
});

const WORKSPACE_ID = process.env.NEXT_PUBLIC_WORKSPACE_ID ?? "demo-workspace";

export function Workspace() {
  const auth = useAuth(WORKSPACE_ID);
  const { events, budgets, connectionState } = useWorkspaceSocket(WORKSPACE_ID, auth.token);
  const { overrides, queue, send } = useOrchestration(WORKSPACE_ID);
  const [selection, setSelection] = useState<HubSelection>(null);
  const [canvasPlugins, setCanvasPlugins] = useState<CanvasPluginPanel[]>([]);

  const hub = useMemo(() => buildHub(events, overrides), [events, overrides]);
  const ledgerAgents = useMemo(() => [hub.bob, ...hub.agents], [hub]);
  const demoEnabled =
    process.env.NEXT_PUBLIC_MOCK !== "1" &&
    connectionState === "connected" &&
    auth.permissions.canControl;
  const openPlugin = (plugin: PluginId) => {
    setCanvasPlugins((current) => [
      ...current,
      { id: `plugin-${crypto.randomUUID()}`, plugin },
    ]);
  };

  /**
   * A run is in flight from its `task_submitted` until a terminal event. The
   * orchestrator does not emit `task_completed` when a task fails — it appends an
   * `error` carrying the final status — so both have to be treated as "settled",
   * otherwise the toggle sticks on "Stop Demo" for a task that already died and
   * the stop call is rejected with 409.
   */
  const demoRunning = useMemo(() => {
    const TERMINAL = new Set(["failed", "completed", "stopped", "cancelled"]);
    let running = false;
    for (const e of events) {
      if (e.event_type === "task_submitted") {
        running = true;
        continue;
      }
      const status = e.payload?.status;
      if (
        e.event_type === "task_completed" ||
        (e.event_type === "error" && typeof status === "string" && TERMINAL.has(status))
      ) {
        running = false;
      }
    }
    return running;
  }, [events]);

  return (
    <main className="mx-auto flex min-h-screen w-full max-w-[1920px] flex-col gap-4 px-4 py-4 sm:px-6 2xl:px-10 2xl:py-6">
      {/* ── Header ─────────────────────────────────────────────── */}
      <header className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="flex items-center gap-3">
          {/* Brand mark */}
          <Image
            src="/logos/unfoldx.png"
            alt=""
            aria-hidden
            width={36}
            height={36}
            unoptimized
            className="h-9 w-9 shrink-0 rounded-xl object-cover shadow-[0_0_0_1px_rgba(255,255,255,0.08)]"
          />
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-parchment">UnfoldX</h1>
            <p className="font-mono text-[10px] text-muted">workspace/{WORKSPACE_ID}</p>
          </div>
        </div>

        <div className="flex flex-col items-end gap-2">
          <AuthBar
            status={auth.status}
            user={auth.user}
            role={auth.role}
            busy={auth.busy}
            error={auth.error}
            onLogin={auth.login}
            onRegister={auth.register}
            onLogout={auth.logout}
          />
          <div className="flex items-center gap-3">
            <ConnectionStatus state={connectionState} />
            <span className="hidden text-[11px] tabular-nums text-muted sm:inline">
              {hub.agents.length + 1} agents · {hub.subtasks.length} routes · {queue.length} controls
            </span>
          </div>
        </div>
      </header>

      {/* ── Main grid ─────────────────────────────────────────── */}
      <div className="grid flex-1 grid-cols-1 items-start gap-3 xl:grid-cols-[220px_minmax(0,1fr)_368px]">

        {/* Left column — Agent rail + Budget */}
        <div className="min-w-0 xl:sticky xl:top-4 xl:max-h-[calc(100vh-2rem)] xl:overflow-y-auto">
          <AgentRail
            agents={ledgerAgents}
            selection={selection}
            permissions={auth.permissions}
            demoEnabled={demoEnabled}
            demoRunning={demoRunning}
            onSelect={setSelection}
            onAction={send}
          />
          <PluginsPanel onOpenPlugin={openPlugin} />
          <ProvidersPanel workspaceId={WORKSPACE_ID} canApprove={auth.permissions.canApprove} />
          <section className="mt-3 overflow-hidden rounded-xl bg-ink-900">
            <div className="border-b border-ink-700 px-4 py-3">
              <h2 className="text-[11px] font-semibold uppercase tracking-widest text-state-info">Budget ledger</h2>
            </div>
            <div className="p-3">
              <BudgetStrip budgets={budgets} agents={ledgerAgents} />
            </div>
          </section>
        </div>

        {/* Center column — Canvas + Ask Bob */}
        <section className="flex min-w-0 flex-col gap-3">
          <div className="relative h-[62vh] min-h-[520px] overflow-hidden rounded-xl border border-white/[0.05] bg-ink-950 shadow-[0_12px_36px_-28px_rgba(0,0,0,0.8)]">
            <HubCanvas
              model={hub}
              permissions={auth.permissions}
              onAction={send}
              selection={selection}
              onSelectionChange={setSelection}
              pluginPanels={canvasPlugins}
              workspaceId={WORKSPACE_ID}
            />
            <div className="pointer-events-none absolute right-3 top-3 z-10">
              <ChainBadge workspaceId={WORKSPACE_ID} eventsLen={events.length} />
            </div>
          </div>
          <AskBob onAction={send} events={events} permissions={auth.permissions} />
        </section>

        {/* Right column — Inspector */}
        <Inspector agents={ledgerAgents} selection={selection} subtasks={hub.subtasks} events={events} workspaceId={WORKSPACE_ID} />
      </div>

      {/* ── Event log (collapsible) ─────────────────────────── */}
      <details className="overflow-hidden rounded-xl bg-ink-900">
        <summary className="flex cursor-pointer select-none items-center gap-2 px-4 py-3 text-xs font-medium text-muted transition-colors hover:text-parchment">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden className="shrink-0 text-muted">
            <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          View execution details
        </summary>
        <div className="border-t border-ink-700 space-y-4 p-4">
          <OutputPanel events={events} />
          <div className="border-t border-ink-700 pt-4">
            <EventFeed events={events} />
          </div>
        </div>
      </details>
    </main>
  );
}
