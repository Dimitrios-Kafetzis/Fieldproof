# cockpit

pnpm workspace for layer L5, the browser cockpit
([ADR-0003](../docs/adr/0003-web-cockpit-threejs-foxglove-protocol.md)): TypeScript strict,
Vite, React, Three.js, speaking the Foxglove WebSocket protocol to the engine. Node 22 and pnpm
come from the host setup; nothing else is installed on the host.

```bash
pnpm install --frozen-lockfile   # from cockpit/
pnpm typecheck && pnpm test      # tsc --noEmit and Vitest in every package
pnpm --filter @fieldproof/cockpit dev   # Vite dev server
make test-cockpit                # the same, from the repository root
```

| Path | Contents |
| --- | --- |
| `apps/cockpit/` | the Vite + React application (`@fieldproof/cockpit`); P0-T07 ships a placeholder shell and a Vitest test |
| `packages/` | shared packages (protocol client, design tokens, panels) as they appear from P3-T02 on |

The root ESLint and Prettier configuration applies here through the pre-commit hooks; P3-T02 adds
the React and type-aware rules. See [docs/setup/workspaces.md](../docs/setup/workspaces.md).
