const JST_OFFSET_MS = 9 * 60 * 60 * 1000;

export type ScheduleTask = Readonly<{
  id: string;
  hour: number;
  minute: number;
  workflow: "daily.yml" | "misc_batch.yml" | "price_alerts.yml";
  inputs: Readonly<Record<string, string>>;
  weekdays?: ReadonlySet<number>;
  monthDays?: ReadonlySet<number>;
}>;

export type JstParts = Readonly<{
  year: number;
  month: number;
  day: number;
  weekday: number;
  hour: number;
  minute: number;
}>;

const WEEKDAYS = new Set([1, 2, 3, 4, 5]);

export const TASKS: readonly ScheduleTask[] = [
  {
    id: "edinet-and-profiles",
    hour: 0,
    minute: 30,
    workflow: "misc_batch.yml",
    inputs: { job: "edinet_texts" },
  },
  {
    id: "analyst-consensus",
    hour: 5,
    minute: 0,
    workflow: "misc_batch.yml",
    inputs: { job: "consensus" },
  },
  {
    id: "youtube-weekly-recovery",
    hour: 5,
    minute: 30,
    weekdays: new Set([1]),
    workflow: "misc_batch.yml",
    inputs: { job: "youtube_recovery" },
  },
  {
    id: "fund-watch",
    hour: 6,
    minute: 0,
    monthDays: new Set([5, 15, 25]),
    workflow: "misc_batch.yml",
    inputs: { job: "fund_watch" },
  },
  {
    id: "price-alerts",
    hour: 7,
    minute: 30,
    workflow: "price_alerts.yml",
    inputs: { dry_run: "false" },
  },
  {
    id: "daily-market-update",
    hour: 16,
    minute: 5,
    weekdays: WEEKDAYS,
    workflow: "daily.yml",
    inputs: { mode: "main" },
  },
  {
    id: "youtube-daily",
    hour: 19,
    minute: 0,
    weekdays: WEEKDAYS,
    workflow: "daily.yml",
    inputs: { mode: "youtube_daily" },
  },
  {
    id: "daily-evening-final",
    hour: 20,
    minute: 30,
    weekdays: WEEKDAYS,
    workflow: "daily.yml",
    inputs: { mode: "evening" },
  },
  {
    id: "youtube-weekly",
    hour: 22,
    minute: 0,
    weekdays: new Set([0]),
    workflow: "misc_batch.yml",
    inputs: { job: "youtube" },
  },
] as const;

export function toJstParts(epochMs: number): JstParts {
  const shifted = new Date(epochMs + JST_OFFSET_MS);
  return {
    year: shifted.getUTCFullYear(),
    month: shifted.getUTCMonth() + 1,
    day: shifted.getUTCDate(),
    weekday: shifted.getUTCDay(),
    hour: shifted.getUTCHours(),
    minute: shifted.getUTCMinutes(),
  };
}

export function matchingTasks(epochMs: number): readonly ScheduleTask[] {
  const current = toJstParts(epochMs);
  return TASKS.filter((task) => {
    if (task.hour !== current.hour || task.minute !== current.minute) return false;
    if (task.weekdays && !task.weekdays.has(current.weekday)) return false;
    if (task.monthDays && !task.monthDays.has(current.day)) return false;
    return true;
  });
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

export function scheduledForJst(epochMs: number): string {
  const current = toJstParts(epochMs);
  return `${current.year}-${pad(current.month)}-${pad(current.day)}T${pad(current.hour)}:${pad(current.minute)}:00+09:00`;
}
