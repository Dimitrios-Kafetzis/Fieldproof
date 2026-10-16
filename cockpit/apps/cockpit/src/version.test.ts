import { APP_NAME, APP_VERSION, banner, isSemver } from "./version.ts";

describe("version", () => {
  it("is a semantic version injected from package.json", () => {
    expect(isSemver(APP_VERSION)).toBe(true);
  });

  it("rejects strings that are not semantic versions", () => {
    expect(isSemver("1.2")).toBe(false);
    expect(isSemver("v1.2.3")).toBe(false);
    expect(isSemver("")).toBe(false);
  });

  it("builds the banner from name and version", () => {
    expect(banner()).toBe(`${APP_NAME} ${APP_VERSION}`);
    expect(banner("x", "0.0.1")).toBe("x 0.0.1");
  });
});
