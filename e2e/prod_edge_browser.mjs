/**
 * Two browser tabs against a SINGLE-ORIGIN deployment (Stage 5 topology).
 *
 * The sibling script `two_instances_browser.mjs` assumes the Stage 4 layout:
 * client on :5173, and the instances individually reachable on :8001/:8002 so it
 * can interrogate each replica directly. Production closes both of those, which
 * is the point -- so this asserts what it can through the one public origin.
 *
 *   # local prod overlay
 *   node prod_edge_browser.mjs http://localhost:8081
 *
 *   # the EC2 box
 *   node prod_edge_browser.mjs http://ec2-1-2-3-4.compute-1.amazonaws.com
 *
 * Complements scripts/verify_deployment.py rather than duplicating it. That one
 * drives the protocol with a WebSocket client; this one drives a real browser,
 * so it is the only check that proves the *bundle's baked-in* WebSocket URL is
 * right. A wrong VITE_WS_URL passes every protocol-level test and still leaves
 * every user staring at a "connecting" pill.
 *
 * Retries until the two tabs land on different instances: nginx round-robins per
 * TCP connection, so two tabs opened back to back can land on the same one.
 * Exits 1 if it never manages it, or if the edit doesn't cross.
 */
import { chromium } from 'playwright-core';

const EXECUTABLE =
  process.env.CHROME_PATH ??
  '/Users/chiragpatil/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';

const BASE = (process.argv[2] ?? 'http://localhost:8081').replace(/\/$/, '');
const ATTEMPTS = Number(process.env.ATTEMPTS ?? 6);
// Generous: this may be crossing the public internet, not loopback.
const TIMEOUT = Number(process.env.TIMEOUT_MS ?? 20000);
const TEXT = 'typed in a real browser through the public edge';

const browser = await chromium.launch({ executablePath: EXECUTABLE });
const errors = [];

/** One tab, open on `doc`, waited until its socket reports open. */
async function openTab(doc) {
  const page = await (await browser.newContext()).newPage();
  page.on('pageerror', (e) => errors.push(String(e)));
  await page.goto(`${BASE}/?doc=${doc}`, { timeout: TIMEOUT });
  // The status pill going to "open" is the app's own signal that the WebSocket
  // connected -- which is exactly what a mis-baked VITE_WS_URL would prevent.
  await page.waitForSelector('.pill.status-open', { timeout: TIMEOUT });
  return page;
}

const instanceOf = (page) => page.$eval('.pill.instance', (e) => e.textContent.replace('instance:', '').trim());

let tabA;
let tabB;
let doc;

for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
  doc = `prod-${Date.now()}`;
  tabA = await openTab(doc);
  tabB = await openTab(doc);
  const [a, b] = [await instanceOf(tabA), await instanceOf(tabB)];
  if (a !== b) {
    console.log(`\ndoc: ${doc}`);
    console.log(`  tab A -> instance ${a}`);
    console.log(`  tab B -> instance ${b}   (different process)`);
    break;
  }
  console.log(`  attempt ${attempt}: both tabs landed on ${a}, retrying`);
  await tabA.context().close();
  await tabB.context().close();
  tabA = tabB = undefined;
}

if (!tabA || !tabB) {
  console.error(`\nFAIL: could not land two tabs on different instances in ${ATTEMPTS} attempts.`);
  console.error('Either only one instance is serving, or the balancer is not round-robining.');
  await browser.close();
  process.exit(1);
}

await tabA.fill('.editor', TEXT);

let crossed = true;
try {
  await tabB.waitForFunction(
    (t) => document.querySelector('.editor').value === t,
    TEXT,
    { timeout: TIMEOUT },
  );
} catch {
  crossed = false;
}

const seenByB = await tabB.$eval('.editor', (e) => e.value);
console.log(`\n  tab A typed : ${JSON.stringify(TEXT)}`);
console.log(`  tab B sees  : ${JSON.stringify(seenByB)}`);
console.log(`\n  edit crossed instances? ${crossed ? 'YES' : 'NO'}`);
console.log(`  JS errors:              ${errors.length ? errors.join('; ') : 'none'}`);

await tabA.screenshot({ path: 'prod-edge-tab-a.png' });
console.log('\n  screenshot: e2e/prod-edge-tab-a.png');

await browser.close();
process.exit(crossed && errors.length === 0 ? 0 : 1);
