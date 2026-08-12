import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Activity, LoaderCircle, Radio, RefreshCw, Square, Trash2 } from "lucide-react";
import { TaskLogPanel } from "@/components/tasks/TaskLogPanel";
import { Button } from "@/components/ui/button";
import type { TranslationKey } from "@/lib/i18n";
import { useI18n } from "@/lib/i18n-context";
import { useLiveJobs, type LiveJob } from "@/lib/live-jobs";
import { cancelTask, getTaskStatusText } from "@/lib/tasks";
import { apiFetch, cn } from "@/lib/utils";

type ServerTask = {
  id: string;
  task_id?: string;
  type?: string;
  status?: string;
  created_at?: string;
  started_at?: string;
  progress_detail?: { current?: number; total?: number };
  success?: number;
  effective_mode?: string;
  effective_concurrency?: number;
  requested_concurrency?: number;
  current_concurrency?: number;
  configured_concurrency_limit?: number;
  peak_active_concurrency?: number;
  healthy_concurrency?: number;
  limiting_resource?: string;
  egress_state?: string;
  cooldown_seconds?: number;
  replacement_count?: number;
  elapsed_seconds?: number;
  throughput_per_minute?: number;
  top_error_code?: string;
};

type JobSource = LiveJob["source"];

const GROUP_ORDER: JobSource[] = ["register", "batch", "other"];

function taskTitle(task: ServerTask, t: (key: "jobs.typeRegister" | "jobs.typeCheck") => string) {
  if (task.type === "register") return t("jobs.typeRegister");
  if (task.type === "account_check_all") return t("jobs.typeCheck");
  return task.type || "Task";
}

function taskSource(task: ServerTask): JobSource {
  if (task.type === "register") return "register";
  if (task.type === "account_check_all") return "batch";
  return "other";
}

function shortDuration(seconds: number) {
  const value = Math.max(0, Math.round(seconds || 0));
  if (value < 60) return `${value}s`;
  return `${Math.floor(value / 60)}m ${value % 60}s`;
}

function pressureLabel(
  task: ServerTask | undefined,
  t: (key: TranslationKey, params?: Record<string, string | number>) => string,
) {
  if (!task) return "";
  if (["succeeded", "partial", "failed", "cancelled", "interrupted", "timed_out"].includes(task.status || "")) {
    return "";
  }
  const cooldown = Number(task.cooldown_seconds || 0);
  if (cooldown > 0) return `${t("jobs.cooldown")} · ${shortDuration(cooldown)}`;
  if (task.egress_state === "half_open") return t("jobs.halfOpen");
  const requested = Number(task.requested_concurrency || 1);
  const current = Number(task.current_concurrency ?? 0);
  if (current < requested) {
    const state = task.egress_state === "open" ? t("jobs.scaledDown") : t("jobs.scalingUp");
    return `${state} · ${t("jobs.healthy")} ${Number(task.healthy_concurrency || current)}`;
  }
  return "";
}

export default function Jobs() {
  const { language, t } = useI18n();
  const {
    jobs,
    activeTaskId,
    setActiveTaskId,
    updateJobStatus,
    dismissJob,
    clearTerminal,
    trackJob,
    isJobHidden,
  } = useLiveJobs();
  const [serverTasks, setServerTasks] = useState<Record<string, ServerTask>>({});
  const [cancellingTaskId, setCancellingTaskId] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const response = await apiFetch("/tasks?limit=80");
      const items: ServerTask[] = Array.isArray(response?.items)
        ? response.items
        : [];
      setServerTasks((previous) => {
        const next = { ...previous };
        for (const item of items) {
          const id = item.id || item.task_id;
          if (id) next[id] = item;
        }
        return next;
      });
      for (const item of items.slice(0, 40)) {
        const id = item.id || item.task_id;
        if (!id || isJobHidden(id)) continue;
        trackJob({
          taskId: id,
          title: taskTitle(item, t),
          source: taskSource(item),
          status: item.status || null,
          startedAt: item.started_at
            ? Date.parse(item.started_at)
            : item.created_at
              ? Date.parse(item.created_at)
              : Date.now(),
        });
      }
    } catch {
      return;
    }
  }, [isJobHidden, t, trackJob]);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, 3000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const visibleJobs = useMemo(
    () =>
      jobs
        .filter(
          (job) =>
            !isJobHidden(job.taskId) &&
            ["register", "batch", "other"].includes(job.source),
        )
        .sort((left, right) => right.startedAt - left.startedAt),
    [isJobHidden, jobs],
  );

  const groups = useMemo(() => {
    const grouped: Record<JobSource, LiveJob[]> = {
      register: [],
      batch: [],
      other: [],
    };
    for (const job of visibleJobs) grouped[job.source].push(job);
    return GROUP_ORDER.filter((source) => grouped[source].length > 0).map(
      (source) => ({
        source,
        label:
          source === "register"
            ? t("jobs.groupRegister")
            : source === "batch"
              ? t("jobs.groupCheck")
              : t("jobs.groupOther"),
        items: grouped[source],
      }),
    );
  }, [t, visibleJobs]);

  const active =
    visibleJobs.find((job) => job.taskId === activeTaskId) ||
    visibleJobs[0] ||
    null;

  const stopJob = async (taskId: string) => {
    if (cancellingTaskId) return;
    setCancellingTaskId(taskId);
    try {
      const response = await cancelTask(taskId);
      setServerTasks((previous) => ({
        ...previous,
        [taskId]: { ...previous[taskId], ...response },
      }));
      updateJobStatus(taskId, String(response?.status || "cancel_requested"));
    } catch {
      // The detail panel continues polling and will surface the authoritative
      // task state; the control is re-enabled immediately for a retry.
    } finally {
      setCancellingTaskId(null);
    }
  };

  useEffect(() => {
    if (active && active.taskId !== activeTaskId) {
      setActiveTaskId(active.taskId);
    }
  }, [active, activeTaskId, setActiveTaskId]);

  const runningCount = visibleJobs.filter(
    (job) =>
      !job.status || ["running", "claimed", "pending"].includes(job.status),
  ).length;
  const registerCount = visibleJobs.filter(
    (job) => job.source === "register",
  ).length;
  const checkCount = visibleJobs.filter((job) => job.source === "batch").length;

  return (
    <div className="xy-page">
      <div className="xy-strip">
        <div>
          <div className="xy-k">{t("jobs.kicker")}</div>
          <h1 className="xy-h1">{t("jobs.title")}</h1>
          <p className="xy-sub">{t("jobs.subtitle")}</p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
            {t("common.refresh")}
          </Button>
          <Button variant="outline" size="sm" onClick={clearTerminal}>
            <Trash2 className="mr-1.5 h-3.5 w-3.5" />
            {t("jobs.clearTerminal")}
          </Button>
          <Link
            to="/accounts/chatgpt?mode=register"
            className="inline-flex h-8 items-center border-2 border-[var(--accent)] bg-[var(--accent)] px-3 text-[12px] font-bold text-[#04140f]"
          >
            {t("jobs.goRegister")}
          </Link>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <span className="xy-lamp xy-lamp-mag">
          {t("jobs.chipRunning")}: {runningCount}
        </span>
        {registerCount > 0 ? (
          <span className="xy-lamp xy-lamp-ok">
            {t("jobs.groupRegister")}: {registerCount}
          </span>
        ) : null}
        {checkCount > 0 ? (
          <span className="xy-lamp xy-lamp-warn">
            {t("jobs.groupCheck")}: {checkCount}
          </span>
        ) : null}
      </div>

      {visibleJobs.length === 0 ? (
        <div className="empty-state-panel py-16">
          <Radio className="mx-auto mb-3 h-6 w-6 text-[var(--text-muted)]" />
          <div className="text-[14px] font-semibold text-[var(--text-primary)]">
            {t("jobs.emptyTitle")}
          </div>
          <p className="mx-auto mt-2 max-w-sm text-[13px]">
            {t("jobs.emptyDesc")}
          </p>
        </div>
      ) : (
        <div className="xy-jobs-layout">
          <aside className="xy-panel xy-jobs-queue">
            <div className="xy-panel-h shrink-0">
              <h2 className="xy-panel-t">{t("jobs.queue")}</h2>
              <span className="xy-lamp">{visibleJobs.length}</span>
            </div>
            <div className="xy-jobs-queue-list">
              {groups.map((group) => (
                <div key={group.source}>
                  <div className="sticky top-0 z-[1] border-b border-[var(--border-soft)] bg-[var(--bg-pane)] px-3 py-1.5 font-[family-name:var(--font-mono)] text-[10px] uppercase tracking-[0.1em] text-[var(--cyan)]">
                    {group.label}
                    <span className="ml-2 text-[var(--text-muted)]">
                      {group.items.length}
                    </span>
                  </div>
                  {group.items.map((job) => (
                    (() => {
                      const serverTask = serverTasks[job.taskId];
                      const status = String(serverTask?.status || job.status || "");
                      const stoppable = ["pending", "claimed", "running"].includes(status);
                      const cancelRequested = status === "cancel_requested";
                      const isCancelling = cancellingTaskId === job.taskId;
                      return (
                    <div
                      key={job.taskId}
                      className={cn(
                        "group relative flex border-b border-[var(--border-soft)] transition-colors",
                        active?.taskId === job.taskId
                          ? "bg-[var(--accent-soft)]"
                          : "hover:bg-[var(--bg-hover)]",
                      )}
                    >
                      <button
                        type="button"
                        onClick={() => setActiveTaskId(job.taskId)}
                        className="min-w-0 flex-1 px-3 py-3 pr-8 text-left"
                      >
                        <div className="flex items-center gap-2">
                          <span className="font-[family-name:var(--font-mono)] text-[10px] text-[var(--accent-strong)]">
                            {serverTasks[job.taskId]?.effective_mode || group.label}
                          </span>
                          <span className="ml-auto text-[10px] font-semibold text-[var(--text-secondary)]">
                            {job.status
                              ? getTaskStatusText(job.status, language)
                              : t("taskStatus.running")}
                          </span>
                        </div>
                        <div className="mt-1 line-clamp-1 text-[12px] font-semibold">
                          {job.title}
                        </div>
                        <div className="mt-1 flex flex-wrap gap-x-2 gap-y-0.5 font-[family-name:var(--font-mono)] text-[9px] text-[var(--text-muted)]">
                          <span>
                            {serverTasks[job.taskId]?.success || 0}/
                            {serverTasks[job.taskId]?.progress_detail?.total || 0}
                          </span>
                          <span>
                            {t("jobs.concurrency")} {serverTasks[job.taskId]?.requested_concurrency || 1}/
                            {serverTasks[job.taskId]?.current_concurrency ?? 0}
                          </span>
                          <span>{(serverTasks[job.taskId]?.throughput_per_minute || 0).toFixed(1)}/m</span>
                          <span>{shortDuration(serverTasks[job.taskId]?.elapsed_seconds || 0)}</span>
                        </div>
                        {pressureLabel(serverTasks[job.taskId], t) ? (
                          <div
                            className={cn(
                              "mt-1 truncate font-[family-name:var(--font-mono)] text-[9px]",
                              Number(serverTasks[job.taskId]?.cooldown_seconds || 0) > 0 || serverTasks[job.taskId]?.egress_state === "open"
                                ? "text-[var(--warning)]"
                                : "text-[var(--accent-strong)]",
                            )}
                          >
                            {pressureLabel(serverTasks[job.taskId], t)}
                          </div>
                        ) : null}
                        {serverTasks[job.taskId]?.top_error_code ? (
                          <div className="mt-1 truncate font-[family-name:var(--font-mono)] text-[9px] text-[var(--danger)]">
                            {serverTasks[job.taskId]?.top_error_code}
                          </div>
                        ) : null}
                      </button>
                      {stoppable || cancelRequested ? (
                        <button
                          type="button"
                          title={cancelRequested ? t("taskStatus.cancel_requested") : t("taskHistory.terminateTitle")}
                          aria-label={cancelRequested ? t("taskStatus.cancel_requested") : t("taskHistory.terminateTitle")}
                          disabled={cancelRequested || isCancelling}
                          onClick={(event) => {
                            event.stopPropagation();
                            stopJob(job.taskId);
                          }}
                          className="absolute right-1 top-1 grid h-7 w-7 place-items-center border border-[var(--danger)] text-[var(--danger)] opacity-0 transition-colors hover:bg-[var(--danger-soft)] focus:opacity-100 group-hover:opacity-100 disabled:cursor-wait disabled:opacity-100"
                        >
                          {cancelRequested || isCancelling ? (
                            <LoaderCircle className="h-3 w-3 animate-spin" />
                          ) : (
                            <Square className="h-3 w-3" />
                          )}
                        </button>
                      ) : (
                        <button
                          type="button"
                          title={t("jobs.dismiss")}
                          aria-label={t("jobs.dismiss")}
                          onClick={(event) => {
                            event.stopPropagation();
                            dismissJob(job.taskId);
                          }}
                          className="absolute right-1 top-1 grid h-7 w-7 place-items-center text-[var(--text-muted)] opacity-0 hover:text-[var(--danger)] focus:opacity-100 group-hover:opacity-100"
                        >
                          <Trash2 className="h-3 w-3" />
                        </button>
                      )}
                    </div>
                      );
                    })()
                  ))}
                </div>
              ))}
            </div>
          </aside>

          <section className="xy-panel xy-jobs-detail">
            <div className="xy-panel-h shrink-0">
              <div className="flex min-w-0 items-center gap-2">
                <Activity className="h-3.5 w-3.5 shrink-0 text-[var(--accent-strong)]" />
                <h2 className="xy-panel-t truncate">
                  {active ? active.title : t("jobs.selectTask")}
                </h2>
              </div>
              {active ? (
                <span className="xy-lamp xy-lamp-accent font-[family-name:var(--font-mono)] text-[10px]">
                  {active.taskId.slice(0, 8)}
                </span>
              ) : null}
            </div>
            <div className="xy-panel-b xy-jobs-detail-body !p-3">
              {active ? (
                <TaskLogPanel
                  taskId={active.taskId}
                  onDone={(status) => updateJobStatus(active.taskId, status)}
                />
              ) : (
                <div className="empty-state-panel m-0 flex h-full items-center justify-center">
                  {t("jobs.selectHint")}
                </div>
              )}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
