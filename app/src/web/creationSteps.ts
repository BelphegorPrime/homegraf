import type { PendingCreation } from "../shared/types";

export type StepState = "done" | "now" | "waiting" | "failed";

export interface Step {
  label: string;
  state: StepState;
  detail?: string;
}

/** The way from the pull request to a running app, as the page lists it; the first step that is not done is the one in progress. */
export function creationSteps(c: PendingCreation): Step[] {
  const merged = c.pr === "merged";
  const running = c.stack?.health === "ok";
  const steps: Step[] = [
    { label: "Pull request merged", state: merged ? "done" : c.pr === "closed" ? "failed" : "now",
      detail: c.pr === "open" ? "waiting for you to merge it" : c.pr === "closed" ? "closed without a merge: the stack and its secrets are still there, reopen the pull request or remove the app" : c.pr === "unknown" ? "Forgejo cannot be reached" : undefined },
    { label: "Stack running", state: running ? "done" : merged ? "now" : "waiting",
      detail: merged && !running ? (c.stack ? `${c.stack.detail ?? c.stack.health}: Komodo deploys after the merge` : "Komodo has not answered yet") : undefined },
  ];
  if (c.domain) {
    steps.push({ label: `https://${c.domain} answers`, state: c.https?.ok ? "done" : running ? "now" : "waiting",
      detail: running && !c.https?.ok ? c.https?.detail ?? "checking" : undefined });
  }
  return steps;
}
