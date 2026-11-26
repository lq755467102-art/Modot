// 将后端运行事实和角色记录转换为会话展示使用的标签与状态。
// 主要引用方包括 `RoleHeartbeat.tsx` 和 `StageProgress.tsx`。
import type {RoleProgress, Runtime, StageOverview, StageRun} from './generated';

const names: Record<string, string> = {planner: '规划者', executor: 'Modot', reviewer: '测试者'};
const order = Object.keys(names);
const labels: Record<string, string> = {
  not_run: '未启用', running: '运行中', completed: '阶段已结束', failed: '未成功结束',
  skipped: '已跳过', paused: '已暂停', timed_out: '复核超时', waiting_human: '待回应',
  unknown: '待核对', stopping: '正在停止',
};
export const roleName = (role: string) => names[role] || role;

// 呈现当前轮时，`currentRuntime` 来自独立运行通道；`requireCurrentRuntime` 要求进展记录与它一致。
type Options = {loading?: boolean; error?: boolean; empty?: boolean; currentRuntime?: Runtime; requireCurrentRuntime?: boolean};
type RoleEntry = [string, RoleProgress];
type Presentation =
  | {state: 'idle' | 'loading' | 'disconnected' | 'pending' | 'unknown'; message: string; previousRunId?: string}
  | {state: 'ready'; run: StageRun; runtime: Runtime; historical: boolean; roles: RoleEntry[]; inactive: RoleEntry[]};

// 合并进展索引与运行通道。当前运行待核对时保留历史轮标识，只把同轮可信记录显示为当前状态。
export function presentRun(data: StageOverview | undefined, options: Options = {}): Presentation {
  if (options.empty) return {state: 'idle', message: '开始对话后显示本轮进展'};
  if (options.error) return {state: 'disconnected', message: '进展连接中断，暂时无法核对状态', previousRunId: data?.run?.id};
  if (options.loading || !data) return {state: 'loading', message: '正在读取本轮进展…'};
  if (data.currentRunPending) return {state: 'pending', message: '正在核对当前轮进展…', previousRunId: data.run?.id};
  if (data.currentRunUnknown) return {state: 'unknown', message: '当前轮缺少可核对的阶段记录，结果未知。', previousRunId: data.run?.id};
  const run = data.run;
  if (!run) return data.runtime.active
    ? {state: 'pending', message: '正在核对当前轮进展…'}
    : {state: 'idle', message: '暂无已记录的运行进展'};
  const current = run.id === data.runtime.run_id;
  if ((data.runtime.active && !current) || (options.requireCurrentRuntime &&
      (!current || options.currentRuntime?.run_id !== run.id))) {
    return {state: 'unknown', message: '当前轮进展待核对，旧记录未作为本轮状态。', previousRunId: run.id};
  }
  if (options.requireCurrentRuntime && options.currentRuntime?.active !== data.runtime.active)
    return {state: 'pending', message: '正在核对当前轮运行状态…', previousRunId: run.id};
  const entries = Object.entries(run.roles).sort(([a], [b]) => {
    const rank = (role: string) => order.includes(role) ? order.indexOf(role) : order.length;
    return rank(a) - rank(b);
  });
  const inactive = entries.filter(([role, phase]) => run.crew === 'l0' && phase.state === 'not_run' &&
    phase.attempts === 0 && phase.source?.runId === run.id && phase.source.kind === 'run_config' &&
    (!phase.source.role || phase.source.role === role));
  return {state: 'ready', run, runtime: data.runtime, historical: !current,
    roles: entries.filter(entry => !inactive.includes(entry)), inactive};
}

// 以新鲜运行状态修正保留角色事件中的运行中标签，避免已停止的角色继续显示活跃。
export function roleStatus(role: string, phase: RoleProgress, runtime: Runtime) {
  let state = phase.state;
  if (state === 'running') {
    if (runtime.state === 'stopping') state = 'stopping';
    else if (!runtime.active || runtime.state === 'unknown') state = 'unknown';
  }
  const skipped = role === 'reviewer' && /无工具|无改动|没有独立可核查|无需/.test(phase.reason) ? '本轮无需检查'
    : role === 'planner' && /沿用|续跑|已有方向|已保存方向/.test(phase.reason) ? '沿用既有方向'
    : phase.reason.slice(0, 24);
  const label = role === 'reviewer' && state === 'failed' ? '复核未完成'
    : role === 'reviewer' && state === 'completed' ? '已提交复核意见'
    : role === 'executor' && state === 'completed' ? '执行已结束'
    : state === 'running' && phase.reason === '正在重连上游' ? '正在重连'
    : state === 'skipped' && skipped ? '已跳过 · ' + skipped
    : labels[state] || '状态待核对';
  return {state, label};
}
