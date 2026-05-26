#!/usr/bin/env node
/**
 * Capture dashboard screenshots for README.
 * Requires: server running (python3 server.py), puppeteer-core installed.
 */
import puppeteer from 'puppeteer-core';
import { mkdir } from 'fs/promises';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const OUT = path.join(ROOT, 'docs', 'screenshots');
const BASE = 'http://localhost:7432/jira_dashboard.html';
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

await mkdir(OUT, { recursive: true });

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox', '--disable-setuid-sandbox'],
});

const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900, deviceScaleFactor: 2 });

const delay = (ms) => new Promise((r) => setTimeout(r, ms));

async function shot(name, fn) {
  if (fn) await fn();
  await delay(1500);
  await page.screenshot({ path: path.join(OUT, name), fullPage: true });
  console.log('  ✓', name);
}

await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
await delay(3000);

await shot('overview.png');

await page.evaluate(() => {
  document.querySelector('.tab-btn[onclick*="mystuff"]')?.click();
});
await shot('my-stuff.png');

await page.evaluate(() => {
  document.querySelector('.tab-btn[onclick*="team"]')?.click();
});
await shot('team.png');

await page.evaluate(() => {
  document.querySelector('.tab-btn[onclick*="manager"]')?.click();
});
await shot('manager.png');

// Standup modal
await page.evaluate(() => {
  document.querySelector('.tab-btn[onclick*="overview"]')?.click();
});
await page.evaluate(() => { if (typeof openStandupModal === 'function') openStandupModal(); });
await shot('standup-modal.png', async () => {
  await page.evaluate(() => { if (typeof openStandupModal === 'function') openStandupModal(); });
});
await page.evaluate(() => {
  document.querySelectorAll('.modal-overlay.active .modal-close').forEach(b => b.click());
});

// Command palette
await page.keyboard.down('Meta');
await page.keyboard.press('k');
await page.keyboard.up('Meta');
await shot('command-palette.png');

await browser.close();
console.log('Done — screenshots in docs/screenshots/');
