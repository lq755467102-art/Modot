// 将执行设置编辑为普通会话的原生执行者偏好。
// 主要引用方包括 `ApplicationSettings.tsx` 和 `ExecutionControl.tsx`。
import type {ExecutionConfig} from './generated';

export function needsExecutor(config:ExecutionConfig){
  return config.crew!=='l0'||!!config.parallelRoles||!!config.backgroundPlanner;
}
/** Only future execution preferences change; recorded runs and limits stay intact. */
export function executorConfig(config:ExecutionConfig):ExecutionConfig{
  return needsExecutor(config)?{...config,crew:'l0',parallelRoles:false,backgroundPlanner:false}:config;
}
export function executorBody(config:ExecutionConfig){
  const value=executorConfig(config);
  return {crew:value.crew,maxTurns:value.maxTurns,modelCalls:value.modelCalls,charter:value.charter,
    parallelRoles:false,backgroundPlanner:false,workerContinuation:value.workerContinuation??'parent-run',
    ...(typeof value.contextLimit==='number'?{contextLimit:value.contextLimit}:{}),expectedRevision:value.revision};
}
