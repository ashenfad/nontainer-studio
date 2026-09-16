// Reference ws-vitest file for format.js. Copy to
// /workspace/tests/format.test.js and run `ws-vitest`.
//
// tests/, not app/: app/ is what publishes, so a test file under it
// ships with the app and is fetchable from it.
//
// The import is relative from tests/ to app/ — they are siblings under
// one root, so '../app/format.js'. describe, it and expect are globals
// here; there is nothing to import for them.
import { filterQuery, formatValue } from "../app/format.js";

describe("formatValue", () => {
  it("renders a number the way the page shows it", () => {
    expect(formatValue(1234)).toBe("1,234");
  });

  // The whole reason the function exists. A null reaches the page from
  // any nullable column, and from an aggregate over nothing;
  // null.toLocaleString() throws and takes the render down with it.
  it("renders a missing value as a dash", () => {
    expect(formatValue(null)).toBe("—");
    expect(formatValue(undefined)).toBe("—");
  });

  it("leaves a string alone", () => {
    expect(formatValue("north")).toBe("north");
  });

  // 0 is a real value, not a missing one. A `value || "—"` guard would
  // hide it, and the page would show a dash where the answer is zero.
  it("keeps a zero", () => {
    expect(formatValue(0)).toBe("0");
  });
});

describe("filterQuery", () => {
  it("sends only the filters that are set", () => {
    expect(filterQuery({ category: "a", region: "" })).toBe("category=a");
  });

  it("is empty when nothing is filtered", () => {
    expect(filterQuery({ category: "", region: "" })).toBe("");
  });

  it("escapes what the user picked", () => {
    expect(filterQuery({ region: "north west" })).toBe("region=north+west");
  });
});
