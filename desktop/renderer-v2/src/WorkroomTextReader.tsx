// 按页读取大文本和二进制字节，并显示工作间的图像预览。
// 主要引用方包括 `WorkroomPage.tsx`。
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useUiLanguage } from "./UiLanguage";
import { api, queryString, wsPath } from "./client";
import type { WorkspaceBytePage, WorkspaceImagePreview, WorkspaceTextPage } from "./generated";
import "./conversation-record.css";

type FilePage = WorkspaceTextPage | WorkspaceBytePage | WorkspaceImagePreview;
type Position = { offset: number; view: "preview" | "bytes" };
const first: Position = { offset: 0, view: "preview" };

function hexRows(page: WorkspaceBytePage) {
  const rows: string[] = [];
  for (let i = 0; i < page.hex.length; i += 32) {
    const bytes = page.hex.slice(i, i + 32).match(/../g)!;
    const ascii = bytes.map(pair => {
      const byte = parseInt(pair, 16);
      return byte >= 32 && byte <= 126 ? String.fromCharCode(byte) : ".";
    });
    const address = (page.offset + i / 2).toString(16).padStart(8, "0");
    rows.push(address + "  " + bytes.join(" ").padEnd(47) + "  " + ascii.join(""));
  }
  return rows.join("\n");
}

export function WorkroomFileReader({ workspace, path, visible }: {
  workspace: string; path: string; visible: boolean;
}) {
  const { t, locale } = useUiLanguage();
  const [cursor, setCursor] = useState({ positions: [first], revision: "", fromImage: false });
  const { offset, view } = cursor.positions.at(-1)!;
  // 第一页返回文件版本，后续页和图片切字节都用它校验同一份内容。
  const page = useQuery({
    queryKey: ["workroom-file-page", workspace, path, view, cursor.revision, offset],
    queryFn: () => api<FilePage>(wsPath(workspace, "file") +
      "?view=" + view + "&path=" + queryString(path) + "&after=" + offset +
      (cursor.revision ? "&revision=" + queryString(cursor.revision) : "")),
    enabled: visible,
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
  const body = useRef<HTMLPreElement>(null);
  useEffect(() => { body.current?.scrollTo(0, 0); }, [page.data]);
  const data = page.error ? undefined : page.data;
  const image = data && "kind" in data && data.kind === "image" ? data : undefined;
  const bytes = data && "kind" in data && data.kind === "bytes" ? data : undefined;
  const paged = data && "nextOffset" in data ? data : undefined;
  function restart() {
    if (cursor.revision || offset || view !== "preview") {
      setCursor({ positions: [first], revision: "", fromImage: false });
    } else void page.refetch();
  }
  return <section className="workroom-record workroom-file-page" aria-label={t("文件分页阅读")}>
    {page.isPending && <p role="status">{t("正在读取文件…")}</p>}
    {page.error && <div className="workroom-record-error">
      <span role="alert">{page.error.message}</span>
      <button type="button" onClick={restart}>{t("从头读取")}</button>
    </div>}
    {data && "text" in data && <pre ref={body} className="workroom-record-body" aria-label={t("文件正文")} tabIndex={0}>{data.text}</pre>}
    {image && <>
      <div className="workroom-image-preview"><img alt={path} src={"data:image/png;base64," + image.png}/></div>
      <footer>
        <span>{image.format} · {image.width} × {image.height} · {t("{size} 字节", {size:image.sizeBytes.toLocaleString(locale)})}{image.firstFrame && " · " + t("首帧")}</span>
        <button type="button" onClick={() => setCursor({ positions: [{offset:0, view:"bytes"}], revision:image.revision, fromImage:true })}>{t("查看字节")}</button>
      </footer>
    </>}
    {bytes && <>
      {bytes.reason && <p role="status">{t(bytes.reason === "image_limit" ? "图片超出预览大小，已显示文件字节" :
        bytes.reason === "invalid_image" ? "图片无法解码，已显示文件字节" : "图片解码器不可用，已显示文件字节")}</p>}
      <pre ref={body} className="workroom-record-body workroom-binary-bytes" aria-label={t("文件字节")} tabIndex={0}>{hexRows(bytes)}</pre>
    </>}
    {paged && <footer>
      <button type="button" data-file-previous disabled={cursor.positions.length === 1 || page.isFetching}
        onClick={() => setCursor({ ...cursor, positions:cursor.positions.slice(0, -1) })}>{t("上一页")}</button>
      <span role="status">{t("第 {page} 页", {page:cursor.positions.length})}
        {bytes && " · " + t("{size} 字节", {size:bytes.sizeBytes.toLocaleString(locale)})}</span>
      <button type="button" data-file-next disabled={!paged.hasMore || page.isFetching}
        onClick={() => setCursor({ ...cursor, positions:[...cursor.positions, {offset:paged.nextOffset, view:bytes ? "bytes" : "preview"}], revision:paged.revision })}>{t("下一页")}</button>
      {cursor.fromImage && <button type="button" onClick={() => setCursor({ positions:[first], revision:paged.revision, fromImage:false })}>{t("返回图片")}</button>}
    </footer>}
  </section>;
}

