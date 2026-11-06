import { useEffect, useRef, useState } from "react";
import type { JobSnapshot } from "../shared/types";

/** Following one job: its log as it arrives, then the result. */
export function useJob() {
  const [job, setJob] = useState<{ log: string; done?: JobSnapshot } | null>(null);
  const source = useRef<EventSource | null>(null);
  useEffect(() => () => source.current?.close(), []);
  return {
    job,
    follow(jobId: string, onDone: () => void) {
      source.current?.close();
      setJob({ log: "" });
      const es = new EventSource(`/api/apps/jobs/${jobId}/events`);
      source.current = es;
      es.addEventListener("log", (ev) => setJob((j) => ({ ...j, log: (j?.log ?? "") + JSON.parse((ev as MessageEvent).data) })));
      es.addEventListener("done", (ev) => {
        setJob((j) => ({ log: j?.log ?? "", done: JSON.parse((ev as MessageEvent).data) }));
        es.close();
        onDone();
      });
    },
  };
}
