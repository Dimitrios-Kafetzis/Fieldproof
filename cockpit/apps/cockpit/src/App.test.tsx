import { renderToStaticMarkup } from "react-dom/server";

import { App } from "./App.tsx";
import { banner } from "./version.ts";

describe("App", () => {
  it("renders the banner in the shell", () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain(`<h1>${banner()}</h1>`);
  });
});
