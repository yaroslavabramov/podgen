import { readFileSync } from 'node:fs';
import { strict as assert } from 'node:assert';
import { canonical, contentHash, selectText } from './canonical.ts';
const fixture = JSON.parse(readFileSync(new URL('./golden.json',import.meta.url),'utf8'));
for (const test of fixture.cases) {
  assert.equal(canonical(test.document),test.canonical);
  const d = test.document;
  assert.equal(contentHash(d.kind,d.schema_version,d.payload,d.input_refs,d.blob_hashes),test.content_hash);
}
for (const test of fixture.selectors) assert.equal(selectText(test.text,test.start,test.end,test.exact),test.exact);
for (const bad of [NaN, Infinity, 2 ** 60, '\ud800']) assert.throws(() => canonical(bad));
console.log(`Passed ${fixture.cases.length} Python/TypeScript hash vectors, ${fixture.selectors.length} Unicode selectors and invalid inputs`);
