import { matchingTasks, scheduledForJst, type ScheduleTask } from "./schedule";

async function dispatchWorkflow(
  task: ScheduleTask,
  scheduledTime: number,
  env: Env,
): Promise<void> {
  const scheduledFor = scheduledForJst(scheduledTime);
  const url = `https://api.github.com/repos/${encodeURIComponent(env.GITHUB_OWNER)}/${encodeURIComponent(env.GITHUB_REPO)}/actions/workflows/${encodeURIComponent(task.workflow)}/dispatches`;
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "stock-tracker-cloudflare-scheduler",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({
      ref: env.GITHUB_REF,
      inputs: {
        ...task.inputs,
        source: "cloudflare",
        scheduled_for: scheduledFor,
      },
    }),
  });

  if (!response.ok) {
    const errorBody = (await response.text()).slice(0, 500);
    throw new Error(
      `GitHub dispatch failed: task=${task.id} status=${response.status} body=${errorBody}`,
    );
  }

  console.log(JSON.stringify({
    event: "workflow_dispatched",
    task: task.id,
    workflow: task.workflow,
    scheduledFor,
    status: response.status,
  }));
}

export default {
  async scheduled(controller, env): Promise<void> {
    const tasks = matchingTasks(controller.scheduledTime);
    if (tasks.length === 0) return;

    const results = await Promise.allSettled(
      tasks.map((task) => dispatchWorkflow(task, controller.scheduledTime, env)),
    );
    const failures = results
      .map((result, index) => ({ result, task: tasks[index] }))
      .filter((item): item is { result: PromiseRejectedResult; task: ScheduleTask } =>
        item.result.status === "rejected" && item.task !== undefined,
      );

    if (failures.length > 0) {
      const message = failures
        .map(({ result, task }) => `${task.id}: ${String(result.reason)}`)
        .join(" / ");
      console.error(JSON.stringify({ event: "dispatch_failed", message }));
      throw new Error(message);
    }
  },
} satisfies ExportedHandler<Env>;
