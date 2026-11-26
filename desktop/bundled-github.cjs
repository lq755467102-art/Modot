// Provision the shipped connector through the same versioned installer used by the UI.
// 主要引用方包括 `bridge-v2.cjs`。
const path = require('node:path');
const fs = require('node:fs/promises');
const {createHash} = require('node:crypto');

const endpoint = '/api/v2/plugins';
const initialInstall = 'modot-bundled-github-initial-install';
const installReceipt = '/api/operations/' + initialInstall;
const githubManifests = ['.codex-plugin/plugin.json', '.mcp.json'];

function packageId(fingerprint) {
  return createHash('sha256').update('github-mcp\0' + fingerprint).digest('hex').slice(0, 32);
}

function isGitHubConnector(pkg) {
  const server = pkg?.servers?.[0];
  return pkg?.name === 'github-mcp' && /^[a-f0-9]{64}$/.test(pkg.fingerprint) &&
    pkg.servers?.length === 1 && server?.supported && server.name === 'github' &&
    server.url === 'https://api.githubcopilot.com/mcp/';
}

module.exports = function bundledGitHub({request, packageRoot, dataRoot}) {
  const source = path.join(packageRoot, 'loop', 'plugin_catalog', 'github');
  const retained = id => path.join(dataRoot, 'runs', 'workbench', 'plugins', 'package-' + id);
  let pending;

  async function previewPackage(directory) {
    return request({method:'POST', path:endpoint + '/preview', body:{path:directory}});
  }

  // Windows checkouts used CRLF for the same manifests. Keep the original
  // installation identity and exact byte fingerprint after verifying both files.
  async function compatibleInstall(record, current) {
    if (!isGitHubConnector(record) || record.id !== packageId(record.fingerprint) ||
        current.fileCount !== githubManifests.length) return;
    const directory = retained(record.id);
    const previous = await previewPackage(directory);
    if (previous.status !== 200 || !isGitHubConnector(previous.body) ||
        previous.body.fingerprint !== record.fingerprint || previous.body.fileCount !== githubManifests.length) return;
    const normalize = text => text.replace(/\r\n/g, '\n');
    for (const file of githubManifests) {
      try {
        const [oldText, newText] = await Promise.all([
          fs.readFile(path.join(directory, file), 'utf8'),
          fs.readFile(path.join(source, file), 'utf8'),
        ]);
        if (normalize(oldText) !== normalize(newText)) return;
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
        return;
      }
    }
    return {id:record.id, fingerprint:record.fingerprint, path:directory};
  }

  async function prepare() {
    const preview = await previewPackage(source);
    if (preview.status !== 200) throw Error(preview.body?.error || '无法核对内置 GitHub 包。');
    const pkg = preview.body;
    if (!isGitHubConnector(pkg)) throw Error('内置 GitHub 包与桌面适配器不匹配。');

    // The store identifies a retained version by the package name and byte fingerprint.
    const id = packageId(pkg.fingerprint);
    const descriptor = {id, fingerprint:pkg.fingerprint, path:source};
    // Removal keeps this version directory. This also respects uninstalls from
    // before desktop provisioning existed, without opening the credential registry.
    try {
      await fs.lstat(retained(id));
      return descriptor;
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
    const receipt = (await request({path:installReceipt})).body?.operation;
    if (receipt?.state === 'completed' && receipt.response?.id) {
      const compatible = await compatibleInstall(receipt.response, pkg);
      return compatible || {...descriptor, notice:'已有 GitHub 安装，当前版本可在插件目录确认后安装。'};
    }
    // All windows and restarts share the backend's durable first-install receipt.
    // An explicit restore goes through the normal installer with its own receipt.
    const reply = await request({method:'POST', path:endpoint + '/install', requestId:initialInstall,
      body:{path:source, fingerprint:pkg.fingerprint}});
    if (reply.status === 200 && reply.body?.id === id) return descriptor;
    if (reply.status === 202) {
      for (let attempt = 0; attempt < 20; attempt++) {
        const {body} = await request({path:installReceipt});
        const operation = body?.operation;
        if (operation?.state === 'completed' && operation.response?.id === id) return descriptor;
        if (operation?.state !== 'processing' && operation?.state !== 'queued') break;
        await new Promise(resolve => setTimeout(resolve, 100));
      }
    }
    return {...descriptor, notice:reply.body?.error || '首次安装结果待核对，可在插件目录重新读取。'};
  }

  return () => {
    if (!pending) pending = prepare().finally(() => {pending = undefined;});
    return pending;
  };
};
