/**
 * Two browser tabs, forced onto different instances, through nginx.
 * Verifies Stage 3 in the real UI rather than just at the protocol level.
 */
import { chromium } from 'playwright-core';

const EXECUTABLE =
  process.env.CHROME_PATH ??
  '/Users/chiragpatil/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const DOC = `browser-${Date.now()}`;
const URL = `http://localhost:5173/?doc=${DOC}`;

const browser = await chromium.launch({ executablePath: EXECUTABLE });
const errors = [];

async function openTab() {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`console: ${m.text()}`);
  });
  await page.goto(URL);
  await page.waitForSelector('.status-open');
  await page.waitForFunction(() =>
    /instance: \S+/.test(document.querySelector('.meta')?.textContent ?? ''),
  );
  const instance = (await page.locator('.meta').innerText()).match(/instance: (\S+)/)[1];
  return { ctx, page, instance };
}

// Keep opening tabs until two of them are on different instances.
const tabs = [];
let a = null;
let b = null;
for (let i = 0; i < 10 && !(a && b); i++) {
  const tab = await openTab();
  tabs.push(tab);
  if (!a) a = tab;
  else if (tab.instance !== a.instance) b = tab;
}
if (!b) {
  console.log('could not get two tabs on different instances');
  await browser.close();
  process.exit(1);
}

console.log(`doc: ${DOC}`);
console.log(`  tab A -> instance ${a.instance}`);
console.log(`  tab B -> instance ${b.instance}  (different process)`);

const value = (t) => t.page.locator('.editor').inputValue();
const waitForText = (t, want) =>
  t.page.waitForFunction(
    (w) => document.querySelector('.editor')?.value === w,
    want,
    { timeout: 8000 },
  );

// 1. A types, B must see it — through Redis.
await a.page.locator('.editor').click();
await a.page.locator('.editor').pressSequentially('typed on instance A');
await waitForText(b, 'typed on instance A');
console.log(`\n1. A typed -> B sees: ${JSON.stringify(await value(b))}`);

// 2. B types back.
await b.page.locator('.editor').click();
await b.page.locator('.editor').press('End');
await b.page.locator('.editor').pressSequentially(' + instance B');
await waitForText(a, 'typed on instance A + instance B');
console.log(`2. B typed -> A sees: ${JSON.stringify(await value(a))}`);

// 3. Caret survives a cross-instance edit (the Stage 1 property, still holding).
await b.page.locator('.editor').click();
await b.page.locator('.editor').evaluate((el) => {
  el.focus();
  el.setSelectionRange(5, 5);
});
const caretBefore = await b.page.locator('.editor').evaluate((el) => el.selectionStart);
await a.page.locator('.editor').press('End');
await a.page.locator('.editor').pressSequentially('!!');
await waitForText(b, 'typed on instance A + instance B!!');
const caretAfter = await b.page.locator('.editor').evaluate((el) => el.selectionStart);
console.log(`3. caret in B across a remote-instance edit: ${caretBefore} -> ${caretAfter}`);

// 4. Concurrent typing on two different instances.
await a.page.locator('.editor').evaluate((el) => {
  el.focus();
  el.setSelectionRange(0, 0);
});
await b.page.locator('.editor').evaluate((el) => {
  el.focus();
  el.setSelectionRange(0, 0);
});
await Promise.all([
  a.page.locator('.editor').pressSequentially('AAA', { delay: 12 }),
  b.page.locator('.editor').pressSequentially('BBB', { delay: 12 }),
]);
await a.page.waitForTimeout(1500);

const aText = await value(a);
const bText = await value(b);
const fromInstanceA = await (await fetch(`http://127.0.0.1:8001/api/doc/${DOC}`)).json();
const fromInstanceB = await (await fetch(`http://127.0.0.1:8002/api/doc/${DOC}`)).json();

console.log('\n4. both tabs typed at once, on different instances:');
console.log('   tab A      :', JSON.stringify(aText));
console.log('   tab B      :', JSON.stringify(bText));
console.log('   instance a :', JSON.stringify(fromInstanceA.text));
console.log('   instance b :', JSON.stringify(fromInstanceB.text));

const allAgree =
  aText === bText && aText === fromInstanceA.text && aText === fromInstanceB.text;
console.log(`\nall four agree?     ${allAgree ? 'YES' : 'NO'}`);
console.log(`both edits kept?    ${aText.includes('AAA') && aText.includes('BBB') ? 'YES' : 'NO'}`);
console.log(`caret preserved?    ${caretAfter === caretBefore ? 'YES' : `NO (${caretBefore}->${caretAfter})`}`);

for (const port of [8001, 8002]) {
  const m = await (await fetch(`http://127.0.0.1:${port}/metrics`)).json();
  console.log(
    `  instance ${m.instance}: docs=${m.documents_cached} propagation=${JSON.stringify(m.cross_instance_propagation)}`,
  );
}

console.log('\nJS errors:', errors.length ? errors : 'none');
await a.page.screenshot({ path: 'stage3-tab-a.png', fullPage: true });
await browser.close();
