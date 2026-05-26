#!/usr/bin/env node
/** Record tab-switching demo as animated GIF for README. */
import puppeteer from 'puppeteer-core';
import { mkdir, writeFile, readFile, unlink } from 'fs/promises';
import { execSync } from 'child_process';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.join(__dirname, '..');
const TMP = path.join(ROOT, 'docs', 'gifs', '_frames');
const OUT = path.join(ROOT, 'docs', 'gifs', 'tab-tour.gif');
const BASE = 'http://localhost:7432/jira_dashboard.html';
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const delay = (ms) => new Promise((r) => setTimeout(r, ms));

await mkdir(TMP, { recursive: true });
await mkdir(path.dirname(OUT), { recursive: true });

const browser = await puppeteer.launch({
  executablePath: CHROME,
  headless: true,
  args: ['--no-sandbox'],
});
const page = await browser.newPage();
await page.setViewport({ width: 1280, height: 720, deviceScaleFactor: 1 });
await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
await delay(2500);

const tabs = ['overview', 'mystuff', 'team', 'manager', 'overview'];
let i = 0;
for (const tab of tabs) {
  await page.evaluate((t) => {
    document.querySelector(`.tab-btn[onclick*="${t}"]`)?.click();
  }, tab);
  await delay(800);
  const buf = await page.screenshot({ type: 'png' });
  await writeFile(path.join(TMP, `frame-${String(i++).padStart(3, '0')}.png`), buf);
}

await browser.close();

// Try ffmpeg, then ImageMagick convert
const frames = path.join(TMP, 'frame-%03d.png');
try {
  execSync(
    `ffmpeg -y -framerate 2 -i "${frames}" -vf "scale=960:-1:flags=lanczos,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse" "${OUT}"`,
    { stdio: 'pipe' }
  );
  console.log('✓ GIF via ffmpeg:', OUT);
} catch {
  try {
    execSync(`convert -delay 50 -loop 0 "${TMP}"/frame-*.png "${OUT}"`, { stdio: 'pipe' });
    console.log('✓ GIF via ImageMagick:', OUT);
  } catch (e) {
    console.warn('No ffmpeg/ImageMagick — install ffmpeg to generate GIF. Frames saved in', TMP);
    process.exit(0);
  }
}

// cleanup frames
for (const f of await import('fs').then((m) => m.promises.readdir(TMP))) {
  await unlink(path.join(TMP, f)).catch(() => {});
}
