import type { ReactNode } from "react";

/** The top of a manage page: what it is, in a sentence a newcomer understands. */
export function PageHeader({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="mb-5 mt-3 max-w-3xl">
      <h1 className="m-0 text-xl font-semibold">{title}</h1>
      <p className="mb-0 mt-1 text-sm text-muted-foreground">{children}</p>
    </div>
  );
}

/** One thing you can do on a page, with a title and a line saying what it does. */
export function Section({
  title,
  description,
  action,
  children,
  className,
}: {
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`mb-4 max-w-4xl rounded-xl border bg-card p-4 ${className ?? ""}`}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 className="!m-0 !text-base !font-semibold !normal-case !tracking-normal !text-foreground">{title}</h2>
          {description && <p className="mb-0 mt-1 text-sm text-muted-foreground">{description}</p>}
        </div>
        {action}
      </div>
      <div className="mt-3">{children}</div>
    </section>
  );
}
