// commitlint (task P0-T04): Conventional Commits with the scopes from
// docs/plan/04-engineering-standards.md. Enforced locally by the commit-msg hook and on the
// branch by `make lint-commits`.
const layers = ["engine", "sim", "bridge", "robots", "cockpit", "docker", "docs", "ci"];
const repoScopes = ["repo", "plan", "tools", "deps", "release"];
const lineLimit = 100;

// Dependabot's commit bodies carry release notes and compare links well past 100 characters,
// which nobody wraps by hand; its header still follows the rules. The sign-off is its mark.
// A plugin rule sees the parsed commit (a rule's configuration does not), so the built-in
// line-length rules of the body and footer are replaced by these two.
const fromDependabot = (parsed) => /Signed-off-by: dependabot\[bot\]/.test(parsed.raw ?? "");
const lineLengthUnlessDependabot = (field) => (parsed, _when, limit) => {
  if (fromDependabot(parsed)) return [true];
  const long = (parsed[field] ?? "").split("\n").filter((line) => line.length > limit);
  return [long.length === 0, `${field}'s lines must not be longer than ${limit} characters`];
};

export default {
  extends: ["@commitlint/config-conventional"],
  plugins: [
    {
      rules: {
        "body-max-line-length-unless-dependabot": lineLengthUnlessDependabot("body"),
        "footer-max-line-length-unless-dependabot": lineLengthUnlessDependabot("footer"),
      },
    },
  ],
  rules: {
    "type-enum": [
      2,
      "always",
      ["feat", "fix", "docs", "test", "refactor", "build", "ci", "chore", "perf", "revert"],
    ],
    "scope-enum": [2, "always", [...layers, ...repoScopes]],
    "scope-empty": [2, "never"],
    "scope-case": [2, "always", "kebab-case"],
    "subject-case": [2, "never", ["sentence-case", "start-case", "pascal-case", "upper-case"]],
    "subject-empty": [2, "never"],
    "subject-full-stop": [2, "never", "."],
    "header-max-length": [2, "always", lineLimit],
    "body-max-line-length": [0],
    "footer-max-line-length": [0],
    "body-max-line-length-unless-dependabot": [2, "always", lineLimit],
    "footer-max-line-length-unless-dependabot": [2, "always", lineLimit],
  },
  ignores: [
    // Squash-merge commits created by GitHub carry the PR title (`P0-T02 Host environment setup (#2)`).
    (message) => /^P\d-T\d{2} .*\(#\d+\)/.test(message),
  ],
};
