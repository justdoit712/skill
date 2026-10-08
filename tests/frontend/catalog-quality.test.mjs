import test from "node:test";
import assert from "node:assert/strict";
import { qualityBlock } from "../../public/js/catalog-view.js";

test("quality rationale and review disagreement are visible and escaped", () => {
  const entry = {
    quality_summary: {
      review_status: "disagreed",
      review_note: "复核分歧",
      blocking_reasons: ["阻断原因"],
      checks: { practical_value: { value: "pass", evidence: "证据 1" } },
      review_checks: { practical_value: { value: "fail", evidence: "复核证据 <unsafe>" } }
    }
  };
  const html = qualityBlock(entry);
  assert.match(html, /两轮评估有分歧/);
  assert.match(html, /复核分歧/);
  assert.match(html, /阻断原因/);
  assert.match(html, /&lt;unsafe&gt;/);
});
