import { describe, expect, it } from "vitest";
import { matchingTasks, scheduledForJst, toJstParts } from "../src/schedule";

function utc(value: string): number {
  return Date.parse(value);
}

describe("JST schedule", () => {
  it("dispatches the main batch at 16:05 JST on weekdays", () => {
    const tasks = matchingTasks(utc("2026-11-06T07:05:00Z"));
    expect(tasks.map((task) => task.id)).toEqual(["daily-market-update"]);
  });

  it("does not dispatch the main batch on Saturday", () => {
    const tasks = matchingTasks(utc("2026-11-07T07:05:00Z"));
    expect(tasks.map((task) => task.id)).not.toContain("daily-market-update");
  });

  it("retries the main batch at 18:00 JST on weekdays", () => {
    const tasks = matchingTasks(utc("2026-11-06T09:00:00Z"));
    expect(tasks.map((task) => task.id)).toEqual(["daily-market-retry"]);
  });

  it("handles Monday recovery across the UTC date boundary", () => {
    const tasks = matchingTasks(utc("2026-11-08T20:30:00Z"));
    expect(tasks.map((task) => task.id)).toContain("youtube-weekly-recovery");
  });

  it("runs fund watch only on the configured month days", () => {
    expect(matchingTasks(utc("2026-11-14T21:00:00Z")).map((task) => task.id))
      .toContain("fund-watch");
    expect(matchingTasks(utc("2026-11-15T21:00:00Z")).map((task) => task.id))
      .not.toContain("fund-watch");
  });

  it("formats the logical execution slot in JST", () => {
    const time = utc("2026-11-06T07:05:00Z");
    expect(toJstParts(time).hour).toBe(16);
    expect(scheduledForJst(time)).toBe("2026-11-06T16:05:00+09:00");
  });
});
