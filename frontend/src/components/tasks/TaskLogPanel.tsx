import { useEffect, useMemo, useRef, useState } from "react";
import {
  Bug,
  Check,
  ChevronDown,
  ChevronRight,
  Clipboard,
  Filter,
  ListChecks,
  LoaderCircle,
  Square,
  Terminal,
  X,
} from "lucide-react";

import { API_BASE, apiFetch, cn } from "@/lib/utils";
import { cancelTask, getTaskStatusText, isTerminalTaskStatus } from "@/lib/tasks";
import { useI18n } from "@/lib/i18n-context";
import {
  localizeEventMessage,
  translateLevel,
  translateMode,
  translateStage,
} from "@/lib/i18n";

type StructuredEvent = {
  id: number;
  line: string;
  message: string;
  attemptId: string;
  subtaskLabel: string;
  kind: string;
  level: string;
  stage: string;
  errorCode: string;
  retryIndex: number;
  schemaVersion: number;
};

type Attempt = {
  attempt_id: string;
  ordinal: number;
  effective_mode: string;
  status: string;
  current_stage: string;
  retry_count: number;
  duration_ms: number;
  error_code: string;
};

type RegistrationSummary = {
  total: number;
  completed: number;
  success: number;
  failed: number;
  timed_out: number;
  success_rate: number;
  retry_rate: number;
  timeout_rate: number;
  throughput_per_minute: number;
  p95_duration_ms: number;
  elapsed_seconds: number;
  requested_concurrency: number;
  effective_concurrency: number;
  current_concurrency: number;
  healthy_concurrency: number;
  limiting_resource: string;
  egress_state: string;
  cooldown_seconds: number;
  replacement_count: number;
  top_error_code: string;
  error_codes: Record<string, number>;
  stages: Record<string, number>;
};

type Artifact = {
  id: string;
  attempt_id: string;
  artifact_type: string;
  size_bytes: number;
  sha256: string;
};

type CollapsedEvent = StructuredEvent & { count: number };

const STAGES = [
  "prepare",
  "preflight",
  "auth_begin",
  "email_submit",
  "otp_trigger",
  "otp_wait",
  "otp_submit",
  "profile_create",
  "callback",
  "session_validate",
  "persist",
  "done",
] as const;

const MAX_CLIENT_EVENTS = 800;
const DEFAULT_VISIBLE_KINDS = new Set([
  "state",
  "stage",
  "retry",
  "result",
  "summary",
]);

function numberPercent(value: number) {
  return `${Math.round(Math.max(0, value || 0) * 100)}%`;
}

function durationText(milliseconds: number) {
  const seconds = Math.max(0, Math.round((milliseconds || 0) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${seconds % 60}s`;
}

function eventTone(event: StructuredEvent) {
  if (event.level === "error" || event.errorCode)
    return "border-l-[var(--danger)] text-[var(--danger)]";
  if (event.level === "warning" || event.kind === "retry")
    return "border-l-[var(--warn)] text-[var(--warn)]";
  if (event.kind === "result")
    return "border-l-[var(--ok)] text-[var(--ok)]";
  if (event.kind === "stage" || event.kind === "state")
    return "border-l-[var(--cyan)] text-[var(--text-secondary)]";
  return "border-l-[var(--border-hard)] text-[var(--text-muted)]";
}

function statusTone(status: string) {
  if (status === "succeeded") return "text-[var(--ok)]";
  if (status === "partial") return "text-[var(--warn)]";
  if (status === "failed" || status === "timed_out") return "text-[var(--danger)]";
  if (status === "cancelled" || status === "interrupted") return "text-[var(--warn)]";
  return "text-[var(--info)]";
}

function collapseEvents(events: StructuredEvent[]): CollapsedEvent[] {
  const collapsed: CollapsedEvent[] = [];
  for (const event of events) {
    const last = collapsed[collapsed.length - 1];
    const signature = [
      event.attemptId,
      event.kind,
      event.level,
      event.stage,
      event.errorCode,
      event.message,
    ].join("|");
    const previousSignature = last
      ? [
          last.attemptId,
          last.kind,
          last.level,
          last.stage,
          last.errorCode,
          last.message,
        ].join("|")
      : "";
    if (last && signature === previousSignature) {
      last.count += 1;
      last.id = event.id;
    } else {
      collapsed.push({ ...event, count: 1 });
    }
  }
  return collapsed;
}

export function TaskLogPanel({
  taskId,
  onDone,
  compact = false,
}: {
  taskId: string;
  onDone: (status: string) => void;
  compact?: boolean;
}) {
  const { t, language } = useI18n();
  const [events, setEvents] = useState<StructuredEvent[]>([]);
  const [task, setTask] = useState<any | null>(null);
  const [summary, setSummary] = useState<RegistrationSummary | null>(null);
  const [attempts, setAttempts] = useState<Attempt[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [doneStatus, setDoneStatus] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const [view, setView] = useState<"attempts" | "logs">("attempts");
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [attemptFilter, setAttemptFilter] = useState("");
  const [stageFilter, setStageFilter] = useState("");
  const [levelFilter, setLevelFilter] = useState("");
  const [errorFilter, setErrorFilter] = useState("");
  const seenEventIdsRef = useRef<Set<number>>(new Set());
  const cursorRef = useRef(0);
  const doneRef = useRef(false);
  const onDoneRef = useRef(onDone);
  const sseHealthyRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const logScrollRef = useRef<HTMLDivElement>(null);
  const stickBottomRef = useRef(true);

  useEffect(() => {
    onDoneRef.current = onDone;
  }, [onDone]);

  useEffect(() => {
    if (!taskId) return;
    seenEventIdsRef.current = new Set();
    cursorRef.current = 0;
    doneRef.current = false;
    sseHealthyRef.current = false;
    setEvents([]);
    setTask(null);
    setSummary(null);
    setAttempts([]);
    setArtifacts([]);
    setDoneStatus(null);
    setCanceling(false);
    setCancelError("");
    setAttemptFilter("");
    setStageFilter("");
    setLevelFilter("");
    setErrorFilter("");

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0);
      if (eventId && seenEventIdsRef.current.has(eventId)) return;
      if (eventId) {
        seenEventIdsRef.current.add(eventId);
        cursorRef.current = Math.max(cursorRef.current, eventId);
      }
      if (payload?.line || payload?.message) {
        const detail = payload?.detail || {};
        setEvents((previous) => {
          const next = [
            ...previous,
            {
              id: eventId || previous.length + 1,
              line: String(payload.line || payload.message || ""),
              message: String(payload.message || payload.line || ""),
              attemptId: String(payload.attempt_id || detail.attempt_id || ""),
              subtaskLabel: String(detail.subtask_label || ""),
              kind: String(payload.kind || detail.kind || payload.type || "log"),
              level: String(payload.level || "info"),
              stage: String(payload.stage || detail.stage || ""),
              errorCode: String(payload.error_code || detail.error_code || ""),
              retryIndex: Number(payload.retry_index || detail.retry_index || 0),
              schemaVersion: Number(payload.schema_version || detail.schema_version || 1),
            },
          ];
          return next.length > MAX_CLIENT_EVENTS
            ? next.slice(next.length - MAX_CLIENT_EVENTS)
            : next;
        });
      }
      if (payload?.done && !doneRef.current) {
        doneRef.current = true;
        sseHealthyRef.current = false;
        eventSourceRef.current?.close();
        eventSourceRef.current = null;
        const nextStatus = String(payload.status || "succeeded");
        setDoneStatus(nextStatus);
        onDoneRef.current(nextStatus);
      }
    };

    const syncSnapshot = async () => {
      const [latestTask, latestSummary, latestAttempts, latestArtifacts] =
        await Promise.all([
          apiFetch(`/tasks/${taskId}`),
          apiFetch(`/tasks/${taskId}/summary`).catch(() => null),
          apiFetch(`/tasks/${taskId}/attempts?limit=200`).catch(() => ({ items: [] })),
          apiFetch(`/tasks/${taskId}/artifacts?limit=100`).catch(() => ({ items: [] })),
        ]);
      setTask(latestTask);
      setSummary(latestSummary);
      setAttempts(Array.isArray(latestAttempts?.items) ? latestAttempts.items : []);
      setArtifacts(Array.isArray(latestArtifacts?.items) ? latestArtifacts.items : []);
      if (isTerminalTaskStatus(latestTask.status) && !doneRef.current) {
        pushEvent({ done: true, status: latestTask.status });
      }
    };

    const streamUrl = `${API_BASE}/tasks/${taskId}/logs/stream`;
    const eventSource = new EventSource(streamUrl, { withCredentials: true });
    eventSourceRef.current = eventSource;
    eventSource.onopen = () => {
      sseHealthyRef.current = true;
    };
    eventSource.onmessage = (message) => {
      sseHealthyRef.current = true;
      try {
        pushEvent(JSON.parse(message.data));
      } catch {
        return;
      }
    };
    eventSource.onerror = () => {
      sseHealthyRef.current = false;
    };

    syncSnapshot().catch(() => {});
    const snapshotPoll = window.setInterval(() => {
      if (!doneRef.current) syncSnapshot().catch(() => {});
    }, 1500);
    const fallbackPoll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return;
      try {
        const data = await apiFetch(
          `/tasks/${taskId}/events?since=${cursorRef.current}`,
        );
        for (const item of data.items || []) pushEvent(item);
      } catch {
        return;
      }
    }, 1000);

    return () => {
      sseHealthyRef.current = false;
      eventSource.close();
      if (eventSourceRef.current === eventSource) eventSourceRef.current = null;
      window.clearInterval(snapshotPoll);
      window.clearInterval(fallbackPoll);
    };
  }, [taskId]);

  const currentStatus = doneStatus || task?.status || "running";
  const canCancel = Boolean(task) && ["pending", "claimed", "running"].includes(currentStatus);
  const progress = task?.progress_detail || {};
  const progressTotal = Number(progress.total || summary?.total || 0);
  const progressCurrent = Number(progress.current || summary?.completed || 0);
  const progressPercent = progressTotal
    ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100))
    : 0;
  const requestedConcurrency = Number(
    summary?.requested_concurrency || task?.requested_concurrency || 1,
  );
  const currentConcurrency = Number(
    summary?.current_concurrency ?? task?.current_concurrency ?? 0,
  );
  const cooldownSeconds = Number(
    summary?.cooldown_seconds || task?.cooldown_seconds || 0,
  );
  const replacementCount = Number(
    summary?.replacement_count || task?.replacement_count || 0,
  );

  const filterOptions = useMemo(() => {
    const stages = Array.from(new Set(events.map((event) => event.stage).filter(Boolean)));
    const levels = Array.from(new Set(events.map((event) => event.level).filter(Boolean)));
    const errors = Array.from(new Set(events.map((event) => event.errorCode).filter(Boolean)));
    return { stages, levels, errors };
  }, [events]);

  const visibleEvents = useMemo(() => {
    const filtered = events.filter((event) => {
      if (!showDiagnostics && !DEFAULT_VISIBLE_KINDS.has(event.kind)) {
        if (!event.errorCode && !["warning", "error"].includes(event.level)) return false;
      }
      if (attemptFilter && event.attemptId !== attemptFilter) return false;
      if (stageFilter && event.stage !== stageFilter) return false;
      if (levelFilter && event.level !== levelFilter) return false;
      if (errorFilter && event.errorCode !== errorFilter) return false;
      return true;
    });
    return collapseEvents(filtered);
  }, [attemptFilter, errorFilter, events, levelFilter, showDiagnostics, stageFilter]);

  useEffect(() => {
    if (view !== "logs" || !stickBottomRef.current) return;
    const frame = requestAnimationFrame(() => {
      const element = logScrollRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
    return () => cancelAnimationFrame(frame);
  }, [view, visibleEvents]);

  const clearFilters = () => {
    setAttemptFilter("");
    setStageFilter("");
    setLevelFilter("");
    setErrorFilter("");
  };
  const filtersActive = Boolean(
    attemptFilter || stageFilter || levelFilter || errorFilter,
  );
  const copyLogs = () => {
    navigator.clipboard
      ?.writeText(visibleEvents.map((event) => event.line).join("\n"))
      .catch(() => {});
  };

  const stopTask = async () => {
    if (!canCancel || canceling) return;
    setCanceling(true);
    setCancelError("");
    try {
      const response = await cancelTask(taskId);
      setTask(response);
      const nextStatus = String(response?.status || "cancel_requested");
      setDoneStatus(nextStatus);
      onDoneRef.current(nextStatus);
    } catch {
      setCancelError(t("login.requestFailed"));
    } finally {
      setCanceling(false);
    }
  };

  if (compact) {
    return (
      <div className="task-log-panel flex h-full min-h-0 flex-col overflow-hidden">
        <div className="flex shrink-0 items-center gap-3 border-b-2 border-[var(--border)] px-3 py-2">
          <span className={cn("text-[12px] font-semibold", statusTone(currentStatus))}>
            {getTaskStatusText(currentStatus, language)}
          </span>
          <span className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
            {progressCurrent}/{progressTotal}
          </span>
          <button
            type="button"
            title={t("taskLog.copyLogs")}
            aria-label={t("taskLog.copyLogs")}
            onClick={copyLogs}
            className="xy-icon-btn ml-auto !h-7 !w-7"
          >
            <Clipboard className="h-3.5 w-3.5" />
          </button>
          {canCancel ? (
            <button
              type="button"
              title={t("taskHistory.terminateTitle")}
              aria-label={t("taskHistory.terminateTitle")}
              onClick={stopTask}
              disabled={canceling}
              className="xy-icon-btn !h-7 !w-7 !border-[var(--danger)] !text-[var(--danger)] disabled:opacity-50"
            >
              {canceling ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
            </button>
          ) : null}
        </div>
        <div className="h-1 shrink-0 bg-[var(--bg-input)]">
          <div
            className="h-full bg-[var(--accent)] transition-[width]"
            style={{ width: `${progressPercent}%` }}
          />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2 font-[family-name:var(--font-mono)] text-[11px]">
          {visibleEvents.slice(-200).map((event) => (
            <div
              key={event.id}
              className={cn("mb-1 border-l-2 px-2 py-1", eventTone(event))}
            >
              {localizeEventMessage(event.line, language)}
              {event.count > 1 ? <span className="ml-2 opacity-70">x{event.count}</span> : null}
            </div>
          ))}
        </div>
      </div>
    );
  }

  const metrics = [
    [t("taskLog.successRate"), numberPercent(summary?.success_rate || 0)],
    [t("taskLog.speed"), `${(summary?.throughput_per_minute || 0).toFixed(1)}/m`],
    ["P95", durationText(summary?.p95_duration_ms || 0)],
    [t("taskLog.retryRate"), numberPercent(summary?.retry_rate || 0)],
    [t("taskLog.timeoutRate"), numberPercent(summary?.timeout_rate || 0)],
  ];

  return (
    <div className="task-log-panel flex h-full min-h-0 flex-col overflow-hidden">
      <header className="shrink-0 border-b-2 border-[var(--border)] pb-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className={cn("text-[13px] font-semibold", statusTone(currentStatus))}>
                {getTaskStatusText(currentStatus, language)}
              </span>
              {task?.effective_mode ? (
                <span className="xy-lamp font-[family-name:var(--font-mono)] text-[10px]">
                  {translateMode(task.effective_mode, language)}
                </span>
              ) : null}
              <span className="font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
                {t("taskLog.requestedCurrent", {
                  requested: requestedConcurrency,
                  current: currentConcurrency,
                })}
              </span>
              {cooldownSeconds > 0 ? (
                <span className="xy-lamp xy-lamp-warn font-[family-name:var(--font-mono)] text-[10px]">
                  {t("taskLog.cooldown", { duration: durationText(cooldownSeconds * 1000) })}
                </span>
              ) : null}
              {replacementCount > 0 ? (
                <span className="xy-lamp font-[family-name:var(--font-mono)] text-[10px]">
                  {t("taskLog.replacements", { count: replacementCount })}
                </span>
              ) : null}
            </div>
            <div className="mt-1 font-[family-name:var(--font-mono)] text-[12px] text-[var(--text-secondary)]">
              {progressCurrent}/{progressTotal} · {durationText((summary?.elapsed_seconds || 0) * 1000)}
              {(summary?.top_error_code || task?.top_error_code) ? (
                <span className="ml-3 text-[var(--danger)]">
                  {summary?.top_error_code || task?.top_error_code}
                </span>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-1">
            {canCancel ? (
              <button
                type="button"
                title={t("taskHistory.terminateTitle")}
                onClick={stopTask}
                disabled={canceling}
                className="inline-flex h-8 items-center gap-1.5 border border-[var(--danger)] bg-[var(--danger-soft)] px-2.5 text-[11px] font-semibold text-[var(--danger)] transition-colors hover:bg-[var(--danger)] hover:text-[var(--bg-base)] active:translate-y-px disabled:cursor-wait disabled:opacity-60"
              >
                {canceling ? <LoaderCircle className="h-3.5 w-3.5 animate-spin" /> : <Square className="h-3.5 w-3.5" />}
                {canceling ? t("taskHistory.terminating") : t("taskHistory.terminate")}
              </button>
            ) : null}
            <button
              type="button"
              title={t("taskLog.filters")}
              aria-label={t("taskLog.filters")}
              onClick={() => setFiltersOpen((value) => !value)}
              className={cn("xy-icon-btn !h-8 !w-8", filtersOpen && "!border-[var(--accent)]")}
            >
              <Filter className="h-4 w-4" />
            </button>
            <button
              type="button"
              title={t("taskLog.diagnostics")}
              aria-label={t("taskLog.diagnostics")}
              onClick={() => setShowDiagnostics((value) => !value)}
              className={cn("xy-icon-btn !h-8 !w-8", showDiagnostics && "!border-[var(--warn)]")}
            >
              <Bug className="h-4 w-4" />
            </button>
            <button
              type="button"
              title={t("taskLog.copyLogs")}
              aria-label={t("taskLog.copyLogs")}
              onClick={copyLogs}
              className="xy-icon-btn !h-8 !w-8"
            >
              <Clipboard className="h-4 w-4" />
            </button>
          </div>
        </div>
        {cancelError ? (
          <div className="mt-2 border border-[var(--danger)] bg-[var(--danger-soft)] px-2 py-1.5 text-[11px] text-[var(--danger)]" role="alert">
            {cancelError}
          </div>
        ) : null}
        <div className="mt-3 h-1.5 bg-[var(--bg-input)]">
          <div
            className={cn(
              "h-full transition-[width]",
              currentStatus === "failed" ? "bg-[var(--danger)]" : "bg-[var(--accent)]",
            )}
            style={{ width: `${progressPercent}%` }}
          />
        </div>
      </header>

      {filtersOpen ? (
        <div className="grid shrink-0 grid-cols-2 gap-2 border-b border-[var(--border-soft)] py-2 md:grid-cols-5">
          <select
            aria-label={t("taskLog.attempt")}
            value={attemptFilter}
            onChange={(event) => setAttemptFilter(event.target.value)}
            className="field-select !h-8 !py-0 text-[11px]"
          >
            <option value="">{t("taskLog.allAttempts")}</option>
            {attempts.map((attempt) => (
              <option key={attempt.attempt_id} value={attempt.attempt_id}>
                #{attempt.ordinal}
              </option>
            ))}
          </select>
          <select
            aria-label={t("taskLog.stage")}
            value={stageFilter}
            onChange={(event) => setStageFilter(event.target.value)}
            className="field-select !h-8 !py-0 text-[11px]"
          >
            <option value="">{t("taskLog.allStages")}</option>
            {filterOptions.stages.map((stage) => (
              <option key={stage}>{stage}</option>
            ))}
          </select>
          <select
            aria-label={t("taskLog.level")}
            value={levelFilter}
            onChange={(event) => setLevelFilter(event.target.value)}
            className="field-select !h-8 !py-0 text-[11px]"
          >
            <option value="">{t("taskLog.allLevels")}</option>
            {filterOptions.levels.map((level) => (
              <option key={level}>{level}</option>
            ))}
          </select>
          <select
            aria-label={t("taskLog.errorCode")}
            value={errorFilter}
            onChange={(event) => setErrorFilter(event.target.value)}
            className="field-select !h-8 !py-0 text-[11px]"
          >
            <option value="">{t("taskLog.allErrors")}</option>
            {filterOptions.errors.map((error) => (
              <option key={error}>{error}</option>
            ))}
          </select>
          <button
            type="button"
            onClick={clearFilters}
            disabled={!filtersActive}
            className="inline-flex h-8 items-center justify-center gap-1 border-2 border-[var(--border)] px-2 text-[11px] text-[var(--text-secondary)] disabled:opacity-40"
          >
            <X className="h-3.5 w-3.5" />
            {t("taskLog.clearFilters")}
          </button>
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-y-auto pt-3">
        <section className="grid grid-cols-2 border-y border-[var(--border-soft)] md:grid-cols-5">
          {metrics.map(([label, value]) => (
            <div
              key={label}
              className="border-b border-r border-[var(--border-soft)] px-3 py-2 last:border-r-0 md:border-b-0"
            >
              <div className="text-[10px] text-[var(--text-muted)]">{label}</div>
              <div className="mt-1 font-[family-name:var(--font-mono)] text-[15px] font-semibold text-[var(--text-primary)]">
                {value}
              </div>
            </div>
          ))}
        </section>

        {summary?.total ? (
          <section className="mt-3 border-b border-[var(--border-soft)] pb-3">
            <div className="mb-2 text-[11px] font-semibold text-[var(--text-secondary)]">
              {t("taskLog.stageFunnel")}
            </div>
            <div className="flex gap-1 overflow-x-auto pb-1">
              {STAGES.map((stage) => {
                const reached = Number(summary.stages?.[stage] || 0);
                const active = reached > 0;
                return (
                  <button
                    key={stage}
                    type="button"
                    onClick={() => {
                      setStageFilter(stage);
                      setView("logs");
                    }}
                    className={cn(
                      "min-w-[92px] border-t-2 px-2 py-2 text-left",
                      active
                        ? "border-[var(--accent)] bg-[var(--accent-soft)]"
                        : "border-[var(--border)] bg-[var(--bg-input)] opacity-55",
                    )}
                  >
                    <div className="truncate font-[family-name:var(--font-mono)] text-[9px] text-[var(--text-muted)]">
                      {translateStage(stage, language)}
                    </div>
                    <div className="mt-1 font-[family-name:var(--font-mono)] text-[14px] font-semibold">
                      {reached}
                    </div>
                  </button>
                );
              })}
            </div>
          </section>
        ) : null}

        <div className="sticky top-0 z-[2] mt-3 flex border-y-2 border-[var(--border)] bg-[var(--bg-pane)]">
          <button
            type="button"
            onClick={() => setView("attempts")}
            className={cn(
              "inline-flex h-9 flex-1 items-center justify-center gap-2 border-r border-[var(--border)] text-[11px] font-semibold",
              view === "attempts" ? "bg-[var(--accent-soft)] text-[var(--accent-strong)]" : "text-[var(--text-muted)]",
            )}
          >
            <ListChecks className="h-3.5 w-3.5" />
            {t("taskLog.attempts")} {attempts.length}
          </button>
          <button
            type="button"
            onClick={() => setView("logs")}
            className={cn(
              "inline-flex h-9 flex-1 items-center justify-center gap-2 text-[11px] font-semibold",
              view === "logs" ? "bg-[var(--cyan-soft)] text-[var(--cyan)]" : "text-[var(--text-muted)]",
            )}
          >
            <Terminal className="h-3.5 w-3.5" />
            {t("taskLog.events")} {visibleEvents.length}
          </button>
        </div>

        {view === "attempts" ? (
          <div className="overflow-x-auto">
            {attempts.length ? (
              <table className="w-full min-w-[680px] border-collapse text-left text-[11px]">
                <thead className="text-[var(--text-muted)]">
                  <tr className="border-b border-[var(--border-soft)]">
                    <th className="px-2 py-2 font-medium">#</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.mode")}</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.stage")}</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.status")}</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.retries")}</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.duration")}</th>
                    <th className="px-2 py-2 font-medium">{t("taskLog.errorCode")}</th>
                  </tr>
                </thead>
                <tbody>
                  {attempts.map((attempt) => (
                    <tr
                      key={attempt.attempt_id}
                      onClick={() => {
                        setAttemptFilter(attempt.attempt_id);
                        setView("logs");
                      }}
                      className="cursor-pointer border-b border-[var(--border-soft)] hover:bg-[var(--bg-hover)]"
                    >
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)]">{attempt.ordinal}</td>
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)]">{translateMode(attempt.effective_mode, language)}</td>
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)] text-[var(--cyan)]">{translateStage(attempt.current_stage, language)}</td>
                      <td className={cn("px-2 py-2 font-semibold", statusTone(attempt.status))}>
                        {getTaskStatusText(attempt.status, language)}
                      </td>
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)]">{attempt.retry_count}</td>
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)]">{durationText(attempt.duration_ms)}</td>
                      <td className="px-2 py-2 font-[family-name:var(--font-mono)] text-[var(--danger)]">{attempt.error_code || "-"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <div className="py-10 text-center text-[12px] text-[var(--text-muted)]">
                {t("taskLog.noAttempts")}
              </div>
            )}
            {summary && Object.keys(summary.error_codes || {}).length ? (
              <div className="border-t-2 border-[var(--border)] px-2 py-3">
                <div className="mb-2 text-[11px] font-semibold text-[var(--text-secondary)]">
                  {t("taskLog.errorRanking")}
                </div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(summary.error_codes).map(([code, count]) => (
                    <button
                      key={code}
                      type="button"
                      onClick={() => {
                        setErrorFilter(code);
                        setView("logs");
                      }}
                      className="inline-flex items-center gap-2 border border-[var(--danger)] bg-[var(--danger-soft)] px-2 py-1 font-[family-name:var(--font-mono)] text-[10px] text-[var(--danger)]"
                    >
                      {code}<span>{count}</span>
                    </button>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        ) : (
          <div>
            <div
              ref={logScrollRef}
              onScroll={(event) => {
                const element = event.currentTarget;
                stickBottomRef.current =
                  element.scrollHeight - element.scrollTop - element.clientHeight < 64;
              }}
              className="max-h-[300px] min-h-[180px] overflow-y-auto bg-[var(--bg-input)] p-2 font-[family-name:var(--font-mono)] text-[11px]"
            >
              {visibleEvents.length ? (
                visibleEvents.map((event) => (
                  <div
                    key={event.id}
                    className={cn("mb-1 border-l-2 px-2 py-1.5", eventTone(event))}
                  >
                    <div className="flex items-start gap-2">
                      {event.kind === "result" ? <Check className="mt-0.5 h-3 w-3 shrink-0" /> : null}
                      <span className="min-w-0 flex-1 break-words">{localizeEventMessage(event.line, language)}</span>
                      {event.count > 1 ? (
                        <span className="shrink-0 border border-current px-1 opacity-75">x{event.count}</span>
                      ) : null}
                    </div>
                    {(event.stage || event.errorCode || event.retryIndex) ? (
                      <div className="mt-1 flex flex-wrap gap-2 text-[9px] opacity-70">
                        {event.stage ? <span>{translateStage(event.stage, language)}</span> : null}
                        {event.level ? <span>{translateLevel(event.level, language)}</span> : null}
                        {event.errorCode ? <span>{event.errorCode}</span> : null}
                        {event.retryIndex ? <span>retry {event.retryIndex}</span> : null}
                      </div>
                    ) : null}
                  </div>
                ))
              ) : (
                <div className="flex min-h-[160px] items-center justify-center text-[var(--text-muted)]">
                  {t("taskLog.waiting")}
                </div>
              )}
            </div>

            <button
              type="button"
              onClick={() => setShowDiagnostics((value) => !value)}
              className="flex h-9 w-full items-center gap-2 border-y border-[var(--border)] px-2 text-left text-[11px] text-[var(--text-secondary)]"
            >
              {showDiagnostics ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
              <Bug className="h-3.5 w-3.5 text-[var(--warn)]" />
              {t("taskLog.diagnostics")}
              <span className="ml-auto font-[family-name:var(--font-mono)] text-[10px] text-[var(--text-muted)]">
                {artifacts.length}
              </span>
            </button>
            {showDiagnostics ? (
              <div className="divide-y divide-[var(--border-soft)]">
                {artifacts.length ? artifacts.map((artifact) => (
                  <div key={artifact.id} className="grid grid-cols-[1fr_auto] gap-3 px-2 py-2 text-[10px]">
                    <div className="min-w-0">
                      <div className="font-semibold text-[var(--text-secondary)]">{artifact.artifact_type}</div>
                      <div className="truncate font-[family-name:var(--font-mono)] text-[var(--text-muted)]">
                        #{attempts.find((item) => item.attempt_id === artifact.attempt_id)?.ordinal || "-"} · {artifact.sha256.slice(0, 12)}
                      </div>
                    </div>
                    <span className="font-[family-name:var(--font-mono)] text-[var(--text-muted)]">
                      {Math.ceil(artifact.size_bytes / 1024)} KB
                    </span>
                  </div>
                )) : (
                  <div className="px-2 py-6 text-center text-[11px] text-[var(--text-muted)]">
                    {t("taskLog.noDiagnostics")}
                  </div>
                )}
              </div>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
