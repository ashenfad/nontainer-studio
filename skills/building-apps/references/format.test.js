// Reference ws-vitest file for format.js. Copy to
// /workspace/tests/format.test.js and run `ws-vitest`.
//
// tests/, not app/: app/ is what publishes, so a test file under it
// ships with the app and is fetchable from it.
//
// The import is relative from tests/ to app/ — they are siblings under
// one root, so '../app/format.js'. describe, it and expect are globals
// here; there is nothing to import for them.
import {
  filterQuery,
  filtersFromSearch,
  formatLabel,
  formatValue,
  searchWithFilters,
} from "../app/format.js";

describe("formatLabel", () => {
  // The bug this closes: a year through a number formatter renders as
  // "2,023". An identifier is not a quantity and takes no separators.
  it("leaves a year ungrouped", () => {
    expect(formatLabel(2023)).toBe("2023");
  });

  it("leaves a string alone", () => {
    expect(formatLabel("north")).toBe("north");
  });

  it("renders a missing label as a dash", () => {
    expect(formatLabel(null)).toBe("—");
    expect(formatLabel(undefined)).toBe("—");
  });
});

describe("formatValue", () => {
  it("groups a measure for reading", () => {
    expect(formatValue(1234)).toBe("1,234");
  });

  // Bare toLocaleString() caps at three fractional digits, so this
  // would come back "1.235" — a different number, silently.
  it("keeps every digit it was given", () => {
    expect(formatValue(1.23456)).toBe("1.23456");
  });

  // ...and the same cap renders a small value as "0", which reads as
  // "there is nothing here" when there is.
  it("does not flatten a tiny value to zero", () => {
    expect(formatValue(0.00001)).toBe("0.00001");
  });

  it("groups the integer part without touching the fraction", () => {
    expect(formatValue(1234.5678)).toBe("1,234.5678");
  });

  // Rounding is the caller's choice, per column, never the default.
  it("rounds to a fixed width when asked", () => {
    expect(formatValue(1.23456, { digits: 2 })).toBe("1.23");
    expect(formatValue(50, { digits: 2 })).toBe("50.00");
  });

  // The whole reason the function exists. A null reaches the page from
  // any nullable column, and from an aggregate over nothing;
  // null.toLocaleString() throws and takes the render down with it.
  it("renders a missing value as a dash", () => {
    expect(formatValue(null)).toBe("—");
    expect(formatValue(undefined)).toBe("—");
  });

  // 0 is a real value, not a missing one. A `value || "—"` guard would
  // hide it, and the page would show a dash where the answer is zero.
  it("keeps a zero", () => {
    expect(formatValue(0)).toBe("0");
  });

  it("leaves a string alone", () => {
    expect(formatValue("north")).toBe("north");
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

describe("filters in the URL", () => {
  it("reads the filters the page was opened with", () => {
    expect(filtersFromSearch("?category=a&region=east", ["category", "region"])).toEqual({
      category: "a",
      region: "east",
    });
  });

  it("reads a missing filter as unset", () => {
    expect(filtersFromSearch("?v=3", ["category", "region"])).toEqual({
      category: "",
      region: "",
    });
  });

  it("writes the filters back over the current query", () => {
    expect(searchWithFilters("?category=a", { category: "b", region: "east" })).toBe(
      "?category=b&region=east",
    );
  });

  it("keeps a param that is not a filter", () => {
    // the studio loads the page with its own cache-busting `v`
    expect(searchWithFilters("?v=7", { category: "a", region: "" })).toBe("?v=7&category=a");
  });

  it("removes a cleared filter and returns nothing when nothing remains", () => {
    expect(searchWithFilters("?category=a", { category: "", region: "" })).toBe("");
  });
});
