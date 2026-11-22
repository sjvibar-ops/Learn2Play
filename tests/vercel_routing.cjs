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
    "import * as routing from './vercel.mjs'; console.log(JSON.stringify({ exports: Object.keys(routing), config: routing.config }));",
  ], { cwd: resolve(__dirname, '..'), env, encoding: 'utf8' });
}

const origin = 'https://learn2play-staging.onrender.com';

for (const environment of ['preview', 'production', undefined, '', 'development', 'staging', 'constructor']) {
  test(`VERCEL_ENV ${JSON.stringify(environment)} uses the Staging catch-all`, () => {
    const result = evaluateConfig(environment);
    assert.equal(result.status, 0, result.stderr);
    const { exports, config } = JSON.parse(result.stdout);
    assert.deepEqual(exports, ['config']);
    assert.deepEqual(config, {
      version: 2,
      rewrites: [{ source: '/:path*', destination: `${origin}/:path*` }],
    });

    const rewrite = config.rewrites[0];
    assert.ok(Object.hasOwn(rewrite, 'destination'));
    const template = new URL(rewrite.destination);
    assert.equal(template.protocol, 'https:');
    assert.equal(template.pathname, '/:path*');

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
