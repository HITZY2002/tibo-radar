#!/usr/bin/env node
/** Independent, read-only Firefox collector for the AstrBot coverage contract. */
'use strict';

const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

function argumentsFrom(argv) {
  const options = { handle: '', limit: 30, days: 7, profile: '', firefox: '', stateFile: '' };
  const names = { '--handle': 'handle', '--limit': 'limit', '--days': 'days',
    '--profile': 'profile', '--firefox': 'firefox', '--state-file': 'stateFile' };
  for (let index = 0; index < argv.length; index++) {
    if (argv[index] === '--since-id') { index++; continue; }
    if (!names[argv[index]]) throw new Error('Unknown collector argument');
    options[names[argv[index]]] = argv[++index] || '';
  }
  options.handle = String(options.handle).replace(/^@/, '').toLowerCase();
  if (!/^[a-z0-9_]{1,15}$/.test(options.handle)) throw new Error('Invalid X handle');
  options.limit = Math.max(1, Math.min(200, Number(options.limit) || 30));
  options.days = Math.max(1, Math.min(30, Number(options.days) || 7));
  return options;
}

function postsFromResponse(payload, handle) {
  const output = [];
  const stack = [payload];
  const visited = new Set();
  while (stack.length) {
    const value = stack.pop();
    if (!value || typeof value !== 'object' || visited.has(value)) continue;
    visited.add(value);
    const legacy = value.legacy;
    const user = value.core?.user_results?.result;
    const author = user?.legacy?.screen_name || user?.core?.screen_name;
    const id = String(value.rest_id || legacy?.id_str || '');
    if (legacy && typeof legacy.full_text === 'string' && /^\d+$/.test(id) &&
        String(author || '').toLowerCase() === handle && !legacy.retweeted_status_result &&
        !legacy.retweeted_status && !legacy.full_text.startsWith('RT @')) {
      const longText = value.note_tweet?.note_tweet_results?.result?.text;
      const incomplete = Boolean(legacy.truncated && !longText);
      const timestamp = Date.parse(legacy.created_at || '');
      output.push({ id, author: handle, text: longText || legacy.full_text,
        timestamp: Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : '',
        url: `https://x.com/${handle}/status/${id}`,
        isReply: Boolean(legacy.in_reply_to_status_id_str), isQuote: Boolean(legacy.is_quote_status),
        isRetweet: false, isTruncated: incomplete, content_complete: !incomplete });
    }
    stack.push(...Object.values(value).filter(child => child && typeof child === 'object'));
  }
  return output;
}

function mergePosts(posts, handle, limit = 200) {
  const unique = new Map();
  for (const post of posts) {
    if (!/^\d+$/.test(String(post.id || '')) || !post.text ||
        String(post.author || '').toLowerCase() !== handle || post.isRetweet !== false) continue;
    const previous = unique.get(post.id);
    if (!previous || (post.content_complete && !previous.content_complete) ||
        (Boolean(post.content_complete) === Boolean(previous.content_complete) &&
          post.text.length > previous.text.length)) unique.set(post.id,
            { ...post, url: `https://x.com/${handle}/status/${post.id}` });
  }
  return [...unique.values()].sort((left, right) =>
    BigInt(left.id) > BigInt(right.id) ? -1 : BigInt(left.id) < BigInt(right.id) ? 1 : 0
  ).slice(0, limit);
}

function firefoxFiles(options) {
  const executable = options.firefox || [
    path.join(process.env.PROGRAMFILES || 'C:/Program Files', 'Mozilla Firefox/firefox.exe'),
    path.join(process.env['PROGRAMFILES(X86)'] || 'C:/Program Files (x86)', 'Mozilla Firefox/firefox.exe'),
    '/usr/bin/firefox', '/Applications/Firefox.app/Contents/MacOS/firefox'
  ].find(candidate => fs.existsSync(candidate));
  if (!executable || !fs.existsSync(executable)) throw new Error('Firefox executable not found');
  let profile = options.profile;
  if (!profile) {
    const root = process.platform === 'win32'
      ? path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData/Roaming'), 'Mozilla/Firefox')
      : process.platform === 'darwin'
        ? path.join(os.homedir(), 'Library/Application Support/Firefox')
        : path.join(os.homedir(), '.mozilla/firefox');
    const ini = fs.readFileSync(path.join(root, 'profiles.ini'), 'utf8');
    const profiles = ini.split(/\r?\n\[/).map(section => {
      const fields = Object.fromEntries(section.split(/\r?\n/).filter(line =>
        /^[A-Za-z]+=/.test(line)).map(line => [line.slice(0, line.indexOf('=')), line.slice(line.indexOf('=') + 1)]));
      return fields.Path ? { path: fields.IsRelative === '0' ? fields.Path : path.join(root, fields.Path),
        preferred: fields.Default === '1' } : null;
    }).filter(Boolean);
    profile = (profiles.find(item => item.preferred) || profiles[0])?.path;
  }
  if (!profile || !fs.existsSync(profile)) throw new Error('Firefox login profile not found');
  return { executable, profile };
}

function snapshotCookies(profile) {
  const snapshot = fs.mkdtempSync(path.join(os.tmpdir(), 'astrbot-tibo-'));
  if (!path.resolve(snapshot).startsWith(path.resolve(os.tmpdir()) + path.sep)) {
    throw new Error('Temporary profile path is outside the expected directory');
  }
  try {
    // Never copy saved passwords, keys, extensions or session history.
    for (const filename of ['cookies.sqlite', 'cookies.sqlite-wal', 'cookies.sqlite-shm']) {
      const source = path.join(profile, filename);
      if (fs.existsSync(source)) fs.copyFileSync(source, path.join(snapshot, filename));
    }
    if (!fs.existsSync(path.join(snapshot, 'cookies.sqlite'))) throw new Error('No Firefox login cookies found');
    return snapshot;
  } catch (error) {
    fs.rmSync(snapshot, { recursive: true, force: true });
    throw error;
  }
}

async function visiblePosts(page, handle) {
  return page.evaluate(target => {
    const output = [];
    for (const article of document.querySelectorAll('article[data-testid="tweet"]')) {
      const time = article.querySelector('time');
      const link = time?.closest('a[href]');
      const match = link ? new URL(link.href).pathname.match(/^\/([^/]+)\/status\/(\d+)/) : null;
      if (!match || match[1].toLowerCase() !== target) continue;
      const context = article.querySelector('[data-testid="socialContext"]')?.textContent || '';
      if (/reposted|retweeted|转帖|转推|转发/i.test(context)) continue;
      const text = article.querySelector('[data-testid="tweetText"]')?.innerText || '';
      if (!text.trim()) continue;
      const incomplete = Boolean(article.querySelector('[data-testid="tweet-text-show-more-link"]'));
      const translated = /translated by|翻译来自|由.*翻译/i.test(article.textContent || '');
      output.push({ id: match[2], author: target, text, timestamp: time.dateTime || '',
        url: `https://x.com/${target}/status/${match[2]}`, isReply: /replying to|回复/i.test(article.innerText || ''),
        isQuote: Boolean(article.querySelector('[data-testid="quoteTweet"]')), isRetweet: false,
        isTruncated: incomplete, content_complete: !incomplete && !translated });
    }
    return output;
  }, handle);
}

async function collect(options) {
  const { executable, profile } = firefoxFiles(options);
  const snapshot = snapshotCookies(profile);
  let browser;
  const requests = new Set();
  const originals = new Map();
  const report = { posts: [], sources: {}, expanded: 0, details_failed: [] };
  const cutoff = Date.now() - options.days * 86400000;
  try {
    const puppeteer = require('puppeteer-core');
    browser = await puppeteer.launch({ browser: 'firefox', executablePath: executable,
      userDataDir: snapshot, headless: true, timeout: 45000,
      env: { ...process.env, MOZ_HEADLESS: '1', MOZ_CRASHREPORTER_DISABLE: '1' } });
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 900 });
    page.on('response', response => {
      if (!response.url().includes('/graphql/') || response.status() !== 200) return;
      const pending = response.json().then(json => {
        for (const post of postsFromResponse(json, options.handle)) {
          const previous = originals.get(post.id);
          if (!previous || post.text.length > previous.text.length) originals.set(post.id, post);
        }
      }).catch(() => {}).finally(() => requests.delete(pending));
      requests.add(pending);
    });
    for (const route of ['timeline', 'with_replies']) {
      let posts = [];
      try {
        const url = `https://x.com/${options.handle}${route === 'with_replies' ? '/with_replies' : ''}`;
        await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
        await page.waitForSelector('article[data-testid="tweet"]', { timeout: 25000 });
        let unchanged = 0;
        for (let turn = 0; turn < 35; turn++) {
          await Promise.allSettled([...requests]);
          const before = posts.length;
          posts = mergePosts([...posts, ...await visiblePosts(page, options.handle), ...originals.values()], options.handle);
          unchanged = posts.length === before ? unchanged + 1 : 0;
          if (posts.length >= options.limit || unchanged >= 5) break;
          await page.evaluate(() => window.scrollBy(0, 1000));
          await delay(800);
        }
        if (!posts.length) throw new Error('No authored posts could be verified on this route');
        report.sources[route] = { ok: true, count: posts.length };
        report.posts.push(...posts);
      } catch (error) {
        report.sources[route] = { ok: false, error: error.message };
      }
    }
    report.posts = mergePosts(report.posts.filter(post =>
      !post.timestamp || Date.parse(post.timestamp) >= cutoff), options.handle, options.limit);
    if (!Object.values(report.sources).some(source => source.ok)) throw new Error('Both X timeline routes failed');
    // Retry incomplete pending posts as well as truncated items in the new timeline.
    if (options.stateFile && fs.existsSync(options.stateFile)) {
      const state = JSON.parse(fs.readFileSync(options.stateFile, 'utf8'));
      const pending = Object.values(state.pending_posts || {}).map(item => item.post || item)
        .filter(post => post.content_complete === false);
      report.posts = mergePosts([...report.posts, ...pending], options.handle, options.limit);
    }
    for (const post of report.posts.filter(item => !item.content_complete).slice(0, 10)) {
      try {
        await page.goto(post.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
        await page.waitForSelector('article[data-testid="tweet"]', { timeout: 15000 });
        const more = await page.$('[data-testid="tweet-text-show-more-link"]');
        if (more) await more.click();
        await delay(1000);
        await Promise.allSettled([...requests]);
        const full = mergePosts([...await visiblePosts(page, options.handle), ...originals.values()], options.handle)
          .find(item => item.id === post.id && item.content_complete);
        if (!full) throw new Error('Original full text could not be confirmed');
        Object.assign(post, full);
        report.expanded++;
      } catch (error) {
        post.fetch_error = error.message;
        report.details_failed.push(post.id);
      }
    }
    return report;
  } finally {
    try { if (browser) await browser.close(); }
    finally {
      const expected = path.join(os.tmpdir(), 'astrbot-tibo-');
      if (path.resolve(snapshot).startsWith(expected)) fs.rmSync(snapshot, { recursive: true, force: true });
    }
  }
}

if (require.main === module) {
  collect(argumentsFrom(process.argv.slice(2))).then(report => console.log(JSON.stringify(report)))
    .catch(error => { console.error(`[fetch] ${error.message}`); process.exitCode = 1; });
}
module.exports = { argumentsFrom, postsFromResponse, mergePosts, snapshotCookies, collect };
