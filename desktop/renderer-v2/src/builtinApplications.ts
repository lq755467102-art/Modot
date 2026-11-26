// 统一定义桌面预设应用的标识、标签与图标。
// 主要引用方包括 `App.tsx` 和 `ApplicationNavigation.tsx`。
export const builtinApplications=[
  {id:'agent',name:'对话',description:'与默认Modot交流，查看运行进展与结果。',icon:'conversation'},
  {id:'workbench',name:'工作间',description:'在项目实际目录中查看、编辑文件并使用运行工具。',icon:'workroom'},
  {id:'schedules',name:'定时任务',description:'管理计划、查看运行事件并打开关联会话。',icon:'calendar'},
  {id:'memory',name:'记忆',description:'查看和管理带来源、作用域的记忆内容。',icon:'memory'},
  {id:'extensions',name:'插件管理',description:'统一管理插件安装、启停、连接和可用能力。',icon:'extensions'},
] as const;
export type BuiltinApplicationId=typeof builtinApplications[number]['id'];
