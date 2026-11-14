import type { ReactNode } from "react";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

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
    <Card className={`mb-4 max-w-4xl ${className ?? ""}`}>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
        {action && <CardAction>{action}</CardAction>}
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}
