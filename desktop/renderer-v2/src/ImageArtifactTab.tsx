import {imageArtifact, useArtifactImage, useArtifactList} from "./artifactImages";
import {ImageViewport} from "./ImageViewport";
import type {ImageArtifactPage} from "./ResourceLinks";
import {useUiLanguage} from "./UiLanguage";

export default function ImageArtifactTab({page}:{page:ImageArtifactPage}) {
  const {t} = useUiLanguage();
  const authority = useArtifactList(page.owner, true);
  // Reopening a tab checks authorization before showing a cached image.
  const item = authority.isFetchedAfterMount && !authority.error
    ? authority.data?.artifacts.find(artifact => artifact.id === page.artifactId)
    : undefined;
  const candidate = item?.versions.find(version => version.id === page.versionId);
  const version = item?.permission === "read" && candidate?.state === "ready" && imageArtifact(candidate)
    ? candidate : undefined;
  const preview = useArtifactImage(page.owner, page.artifactId, version);

  return <section className="side-content image-artifact-tab" aria-label={t("图片详情")}>
    <header>
      <h2>{page.label}</h2>
      {version?.title && version.title !== page.label && <small>{version.title}</small>}
    </header>
    {authority.error ? <p role="alert">{t("成果不可用")}{" "}
      <button type="button" onClick={() => void authority.refetch()}>{t("重试")}</button></p>
      : !authority.isFetchedAfterMount || preview.pending ? <p role="status">{t("正在读取图片…")}</p>
      : !version ? <p role="status">{t("成果不可用")}</p>
      : !preview.src ? <p role="alert">{t("图片预览不可用")}{" "}
        {preview.error && <button type="button" onClick={() => void preview.retry()}>{t("重试")}</button>}</p>
      : <ImageViewport key={version.hash} src={preview.src} name={page.label}/>}
  </section>;
}
