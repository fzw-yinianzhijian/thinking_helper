"""Short spoken reports from current records, without a model or history."""
from __future__ import annotations

import re


def _text(value):
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def _excerpt(value, limit):
    text = _text(value)
    if len(text) <= limit:
        return text, False
    first_sentence = re.search(r"[。！？]|[.!?](?=\s|$)", text)
    if first_sentence is not None and first_sentence.end() <= limit:
        return text[:first_sentence.end()], True
    return text[:max(0, limit - 1)].rstrip() + "…", True


def _line(prefix, excerpt_prefix, value, budget):
    text, shortened = _excerpt(value, max(1, budget - max(len(prefix), len(excerpt_prefix)) - 1))
    ending = "" if re.search(r"[。！？.!?…][\"'”’」』]*$", text) else "。"
    return (excerpt_prefix if shortened else prefix) + text + ending


def _count(value):
    return value if type(value) is int and value >= 0 else None


def _background(background):
    if not isinstance(background, dict):
        return "后台状态未记录。"
    jobs = background.get("jobs")
    jobs = jobs if isinstance(jobs, list) else None
    candidates = background.get("candidates")
    candidates = candidates if isinstance(candidates, list) else None
    running = _count(background.get("running"))
    if running is None and jobs is not None:
        running = sum(isinstance(job, dict) and job.get("status") == "running" for job in jobs)
    pending = _count(background.get("pending"))
    if pending is None and candidates is not None:
        pending = sum(isinstance(item, dict) and item.get("status") == "pending" for item in candidates)
    if background.get("paused") is True:
        runtime = "后台已暂停"
    elif running:
        runtime = "后台运行中"
    elif running == 0:
        runtime = "后台待命"
    else:
        runtime = "后台状态未记录"
    return runtime + (f"，待审{pending}项" if pending else "") + "。"


def _change(topic, latest_change):
    if isinstance(latest_change, str):
        return _text(latest_change)
    if not isinstance(latest_change, dict):
        return ""
    version = latest_change.get("applied_version", latest_change.get("version"))
    if version is not None and topic.get("version") is not None and version != topic["version"]:
        return ""
    # Reflection, rejected content and model descriptions of runtime are never
    # a substitute for the explicit note/summary of this version's change.
    return _text(latest_change.get("note")) or _text(latest_change.get("summary"))


def compose_report(topic, *, latest_change=None, background=None):
    """Return three calm paragraphs, each <=80 characters and total <=200.

    Only current title/document and explicitly supplied change/status fields
    are read. Excerpts are labelled as excerpts, never completed by inference.
    """
    topic = topic if isinstance(topic, dict) else {}
    document = topic.get("document")
    document = document if isinstance(document, dict) else {}
    summary = _text(document.get("summary"))
    if summary:
        first = _line("当前笔记概况：", "概况节选：", summary, 40)
    elif _text(topic.get("title")):
        first = _line("当前问题：", "问题标题节选：", topic["title"], 40)
    else:
        first = "当前没有标题或概述记录。"
    change = _change(topic, latest_change)
    if change:
        second = _line("本版变化记录：", "变化记录节选：", change, 80)
    else:
        second = "当前版本没有新的整合记录。"

    questions = document.get("questions")
    questions = questions if isinstance(questions, list) else []
    opened = []
    for question in questions:
        if isinstance(question, str):
            text = _text(question)
        elif isinstance(question, dict) and str(question.get("status", "open")).lower() != "resolved":
            text = _text(question.get("text"))
        else:
            continue
        if text:
            opened.append(text)
    status = _background(background)
    steps = document.get("next_steps")
    steps = steps if isinstance(steps, list) else []
    next_step = next((_text(item if isinstance(item, str) else item.get("text"))
                      for item in steps if isinstance(item, (str, dict))
                      and _text(item if isinstance(item, str) else item.get("text"))), "")
    if next_step:
        remaining = f"还有{len(opened)}个开放问题。" if opened else "暂无开放问题。"
        tail = remaining + status
        third = _line("下一步记录：", "下一步记录节选：", next_step, max(12, 80 - len(tail))) + tail
    elif opened:
        remaining = f"还有{len(opened) - 1}个开放问题。" if len(opened) > 1 else "当前就这一个开放问题。"
        tail = remaining + status
        third = _line("先看一个问题：", "问题节选：", opened[0], max(12, 80 - len(tail))) + tail
    else:
        third = "尚无下一步或开放问题记录。" + status
    paragraphs = [first, second, third]
    return [_excerpt(paragraph, limit)[0] for paragraph, limit in zip(paragraphs, (40, 80, 80))]
