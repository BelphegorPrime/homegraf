import { describe, expect, it } from "vitest";
import { certHealth } from "./certificateHealth";

const cert = (over: object) => ({ domain: "a.x", matches: true, daysLeft: 60, ...over });

describe("certHealth", () => {
  it("is ok with weeks left", () => expect(certHealth(cert({}))).toEqual({ health: "ok", why: "60 days left" }));
  it("warns when renewal is due, and is urgent in the last week", () => {
    expect(certHealth(cert({ daysLeft: 21 })).health).toBe("warn");
    expect(certHealth(cert({ daysLeft: 22 })).health).toBe("ok");
    expect(certHealth(cert({ daysLeft: 7 }))).toEqual({ health: "down", why: "7 days left" });
    expect(certHealth(cert({ daysLeft: 1 })).why).toBe("1 day left");
    expect(certHealth(cert({ daysLeft: 0 })).why).toBe("expires today");
  });
  it("is down when expired, for other names, or unreadable", () => {
    expect(certHealth(cert({ daysLeft: -1 }))).toEqual({ health: "down", why: "expired 1 day ago" });
    expect(certHealth(cert({ daysLeft: -5 })).why).toBe("expired 5 days ago");
    expect(certHealth(cert({ matches: false })).why).toBe("does not cover this domain");
    expect(certHealth({ domain: "a.x", error: "timeout" }).why).toBe("cannot read it: timeout");
  });
});
