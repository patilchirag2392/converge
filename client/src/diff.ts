/**
 * Turning a <textarea> into operations.
 *
 * The DOM hands us the whole new string on every change -- never "you typed x at
 * offset 12". So we recover the change by trimming the unchanged prefix and
 * suffix; whatever is left in the middle is what was replaced.
 *
 * Limitation, and it's a real one: this assumes a single contiguous change per
 * event. That holds for everything a textarea can do in one go (typing,
 * backspace, paste, select-and-replace), but it would be wrong for a multi-
 * cursor editor, which would need one change per cursor.
 */

export interface TextChange {
  /** Offset where the change begins. */
  index: number;
  /** How many characters were removed at `index`. */
  removed: number;
  /** What was inserted at `index`, after the removal. */
  inserted: string;
}

export function diffText(before: string, after: string): TextChange | null {
  if (before === after) return null;

  let start = 0;
  const shortest = Math.min(before.length, after.length);
  while (start < shortest && before[start] === after[start]) start += 1;

  let endBefore = before.length;
  let endAfter = after.length;
  while (endBefore > start && endAfter > start && before[endBefore - 1] === after[endAfter - 1]) {
    endBefore -= 1;
    endAfter -= 1;
  }

  return {
    index: start,
    removed: endBefore - start,
    inserted: after.slice(start, endAfter),
  };
}
