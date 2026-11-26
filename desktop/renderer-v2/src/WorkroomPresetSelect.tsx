// 在工作间工具栏选择运行命令预设，并提供键盘导航。
// 主要引用方包括 `WorkroomPage.tsx`。
type Preset = {name:string; argv:string[]};

export function WorkroomPresetSelect({presets, current, dirty=false, onChoose}: {
  presets?:readonly Preset[]; current:string; dirty?:boolean; onChoose:(preset:Preset)=>void;
}) {
  return <select aria-label="运行预设" value="" onChange={event => {
    const preset = presets?.find(item => item.name === event.target.value);
    if (preset) onChoose(preset);
  }}>
    <option value="">{current}{dirty ? ' · 未保存' : ''}</option>
    {presets?.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}
  </select>;
}
