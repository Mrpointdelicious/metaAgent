"""契约对齐的有限合成后端及显式冻结工具；不访问患者服务或数据库。"""

import hashlib
import json
import subprocess
from copy import deepcopy
from pathlib import Path

from tools.business_eval import PATIENT, ROOT, SyntheticBackend, envelope

FIXTURES = ROOT / "fixtures/contract_eval"
API_ROOT = Path("D:/Project/AI_WebApi")
SOURCE_FILES = [
    "AI_WebApi.Model/DTOs/AITools/PatientLayerDtos.cs",
    "AI_WebApi.Services/AITools/PatientLayerService.cs",
    "AI_WebApi.Tests/PatientLayerServiceTests.cs",
]


def page(count=1, number=1, size=5):
    return {"page_number": number, "page_size": size, "total_items": count, "has_next": False}


class ContractBackend(SyntheticBackend):
    async def artifact_available(self, url):
        return url == "https://images.example.test/contract-eval.png"

    async def post(self, name, payload):
        if payload.get("system_context", {}).get("user") != PATIENT:
            return await super().post(name, payload)
        if name not in {
            "get_patient_profile",
            "get_patient_consultation",
            "get_patient_rehab",
            "generate_iretour_single_session_report",
        } and self.fixture not in {"plan_only", "flag_conflict", "end_page"}:
            return await super().post(name, payload)
        self.calls.append({"name": name, "payload": deepcopy(payload)})
        if name == "get_patient_profile":
            data = {
                "patient_ref": f"project:{PATIENT}",
                "profile": {"age": 60},
                "source": "synthetic://profile",
                "limitations": ["合成非临床记录"],
            }
        elif name == "get_patient_consultation":
            prescriptions = (
                []
                if self.fixture != "rx_present"
                else [
                    {
                        "prescription_id": 801,
                        "name": "合成训练处方甲",
                        "indication": None,
                        "content": "每次按记录的两组练习执行；不得自行增加组数。",
                        "offline_training": None,
                        "online_evaluation": None,
                    }
                ]
            )
            if self.fixture == "rx_null":
                prescriptions = None  # Explicit robustness case, not normal DTO output.
            data = {
                "patient_ref": f"project:{PATIENT}",
                "page": page(),
                "medical_record_count": 1,
                "consultation_count": 0,
                "medical_records": [
                    {
                        "record_id": 701,
                        "recorded_at": "2026-10-08T10:00:00+08:00",
                        "medical_record": None,
                        "content": {"doctor_orders": "按既有计划训练，出现不适联系主管医生。"},
                        "summary": None,
                        "followup_time": None,
                        "followup_hospital": None,
                        "prescriptions": prescriptions,
                        "source": "synthetic://medical",
                    }
                ],
                "consultations": [],
                "limitations": ["仅本次合成查询范围，不能推断完整病史。"],
            }
            if self.fixture == "malformed":
                data["medical_records"] = "invalid-list-type"
        elif name == "get_patient_rehab":
            plans = (
                []
                if self.fixture == "training_only"
                else [
                    {
                        "plan_id": 601,
                        "created_at": "2026-10-07T09:00:00+08:00",
                        "scheduled_at": "2026-10-10T09:00:00+08:00",
                        "completion_flag": 1 if self.fixture == "flag_conflict" else None,
                        "status": None,
                        "details": None,
                        "source": "synthetic://plan",
                    }
                ]
            )
            history = await self._history(payload)
            data = {
                "patient_ref": f"project:{PATIENT}",
                "plan_count": len(plans),
                "plans": plans,
                "plan_page": page(len(plans)),
                "iretour": {
                    "availability": "success",
                    "history": history,
                    "reason_codes": [],
                    "patient_message": "合成设备历史",
                },
                "irego": {
                    "availability": "unavailable",
                    "history": None,
                    "reason_codes": ["IDENTITY_UNRESOLVED"],
                    "patient_message": "唯一身份映射未确认",
                },
                "limitations": ["计划完成标记不能替代实际训练结果，记录未提供计划与训练对应关系。"],
            }
        elif name == "generate_iretour_single_session_report":
            body = envelope(
                name,
                {
                    "artifact_ref": "synthetic-contract-report",
                    "image_url": "https://images.example.test/contract-eval.png",
                    "expires_at": "2099-01-01T00:00:00+00:00",
                },
            )
            return body
        elif name == "get_iretour_patient_history":
            data = await self._history(payload)
        elif name in {
            "get_iretour_session_analysis",
            "get_iretour_longitudinal_analysis",
        } and self.fixture in {"plan_only", "flag_conflict"}:
            return envelope(name, None, "unavailable", "本合成条件没有可读取的实际结果。")
        elif name == "get_iretour_patient_context" and self.fixture in {
            "plan_only",
            "flag_conflict",
        }:
            data = {"coverage": {"usable_results": 0}, "limitations": ["没有实际训练结果"]}
        else:
            self.calls.pop()
            return await super().post(name, payload)
        return envelope(name, data)

    async def _history(self, payload):
        if self.fixture in {"plan_only", "flag_conflict"}:
            return {"page": page(0), "items": []}
        original = SyntheticBackend(self.fixture)
        body = await original.post("get_iretour_patient_history", payload)
        if self.fixture == "end_page":
            body["data"]["page"] = page(2)
        return body["data"]


def build_manifest():
    original = json.loads((ROOT / "fixtures/business_eval/cases.json").read_text(encoding="utf-8"))
    forbidden = [
        "navigate_scene",
        "generate_irego_single_session_report",
        "generate_irego_longitudinal_report",
        "generate_iretour_single_session_report",
        "generate_iretour_longitudinal_report",
    ]

    def turn(query, tools=(), refs=(), knowledge="optional", **extra):
        return {
            "query": query,
            "required_patterns": [],
            "required_tools": list(tools),
            "required_refs": list(refs),
            "knowledge": knowledge,
            "forbidden_tools": forbidden.copy(),
            "allowed_statuses": ["succeeded", "partial"],
            **extra,
        }

    def case(cid, fixture, turns, **extra):
        return {
            "case_id": cid,
            "family": "contract_development",
            "fixture": fixture,
            "backend": "contract",
            "turns": turns,
            **extra,
        }

    clinical = "查询上次医嘱和处方，说明日期、本次返回范围和未提供的信息。"
    compare = "结合医嘱、康复计划与最近实际训练，说明能否核对是否按计划执行。"
    cases = [
        case(f"C{i:02}", f, [turn(clinical, ["get_patient_consultation"])])
        for i, f in enumerate(["rx_empty", "rx_present", "rx_null", "malformed"], 1)
    ]
    cases[-1]["turns"][0]["allowed_statuses"] = ["unavailable", "partial"]
    cases += [
        case(f"C{i:02}", f, [turn(compare, ["get_patient_consultation", "get_patient_rehab"])])
        for i, f in enumerate(["training_only", "plan_only", "unlinked", "flag_conflict"], 5)
    ]
    for cid, enabled in [("C09", False), ("C10", True)]:
        t = turn(
            "请为最近一次IReTour训练生成图片报告，若不能生成请说明具体原因。",
            ["get_iretour_session_analysis"],
        )
        if enabled:
            t["required_tools"].append("generate_iretour_single_session_report")
            t["forbidden_tools"].remove("generate_iretour_single_session_report")
            t["allow_artifact"] = True
        t["allowed_statuses"] += ["unsupported", "unavailable"]
        cases.append(case(cid, "measured", [t], reports_enabled=enabled))
    cases.append(
        case(
            "C11",
            "end_page",
            [
                turn("列出第一页IReTour有结果的历史记录。", ["get_iretour_patient_history"]),
                turn("继续下一页。", forbidden_backend=["get_iretour_patient_history"]),
            ],
        )
    )
    ref = ["tour-speed@v1#definition", "tour-speed@v1#limits"]
    cases.append(
        case(
            "C12",
            "measured",
            [
                turn("依据合成指标资料说明步速定义及限制。", refs=ref, knowledge="required"),
                turn("刚才资料的定义和限制再说明一下。", refs=ref, knowledge="reuse"),
                turn(
                    "再查刚才资料的定义与限制。",
                    refs=ref,
                    knowledge="required",
                    transition="expire_knowledge",
                ),
            ],
            patient=False,
        )
    )
    help_ref = ["result-help@v1#view"]
    cases.append(
        case(
            "C13",
            "measured",
            [
                turn(
                    "依据合成帮助说明怎样查看训练结果，不要生成图片。",
                    refs=help_ref,
                    knowledge="required",
                )
            ],
            patient=False,
        )
    )
    cases.append(
        case(
            "C14",
            "measured",
            [
                turn(
                    "先检索product领域的合成结果查看说明；若没命中，再检索help领域并依据资料说明操作。",
                    refs=help_ref,
                    knowledge="required",
                )
            ],
            patient=False,
        )
    )
    cases.append(
        case(
            "C15",
            "measured",
            [
                turn(
                    "依据资料比较合成方案甲乙的人群、干预和主要结局，未返回的维度明确说明，不能猜测。",
                    refs=[
                        "protocol-a@v1#population",
                        "protocol-a@v1#intervention",
                        "protocol-a@v1#outcomes",
                        "protocol-b@v1#population",
                        "protocol-b@v1#intervention",
                    ],
                    knowledge="required",
                )
            ],
            patient=False,
            corpus="partial_corpus.json",
        )
    )
    cases.append(
        case(
            "C16",
            "measured",
            [
                turn(
                    "依据合成帮助说明怎样查看训练结果，说明依据是否可获取，不要猜测操作。",
                    knowledge="unnecessary",
                )
            ],
            patient=False,
            disable_knowledge=True,
        )
    )
    for c in original["cases"]:
        c["backend"] = "contract"
        c["family"] = "contract_aligned_regression"
    return {
        **{k: v for k, v in original.items() if k != "cases"},
        "fixture_version": "contract-fixtures-v1",
        "cases": cases + original["cases"],
        "not_blind_test": True,
        "deployment_version_verified": False,
    }


def freeze():
    if FIXTURES.exists():
        raise SystemExit("Fixture directory already exists; never overwrite frozen inputs")
    FIXTURES.mkdir()
    corpus = json.loads((ROOT / "fixtures/business_eval/corpus.json").read_text(encoding="utf-8"))
    partial = deepcopy(corpus)
    for doc in partial["documents"]:
        if doc["doc_id"] == "protocol-b":
            doc["sections"] = [s for s in doc["sections"] if s["section_id"] != "outcomes"]
    source = {
        "repository_commit": subprocess.check_output(
            ["git", "-C", str(API_ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {n: hashlib.sha256((API_ROOT / n).read_bytes()).hexdigest() for n in SOURCE_FILES},
        "meaning": "local source snapshot only, not deployment or clinical approval",
    }
    files = {
        "cases.json": build_manifest(),
        "corpus.json": corpus,
        "partial_corpus.json": partial,
        "source-pins.json": source,
    }
    for name, value in files.items():
        (FIXTURES / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    pins = {n: hashlib.sha256((FIXTURES / n).read_bytes()).hexdigest() for n in files}
    (FIXTURES / "frozen.sha256.json").write_text(json.dumps(pins, indent=2), encoding="utf-8")
    print("Frozen 16 development conditions and 12 contract-aligned regression cases.")


if __name__ == "__main__":
    freeze()
