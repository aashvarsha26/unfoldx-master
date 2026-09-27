"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { WorkspaceEvent } from "@/lib/types";

/**
 * Output panel: streams each agent's raw text per task as `agent_output`
 * events arrive. One collapsed-by-default stream per task; the newest task
 * auto-expands and auto-scrolls while the user hasn't scrolled away.
 *
 * Pure derived view over the event list, like EventFeed — no extra
 * subscription plumbing: everything already flows through
 * useWorkspaceSocket into `events`.
 */

interface ExecutionResult { type?: string; status?: string | null; stats?: { duration_ms?: number; session_costs?: number; tool_calls?: number; [key: string]: unknown } }\n\ninterface TaskStream {
  taskId: string;
  lines: { id: string; provider: string | null; text: string; simulated: boolean }[];
  simulated: boolean;
}

function asResult(value: unknown): ExecutionResult | undefined {\n  if (!value || typeof value !== "object") return undefined;\n  const v = value as Record<string, unknown>;\n  return { type: typeof v.type === "string" ? v.type : undefined, status: typeof v.status === "string" ? v.status : null, stats: v.stats && typeof v.stats === "object" ? (v.stats as ExecutionResult["stats"]) : undefined };\n}\n\nfunction groupByTask(events: WorkspaceEvent[]): TaskStream[] {
  const byTask = new Map<string, TaskStream>();
  for (const e of events) {
    if (e.event_type !== "agent_output" || !e.task_id) continue;
    const text = typeof e.payload.text === "string" ? e.payload.text : "";
    let stream = byTask.get(e.task_id);
    if (!stream) {
      stream = { taskId: e.task_id, lines: [], simulated: false, resultText: "", files: [] };
      byTask.set(e.task_id, stream);
    }
    if (text) {
      stream.lines.push({
        id: e.id,
        provider: e.provider ?? null,
        text,
        simulated: e.payload.simulated === true,
      });
      stream.simulated = stream.simulated || e.payload.simulated === true;
    }
  }
  return [...byTask.values()].reverse(); // newest task first
}

function TaskStreamView({ stream, defaultOpen }: { stream: TaskStream; defaultOpen: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const scrollRef = useRef<HTMLDivElement>(null);
  const wasAtBottom = useRef(true);

  const lastLen = stream.lines[stream.lines.length - 1]?.text.length ?? 0;\n  const stats = stream.result?.stats;\n  const status = stream.result?.status;

  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !open || !wasAtBottom.current) return;
    el.scrollTo({ top: el.scrollHeight });
  }, [lastLen, open]);

  return (
    <div className="overflow-hidden rounded-md bg-ink-800/70">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-ink-800"
      >
        <span className={`shrink-0 text-state-inactive ${open ? "rotate-90" : ""}`}>▸</span>
        <span className="truncate font-mono text-parchment/90">task {stream.taskId.slice(0, 8)}</span>
        {stream.simulated && (
          <span className="shrink-0 rounded-sm border border-state-warning/40 bg-state-warning/10 px-1 py-0.5 text-[9px] uppercase tracking-wide text-state-warning">
            simulated
          </span>
        )}
        <span className="ml-auto shrink-0 text-[10px] tabular-nums text-state-inactive">
          {stream.lines.length} output{stream.lines.length === 1 ? "" : "s"}
        </span>
      </button>
      {open && (
        <div className="border-t border-ink-700">
          <div className="px-3 py-3">
            <div className="mb-2 flex items-center gap-2">
              <span className={status === "success" ? "text-state-success" : "text-state-error"}>
                {status === "success" ? "✓ Completed" : "Execution"}
              </span>
              {typeof stats?.duration_ms === "number" && <span className="text-[10px] text-muted">{(stats.duration_ms / 1000).toFixed(2)}s</span>}
              {typeof stats?.tool_calls === "number" && <span className="text-[10px] text-muted">{stats.tool_calls} tool call{stats.tool_calls === 1 ? "" : "s"}</span>}
              {typeof stats?.session_costs === "number" && <span className="text-[10px] text-muted">${stats.session_costs.toFixed(4)}</span>}
            </div>
            {stream.resultText && <p className="text-sm leading-relaxed text-parchment/90">{stream.resultText}</p>}
            {stream.files.length > 0 && <div className="mt-3"><div className="mb-1 text-[9px] font-semibold uppercase tracking-widest text-muted">Files changed</div>{stream.files.map(file => <div key={file} className="rounded bg-ink-900 px-2 py-1 font-mono text-[11px] text-parchment/80">✓ {file}</div>)}</div>}
          </div>
          <details className="border-t border-ink-700 px-3 py-2">
            <summary className="cursor-pointer text-[10px] font-medium text-muted hover:text-parchment">Execution details</summary>
            <div
              ref={scrollRef}
          className="max-h-64 overflow-y-auto border-t border-ink-700 px-3 py-2"
          onScroll={(e) => {
            const el = e.currentTarget;
            wasAtBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 48;
          }}
        >
          {stream.lines.map((line, i) => (
            <div key={line.id} className="mb-2 last:mb-0">
              <div className="flex items-center gap-2 text-[10px] text-muted/60">
                <span className="font-mono">
                  {i + 1}. {line.provider ?? "agent"}
                </span>
                {line.simulated && <span className="text-state-warning">simulated</span>}
              </div>
              <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-parchment/85">
                {line.text}
              </pre>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function OutputPanel({ events }: { events: WorkspaceEvent[] }) {
  const streams = useMemo(() => groupByTask(events), [events]);

  return (
    <section>
      <h2 className="mb-2 text-sm font-medium text-muted">Output</h2>
      {streams.length === 0 ? (
        <p className="px-3 py-4 text-[11px] text-muted/60">
          No agent output yet — agent text appears here as each subtask completes.
        </p>
      ) : (
        <div className="flex flex-col gap-2">
          {streams.map((s, i) => (
            <TaskStreamView key={s.taskId} stream={s} defaultOpen={i === 0} />
          ))}
        </div>
      )}
    </section>
  );
}
