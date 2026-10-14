// commitlint (task P0-T04): Conventional Commits with the scopes from
// docs/plan/04-engineering-standards.md. Enforced locally by the commit-msg hook and on the
// branch by `make lint-commits`.
const layers = ["engine", "sim", "bridge", "robots", "cockpit", "docker", "docs", "ci"];
const repoScopes = ["repo", "plan", "tools", "deps", "release"];

export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "type-enum": [
      2,
      "always",
      ["feat", "fix", "docs", "test", "refactor", "build", "ci", "chore", "perf", "revert"],
    ],
    "scope-enum": [2, "always", [...layers, ...repoScopes]],
    "scope-empty": [2, "never"],
    "scope-case": [2, "always", "kebab-case"],
    "subject-case": [2, "never", ["start-case", "pascal-case", "upper-case"]],
    "subject-empty": [2, "never"],
    "subject-full-stop": [2, "never", "."],
    "header-max-length": [2, "always", 100],
    "body-max-line-length": [2, "always", 100],
    "footer-max-line-length": [2, "always", 100],
  },
  ignores: [
    // Squash-merge commits created by GitHub carry the PR title (`P0-T02 Host environment setup (#2)`).
    (message) => /^P\d-T\d{2} .*\(#\d+\)/.test(message),
  ],
};
