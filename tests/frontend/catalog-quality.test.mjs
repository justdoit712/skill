import test from "node:test";
import assert from "node:assert/strict";
import { qualityBlock } from "../../public/js/catalog-view.js";

test("old entries are not labelled as deeply reviewed", () => {
  assert.equal(qualityBlock({}), "");
});

test("quality rationale and review disagreement are visible and escaped", () => {
  const html = qualityBlock({ quality_summary: {
    review_status: "disagreed", review_note: "需要复核",
    checks: { practical_value: { value: "pass", evidence: "<script>bad()</script>" } },
    review_checks: { verification: { value: "unknown", evidence: "缺少验收条件" } }
  } });
  assert.ok(html.includes("两轮评估有分歧"));
  assert.ok(html.includes("缺少验收条件"));
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!html.includes("<script>"));
});
