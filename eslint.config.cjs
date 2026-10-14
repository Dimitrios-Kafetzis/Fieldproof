// Root ESLint flat configuration (task P0-T04): TypeScript strict rules for every JS/TS file in
// the repo. CommonJS on purpose: the pre-commit hook installs eslint and these packages in its own
// environment and exposes them through NODE_PATH, which only `require` honours. The cockpit
// workspace (cockpit/) extends this with React and type-aware rules once it has a tsconfig
// (P3-T02). Prettier owns formatting, so eslint-config-prettier comes last.
const js = require("@eslint/js");
const prettier = require("eslint-config-prettier");
const globals = require("globals");
const tseslint = require("typescript-eslint");

module.exports = tseslint.config(
  {
    ignores: [
      "**/node_modules/**",
      "**/dist/**",
      "**/build/**",
      "**/install/**",
      "**/log/**",
      "**/coverage/**",
      "assets/**",
    ],
  },
  js.configs.recommended,
  ...tseslint.configs.strict,
  ...tseslint.configs.stylistic,
  {
    languageOptions: {
      ecmaVersion: 2023,
      sourceType: "module",
      globals: { ...globals.node, ...globals.browser },
    },
    rules: {
      "no-console": ["error", { allow: ["warn", "error"] }],
      eqeqeq: ["error", "always"],
      "no-var": "error",
      "prefer-const": "error",
      "@typescript-eslint/consistent-type-imports": "error",
      "@typescript-eslint/no-unused-vars": [
        "error",
        { argsIgnorePattern: "^_", varsIgnorePattern: "^_" },
      ],
    },
  },
  {
    files: ["**/*.cjs"],
    languageOptions: { sourceType: "commonjs" },
    rules: { "@typescript-eslint/no-require-imports": "off" },
  },
  prettier,
);
