"""Verified service presets; model identifiers always come from the endpoint.

No credentials or static model claims belong in this catalogue. Regional keys
and coding subscriptions are not interchangeable with general API billing.
"""
# 主要引用方包括 `provider.py` 和 `models.py`。
from urllib.parse import urlsplit


def preset(ident, name, base, docs, group, note="", **fields):
    return dict(id=ident, name=name, base_url=base, docs_url=docs, group=group,
                note=note, protocol="OpenAI 兼容 Chat Completions", checked="2026-11-05",
                runtime_supported=True, **fields)


PROVIDERS = [
    preset("deepseek", "DeepSeek", "https://api.deepseek.com/v1", "https://api-docs.deepseek.com/api/list-models/", "国内服务", aliases=["https://api.deepseek.com"]),
    preset("glm", "智谱 GLM", "https://open.bigmodel.cn/api/paas/v4", "https://docs.bigmodel.cn/cn/guide/develop/openai/introduction", "国内服务", "普通 API 地址；GLM Coding Plan 使用独立套餐地址和额度。"),
    preset("kimi", "Kimi / Moonshot", "https://api.moonshot.cn/v1", "https://platform.kimi.com/docs/get-api-key", "国内服务", "普通 API Key；Kimi Code 订阅凭据和地址不同，不可混用。"),
    preset("qwen", "通义千问 / 阿里云百炼", "https://dashscope.aliyuncs.com/compatible-mode/v1", "https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope", "国内服务", "北京普通 API 兼容地址；地域、业务空间及套餐 Key 必须与控制台地址匹配，新业务空间可替换为其专属地址。"),
    preset("doubao", "豆包 / 火山方舟", "https://ark.cn-beijing.volces.com/api/v3", "https://docs.volcengine.com/docs/ark/compatible-with-openai-sdk?lang=zh", "国内服务", "按量 API 地址；需开通模型。部分调用使用推理接入点 ID，请按控制台提供的模型或接入点填写。"),
    preset("minimax-cn", "MiniMax 中国", "https://api.minimax.cn/v1", "https://platform.minimax.cn/docs/api-reference/text-openai-api", "国内服务", "中国站 API；国际站 Key 与地址分开配置。", aliases=["https://api.minimaxi.com/v1"]),
    preset("baidu", "百度千帆 / 文心 ERNIE", "https://qianfan.baidubce.com/v2", "https://cloud.baidu.com/doc/qianfan-docs/s/Fm9l6ocai", "国内服务", "使用千帆新版 API Key；旧版 AK/SK 或 OAuth access_token 不是此处的 API Key。"),
    preset("hunyuan", "腾讯混元", "https://api.hunyuan.cloud.tencent.com/v1", "https://cloud.tencent.com/document/product/1729/111007", "国内服务", "使用兼容接口 API Key；腾讯云 SecretId/SecretKey 不能直接填入。新购服务请核对当前平台入口。"),
    preset("openai", "OpenAI / GPT", "https://api.openai.com/v1", "https://developers.openai.com/api/reference/resources/models/methods/list", "国际服务", "使用 API 平台 Key；ChatGPT 订阅不等于 API 额度。仅限支持 Chat Completions 的模型。", completion_limit_field="max_completion_tokens"),
    preset("anthropic", "Anthropic / Claude", "https://api.anthropic.com/v1", "https://platform.claude.com/docs/en/cli-sdks-libraries/libraries/openai-sdk", "国际服务", "使用官方兼容接口及已绑定 Workspace 的 API Key；原生 Messages 未接入，兼容接口不返回原生思考内容。", version_header="2026-10-21"),
    preset("gemini", "Google / Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", "https://ai.google.dev/gemini-api/docs/openai", "国际服务", "使用 Gemini API Key 和官方兼容入口；Google Cloud 服务账号及 Vertex AI 凭据不是此处的 Key。"),
    preset("grok", "xAI / Grok", "https://api.x.ai/v1", "https://docs.x.ai/developers/rest-api-reference/inference", "国际服务", "使用 xAI API Key；X 平台订阅不等于 API 额度。"),
    preset("zai", "Z.AI / GLM 国际", "https://api.z.ai/api/paas/v4", "https://docs.z.ai/guides/overview/quick-start", "国际服务", "Z.AI 国际站普通 API，和中国智谱站、Coding Plan 分开配置。"),
    preset("minimax", "MiniMax 国际", "https://api.minimax.io/v1", "https://platform.minimax.io/docs/api-reference/text-openai-api", "国际服务", "国际站 API；使用该站对应 Key。"),
    preset("mistral", "Mistral AI", "https://api.mistral.ai/v1", "https://docs.mistral.ai/resources/migration-guides", "国际服务"),
    preset("cohere", "Cohere / Command", "https://api.cohere.ai/compatibility/v1", "https://docs.cohere.com/docs/compatibility-api", "国际服务", "使用官方兼容入口；若不提供模型目录，请按控制台手填模型 ID。"),
    preset("siliconflow", "硅基流动 / SiliconFlow", "https://api.siliconflow.cn/v1", "https://docs.siliconflow.cn/docs/userguide/quickstart", "聚合与托管平台"),
    preset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", "https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties", "聚合与托管平台", "网关目录不代表原厂直接授权；模型访问受账户、路由与供应商设置影响。"),
    preset("groq", "Groq", "https://api.groq.com/openai/v1", "https://console.groq.com/docs/openai", "聚合与托管平台", "Groq 是推理服务商，与 xAI 的 Grok 不同。"),
    preset("together", "Together AI", "https://api.together.ai/v1", "https://docs.together.ai/docs/inference/openai-compatibility", "聚合与托管平台", aliases=["https://api.together.xyz/v1"]),
    preset("fireworks", "Fireworks AI", "https://api.fireworks.ai/inference/v1", "https://docs.fireworks.ai/tools-sdks/openai-compatibility", "聚合与托管平台"),
    preset("nvidia", "NVIDIA NIM / API Catalog", "https://integrate.api.nvidia.com/v1", "https://docs.api.nvidia.com/nim/reference/llm-apis", "聚合与托管平台", "此为托管 API 地址；自托管 NIM 应使用自己的兼容地址。"),
    preset("ollama", "Ollama 本地", "http://127.0.0.1:11434/v1", "https://docs.ollama.com/api/openai-compatibility", "本地服务", "先启动本地服务并安装模型；无需真实密钥时使用界面填入的本地占位值。", local=True, aliases=["http://localhost:11434/v1"]),
    preset("lmstudio", "LM Studio 本地", "http://127.0.0.1:1234/v1", "https://lmstudio.ai/docs/developer/openai-compat", "本地服务", "先启动本地兼容服务；若服务器启用认证，请替换本地占位 Key。", local=True, aliases=["http://localhost:1234/v1"]),
    preset("azure", "Azure OpenAI（资源地址）", "", "https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/switching-endpoints?view=foundry-classic", "资源与特殊接入", "填写资源的 /openai/v1 地址和 API Key，模型栏使用部署名称；不支持旧 api-version 查询串或自动获取 Entra 令牌。", requires_endpoint=True, endpoint_hint="https://你的资源.openai.azure.com/openai/v1", completion_limit_field="max_completion_tokens"),
    dict(preset("perplexity", "Perplexity（待 Responses 适配）", "", "https://docs.perplexity.ai/docs/agent-api/openai-compatibility", "资源与特殊接入", "当前官方路线为 Responses API，运行器尚未适配；此项仅说明入口，暂不能保存。已有 Chat Completions 网关可用自定义端点。", endpoint_hint="https://api.perplexity.ai/v1"), runtime_supported=False, protocol="Responses（待适配）"),
]


def model_source(base):
    """Recognize only exact service URLs or the documented Azure namespace."""
    try:
        u = urlsplit(base)
        if u.username or u.password or u.query or u.fragment:
            return None
        u.port  # validate before matching
        for provider in PROVIDERS:
            for value in [provider["base_url"], *provider.get("aliases", [])]:
                if not value:
                    continue
                official = urlsplit(value)
                port = u.port or (443 if u.scheme == "https" else 80)
                expected = official.port or (443 if official.scheme == "https" else 80)
                if (u.scheme == official.scheme and u.hostname == official.hostname
                        and port == expected and u.path.rstrip("/") == official.path.rstrip("/")):
                    return dict(provider)
        if (u.scheme == "https" and (u.port in (None, 443)) and u.hostname
                and u.hostname.endswith(".openai.azure.com") and u.path.rstrip("/") == "/openai/v1"):
            return dict(next(p for p in PROVIDERS if p["id"] == "azure"))
    except (ValueError, TypeError):
        pass
    return None


def request_headers(base, key):
    headers = {"Authorization": "Bearer " + key}
    service = model_source(base)
    if service and service.get("version_header"):
        headers["anthropic-version"] = service["version_header"]
    return headers
