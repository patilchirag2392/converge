import { afterEach, describe, expect, it, vi } from 'vitest';
import { randomClientId } from './useDocSocket';

describe('randomClientId', () => {
  afterEach(() => vi.restoreAllMocks());

  it('is 8 hex characters', () => {
    expect(randomClientId()).toMatch(/^[0-9a-f]{8}$/);
  });

  it('does not depend on crypto.randomUUID, which plain-HTTP pages lack', () => {
    // On http://<public host> the browser leaves randomUUID undefined; calling it
    // is what blanked the deployed page. Make any use of it fail loudly here.
    vi.spyOn(crypto, 'randomUUID').mockImplementation(() => {
      throw new Error('randomUUID is unavailable outside a secure context');
    });
    expect(randomClientId()).toMatch(/^[0-9a-f]{8}$/);
  });

  it('varies between calls', () => {
    const ids = new Set(Array.from({ length: 50 }, randomClientId));
    expect(ids.size).toBe(50);
  });
});
