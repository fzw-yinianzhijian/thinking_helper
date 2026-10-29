"""Create one real research workspace. No cloud request until the service runs."""
from app.config import Settings
from app.core import Store
import hashlib


SEED_SOURCES = [
    ("https://docs.letta.com/configuration/schedules", "Letta Schedules 官方文档"),
    ("https://docs.letta.com/concepts/memfs", "Letta MemFS 官方文档"),
    ("https://help.aliyun.com/zh/model-studio/multimodal-app-configuration", "百炼语音交互配置官方文档"),
]


def main():
    store = Store(Settings.load().database)
    topics = store.list_topics()
    if topics:
        print("Existing workspace:", topics[0]["id"])
        return
    topic = store.create_topic(
        "让 Thinking Helper 持续推进，而不打断思考",
        "帮我持续思考和创造：自然低延迟的语音入口；我离开后仍围绕长期问题查证、反思和探索；成果整合回清楚、可复利的结构；我能随时理解全局、纠正方向。首轮查证可复用后台与语音方案，给出能力边界。",
        document={
            "summary": "核心是持续演化的问题与作品。语音是入口，长期工作区承接后台成果。第一版要跑通跨时间的闭环，再用实际体感与成果质量迭代。",
            "claims": [
                {"id": "c-agency", "text": "系统应帮助用户更容易继续表达、形成判断和创造。", "status": "hypothesis", "source_ids": []},
                {"id": "c-structure", "text": "每次更新要说明对旧结构的补充、反驳或替代关系。", "status": "hypothesis", "source_ids": []},
            ],
            "questions": [
                {"id": "q-runtime", "text": "怎样让用户离开后仍持续推进，并避免重复或过期工作？", "status": "open"},
                {"id": "q-voice", "text": "怎样在低延迟回应、思考停顿和打断之间取得合适体感？", "status": "open"},
            ],
            "next_steps": ["查证持久后台和语音方案的当前一手资料。", "实测一次离线推进、整合、口头汇报与纠正。"],
        }, sources=[{"id": "src-" + hashlib.sha256(url.encode()).hexdigest()[:12], "url": url,
                     "title": title, "provenance": "curated_source_scope"} for url, title in SEED_SOURCES],
        interval_seconds=3600, auto_apply=False,
    )
    print("Created workspace:", topic["id"])


if __name__ == "__main__":
    main()
