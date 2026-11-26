"""Durable queue state. The owning role coordinator serializes transitions."""

# 主要引用方包括 `role_coordination.py` 和 `background_planning.py`。

from copy import deepcopy

from . import evidence


class QueueConflict(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class WorkQueue:
    def __init__(self, state, goal):
        self.state = state.setdefault("workQueue", dict(
            revision=1, order=["goal"], pendingPlan=[],
            items={"goal": dict(id="goal", kind="feature", text=evidence.redact(goal)[:1800],
                                state="active", dependsOn=[])}))

    def _change(self):
        self.state["revision"] += 1

    def link_changes(self, item_id, change_ids):
        item = self.state["items"].get(item_id)
        if item:
            old = item.get("changeIds", [])
            current = list(dict.fromkeys([*old, *change_ids]))[-128:]
            if old != current:
                item["changeIds"] = current
                self._change()

    def read(self):
        items = self.state["items"]
        return dict(queueRevision=self.state["revision"], reason=self.state.get("reason"),
                    items=[dict(deepcopy({k: v for k, v in items[key].items() if k not in {"check", "sourcePath"}}), ready=all(
                        items[d]["state"] in {"implemented", "verified"} for d in items[key]["dependsOn"]))
                        for key in self.state["order"]])

    def propose(self, revision, order, reason, features, dependencies):
        if type(revision) is not int or revision != self.state["revision"]:
            raise QueueConflict("queue_conflict", "队列已有更新，请重新读取后再提交")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1200:
            raise QueueConflict("invalid_work_order", "请提供调序依据")
        items = deepcopy(self.state["items"])
        if not isinstance(features, list) or len(features) > 6:
            raise QueueConflict("invalid_work_order", "每次最多拆出六项功能需求")
        for row in features:
            key, title = (row.get("id"), row.get("text")) if isinstance(row, dict) else (None, None)
            if (not isinstance(key, str) or not key.startswith("feature-") or len(key) > 40
                    or not key[8:].isascii() or not key[8:].isalnum() or key in items
                    or not isinstance(title, str) or not title.strip() or len(title) > 800
                    or len(items) >= 48):
                raise QueueConflict("invalid_work_order", "功能项需要唯一的 feature- 前缀编号和简述")
            items[key] = dict(id=key, kind="feature", text=evidence.redact(title.strip()),
                              state="pending", dependsOn=[])
        if (not isinstance(order, list) or not all(isinstance(key, str) for key in order)
                or len(order) != len(items) or set(order) != set(items)):
            raise QueueConflict("invalid_work_order", "顺序必须恰好包含队列中的所有条目")
        if not isinstance(dependencies, dict):
            raise QueueConflict("invalid_work_order", "依赖必须按条目编号提供")
        position = {key: index for index, key in enumerate(order)}
        for key, needs in dependencies.items():
            if (key not in items or not isinstance(needs, list) or len(needs) > 12
                    or not all(isinstance(dep, str) and dep in items for dep in needs)
                    or len(set(needs)) != len(needs)):
                raise QueueConflict("invalid_work_order", "依赖必须引用已知且不重复的条目")
            items[key]["dependsOn"] = needs
        if any(position[dep] >= position[key] for key in order for dep in items[key]["dependsOn"]):
            raise QueueConflict("invalid_work_order", "前置工作必须在依赖它的工作之前")
        self.state.update(items=items, order=order, reason=evidence.redact(reason.strip()), pendingPlan=[])
        self._change()
        self.state["orderRevision"] = self.state["revision"]
        return self.read()

    def defect(self, candidate, action, text, signature, argv=None, *, code=None, material=None, existing_item_id=None):
        items = self.state["items"]
        if existing_item_id is not None:
            existing = items.get(existing_item_id) if isinstance(existing_item_id, str) else None
            if (not existing or existing["kind"] != "defect"
                    or existing["resourceId"] != candidate["resourceId"]
                    or existing["sourcePath"] != candidate["sourcePath"]
                    or existing["revision"] != candidate["revision"]):
                raise QueueConflict("invalid_defect_evidence", "只能追加同一候选的缺陷证据")
            receipts = existing.setdefault("additionalEvidence", [])
            if len(receipts) < 12 and not any(r["actionId"] == action for r in receipts):
                receipt = dict(actionId=action, text=text, kind="code" if code else "check")
                if code:
                    receipt["codeEvidence"] = code
                receipts.append(receipt)
                # Prefer a repeatable execution receipt when one is now available.
                if not code and existing.get("codeEvidence") and existing["state"] != "verified":
                    existing["check"] = signature
                    if argv is not None:
                        existing["argv"] = argv
                    if material is not None:
                        existing["material"] = material
                self._change()
            return existing_item_id, False
        existing = next((item for item in items.values()
                         if item["kind"] == "defect" and item["state"] != "verified"
                         and item["resourceId"] == candidate["resourceId"]
                         and item["sourcePath"] == candidate["sourcePath"]
                         and item["check"] == signature), None)
        if existing:
            return existing["id"], False
        key = f"defect-{1 + sum(item['kind'] == 'defect' for item in items.values())}"
        items[key] = dict(id=key, kind="defect", issueKind="product_defect", schemaVersion=1,
                          state="pending", text=text, dependsOn=[],
                          candidateId=candidate["id"], revision=candidate["revision"], actionId=action,
                          resourceId=candidate["resourceId"], sourcePath=candidate["sourcePath"], check=signature)
        if argv is not None:
            items[key]["argv"] = argv
        if material is not None:
            items[key]["material"] = material
        if code is not None:
            items[key]["codeEvidence"] = code
        self.state["order"].append(key)
        self.state["pendingPlan"].append(key)
        self._change()
        return key, True

    def applicable(self, candidate):
        return [item for item in self.state["items"].values()
                if item["kind"] == "defect" and item["state"] != "verified"
                and item["resourceId"] == candidate["resourceId"] and item["sourcePath"] == candidate["sourcePath"]]

    @staticmethod
    def retest_receipt(item, candidate, *, accept_checks=True):
        if receipt := candidate.get("codeVerifications", {}).get(item["id"]):
            return receipt
        receipt = candidate.get("checks", {}).get(item["check"], {})
        if (accept_checks and receipt.get("state") == "passed"
                and receipt.get("sourceVersion", "candidate") == "candidate"):
            return receipt
        return None

    def pending_retests(self, candidate):
        return [item for item in self.applicable(candidate)
                if item.get("lastObservedRevision", item["revision"]) != candidate["revision"]
                and not self.retest_receipt(item, candidate)]

    def retest(self, candidate, *, bind=False, accept_checks=True):
        changed = []
        for item in self.applicable(candidate):
            if item.get("lastObservedRevision", item["revision"]) == candidate["revision"]:
                continue
            receipt = self.retest_receipt(item, candidate, accept_checks=accept_checks)
            if receipt:
                item.update(state="verified", verifiedCandidateId=candidate["id"],
                            verifiedRevision=candidate["revision"], verifiedActionId=receipt["actionId"])
                if item.get('repair'):
                    item['repair']['state'] = 'closed'
            elif bind and item.get("reviewCandidateId") != candidate["id"]:
                item.update(state="awaiting_review", reviewCandidateId=candidate["id"])
            else:
                continue
            changed.append(item["id"])
        if changed:
            self._change()
        return changed

    def implemented(self, key):
        item = self.state["items"].get(key) if isinstance(key, str) else None
        if not item or item["kind"] != "feature" or key == "goal":
            raise QueueConflict("invalid_work_item", "只能标记队列中的功能子项")
        if item["state"] == "pending":
            if any(self.state["items"][dep]["state"] not in {"implemented", "verified"}
                   for dep in item["dependsOn"]):
                raise QueueConflict("work_dependencies_pending", "该功能项的前置工作尚未完成")
            item["state"] = "implemented"
            self._change()
        return item["state"]

    def finish(self):
        self.state["items"]["goal"]["state"] = "verified"
        self._change()
