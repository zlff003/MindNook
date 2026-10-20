"""Opt-in live Memory evaluation. See docs/MEMORY_LIVE_TEST_PLAN.md.

Run: python scripts/memory_live_eval.py --live
Never imported by pytest; never accesses the business database.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select
from app.agents.prompts import RESPONSE_SYSTEM_PROMPT
from app.agents.runtime import RuntimeInput
from app.core.config import Settings
from app.db import Base, build_engine, build_session_factory
from app.memory.index import MemoryIndex
from app.memory.repository import memory_dict
from app.memory.service import MemoryService
from app.memory.store import InMemoryRedisStore
from app.models import Conversation, ConversationSummary, Message, ToolJob, User, UserMemory
from app.services.model import ModelMessage, build_model_provider


# Fixed, fictional history. The expected facts are never sent to the answering model.
STUDENT_TURNS = [
    "我最近为三周后的统计学考试担心，晚上躺下就反复想没复习完的章节，通常拖到凌晨两点才睡，早上八点还得上课。白天不是完全不能学习，而是下午上课容易走神，我想先把这段情况说清楚。",
    "考试最让我紧张的是概率题，不是所有课程都学不进去。昨天在图书馆坐了两个小时，完成了条件概率的一小节；只要开始做具体题目，我就比反复看复习目录踏实些。",
    "有一个长期的交流偏好想告诉你：我一直希望你每次只问一个问题，回复尽量简短。连续收到很多建议时，我反而不知道先回答哪件事，希望之后聊别的主题也保持这个节奏。",
    "我一般晚上十一点回宿舍，洗漱后会拿起手机看考试群。群里有人说已经开始第二轮复习，我就担心进度落后，有时候关掉群聊又忍不住点开，这比题目本身更让我难放松。",
    "你刚才提到的呼吸练习，我愿意之后了解一下，但目前只是在考虑，还没有开始做。现在我更想知道，怎么把明天的复习任务限定得小一些，不要一打开本子就感觉全都做不完。",
    "我把明天的目标缩成两道例题和一页错题整理，不准备追赶其他人的章节数。下午有实验课，所以真正能自己安排的时间只有晚饭后的一个小时，我想按这个实际时间排任务。",
    "昨天做题时，我发现自己不是所有步骤都不会，主要卡在判断事件是否独立。把卡住的步骤单独圈出来后，任务看起来具体多了，但一到准备睡觉时，担心仍然会冒出来。",
    "我暂时不想让你给一大张时间表，先陪我把概率这一章理清楚就行。早上八点的课还会继续上，下午走神的问题也没有消失，只是今天完成例题时稍微有一点把握。",
    "我试过睡前在纸上列三项待办，连续三晚有帮助，躺下后不再一直盘算第二天的任务。不过我没有记录准确入睡时间，所以只能说主观上轻松一些，不能说已经恢复正常睡眠。",
    "昨天的三项任务里只完成了两项，剩下一项挪到今天。我没有再额外加五六项补偿任务，晚饭也正常吃了。现在最想保持的是有界限地复习，不要因为一次没完成就否定整天。",
    "今天去听了统计学的习题课，老师说先辨认条件再写公式，我把这句话记在错题本扉页。考试日期没有变化，我对概率题仍有担心，睡眠的问题也还在观察，没有说已经解决。",
    "这轮先停在这里：主要问题仍是统计学考试带来的睡前反复思考。下午上课容易分心，但我还能完成小任务。现在不用再增加新的练习，先按我们已经聊过的情况往后观察。",
    "过了一周，这几天实验报告和复习挤到一起，我的睡前担心又明显了。以前可以把任务放下，现在关灯以后还会在脑子里排顺序，下午那种疲惫感也又回来了。",
    "我开始在晚饭后到操场散步十五分钟，不追求步数，也不边走边背书。昨天走完回宿舍时，身体没有一直绷着，后来坐下来整理错题时也比较容易集中注意力。",
    "操场那条路线人比较多，但不会像自习室那样让我总想和别人比进度。我只是绕外圈走，回去以后照常洗漱，没有把散步当成必须打卡完成的新任务。",
    "这两天复习依然有卡住的地方，我准备在助教答疑时间问独立事件那一题。上一次我没有开口，主要担心问题太基础；这次已经把具体卡住的步骤标好了，问起来应该容易一些。",
    "昨天去找助教后，才发现我把两个条件混在一起了。解决的是那一道题的理解，不代表以后再也不会紧张。我愿意继续一次处理一个具体问题，这样比笼统要求自己更努力更清楚。",
    "舍友最近开始晚上连麦打游戏，声音有时持续到很晚。我先把这个情况记在这里，今天还想继续说学习，不打算马上谈关系处理，也没有和舍友发生争吵。",
    "关于晚饭后散步，我已经连续四天做了，确实有帮助，主要是回来以后身体没那么紧、思绪也慢一点。但入睡时间有波动，所以我不想把它说成已经解决失眠。",
    "今天我把一套练习拆成三个部分，先完成了最容易的一部分。中途休息没有刷考试群，下午还是觉得困，不过没有因为困就取消所有学习安排，也没有突然把目标翻倍。",
    "最近考试群又出现了新的资料包，我没有全部下载。之前那本错题本还没有整理完，我打算先把已经看过的内容用起来；额外材料太多时，我容易忘记自己原本要解决什么。",
    "今天只想说说这一周的变化，不要给我新的建议。这只是今天比较累，想有人听一下，不是说我以后都拒绝建议。我们平时一次讨论一个问题的方式，我还是希望继续保留。",
    "列待办以前有效，现在没用了。这一周任务一多，我看见清单反而更着急，睡前又开始在脑子里核对遗漏。散步对放松身体仍有帮助，这两个办法的当前感受不一样。",
    "这一阶段最明显的是任务拥挤后，我对清单的感觉变了。考试还没结束，下午疲惫也仍然存在。暂时不要把任何一个办法写成完全解决问题，我说的都是这一段时间的具体体验。",
    "我需要更正一件事：之前说错了，列待办其实没试过。前面关于连续三晚有效、后来无效的记录都是同学的，我转述时混成了自己的经历。请不要再把列待办的执行和效果记在我身上；散步是我自己做过的。",
    "接下来想聊宿舍沟通。舍友晚上连麦的声音一般到十二点半，我戴耳塞也还能听到。我担心开口会让关系变僵，所以这几天只是自己忍着，没有正式和他说过安静时间。",
    "我不是想让舍友完全不能玩游戏，只希望到晚上十一点以后用耳机、说话小声些。白天我们相处还正常，也会一起吃饭；这件事主要卡在怎么开口，而不是双方已经闹翻。",
    "我在想先约一个白天都不赶时间的空档，讲具体的声音和睡觉时间，不评价他这个人。我只是准备这样说，还没实际约到时间，也不知道他会怎么回应。",
    "昨天他问我为什么最近总戴耳塞，我只说晚上有点吵，没有继续说明十一点的想法。我回来以后有点懊恼，但至少这次没有直接指责，也不想把这一句简短交流算作事情已经谈好。",
    "我写了一句准备说的话：十一点以后我想休息，能不能戴耳机并降低说话声音。写下来之后感觉需求清楚些，不过我还没把这句话当面说出来，我们的约定当然也还没有形成。",
    "今天在食堂和舍友一起吃饭，聊的都是下午课程，没有提晚上的声音。我注意到关系没有我想得那么紧张，只是我一想到提出要求，就担心对方觉得我管得太多。",
    "我准备周六下午和他单独聊一会儿，因为那时我们都不用赶课。如果他不愿意，我还需要再想下一步，但目前没有结果，请先保留这件事还没谈妥的状态。",
    "除了声音问题，我们宿舍公共桌面也有点乱，但我不想一次把所有不满都倒出来。先只讨论十一点后的音量，等这件事清楚了，再决定是否需要说公共空间的安排。",
    "今天我练习把请求说得短一些，没有加你总是这样之类的话。练习只是我自己对着纸念，并不代表舍友已经听过，更不能推断他已经同意了我的要求。",
    "周六还没到，这两天晚上的声音情况差不多。我没有因为我们白天还能正常聊天就认为睡眠影响已经消失，考试担忧和宿舍声音可能都在影响我，但它们是不同的问题。",
    "宿舍这部分先说到这里：我希望十一点后安静，准备周六白天沟通，目前尚未形成约定。之前的考试睡眠问题也没有正式结束，只是这会儿我们把注意力转到关系上了。",
    "现在又有一个课程小组的事情。我们四个人做数据分析展示，我负责清洗问卷数据，另一位同学负责图表。分工写得太笼统，昨天两个人都改了同一列，导致合并时有冲突。",
    "这不是谁故意拖延，而是我们没有约定原始数据放哪里、哪些列能修改。我想先把自己的清洗步骤写清楚，再和负责图表的同学确认输入格式，避免下次继续相互覆盖。",
    "老师要求下周三展示，我已经完成缺失值标记，但还没统一日期格式。我没有承诺今晚把全部数据处理完，打算先完成日期这一项，再让同伴看看能不能直接用来画图。",
    "今天我和图表同学确认了文件名，他希望拿到一个固定版本再开始画。我答应先发样本，不发尚未检查的全部结果。这只是我们这次小组任务的约定，不是我对所有合作的固定偏好。",
    "我把清洗步骤写成了四行备注，说明删除了哪些空白行、保留了哪些异常值。以前我只交结果文件，现在多写这些说明，主要是想让同伴能复查，不是为了证明自己比别人负责。",
    "今天下午小组讨论没有争吵，但我有几次想一次解释所有细节。后来先把日期格式的问题讲完，再说下一项，大家反而比较容易接上；这让我觉得之前逐条讨论的方式也适合这里。",
    "展示里的配色还没有决定，这件事由负责图表的同学处理。我只需要确认数字和分类一致，不准备接管所有版式，不然我的复习时间又会被挤掉。",
    "有一个临时变化：组长希望明天先合并一个演示版本，最终稿仍然是下周三。我准备交已经检查好的部分，明确剩下哪些还没完成，避免大家把演示版当成最终数据。",
    "今晚我完成了日期格式统一，给同伴发了样本文件，他说可以读取。问卷分类里还有两个标签需要确认，我已经标出来，不能为了赶合并时间就随便归到同一类。",
    "明天中午我们会用十分钟确认那两个分类，不需要再开一次很长的会。我在群里只发了两个具体问题，没有要求大家马上回复，也没把对方暂时没回当成对我不满意。",
    "这几天课程展示占了不少精力，我的考试复习只保留最基本的部分。这样的调整不代表考试不重要，也不是宣布睡眠问题已经好了，只是暂时把有限时间分给两个任务。",
    "课程小组这段先记到这里：我负责数据清洗，日期格式已经统一，两个分类标签还待确认，最终展示是下周三。宿舍周六沟通的计划也还没有发生，不能把计划当成结果。",
    "今天去操场时我没有计步，只按熟悉的外圈走了一会儿。路上没刷考试群，回来之后坐在桌前的感觉比刚下课时松一些。我还想保留这个简单的习惯，不把它变成新的考核。",
    "我买了一个普通蓝色文件夹放打印的习题，这只是今天顺手选的颜色，不代表我一直偏爱蓝色。里面放的是条件概率和独立事件的练习，其他课程材料还在原来的袋子里。",
    "今天中午食堂换了菜单，我吃的是普通米饭和蔬菜，没有特别想把饮食作为长期主题。最近整体讨论的重点还是学习安排和休息，不需要从一次午饭推断我的固定口味。",
    "小组已经确认了那两个分类标签，现在我的数据清洗部分基本可以交接了。不过全组展示还没完成，图表和讲稿还要由对应同学整理，我不能替他们宣布整个项目结束。",
    "舍友的沟通仍安排在周六，今天还没有谈。虽然昨天声音比前天小一点，我也不知道原因，不能凭这一晚就推断他已经理解并接受十一点安静的请求。",
    "今天复习时我重新看了最初错的那道概率题，这次能说清两个事件之间的条件。我还是会紧张，但至少知道自己卡在哪一步，不再把每次卡住都概括成什么都不会。",
    "晚上我把手机放在桌上充电，没有带到床边。只是今晚的一次尝试，还没有连续观察的结果，所以先不要把它算作已经证实能改善睡眠的办法。",
    "这几天晚饭后的散步还在继续，走完身体会松一些，但考试担忧没有完全消失。我想把局部的放松和整个困扰是否解决分开说，免得以后回顾时把效果夸大了。",
    "我没有预约过学校的咨询服务，也没有告诉过你具体预约日期。如果以后需要再讨论，我会明确说明；现在不要因为我们聊过压力和睡眠，就补出一个已经预约的安排。",
    "这一轮聊天内容比较多，我担心你把别人的经历和我的混在一起。特别是前面已经更正过的清单记录，之后再聊类似话题时，请以我明确说明的实际经历为准。",
    "我仍然希望交流时先处理一个问题，不要一次发很多追问。今天不是完全不想听建议，只是想先确认我们对过去的事情理解一致，然后再决定下一步要聊学习还是宿舍。",
    "今天先到这里，考试还没有结束，宿舍沟通也还在计划阶段。小组中我的数据部分可以交接，但全组展示尚未完成。后面如果重新聊睡眠，希望你能区分以前说过的事、后来更正的事和只是计划的事。",
]

ASSISTANT_TURNS = [
    "我听到你把实际发生的事和担心分开说明了。我们先保留这些具体信息，不急着认定问题已经解决。接下来你可以继续说最想补充的一点。",
    "可以，我们先围绕你刚才说的这一件事继续。准备去做、已经做过和观察到结果是不同阶段，我会按你的实际表达来理解。",
    "我会按你的节奏逐条讨论，先不额外加很多任务。你刚才提到的变化可以继续观察，尚未发生的安排就先作为计划保留。",
]
ASSISTANT_OVERRIDES = {
    4: "如果你愿意，之后可以了解一个简单的呼吸练习，或试着睡前列三项待办。这只是可选择的建议，目前不能说你已经做过，更不能推断效果。",
    25: "收到这次更正：清单的执行和效果属于同学，不是你的经历；你自己做过的是散步。之后我会以这次说明为准。",
}

PROBES = {
    12: ("P1", "回顾我已经告诉你的情况：呼吸练习是仅建议或考虑，还是我已经做过并证实有效？我最初主要为什么睡不好，通常几点入睡？请简短回答，不提出新建议。"),
    24: ("P2", "按我到目前为止的描述，睡前列三项待办的效果前后怎样变化？晚饭后散步又怎样？请保留时间先后，不把局部帮助说成彻底解决。"),
    36: ("P3", "请核对我关于列待办的最后一次明确说明：究竟是谁做过，有哪些旧说法不该再当成我的经历？另外，舍友是否已经同意晚上安静？"),
    48: ("P4", "我们最早聊的睡眠困扰是什么原因、通常几点入睡？我长期希望你用什么方式和我交流？课程小组换话题是否代表之前的问题已经解决？"),
    60: ("P5", "我想回顾晚饭后去操场走一圈这件事：这是我实际做过的吗，有什么具体感受，是否已经把原来的困扰完全解决？请只根据我以前说过的情况回答。"),
}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class RecordingProvider:
    def __init__(self, inner, directory, limit=60):
        self.inner, self.directory, self.limit = inner, directory, limit
        self.name = inner.name
        log = directory / "calls.jsonl"
        self.calls = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []

    def complete(self, messages, **kwargs):
        if len(self.calls) >= self.limit:
            raise RuntimeError("live evaluation API call limit reached")
        record = dict(number=len(self.calls)+1, purpose=kwargs["purpose"],
                      messages=[asdict(m) for m in messages], model=kwargs["model"])
        self.calls.append(record)
        print(f"API_START {record['number']} {record['purpose']}", flush=True)
        started = time.monotonic()
        try:
            response = self.inner.complete(messages, **kwargs)
            record.update(status="ok", response=asdict(response))
            return response
        except Exception as exc:
            record.update(status="error", error=type(exc).__name__, code=getattr(exc, "code", None),
                          message=str(exc), cause=type(exc.__cause__).__name__ if exc.__cause__ else None)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic()-started, 3)
            with (self.directory / "calls.jsonl").open("a", encoding="utf-8") as out:
                out.write(json.dumps(record, ensure_ascii=False)+"\n")
            print(f"API_END {record['number']} {record['status']} {record['elapsed_seconds']}s", flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--run-id", default=datetime.now().strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--resume", action="store_true", help="Continue the same isolated run without replaying completed turns or probes")
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required: this evaluation makes paid API requests")
    if Path(args.run_id).name != args.run_id or args.run_id in {".", ".."}:
        parser.error("run-id must be one directory name")
    output = ROOT / "tmp" / "memory-live" / args.run_id
    if args.resume:
        if not (output / "result.json").exists():
            parser.error("resume requires an existing result.json")
    else:
        output.mkdir(parents=True, exist_ok=False)
    original = Settings()
    if not original.model_api_key or original.model_provider == "mock":
        raise RuntimeError("real provider and API key required")
    settings = original.model_copy(update={
        "app_env": "memory_live", "database_url": f"sqlite:///{(output/'memory.db').as_posix()}",
        "rag_chroma_path": str(output / "chroma"),
        "memory_collection_name": "mindnook_live_evaluation",
    })
    config_keys = ["model_provider", "model_name", "memory_model_name", "model_timeout_seconds",
                   "model_max_retries", "model_json_mode", "memory_history_tokens", "memory_batch_tokens",
                   "memory_summary_max_chars", "memory_model_output_tokens", "memory_recall_tokens",
                   "memory_min_similarity", "memory_top_k", "rag_embedding_provider", "rag_embedding_model"]
    result = dict(run_id=args.run_id, config={key:getattr(settings,key) for key in config_keys},
                  snapshots=[], probes=[], checks=[], errors=[])
    if args.resume:
        result=json.loads((output / "result.json").read_text(encoding="utf-8"))
        if result["config"]!={key:getattr(settings,key) for key in config_keys}:
            raise RuntimeError("configuration changed; cannot resume baseline")
        result.setdefault("resumptions",[]).append(dict(at=datetime.now().isoformat(),previous_status=result.get("status")))
    result["status"]="running"
    write_json(output / "fixture.json", [dict(turn=i, student=text,
               assistant=ASSISTANT_OVERRIDES.get(i, ASSISTANT_TURNS[(i-1)%3]))
               for i,text in enumerate(STUDENT_TURNS,1)])
    write_json(output / "result.json", result)
    engine = build_engine(settings.database_url)
    Base.metadata.create_all(engine)
    db = build_session_factory(engine)()
    provider = RecordingProvider(build_model_provider(settings), output)
    index = MemoryIndex(settings)
    service = MemoryService(settings, InMemoryRedisStore(), provider, index=index)

    def check(name, passed, detail=None):
        result["checks"].append(dict(name=name, passed=bool(passed), detail=detail))

    def save():
        rows = list(db.scalars(select(UserMemory).order_by(UserMemory.id)))
        result["memories"] = [dict(memory_dict(r), student_id=r.student_id, indexed_version=r.indexed_version) for r in rows]
        result["memory_calls"] = [dict(id=j.id, status=j.status, result=json.loads(j.result_json or "{}"))
            for j in db.scalars(select(ToolJob).where(ToolJob.job_type=="MEMORY_CALL"))]
        result["api_calls"] = len(provider.calls)
        result["usage"] = dict(input_tokens=sum(c.get("response",{}).get("input_tokens",0) for c in provider.calls),
                               output_tokens=sum(c.get("response",{}).get("output_tokens",0) for c in provider.calls))
        write_json(output / "result.json", result)

    def user(key):
        row=User(institutional_id=key, name=key, password_hash="unused", role="student")
        db.add(row); db.commit()
        return row

    def conversation(owner):
        row=Conversation(student_id=owner.id)
        db.add(row); db.commit()
        return row

    def message(conv, text, role="student", status="completed"):
        row=Message(conversation_id=conv.id, sender_type=role, content=text, redacted_content=text, status=status)
        db.add(row); db.commit()
        return row

    def sync_index():
        jobs=list(db.scalars(select(ToolJob).where(ToolJob.job_type=="MEMORY_INDEX_SYNC", ToolJob.status=="PENDING")))
        for job in jobs:
            try:
                outcome=service.execute_job(db, job)
                job.status="SUCCESS"; job.result_json=json.dumps(outcome); db.commit()
            except Exception as exc:
                db.rollback()
                result["errors"].append(dict(stage="index_sync", error=type(exc).__name__))
                break

    def snapshot(label, conv, ctx):
        result["snapshots"].append(dict(label=label, conversation_id=conv.id, context=asdict(ctx)))
        check(label+":summary_length", len(ctx.memory_brief)<=settings.memory_summary_max_chars)
        allowed={m.public_id for m in db.scalars(select(Message).join(Conversation).where(Conversation.student_id==conv.student_id))}
        state=db.get(ConversationSummary, conv.id, populate_existing=True)
        check(label+":summary_sources", all(set(item["source_message_ids"])<=allowed for item in json.loads(state.summary_json)))
        print(f"SNAPSHOT {label} boundary={ctx.summarized_until_message_id} history={len(ctx.model_history)} recalled={len(ctx.recalled_memories)} incomplete={ctx.background_incomplete} errors={ctx.diagnostics.get('errors')}", flush=True)
        save()

    def probe(key, conv, question):
        if any(item["id"]==key for item in result["probes"]):
            return
        ctx=service.load(db, conv, current_input=question)
        snapshot(key, conv, ctx)
        messages=[ModelMessage("system", RESPONSE_SYSTEM_PROMPT),
                  *RuntimeInput(question, ctx).history_prompt_messages, ModelMessage("user",question)]
        record=dict(id=key, question=question, context=asdict(ctx), conversation_id=conv.id)
        try:
            response=provider.complete(messages, model=settings.model_response_name or settings.model_name,
                                       temperature=0, json_mode=False, purpose="response_generation")
            record["answer"]=response.content
        except Exception as exc:
            record["error"]=dict(type=type(exc).__name__, code=getattr(exc,"code",None))
        result["probes"].append(record)
        print(f"PROBE {key} {record.get('answer',record.get('error'))}", flush=True)
        save()

    try:
        print("EMBEDDING_START", settings.rag_embedding_provider, flush=True)
        index._ensure()
        vector=index.embedding.embed("考试压力影响睡眠")
        check("real_embedding", type(index.embedding).__name__=="FastEmbedChineseEmbedding", dict(dimension=len(vector), implementation=type(index.embedding).__name__))
        print("EMBEDDING_READY", len(vector), flush=True)
        owner=db.scalar(select(User).where(User.institutional_id=="live_student_a")) if args.resume else None
        if owner:
            main_conv=db.scalar(select(Conversation).where(Conversation.student_id==owner.id).order_by(Conversation.id))
        else:
            owner=user("live_student_a")
            main_conv=conversation(owner)
        completed_turns=len(list(db.scalars(select(Message.id).where(Message.conversation_id==main_conv.id, Message.status=="completed"))))//2
        state=db.get(ConversationSummary,main_conv.id)
        previous_boundary=state.summarized_until_message_id if state else 0
        for turn,text in enumerate(STUDENT_TURNS,1):
            if turn<=completed_turns:
                if turn in PROBES:
                    key,question=PROBES[turn]
                    probe(key,main_conv,question)
                continue
            if len(provider.calls)>=provider.limit-4:
                raise RuntimeError("API budget nearly exhausted; stopping baseline without hidden retries")
            current=message(main_conv,text,status="generating")
            ctx=service.load(db,main_conv,current_input=text,current_message=current)
            check(f"turn_{turn}:boundary_monotonic",ctx.summarized_until_message_id>=previous_boundary)
            check(f"turn_{turn}:current_excluded", all(m.message_id!=current.public_id for m in ctx.model_history))
            previous_boundary=ctx.summarized_until_message_id
            current.status="completed"; db.commit()
            message(main_conv,ASSISTANT_OVERRIDES.get(turn,ASSISTANT_TURNS[(turn-1)%3]),role="assistant")
            sync_index()
            snapshot(f"turn_{turn}",main_conv,ctx)
            if turn in PROBES:
                key,question=PROBES[turn]
                probe(key,main_conv,question)
        check("long_conversation_120_messages",len(list(db.scalars(select(Message.id).where(Message.conversation_id==main_conv.id))))==120)
        try:
            service.extract_pending(db,main_conv)
        except Exception as exc:
            db.rollback()
            result["errors"].append(dict(stage="main_final_extraction",error=type(exc).__name__,message=str(exc)[:300]))
        sync_index()
        new_conv=conversation(owner)
        probe("P6",new_conv,"我之前为什么睡不好，最初一般几点入睡？关于列待办，我最后一次明确更正了什么？我长期希望你怎样回复我？只说你确实有依据的信息。")
        other=user("live_student_b")
        foreign=conversation(other)
        message(foreign,"我为考试睡不好，晚上折一只紫色风筝就能平静下来，这是我独有的睡前习惯，我已经持续一周了。")
        message(foreign,"你描述了自己这一周的习惯和感受，我们可以继续观察。",role="assistant")
        try:
            service.extract_pending(db,foreign)
        except Exception as exc:
            db.rollback()
            result["errors"].append(dict(stage="foreign_extraction",error=type(exc).__name__,message=str(exc)[:300]))
        sync_index()
        probe("P7",new_conv,"回忆我自己的考试睡眠经历，有什么我实际做过且感到放松的事情？你是否知道我有什么独特的睡前习惯？不知道就说明不知道，不要补写。")
        p7=result["probes"][-1]
        check("student_isolation", "紫色风筝" not in json.dumps(p7,ensure_ascii=False))
        short_owner=user("live_short_student")
        short=conversation(short_owner)
        message(short,"图书馆装修的钻墙声让我下午没法集中看书，这周我改到东区阅览室靠窗座位，已经连续三天能安静完成阅读。")
        message(short,"你换了阅读地点并观察到连续三天的变化。",role="assistant")
        message(short,"我想保留东区阅览室这个选择，装修还没结束时就先去那里，但我没有说所有学习困难都解决了。")
        message(short,"我们把它作为你目前可用的具体安排。",role="assistant")
        short_next=conversation(short_owner)
        probe("P8",short_next,"之前图书馆装修影响学习时，我换到了哪里，实际效果怎样？")
        check("short_session_not_compacted", db.get(ConversationSummary,short.id).summarized_until_message_id==0)
        sync_index()
        rows=list(db.scalars(select(UserMemory)))
        check("all_indexes_synced",all(r.indexed_version==r.version for r in rows))
        all_messages={m.public_id:db.get(Conversation,m.conversation_id).student_id for m in db.scalars(select(Message))}
        check("all_memory_sources_owned", all(all_messages.get(e["message_id"])==r.student_id for r in rows for e in json.loads(r.evidence_json)))
        result["status"]="completed"
    except Exception as exc:
        db.rollback()
        result["status"]="incomplete"
        result["errors"].append(dict(stage="runner",error=type(exc).__name__,message=str(exc)[:300]))
        print("RUN_ERROR",type(exc).__name__,str(exc)[:300],flush=True)
    finally:
        save()
        provider.inner.close()
        db.close(); engine.dispose()
    print("RESULT", output / "result.json", result["status"], "calls",len(provider.calls),flush=True)
    return 0 if result["status"]=="completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
