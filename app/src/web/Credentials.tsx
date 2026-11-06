import { useState } from "react";
import { Input } from "@/components/ui/input";
import { rememberedUser, ssoSession, useSso, type Session } from "./api";

/**
 * Who acts for a write action: the person signed in through the identity provider, or a name and password typed for this one action
 * (cleared again by `spent()`).
 */
export function useCredentials() {
  const sso = useSso().data?.user;
  const [user, setUser] = useState(rememberedUser);
  const [password, setPassword] = useState("");
  return {
    sso, user, setUser, password, setPassword,
    /** Enough to send the request. */
    ready: !!sso || (!!user.trim() && !!password),
    session: (): Session => (sso ? ssoSession(sso) : { name: user.trim(), password }),
    spent: () => setPassword(""),
  };
}
export type Credentials = ReturnType<typeof useCredentials>;

/** The fields for it: nothing to type while signed in through the provider. */
export function CredentialFields({ c, className = "flex flex-wrap gap-2" }: { c: Credentials; className?: string }) {
  if (c.sso) return <p className="muted">Acting as <strong>{c.sso.name}</strong>.</p>;
  return (
    <div className={className}>
      <Input className="w-40" autoComplete="username" placeholder="User" aria-label="User" value={c.user} onChange={(e) => c.setUser(e.target.value)} />
      <Input className="w-56" type="password" autoComplete="current-password" placeholder="Password" aria-label="Password" value={c.password} onChange={(e) => c.setPassword(e.target.value)} />
    </div>
  );
}
