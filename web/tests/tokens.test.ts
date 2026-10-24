import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

function theme(css: string, selector: string): Record<string, string> {
  const block = css.split(selector)[1]!.split("}")[0]!;
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*(#[0-9a-fA-F]{6})/g)].map((m) => [m[1]!, m[2]!]));
}
const lin = (c: number) => { const s = c / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
function luminance(hex: string) { const n = parseInt(hex.slice(1), 16); return 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255); }
function ratio(a: string, b: string) { const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p); return (x! + 0.05) / (y! + 0.05); }

const css = readFileSync("src/styles/tokens.css", "utf8");
const themes = { dark: theme(css, ':root, [data-theme="dark"] {'), light: theme(css, '[data-theme="light"] {') };

describe.each(Object.entries(themes))("%s theme meets WCAG AA", (_name, t) => {
  it("body and secondary text are readable on every surface", () => {
    for (const surface of ["bg", "s1", "s2", "s3"]) {
      expect(ratio(t.text!, t[surface]!), `text on ${surface}`).toBeGreaterThanOrEqual(7);
      expect(ratio(t.muted!, t[surface]!), `muted on ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
    expect(ratio(t.faint!, t.bg!), "faint on bg").toBeGreaterThanOrEqual(4.5);
    expect(ratio(t.faint!, t.s1!), "faint on s1").toBeGreaterThanOrEqual(4.5);
  });
  it("every status colour is legible on its own tinted background, so state never relies on hue alone", () => {
    for (const tone of ["pass", "warn", "fail", "idle", "active"]) expect(ratio(t[tone]!, t[`${tone}-bg`]!), tone).toBeGreaterThanOrEqual(4.5);
    for (const tone of ["pass", "warn", "fail", "active"]) expect(ratio(t[tone]!, t.s1!), `${tone} on surface`).toBeGreaterThanOrEqual(4.5);
  });
  it("primary buttons and focus rings are visible", () => {
    expect(ratio(t["accent-ink"]!, t.accent!)).toBeGreaterThanOrEqual(7);
    expect(ratio(t.focus!, t.bg!)).toBeGreaterThanOrEqual(3);
    expect(ratio(t["line-strong"]!, t.bg!), "input borders").toBeGreaterThanOrEqual(1.8);
  });
});

it("green is reserved for verified state: no brand surface uses the pass colour", () => {
  expect(themes.dark.accent).not.toBe(themes.dark.pass);
  expect(themes.light.accent).not.toBe(themes.light.pass);
});
