// 共用插件与扩展卡片样式，展示已声明的能力、来源和启用状态。
// 主要引用方包括 `CatalogPackageManagement.tsx` 和 `CatalogSkillDetail.tsx`。
import {useEffect,useLayoutEffect,useRef,useState,type ReactNode} from "react";
import {ApplicationIcon,usePackageIcon} from './ApplicationIcon';
import {PackageIconPicker} from './PackageIconPicker';
import {UiIcon} from './UiIcon';
import './application-shell.css';

export function ExtensionCard({name, description, status, onOpen, pluginId}: {
  name: string;
  description: string;
  status: string;
  onOpen: () => void;
  pluginId?: string;
}) {
  const choice=usePackageIcon(pluginId),[picking,setPicking]=useState(false),container=useRef<HTMLDivElement>(null);
  useEffect(()=>{if(!picking)return;const observer=new ResizeObserver(()=>{if(!container.current?.getClientRects().length)setPicking(false);});if(container.current)observer.observe(container.current);return()=>observer.disconnect();},[picking]);
  const card=<button type="button" className="extension-card" aria-haspopup="dialog"
    aria-label={"查看 "+name+" 详情"} data-plugin-id={pluginId} onClick={onOpen}>
    {pluginId?<span className="extension-card-title"><ApplicationIcon choice={choice}/><strong>{name}</strong></span>:<strong>{name}</strong>}
    <span className="extension-card-description">{description || "暂无用途说明"}</span>
    <small>{status}</small>
  </button>;
  if(!pluginId)return card;
  return <div ref={container} className="plugin-package-card">{card}<button type="button" className="package-icon-trigger" aria-label={'更改 '+name+' 的显示图标'} title="选择本机显示图标" onClick={()=>setPicking(true)}><UiIcon name="settings"/></button>{picking&&<PackageIconPicker packageId={pluginId} name={name} onClose={()=>setPicking(false)}/>}</div>;
}

export function ExtensionDetails({title, onClose, children, busy=false}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  busy?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useLayoutEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className="extension-details" aria-label={title}
    onCancel={event => {event.preventDefault();if(!busy)onClose();}}>
    <header className="tool-title"><h2>{title}</h2>
      <button type="button" onClick={onClose} disabled={busy}>关闭详情</button>
    </header>
    {children}
  </dialog>;
}
