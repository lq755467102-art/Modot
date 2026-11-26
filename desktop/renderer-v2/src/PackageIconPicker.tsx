// 编辑插件包在桌面应用中的本地图标选择。
// 主要引用方包括 `ExtensionCard.tsx` 和 `CatalogPackageManagement.tsx`。
import {useState} from 'react';
import {ApplicationIcon,defaultApplicationIcons,savePackageIcon,usePackageIcon,type IconChoice} from './ApplicationIcon';
import {Dialog} from './Dialog';

export function PackageIconPicker({packageId,name,onClose}:{packageId:string;name:string;onClose:()=>void}){
  const choice=usePackageIcon(packageId),[error,setError]=useState('');
  function choose(value:IconChoice){setError(savePackageIcon(packageId,value)?'':'图标设置未能保存，请检查本机存储后重试。');}
  return <div className="modal-backdrop"><Dialog label={name+' 显示图标'} className="package-icon-picker" onClose={onClose}>
    <header><h3>{name}</h3><button type="button" onClick={onClose}>完成</button></header>
    <p>选择本机显示图标，插件的启用和权限保持原设置。</p>
    <div className="package-icon-options" role="group" aria-label="默认图标">{defaultApplicationIcons.map(([kind,label])=><button type="button" key={kind} aria-pressed={choice===kind} aria-label={'使用'+label+'图标'} onClick={()=>choose(kind)}><ApplicationIcon kind={kind}/><span>{label}</span></button>)}</div>
    <button type="button" aria-pressed={choice==='auto'} onClick={()=>choose('auto')}>恢复默认图标</button>
    {error&&<p role="alert">{error}</p>}
  </Dialog></div>;
}
