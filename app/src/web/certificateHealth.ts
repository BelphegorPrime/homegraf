import type { CertificateInfo } from "../shared/types";

/** Renewal runs 30 days before the end, so a certificate under 21 days means a renewal did not happen; under 7 is urgent. */
export const WARN_DAYS = 21;
export const DOWN_DAYS = 7;

export type CertHealth = "ok" | "warn" | "down";

export function certHealth(c: CertificateInfo): { health: CertHealth; why: string } {
  if (c.error) return { health: "down", why: `cannot read it: ${c.error}` };
  if (!c.matches) return { health: "down", why: "does not cover this domain" };
  const days = c.daysLeft ?? 0;
  if (days < 0) return { health: "down", why: `expired ${-days} day${days === -1 ? "" : "s"} ago` };
  if (days <= DOWN_DAYS) return { health: "down", why: days === 0 ? "expires today" : `${days} day${days === 1 ? "" : "s"} left` };
  if (days <= WARN_DAYS) return { health: "warn", why: `${days} days left, renewal is due` };
  return { health: "ok", why: `${days} days left` };
}
