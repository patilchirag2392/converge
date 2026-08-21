/**
 * A hand-written sequence CRDT: RGA (Replicated Growable Array), a.k.a. a causal tree.
 *
 * This is a deliberate mirror of server/app/crdt.py -- same algorithm, same
 * ordering rule, same field names. The two are pinned together by
 * shared/crdt-vectors.json, asserted from both crdt.test.ts and test_crdt.py.
 *
 * Three properties make `apply` safe to call with anything, in any order:
 *
 *   commutative  ops can arrive in any order and land in the same final state
 *   idempotent   applying the same op twice is a no-op
 *   causal-safe  an insert whose parent hasn't arrived yet is parked, not dropped
 */

/** Unique for all time, never reused, never changed once assigned. An operation
 *  names a *character* rather than a *position*, because positions shift under
 *  you and identities don't. */
export interface Id {
  client: string;
  counter: number;
}

/** "The very beginning of the document". Not a real character. */
export const ROOT: Id = { client: '', counter: 0 };

/** On the wire an id is `[client, counter]` -- see crdt.py's `to_id`. */
export type WireId = [string, number];

export type Operation =
  | { kind: 'insert'; id: WireId; parent: WireId; value: string }
  | { kind: 'delete'; id: WireId };

export interface Node {
  id: Id;
  parent: Id;
  value: string;
}

/**
 * Map key for an id, because JS Maps compare objects by reference. The
 * separator is NUL so it cannot collide with anything inside a client id, and
 * it's written as an escape rather than a literal byte to keep it visible in
 * source. Nothing relies on how this string sorts -- ordering always goes
 * through `compareIds`.
 */
export function idKey(id: Id): string {
  return `${id.counter}\u0000${id.client}`;
}

/**
 * Total order over ids; siblings are held in *descending* order.
 *
 * Doing two jobs at once:
 *
 * 1. It's a total order, so every replica independently computes the same
 *    sequence with no coordination. That's the deterministic-ordering guarantee.
 * 2. Descending puts a newly inserted child *before* its parent's older
 *    children. Not cosmetic -- required. In "ab", inserting "c" after "a" gives
 *    "a" two children, c and b. Newest-first yields "acb"; oldest-first would
 *    yield "abc", dropping the character in the wrong place.
 */
export function compareIds(a: Id, b: Id): number {
  if (a.counter !== b.counter) return a.counter - b.counter;
  return a.client < b.client ? -1 : a.client > b.client ? 1 : 0;
}

export function sameId(a: Id, b: Id): boolean {
  return a.counter === b.counter && a.client === b.client;
}

function toId(raw: unknown): Id {
  if (!Array.isArray(raw) || raw.length !== 2) {
    throw new Error('id must be [client, counter]');
  }
  const [client, counter] = raw as [unknown, unknown];
  if (typeof client !== 'string' || typeof counter !== 'number' || !Number.isInteger(counter)) {
    throw new Error('id must be [string, integer]');
  }
  return { client, counter };
}

function fromId(id: Id): WireId {
  return [id.client, id.counter];
}

export function insertOp(node: Node): Operation {
  return { kind: 'insert', id: fromId(node.id), parent: fromId(node.parent), value: node.value };
}

export function deleteOp(id: Id): Operation {
  return { kind: 'delete', id: fromId(id) };
}

type ParsedOp = { kind: 'insert'; node: Node } | { kind: 'delete'; id: Id };

/** Validate one operation off the wire without touching any document. */
export function parseOp(raw: unknown): ParsedOp {
  if (typeof raw !== 'object' || raw === null) throw new Error('operation must be an object');
  const op = raw as Record<string, unknown>;

  if (op.kind === 'insert') {
    if (typeof op.value !== 'string' || [...op.value].length !== 1) {
      throw new Error('insert.value must be exactly one character');
    }
    return { kind: 'insert', node: { id: toId(op.id), parent: toId(op.parent), value: op.value } };
  }
  if (op.kind === 'delete') {
    return { kind: 'delete', id: toId(op.id) };
  }
  throw new Error(`unknown operation kind: ${String(op.kind)}`);
}

export class RGA {
  readonly clientId: string;
  /** Operations that have taken effect. Display only. */
  applied = 0;

  private nodes = new Map<string, Node>();
  private children = new Map<string, Id[]>();
  /** Keyed by idKey, holding the id itself so snapshots never have to parse a
   *  key back apart. Python's equivalent is a plain set, since tuples hash. */
  private deleted = new Map<string, Id>();
  private pending = new Map<string, Node[]>();
  private counter = 0;

  constructor(clientId: string) {
    this.clientId = clientId;
  }

  // ---------------------------------------------------------------- reading

  /**
   * Depth-first walk, yielding visible nodes in document order. A node's
   * children come immediately after it, siblings in `compareIds` order.
   *
   * Iterative on purpose: typing n characters in a row builds a chain n deep,
   * so recursion would overflow the stack on any real document.
   */
  private *walk(): Generator<Node> {
    const roots = this.children.get(idKey(ROOT));
    const stack: Id[] = roots ? [...roots].reverse() : [];

    while (stack.length > 0) {
      const id = stack.pop()!;
      const key = idKey(id);
      if (!this.deleted.has(key)) yield this.nodes.get(key)!;

      const kids = this.children.get(key);
      if (kids) {
        for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
      }
    }
  }

  get text(): string {
    let out = '';
    for (const node of this.walk()) out += node.value;
    return out;
  }

  /** Ids of visible characters, in order. Index i is offset i in `text`. */
  visibleIds(): Id[] {
    const ids: Id[] = [];
    for (const node of this.walk()) ids.push(node.id);
    return ids;
  }

  // -------------------------------------------- applying ops, in any order

  apply(raw: unknown): void {
    this.applyParsed(parseOp(raw));
  }

  applyParsed(op: ParsedOp): void {
    if (op.kind === 'insert') this.applyInsert(op.node);
    else this.applyDelete(op.id);
  }

  private knows(id: Id): boolean {
    return sameId(id, ROOT) || this.nodes.has(idKey(id));
  }

  private applyInsert(node: Node): void {
    if (this.nodes.has(idKey(node.id))) return; // idempotent

    // Lamport-style clock: never hand out an id whose counter is lower than one
    // we've already seen, so "newer" stays meaningful across replicas.
    this.counter = Math.max(this.counter, node.id.counter);

    if (!this.knows(node.parent)) {
      // Causally not ready: we haven't seen the character this attaches to.
      // Park it -- do NOT drop it -- and link it when the parent lands.
      const waiting = this.pending.get(idKey(node.parent));
      if (waiting) waiting.push(node);
      else this.pending.set(idKey(node.parent), [node]);
      return;
    }

    // Linking can unblock nodes that arrived early, which can unblock more.
    // Worklist rather than recursion, for the same stack reason as walk().
    const ready: Node[] = [node];
    while (ready.length > 0) {
      const current = ready.pop()!;
      const key = idKey(current.id);
      if (this.nodes.has(key)) continue;

      this.counter = Math.max(this.counter, current.id.counter);
      this.link(current);

      const unblocked = this.pending.get(key);
      if (unblocked) {
        this.pending.delete(key);
        ready.push(...unblocked);
      }
    }
  }

  private link(node: Node): void {
    this.nodes.set(idKey(node.id), node);
    const parentKey = idKey(node.parent);
    const siblings = this.children.get(parentKey);
    if (siblings) {
      siblings.push(node.id);
      siblings.sort((a, b) => compareIds(b, a)); // descending
    } else {
      this.children.set(parentKey, [node.id]);
    }
    this.applied += 1;
  }

  /**
   * Tombstone: mark invisible, keep the id.
   *
   * The character can't actually be removed, because an operation still in
   * flight may say "insert after that one" and would have nothing to attach to.
   *
   * Recording deletions as a *set of ids* also means a delete can arrive before
   * its insert and still be correct -- the character is simply invisible the
   * moment it shows up. Idempotent and order-independent for free.
   */
  private applyDelete(id: Id): void {
    const key = idKey(id);
    if (this.deleted.has(key)) return;
    this.deleted.set(key, id);
    this.applied += 1;
  }

  // ------------------------------------------------------------ local edits

  private nextId(): Id {
    this.counter += 1;
    return { client: this.clientId, counter: this.counter };
  }

  /** Insert `value` at visible offset `index`; returns ops to broadcast. */
  localInsert(index: number, value: string): Operation[] {
    const visible = this.visibleIds();
    if (index < 0 || index > visible.length) {
      throw new Error(`insert index ${index} outside 0..${visible.length}`);
    }

    let parent = index > 0 ? visible[index - 1] : ROOT;
    const ops: Operation[] = [];
    for (const char of value) {
      const node: Node = { id: this.nextId(), parent, value: char };
      this.applyInsert(node);
      ops.push(insertOp(node));
      // Chain onto the previous character rather than anchoring everything to
      // the same parent, so a typed or pasted run stays contiguous instead of
      // being interleaved by a concurrent insert at the same spot.
      parent = node.id;
    }
    return ops;
  }

  /** Delete `length` visible characters from `index`; returns ops. */
  localDelete(index: number, length: number): Operation[] {
    const visible = this.visibleIds();
    const ops: Operation[] = [];
    for (const id of visible.slice(index, index + length)) {
      this.applyDelete(id);
      ops.push(deleteOp(id));
    }
    return ops;
  }

  // -------------------------------------------------------------- snapshot

  /** Every operation needed to rebuild this document from empty. Order is not
   *  causal -- the receiver buffers anything early. */
  snapshotOps(): Operation[] {
    const ops: Operation[] = [];
    for (const node of this.nodes.values()) ops.push(insertOp(node));
    for (const id of this.deleted.values()) ops.push(deleteOp(id));
    return ops;
  }
}
