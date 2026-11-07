// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { LandingPage } from "./LandingPage";

const REPO = "https://github.com/ishaq88/UpClose";

beforeAll(() => {
  // jsdom has no layout; the loupe only needs the observer to exist.
  vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("marketing origin boundary", () => {
  it("renders the sample findings without reading storage or calling any API", () => {
    const read = vi.spyOn(Storage.prototype, "getItem");
    const write = vi.spyOn(Storage.prototype, "setItem");
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    render(<LandingPage appOrigin="https://app.example.test" repoUrl={REPO} />);
    expect(screen.getByRole("heading", { level: 1 }).textContent).toMatch(/Good find.*Look closer/);
    fireEvent.click(screen.getByRole("button", { name: /Dent/ }));
    expect(screen.getByRole("button", { name: /Dent/ }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByText("1 serious issue")).toBeTruthy();
    expect(read).not.toHaveBeenCalled();
    expect(write).not.toHaveBeenCalled();
    expect(fetch).not.toHaveBeenCalled();
    vi.unstubAllGlobals();
    vi.stubGlobal("ResizeObserver", class { observe() {} disconnect() {} });
  });

  it("routes every product action straight to the configured origin when the app is live", () => {
    const { container } = render(<LandingPage appOrigin="https://app.example.test" repoUrl={REPO} />);
    const product = [...container.querySelectorAll<HTMLAnchorElement>("a")].filter((a) => /Start/.test(a.textContent ?? ""));
    expect(product.length).toBeGreaterThan(0);
    for (const link of product) expect(link.getAttribute("href")).toBe("https://app.example.test");
    for (const link of container.querySelectorAll<HTMLAnchorElement>('a[href^="#"]')) {
      const fragment = link.getAttribute("href")!.slice(1);
      expect(document.getElementById(fragment)).not.toBeNull();
    }
  });

  it("points to the sample report and the code, and never to an app, while the app is not public", () => {
    const { container } = render(<LandingPage appOrigin={null} repoUrl={REPO} />);
    const hrefs = [...container.querySelectorAll<HTMLAnchorElement>("a")].map((a) => a.getAttribute("href") ?? "");
    for (const href of hrefs) expect(href === REPO || href === "/" || href === "/sample/" || href.startsWith("#")).toBe(true);
    expect(screen.queryByText("Start an inspection")).toBeNull();
    expect(screen.getAllByRole("link", { name: /See a sample report|Open the sample report/ }).length).toBeGreaterThan(0);
    for (const link of container.querySelectorAll<HTMLAnchorElement>(`a[href="${REPO}"]`)) {
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });
});
