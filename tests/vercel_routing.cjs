const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const { resolve } = require('node:path');
const { test } = require('node:test');

function evaluateConfig(environment) {
  const env = { ...process.env };
  delete env.VERCEL_ENV;
  if (environment !== undefined) {
    env.VERCEL_ENV = environment;
  }
  return spawnSync(process.execPath, [
    '--input-type=module', '--eval',
    "import { config } from './vercel.mjs'; console.log(JSON.stringify(config));",
  ], { cwd: resolve(__dirname, '..'), env, encoding: 'utf8' });
}

for (const [environment, origin] of [
  ['preview', 'https://learn2play-staging.onrender.com'],
  ['production', 'https://learn2play.onrender.com'],
]) {
  test(`${environment} emits the existing native catch-all for its own origin`, () => {
    const result = evaluateConfig(environment);
    assert.equal(result.status, 0, result.stderr);
    const config = JSON.parse(result.stdout);
    assert.deepEqual(config, {
      version: 2,
      rewrites: [{ source: '/:path*', destination: `${origin}/:path*` }],
    });

    // Check the route template contract. Vercel performs the actual forwarding.
    const { destination } = config.rewrites[0];
    for (const path of ['/', '/static/js/sessions.js', '/api/bookings',
      '/api/me/discord/callback?code=a%2Bb&state=x&state=y']) {
      const request = new URL(path, 'https://learn2play.example');
      const target = new URL(destination.replace(':path*', request.pathname.slice(1)));
      assert.equal(target.search, '');
      target.search = request.search;
      assert.equal(target.href, `${origin}${path}`);
    }
  });
}

for (const environment of [undefined, '', 'development', 'staging', 'constructor']) {
  test(`rejects VERCEL_ENV ${JSON.stringify(environment)}`, () => {
    const result = evaluateConfig(environment);
    assert.equal(result.status, 1, result.stderr);
    assert.equal(result.stdout, '');
    assert.match(result.stderr, /Set VERCEL_ENV to preview or production/);
  });
}
