// 把原生文件夹选择交给项目入口，并将选中的目录绑定为项目资源。
// 主要引用方包括 `AgentHome.tsx` 和 `ProjectScope.tsx`。
import {api} from './client';
import type {Project} from './generated';

export async function chooseFolderProject():Promise<Project|null>{
  const path=await window.workbench.chooseProject('project');
  return path?api<Project>('/api/v2/projects',{path,confirm:true}):null;
}
