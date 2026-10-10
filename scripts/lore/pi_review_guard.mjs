// Shared by Pi's tools and the host's independent replay. No runtime story writes.
export const PAGE_CHARS = 2400;

export class ReviewSession {
  constructor(snapshot) {
    this.snapshot = structuredClone(snapshot);
    this.documents = new Map([
      [`l:${snapshot.entry.id}`, snapshot.entry.text],
      ...snapshot.sources.map(source => [source.id, source.text]),
    ]);
    this.ranges = new Map();
    this.reads = 0;
    this.accepted = null;
  }

  read({id, offset = 0}) {
    if (this.accepted) throw Error('Review already submitted');
    if (!this.documents.has(id)) throw Error('Unknown snapshot id; file paths are not accepted');
    const text = this.documents.get(id);
    if (!Number.isInteger(offset) || offset < 0 || offset >= text.length) throw Error('Invalid offset');
    if (this.reads >= 16) throw Error('Read budget exhausted');
    this.reads++;
    const end = Math.min(text.length, offset + PAGE_CHARS);
    const ranges = this.ranges.get(id) || [];
    ranges.push([offset, end]); this.ranges.set(id, ranges);
    const result = {id, text: text.slice(offset, end), start: offset, end,
      total_length: text.length, next_offset: end < text.length ? end : null};
    // Claim checklist is exposed only after the entire entry has been retrieved.
    if (id === `l:${this.snapshot.entry.id}` && this.complete(id)) result.claims = this.snapshot.claims;
    return result;
  }

  complete(id) {
    let covered = 0;
    for (const [start, end] of [...(this.ranges.get(id) || [])].sort((a, b) => a[0] - b[0])) {
      if (start > covered) return false;
      covered = Math.max(covered, end);
    }
    return covered === this.documents.get(id)?.length;
  }

  submit({checks}) {
    if (this.accepted) throw Error('Review already submitted');
    const missing = [...this.documents.keys()].filter(id => !this.complete(id));
    if (missing.length) throw Error(`Read all pages before submitting: ${missing.join(', ')}`);
    if (!Array.isArray(checks) || checks.length !== this.snapshot.claims.length) throw Error('Every claim needs exactly one check');
    const seen = new Set();
    const normalized = checks.map(check => {
      if (!this.snapshot.claims.some(claim => claim.id === check.claim_id) || seen.has(check.claim_id)) throw Error('Unknown or duplicate claim');
      seen.add(check.claim_id);
      if (!['supported', 'conflict', 'insufficient'].includes(check.verdict)) throw Error('Invalid verdict');
      if (typeof check.reason !== 'string' || !check.reason.trim() || check.reason.length > 1200) throw Error('A bounded reason is required');
      if (!Array.isArray(check.evidence) || check.evidence.length > 6) throw Error('Invalid evidence list');
      if (check.verdict !== 'insufficient' && !check.evidence.length) throw Error('Supported/conflict checks require exact source quotes');
      const evidence = check.evidence.map(citation => {
        const source = this.snapshot.sources.find(item => item.id === citation.source_id);
        if (!source || typeof citation.quote !== 'string' || !citation.quote.trim() || citation.quote.length > 1000) throw Error('Invalid source citation');
        const start = source.text.indexOf(citation.quote);
        if (start < 0) throw Error('Citation does not occur verbatim in the declared source');
        return {source_id: source.id, message_index: source.message_index, hash: source.hash,
          quote: citation.quote, start, end: start + citation.quote.length};
      });
      return {claim_id: check.claim_id, verdict: check.verdict, reason: check.reason.trim(), evidence};
    });
    this.accepted = {entry_id: this.snapshot.entry.id, checks: normalized,
      all_supported: normalized.every(check => check.verdict === 'supported'),
      fully_read_ids: [...this.documents.keys()],
      limitation: 'Read coverage and exact citations verified; semantic judgments remain model assessments.'};
    return this.accepted;
  }
}
