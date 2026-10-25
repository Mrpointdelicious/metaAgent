"""
创建日期：2026-10-23
文件功能：用固定问题集记录真实检索分数，校准并验证分库相关性策略。
"""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / ".local/rag-upgrade"


def cases():
    catalog = json.loads(
        (ROOT / "src/meta_agent/domains/space_catalog.json").read_text(encoding="utf-8")
    )
    rooms = list(next(iter(catalog.values()))["scenes"])
    hospital = [f"岳阳医院的{room}在哪里，有什么功能？" for room in rooms[:25]]
    medical = [
        "脑卒中立位平衡障碍有哪些评定方法？",
        "Berg平衡量表的项目和评分范围是什么？",
        "PASS量表如何评估脑卒中患者的姿势控制？",
        "Tinetti量表包含哪些评估内容？",
        "脑卒中后站立平衡训练有哪些方法？",
        "脑卒中康复指南如何建议步行训练？",
        "机器人辅助步态训练对脑卒中患者的平衡有什么影响？",
        "可穿戴外骨骼对卒中后步态康复的证据有哪些？",
        "膝骨关节炎康复治疗指南如何建议运动治疗？",
        "膝骨关节炎患者的肌力训练有哪些建议？",
        "卒中康复中的任务导向训练有哪些推荐？",
        "智能康复技术在脑卒中治疗中有哪些应用？",
        "脑卒中中西医结合康复指南包含哪些康复方法？",
        "卒中后步行障碍的机器人辅助训练证据是否充分？",
        "脑卒中后平衡评估时BBS和PASS有什么区别？",
        "膝骨关节炎康复如何开展患者教育？",
        "外骨骼训练改善卒中后步行速度的研究结论是什么？",
        "脑卒中康复治疗指南如何看待早期康复？",
        "脑卒中后平衡训练与跌倒风险评估的相关依据是什么？",
        "机器人步态训练改善卒中平衡的系统综述结论是什么？",
        "脑卒中智能康复共识如何看待可穿戴设备？",
        "膝骨关节炎康复运动中的有氧训练有哪些依据？",
        "立位平衡障碍临床评估包含哪些常用量表？",
        "脑卒中康复中步态训练的研究局限有哪些？",
        "卒中后姿势稳定性和行走能力的康复证据有哪些？",
    ]
    unrelated = [
        "量子计算机的纠错阈值是多少？",
        "如何证明黎曼猜想？",
        "今天比特币实时价格是多少？",
        "明天上海天气如何？",
        "Python怎样实现快速排序？",
        "如何配置Kubernetes集群？",
        "宇宙微波背景辐射是什么？",
        "如何烘焙一只巧克力蛋糕？",
        "法国大革命发生在哪一年？",
        "怎样给汽车更换机油？",
        "今年世界杯谁会夺冠？",
        "怎样计算房屋贷款利息？",
        "如何学习古典吉他和弦？",
        "上海到巴黎的最新机票价格是多少？",
        "如何制作一份公司财务报表？",
        "太阳系有多少颗行星？",
        "怎样设计数据库的B树索引？",
        "如何申请日本旅游签证？",
        "怎么搭建一个电子商务网站？",
        "红烧牛肉应该放哪些调料？",
        "生成一首关于月亮的诗。",
        "如何使用Photoshop替换照片背景？",
        "钢筋混凝土桥梁如何设计？",
        "帮我预测下周股票价格。",
        "圆周率的前一百位是多少？",
    ]
    result = []
    for domain, positives in [("help", hospital), ("health", medical)]:
        for positive, queries in [(True, positives), (False, unrelated)]:
            for index, query in enumerate(queries):
                result.append(
                    {
                        "id": f"{domain}-{'positive' if positive else 'negative'}-{index:02}",
                        "domain": domain,
                        "query": query,
                        "relevant": positive,
                        "split": "validation" if index % 5 == 0 else "calibration",
                    }
                )
    return result


REMOTE = r"""
import asyncio,json,sys,time
import httpx
from meta_agent.config import Settings
args=json.load(sys.stdin)
async def main():
 s=Settings(); sem=asyncio.Semaphore(3)
 # Historical space mapping is used only to discover the already-authorized KBs
 # during the baseline; health is configured globally by the deployment.
 hospital=next(iter(s.portfolio_space_kb_ids.values()))
 async with httpx.AsyncClient(timeout=90,trust_env=False) as c:
  async def run(case):
   async with sem:
    domain=case['domain']
    token=(s.portfolio_help_token if domain=='help' else s.portfolio_token).get_secret_value()
    ids=(s.portfolio_kb_ids or hospital['health']) if domain=='health' else hospital['help']
    for attempt in range(2):
     try:
      r=await c.post(s.portfolio_base_url+'/v1/retrieve',
        headers={'Authorization':'Bearer '+token},
        json={'kb_ids':ids,'queries':[{'q':case['query']}],'strategy':'hybrid',
          'candidate_k':40,'top_k':5,'max_context_tokens':4000,
          'rerank':True,'answer_mode':'evidence'})
      r.raise_for_status(); body=r.json()
      row={**case,'trace_id':body.get('trace_id'),'notes':body.get('notes',[]),'decision':body.get('decision'),
        'scores':[e['score'] for e in body['evidence']],
        'sources':[e['title'] for e in body['evidence']]}
      print('ROW='+json.dumps(row,ensure_ascii=True),flush=True);return
     except Exception as e:
      if attempt: print('ROW='+json.dumps({**case,'error':type(e).__name__}),flush=True)
      else: await asyncio.sleep(1)
  await asyncio.gather(*(run(case) for case in args['cases']))
asyncio.run(main())
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="baseline")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    dataset = cases()
    (OUTPUT / "cases.json").write_text(
        json.dumps(dataset, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    process = subprocess.Popen(
        ["docker", "exec", "-i", "meta_agent", "python", "-c", REMOTE],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
    )
    process.stdin.write(json.dumps({"cases": dataset[: args.limit] if args.limit else dataset}))
    process.stdin.close()
    rows = []
    with (OUTPUT / f"{args.name}.jsonl").open("w", encoding="utf-8") as stream:
        for line in process.stdout:
            if line.startswith("ROW="):
                row = json.loads(line[4:])
                rows.append(row)
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                stream.flush()
                print(
                    json.dumps(
                        {"completed": len(rows), "case": row["id"], "error": row.get("error")}
                    ),
                    flush=True,
                )
    if process.wait():
        raise RuntimeError("Evaluation process failed; inspect local output")


if __name__ == "__main__":
    main()
