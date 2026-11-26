// 文件树与标签共用图标规则。目录按需读取，已有文件的草稿和选区仍由编辑器持有。
// `WorkroomPage.tsx` 提供文件树的数据与回调，`WorkroomFileTabs.tsx` 复用同一套文件图标。
import { useUiLanguage } from "./UiLanguage";
import {useEffect,useState} from 'react';
import {useQuery} from '@tanstack/react-query';
import {api,queryString} from './client';
import {FileTypeIcon} from './FileTypeIcon';
import {UiIcon} from './UiIcon';

// 文件列表接口返回的条目包含资源内路径、显示名称和目录标记。
export type FileEntry={path:string;name:string;directory:boolean};
// `base` 与 `workspace` 指向工作间选定的检出目录，`active` 和 `dirty` 由打开标签的状态派生。
type TreeProps={base:string;workspace:string;active?:string;folder:string;dirty:Set<string>;onFolder:(path:string)=>void;onFile:(path:string)=>void};
type BranchProps=TreeProps&{entries:FileEntry[];expanded:Set<string>;toggle:(path:string)=>void};

// 目录第一次展开时才通过 `client.ts` 的 `api` 请求内容。缓存按工作间和资源内路径区分，失败时可重试。
function Directory({path,...props}: Omit<BranchProps,'entries'>&{path:string}) {
  const { t } = useUiLanguage();
  const listing=useQuery<{entries:FileEntry[]}>({queryKey:['files',props.workspace,path],queryFn:()=>api(props.base+'/files?path='+queryString(path))});
  if(listing.error)return <p role="alert">{t("目录读取失败。")}<button onClick={()=>void listing.refetch()}>{t("重试目录")}</button></p>;
  if(!listing.data)return <small role="status">{t("读取目录…")}</small>;
  if(!listing.data.entries.length)return <small>{t("空目录")}</small>;
  return <Branch {...props} entries={listing.data.entries}/>;
}
// 渲染一层条目，展开的目录递归加载子项，文件则交给工作间选择或打开。
// FileTypeIcon 复用 editorSnippets 的语言识别，配置文件的图标优先于语言标签。
function Branch(props:BranchProps) {
  const { t } = useUiLanguage();
  return <ul className="workroom-tree">{props.entries.map(file=>{
    const open=props.expanded.has(file.path);
    return <li key={file.path}><button type="button" className="file-row" title={file.path}
      aria-current={!file.directory&&props.active===file.path?'page':undefined} aria-expanded={file.directory?open:undefined}
      onClick={()=>file.directory?props.toggle(file.path):props.onFile(file.path)}>
      {file.directory?<><span className="tree-chevron" aria-hidden="true">{open?'⌄':'›'}</span><UiIcon name={open?'folder-open':'folder'}/></>:<FileTypeIcon path={file.path}/>}
      <span className="tree-name">{file.name}</span>{props.dirty.has(file.path)&&<i className="workroom-dirty-dot" aria-label={t("未保存")}/>}
    </button>{file.directory&&open&&<Directory {...props} path={file.path}/>}</li>;
  })}</ul>;
}
// 外部定位目录时自动补齐祖先展开状态，保留用户已经展开的其他目录。
export function WorkroomFileTree(props:TreeProps&{entries:FileEntry[]}) {
  const [expanded,setExpanded]=useState<Set<string>>(()=>new Set());
  useEffect(()=>{
    if(!props.folder)return;
    const parts=props.folder.split('/');
    setExpanded(old=>new Set([...old,...parts.map((_,i)=>parts.slice(0,i+1).join('/'))]));
  },[props.folder]);
  // 展开后使用该路径作为当前目录。折叠当前目录或其祖先时，将工作间的目录选择移回上一级。
  function toggle(path:string){
    const next=new Set(expanded),closing=next.delete(path);
    if(!closing)next.add(path);
    setExpanded(next);
    if(!closing)props.onFolder(path);
    else if(props.folder===path||props.folder.startsWith(path+'/'))props.onFolder(path.split('/').slice(0,-1).join('/'));
  }
  return <Branch {...props} expanded={expanded} toggle={toggle}/>;
}
