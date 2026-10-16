export const APP_NAME = "Fieldproof cockpit";

/** The version baked in at build time (package.json, kept equal to version.txt by release-please). */
export const APP_VERSION: string = __COCKPIT_VERSION__;

const SEMVER = /^\d+\.\d+\.\d+(?:[-+].*)?$/;

/** `true` when a version string is a semantic version (`MAJOR.MINOR.PATCH` with optional tags). */
export function isSemver(version: string): boolean {
  return SEMVER.test(version);
}

/** The banner shown in the shell and logged on start-up: `<name> <version>`. */
export function banner(name: string = APP_NAME, version: string = APP_VERSION): string {
  return `${name} ${version}`;
}
