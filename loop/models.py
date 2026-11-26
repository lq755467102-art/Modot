#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""models.py —— 模型档案：哪一把 key、接哪个端点、用哪个模型

为什么要有这一层
    原来凭据只有一份，写在 `~/.workbuddy/harness/credentials.env` 里，
    并且**环境变量永远赢**。于是两件事做不到：
      ① 换模型要改文件、重启，而且改完看不出「现在到底生效的是哪一份」；
      ② 自我和本我想要「各用一家的 key」，得手写两套前缀，写错就静默回退。
    现在改成**档案**（profile）：
      · 一份档案 = 一个名字 + 端点 + key + 模型；
      · 自我挑一份、本我挑一份；
      · 界面上一眼看得见两个角色各用哪个模型，能一键互换。

★ 它和 `~/.workbuddy/harness/credentials.env` 的关系（别搞混）
    credentials.env  是**上一代**的形态：固定前缀、只能有一套、环境变量优先。
    models.json      是这一代的形态：多套、可选、按角色分。
    优先级从高到低：
        显式传参  >  环境变量  >  models.json 里选中的那一份  >  credentials.env
    为什么环境变量排在 models.json 前面：命令行临时指定（`HARNESS_MODEL=x`）是
    一个有意的、当场的决定，该压过界面上的设置。界面会把「被环境变量盖住」
    显示出来 —— 不然就会出现「我在界面上选了，它不认」而这种沉默失败。

★ 为什么存在仓库外
    这里面有 key。仓库会进 git、会被拷走、会被打包。所以文件固定落在
    `~/.workbuddy/harness/models.json`（和 credentials.env 同一个目录），
    可用 `HARNESS_MODELS` 覆盖（测试用）。
    ⇒ **打包出来的程序里不会带任何人的 key**：它读的是「装它的那台机器的家目录」。

★ 它不负责什么
    不做连接测试（那是 `provider` 的事）、不管 fallback 链、不碰 `charter.json`。
    这一层只回答一个问题：「这个角色现在该用哪一份」。
"""

# 主要引用方包括 `provider.py` 和 `permissions.py`。

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .model_catalog import PROVIDERS, model_source, request_headers

# 角色名 → 环境变量前缀（和 provider 里那套保持一致）
ROLES = {"main": "HARNESS", "review": "HARNESS_REVIEW", "plan": "HARNESS_PLAN",
         "vision": "HARNESS_VISION"}
# 称呼统一（2026-10-29 用户：「不用本我、自我来称呼 agent 了」）：
#   执行者 = 干活的那个；测试者 = 复核的那个；规划者 = 定契约/方向的那个
ROLE_CN = {"main": "执行者", "review": "测试者", "plan": "规划者", "vision": "视觉核心模型"}


def path() -> Path:
    """档案文件在哪。`HARNESS_MODELS` 可覆盖（探针和"两套环境"用）。"""
    env = os.environ.get("HARNESS_MODELS")
    if env:
        return Path(env)
    return Path.home() / ".workbuddy" / "harness" / "models.json"


def _empty() -> dict:
    return {"version": 1, "profiles": [], **dict.fromkeys(ROLES, "")}


def load() -> dict:
    """读档案。坏文件不抛 —— 但要**留下原因**，别让人对着空列表猜。"""
    p = path()
    if not p.exists():
        return _empty()
    try:
        if p.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("模型配置超过2MiB读取边界")
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        out = _empty()
        out["error"] = "models.json 读不出来（%s）—— 现在按「没配过」处理" % e
        return out
    if not isinstance(d, dict):
        out = _empty()
        out["error"] = "models.json 不是一个对象，按「没配过」处理"
        return out
    out = _empty()
    profiles = d.get("profiles")
    if (
        d.get("version", 1) != 1
        or not isinstance(profiles, list)
        or any(
            not isinstance(x, dict)
            or any(
                not isinstance(x.get(k), str)
                for k in ("id", "name", "base_url", "model", "api_key")
            )
            for x in profiles
        )
        or len({x["id"] for x in profiles}) != len(profiles)
    ):
        out["error"] = "models.json 结构或版本不兼容，原文件保留；禁止覆盖写入"
        return out
    out["profiles"] = profiles
    out.update({role: str(d.get(role) or "") for role in ROLES})
    return out


def save(d: dict) -> dict:
    """原子写。key 在里面，所以顺手把权限收到 600（Windows 上尽力而为）。"""
    p = path()
    if p.exists() and load().get("error"):
        return {
            "ok": False,
            "error": "已有模型配置无法读取，原文件保留；请先备份核对，不覆盖为空配置",
        }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        try:
            os.chmod(str(tmp), 0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(p))
        return {"ok": True, "path": str(p)}
    except OSError as e:
        return {"ok": False, "error": "写不进去（%s）：%s" % (p, e)}


# ── 展示用 ────────────────────────────────────────────────────────
def _mask(key: str) -> str:
    """只露头尾。**不返回明文** —— 响应可能被截图、被日志带走。"""
    k = str(key or "")
    if len(k) <= 8:
        return "•" * len(k) if k else ""
    return "%s…%s" % (k[:4], k[-4:])


def _id(name: str, base_url: str, model: str) -> str:
    import hashlib

    h = hashlib.sha256(
        ("%s|%s|%s" % (name, base_url, model)).encode("utf-8")
    ).hexdigest()
    return "p-" + h[:8]


def public_url(value):
    from urllib.parse import urlsplit, urlunsplit
    from .evidence_redaction import redact

    try:
        url = urlsplit(str(value))
        host = url.hostname or ""
        if ":" in host:
            host = "[" + host + "]"
        if url.port:
            host += ":" + str(url.port)
        return redact(urlunsplit((url.scheme, host, url.path, "", "")))
    except ValueError:
        return "无法显示的端点"


def public_name(name, endpoint):
    from urllib.parse import urlsplit
    from .evidence_redaction import redact

    try:
        parsed = urlsplit(str(endpoint))
        if parsed.username is not None and parsed.netloc and parsed.hostname:
            return redact(str(name).replace(parsed.netloc, parsed.hostname))
        if str(name) == parsed.netloc and parsed.hostname:
            return parsed.hostname
    except ValueError:
        pass
    return redact(str(name))


def public() -> dict:
    """给界面看的一份：key 只给掩码，附带"现在生效的是什么"。"""
    d = load()
    out = []
    for x in d["profiles"]:
        out.append(
            {
                "id": x.get("id") or "",
                "name": public_name(x.get("name") or "(没名字)", x.get("base_url")),
                "base_url": public_url(x.get("base_url") or ""),
                "provider_name": (model_source(x.get("base_url") or "") or {}).get("name", ""),
                "model": x.get("model") or "",
                **{k: x[k] for k in ("max_tokens", "max_output_tokens") if k in x},
                "key_masked": _mask(x.get("api_key")),
                "has_key": bool(x.get("api_key")),
                "at": x.get("at") or "",
            }
        )
    eff = {}
    for role in ROLES:
        r = resolve(role)
        eff[role] = {
            "name": public_name(r.get("name") or "", r.get("base_url")),
            "model": r.get("model") or "",
            "base_url": public_url(r.get("base_url") or ""),
            "source": r.get("source") or "none",
            "source_cn": {
                "models": "档案里选的",
                "env": "环境变量",
                "creds": "credentials.env",
                "none": "没配",
            }.get(r.get("source") or "none", "?"),
            "overridden": r.get("overridden") or [],
        }
    return {
        "ok": True,
        "path": str(path()),
        "revision": revision(),
        "profiles": out,
        "providers": [dict(p) for p in PROVIDERS],
        **{role: d[role] for role in ROLES},
        "effective": eff,
        "error": d.get("error") or "",
        "env_prefixes": {role: prefix + "_*" for role, prefix in ROLES.items()},
    }


def revision():
    import hashlib

    p = path()
    return (
        "unavailable"
        if p.exists() and p.stat().st_size > 2 * 1024 * 1024
        else hashlib.sha256(p.read_bytes()).hexdigest()
        if p.exists()
        else "missing"
    )


def user_action(body):
    """Serialize the human read-modify-write boundary, including imports."""
    from .filelock import FileLock

    act = str(body.get("act") or "save").strip().lower()
    if act == "probe":
        return probe(
            base_url=str(body.get("base_url") or ""),
            api_key=str(body.get("api_key") or ""),
            pid=str(body.get("id") or ""),
            role=str(body.get("role") or "main"),
        )
    with FileLock(path().with_suffix(".lock")):
        if (
            body.get("expectedRevision") is not None
            and body["expectedRevision"] != revision()
        ):
            return dict(
                ok=False,
                code="stale_models",
                error="模型配置已变化，旧稿保留；请刷新核对后再保存",
            )
        if act == "save":
            ident = str(body.get("id") or "")
            if ident and not any(p["id"] == ident for p in load()["profiles"]):
                return dict(ok=False, error="待编辑档案已经不存在，未另建或覆盖")
            return upsert(
                pid=ident,
                name=str(body.get("name") or ""),
                base_url=str(body.get("base_url") or ""),
                api_key=str(body.get("api_key") or ""),
                model=str(body.get("model") or ""),
                max_tokens=body.get("max_tokens"),
                max_output_tokens=body.get("max_output_tokens"),
            )
        if act == "del":
            return remove(str(body.get("id") or ""))
        if act == "select":
            return select(str(body.get("role") or "main"), str(body.get("id") or ""))
        if act == "swap":
            return swap()
        if act == "import_env":
            return import_env()
        return dict(ok=False, error="未知模型管理动作")


# ── 改档案 ────────────────────────────────────────────────────────
def _clean_url(u: str) -> str:
    u = str(u or "").strip().rstrip("/")
    if u and not (u.startswith("http://") or u.startswith("https://")):
        return ""
    return u


def output_limit(value, minimum=0):
    """Parse token counts without silently truncating floats or accepting bools."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("输出额度必须是整数")
    if isinstance(value, str) and not value.strip().isdigit():
        raise ValueError("输出额度必须是整数")
    number = int(value)
    if number < minimum:
        raise ValueError("输出额度不能小于%s" % minimum)
    return number


def upsert(
    pid: str = "",
    name: str = "",
    base_url: str = "",
    api_key: str = "",
    model: str = "",
    max_tokens=None,
    max_output_tokens=None,
) -> dict:
    """新增或改一份档案。`api_key` 留空 = 不改（和原来那个面板的约定一致）。"""
    d = load()
    url = _clean_url(base_url)
    if base_url and not url:
        return {"ok": False, "error": "端点要以 http:// 或 https:// 开头"}
    model = str(model or "").strip()
    if len(model)>256 or any(ord(c)<32 for c in model):
        return dict(ok=False,error="模型标识必须在256字符内且不含控制字符")
    name = str(name or "").strip()
    limits = {}
    try:
        for field, value, minimum in (("max_tokens", max_tokens, 0),
                                       ("max_output_tokens", max_output_tokens, 1)):
            if value is not None:
                limits[field] = None if value == "" else output_limit(value, minimum)
    except ValueError as error:
        return dict(ok=False, error=str(error))

    cur = None
    for x in d["profiles"]:
        if x.get("id") == pid:
            cur = x
            break
    if cur is None:
        # 新建：key 必须给（否则这份档案是空壳，选了也用不了）
        if not str(api_key or "").strip():
            return {"ok": False, "error": "新建一份档案要给 key（改已有的可以留空）"}
        if not model:
            return {"ok": False, "error": "要填模型名"}
        if not url:
            return {"ok": False, "error": "要填接口地址"}
        if not name:
            # 名字留空就从端点里取一段，别让人为了用起来先想个名字
            from urllib.parse import urlsplit

            host = urlsplit(url).hostname or "模型档案"
            name = "%s · %s" % (host, model)
        x = {
            "id": _id(name, url, model),
            "name": name[:60],
            "base_url": url,
            "api_key": str(api_key).strip(),
            "model": model,
            "at": _now(),
        }
        x.update({k: v for k, v in limits.items() if v is not None})
        # 同一个 id 已经在（同名字同端点同模型）⇒ 覆盖，不要存两份
        d["profiles"] = [p for p in d["profiles"] if p.get("id") != x["id"]]
        d["profiles"].append(x)
        if not d.get("main"):
            d["main"] = x["id"]
        r = save(d)
        if not r.get("ok"):
            return r
        return {"ok": True, "id": x["id"], "note": "存好了：%s" % x["name"]}

    if url and url != _clean_url(cur.get("base_url") or "") and not str(api_key or "").strip():
        return dict(ok=False,error="API 地址已改变，请为新地址重新填写 Key；未复用原档案密钥")
    if ((url and url != _clean_url(cur.get("base_url") or ""))
            or (model and model != cur.get("model"))):
        # Explicitly rebind limits when changing the identity of a model profile.
        for field in ("max_tokens", "max_output_tokens"):
            cur.pop(field, None)
    if name:
        cur["name"] = name[:60]
    if url:
        cur["base_url"] = url
    if model:
        cur["model"] = model
    if str(api_key or "").strip():
        cur["api_key"] = str(api_key).strip()
    for field, value in limits.items():
        if value is None:
            cur.pop(field, None)
        else:
            cur[field] = value
    cur["at"] = _now()
    r = save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "id": cur["id"], "note": "改好了：%s" % cur.get("name")}


def remove(pid: str) -> dict:
    d = load()
    before = len(d["profiles"])
    d["profiles"] = [x for x in d["profiles"] if x.get("id") != pid]
    if len(d["profiles"]) == before:
        return {"ok": False, "error": "没有这份档案（可能已经删了）"}
    # ★ 删掉正在用的那一份，要把选择一起清掉 ——
    #   否则界面会显示"自我 = 某个不存在的东西"，而下一步就是静默回退到旧凭据。
    # ★ 遍历 ROLES，不写死两个（2026-10-31 盘点）：加了规划者之后，
    #   这里还只清 main/review，删掉正在用的规划者档案会留下一个悬空选择。
    for role in ROLES:
        if d.get(role) == pid:
            d[role] = ""
    r = save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "note": "删了（正在用它的话，那个角色现在空着）"}


def select(role: str, pid: str) -> dict:
    """把某个角色指到一份档案上。`pid` 为空 = 清掉选择（回落到环境/凭据文件）。"""
    role = str(role) if str(role) in ROLES else "main"
    d = load()
    if pid and not any(x.get("id") == pid for x in d["profiles"]):
        return {"ok": False, "error": "没有这份档案"}
    d[role] = pid
    r = save(d)
    if not r.get("ok"):
        return r
    if not pid:
        return {
            "ok": True,
            "note": "%s改回用环境变量 / credentials.env" % ROLE_CN[role],
        }
    # ★ 提示语说**模型名**，不说档案名（2026-10-27）：档案名是给人区分"哪一套 key"的，
    #   而"现在用谁"要回答的是模型。名字里带角色的时候，那句提示会读成
    #   「自我现在用：本我（glm-5.3-flash）」—— 自己把自己绕死。
    _p = next((x for x in d["profiles"] if x.get("id") == pid), {})
    nm = _p.get("model") or _p.get("name") or pid
    return {"ok": True, "note": "%s现在用：%s" % (ROLE_CN[role], nm)}


def swap() -> dict:
    """一键互换自我与本我。**两个都换**，不是只换模型名。

    ★ 为什么是互换而不是"交换模型名"：互换的意思是
      「接下来让本我做自我、让自我做本我」。只换模型名的话，
      端点/key 还留在原角色上，换完变成了两份都不成立的配置。
    """
    d = load()
    a, b = d.get("main") or "", d.get("review") or ""
    # ★★ 空手互换必须**说不行**（2026-10-27 用户报的「模型切换按钮依旧有问题」）。
    #   原来这里只判"两个都空"，然后 `d["main"], d["review"] = b, a` ——
    #   而两边都走 credentials.env 时两个值都是 `""`，互换之后还是 `""`，
    #   什么都没变，却照样返回 ok=True，界面弹「换过来了 —— 自我用 deepseek-flash，
    #   本我用 glm-5.3-flash」。那两句话是真的（读的是 effective），
    #   但**这次点击没有产生任何变化** —— 典型的假成功：人点了、看到成功、世界没动。
    #   ⇒ 互换只对"两边各有档案"才有意义。其余情况如实拒绝，并给下一步。
    if not a and not b:
        return {
            "ok": False,
            "error": "两边都还在用凭据文件（credentials.env），没有档案可以互换",
            "hint": "先点「导入当前配置」，把这两套各存成一份档案；"
            "存好之后 ⇄ 才是真的互换",
        }
    if not a or not b:
        return {
            "ok": False,
            "error": "只有%s一边选了档案，换过去另一边还是空的"
            % ("自我" if a else "本我"),
            "hint": "两边都先选上档案（各存一份），再互换",
        }
    d["main"], d["review"] = b, a
    r = save(d)
    if not r.get("ok"):
        return r
    return {"ok": True, "note": "换过来了：自我 ↔ 本我"}


def import_env() -> dict:
    """把**当前**环境变量 / credentials.env 里那套存成一份档案。

    ★ 为什么要这个：老用户已经配好了，切到档案制之后不该让他重新填一遍 key。
      这也解决了"界面上一个空列表"的第一印象 —— 那是劝退最快的方式。
    """
    from .provider import OpenAICompatProvider as P

    out = []
    for role, pre in ROLES.items():
        cred = {}
        try:
            cred = P._read_creds()
        except Exception:
            cred = {}
        key = os.environ.get(pre + "_API_KEY") or cred.get(pre + "_API_KEY", "")
        url = os.environ.get(pre + "_BASE_URL") or cred.get(pre + "_BASE_URL", "")
        model = os.environ.get(pre + "_MODEL") or cred.get(pre + "_MODEL", "")
        if not (key and url and model):
            continue
        # ★ 名字里**不许带角色**（2026-10-27 用户报的「模型切换按钮依旧有问题」）：
        #   原来叫「自我（deepseek-flash）」/「本我（glm-5.3-flash）」，
        #   而档案是"一套凭据"、不属于哪个角色。一旦点 ⇄ 互换，
        #   界面就变成「自我 本我（glm-5.3-flash）」—— 读起来像开关坏了，
        #   实际是换成功了。角色由下拉的位置表达，名字只说这套**是什么**。
        _nm = model
        _k = 2
        _taken = {str(x.get("name") or "") for x in load()["profiles"]}
        while _nm in _taken:
            _nm = "%s %d" % (model, _k)
            _k += 1
        r = upsert(name=_nm, base_url=url, api_key=key, model=model)
        if r.get("ok"):
            out.append(r["id"])
            select(role, r["id"])
    if not out:
        return {
            "ok": False,
            "error": "当前环境里没有完整的配置可导入"
            "（要 base_url + key + model 三样都在）",
        }
    return {"ok": True, "ids": out, "note": "导入了 %d 份" % len(out)}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M")


# ── 解析：这个角色现在该用哪一份 ───────────────────────────────────
def resolve(role: str = "main") -> dict:
    """返回 `{base_url, api_key, model, name, source, overridden}`。

    **不抛异常**：模型入口建不起来时要能降级，而不是把整轮打死。
    已选择的视觉模型档案按整份连接使用。其他角色的 `overridden` 列出环境变量覆盖的字段，
    供界面显示实际生效的配置。
    """
    role = str(role) if str(role) in ROLES else "main"
    pre = ROLES[role] + "_"
    d = load()
    pid = d.get(role) or ""
    prof = next((x for x in d["profiles"] if x.get("id") == pid), None)
    out = {
        "base_url": "",
        "api_key": "",
        "model": "",
        "name": "",
        "source": "none",
        "overridden": [],
    }
    if prof:
        out["name"] = prof.get("name") or ""
    try:
        cred = _creds()
    except Exception:
        cred = {}
    srcs: dict = {}
    for field, env_name in (
        ("base_url", "BASE_URL"),
        ("api_key", "API_KEY"),
        ("model", "MODEL"),
    ):
        pv = str((prof or {}).get(field) or "")
        ev = os.environ.get(pre + env_name, "")
        cv = cred.get(pre + env_name, "")
        # ★ 三个来源**逐字段**比，不整份覆盖：实测常见的是"档案里有 key 和端点、
        #   环境里只临时指定了 model"。整份覆盖会把这三种信息一起丢掉。
        if role == "vision" and prof:
            # The selected visual connection keeps its endpoint, model and credential together.
            out[field] = pv
            if pv:
                srcs[field] = "models"
        elif ev:
            if pv and pv != ev:
                out["overridden"].append(env_name.lower())
            out[field] = ev
            srcs[field] = "env"
        elif pv:
            out[field] = pv
            srcs[field] = "models"
        elif cv:
            out[field] = cv
            srcs[field] = "creds"
    # Settings belong to this exact model/route, not an environment override.
    # Resolving them does not introduce another discovery/network request.
    if (prof and out["model"] == prof.get("model")
            and out["base_url"].rstrip("/") == prof.get("base_url", "").rstrip("/")):
        for field, minimum in (("max_tokens", 0), ("max_output_tokens", 1)):
            if field in prof:
                try:
                    out[field] = output_limit(prof[field], minimum)
                except ValueError:
                    pass
    # 整体来源按 **key** 算：没有 key，端点和模型名再齐也用不了。
    out["source"] = srcs.get("api_key") or (
        next(iter(srcs.values())) if srcs else "none"
    )
    return out


def probe(
    base_url: str = "",
    api_key: str = "",
    pid: str = "",
    role: str = "main",
    timeout: float = 15.0,
) -> dict:
    """问端点「你有哪些模型」。**只回名字，绝不回 key。**

    为什么要这个（2026-10-27 用户：「像 workbuddy 那样有清单可以选」）
        让人手填模型名，等于让他去猜对方的命名（`deepseek-chat` / `deepseek-flash` /
        `deepseek-v4-pro` 都是合法的，填错了要到第一次真跑才发现，
        而那时候报的是 404，看着像端点写错了）。
        清单是端点自己给的，比任何内置表都准 —— 也不会像内置价格表那样过期。

    ★ 不给 `pid` 就用"当前生效的那一套"（档案 > 环境变量 > credentials.env），
      所以"新增档案"时也可以先拉一次再挑。
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urlsplit

    d = load()
    prof = next((x for x in d["profiles"] if x.get("id") == pid), None) if pid else None
    if pid and prof is None:
        return dict(ok=False, models=[], why="待查询档案已经不存在，请刷新后重选")
    original_base = str((prof or {}).get("base_url") or "").strip().rstrip("/")
    base = str(base_url or original_base).strip().rstrip("/")
    key = api_key
    src = (prof or {}).get("name") or ""
    if prof and not key and base == original_base:
        key = str(prof.get("api_key") or "")
    elif prof and not key:
        return dict(ok=False, models=[], why="API 地址已改变，请为新地址重新填写 Key；未发送原档案密钥")
    if not prof and not base_url and not api_key:
        r = resolve(role)
        base = str(r.get("base_url") or "").strip().rstrip("/")
        key = r.get("api_key") or ""
        src = src or "当前生效的那套"
    if not (base and key):
        return {
            "ok": False,
            "models": [],
            "why": "还没有端点和 key —— 先填上（或在「档案」里存一份）再拉",
        }

    try:
        parsed = urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(ord(c) < 33 for c in base) or "\\" in base):
            raise ValueError()
        parsed.port
    except ValueError:
        return dict(ok=False, models=[], why="请填写不含账号、查询参数或片段的 HTTP(S) API 地址")
    if any(ord(c) < 32 for c in key):
        return dict(ok=False, models=[], why="Key 包含无效控制字符，未发送请求")

    src = public_name(src, base)

    def safe_reason(value):
        from .evidence_redaction import redact

        text = str(value).replace(base, public_url(base))
        return redact(text.replace(key, "«凭据已遮»") if key else text)

    src = safe_reason(src)

    req = urllib.request.Request(
        base + "/models", headers=request_headers(base, key)
    )
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        # ★ 和 provider / mcp 用**同一个出口**（默认直连）——
        #   否则"拉取可用模型"会被同一个会失效的系统代理拖住。
        from . import netpath as _NP

        opener = _NP.opener(handlers=(NoRedirect(),))
        with opener.open(req, timeout=timeout) as r:
            payload = r.read(1024 * 1024 + 1)
            if len(payload) > 1024 * 1024:
                return {
                    "ok": False,
                    "models": [],
                    "source": src,
                    "why": "模型清单响应超过1MiB容量，未载入或保存",
                }
            raw = payload.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # Error bodies may echo credentials; do not pass provider text to UI.
        e.close()
        return {
            "ok": False,
            "models": [],
            "source": src,
            "why": ("模型接口发生重定向，未跟随或转发密钥；请核对最终 API 地址"
                    if 300 <= e.code < 400 else
                    ("端点返回 HTTP %s，请核对 Key、地址、账户权限或稍后重试" % e.code)
                    + ("；该服务可能未提供模型目录，可查阅官方说明并手填模型 ID" if e.code in (404, 405, 501) else "")),
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            "ok": False,
            "models": [],
            "source": src,
            "why": "连不上这个端点：%s" % safe_reason(e),
        }

    try:
        j = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "models": [],
            "source": src,
            "why": "端点未返回有效JSON，原配置未修改",
        }
    items = j.get("data") if isinstance(j, dict) else j
    if not isinstance(items, list):
        return {
            "ok": False,
            "models": [],
            "source": src,
            "why": "模型列表格式应为数组，原配置未修改",
        }
    names = set()
    omitted = 0
    for it in items:
        nm = it.get("id") if isinstance(it, dict) else str(it)
        if (
            not isinstance(nm, str)
            or not nm
            or len(nm) > 256
            or key in nm
            or any(ord(c) < 32 for c in nm)
        ):
            omitted += 1
            continue
        if nm not in names and len(names) >= 1000:
            omitted += 1
            continue
        names.add(nm)
    names = sorted(names)
    if not names:
        return {
            "ok": False,
            "models": [],
            "source": src,
            "why": "端点没回任何模型名（可能这个网关不支持 /models）",
        }
    return {
        "ok": True,
        "models": names,
        "source": src,
        "count": len(names),
        "base_url": safe_reason(public_url(base)),
        "list_url": safe_reason(public_url(base + "/models")),
        "fetched_at": time.time(),
        "provider": model_source(base),
        "availability": "端点返回的目录；账户权限、余额及调用能力尚未逐个验证",
        "has_more": bool(isinstance(j, dict) and (j.get("has_more") or j.get("next_page_token"))),
        "omitted": omitted,
        "note": "列表最多1000项，忽略%d项超限或无效标识；原配置未修改" % omitted
        if omitted
        else "已读取模型列表，未更改配置",
    }


def _creds() -> dict:
    """credentials.env 的 `K=V`（复用 provider 那套读法，别写第二份）。"""
    from .provider import OpenAICompatProvider as P

    return P._read_creds()


def describe() -> str:
    """一行（进启动日志）。"""
    p = public()
    e = p["effective"]
    return "模型：自我 %s（%s）｜ 本我 %s（%s）｜ 档案 %d 份" % (
        e["main"]["model"] or "没配",
        e["main"]["source_cn"],
        e["review"]["model"] or "没配",
        e["review"]["source_cn"],
        len(p["profiles"]),
    )
