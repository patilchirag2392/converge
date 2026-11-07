import { describe, expect, it } from 'vitest';
import { resolveWsBase } from './useDocSocket';

const ec2 = { protocol: 'http:', host: 'ec2-54-1-2-3.compute-1.amazonaws.com' };

describe('resolveWsBase', () => {
  it('defaults to the dev server when unset', () => {
    expect(resolveWsBase(undefined, ec2)).toBe('ws://localhost:8000');
    expect(resolveWsBase('', ec2)).toBe('ws://localhost:8000');
  });

  it('uses an explicit URL as given', () => {
    expect(resolveWsBase('ws://localhost:8080', ec2)).toBe('ws://localhost:8080');
  });

  it('follows the page host for same-origin, so a new EC2 hostname needs no rebuild', () => {
    expect(resolveWsBase('same-origin', ec2)).toBe(
      'ws://ec2-54-1-2-3.compute-1.amazonaws.com',
    );
    expect(resolveWsBase('same-origin', { protocol: 'http:', host: 'localhost:8081' })).toBe(
      'ws://localhost:8081',
    );
  });

  it('switches to wss:// on an https page, which would otherwise be blocked as mixed content', () => {
    expect(resolveWsBase('same-origin', { protocol: 'https:', host: 'converge.example.com' })).toBe(
      'wss://converge.example.com',
    );
  });
});
