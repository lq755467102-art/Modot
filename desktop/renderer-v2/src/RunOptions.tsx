
// 编辑下一次运行可使用的上下文窗口和执行轮次，空值保留默认配置。

// 这是下次运行的草稿。`ctx` 或 `max_turns` 为空时交给模型或内核使用默认值。
export type RunSettings = { crew: string; ctx?: number; max_turns?: number };
// 让父组件保存会话的下次运行参数，输入数值受对应预算范围限制。
export function RunOptions({
  value,
  onChange,
}: {
  value: RunSettings;
  onChange: (value: RunSettings) => void;
}) {
  return (
    <div className="run-options">
      <span>下一次新运行 · Modot</span>
      <label>
        上下文窗口{" "}
        <input
          type="number"
          min={1024}
          max={2000000}
          placeholder="使用模型配置"
          value={value.ctx ?? ""}
          onChange={(e) =>
            onChange({
              ...value,
              ctx: e.target.value
                ? Math.max(1024, Math.min(2000000, Number(e.target.value)))
                : undefined,
            })
          }
        />
      </label>
      <label>
        最大执行轮次{" "}
        <input
          type="number"
          min={1}
          max={1000}
          placeholder="使用内核默认"
          value={value.max_turns ?? ""}
          onChange={(e) =>
            onChange({
              ...value,
              max_turns: e.target.value
                ? Math.max(1, Math.min(1000, Number(e.target.value)))
                : undefined,
            })
          }
        />
      </label>
      <small>
        仅作用于本会话的下一次运行，运行中的轮次保持原选项。旁问固定只读。
      </small>
    </div>
  );
}
