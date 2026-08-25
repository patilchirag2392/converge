import { chromium } from 'playwright-core';

const EXECUTABLE =
  process.env.CHROME_PATH ??
  '/Users/chiragpatil/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const DOC = `stage1-${Date.now()}`;
const URL = `http://localhost:5173/?doc=${DOC}`;

const browser = await chromium.launch({ executablePath: EXECUTABLE });
const errors = [];
const ctxA = await browser.newContext();
const ctxB = await browser.newContext();
const a = await ctxA.newPage();
const b = await ctxB.newPage();
for (const [name, page] of [['A', a], ['B', b]]) {
  page.on('pageerror', (e) => errors.push(`${name} pageerror: ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push(`${name} console: ${m.text()}`);
  });
}

const value = (page) => page.locator('.editor').inputValue();
const caret = (page) => page.locator('.editor').evaluate((el) => el.selectionStart);
const setCaret = (page, at) =>
  page.locator('.editor').evaluate((el, n) => {
    el.focus();
    el.setSelectionRange(n, n);
  }, at);
const waitForText = (page, expected) =>
  page.waitForFunction(
    (want) => document.querySelector('.editor')?.value === want,
    expected,
    { timeout: 5000 },
  );

await a.goto(URL);
await b.goto(URL);
await a.waitForSelector('.status-open');
await b.waitForSelector('.status-open');
await a.waitForFunction(() => document.querySelector('.meta')?.textContent?.includes('clients: 2'));
console.log('-- both tabs connected, clients: 2 --');

// 1. Sequential propagation.
await a.locator('.editor').click();
await a.locator('.editor').pressSequentially('hello world');
await waitForText(b, 'hello world');
console.log('sequential edit propagated:', JSON.stringify(await value(b)));

// 2. Caret survives a remote append. Stage 0 measured 2 -> 14 here.
await setCaret(b, 2);
const caretBeforeAppend = await caret(b);
await a.locator('.editor').press('End');
await a.locator('.editor').pressSequentially('!!');
await waitForText(b, 'hello world!!');
const caretAfterAppend = await caret(b);
console.log(`\n-- remote append, caret in tab B --`);
console.log(`  before: ${caretBeforeAppend}   after: ${caretAfterAppend}   (expected unchanged: 2)`);

// 3. Caret shifts correctly when the remote insert is BEFORE it.
await setCaret(b, 2);
await a.locator('.editor').press('Home');
await a.locator('.editor').pressSequentially('XYZ');
await waitForText(b, 'XYZhello world!!');
const caretAfterPrepend = await caret(b);
console.log(`\n-- remote insert of "XYZ" before the caret --`);
console.log(`  before: 2   after: ${caretAfterPrepend}   (expected shifted: 5)`);

// 4. The race: both tabs type at the same instant, at the same spot.
await setCaret(a, 0);
await setCaret(b, 0);
await Promise.all([
  a.locator('.editor').pressSequentially('AAA', { delay: 10 }),
  b.locator('.editor').pressSequentially('BBB', { delay: 10 }),
]);
await a.waitForTimeout(1200);

const aText = await value(a);
const bText = await value(b);
const server = await (await fetch(`http://localhost:8000/api/doc/${DOC}`)).json();

console.log('\n-- after typing in both tabs at once --');
console.log('tab A textarea :', JSON.stringify(aText));
console.log('tab B textarea :', JSON.stringify(bText));
console.log('server document:', JSON.stringify(server.text));

const agree = aText === bText && aText === server.text;
const keptBoth = aText.includes('AAA') && aText.includes('BBB');
console.log(`\nall three agree?        ${agree ? 'YES' : 'NO — diverged'}`);
console.log(`both edits survived?    ${keptBoth ? 'YES' : 'NO — data lost'}`);
console.log(`each word contiguous?   ${keptBoth ? 'YES (no interleaving)' : 'n/a'}`);

console.log('\nJS errors:', errors.length ? errors : 'none');
await a.screenshot({ path: 'stage1-tab-a.png', fullPage: true });
await browser.close();
