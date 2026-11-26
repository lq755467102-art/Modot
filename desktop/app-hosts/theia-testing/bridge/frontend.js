// All extension-specific behaviour stays in the publisher's Testing API controller.
import { formattingDocuments } from './formatting.js';
import { ContainerModule } from '@theia/core/shared/inversify';
import { FrontendApplicationContribution } from '@theia/core/lib/browser/frontend-application-contribution.js';
import { open, OpenerService } from '@theia/core/lib/browser/opener-service.js';
import URI from '@theia/core/lib/common/uri.js';
import { WorkspaceService } from '@theia/workspace/lib/browser/workspace-service.js';
import { HostedPluginServer } from '@theia/plugin-ext/lib/common/plugin-protocol.js';
import { TestService, TestRunProfileKind, TestExecutionState } from '@theia/test/lib/browser/test-service.js';

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(read, message, timeout = 30000) {
  const end = Date.now() + timeout;
  do {
    const value = await read();
    if (value) return value;
    await pause(150);
  } while (Date.now() < end);
  throw Error(typeof message === 'function' ? message() : message);
}
function sameFile(left, right) {
  if (!left || !right) return false;
  const canonical = (value) =>
    decodeURIComponent(new URL(value.toString()).pathname).replace(
      /^\/([A-Z]):/,
      (_, drive) => '/' + drive.toLowerCase() + ':',
    );
  return canonical(left) === canonical(right);
}
function fileItems(items, target) {
  return items.flatMap((item) => (sameFile(item.uri, target) ? [item] : fileItems(item.tests, target)));
}
// Theia may expose either LSP ranges or VS Code ranges in a TestMessage.
function messageView(message) {
  const start = message.location?.range?.start ?? message.location?.range?._start;
  const line = start?.line ?? start?._line,
    column = start?.character ?? start?._character;
  const text = typeof message.message === 'string' ? message.message : message.message?.value;
  return {
    message: (text ?? '').slice(0, 5000),
    expected: message.expected?.slice(0, 1000),
    actual: message.actual?.slice(0, 1000),
    uri: message.location?.uri?.toString(),
    line: Number.isInteger(line) ? line + 1 : undefined,
    column: Number.isInteger(column) ? column + 1 : undefined,
  };
}
function collect(run) {
  const cases = [],
    totals = {},
    limit = 60;
  let omitted = 0,
    messageBudget = 24000;
  function visit(item) {
    const observed = run.getTestState(item);
    if (observed && (!item.tests.length || observed.messages?.length)) {
      const state = TestExecutionState[observed.state] ?? 'Unknown';
      totals[state] = (totals[state] ?? 0) + 1;
      const failed = state === 'Failed' || state === 'Errored';
      if (cases.length >= limit && failed) {
        const index = cases.findLastIndex((c) => !['Failed', 'Errored'].includes(c.state));
        if (index >= 0) {
          cases.splice(index, 1);
          omitted++;
        }
      }
      if (cases.length >= limit) omitted++;
      else {
        const messages = (observed.messages ?? []).slice(0, 3).map(messageView);
        for (const m of messages) {
          m.message = m.message.slice(0, messageBudget);
          messageBudget -= m.message.length;
        }
        cases.push({ name: item.label.slice(0, 200), uri: item.uri?.toString(), state, messages });
      }
    }
    for (const child of item.tests) visit(child);
  }
  run.items.forEach(visit);
  return { controller: run.controller.id, providerRunId: run.id, totals, cases, omitted };
}
export default new ContainerModule((bind) => {
  const format = formattingDocuments(bind, sameFile);
  bind(FrontendApplicationContribution)
    .toDynamicValue(({ container }) => ({
      onStart() {
        const service = container.get(TestService),
          plugins = container.get(HostedPluginServer);
        globalThis.modotAppTesting = async (args) => {
          const { extension, workspace, file, runId } = args;
          const roots = await container.get(WorkspaceService).roots;
          if (roots.length !== 1 || !sameFile(roots[0].resource, workspace))
            throw Error('工作间检出目录不匹配。');
          await until(async () => {
            const deployed = await plugins.getDeployedPluginIds();
            if (deployed.some((id) => id !== extension))
              throw Error('宿主中还有其他扩展，无法核实结果的归属。');
            return deployed.includes(extension);
          }, '未部署所选版本的扩展。');
          const openFile = () => open(container.get(OpenerService), new URI(file));
          if (args.slot === 'vscode.formatting/1') return format(args, openFile, until);
          await openFile();
          await until(() => service.getControllers().length, '扩展未登记 Testing API 控制器。');
          // Opening a file starts discovery. Refreshing now can dispose the very run profile we are about to use.
          // Give the initial tree its full discovery window. Providers that discover only on refresh get one retry.
          let refreshAfter = Date.now() + 30000;
          let missing = '扩展没有在所选文件中发现测试。';
          const { c, items, profile } = await until(() => {
            if (service.isRefreshing) return;
            const found = service
              .getControllers()
              .map((c) => ({ c, items: fileItems(c.tests, file) }))
              .filter((m) => m.items.length);
            if (!found.length) {
              missing = '扩展没有在所选文件中发现测试。';
              if (Date.now() >= refreshAfter) {
                refreshAfter = Infinity;
                service.refresh();
              }
              return;
            }
            if (found.length !== 1) throw Error('多个测试控制器认领了此文件，无法唯一选择。');
            const { c, items } = found[0];
            const runProfiles = c.testRunProfiles.filter((p) => p.kind === TestRunProfileKind.Run);
            if (!runProfiles.length) {
              missing = '扩展已发现测试文件，但没有登记运行测试配置。';
              return;
            }
            const candidates = runProfiles.length === 1 ? runProfiles : runProfiles.filter((p) => p.isDefault);
            if (candidates.length !== 1) throw Error('没有唯一的运行测试配置。');
            return { c, items, profile: candidates[0] };
          }, () => missing, 60000);
          const before = new Set(c.testRuns.map((r) => r.id));
          profile.run(runId, items, [], true);
          const run = await until(() => c.testRuns.find((r) => !before.has(r.id)), '扩展未创建测试运行。');
          await until(() => !run.isRunning, '测试运行超时。', 120000);
          return collect(run);
        };
      },
    }))
    .inSingletonScope();
});
