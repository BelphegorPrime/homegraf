import { useState } from "react";
import { cn } from "cn";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldError, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

export interface LoginFormProps extends Omit<React.ComponentProps<"div">, "onSubmit"> {
  /** Signs in with a name and a password; rejects with a message fit for the person. */
  onSubmit: (name: string, password: string) => Promise<void>;
  /** Why name and password cannot be used (no HOMEGRAF_PASSWORD set); the form is disabled. */
  disabledReason?: string;
  /** The identity provider's name, when single sign-on is set up. */
  sso?: { label?: string };
  /** What went wrong before this form (e.g. a failed single sign-on). */
  notice?: string | null;
  initialName?: string;
}

export function LoginForm({ className, onSubmit, disabledReason, sso, notice, initialName = "", ...props }: LoginFormProps) {
  const [name, setName] = useState(initialName);
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const shown = error ?? notice ?? disabledReason;

  return (
    <div className={cn("flex flex-col gap-6", className)} {...props}>
      <Card>
        <CardHeader>
          <CardTitle>Sign in to homegraf</CardTitle>
          <CardDescription>The map and everything behind it need an account.</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setError(null);
              setPending(true);
              try {
                await onSubmit(name.trim(), password);
              } catch (err) {
                setError(err instanceof Error ? err.message : String(err));
              } finally {
                setPassword("");
                setPending(false);
              }
            }}
          >
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="login-name">User name</FieldLabel>
                <Input
                  id="login-name"
                  autoComplete="username"
                  autoFocus
                  required
                  disabled={!!disabledReason}
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor="login-password">Password</FieldLabel>
                <Input
                  id="login-password"
                  type="password"
                  autoComplete="current-password"
                  required
                  disabled={!!disabledReason}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
              </Field>
              <Field>
                <Button type="submit" disabled={!!disabledReason || pending || !name.trim() || !password}>
                  Sign in
                </Button>
                {sso && (
                  <Button variant="outline" type="button" nativeButton={false} render={<a href="/api/auth/login" />}>
                    Sign in with {sso.label ?? "single sign-on"}
                  </Button>
                )}
                {shown && <FieldError>{shown}</FieldError>}
                <FieldDescription className="text-center">Accounts are managed by an administrator.</FieldDescription>
              </Field>
            </FieldGroup>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
