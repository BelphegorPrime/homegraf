import { useState } from "react";
import { LoginForm } from "@/components/login-form";
import { rememberedUser, rememberUser, useAuth, useSso } from "./api";

/** Why the last single sign-on did not work, as the server put it in the address; shown once and removed from the address. */
function ssoError(): string | null {
  const q = new URLSearchParams(location.search);
  const error = q.get("sso_error");
  if (error) {
    q.delete("sso_error");
    history.replaceState(null, "", `${location.pathname}${q.size ? `?${q}` : ""}${location.hash}`);
  }
  return error;
}

/** The page everybody sees first: nothing else is served to somebody who is not signed in. */
export function Login() {
  const sso = useSso().data;
  const { signIn } = useAuth();
  const [failure] = useState(ssoError);
  return (
    <div className="flex min-h-svh w-full items-center justify-center p-6 md:p-10">
      <div className="w-full max-w-sm">
        <p className="mb-4 text-center text-lg font-semibold tracking-tight">
          homegraf <span className="text-xs font-normal text-muted-foreground">{__APP_VERSION__}</span>
        </p>
        <LoginForm
          initialName={rememberedUser()}
          notice={failure ?? sso?.problem}
          disabledReason={sso?.password.enabled === false ? `Signing in is off: ${sso.password.reason}` : undefined}
          sso={sso?.enabled ? { label: sso.label } : undefined}
          onSubmit={async (name, password) => {
            await signIn(name, password);
            rememberUser(name);
          }}
        />
      </div>
    </div>
  );
}
