// 集中处理应用图标、插件包自定义图标和缺省图标回退。
// 主要引用方包括 `pluginApplications.ts` 和 `ExtensionCard.tsx`。
import {useState,useSyncExternalStore,type ReactNode} from 'react';
import {loadJSON,saveJSON} from './client';
import {UiIcon} from './UiIcon';
import {useInstalledPackageIcon, type IconInstallation} from './extensionCatalog';

export const defaultApplicationIcons=[['extension','通用'],['code','代码'],['image','图像'],['video','视频'],['audio','音频'],['document','文档'],['tool','工具']] as const;
export type ApplicationIconKind=typeof defaultApplicationIcons[number][0];
export type IconChoice=ApplicationIconKind|'auto';
const storageKey='package-display-icons-v1',listeners=new Set<()=>void>();
let preferences:Record<string,IconChoice>|undefined;
const validChoice=(value:unknown):value is IconChoice=>value==='auto'||defaultApplicationIcons.some(([kind])=>kind===value);
// 通过 `client.ts` 读取历史偏好，缓存前筛掉过期或无效图标值。
function readPreferences(){
  if(!preferences){const stored=loadJSON<unknown>(storageKey,{});preferences=stored&&typeof stored==='object'&&!Array.isArray(stored)?Object.fromEntries(Object.entries(stored).filter(([,value])=>validChoice(value))):{};}
  return preferences;
}
window.addEventListener('storage',event=>{if(event.key==='workbench-v2-'+storageKey||event.key===null){preferences=undefined;listeners.forEach(listener=>listener());}});
// 订阅同窗口的偏好写入与跨窗口存储失效。
function subscribe(listener:()=>void){listeners.add(listener);return()=>listeners.delete(listener);}
// 同插件包的图标共用选择；未关联包时使用自动回退。
export function usePackageIcon(packageId?:string){
  return useSyncExternalStore(subscribe,()=>packageId?readPreferences()[packageId]||'auto':'auto');
}
// 读回本地持久值确认写入，再通知已经挂载的图标。
export function savePackageIcon(packageId:string,choice:IconChoice){
  const next={...readPreferences(),[packageId]:choice};saveJSON(storageKey,next);
  if(loadJSON<Record<string,IconChoice>>(storageKey,{})[packageId]!==choice)return false;
  preferences=next;listeners.forEach(listener=>listener());return true;
}

// Image bytes must already have been obtained by a trusted host adapter. No file/remote URLs.
function supportedImage(value?:string): value is string {
  return !!value && value.length < 262144 && /^data:image\/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\s]+$/.test(value);
}
// 字节虽通过格式筛选，渲染解码仍可能失败。由调用方提供下一层图标。
function ImageWithFallback({src,kind,fallback}:{src:string;kind:ApplicationIconKind;fallback:ReactNode}){
  const [failed,setFailed]=useState(false);
  return failed ? fallback : <span className="application-icon" data-icon-kind={kind} aria-hidden="true"><img src={src} alt="" onError={()=>setFailed(true)}/></span>;
}
// `imageDataUrl` 是应用贡献的主题图，`defaultImageDataUrl` 是缺省包图；`kind` 和 `fallbackLabel` 在内置回退时使用。
type IconProps={kind?:ApplicationIconKind; imageDataUrl?:string; defaultImageDataUrl?:string; fallbackLabel?:string};
type InstalledIconProps = {installation:IconInstallation; theme:string; catalogReady:boolean};

function InstalledPackageImage({installation, theme, catalogReady, ...props}: IconProps & InstalledIconProps) {
  const image = useInstalledPackageIcon(installation, theme, catalogReady);
  return <ApplicationIcon {...props} imageDataUrl={image}/>;
}

// 用户选择优先，随后使用已登记应用图标或当前安装声明的图标。
export function PackageDisplayIcon({packageId, installation, theme='default', catalogReady=true, ...props}:
  IconProps & Partial<InstalledIconProps> & {packageId?:string}) {
  const choice = usePackageIcon(packageId);
  if (choice === 'auto' && !props.imageDataUrl && !props.defaultImageDataUrl && installation?.iconThemes?.length) {
    return <InstalledPackageImage {...props} installation={installation} theme={theme} catalogReady={catalogReady}/>;
  }
  return <ApplicationIcon {...props} choice={choice}/>;
}
// 用户显式选图时沿用内置样式。自动模式依次尝试应用图、包缺省图和内置图标。
export function ApplicationIcon({kind='extension',choice='auto',imageDataUrl,defaultImageDataUrl,fallbackLabel}:IconProps & {choice?:IconChoice}){
  const actual=choice==='auto'?kind:choice;
  const paths={video:'M3 5h18v14H3zM8 5v14M16 5v14M3 10h5m8 0h5M3 14h5m8 0h5',audio:'M9 18V5l11-2v13M9 8l11-2M9 18c0 3-6 4-6 1s6-4 6-1Zm11-2c0 3-6 4-6 1s6-4 6-1Z',document:'M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6',tool:'m4 20 7-7M14 3a6 6 0 0 0-5 8l-7 7 4 4 7-7a6 6 0 0 0 8-7l-4 4-5-5 4-4Z'};
  const fallback=actual==='extension'||actual==='code'||actual==='image'?<UiIcon name={actual==='extension'?'extensions':actual}/>:<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d={paths[actual]}/></svg>;
  const defaultIcon = <><span className="application-icon" data-icon-kind={actual} aria-hidden="true">{fallback}</span>{fallbackLabel && <small>{fallbackLabel}</small>}</>;
  if (choice !== 'auto') return defaultIcon;
  const commonImage = supportedImage(defaultImageDataUrl)
    ? <ImageWithFallback key={defaultImageDataUrl} src={defaultImageDataUrl} kind={actual} fallback={defaultIcon}/>
    : defaultIcon;
  return imageDataUrl !== defaultImageDataUrl && supportedImage(imageDataUrl)
    ? <ImageWithFallback key={imageDataUrl} src={imageDataUrl} kind={actual} fallback={commonImage}/>
    : commonImage;
}
