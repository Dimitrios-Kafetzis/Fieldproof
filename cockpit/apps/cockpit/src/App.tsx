import type { ReactElement } from "react";
import { banner } from "./version.ts";

/**
 * Placeholder shell (task P0-T07). P3-T02 replaces it with the layout shell: 3D viewport, side
 * panels and timeline bar, connected to the engine over the Foxglove WebSocket protocol.
 */
export function App(): ReactElement {
  return (
    <main>
      <h1>{banner()}</h1>
      <p>Run the real stack. Prove it before the field.</p>
    </main>
  );
}
