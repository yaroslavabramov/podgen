/** artifact-jcs-v1. Owns no schemas: generated contracts live beside this adapter. */
import { createHash } from 'node:crypto';

export function canonical(value: unknown): string {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'string') {
    for (const point of value) {
      const code = point.codePointAt(0)!;
      if (code >= 0xd800 && code <= 0xdfff) throw new Error('invalid_unicode');
    }
    return JSON.stringify(value);
  }
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('nonfinite_number');
    if (Number.isInteger(value) && !Number.isSafeInteger(value)) throw new Error('unsafe_integer_use_decimal_string');
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (typeof value === 'object' && Object.getPrototypeOf(value) === Object.prototype) {
    return '{' + Object.keys(value).sort().map(key => canonical(key) + ':' + canonical((value as Record<string,unknown>)[key])).join(',') + '}';
  }
  throw new Error('not_json');
}

export function digest(value: unknown): string {
  return createHash('sha256').update(canonical(value), 'utf8').digest('hex');
}

export type ArtifactRef = {kind: string; artifact_id: string; revision_id: string};
export function contentHash(kind: string, schema_version: string, payload: unknown,
  input_refs: ArtifactRef[], blob_hashes: {purpose: string; hash: string}[]): string {
  const compare = (a: string, b: string) => a < b ? -1 : a > b ? 1 : 0;
  const refs = [...new Map(input_refs.map(ref => [[ref.kind,ref.artifact_id,ref.revision_id].join('/'),ref])).values()]
    .sort((a,b) => compare(a.kind,b.kind) || compare(a.artifact_id,b.artifact_id) || compare(a.revision_id,b.revision_id));
  const hashes = [...blob_hashes].sort((a,b) => compare(a.purpose,b.purpose) || compare(a.hash,b.hash));
  return 'sha256:artifact-jcs-v1:' + digest({kind,schema_version,payload,input_refs:refs,blob_hashes:hashes});
}

export function selectText(text: string, start: number, end: number, exact: string): string {
  const points = Array.from(text);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || end < start || end > points.length
      || points.slice(start,end).join('') !== exact) throw new Error('selector_mismatch');
  return exact;
}
