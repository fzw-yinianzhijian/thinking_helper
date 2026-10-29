"""The default oral report reads records, never old model reflections."""
import unittest

from app.reporting import compose_report


class ReportingScenarios(unittest.TestCase):
    def test_old_memfs_reflections_and_rejected_candidates_cannot_pollute_current_report(self):
        topic = {
            "title": "持续思考与决策权", "version": 7,
            "document": {"summary": "后台可以探索，重要决定仍由用户作出。",
                         "claims": [{"id": "h", "text": "保留独立判断", "status": "hypothesis", "source_ids": []},
                                    {"id": "c", "text": "外部资料介绍三种打断模式", "status": "supported", "source_ids": ["s"]}],
                         "questions": [{"id": "q-now", "text": "主动探索与最终决策权如何区分？", "status": "open"},
                                       {"id": "q-resolved", "text": "已经解决的旧问题", "status": "resolved"}],
                         "next_steps": ["核对一次真实停顿与打断样本。"]},
            "candidates": [{"status": "rejected", "reflection": "当前系统已部署MemFS，需要检查同步。", "next_steps": ["MemFS离线同步待验证（旧计划）"]}],
        }
        report = compose_report(topic,
            latest_change={"applied_version": 7, "summary": "把用户原话关联到既有决策边界。", "reflection": "MemFS部署有缺陷。"},
            background={"jobs": [{"status": "running"}], "candidates": [{"status": "pending", "summary": "MemFS待验证"}, {"status": "rejected", "reflection": "已部署MemFS"}]})
        spoken = "".join(report)
        self.assertNotIn("MemFS", spoken)
        self.assertNotIn("已经解决的旧问题", spoken)
        self.assertIn("核对一次真实停顿与打断样本", spoken)
        self.assertIn("还有1个开放问题", spoken)
        self.assertIn("待审1项", spoken)
        self.assertIn("后台运行中", spoken)
        self.assertNotIn("已部署", spoken)
        self.assertNotIn("有引用判断1条", spoken)
        stale = compose_report(topic, latest_change={"applied_version": 6, "summary": "旧版MemFS变化"})
        self.assertNotIn("MemFS", "".join(stale))
        self.assertIn("没有新的整合记录", stale[1])

    def test_empty_workspace_says_what_has_not_been_recorded(self):
        report = compose_report({})
        self.assertEqual(len(report), 3)
        spoken = "".join(report)
        self.assertIn("没有标题或概述记录", spoken)
        self.assertIn("没有新的整合记录", spoken)
        self.assertIn("尚无下一步或开放问题记录", spoken)
        self.assertIn("后台状态未记录", spoken)

    def test_long_content_is_bounded_and_explicitly_an_excerpt(self):
        topic = {"title": "一个需要长期思考的问题" * 30, "version": 2,
                 "document": {"summary": "这里保留尚未确定的解释与条件。" * 100,
                              "claims": [{"status": "contested", "source_ids": ["s"]}],
                              "questions": [{"id": str(i), "text": f"第{i}个问题：" + "请保留条件和不确定性" * 50, "status": "open"} for i in range(5)]}}
        report = compose_report(topic, latest_change={"version": 2, "note": "这次只增加了一个待核查的关联。" * 100},
                                background={"paused": True, "running": 0, "pending": 3})
        self.assertEqual(len(report), 3)
        self.assertTrue(all(len(paragraph) <= 80 for paragraph in report))
        self.assertLessEqual(sum(map(len, report)), 200)
        self.assertIn("节选", report[0])
        self.assertIn("节选", report[2])
        self.assertIn("…", "".join(report))
        self.assertIn("后台已暂停", report[2])
        self.assertIn("待审3项", report[2])
        self.assertIn("还有4个开放问题", report[2])

    def test_current_next_step_precedes_questions_and_is_a_plan_not_a_deployment_claim(self):
        topic = {"title": "比较方案", "version": 3,
                 "document": {"summary": "当前比较外部方案，还没有确定部署选择。",
                              "next_steps": ["核对外部MemFS的同步条件，再决定是否采用。", "部署它。"],
                              "questions": [{"id": "q1", "text": "如何保证持续推进？", "status": "open"},
                                            {"id": "q2", "text": "怎样减少反复解释？", "status": "open"},
                                            {"id": "q3", "text": "哪些部分需要评估？", "status": "open"}]}}
        report = compose_report(topic, latest_change={"version": 3, "note": "留下了下一项查证任务。"},
                                background={"running": 0, "pending": 0})
        self.assertIn("下一步记录：核对外部MemFS", report[2])
        self.assertIn("再决定是否采用", report[2])
        self.assertIn("还有3个开放问题", report[2])
        self.assertNotIn("部署它", "".join(report))
        self.assertFalse(any(q["text"] in report[2] for q in topic["document"]["questions"]))
        topic["document"]["next_steps"] = []
        fallback = compose_report(topic)
        self.assertIn("如何保证持续推进", fallback[2])
        self.assertIn("还有2个开放问题", fallback[2])
        self.assertNotIn("怎样减少反复解释", fallback[2])
        self.assertNotIn("哪些部分需要评估", fallback[2])


if __name__ == "__main__":
    unittest.main()
