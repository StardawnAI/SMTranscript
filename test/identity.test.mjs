import { execFileSync } from 'node:child_process';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'));

for (const script of Object.values(pkg.bin)) {
  test(`${script} identifies AWG Social Media on initialize`, () => {
    const request = { jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-11-25' } };
    const stdout = execFileSync(process.execPath, [fileURLToPath(new URL('../' + script, import.meta.url))], {
      input: JSON.stringify(request) + '\n', encoding: 'utf8', stdio: ['pipe', 'pipe', 'pipe'], timeout: 10000,
    });
    const { serverInfo } = JSON.parse(stdout).result;
    assert.equal(serverInfo.name, pkg.name);
    assert.equal(serverInfo.version, pkg.version);
    assert.equal(serverInfo.title, 'AWG Social Media');
    assert.equal(serverInfo.websiteUrl, pkg.homepage);
    assert.deepEqual(serverInfo.icons, [512, 192].map((size) => ({
      src: `https://awg.stardawn-service.com/brand/awg-icon-${size}.png`,
      mimeType: 'image/png', sizes: [`${size}x${size}`],
    })));
  });
}
