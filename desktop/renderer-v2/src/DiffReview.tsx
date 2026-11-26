/** Each operation is bound to the exact revision that produced this patch. */
// 主要引用方包括 `WorkroomPage.tsx`。
export function DiffReview({
  text,
  staged,
  truncated,
  onStage,
}: {
  text: string;
  staged: boolean;
  truncated: boolean;
  onStage?: (patch: string) => void;
}) {
  const lines = text.split("\n"),
    starts = lines.flatMap((line, i) => (line.startsWith("@@ ") ? [i] : []));
  if (!starts.length) return <pre>{text || "当前范围没有文本差异。"}</pre>;
  const header = lines.slice(0, starts[0]).join("\n") + "\n";
  return (
    <>
      <pre className="diff-header">{header}</pre>
      {starts.map((start, i) => {
        const hunk = lines
          .slice(start, starts[i + 1] ?? lines.length)
          .join("\n")
          .replace(/\n*$/, "\n");
        return (
          <section className="diff-hunk" key={start}>
            <div>
              <span>差异块 {i + 1}</span>
              {onStage && <button
                disabled={staged || truncated}
                onClick={() => onStage(header + hunk)}
              >
                暂存此块
              </button>}
            </div>
            <pre>{hunk}</pre>
          </section>
        );
      })}
    </>
  );
}
