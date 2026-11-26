import {documentLanguage} from './editorSnippets';
import {UiIcon} from './UiIcon';

const labels:Record<string,string>={
  javascript:'JS',javascriptreact:'JS',typescript:'TS',typescriptreact:'TS',python:'Py',markdown:'#',css:'%',scss:'%',
};
const linkLabels:Record<string,string>={...labels,javascriptreact:'JSX',typescriptreact:'TSX'};

export function FileTypeIcon({path,filled=false}:{path:string;filled?:boolean}) {
  const name=path.split('/').pop()?.toLowerCase()||'';
  const language=documentLanguage(path);
  const settings=name.startsWith('.')||/^(?:dockerfile|makefile)$|\.config\.[\w]+$/.test(name)||['yaml','toml'].includes(language);
  const label=settings?undefined:(filled?linkLabels:labels)[language];
  const icon=settings?'settings':language.startsWith('json')?'braces':/\.(png|jpe?g|gif|webp|svg|ico)$/.test(name)?'image':language==='plaintext'?'document':'code';
  return <span className={filled?'message-file-icon':'workroom-file-icon'} data-language={settings?'config':language}
    data-symbol={filled?label:undefined} aria-hidden="true">
    {label ? (!filled && label) : <UiIcon name={icon}/>}
  </span>;
}
