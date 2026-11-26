// 呈现预设应用、已安装插件页面和设置入口，统一处理侧栏键盘导航。
// 主要引用方包括 `App.tsx`。

import { useUiLanguage } from "./UiLanguage";
import type {KeyboardEvent} from 'react';
import {PackageDisplayIcon} from './ApplicationIcon';
import type {InstalledApplication} from './pluginApplications';
import {builtinApplications,type BuiltinApplicationId} from './builtinApplications';
import {UiIcon} from './UiIcon';
import './application-shell.css';

// Each application entry has an installed package and a page supplied by its host adapter.
export type PluginApplicationShortcut=Pick<InstalledApplication,'id'|'pageId'|'name'|'packageId'|'icon'|'imageDataUrl'|'defaultImageDataUrl'|'dataPageRevision'>
  & {pkg?:InstalledApplication['pkg'];available:boolean;reason?:string};
// 按安装身份绘制页面入口，将不可用的入口转到应用管理。
function PluginShortcut({entry,selected,theme,onSelect,onManage}:{entry:PluginApplicationShortcut;selected:boolean;theme:string;onSelect:(id:string)=>void;onManage:()=>void}){
  return <button type="button" data-plugin-application={entry.pageId} data-plugin-package={entry.packageId}
    aria-label={entry.name+(entry.available?'':' · 暂不可用')} title={entry.name+(entry.available?'':' · '+(entry.reason||'暂不可用，前往插件管理'))} aria-current={selected?'page':undefined} data-available={entry.available} onClick={()=>entry.available?onSelect(entry.id):onManage()}>
    <PackageDisplayIcon packageId={entry.dataPageRevision === undefined ? entry.packageId : undefined}
      installation={entry.dataPageRevision === undefined ? entry.pkg : undefined} theme={theme} catalogReady={entry.available}
      kind={entry.icon} imageDataUrl={entry.imageDataUrl}
      defaultImageDataUrl={entry.defaultImageDataUrl} fallbackLabel={entry.name}/>
  </button>;
}
// 从 `App.tsx` 接收当前应用及已核对的插件页，按页面实例身份定位快捷入口。
export function ApplicationNavigation({surface,settingsOpen,onSelect,onSettings,onApplications,applications=[],theme='default',onSelectApplication}:{surface:string;settingsOpen:boolean;theme?:string;onSelect:(id:BuiltinApplicationId)=>void;onSettings:()=>void;onApplications:()=>void;applications?:readonly PluginApplicationShortcut[];onSelectApplication?:(id:string)=>void}){
  const { t } = useUiLanguage();
  // 把方向键和首尾键映射到可见导航项的焦点，激活仍由按钮本身处理。
  function navigate(event:KeyboardEvent<HTMLElement>){
    if(event.nativeEvent.isComposing||event.keyCode===229||!['ArrowDown','ArrowUp','Home','End'].includes(event.key))return;
    const buttons=Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button')).filter(button=>!button.disabled&&button.getClientRects().length);
    const current=buttons.indexOf((event.target as HTMLElement).closest('button')!);if(current<0)return;
    const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(current+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;
    event.preventDefault();const target=buttons[next];target.focus({preventScroll:true});
    const list=target.closest<HTMLElement>('.application-shortcuts');if(list){const box=list.getBoundingClientRect(),item=target.getBoundingClientRect();if(item.top<box.top)list.scrollTop-=box.top-item.top;else if(item.bottom>box.bottom)list.scrollTop+=item.bottom-box.bottom;}
  }
  return <nav className="surface-navigation application-navigation" aria-label={t("应用视图")} onKeyDown={navigate}>
    <div className="builtin-application-navigation" role="group" aria-label={t("Modot 预设应用")}>{builtinApplications.map(entry=><button type="button" key={entry.id} data-builtin-app={entry.id} aria-label={t(entry.name)} title={t(entry.name)} aria-current={!settingsOpen&&surface===entry.id?'page':undefined} onClick={()=>onSelect(entry.id)}>
      <UiIcon name={entry.icon==='conversation'?'compose':entry.icon==='workroom'?'code':entry.icon}/><small>{entry.id==='extensions'?t("插件"):entry.id==='schedules'?t("定时"):t(entry.name)}</small>
    </button>)}</div>
    <div role="separator" aria-label={t("预设应用与插件应用分区")} className="application-navigation-divider"/>
    <div className="application-shortcuts" role="group" aria-label={t("插件应用")}>{applications.map(entry=><PluginShortcut key={entry.id} entry={entry} theme={theme} selected={!settingsOpen&&surface==='plugin:'+entry.id} onSelect={onSelectApplication||onApplications} onManage={onApplications}/>)}
      <button type="button" className="browse-plugin-applications" aria-label={t("查看插件应用")} title={applications.length?t("查看应用与宿主"):t("插件应用尚未接入，查看应用与宿主")} onClick={onApplications}><UiIcon name="plus"/><small>{t("应用")}</small></button>
    </div>
    <div className="application-navigation-bottom"><button type="button" className="application-settings-trigger" aria-label={t("设置")} title={t("设置")} aria-current={settingsOpen?'page':undefined} disabled={settingsOpen} onClick={onSettings}><UiIcon name="gear"/><small>{t("设置")}</small></button></div>
  </nav>;
}
