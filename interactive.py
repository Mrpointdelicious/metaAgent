"""
创建日期：2026-10-09
文件功能：交互式命令行客户端 —— 连接已运行的 MetaAgent 服务，逐轮提问并打印回答。

这是 HTTP 服务之外的「交互式版本」：不改服务逻辑，只在终端里逐条收发 /v1/chat。
只依赖 Python 标准库，宿主机的任意 Python 3 都可运行。

用法：
    python interactive.py                    # 启动后按提示输入患者标识
    python interactive.py -i 5718            # 直接用项目患者 ID 5718
    python interactive.py -i phone:15932910402   # 用手机号（网关会解析成项目患者ID）
    python interactive.py -v                 # 额外显示工具/证据等指标
    python interactive.py -k 4985b6d... --apikey ... --apitoken ...   # 企业知识库检索
    python interactive.py --pos "问诊 房间"                          # 走 consultation_room 分支
    python interactive.py --patientphone 15932910402 --requesterphone 15932910402   # 病历填写分支用

对话中的命令：
    exit / quit / q / 退出    退出
    new / reset / 新会话       清空会话，开始新对话
    id / 患者                 重新设置患者标识
    kb / 知识库               设置企业知识库 richId
    phone / 患者手机          设置患者手机号（inputs.patientphone）
    rphone / 请求手机         设置请求手机号（inputs.requesterphone）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Windows 控制台默认 GBK，统一成 UTF-8 并容错，避免中文乱码与代理字符报错。
for _stream in (sys.stdin, sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

DEFAULT_URL = "http://localhost:8080"
FALLBACK_TOKEN = "dev-service-token-2026"


def _clean(text: str) -> str:
    # 防御性清理：万一 stdin 仍带入孤立代理字符，编码前替换掉。
    return text.encode("utf-8", "replace").decode("utf-8", "replace")


def load_service_token() -> str:
    env = os.environ.get("META_AGENT_SERVICE_TOKEN")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for path in (".env", os.path.join(here, ".env")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("META_AGENT__SERVICE_BEARER_TOKEN="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            continue
    return FALLBACK_TOKEN


def classify_identity(raw: str) -> dict:
    raw = (raw or "").strip()
    if not raw:
        return {}
    if ":" in raw:
        kind, _, val = raw.partition(":")
        kind, val = kind.strip().lower(), val.strip()
        if kind in ("phone", "p", "手机"):
            return {"patientphone": val}
        if kind in ("id", "pid", "project", "项目"):
            return {"projectPatientId": val}
    if raw.isdigit():
        return {"patientphone": raw} if len(raw) == 11 else {"projectPatientId": raw}
    return {"patientphone": raw}


def with_space(inputs: dict, space_id: str) -> dict:
    if space_id:
        inputs = dict(inputs or {})
        inputs["space_id"] = space_id
    return inputs


def with_pos(inputs: dict, pos: str) -> dict:
    if pos:
        inputs = dict(inputs or {})
        inputs["pos"] = pos
    return inputs


def with_kb(inputs: dict, apikey: str, apitoken: str, companykb: str) -> dict:
    if apikey or apitoken or companykb:
        inputs = dict(inputs or {})
        if apikey:
            inputs["apikey"] = apikey
        if apitoken:
            inputs["apitoken"] = apitoken
        if companykb:
            inputs["companykb"] = companykb
    return inputs


def with_phones(inputs: dict, patientphone: str, requesterphone: str) -> dict:
    if patientphone or requesterphone:
        inputs = dict(inputs or {})
        if patientphone:
            inputs["patientphone"] = patientphone
        if requesterphone:
            inputs["requesterphone"] = requesterphone
    return inputs


def build_inputs(identity_raw: str, args) -> dict:
    inputs = with_space(
        classify_identity(identity_raw) if identity_raw is not None else None, args.space
    )
    inputs = with_pos(inputs, args.pos)
    inputs = with_phones(inputs, args.patientphone, args.requesterphone)
    return with_kb(inputs, args.apikey, args.apitoken, args.companykb)


def _error_text(body: str) -> str:
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            return parsed.get("message") or parsed.get("detail") or body
    except (ValueError, TypeError):
        pass
    return body


def post_chat(url, token, query, inputs, conversation_id):
    payload = {
        "user": "interactive-cli",
        "inputs": inputs,
        "query": _clean(query),
        "conversation_id": conversation_id,
        "response_mode": "blocking",
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/v1/chat",
        data=data,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": _error_text(exc.read().decode("utf-8", "replace"))}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}


def fetch_status(url, token, task_id, user, inputs):
    req = urllib.request.Request(
        url.rstrip("/") + f"/v1/runs/{task_id}/status",
        data=json.dumps({"user": user, "inputs": inputs}, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": _error_text(exc.read().decode("utf-8", "replace"))}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}


def extract_answers(resp):
    answer = resp.get("answer")
    return [answer] if answer else []


def render_metrics(snapshot):
    m = snapshot.get("metrics") or {}
    lines = [
        f"  状态: {snapshot.get('status')}",
        f"  模式: {m.get('orchestration_mode') or m.get('branch')}  工具调用: {m.get('tool_calls')}  LLM调用: {m.get('llm_calls')}",
    ]
    for step in m.get("agent_model_steps") or []:
        names = [t.get("name") for t in step.get("tools", [])] or ["(无工具)"]
        lines.append(f"  第{step.get('call')}轮LLM工具: {', '.join(names)}")
    pc = m.get("patient_profile_context")
    if pc:
        lines.append(
            f"  患者档案: {pc.get('status')}  {pc.get('data', {}).get('patient_ref')}"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="MetaAgent 交互式命令行客户端")
    ap.add_argument("url", nargs="?", default=DEFAULT_URL,
                    help="服务地址，默认 http://localhost:8080（Docker 版用 http://localhost:5080）")
    ap.add_argument("-i", "--identity", help="患者标识：5718 或 phone:15932910402")
    ap.add_argument("-s", "--space", help="场景 space_id，塞进 inputs.space_id（用于指令解析工具）")
    ap.add_argument("--pos", help="前端透传的 pos 字段；含「问诊」+「房间」时走 consultation_room 分支")
    ap.add_argument("-k", "--companykb", help="企业知识库 richId，塞进 inputs.companykb（用于知识库检索工具）")
    ap.add_argument("--apikey", help="知识库接口 apikey 头，塞进 inputs.apikey")
    ap.add_argument("--apitoken", help="知识库接口 apitoken 头，塞进 inputs.apitoken")
    ap.add_argument("--patientphone", help="患者手机号，塞进 inputs.patientphone（病历填写/角色查询用）")
    ap.add_argument("--requesterphone", help="请求手机号，塞进 inputs.requesterphone（对话记录查询用）")
    ap.add_argument("-t", "--token",
                    help="服务 Bearer Token（默认读 .env 的 META_AGENT__SERVICE_BEARER_TOKEN）")
    ap.add_argument("-v", "--verbose", action="store_true", help="额外显示工具/证据指标")
    args = ap.parse_args()

    token = args.token or load_service_token()
    inputs = build_inputs(args.identity, args)

    print("MetaAgent 交互式客户端")
    print(f"  服务: {args.url}")
    print(f"  患者: {json.dumps(inputs, ensure_ascii=False) or '（未设置）'}")
    print("  输入问题后回车；exit 退出，new 开新会话，id 改患者，phone/rphone 改手机号。\n")

    if inputs is None:
        raw = input("请输入患者标识（项目ID 如 5718，或手机号如 15932910402，直接回车跳过）: ")
        inputs = build_inputs(raw, args)

    conversation_id = ""
    while True:
        try:
            query = input("你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        low = query.lower()
        if low in ("exit", "quit", "q", "退出"):
            break
        if low in ("new", "reset", "新会话"):
            conversation_id = ""
            print("已开启新会话。")
            continue
        if low in ("id", "患者"):
            raw = input("请输入患者标识: ").strip()
            inputs = build_inputs(raw, args)
            print(f"患者已设为: {json.dumps(inputs, ensure_ascii=False) or '（未设置）'}")
            continue
        if low in ("kb", "知识库"):
            kb = input("请输入企业知识库 richId: ").strip()
            args.companykb = kb
            inputs = with_kb(inputs, args.apikey, args.apitoken, kb)
            print(f"知识库已设为: {json.dumps(inputs, ensure_ascii=False) or '（未设置）'}")
            continue
        if low in ("phone", "患者手机"):
            phone = input("请输入患者手机号: ").strip()
            args.patientphone = phone
            inputs = with_phones(inputs, phone, args.requesterphone)
            print(f"患者手机号已设为: {phone or '（未设置）'}")
            continue
        if low in ("rphone", "请求手机"):
            phone = input("请输入请求手机号: ").strip()
            args.requesterphone = phone
            inputs = with_phones(inputs, args.patientphone, phone)
            print(f"请求手机号已设为: {phone or '（未设置）'}")
            continue
        if not query:
            continue
        print("思考中…", flush=True)
        status, resp = post_chat(
            url=args.url, token=token, query=query, inputs=inputs,
            conversation_id=conversation_id,
        )
        if status != 200 or "error" in resp:
            print(f"[错误] HTTP {status}: {resp.get('error')}")
            continue
        conversation_id = resp.get("conversation_id") or conversation_id
        answers = extract_answers(resp)
        if answers:
            print("答 > " + answers[-1])
        else:
            print(f"[无回答] 事件: {resp.get('event')}  task_id: {resp.get('task_id')}")
        if args.verbose:
            task_id = resp.get("task_id")
            if task_id:
                _, snapshot = fetch_status(args.url, token, task_id, "interactive-cli", inputs)
                if "error" in snapshot:
                    print(f"  [指标不可用] {snapshot.get('error')}")
                else:
                    print(render_metrics(snapshot))
            else:
                print("  [无 task_id，无法查询指标]")
        print()


if __name__ == "__main__":
    main()
