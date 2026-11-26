// 把工作间命令输入或预设转成后端检查接口接收的参数数组。
// 主要引用方包括 `WorkroomPage.tsx`。
export function commandArguments(text:string):string[]{
  let value:unknown;
  try{value=JSON.parse(text);}catch{throw Error('命令参数需要有效的 JSON 数组。');}
  if(!Array.isArray(value)||!value.length||!value.every(item=>typeof item==='string'))throw Error('命令参数需要非空字符串数组。');
  if(!value[0].trim())throw Error('首项需要填写实际命令。');
  return value;
}
