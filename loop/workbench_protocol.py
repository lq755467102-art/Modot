"""Engineering entity contract, used to generate JSON Schema and renderer types."""
# 主要引用方包括 `generate_workbench_protocol.py`。

SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "WorkbenchV2",
    "$defs": {},
}


def obj(type_name, required, /, **properties):
    SCHEMA["$defs"][type_name] = {
        "type": "object",
        "required": required.split(),
        "properties": properties,
    }


def ref(name):
    return {"$ref": "#/$defs/" + name}


def array(spec):
    return {"type": "array", "items": spec}


S = {"type": "string"}
B = {"type": "boolean"}
N = {"type": "number"}
NULL_S = {"type": ["string", "null"]}
obj('WorkroomProject','id name',id=S,name=S)
obj('WorkroomRoot','resourceId originResourceId name path checkout',resourceId=S,originResourceId=S,name=S,path=S,checkout={'enum':['local','worktree']})
obj('WorkroomLocation','resourceId directory', resourceId=S, directory=S)
obj('WorkroomContext','project conversationId scopeRevision roots status notice',project={'anyOf':[ref('WorkroomProject'),{'type':'null'}]},conversationId=NULL_S,scopeRevision=S,roots=array(ref('WorkroomRoot')),importLocation=ref('WorkroomLocation'),status={'enum':['ready','no_project','no_code','readonly']},notice=S)
obj('WorkroomOpened','workspaceId projectId resourceId path checkout scopeRevision',workspaceId=S,projectId=NULL_S,resourceId=S,path=S,checkout={'enum':['local','worktree']},scopeRevision=S)
obj('LinkedWorkroomFile', 'conversationId resourceId path scopeRevision revision mode mimeType absolutePath defaultOpen',
    conversationId=S, resourceId=S, path=S, scopeRevision=S, revision=S,
    mode={'enum': ['edit', 'readonly', 'binary']}, mimeType=S, absolutePath=S, defaultOpen=B)
obj('LegacyHistory','id workspaceId cwd hashes reason unsupported',id=S,title=S,workspaceId=NULL_S,cwd=NULL_S,hashes={'type':'object','additionalProperties':S},reason=S,unsupported=B,legacyTrace=B,runtime=S,readonly=B,migratedBy=S)
obj('MigrationPlan','id state created candidates planHash notice',id=S,state=S,created=N,candidates=array(ref('LegacyHistory')),planHash=S,notice=S,backup=S,hasMore=B)
obj('ManagementEntry','id name state entry legacyWrites paths',id=S,name=S,state=S,entry=S,legacyWrites=B,paths=array(S))
obj('ManagementView','format inventory history plans notice',format={'type':'object'},inventory=array(ref('ManagementEntry')),history=array(ref('LegacyHistory')),plans=array({'type':'object'}),notice=S,totalHistory=N)
obj('LegacyEventPage','item events total generation catchingUp',item=ref('LegacyHistory'),events=array(ref('EventRow')),total=N,generation=S,catchingUp=B,historyNotice=S,hasMore=B,beforeOffset=N)
obj('HumanAction','id actor source principal path state started requestVersion notice',id=S,actor=S,source=S,principal=S,path=S,state=S,started=N,requestVersion=S,notice=S,position=N,workspaceId=NULL_S,conversationId=NULL_S,receiptId=NULL_S,expectedRevision=S,effect={'type':'object'},actualProcess={'type':'object'},httpStatus=N,finished=N)
obj('HumanActionPage','items hasMore nextBefore notice',items=array(ref('HumanAction')),hasMore=B,nextBefore={'type':['number','null']},notice=S)
obj('ContextLayer','scope label values',scope=NULL_S,label=S,values={'type':'object','additionalProperties':B})
obj('ContextModel','name endpoint source',name=S,endpoint=S,source=S)
obj('ContextSource','name status version',name=S,status=S,version=S,kind=S,origin=S,note=S,chars=N,callId=S,actionId=NULL_S,actionRunId=S,spillHash=S)
obj('ContextChoice','id runId role at model',id=S,runId=S,role=S,at=N,model=ref('ContextModel'))
obj('ContextPreferences','effective sources layers revision notice',effective={'type':'object','additionalProperties':B},sources={'type':'object','additionalProperties':S},layers=array(ref('ContextLayer')),revision=S,notice=S)
obj('InputBudget','fixed current history sources method estimated userLimit modelWindow reservedOutput inputLimit historyThreshold deferred requestedGroups',
    fixed=N,current=N,history=N,sources={'type':'object','additionalProperties':N},method=S,estimated=B,
    userLimit={'type':['number','null']},modelWindow={'type':['number','null']},reservedOutput=N,
    inputLimit={'type':['number','null']},historyThreshold=N,deferred=array(S),requestedGroups=array(S),
    reason=S,stage=S,providerInput={'type':['number','null']},decisionTokens=N,decisionMethod=S,
    submissionImages=N,submissionImageTokens=N,submissionTokens=N)
obj('ContextCapacity','estimated method inputTokens toolTokens protocolTokens reservedOutput outputReservationKnown safetyReserve estimatedTotal capacity state',estimated=B,method=S,inputTokens=N,toolTokens=N,protocolTokens=N,reservedOutput=N,outputReservationKnown=B,safetyReserve=N,estimatedTotal=N,capacity={'type':['number','null']},state=S,imageTokens=N,imageCount=N,systemTokens=N,breakdown={'type':'object','additionalProperties':N},budget=ref('InputBudget'))
obj('ContextCheckpoint','id runId role at strategy before after actionReferences preservedUserMessages',id=S,runId=S,role=S,at=N,strategy=S,before=ref('ContextCapacity'),after=ref('ContextCapacity'),actionReferences=N,preservedUserMessages=N)
obj('ContextDelivery','status',status={'enum':['confirmed','unconfirmed']},evidence={'enum':['provider_response']})
obj('ContextEvidenceSource','runId role',runId=S,role=S,checkpointId=S,messageIndex=N,contentField=NULL_S,snapshotId=S,actionId=S)
obj('ContextEvidencePage','kind source text nextOffset hasMore version',kind={'enum':['checkpoint_message','tool_result']},source=ref('ContextEvidenceSource'),text=S,nextOffset=N,hasMore=B,version=S)
obj('ContextSnapshot','id runId role at state model requestVersion systemVersion messageCount sources results preferences',id=S,runId=S,role=S,at=N,state=S,model=ref('ContextModel'),requestVersion=S,systemVersion=S,messageCount=N,sources=array(ref('ContextSource')),results=array(ref('ContextSource')),preferences=ref('ContextPreferences'),notice=S,resourceScope={'type':'object'},toolNames=array(S),omittedResults=N,capacity=ref('ContextCapacity'),checkpointId=NULL_S,delivery=ref('ContextDelivery'))
obj('ContextView','snapshot snapshots preferences authority managementNotice offset hasMore totalSnapshots',snapshot={'anyOf':[ref('ContextSnapshot'),{'type':'null'}]},snapshots=array(ref('ContextChoice')),preferences=ref('ContextPreferences'),authority={'type':'object'},managementNotice=S,offset=N,hasMore=B,totalSnapshots=N,preferenceChanges=array({"type":"object"}),checkpoints=array(ref('ContextCheckpoint')),checkpointOffset=N,checkpointsHasMore=B,totalCheckpoints=N)
obj('ExecutionConfig','revision crew maxTurns modelCalls charter', revision=N,crew={'enum':['l0','l4']},maxTurns=N,modelCalls=N,charter={'enum':['legacy','off']},contextLimit=N,parallelRoles={'type':'boolean'},backgroundPlanner={'type':'boolean'},workerContinuation={'enum':['parent-run','manual','wake']},purpose={'enum':['execute','delegation_draft']},delegationId=NULL_S,subtaskId=NULL_S)
obj('SubtaskOrigin','parentId taskId runId',parentId=S,taskId=S,runId=S)
obj('SubtaskBudget','sourceRunId limit',sourceRunId=S,limit=N)
obj('Subtask','id parentId parentRunId childId goal kind state reason budget',id=S,parentId=S,parentRunId=S,childId=S,goal=S,kind={'enum':['code','report']},state=S,reason=S,budget=ref('SubtaskBudget'),resources=array(ref('ProjectResource')))
obj('SubtaskPage','tasks concurrency maximumTasks budgetNotice',tasks=array(ref('Subtask')),concurrency=N,maximumTasks=N,budgetNotice=S)
obj('SubtaskOverviewItem','id nickname runId state title summary created',id=S,nickname=S,runId=S,state=S,title=S,summary=S,created=N,resultDelivery={'enum':['available','notified','delivered']})
obj('SubtaskOverview','tasks',tasks=array(ref('SubtaskOverviewItem')))
obj('IntegrationFile','path baseHash currentHash sourceHash mergedHash conflict choice kind',path=S,baseHash=NULL_S,currentHash=NULL_S,sourceHash=NULL_S,mergedHash=NULL_S,conflict=B,choice={'type':['object','null']},kind=S)
obj('IntegrationCheck','status',status=S,operationId=S,notice=S)
obj('IntegrationPlan','id parentId taskId resourceId sourceRevision targetRevision sourceBaseline sourceHead sourceBranch targetHead targetBranch files commits state planHash revision applied conflicts created',id=S,parentId=S,taskId=S,resourceId=S,sourceRevision=S,targetRevision=S,sourceBaseline=S,sourceHead=S,sourceBranch=S,targetHead=S,targetBranch=S,files=array(ref('IntegrationFile')),commits=array(S),state=S,planHash=S,revision=N,applied=array(S),alreadyPresent=array(S),conflicts=N,created=N,notice=S,targetStale=B,resultRevision=S,currentTargetRevision=S,targetCheck=ref('IntegrationCheck'),childId=S,sourceChecks=array({'type':'object'}),sourceChecksNotice=S,sourceChecksMore=B,cleanup={'type':['object','null']},abortRequested=B)
obj('IntegrationSummary','id taskId state planHash fileCount appliedCount',id=S,taskId=S,resourceId=S,state=S,planHash=S,conflicts=N,created=N,targetStale=B,position=N,fileCount=N,appliedCount=N)
obj('IntegrationPage','plans hasMore nextBefore',plans=array(ref('IntegrationSummary')),hasMore=B,nextBefore={'type':['number','null']})
obj('IntegrationFilePage','planId planHash path view text next totalChars notice',planId=S,planHash=S,path=S,view=S,text=S,next={'type':['number','null']},totalChars=N,notice=S)
obj('ExecutionRun','state',state=S,runId=S,config=ref('ExecutionConfig'),usedCalls=N,byRole={'type':'object','additionalProperties':N},reviewerReserve=N,exhaustedRole=S,reason=S,charterEffective=S)
obj('ExecutionRoute','id name planner reviewer mechanism',id=S,name=S,planner={'type':'boolean'},reviewer={'type':'boolean'},mechanism=S)
obj('ExecutionView','next current routes permissions budgetNotice migration',next=ref('ExecutionConfig'),current={'anyOf':[ref('ExecutionRun'),{'type':'null'}]},routes=array(ref('ExecutionRoute')),permissions=array({'type':'object'}),nextReviewerReserve=N,budgetNotice=S,migration=S)
obj('DelegationCriterion','id text kind',id=S,text=S,kind={'enum':['manual','code_check','artifact_contains']},resourceId=S,argv=array(S),contains=S)
obj('DelegationSpec','goal boundaries criteria maxRounds modelCalls',goal=S,boundaries=S,criteria=array(ref('DelegationCriterion')),maxRounds=N,modelCalls=N)
obj('DelegationVersion','id spec actor at',id=S,spec=ref('DelegationSpec'),actor=S,at=N)
obj('DelegationBudget','chargedCalls remainingCalls remainingRounds unconfirmed',chargedCalls=N,remainingCalls=N,remainingRounds=N,unconfirmed=B)
obj('DelegationRound','runId state allocatedCalls version',runId=S,state=S,allocatedCalls=N,version=S,observedCalls=N,chargedCalls=N)
obj('DelegationEvaluationItem','criterion claim result',criterion=ref('DelegationCriterion'),claim={'type':['object','null']},result=S,reason=S,weakCheck=B,target={'type':'object'})
obj('DelegationEvaluation','criteria allChecksMet signature',criteria=array(ref('DelegationEvaluationItem')),allChecksMet=B,signature=NULL_S,notice=S)
obj('Delegation','id conversationId revision state versions pendingVersion activeVersion rounds progress decision',id=S,conversationId=S,revision=N,state=S,reason=S,versions=array(ref('DelegationVersion')),pendingVersion=NULL_S,activeVersion=NULL_S,rounds=array(ref('DelegationRound')),progress=array({'type':'object'}),decision={'type':['object','null']},decisionStale=B,budget=ref('DelegationBudget'),evaluation=ref('DelegationEvaluation'),mandate={'type':'object'},reconcileRunId=S)
obj('DelegationCurrent','current',current={'anyOf':[ref('Delegation'),{'type':'null'}]})
_git_head_fields = dict(available=B, reason=S, branch=S, head=NULL_S, protected=B)
obj("GitHead", "available", **_git_head_fields)
obj("GitFile", "path status", path=S, status=S, oldPath=NULL_S)
obj(
    "GitState",
    "available files revision",
    **_git_head_fields,
    revision=NULL_S,
    files=array(ref("GitFile")),
)
obj("GitChangesPage", "git changeCount nextOffset snapshot", git=ref("GitState"),
    changeCount=N, nextOffset={"type": ["number", "null"]}, snapshot=S)
obj(
    "Capabilities",
    "files checks pty debug git",
    files=B,
    checks=B,
    pty=B,
    debug=B,
    git=B,
)
_workspace_fields = dict(
    id=S,
    name=S,
    path=S,
    kind=S,
    parent=NULL_S,
    capabilities=ref("Capabilities"),
)
obj("WorkspaceSummary", "id name path kind capabilities", **_workspace_fields)
obj("Workspace", "id name path kind git capabilities", **_workspace_fields, git=ref("GitState"))
obj("Runtime", "state active", state=S, active=B, policy=NULL_S, run_id=S)
obj(
    "Thread",
    "id taskId workspaceId title kind",
    id=S,
    taskId=S,
    workspaceId=S,
    title=S,
    kind={"enum": ["implementation", "question", "discussion", "review"]},
    runtime=ref("Runtime"),
    acceptedAt=N,
    acceptedRevision=S,
    acceptanceNote=S,
    archived=B,
)
obj(
    "FileContent",
    "path text revision bom eol",
    path=S,
    text=S,
    revision=S,
    bom=B,
    eol={"enum": ["CRLF", "LF"]},
)
obj(
    "WorkspaceTextPage",
    "path text revision nextOffset hasMore sizeBytes",
    path=S, text=S, revision=S, nextOffset=N, hasMore=B, sizeBytes=N,
)
obj("WorkspaceBytePage", "kind path revision sizeBytes offset nextOffset hasMore hex reason",
    kind={"enum": ["bytes"]}, path=S, revision=S, sizeBytes=N, offset=N, nextOffset=N, hasMore=B,
    hex=S, reason={"enum": ["", "image_limit", "invalid_image", "decoder_unavailable"]})
obj("WorkspaceImagePreview", "kind path revision sizeBytes format width height firstFrame png",
    kind={"enum": ["image"]}, path=S, revision=S, sizeBytes=N, format=S, width=N, height=N,
    firstFrame=B, png=S)
obj(
    "EvidenceQuestionSource",
    "conversationId actionId snapshotHash",
    conversationId=S,
    actionId=S,
    snapshotHash=S,
    runId=NULL_S,
    outputHash=NULL_S,
)
obj('ForkSource','conversationId title before seq generation',conversationId=S,title=S,before=N,seq=N,generation=S,projectId=NULL_S)
obj('DiscussionSource','conversationId title hash snapshotHash contextMode',conversationId=S,title=S,hash=S,snapshotHash=S,contextMode=S,at=N)
obj('DiscussionBackground','text nextOffset source notice historical totalChars',text=S,nextOffset={'type':['number','null']},source=ref('DiscussionSource'),notice=S,historical=B,totalChars=N)
obj(
    "Conversation",
    "id title workspaceId projectId",
    id=S,
    title=S,
    workspaceId={"type": "null"},
    projectId=NULL_S,
    scheduledPlanId=S,
    archived=B,
    deletionPending=B,
    pinned=B,
    organizationRevision=N,
    projectName=NULL_S,
    created=N,
    runtime=ref("Runtime"),
    readonlyEvidence=B,
    questionOf=ref("EvidenceQuestionSource"),
    subtaskOf=ref('SubtaskOrigin'),
    forkOf=ref("ForkSource"),discussionOf=ref("DiscussionSource"),eventCursor={"type":["number","null"]},
)
obj('PluginSkill','name description path hash',name=S,description=S,path=S,hash=S,modelInvocable=B,userInvocable=B,unsupported=array(S))
obj('PluginServer','name transport supported reason displayURL',name=S,transport=S,supported=B,reason=S,displayURL=S,url=NULL_S,connected=B,githubAppConfigured=B,githubAccountConnected=B)
obj('GitHubInstallation','id owner manageUrl',id=N,owner=S,manageUrl=NULL_S)
obj('GitHubAccount','login installations installUrl nextPage revision',login=S,installations=array(ref('GitHubInstallation')),installUrl=NULL_S,nextPage={'type':['number','null']},revision=N)
obj('GitHubRepository','fullName private',fullName=S,private=B)
obj('GitHubRepositoryPage','installationId repositories nextPage revision',installationId=N,repositories=array(ref('GitHubRepository')),nextPage={'type':['number','null']},revision=N)
obj('PluginHook','event entry matcher timeout',event=S,entry=S,matcher=S,timeout=N)
obj('PluginContextStore','name description initial',name=S,description=S,initial={})
obj('PluginWorker','name protocol description method inputSchema outputSchema requires',name=S,protocol=S,description=S,method=S,inputSchema={'type':'object'},outputSchema={'type':'object'},requires=array(S),optional=array(S),modelRole={'enum':['vision']})
obj('TaskCoreModel','role model provider',role=S,model=S,provider=S)
obj('VisionCorePreset','id fingerprint revision enabled model modelState',id=S,fingerprint=S,revision={'type':['number','null']},enabled=B,model={'anyOf':[ref('TaskCoreModel'),{'type':'null'}]},modelState={'enum':['ready','worker_model_missing','worker_model_same']})
obj('PluginPackage','name version description format skills servers unsupported compatibility fingerprint bytes fileCount omitted notice',name=S,version=S,description=S,format=S,skills=array(ref('PluginSkill')),servers=array(ref('PluginServer')),hooks=array(ref('PluginHook')),contextStores=array(ref('PluginContextStore')),workers=array(ref('PluginWorker')),workersEnabled=B,memoryProvider={'type':['object','null']},memoryEnabled=B,unsupported=array(S),compatibility=S,fingerprint=S,bytes=N,fileCount=N,omitted=array(S),notice=S,id=S,revision=N,skillsEnabled=B,remoteEnabled=B,writeEnabled=B,hooksEnabled=B,contextEnabled=B,installedAt=N,removed=B)
obj('PluginPage','plugins hasMore next total',plugins=array(ref('PluginPackage')),hasMore=B,next=N,total=N)
obj('PluginInstallRevision','id fingerprint revision',id=S,fingerprint=S,revision=N)
obj('InstalledContribution','kind localId controlledBy independentlySwitchable supported enabled status',
    kind=S,localId=S,controlledBy=array(S),independentlySwitchable=B,supported=B,enabled=B,status=S,
    reason=S,authentication=S,availability=S,transport=S,writeEnabled=B,identityScope=S,event=S,matcher=S,entry=S,pageRevision=N,pageTitle=S)
obj('InstalledContributionPackage','id fingerprint revision name version format installed sourcePath packageKey contributions',
    id=S,fingerprint=S,revision=N,name=S,version=S,format=S,installed=B,sourcePath=NULL_S,packageKey=NULL_S,contributions=array(ref('InstalledContribution')),
    iconThemes=array({"enum":["default","light","dark"]}))
obj('PluginDataPageExample','title text',title=S,text=S)
obj('PluginDataPageDescriptor','version packageKey pageId title description examples actions',
    version={"enum":[1]},packageKey=S,pageId=S,title=S,description=S,examples=array(ref('PluginDataPageExample')),
    actions=array({"enum":["append-to-draft"]}))
obj('PluginDataPageView','id fingerprint revision pageId pageRevision page',
    id=S,fingerprint=S,revision=N,pageId=S,pageRevision=N,page=ref('PluginDataPageDescriptor'))
obj('PluginDataPageState','id fingerprint revision pageId pageRevision enabled',
    id=S,fingerprint=S,revision=N,pageId=S,pageRevision=N,enabled=B)
obj('PluginDataPageDraft','id fingerprint revision pageId pageRevision conversationId exampleIndex title text',
    id=S,fingerprint=S,revision=N,pageId=S,pageRevision=N,conversationId=S,exampleIndex=N,title=S,text=S)
obj('PluginContributionIcon','id fingerprint revision theme resolvedTheme mime width height data',
    id=S,fingerprint=S,revision=N,theme={"enum":["default","light","dark"]},
    resolvedTheme={"enum":["default","light","dark"]},mime={"enum":["image/png"]},width=N,height=N,data=S)
obj('InstalledContributionPage','plugins total snapshot hasMore nextCursor',plugins=array(ref('InstalledContributionPackage')),
    total=N,snapshot=S,hasMore=B,nextCursor=NULL_S)
obj('PluginReplacement','requestId previous installed',requestId=S,previous=ref('PluginInstallRevision'),installed=ref('PluginInstallRevision'))
obj('ApplicationSlot','available format category maxArchiveBytes',available=B,format={'enum':['vsix']},category=S,maxArchiveBytes=N)
obj('ApplicationChild','packageId parentId slot revision enabled canRun version sha256 extensionId name',
    packageId=S,parentId=S,slot=S,revision=N,enabled=B,canRun=B,version=S,sha256=S,extensionId=S,name=S)
obj('ApplicationSlotScope','projectId conversationId resourceId checkout scopeRevision',
    projectId=NULL_S,conversationId=NULL_S,resourceId=S,checkout={'enum':['local','worktree']},scopeRevision=S)
obj('ApplicationChildren','children slots',children=array(ref('ApplicationChild')),
    slots={'type':'object','additionalProperties':ref('ApplicationSlot')},scope=ref('ApplicationSlotScope'))
obj('PluginTool','spec schemaHash callable notice',spec={'type':'object'},schemaHash=S,callable=B,notice=S,requiresHuman=B)
obj('PluginToolsPage','ok pluginId fingerprint revision server protocol catalogHash tools next hasMore total notice',ok=B,pluginId=S,fingerprint=S,revision=N,server=S,protocol=S,catalogHash=S,tools=array(ref('PluginTool')),next=N,hasMore=B,total=N,notice=S)
obj(
    "ProjectResource",
    "id name path kind permission",
    id=S,
    name=S,
    path=S,
    kind={"enum": ["documents", "code", "attachment"]},
    permission={"enum": ["none", "read"]},
    worktreeOf=S,
    write=B,
    execute=B,
)
obj(
    "Project",
    "id name resources",
    id=S,
    name=S,
    resources={"type": "object", "additionalProperties": ref("ProjectResource")},
    folderResourceId=S,
    removed=B,
    visibilityRevision=N,
)
obj("ProjectDirectory", "projects", projects=array(ref("Project")), removedProjects=array(ref("Project")))
obj(
    "ResourceSnapshot",
    "id name kind permission status",
    id=S,
    name=S,
    kind=S,
    permission=S,
    write=B,
    execute=B,
    status=S,
)
obj(
    "ScopeSnapshot",
    "at projectId resources",
    at=N,
    projectId=NULL_S,
    resources=array(ref("ResourceSnapshot")),
    unavailable=array(ref("ResourceSnapshot")),
)
obj(
    "ConversationScope",
    "projectId resources loaded revision",
    projectId=NULL_S,
    folderResourceId=S,
    resources=array(ref("ProjectResource")),
    historicCode=array(ref("ProjectResource")),
    loaded=B,
    revision=S,
    lastRun={"anyOf": [ref("ScopeSnapshot"), {"type": "null"}]},
)
obj(
    "ProcessRun",
    "id workspaceId kind argv state exitCode revision",
    id=S,
    workspaceId=S,
    kind=S,
    argv=array(S),
    state=S,
    exitCode={"type": ["number", "null"]},
    revision=S,
)
obj(
    "Attachment",
    "id name mime bytes hash status permission",
    id=S,
    name=S,
    mime=S,
    bytes=N,
    hash=S,
    status=S,
    permission=S,
    preview=S,
    width=N,
    height=N,
    lastReadAt=N,
    document=ref("DocumentParse"),
)
obj(
    "MessageAttachment",
    "id name mime bytes",
    id=S,
    name=S,
    mime=S,
    bytes=N,
    width=N,
    height=N,
)
obj(
    "DocumentParse",
    "state attempt sourceHash",
    state={"enum": ["queued", "running", "ready", "partial", "failed", "cancelled"]},
    attempt=S,
    sourceHash=S,
    parserVersion=S,
    warnings=array(S),
    notes=array(S),
    error=S,
    count=N,
    created=N,
)
obj(
    "DocumentSource",
    "id hash locator parserVersion",
    id=S,
    hash=S,
    locator=S,
    parserVersion=S,
)
obj(
    "TableSheet",
    "locator name rowsRead columnsRead partial",
    locator=S,
    name=S,
    rowsRead=N,
    columnsRead=N,
    partial=B,
)
obj(
    "DocumentPreview",
    "text source locations warnings partial",
    text=S,
    source=ref("DocumentSource"),
    locations=array(S),
    warnings=array(S),
    notes=array(S),
    partial=B,
    nextOffset={"type": ["number", "null"]},
    nextLocationOffset={"type": ["number", "null"]},
    totalChars=N,
    totalLocations=N,
    sheets=array(ref("TableSheet")),
)
obj(
    "EventRow",
    "seq act",
    seq=N,
    inputBudget=ref("InputBudget"),
    sourceOffset=N,
    act=S,
    text=NULL_S,
    question=NULL_S,
    status=NULL_S,
    tool=NULL_S,
    error=NULL_S,
    run_id=NULL_S,
    action_id=NULL_S,
    attachments=array(ref("MessageAttachment")),
)
obj(
    "ArtifactSource",
    "actor taskId threadId actionId runId",
    actor={"enum": ["agent", "user"]},
    taskId=S,
    threadId=S,
    actionId=NULL_S,
    runId=NULL_S,
    sessionId=NULL_S,
    tool=S,
)
obj("ArtifactInput", "id hash", id=S, hash=S, versionId=S, width=N, height=N, format=S)
obj(
    "ArtifactVersion",
    "id hash bytes mime extension created source state",
    id=S,
    hash=S,
    bytes=N,
    mime=S,
    extension=S,
    created=N,
    source=ref("ArtifactSource"),
    inputResource=ref("ArtifactInput"),
    state={
        "enum": ["creating", "ready", "failed", "interrupted", "unknown", "missing"]
    },
    title=S,
    width=N,
    height=N,
    error=S,
)
obj(
    "Artifact",
    "id title permission versions",
    id=S,
    title=S,
    permission={"enum": ["read", "none"]},
    versions=array(ref("ArtifactVersion")),
)
obj(
    "ArtifactRead",
    "id title version fileName",
    id=S,
    title=S,
    version=ref("ArtifactVersion"),
    fileName=S,
    text=S,
    dataUrl=S,
    data=S,
    protectedRoot=S,
    totalChars=N,
    offset=N,
    nextOffset={"type": ["number", "null"]},
)
obj(
    "ArtifactPublished",
    "id title version",
    id=S,
    title=S,
    version=ref("ArtifactVersion"),
)
SCHEMA["$defs"]["EventRow"]["additionalProperties"] = True
obj(
    "DebugFrame",
    "id name line",
    id=N,
    name=S,
    line=N,
    source={"type": "object"},
    column=N,
)
obj(
    "AgentDebugger",
    "id conversationId resourceId workspaceId revision source state program stack output created",
    id=S,
    conversationId=S,
    resourceId=S,
    workspaceId=S,
    revision=S,
    source=ref("ArtifactSource"),
    state=S,
    program=S,
    stack=array(ref("DebugFrame")),
    output=S,
    created=N,
    evidenceTruncated=B,
    lastEvidence={"type": "object"},
    lastSource=ref("ArtifactSource"),
)
obj(
    "ApplicationPreview",
    "id conversationId resourceId workspaceId revision port url source state created",
    id=S,
    conversationId=S,
    resourceId=S,
    workspaceId=S,
    revision=S,
    port=N,
    url=S,
    readyUrl=S,
    source=ref("ArtifactSource"),
    state=S,
    created=N,
    processId=S,
    canStop=B,
    process=ref("ProcessRun"),
    log=S,
    error=S,
    stale=B,
    httpStatus=N,
)
obj("RepositoryImport", "id state url destination",
    id=S, state={"enum": ["running", "stopping", "completed", "failed", "cancelled", "interrupted"]},
    url=S, destination=S, project=ref("Project"), projectId=S, resourceId=S, head=S, branch=S,
    shallow=B, isolation=S, code=S, error=S, notice=S)
obj(
    "CodeResult",
    "",
    path=S,
    revision=S,
    previousRevision=NULL_S,
    projectRevision=NULL_S,
    fileChangeRevision=S,
    backupId=S,
    changeId=S,
    projectBaseline=S,
    projectBaselineReason=S,
    historyCoverage=S,
    error=S,
    code=S,
    ok=B,
    process=ref("ProcessRun"),
    text=S,
    next=N,
    hasMore=B,
    truncated=B,
    outputLimitBytes=N,
    outputNotice=S,
    git=ref("GitState"),
    projectRevisionAfter=S,
)
obj(
    "CodeOperation",
    "id operation resourceId workspaceId source started beforeRevision state",
    id=S,
    operation=S,
    resourceId=S,
    workspaceId=S,
    source=ref("ArtifactSource"),
    started=N,
    beforeRevision=NULL_S,
    fileOnly=B,
    state=S,
    finished=N,
    result=ref("CodeResult"),
)

obj('SourceVersion','kind hash',kind=S,hash=NULL_S,bytes=N,bom=B,eol=S,mode=S)
obj('ChangeTotals','additions deletions uncounted',additions=N,deletions=N,uncounted=N)
obj('SourceChangeFile','path before after status attribution',path=S,before=ref('SourceVersion'),after=ref('SourceVersion'),staged=ref('SourceVersion'),status=S,attribution=S,sourceIds=array(S),additions=N,deletions=N)
obj('SourceChange','id conversationId resourceId source operation started state files fileCount fileOffset hasMoreFiles',id=S,conversationId=S,resourceId=S,workspaceId=S,source={'type':'object'},operation=S,started=N,state=S,revision=S,files=array(ref('SourceChangeFile')),fileCount=N,fileOffset=N,hasMoreFiles=B,notice=S,coverage=S,unsupported=N,branch=NULL_S,head=NULL_S,before=S,after=NULL_S,baseline=S,finished=N,totals=ref('ChangeTotals'),projectBaseline=S,projectBaselineReason=S)
obj('SourceChangeSummaryFile','index path status',index=N,path=S,status=S)
obj('SourceChangeSummary','id source operation state files access',id=S,source={'type':'object'},operation=S,started=N,state=S,revision=NULL_S,resourceId=S,notice=S,fileCount={'type':['number','null']},files=array(ref('SourceChangeSummaryFile')),filesExcerpt=B,access=S)
obj('SourceChangePage','changes hasMore nextBefore notice',changes=array(ref('SourceChangeSummary')),hasMore=B,nextBefore={'type':['number','null']},notice=S)
obj('SourceFilePage','text next hasMore revision complete path view',text=S,next=N,hasMore=B,revision=S,complete=B,path=S,view=S,additions=N,deletions=N,totalLines=N,oldLineStart=N,newLineStart=N,inHunk=B,lineStart=N,columnStart=N,totalTextLines=N,oldTextLines=N,newTextLines=N,totalHunks=N,currentHunk=N)
obj('SourceSearchMatch','offset end line column preview previewClipped',offset=N,end=N,line=N,column=N,preview=S,previewClipped=B)
obj('SourceSearchPage','matches next hasMore complete revision path view',matches=array(ref('SourceSearchMatch')),next=N,hasMore=B,complete=B,revision=S,path=S,view=S)
obj('SourceRangeSource','id state revision',id=S,state=S,revision=NULL_S)
obj('SourceRangeIssue','code path changeId',code=S,path=NULL_S,changeId=NULL_S)
obj('SourceRange','id conversationId resourceId scope runId baseline files issues incomplete derived notice fileOffset fileCount hasMoreFiles issueOffset totalIssues hasMoreIssues sourceCount sources sourcesExcerpt separatedExternalEdits revision',id=S,conversationId=S,resourceId=S,scope=S,runId=NULL_S,baseline=S,files=array(ref('SourceChangeFile')),issues=array(ref('SourceRangeIssue')),incomplete=B,derived=B,notice=S,fileOffset=N,fileCount=N,hasMoreFiles=B,issueOffset=N,totalIssues=N,hasMoreIssues=B,sourceCount=N,sources=array(ref('SourceRangeSource')),sourcesExcerpt=B,separatedExternalEdits=N,revision=S,created=N,workspaceRevision=S,totals=ref('ChangeTotals'),projectBaseline=S,projectBaselineReason=S)
obj('RunChangeFile','path status attribution beforeKind afterKind additions deletions resourceId resourceName changeId revision fileIndex',path=S,status=S,attribution=S,beforeKind=S,afterKind=S,additions={'type':['number','null']},deletions={'type':['number','null']},resourceId=S,resourceName=S,changeId=S,revision=S,fileIndex=N)
obj('RunChangeSummary','runId files fileCount nextOffset frames totals incomplete',runId=S,files=array(ref('RunChangeFile')),fileCount=N,nextOffset={'type':['number','null']},frames=array(S),changedFileFrames=array(S),totals=ref('ChangeTotals'),incomplete=B)

obj(
    "EvidenceAction",
    "id identityKind tool role runId state firstPosition summary missingFields",
    id=S,
    identityKind=S,
    tool=NULL_S,
    role=NULL_S,
    runId=NULL_S,
    state=S,
    firstPosition=N,
    summary=NULL_S,
    missingFields=array(S),
    input={},
    location=NULL_S,
    startedAt={"type": ["number", "null"]},
    finishedAt={"type": ["number", "null"]},
    output={"anyOf": [{"type": "object"}, {"type": "null"}]},
    error=NULL_S,
    impact={"type": "object"},
    approvalRefs=array(S),
    conversationId=S,
    projection={"type": "object"},
    evidenceRevision=S,
)
obj(
    "RestorationPlan",
    "id conversationId resourceId sourceOperationId path state currentRevision restoreRevision projectRevision branch head planRevision diff diffTruncated projectAdvanced protected warning writeMethod",
    id=S,
    conversationId=S,
    resourceId=S,
    sourceOperationId=S,
    sourceActionId=NULL_S,
    path=S,
    state={
        "enum": [
            "draft",
            "authorized",
            "applying",
            "completed",
            "failed",
            "unknown",
            "expired",
            "revoked",
        ]
    },
    currentRevision=S,
    restoreRevision=S,
    projectRevision=S,
    branch=S,
    head=S,
    planRevision=S,
    diff=S,
    diffTruncated=B,
    projectAdvanced=B,
    workspaceComparison={"enum": ["matches", "changed", "unavailable"]},
    protected=B,
    warning=S,
    writeMethod=S,
    approvedAt=N,
    expiresAt=N,
    observedFileRevision=S,
    fileMatches=B,
    verification=S,
    result=ref("CodeResult"),
    note=S,
)
obj(
    "RestorationBaseline",
    "text next hasMore hash historical",
    text=S,
    next=N,
    hasMore=B,
    hash=S,
    historical=B,
)
obj(
    "RestorationSummary",
    "id path state actor position",
    id=S,
    path=S,
    state=S,
    actor=S,
    position=N,
    sourceOperationId=S,
    created=N,
    error=S,
)
obj(
    "RestorationPage",
    "plans hasMore nextBefore",
    plans=array(ref("RestorationSummary")),
    hasMore=B,
    nextBefore={"type": ["number", "null"]},
)
obj(
    "CorrectionTarget",
    "kind id",
    kind={"enum": ["action", "artifact", "code"]},
    id=S,
    revision=S,
    versionId=S,
    hash=S,
    runId=NULL_S,
    sourceKind=S,
    conversationId=S,
    fileIndex=N,
    path=S,
    side=S,
    line=N,
    endLine=N,
    resourceId=S,
)
obj(
    "Correction",
    "id position text target state acceptedAt queuedAt note",
    id=S,
    position=N,
    text=S,
    target=ref("CorrectionTarget"),
    state={
        "enum": ["queued", "applying", "applied_at_boundary", "superseded", "unable"]
    },
    acceptedAt=N,
    queuedAt=N,
    appliedAt=N,
    note=S,
    runId=S,
)
obj(
    "CorrectionPage",
    "corrections hasMore nextBefore",
    corrections=array(ref("Correction")),
    hasMore=B,
    nextBefore={"type": ["number", "null"]},
)
obj(
    "EvidencePage",
    "actions hasMore nextBefore gapCount duplicateCount outOfOrderCount malformedCount sequenceConflicts catchingUp",
    actions=array(ref("EvidenceAction")),
    hasMore=B,
    nextBefore={"type": ["number", "null"]},
    gapCount=N,
    duplicateCount=N,
    outOfOrderCount=N,
    malformedCount=N,
    sequenceConflicts=N,
    catchingUp=B,
)
obj("ShellTranscript", "command state exitCode", command=NULL_S, state=NULL_S, exitCode={"type": ["number","null"]},
    error=NULL_S, timedOut=B, processState=NULL_S)
obj(
    "EvidenceOutput",
    "text next hasMore complete",
    terminal=ref("ShellTranscript"),
    text=S,
    next=N,
    hasMore=B,
    complete=B,
    reason=S,
    bytes=N,
    hash=S,
    evidenceRevision=S,
)

obj(
    "DeliveryTarget",
    "kind id",
    kind={"enum": ["artifact", "code"]},
    id=S,
    revision=S,
    versionId=S,
    hash=S,
)
obj(
    "RequirementCoverage",
    "id kind target note status stale",
    id=S,
    kind={"enum": ["automatic", "model", "human"]},
    target=ref("DeliveryTarget"),
    note=S,
    status=S,
    stale=B,
    actionId=S,
    evidenceRevision=S,
    actor=S,
)
obj(
    "DeliveryRequirement",
    "id text revision actor coverage uncovered",
    id=S,
    text=S,
    revision=S,
    actor=S,
    coverage=array(ref("RequirementCoverage")),
    uncovered=B,
    required=B,
    history=array({"type": "object"}),
)
obj(
    "DeliveryDecision",
    "id decision target reason stale",
    id=S,
    decision={"enum": ["accept", "reject"]},
    target=ref("DeliveryTarget"),
    reason=S,
    stale=B,
    at=N,
    acknowledgedGaps=B,
    scopeUnknown=B,
)
obj(
    "DeliveryFinding",
    "id text target state stale",
    id=S,
    text=S,
    target=ref("DeliveryTarget"),
    state={"enum": ["open", "closed"]},
    stale=B,
    history=array({"type": "object"}),
    scopeUnknown=B,
)
obj(
    "DeliveryOverview",
    "revision target requirements decisions findings execution hasMore nextBefore gaps gapsProvisional",
    revision=N,
    target={"anyOf": [ref("DeliveryTarget"), {"type": "null"}]},
    requirements=array(ref("DeliveryRequirement")),
    decisions=array(ref("DeliveryDecision")),
    findings=array(ref("DeliveryFinding")),
    execution=ref("Runtime"),
    hasMore=B,
    nextBefore={"type": ["number", "null"]},
    gaps=B,
    gapsProvisional=B,
    requirementsRevision=S,
    coverageRevision=S,
    totalRequirements=N,
    openFindings=N,
    findingsHasMore=B,
    findingsNextBefore={"type": ["number", "null"]},
    decisionsHasMore=B,
    decisionsNextBefore={"type": ["number", "null"]},
)
obj("DeliveryUpdated", "revision record", revision=N, record={"type": "object"})
obj(
    "StageSource",
    "seq position kind runId",
    seq=N,
    position=N,
    kind=S,
    runId=S,
    role=S,
    state=S,
)
obj(
    "RoleProgress",
    "state reason lastSeq attempts source",
    state=S,
    reason=S,
    lastSeq=N,
    attempts=N,
    source={"anyOf": [ref("StageSource"), {"type": "null"}]},
)
obj("StageJudgment", "role text source", role=S, text=S, source=ref("StageSource"))
obj(
    "StageRun",
    "id firstPosition goal roles judgments blocker outcome modelCalls actionCount failedActions unknownActions",
    id=S,
    firstPosition=N,
    goal=S,
    goalExcerpt=B,
    crew=NULL_S,
    parallelRoles=B,
    latestCandidate={"type": "object"},
    roles={"type": "object", "additionalProperties": ref("RoleProgress")},
    judgments=array(ref("StageJudgment")),
    blocker=NULL_S,
    outcome=NULL_S,
    modelCalls=N,
    actionCount=N,
    failedActions=N,
    unknownActions=N,
)
obj("StageSummary", "kind text", kind=S, text=S, fallback=B)
obj("CompletionGap", "kind id role text state", kind=S, id=S, role=S, text=S, state=S)
obj("EvidenceTarget", "kind conversationId runId actionId evidenceRevision version",
    kind={"enum":["action","check-log"]}, conversationId=S, runId=S, actionId=S,
    evidenceRevision=S, version=ref("DeliveryTarget"), operationId=S, workspaceId=S, reviewRecordId=S)
obj("EvidenceTargetResult", "conversationId runId kind id archived target reason",
    conversationId=S, runId=S, kind=S, id=S, archived=B,
    target={"anyOf":[ref("EvidenceTarget"),{"type":"null"}]}, reason=NULL_S)
obj("CompletionFacts", "run checks acceptance", run=S, checks=S, acceptance=S)
obj("CompletionStatus", "status label completed pending", status=S, label=S, completed=array(S),
    pending=array(ref("CompletionGap")), checkSummary=S, facts=ref("CompletionFacts"))
obj(
    "StageCoverage",
    "fromPosition throughPosition sourceBytes currentSourceBytes maxSeq",
    fromPosition=N,
    throughPosition=N,
    sourceBytes=N,
    currentSourceBytes=N,
    maxSeq=N,
)
obj(
    "StageOverview",
    "run runtime summary checksMissing evidenceIncomplete previousSummaryStale evidenceRange isDelegated",
    run={"anyOf": [ref("StageRun"), {"type": "null"}]},
    runtime=ref("Runtime"),
    summary={"anyOf": [ref("StageSummary"), {"type": "null"}]},
    checksMissing=B,
    completion={"anyOf": [ref("CompletionStatus"), {"type": "null"}]},
    currentRunPending=B,
    currentRunUnknown=B,
    evidenceIncomplete=B,
    previousSummaryStale=B,
    evidenceRange=ref("StageCoverage"),
    isDelegated=B,
)

obj('MemoryNode','id tree layer title body version selected reason adapter',id=S,tree=S,layer=array(S),title=S,body=S,version={'type':['number','string']},selected=B,reason=S,adapter=S,readonly=B,legacyId=S,state=S,priority={'type':['number','string']},weight=N,kind=S,scope=S,storeRevision=N,entryType=S,learning={'type':'object'},sources=array({'type':'object'}),evidence=array({'type':'object'}),versions=array({'type':'object'}),habitProposal={'type':'object'})
obj('MemoryRequest','id runId role at loaded',id=S,runId=S,role=S,at=N,loaded=B)
obj('MemoryProviderIdentity','id generation adapter fingerprint',id=NULL_S,generation=N,adapter=NULL_S,fingerprint=NULL_S)
obj('MemoryProviderView','identity available capabilities',identity=ref('MemoryProviderIdentity'),available=B,capabilities={'type':'object','additionalProperties':B})
obj('MemoryProviderChoice','id name adapter builtin installed description',id=S,name=S,adapter=S,builtin=B,installed=B,description=S)
obj('MemoryProviderSettings','providerId revision generation builtinInstalled providers notice',providerId=NULL_S,revision=N,generation=N,builtinInstalled=B,providers=array(ref('MemoryProviderChoice')),notice=S)
obj('MemoryView','revision workspaceContextId projectId conversationId mode nodes selectionVersion conflicts history notice requests sessionMemory',revision=N,scopeRevisions={'type':'object','additionalProperties':N},provider=ref('MemoryProviderView'),workspaceContextId=NULL_S,projectId=NULL_S,conversationId=NULL_S,mode=S,nodes=array(ref('MemoryNode')),selectionVersion=S,conflicts=array({'type':'object'}),history=array({'type':'object'}),notice=S,requests=array(ref('MemoryRequest')),sessionMemory=S,offset=N,totalNodes=N,hasMore=B,nextOffset={'type':['number','null']})
obj('MemoryInventory','projects conversations',projects=array({'type':'object'}),conversations=array({'type':'object'}))

obj('ConversationRecord', 'status resourceId relativePath historyRevision syncedRevision errorCode hasHistoricalTruncation sizeBytes',
    status={'enum': ['empty', 'pending', 'saved', 'blocked', 'not_applicable']},
    resourceId=NULL_S, relativePath=NULL_S, historyRevision=NULL_S, syncedRevision=NULL_S,
    errorCode=NULL_S, hasHistoricalTruncation=B, sizeBytes={'type': ['number', 'null']})
obj('ConversationRecordPage', 'text revision nextOffset hasMore sizeBytes',
    text=S, revision=S, nextOffset=N, hasMore=B, sizeBytes=N)
