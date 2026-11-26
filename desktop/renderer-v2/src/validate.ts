// 在前端读取响应前核对共享接口的关键字段和兼容约定。
// 主要引用方包括 `client.ts`。
import { WORKBENCH_SCHEMAS } from "./generated";
const schemas: Record<string, any> = WORKBENCH_SCHEMAS;
// 读取 `generated.ts` 的同名协议形状，再交给内部递归验证器。
export function valid(name: string, value: unknown): boolean {
  return test(schemas[name], value);
}
// 按生成契约检查必填字段、复合容器和允许的类型；对象新增字段按契约对附加字段的限制处理。
function test(spec: any, value: any): boolean {
  if (!spec) return false;
  if (Object.keys(spec).length === 0) return true;
  if (spec.anyOf)
    return spec.anyOf.some((child: unknown) => test(child, value));
  if (spec.$ref) return test(schemas[spec.$ref.split("/").pop()], value);
  if (spec.enum) return spec.enum.includes(value);
  const types = Array.isArray(spec.type) ? spec.type : [spec.type];
  return types.some((type: string) => {
    if (type === "null") return value === null;
    if (type === "array")
      return (
        Array.isArray(value) && value.every((item) => test(spec.items, item))
      );
    if (type === "object")
      return (
        value !== null &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        (typeof spec.additionalProperties !== "object" ||
          Object.entries(value).every(
            ([key, child]) =>
              key in (spec.properties || {}) ||
              test(spec.additionalProperties, child),
          )) &&
        (spec.required || []).every((key: string) => key in value) &&
        Object.entries(spec.properties || {}).every(
          ([key, child]) => !(key in value) || test(child, value[key]),
        )
      );
    return typeof value === type;
  });
}
// 由 `client.ts` 在使用后端响应前调用。
// 按方法和路径选择契约，同一接口的部分查询参数也决定返回形状。
export function validateReply(
  path: string,
  method: string,
  body: any,
): boolean {
  const route = path.split("?")[0];
  if (/^\/api\/v2\/projects\/imports\/identities\/[^/]+$/.test(route)) {
    if (!body || typeof body !== "object") return false;
    if (method === "GET") return body.issue === null ||
      ["actionId", "runId", "code", "url", "name"].every(k => typeof body.issue?.[k] === "string");
    const publicMetadata = (item: any) => typeof item?.id === "string" && typeof item.label === "string"
      && typeof item.fingerprint === "string" && item.fingerprint.startsWith("SHA256:");
    if (Array.isArray(body.identities)) return body.identities.every(publicMetadata);
    return publicMetadata(body.identity) && typeof body.identity.publicKey === "string"
      && /^(ssh-|ecdsa-|sk-)[^\s]+ [A-Za-z0-9+/=]+$/.test(body.identity.publicKey);
  }
  if(route==='/api/v2/workroom')return valid('WorkroomContext',body);
  if(route==='/api/v2/workroom/open')return valid('WorkroomOpened',body);
  if(route==='/api/v2/workroom/linked-file')return valid('LinkedWorkroomFile',body);
  if(route==='/api/v2/workroom/linked-file/content')return valid('FileContent',body);
  if(route==='/api/v2/plugins/memory')return valid('MemoryProviderSettings',body);
  if(route==='/api/v2/memory/inventory')return valid('MemoryInventory',body);
  if(route==='/api/v2/memory'||route==='/api/v2/memory/preview'||(/^\/api\/v2\/conversations\/threads\/[^/]+\/memory$/.test(route)&&method==='GET'))return valid('MemoryView',body);
  if (route === "/api/v2/migration")
    return valid(method === "GET" ? "ManagementView" : "MigrationPlan", body);
  if (/^\/api\/v2\/migration\/plans\//.test(route))
    return valid("MigrationPlan", body);
  if (/^\/api\/v2\/migration\/history\//.test(route))
    return valid("LegacyEventPage", body);
  if (route === "/api/v2/human-actions") return valid("HumanActionPage", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/(?:changes|change-ranges)\/[^/]+\/files\/\d+\/search$/.test(route))
    return valid('SourceSearchPage',body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/(?:changes|change-ranges)\/[^/]+\/files\/\d+$/.test(route))
    return valid('SourceFilePage',body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/changes$/.test(route))
    return valid('SourceChangePage',body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/changes\/[^/]+$/.test(route))
    return valid('SourceChange',body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/change-ranges(?:\/[^/]+)?$/.test(route))
    return valid(new URLSearchParams(path.split("?")[1]).get("scope")==="run-summary"?"RunChangeSummary":"SourceRange",body);

  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/integrations$/.test(route))
    return valid("IntegrationPage", body);
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/integrations\/[^/]+\/files\//.test(
      route,
    )
  )
    return valid("IntegrationFilePage", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/integrations\//.test(route))
    return valid("IntegrationPlan", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/subtasks$/.test(route))
    return valid("SubtaskPage", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/subtasks\/overview$/.test(route))
    return valid("SubtaskOverview", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/subtasks\//.test(route))
    return valid("Subtask", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/discussions$/.test(route))
    return valid(
      method === "GET" ? "DiscussionBackground" : "Conversation",
      body,
    );
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/delegations$/.test(route))
    return valid(method === "GET" ? "DelegationCurrent" : "Delegation", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/delegations\//.test(route))
    return valid("Delegation", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/context$/.test(route))
    return valid("ContextView", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/execution$/.test(route))
    return valid("ExecutionView", body);
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/restorations$/.test(route) &&
    method === "GET"
  )
    return valid("RestorationPage", body);
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/restorations(?:\/[^/]+(?:\/(?:approve|revoke))?)?$/.test(
      route,
    )
  )
    return valid("RestorationPlan", body);
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/restorations\/[^/]+\/baseline$/.test(
      route,
    )
  )
    return valid("RestorationBaseline", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/delivery$/.test(route))
    return valid(
      method === "GET" ? "DeliveryOverview" : "DeliveryUpdated",
      body,
    );
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/corrections$/.test(route))
    return valid(method === "GET" ? "CorrectionPage" : "Correction", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/progress$/.test(route))
    return valid("StageOverview", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/evidence$/.test(route))
    return valid("EvidencePage", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/evidence\/targets\/resolve$/.test(route))
    return valid("EvidenceTargetResult", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/evidence\/[^/]+$/.test(route))
    return valid("EvidenceAction", body);
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/evidence\/[^/]+\/output$/.test(
      route,
    )
  )
    return valid("EvidenceOutput", body);

  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/agent-debuggers$/.test(route))
    return (
      Array.isArray(body.debuggers) &&
      body.debuggers.every((v: unknown) => valid("AgentDebugger", v)) &&
      typeof body.hasMore === "boolean"
    );
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/agent-debuggers\/[^/]+$/.test(
      route,
    )
  )
    return valid("AgentDebugger", body);

  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/previews$/.test(route))
    return (
      Array.isArray(body.previews) &&
      body.previews.every((v: unknown) => valid("ApplicationPreview", v)) &&
      typeof body.hasMore === "boolean"
    );
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/previews\/[^/]+(?:\/stop)?$/.test(
      route,
    )
  )
    return valid("ApplicationPreview", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/code-operations$/.test(route))
    return (
      Array.isArray(body.operations) &&
      body.operations.every((op: unknown) => valid("CodeOperation", op)) &&
      typeof body.hasMore === "boolean"
    );
  if (
    /^\/api\/v2\/conversations\/threads\/[^/]+\/code-operations\/[^/]+\/output$/.test(
      route,
    )
  )
    return (
      valid("CodeResult", body) &&
      valid("ProcessRun", body.process) &&
      typeof body.text === "string" &&
      typeof body.hasMore === "boolean"
    );
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/artifacts$/.test(route))
    return method === "POST"
      ? valid("ArtifactPublished", body)
      : Array.isArray(body.artifacts) &&
          body.artifacts.every((a: unknown) => valid("Artifact", a));
  if (/\/artifacts\/[^/]+\/versions\/[^/]+$/.test(route))
    return valid("ArtifactRead", body);
  if (/\/artifacts\/[^/]+\/compare$/.test(route))
    return (
      valid("ArtifactRead", body.left) &&
      valid("ArtifactRead", body.right) &&
      typeof body.diff === "string" &&
      typeof body.truncated === "boolean"
    );
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/attachments$/.test(route))
    return method === "GET"
      ? Array.isArray(body.attachments) &&
          body.attachments.every((r: unknown) => valid("Attachment", r))
      : valid("Attachment", body) ||
          body.ok === true ||
          (body.source
            ? valid("DocumentPreview", body)
            : typeof body.text === "string") ||
          (Array.isArray(body.locations) &&
            body.locations.every((p: unknown) => typeof p === "string") &&
            typeof body.totalLocations === "number") ||
          (typeof body.dataUrl === "string" &&
            /^data:image\/(png|jpeg);base64,/.test(body.dataUrl));
  if (/^\/api\/v2\/projects\/imports(?:\/[a-f0-9]{32}(?:\/cancel)?)?$/.test(route))
    return valid("RepositoryImport", body);
  if (route === "/api/v2/projects")
    return method === "POST" ? valid("Project", body) : valid("ProjectDirectory", body);
  if (/^\/api\/v2\/projects\/[^/]+\/visibility$/.test(route))
    return valid("Project", body);
  if (
    /^\/api\/v2\/projects\/[^/]+\/resources(?:\/[^/]+\/permission)?$/.test(
      route,
    )
  )
    return valid("ProjectResource", body);
  if (/^\/api\/v2\/conversations\/threads\/[^/]+\/scope$/.test(route))
    return valid("ConversationScope", body);
  if (route === "/api/v2/conversations/threads")
    return method === "POST"
      ? valid("Conversation", body)
      : Array.isArray(body.threads) &&
          body.threads.every((v: unknown) => valid("Conversation", v));
  if (/\/api\/v2\/conversations\/threads\/[^/]+\/events$/.test(route))
    return (
      Array.isArray(body.events) &&
      body.events.every((v: unknown) => valid("EventRow", v)) &&
      typeof body.total === "number"
    );
  if (route === "/api/models" && method === "GET")
    return Array.isArray(body.profiles);
  if (/\/api\/v2\/workspaces\/[^/]+\/threads\/[^/]+\/events$/.test(route))
    return (
      Array.isArray(body.events) &&
      body.events.every((row: unknown) => valid("EventRow", row)) &&
      typeof body.total === "number"
    );
  if (/\/api\/v2\/processes\/[^/]+$/.test(route) && method === "GET")
    return (
      valid("ProcessRun", body.process) &&
      typeof body.text === "string" &&
      typeof body.next === "number"
    );
  if (/\/api\/v2\/workspaces\/[^/]+\/checks$/.test(route) && method === "GET")
    return (
      Array.isArray(body.processes) &&
      body.processes.every((row: unknown) => valid("ProcessRun", row))
    );
  if (/\/api\/v2\/workspaces\/[^/]+$/.test(route))
    return valid("Workspace", body);
  if (route === "/api/v2/workspaces" && method === "POST")
    return valid("Workspace", body);
  if (/\/api\/v2\/workspaces\/[^/]+\/file$/.test(route)) {
    const view = method === "GET" && new URLSearchParams(path.split("?")[1]).get("view");
    if (view === "bytes") return valid("WorkspaceBytePage", body);
    if (view === "preview" && body.kind === "image") return valid("WorkspaceImagePreview", body);
    if (view === "preview" && body.kind === "bytes") return valid("WorkspaceBytePage", body);
    return valid(view === "page" || view === "preview" ? "WorkspaceTextPage" : "FileContent", body);
  }
  if (/\/api\/v2\/workspaces\/[^/]+\/git$/.test(route)) {
    const view = method === "GET" && new URLSearchParams(path.split("?")[1]).get("view");
    return valid(view === "head" ? "GitHead" : view === "changes" ? "GitChangesPage" : "GitState", body);
  }
  if (
    /\/api\/v2\/workspaces\/[^/]+\/(checks|terminals)$/.test(route) &&
    method === "POST"
  )
    return valid("ProcessRun", body);
  if (/\/api\/v2\/workspaces\/[^/]+\/threads$/.test(route))
    return method === "POST"
      ? valid("Thread", body)
      : Array.isArray(body.threads) &&
          body.threads.every((v: unknown) => valid("Thread", v));
  if (route === "/api/v2/bootstrap") {
    const summary = new URLSearchParams(path.split("?")[1]).get("includeGit") === "0";
    return (
      body.version === 2 &&
      Array.isArray(body.workspaces) &&
      body.workspaces.every((v: unknown) => valid(summary ? "WorkspaceSummary" : "Workspace", v))
    );
  }
  if(route==='/api/v2/plugins/presets/vision')return valid('VisionCorePreset',body);
  if(route==='/api/v2/plugins'&&method==='GET')return valid('PluginPage',body);
  if(route==='/api/v2/plugins/contributions'&&method==='GET')return valid('InstalledContributionPage',body);
  if(/^\/api\/v2\/plugins\/contributions\/[^/]+\/pages\/[^/]+\/draft$/.test(route)&&method==='POST')
    return valid('PluginDataPageDraft',body);
  if(/^\/api\/v2\/plugins\/contributions\/[^/]+\/pages\/[^/]+$/.test(route))
    return valid(method==='GET'?'PluginDataPageView':'PluginDataPageState',body);
  if(/^\/api\/v2\/plugins\/contributions\/[^/]+\/icon$/.test(route)&&method==='GET')
    return valid('PluginContributionIcon',body);
  if(method==='GET'&&/^\/api\/v2\/plugins\/[^/]+\/connections\/[^/]+\/github-account$/.test(route))return valid('GitHubAccount',body);
  if(method==='GET'&&/^\/api\/v2\/plugins\/[^/]+\/connections\/[^/]+\/github-repositories$/.test(route))return valid('GitHubRepositoryPage',body);
  if(/^\/api\/v2\/plugins\/[^/]+\/replace$/.test(route)&&method==='POST')return valid('PluginReplacement',body);
  if(/^\/api\/v2\/plugins\/[^/]+\/tools$/.test(route))return valid('PluginToolsPage',body);
  if(method==='POST'&&(route==='/api/v2/plugins/local-skills'||/^\/api\/v2\/plugins\/[^/]+\/skill-draft$/.test(route)))return typeof body.title==='string'&&typeof body.text==='string'&&typeof body.version==='string';
  if(/^\/api\/v2\/plugins\/[^/]+$/.test(route)&&method==='POST')return valid('PluginPackage',body);
  return true;
}
