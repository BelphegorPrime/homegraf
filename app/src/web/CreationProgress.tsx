import { Badge } from "@/components/ui/badge";
import { useCreations } from "./api";
import { creationSteps, type StepState } from "./creationSteps";

const MARK: Record<StepState, { text: string; cls: string }> = {
  done: { text: "done", cls: "border-ok text-ok" },
  now: { text: "in progress", cls: "border-warn text-warn" },
  waiting: { text: "waiting", cls: "text-muted-foreground" },
  failed: { text: "stopped", cls: "border-down text-down" },
};

/** The apps created here, followed from the pull request to HTTPS; survives a reload because the server keeps them. */
export function CreationProgress() {
  const { data } = useCreations();
  if (!data?.creations.length && !data?.problem) return null;
  return (
    <section>
      <h2>Created from here</h2>
      {data.problem && <p className="warning !px-0">{data.problem}</p>}
      <ul className="grid max-w-3xl gap-3">
        {data.creations.map((c) => (
          <li key={c.appId} className="rounded-md border p-3">
            <div>
              <strong>{c.appId}</strong>{" "}
              <span className="muted text-xs">
                {c.server}
                {c.requestedBy ? ` · by ${c.requestedBy}` : ""} ·{" "}
                <a href={c.pullRequestUrl} target="_blank" rel="noreferrer">
                  pull request
                </a>
              </span>
              {c.done && (
                <Badge variant="outline" className="ml-2 border-ok text-ok">
                  running
                  {c.domain ? (
                    <>
                      {" "}
                      ·{" "}
                      <a href={`https://${c.domain}`} target="_blank" rel="noreferrer">
                        open
                      </a>
                    </>
                  ) : null}
                </Badge>
              )}
            </div>
            <ol className="mt-2 grid gap-1 text-sm">
              {creationSteps(c).map((s) => (
                <li key={s.label}>
                  <Badge variant="outline" className={MARK[s.state].cls}>
                    {MARK[s.state].text}
                  </Badge>{" "}
                  {s.label}
                  {s.detail && <span className="muted"> — {s.detail}</span>}
                </li>
              ))}
            </ol>
          </li>
        ))}
      </ul>
    </section>
  );
}
