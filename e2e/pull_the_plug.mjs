/**
 * The honest Stage 2 test: kill the server under a live browser tab.
 *
 * 1. type in a tab, confirm Postgres has it
 * 2. kill the server process
 * 3. keep typing while it's down
 * 4. restart it
 * 5. the tab reconnects, the offline typing is flushed, and everything is durable
 */
import { chromium } from 'playwright-core';
import { execSync, spawn } from 'node:child_process';

const EXECUTABLE =
  process.env.CHROME_PATH ??
  '/Users/chiragpatil/Library/Caches/ms-playwright/chromium-1234/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing';
const SERVER_DIR = '/Users/chiragpatil/Developer/converge/server';
const DOC = `plug-${Date.now()}`;
const URL = `http://localhost:5173/?doc=${DOC}`;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function serverUp() {
  try {
    const r = await fetch('http://localhost:8000/healthz');
    return r.ok;
  } catch {
    return false;
  }
}

async function waitFor(label, predicate, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await predicate()) return true;
    await sleep(250);
  }
  throw new Error(`timed out waiting for ${label}`);
}

function killServer() {
  try {
    execSync("pkill -f 'uvicorn app.main:app'");
  } catch {
    /* nothing running */
  }
}

function startServer() {
  const child = spawn('uv', ['run', 'uvicorn', 'app.main:app', '--port', '8000'], {
    cwd: SERVER_DIR,
    detached: true,
    stdio: 'ignore',
  });
  child.unref();
}

const serverText = async () =>
  (await (await fetch(`http://localhost:8000/api/doc/${DOC}`)).json()).text;

const browser = await chromium.launch({ executablePath: EXECUTABLE });
const errors = [];
const page = await browser.newPage();
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));

const value = () => page.locator('.editor').inputValue();
const pills = () => page.locator('.meta').innerText();

await page.goto(URL);
await page.waitForSelector('.status-open');
await page.locator('.editor').click();
await page.locator('.editor').pressSequentially('written before the crash');
await waitFor('server to store the text', async () => (await serverText()) === 'written before the crash');

console.log('1. typed with the server up');
console.log('   tab   :', JSON.stringify(await value()));
console.log('   server:', JSON.stringify(await serverText()));

// ---- pull the plug -------------------------------------------------------
killServer();
await waitFor('server to be down', async () => !(await serverUp()));
await page.waitForSelector('.status-closed', { timeout: 15000 });
console.log('\n2. server killed — tab status pill:', (await pills()).match(/closed|open|connecting/)[0]);
console.log('   tab still shows:', JSON.stringify(await value()));

// ---- keep typing while it is down ---------------------------------------
await page.locator('.editor').click();
await page.locator('.editor').press('End');
await page.locator('.editor').pressSequentially(' + typed while down');
const queuedPill = (await pills()).match(/queued: \d+/)?.[0] ?? 'none';
console.log('\n3. typed with no server. tab:', JSON.stringify(await value()));
console.log('   queued indicator:', queuedPill);

// ---- restart ------------------------------------------------------------
startServer();
await waitFor('server to come back', serverUp, 30000);
await page.waitForSelector('.status-open', { timeout: 20000 });
console.log('\n4. server restarted — tab reconnected');

await waitFor(
  'offline edits to reach Postgres',
  async () => (await serverText()) === 'written before the crash + typed while down',
  15000,
);

const tabText = await value();
const dbText = await serverText();
console.log('   tab   :', JSON.stringify(tabText));
console.log('   server:', JSON.stringify(dbText));
console.log('   queued now:', (await pills()).match(/queued: \d+/)?.[0] ?? 'none (flushed)');

// ---- and it is genuinely durable, not just in memory --------------------
killServer();
await waitFor('second shutdown', async () => !(await serverUp()));
startServer();
await waitFor('second restart', serverUp, 30000);
const afterSecondRestart = await serverText();
console.log('\n5. killed and restarted again, straight from Postgres:');
console.log('   server:', JSON.stringify(afterSecondRestart));

console.log('\n--- results ---');
console.log(`tab and server agree?          ${tabText === dbText ? 'YES' : 'NO'}`);
console.log(`offline typing survived?       ${dbText.includes('typed while down') ? 'YES' : 'NO'}`);
console.log(`durable across two restarts?   ${afterSecondRestart === dbText ? 'YES' : 'NO'}`);
console.log('JS errors:', errors.length ? errors : 'none');

await page.screenshot({ path: 'stage2-after-restart.png', fullPage: true });
await browser.close();
