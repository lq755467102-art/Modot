import {useQuery} from "@tanstack/react-query";
import {api, queryString} from "./client";
import type {Artifact, ArtifactRead, ArtifactVersion} from "./generated";

function artifactPath(conversationId:string) {
  return "/api/v2/conversations/threads/" + queryString(conversationId) + "/artifacts";
}

export function useArtifactList(conversationId:string | undefined, enabled:boolean) {
  return useQuery<{artifacts:Artifact[]}>({
    queryKey:["artifacts", conversationId],
    queryFn:() => api(artifactPath(conversationId ?? "")),
    enabled,
    retry:false,
    refetchInterval:enabled ? 2000 : false,
    refetchIntervalInBackground:true,
  });
}

export function imageArtifact(version:ArtifactVersion | undefined) {
  return version?.mime === "image/png" || version?.mime === "image/jpeg";
}

export function useArtifactImage(conversationId:string, artifactId:string, version:ArtifactVersion | undefined) {
  const enabled = imageArtifact(version);
  const read = useQuery<ArtifactRead>({
    queryKey:["artifact-image", conversationId, artifactId, version?.id, version?.hash],
    queryFn:() => api(artifactPath(conversationId) + "/" + queryString(artifactId) +
      "/versions/" + queryString(version!.id)),
    enabled,
    retry:false,
    gcTime:0,
    staleTime:Infinity,
  });
  const data = !read.error ? read.data : undefined;
  const src = enabled && data?.id === artifactId && data.version.id === version?.id &&
    data.version.hash === version?.hash && data.dataUrl?.startsWith("data:" + version.mime + ";base64,")
    ? data.dataUrl : undefined;
  return {src, pending:enabled && read.isPending, error:read.error, retry:read.refetch};
}
