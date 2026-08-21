/**
 * Tests for the TypeScript CRDT.
 *
 * The important block is "shared vectors": the same cases are asserted by
 * server/tests/test_crdt.py, so the Python and TypeScript implementations of one
 * algorithm cannot quietly drift apart. Regenerate them with
 * `cd server && uv run python scripts/gen_vectors.py`.
 */

import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { RGA, type Operation } from './crdt';
import { diffText } from './diff';

interface Vectors {
  cases: { name: string; note: string; ops: Operation[]; text: string }[];
}

const vectors: Vectors = JSON.parse(
  readFileSync(new URL('../../shared/crdt-vectors.json', import.meta.url), 'utf8'),
);

function replica(clientId: string, ...opsLists: Operation[][]): RGA {
  const doc = new RGA(clientId);
  for (const ops of opsLists) for (const op of ops) doc.apply(op);
  return doc;
}

function* permutations<T>(items: T[]): Generator<T[]> {
  if (items.length <= 1) {
    yield [...items];
    return;
  }
  for (let i = 0; i < items.length; i += 1) {
    const rest = [...items.slice(0, i), ...items.slice(i + 1)];
    for (const tail of permutations(rest)) yield [items[i], ...tail];
  }
}

describe('shared vectors', () => {
  it.each(vectors.cases.map((c) => [c.name, c] as const))('%s', (_name, testCase) => {
    const doc = new RGA('vector-runner');
    for (const op of testCase.ops) doc.apply(op);
    expect(doc.text).toBe(testCase.text);
  });

  it('covers the cases we care about', () => {
    expect(vectors.cases.length).toBeGreaterThanOrEqual(9);
  });
});

describe('local editing', () => {
  it('builds text from inserts', () => {
    const doc = new RGA('a');
    doc.localInsert(0, 'hello');
    expect(doc.text).toBe('hello');
  });

  it('inserts between neighbours, not after them', () => {
    const doc = new RGA('a');
    doc.localInsert(0, 'ab');
    doc.localInsert(1, 'c');
    expect(doc.text).toBe('acb');
  });

  it('reads left to right when typing repeatedly at the front', () => {
    const doc = new RGA('a');
    for (const char of 'cba') doc.localInsert(0, char);
    expect(doc.text).toBe('abc');
  });

  it('deletes only the named characters', () => {
    const doc = new RGA('a');
    doc.localInsert(0, 'hello world');
    doc.localDelete(5, 6);
    expect(doc.text).toBe('hello');
  });

  it('rejects an out-of-range insert', () => {
    const doc = new RGA('a');
    expect(() => doc.localInsert(9, 'x')).toThrow();
  });
});

describe('two replicas', () => {
  it('keeps both concurrent inserts at the same spot', () => {
    const alice = new RGA('alice');
    const bob = new RGA('bob');

    const base = alice.localInsert(0, 'hi');
    for (const op of base) bob.apply(op);

    const aliceOps = alice.localInsert(2, 'A');
    const bobOps = bob.localInsert(2, 'B');
    for (const op of bobOps) alice.apply(op);
    for (const op of aliceOps) bob.apply(op);

    expect(alice.text).toBe(bob.text);
    expect(alice.text).toBe('hiBA');
  });

  it('does not interleave concurrent word insertions', () => {
    const alice = new RGA('alice');
    const bob = new RGA('bob');
    const aliceOps = alice.localInsert(0, 'hello');
    const bobOps = bob.localInsert(0, 'world');
    for (const op of bobOps) alice.apply(op);
    for (const op of aliceOps) bob.apply(op);

    expect(alice.text).toBe(bob.text);
    expect(['helloworld', 'worldhello']).toContain(alice.text);
  });

  it('lets an insert anchor to a character the other replica deleted', () => {
    const alice = new RGA('alice');
    const bob = new RGA('bob');
    for (const op of alice.localInsert(0, 'abc')) bob.apply(op);

    const deletes = alice.localDelete(1, 1);
    const inserts = bob.localInsert(2, 'Z');
    for (const op of inserts) alice.apply(op);
    for (const op of deletes) bob.apply(op);

    expect(alice.text).toBe(bob.text);
    expect(alice.text).toBe('aZc');
  });
});

describe('convergence', () => {
  it('produces the same document under every operation order', () => {
    const alice = new RGA('alice');
    const base = alice.localInsert(0, 'hi');
    const bob = replica('bob', base);
    const carol = replica('carol', base);

    const ops: Operation[] = [
      ...base,
      ...alice.localInsert(2, '!'),
      ...bob.localInsert(0, 'oh '),
      ...carol.localDelete(0, 1),
    ];
    expect(ops.length).toBeLessThanOrEqual(8);

    const expected = replica('reference', ops).text;
    const seen = new Set<string>();
    for (const order of permutations(ops)) seen.add(replica('perm', order).text);

    expect([...seen]).toEqual([expected]);
  });

  it('is idempotent', () => {
    const alice = new RGA('alice');
    const ops = [...alice.localInsert(0, 'abc'), ...alice.localDelete(1, 1)];
    const once = replica('once', ops);
    const twice = replica('twice', ops, ops);
    expect(once.text).toBe('ac');
    expect(twice.text).toBe(once.text);
    expect(twice.applied).toBe(once.applied);
  });

  it('parks an insert whose parent has not arrived', () => {
    const alice = new RGA('alice');
    const ops = alice.localInsert(0, 'abc');
    expect(replica('late', [...ops].reverse()).text).toBe('abc');
  });

  it('accepts a delete before the insert it refers to', () => {
    const alice = new RGA('alice');
    const inserts = alice.localInsert(0, 'abc');
    const deletes = alice.localDelete(1, 1);
    expect(replica('late', deletes, inserts).text).toBe('ac');
  });

  it('handles a chain deep enough to overflow a recursive walk', () => {
    const alice = new RGA('alice');
    const ops = alice.localInsert(0, 'x'.repeat(5000));
    expect(replica('deep', ops).text).toBe('x'.repeat(5000));
  });

  it('rebuilds from a snapshot', () => {
    const alice = new RGA('alice');
    alice.localInsert(0, 'hello world');
    alice.localDelete(5, 6);
    expect(replica('joiner', alice.snapshotOps()).text).toBe('hello');
  });

  it('advances its counter past ids seen from other replicas', () => {
    const alice = new RGA('alice');
    const bob = new RGA('bob');
    for (let i = 0; i < 5; i += 1) bob.localInsert(0, 'x');
    for (const op of bob.snapshotOps()) alice.apply(op);

    const ops = alice.localInsert(0, 'y');
    expect(ops[0].id[1]).toBeGreaterThan(5);
  });
});

describe('validation', () => {
  const bad: unknown[] = [
    'not an object',
    null,
    { kind: 'nonsense', id: ['a', 1] },
    { kind: 'insert', id: ['a', 1], parent: ['', 0], value: 'too long' },
    { kind: 'insert', id: ['a', 1], parent: ['', 0] },
    { kind: 'insert', id: 'a:1', parent: ['', 0], value: 'x' },
    { kind: 'insert', id: ['a', '1'], parent: ['', 0], value: 'x' },
    { kind: 'delete', id: ['a'] },
    { kind: 'delete' },
  ];

  it.each(bad.map((value, index) => [index, value] as const))(
    'rejects malformed operation %i',
    (_index, value) => {
      expect(() => new RGA('a').apply(value)).toThrow();
    },
  );

  it('leaves the document untouched when an operation is rejected', () => {
    const doc = new RGA('a');
    doc.localInsert(0, 'hi');
    expect(() => doc.apply({ kind: 'insert', id: ['a', 9], parent: ['', 0], value: '' })).toThrow();
    expect(doc.text).toBe('hi');
  });
});

describe('diffText', () => {
  it('returns null when nothing changed', () => {
    expect(diffText('abc', 'abc')).toBeNull();
  });

  it('finds a single inserted character', () => {
    expect(diffText('ab', 'axb')).toEqual({ index: 1, removed: 0, inserted: 'x' });
  });

  it('finds an appended character', () => {
    expect(diffText('ab', 'abc')).toEqual({ index: 2, removed: 0, inserted: 'c' });
  });

  it('finds a backspace', () => {
    expect(diffText('abc', 'ac')).toEqual({ index: 1, removed: 1, inserted: '' });
  });

  it('finds a select-and-replace', () => {
    expect(diffText('hello world', 'hello there')).toEqual({
      index: 6,
      removed: 5,
      inserted: 'there',
    });
  });

  it('finds a paste into the middle', () => {
    expect(diffText('ad', 'abcd')).toEqual({ index: 1, removed: 0, inserted: 'bc' });
  });

  it('handles clearing everything', () => {
    expect(diffText('abc', '')).toEqual({ index: 0, removed: 3, inserted: '' });
  });

  it('round-trips through the CRDT', () => {
    const doc = new RGA('a');
    const steps = ['h', 'he', 'hel', 'hello', 'hello!', 'hell!', 'hell', 'hello world', ''];
    for (const next of steps) {
      const change = diffText(doc.text, next);
      if (change === null) continue;
      if (change.removed > 0) doc.localDelete(change.index, change.removed);
      if (change.inserted.length > 0) doc.localInsert(change.index, change.inserted);
      expect(doc.text).toBe(next);
    }
  });
});
