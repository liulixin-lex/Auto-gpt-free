import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { API_BASE, apiFetch } from "@/lib/utils";
import { getTaskStatusText, isTerminalTaskStatus } from "@/lib/tasks";
import { useI18n } from "@/lib/i18n-context";

type LogEvent = {
  id: number;
  line: string;
  subtaskId: string;
  subtaskLabel: string;
};

type LogGroup = {
  id: string;
  label: string;
  events: LogEvent[];
};

const MAIN_GROUP_ID = "__main__";
/** Cap in-memory log lines so long-lived auto_ops never freezes the UI. */
const MAX_CLIENT_EVENTS = 500;
const STICK_THRESHOLD_PX = 64;

function classifyLine(line: string): string {
  if (line.includes("✓") || line.includes("成功") || line.includes("完成"))
    return "text-[var(--ok)]";
  if (
    line.includes("✗") ||
    line.includes("失败") ||
    line.includes("错误") ||
    line.includes("异常")
  )
    return "text-[var(--danger)]";
  if (line.includes("──") || line.includes("进度") || line.includes("探测开始"))
    return "text-[var(--cyan)]";
  if (line.includes("警告") || line.includes("跳过") || line.includes("清扫"))
    return "text-[var(--warn)]";
  return "text-[var(--text-secondary)]";
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
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [task, setTask] = useState<any | null>(null);
  const [doneStatus, setDoneStatus] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const seenEventIdsRef = useRef<Set<number>>(new Set());
  const cursorRef = useRef(0);
  const doneRef = useRef(false);
  const onDoneRef = useRef(onDone);
  const sseHealthyRef = useRef(false);
  const eventSourceRef = useRef<EventSource | null>(null);

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
    setDoneStatus(null);
    setCollapsed({});

    const pushEvent = (payload: any) => {
      const eventId = Number(payload?.id || 0);
      if (eventId && seenEventIdsRef.current.has(eventId)) return;
      if (eventId) {
        seenEventIdsRef.current.add(eventId);
        cursorRef.current = Math.max(cursorRef.current, eventId);
      }
      if (payload?.line) {
        const detail = payload?.detail || {};
        setEvents((prev) => {
          const next = [
            ...prev,
            {
              id: eventId || prev.length + 1,
              line: String(payload.line),
              subtaskId: String(detail?.subtask_id || ""),
              subtaskLabel: String(detail?.subtask_label || ""),
            },
          ];
          // Rolling client buffer — drop oldest when over cap.
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
        const nextStatus = payload.status || "succeeded";
        setDoneStatus(nextStatus);
        onDoneRef.current(nextStatus);
      }
    };

    const syncTask = async () => {
      const latest = await apiFetch(`/tasks/${taskId}`);
      setTask(latest);
      if (isTerminalTaskStatus(latest.status) && !doneRef.current) {
        pushEvent({ done: true, status: latest.status });
      }
    };

    // Prefer cookie session for SSE (set on login); also pass token query for
    // environments where EventSource cannot set Authorization headers.
    const token =
      typeof localStorage !== "undefined"
        ? localStorage.getItem("_auth_token") || ""
        : "";
    const streamUrl = token
      ? `${API_BASE}/tasks/${taskId}/logs/stream?token=${encodeURIComponent(token)}`
      : `${API_BASE}/tasks/${taskId}/logs/stream`;
    const es = new EventSource(streamUrl, { withCredentials: true });
    eventSourceRef.current = es;
    es.onopen = () => {
      sseHealthyRef.current = true;
    };
    es.onmessage = (e) => {
      sseHealthyRef.current = true;
      pushEvent(JSON.parse(e.data));
    };
    es.onerror = () => {
      if (doneRef.current) {
        es.close();
        if (eventSourceRef.current === es) {
          eventSourceRef.current = null;
        }
        return;
      }
      sseHealthyRef.current = false;
    };

    syncTask().catch(() => {});

    const progressPoll = window.setInterval(() => {
      if (doneRef.current) return;
      syncTask().catch(() => {});
    }, 1500);

    const fallbackPoll = window.setInterval(async () => {
      if (doneRef.current || sseHealthyRef.current) return;
      try {
        const data = await apiFetch(
          `/tasks/${taskId}/events?since=${cursorRef.current}`,
        );
        for (const item of data.items || []) {
          pushEvent(item);
        }
      } catch {
        // passive
      }
    }, 1000);

    return () => {
      sseHealthyRef.current = false;
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      window.clearInterval(progressPoll);
      window.clearInterval(fallbackPoll);
    };
  }, [taskId]);

  const groups: LogGroup[] = useMemo(() => {
    const map = new Map<string, LogGroup>();
    map.set(MAIN_GROUP_ID, {
      id: MAIN_GROUP_ID,
      label: t("taskLog.mainGroup"),
      events: [],
    });
    for (const ev of events) {
      const key = ev.subtaskId || MAIN_GROUP_ID;
      if (!map.has(key)) {
        map.set(key, {
          id: key,
          label: ev.subtaskLabel || key,
          events: [],
        });
      }
      const group = map.get(key)!;
      group.events.push(ev);
      if (key !== MAIN_GROUP_ID && ev.subtaskLabel) {
        group.label = ev.subtaskLabel;
      }
    }
    return Array.from(map.values());
  }, [events, t]);

  const toggleGroup = (id: string) => {
    setCollapsed((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const currentStatus = doneStatus || task?.status || "running";
  const progress = task?.progress_detail || {};
  const progressTotal = Number(progress.total || 0);
  const progressCurrent = Number(progress.current || 0);
  const progressPercent =
    progressTotal > 0
      ? Math.min(100, Math.round((progressCurrent / progressTotal) * 100))
      : 0;
  const errorText =
    task?.error || (Array.isArray(task?.errors) ? task.errors[0] : "");
  const statusTone =
    currentStatus === "succeeded"
      ? "border-[var(--ok)] bg-[var(--ok-soft)] text-[var(--ok)]"
      : currentStatus === "failed"
        ? "border-[var(--danger)] bg-[var(--danger-soft)] text-[var(--danger)]"
        : currentStatus === "cancelled" || currentStatus === "interrupted"
          ? "border-[var(--warn)] bg-[var(--warn-soft)] text-[var(--warn)]"
          : "border-[var(--info)] bg-[var(--info-soft)] text-[var(--info)]";

  const copyLogs = () => {
    navigator.clipboard
      ?.writeText(events.map((ev) => ev.line).join("\n"))
      .catch(() => {});
  };

  const compactScrollRef = useRef<HTMLDivElement>(null);
  const fullScrollRef = useRef<HTMLDivElement>(null);
  const stickBottomRef = useRef(true);
  const lastEventIdRef = useRef(0);
  const userScrollingRef = useRef(false);
  const scrollRafRef = useRef(0);

  // Logical stick-to-bottom: only when user is already near bottom and a *new*
  // line arrives. Never fight mid-scroll; never use scrollIntoView (page jump).
  useEffect(() => {
    const lastId = events.length ? events[events.length - 1].id : 0;
    const grew = lastId > lastEventIdRef.current;
    lastEventIdRef.current = lastId;
    if (!grew || !stickBottomRef.current || userScrollingRef.current) return;
    const el = compact ? compactScrollRef.current : fullScrollRef.current;
    if (!el) return;
    if (scrollRafRef.current) cancelAnimationFrame(scrollRafRef.current);
    scrollRafRef.current = requestAnimationFrame(() => {
      scrollRafRef.current = 0;
      if (!stickBottomRef.current) return;
      el.scrollTop = el.scrollHeight;
    });
    return () => {
      if (scrollRafRef.current) cancelAnimationFrame(scrollRafRef.current);
    };
  }, [compact, events]);

  const onLogScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    stickBottomRef.current = dist <= STICK_THRESHOLD_PX;
  };

  const scrollEndTimerRef = useRef(0);
  const markUserScrolling = () => {
    userScrollingRef.current = true;
    if (scrollEndTimerRef.current) window.clearTimeout(scrollEndTimerRef.current);
    scrollEndTimerRef.current = window.setTimeout(() => {
      userScrollingRef.current = false;
      const el = compact ? compactScrollRef.current : fullScrollRef.current;
      if (el) {
        const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
        stickBottomRef.current = dist <= STICK_THRESHOLD_PX;
      }
    }, 180);
  };

  if (compact) {
    return (
      <div className="task-log-panel task-log-panel--compact flex h-full min-h-0 flex-col overflow-hidden">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b-2 border-[var(--border)] bg-[var(--bg-pane)] px-2 py-1.5">
          <div className="min-w-0">
            <div className={`inline-flex border px-1.5 py-0.5 text-[10px] font-semibold ${statusTone}`}>
              {getTaskStatusText(currentStatus, language)}
            </div>
            <div className="mt-0.5 truncate font-[family-name:var(--font-mono)] text-[11px] text-[var(--text-muted)]">
              {progress.label || task?.progress || "0/0"} · {events.length} 行
            </div>
          </div>
          <button type="button" onClick={copyLogs} className="table-action-btn">
            复制
          </button>
        </div>
        <div className="progress-track shrink-0 !my-0 !h-1">
          <div
            className="progress-fill"
            style={{
              width: `${progressTotal > 0 ? progressPercent : isTerminalTaskStatus(currentStatus) ? 100 : 18}%`,
              background:
                currentStatus === "failed"
                  ? "var(--danger)"
                  : currentStatus === "succeeded"
                    ? "var(--ok)"
                    : undefined,
            }}
          />
        </div>
        {errorText ? (
          <div className="shrink-0 border-b border-[var(--danger)] bg-[var(--danger-soft)] px-2 py-1 font-[family-name:var(--font-mono)] text-[11px] text-[var(--danger)]">
            {errorText}
          </div>
        ) : null}
        <div
          ref={compactScrollRef}
          onScroll={onLogScroll}
          onWheel={markUserScrolling}
          onTouchStart={markUserScrolling}
          className="task-log-scroll min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain p-2 font-[family-name:var(--font-mono)] text-[12px]"
        >
          {events.length === 0 ? (
            <div className="py-8 text-center text-[var(--text-muted)]">
              {t("taskLog.waiting")}
            </div>
          ) : (
            <div className="space-y-0.5">
              {events.slice(-300).map((ev) => (
                <div
                  key={ev.id}
                  className={`px-1.5 py-0.5 leading-4 ${classifyLine(ev.line)}`}
                >
                  {ev.line}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="task-log-panel flex h-full min-h-0 flex-col gap-3 overflow-hidden">
      <div className="grid shrink-0 gap-3 md:grid-cols-3">
        <div className={`border-2 px-4 py-3 ${statusTone}`}>
          <div className="px-label opacity-80">{t("taskLog.status")}</div>
          <div className="mt-1 font-[family-name:var(--font-pixel)] text-[9px] uppercase tracking-[0.06em]">
            {getTaskStatusText(currentStatus, language)}
          </div>
        </div>
        <div className="border-2 border-[var(--border)] bg-[var(--bg-pane)] px-4 py-3">
          <div className="px-label">{t("taskLog.progress")}</div>
          <div className="mt-1 px-data text-[22px] text-[var(--text-primary)]">
            {progress.label || task?.progress || "0/0"}
          </div>
        </div>
        <div className="border-2 border-[var(--border)] bg-[var(--bg-pane)] px-4 py-3">
          <div className="px-label">{t("taskLog.events")}</div>
          <div className="mt-1 px-data text-[22px] text-[var(--text-primary)]">
            {t("taskLog.logCount", { count: events.length })}
          </div>
        </div>
      </div>

      <div className="progress-track shrink-0">
        <div
          className={`progress-fill ${
            currentStatus === "failed"
              ? "!bg-[var(--danger)]"
              : currentStatus === "succeeded"
                ? "!bg-[var(--ok)]"
                : ""
          }`}
          style={{
            width: `${progressTotal > 0 ? progressPercent : isTerminalTaskStatus(currentStatus) ? 100 : 18}%`,
            background:
              currentStatus === "failed"
                ? "var(--danger)"
                : currentStatus === "succeeded"
                  ? "var(--ok)"
                  : undefined,
          }}
        />
      </div>

      {errorText ? (
        <div className="shrink-0 border-2 border-[var(--danger)] bg-[var(--danger-soft)] px-4 py-3 font-[family-name:var(--font-mono)] text-[14px] text-[var(--danger)]">
          <div className="mb-1 font-[family-name:var(--font-pixel)] text-[8px] uppercase tracking-[0.08em]">
            {t("taskLog.failureReason")}
          </div>
          <div className="break-words opacity-90">{errorText}</div>
        </div>
      ) : null}

      <div className="flex shrink-0 items-center justify-between gap-3 border-b-2 border-[var(--border)] pb-2">
        <div>
          <div className="px-label">{t("taskLog.liveLog")}</div>
          <div className="mt-1 font-[family-name:var(--font-pixel)] text-[8px] uppercase tracking-[0.06em] text-[var(--text-primary)]">
            {t("taskLog.liveTitle")}
          </div>
        </div>
        <button type="button" onClick={copyLogs} className="table-action-btn">
          {t("taskLog.copyLogs")}
        </button>
      </div>

      <div
        ref={fullScrollRef}
        onScroll={onLogScroll}
        onWheel={markUserScrolling}
        onTouchStart={markUserScrolling}
        className="task-log-scroll min-h-0 flex-1 overflow-y-auto overflow-x-hidden overscroll-contain border-2 border-[var(--border)] bg-[var(--bg-input)] p-2 font-[family-name:var(--font-mono)] text-[14px]"
      >
        {events.length === 0 ? (
          <div className="flex min-h-[160px] items-center justify-center border-2 border-dashed border-[var(--border)] text-[var(--text-muted)]">
            {t("taskLog.waiting")}
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {groups.map((group) => {
              if (group.id === MAIN_GROUP_ID && group.events.length === 0) {
                return null;
              }
              return (
                <LogGroupView
                  key={group.id}
                  group={group}
                  collapsed={!!collapsed[group.id]}
                  isMain={group.id === MAIN_GROUP_ID}
                  onToggle={() => toggleGroup(group.id)}
                />
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

const MAX_VISIBLE_PER_GROUP = 200;

function LogGroupView({
  group,
  collapsed,
  isMain,
  onToggle,
}: {
  group: LogGroup;
  collapsed: boolean;
  isMain: boolean;
  onToggle: () => void;
}) {
  const { t } = useI18n();
  const total = group.events.length;
  const truncated = total > MAX_VISIBLE_PER_GROUP;
  const visible = truncated
    ? group.events.slice(total - MAX_VISIBLE_PER_GROUP)
    : group.events;
  // No scrollIntoView — that scrolls the whole Jobs page. Parent log pane handles stick-to-bottom.

  return (
    <div className="overflow-hidden border-2 border-[var(--border)] bg-[var(--bg-pane)]">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-center gap-2 border-b-2 border-[var(--border)] bg-[var(--bg-hover)] px-3 py-2 text-left font-[family-name:var(--font-pixel)] text-[7px] uppercase tracking-[0.1em] text-[var(--text-secondary)] hover:text-[var(--text-primary)]"
      >
        {collapsed ? (
          <ChevronRight className="h-3.5 w-3.5" />
        ) : (
          <ChevronDown className="h-3.5 w-3.5" />
        )}
        <span className="truncate">
          {isMain ? t("taskLog.mainGroup") : group.label}
        </span>
        <span className="ml-auto font-[family-name:var(--font-mono)] text-[14px] normal-case tracking-normal text-[var(--text-muted)]">
          {t("taskLog.logCount", { count: total })}
        </span>
      </button>
      {!collapsed && (
        <div className="px-2 py-2">
          {truncated && (
            <div className="mb-2 border-2 border-[var(--warn)] bg-[var(--warn-soft)] px-2 py-1 font-[family-name:var(--font-mono)] text-[12px] text-[var(--warn)]">
              {t("taskLog.truncatedHint", {
                shown: MAX_VISIBLE_PER_GROUP,
                total,
              })}
            </div>
          )}
          <div className="space-y-0.5">
            {visible.map((ev) => (
              <div
                key={ev.id}
                className={`px-2 py-0.5 leading-5 ${classifyLine(ev.line)}`}
              >
                {ev.line}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
