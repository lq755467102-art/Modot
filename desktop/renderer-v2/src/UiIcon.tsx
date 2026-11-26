// 集中管理应用内的图标名称与映射。
export function UiIcon({name}:{name:'info'|'image'|'sidebar'|'right-sidebar'|'overview'|'agents'|'sources'|'branch'|'terminal'|'code'|'browser'|'memory'|'model'|'settings'|'gear'|'send'|'stop'|'plus'|'activity'|'extensions'|'compose'|'search'|'more'|'close'|'shield'|'back'|'forward'|'reload'|'log-end'|'sort'|'collapse'|'open'|'folder'|'folder-open'|'paperclip'|'save'|'document'|'braces'|'calendar'|'hand'}){
  const paths={image:'M4 4h16v16H4zM8 8h.01M4 17l5-5 3 3 4-5 4 5',sidebar:'M3 5h18v14H3zM9 5v14',compose:'M14 4l6 6m-3-8 5 5-11 11-6 1 1-6L17 2ZM12 4H4v16h16v-8',extensions:'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z',code:'m8 7-5 5 5 5m8-10 5 5-5 5m-3-13-2 16',browser:'M3 6h18v14H3zM3 10h18M7 7.5h.01M10 7.5h.01',memory:'M5 5c0-3 14-3 14 0s-14 3-14 0v14c0 3 14 3 14 0V5M5 12c0 3 14 3 14 0',model:'m12 3 9 5v8l-9 5-9-5V8l9-5Zm0 0v18M3 8l9 5 9-5',settings:'M4 7h16M4 17h16M8 4v6m8 4v6',send:'M12 20V4m-6 6 6-6 6 6',stop:'M6 6h12v12H6z',plus:'M12 5v14M5 12h14',activity:'M3 12h4l3-7 4 14 3-7h4'};
  const extra={hand:'M8 13V5a1.5 1.5 0 0 1 3 0v6m0-6V3.5a1.5 1.5 0 0 1 3 0V11m0-5a1.5 1.5 0 0 1 3 0v6m0-3a1.5 1.5 0 0 1 3 0v6c0 3-2 6-6 6h-2c-2 0-3-1-4-3l-4-5c-1-2 1-3 2-2l2 2Z',search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',more:'M5 12h.01M12 12h.01M19 12h.01',close:'m6 6 12 12M6 18 18 6',shield:'m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Zm0 4v6m0 3h.01'};
  const navigation={back:'M19 12H5m7-7-7 7 7 7',forward:'M5 12h14m-7-7 7 7-7 7',reload:'M20 4v6h-6m6 0a8 8 0 1 0 .2 4','log-end':'M12 3v12m-5-5 5 5 5-5M5 20h14',sort:'M7 4v16m-4-4 4 4 4-4M14 6h7m-7 6h5m-5 6h3',collapse:'m6 9 6-5 6 5M6 15l6 5 6-5',open:'M13 5h6v6m0-6-8 8m7 0v6H5V6h6'};
  const side={ 'right-sidebar':'M6 5h12a3 3 0 0 1 3 3v8a3 3 0 0 1-3 3H6a3 3 0 0 1-3-3V8a3 3 0 0 1 3-3Zm9 0v14',overview:'M11 7h9m-9 10h9M7 7a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm0 10a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z',agents:'M12 3v6m-8 4h16M4 13v5m8-9v9m8-5v5M2 18h4v3H2zm8 0h4v3h-4zm8 0h4v3h-4z',sources:'M10 13a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-2 2m2 6a4 4 0 0 0-6 0l-4 4a4 4 0 0 0 6 6l2-2',branch:'M6 7v10m12-9c0 5-2 6-8 6H6m2-9a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm0 14a2 2 0 1 1-4 0 2 2 0 0 1 4 0Zm12-13a2 2 0 1 1-4 0 2 2 0 0 1 4 0Z',terminal:'M5 4h14a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm2 5 3 3-3 3m6 0h4'};
  const gear = 'M9 3h6l.7 2.6 2.6 1.5 2.6-.7 2 3.5-1.9 1.9v3l1.9 1.9-2 3.5-2.6-.7-2.6 1.5-.7 2.6H9l-.7-2.6-2.6-1.5-2.6.7-2-3.5L3 14.8v-3L1.1 9.9l2-3.5 2.6.7 2.6-1.5L9 3ZM15 13.3a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z';
  const folders = {
    info:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM12 11v6m0-10v.3',
    paperclip:'m8 14 7-7a2 2 0 0 1 3 3l-9 9a4 4 0 0 1-6-6l10-10a5 5 0 0 1 7 7L10 20',
    save:'M4 3h12l5 5v12H3V4Zm3 0v6h9V3M7 20v-7h10v7',
    document:'M5 3h9l5 5v12H5zM14 3v6h5M8 13h8m-8 3h7',
    braces:'M9 3H7c-2 0-2 2-2 4v2c0 2-1 3-3 3 2 0 3 1 3 3v2c0 2 0 4 2 4h2m6-18h2c2 0 2 2 2 4v2c0 2 1 3 3 3-2 0-3 1-3 3v2c0 2 0 4-2 4h-2',
    folder: 'M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z',
    'folder-open': 'M3 17V7a2 2 0 0 1 2-2h4l2 2h7a2 2 0 0 1 2 2v1M5 19a2 2 0 0 1-2-2l3-6h16l-2.7 6.8a2 2 0 0 1-1.9 1.2z',
  };
  const calendar = 'M5 6h14a1 1 0 0 1 1 1v12H4V7a1 1 0 0 1 1-1Zm3-3v5m8-5v5M4 10h16M8 14h2m4 0h2';
  return <svg width="18" height="18" viewBox="0 0 24 24" fill={name==='stop'?'currentColor':'none'} stroke="currentColor" strokeWidth={name==='more'?3:'1.6'} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d={{...paths,...extra,...navigation,...side,...folders,gear,calendar}[name]}/></svg>;
}
