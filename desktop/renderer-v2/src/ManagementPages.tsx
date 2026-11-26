// 承载记忆入口并根据已启用的记忆实现选择对应查看面板。
// 主要引用方包括 `App.tsx`。
import {useApplicationSettings} from './ApplicationSettings';
import {MemoryTreePage} from './MemoryTreePage';
import {MemoryProviderControl} from './MemoryProviderControl';

export function MemoryPage({visible,conversationId,onManageMemory}:{visible:boolean;conversationId:string;onManageMemory:()=>void}){
  const {preferences,theme,settingsVisible}=useApplicationSettings();
  return <div className={'management-page legacy-conversation theme-'+theme} data-theme={theme} style={{fontSize:preferences.fontSize}}>
    <header className="management-page-heading"><h1>记忆</h1><MemoryProviderControl compact visible={visible} onManage={onManageMemory}/></header>
    <div className="memory-session-page"><MemoryTreePage conversationId={conversationId} visible={visible&&!settingsVisible}/></div>
  </div>;
}
