#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一次 GitHub Release 同步到飞书：发版记录表 + 群卡片。

设计约束（来自 pm/版本管理方案.md）：

- 本脚本**只写发版记录表**（机器所有、只追加），**绝不写 §7.2 的版本台账**。
  台账的唯一信息源是 release.yml，由人在 gate 通过后维护。两张表通过
  「版本」字段关联，但永远不争夺所有权。
- 因此本脚本没有任何删除操作，重复执行同一个 tag 只会更新同一行（幂等）。

依赖：仅标准库。GitHub Actions runner 自带 Python 3。

用法：
    # CI：从 GITHUB_EVENT_PATH 读取 release 事件
    python3 feishu_release.py --from-event

    # 本地冒烟测试：不写任何东西，只打印将要发出的请求
    python3 feishu_release.py --from-event --dry-run

    # 本地回填：把某个已有 tag 补进飞书
    python3 feishu_release.py --repo xeolab/cinema-demo --tag v1.2.0

环境变量见 README.md。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any

FEISHU_HOST = os.environ.get("FEISHU_HOST", "https://open.feishu.cn")

# 约定式提交前缀 → changelog 分组。对齐 pm/版本管理方案.md §8.3 的五类。
COMMIT_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("新增", ("feat", "feature")),
    ("修复", ("fix", "bugfix", "hotfix")),
    ("变更", ("change", "refactor", "perf", "style", "build", "chore")),
    ("移除", ("remove", "revert", "deprecate")),
]
# 不进 changelog 的类型：对使用者无可感知变化（§8.2）
COMMIT_SKIP = ("docs", "test", "ci", "release")


class FeishuError(RuntimeError):
    """飞书接口返回了非 0 的业务码。"""


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    retries: int = 3,
) -> dict[str, Any]:
    """调用飞书 OpenAPI，返回已解析的 JSON。业务码非 0 抛 FeishuError。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            # 4xx 是我们自己的问题，重试没有意义，直接失败并带上飞书的原文。
            if 400 <= exc.code < 500:
                raise FeishuError(f"{method} {url} -> HTTP {exc.code}: {detail}") from exc
            last_err = FeishuError(f"{method} {url} -> HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_err = exc
        else:
            code = payload.get("code", 0)
            if code != 0:
                # 99991663 = tenant_access_token 失效；其余业务码直接失败。
                raise FeishuError(
                    f"{method} {url} -> code={code} msg={payload.get('msg')!r}"
                )
            return payload

        if attempt < retries - 1:
            time.sleep(2**attempt)

    raise FeishuError(f"{method} {url} 重试 {retries} 次后仍失败: {last_err}")


def tenant_token(app_id: str, app_secret: str) -> str:
    payload = _request(
        "POST",
        f"{FEISHU_HOST}/open-apis/auth/v3/tenant_access_token/internal",
        body={"app_id": app_id, "app_secret": app_secret},
    )
    token = payload.get("tenant_access_token")
    if not token:
        raise FeishuError(f"未拿到 tenant_access_token：{payload}")
    return token


# --------------------------------------------------------------------------
# Release notes 整理
# --------------------------------------------------------------------------


def _clean_body(body: str) -> str:
    """清掉 GitHub Release 正文里对读者没有价值的部分。"""
    body = re.sub(r"<!--.*?-->", "", body, flags=re.S)          # HTML 注释
    body = re.sub(r"^#{1,2} +", "### ", body, flags=re.M)        # 降级到 h3，避免压过文档结构
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body.strip()


def _commit_subjects(tag: str, previous: str | None) -> list[str]:
    rng = f"{previous}..{tag}" if previous else tag
    try:
        out = subprocess.run(
            ["git", "log", "--no-merges", "--pretty=format:%s", rng],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _previous_tag(tag: str) -> str | None:
    try:
        return subprocess.run(
            ["git", "describe", "--tags", "--abbrev=0", f"{tag}^"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def notes_from_commits(tag: str) -> str:
    """Release 正文为空时，从提交记录自动整理出分组 changelog。

    只识别约定式提交前缀；无前缀的提交归入「变更」，因为无法判断它是否
    对使用者可感知——宁可多列一条让人删，也不要漏。
    """
    previous = _previous_tag(tag)
    subjects = _commit_subjects(tag, previous)
    if not subjects:
        return ""

    buckets: dict[str, list[str]] = {name: [] for name, _ in COMMIT_GROUPS}
    for subject in subjects:
        match = re.match(r"^(\w+)(?:\([^)]*\))?!?:\s*(.+)$", subject)
        if not match:
            buckets["变更"].append(subject)
            continue
        kind, text = match.group(1).lower(), match.group(2).strip()
        if kind in COMMIT_SKIP:
            continue
        for name, prefixes in COMMIT_GROUPS:
            if kind in prefixes:
                buckets[name].append(text)
                break
        else:
            buckets["变更"].append(text)

    parts: list[str] = []
    for name, _ in COMMIT_GROUPS:
        items = buckets[name]
        if items:
            parts.append(f"### {name}\n" + "\n".join(f"- {i}" for i in items))
    body = "\n\n".join(parts)
    if body and previous:
        body += f"\n\n_自动整理自 {previous}..{tag} 的提交记录，未经人工校订。_"
    elif body:
        body += "\n\n_自动整理自提交记录，未经人工校订。_"
    return body


def build_notes(release: dict[str, Any], tag: str) -> str:
    body = _clean_body(release.get("body") or "")
    if body:
        return body
    generated = notes_from_commits(tag)
    return generated or "_本次发布未提供变更说明。_"


# --------------------------------------------------------------------------
# 飞书写入
# --------------------------------------------------------------------------


def table_field_names(token: str, app_token: str, table_id: str) -> set[str]:
    """读表里实际存在的字段名。用来适配人工改过的表结构。"""
    payload = _request(
        "GET",
        f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}"
        f"/tables/{table_id}/fields?page_size=100",
        token=token,
    )
    return {
        f.get("field_name")
        for f in (payload.get("data") or {}).get("items") or []
        if f.get("field_name")
    }


def adapt_fields(
    canonical: dict[str, Any], actual: set[str], rename: dict[str, str]
) -> tuple[dict[str, Any], list[str]]:
    """把脚本产出的字段对齐到表的真实结构。

    表是人在维护的，列会被改名、删掉、加新的。硬写死字段名的话，
    别人改一次表这条流水线就断，而且报错是 FieldNameNotFound 这种
    看不出所以然的东西。所以：先读表，只写表里真有的列。

    rename 来自 FEISHU_BASE_FIELD_MAP，形如 {"版本": "版本号"}，
    让改过名的列不用改代码就能继续写。
    """
    out: dict[str, Any] = {}
    skipped: list[str] = []
    for logical, value in canonical.items():
        column = rename.get(logical, logical)
        if column in actual:
            out[column] = value
        else:
            skipped.append(f"{logical}→{column}")
    return out, skipped


def upsert_base_record(
    token: str,
    app_token: str,
    table_id: str,
    key_field: str,
    fields: dict[str, Any],
    *,
    dry_run: bool,
) -> str | None:
    """按 key_field 幂等 upsert 一行。返回 record_id。

    先 search 再决定 create/update，而不是无脑 create——workflow 重跑、
    release 被 edit 后重新触发都会走到这里，不能产生重复行。
    """
    base = f"{FEISHU_HOST}/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
    key_value = fields[key_field]

    if dry_run:
        print(f"[dry-run] upsert {key_field}={key_value!r} 到 {app_token}/{table_id}")
        print(json.dumps(fields, ensure_ascii=False, indent=2))
        return None

    found = _request(
        "POST",
        f"{base}/search?page_size=1",
        token=token,
        body={
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {"field_name": key_field, "operator": "is", "value": [key_value]}
                ],
            }
        },
    )
    items = found.get("data", {}).get("items") or []

    if items:
        record_id = items[0]["record_id"]
        _request("PUT", f"{base}/{record_id}", token=token, body={"fields": fields})
        return record_id

    created = _request("POST", base, token=token, body={"fields": fields})
    return created.get("data", {}).get("record", {}).get("record_id")


def send_card_as_bot(
    token: str, chat_id: str, card: dict[str, Any], *, dry_run: bool
) -> None:
    """以应用身份把卡片发到群里。

    比群机器人 webhook 好在：消息来自应用本体（点头像能看到应用信息），
    不需要额外维护一个 webhook secret，且机器人本来就要进群才能收 @ 消息。
    代价是应用必须已被拉进该群，否则报 permission denied。
    """
    url = f"{FEISHU_HOST}/open-apis/im/v1/messages?receive_id_type=chat_id"
    body = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        # content 必须是 JSON 字符串，不是对象——这是 im/v1/messages 的约定
        "content": json.dumps(card, ensure_ascii=False),
    }
    if dry_run:
        print(f"[dry-run] POST {url}\n{json.dumps(body, ensure_ascii=False)[:600]}")
        return
    _request("POST", url, token=token, body=body)


def send_card(webhook: str, card: dict[str, Any], *, dry_run: bool) -> None:
    payload = {"msg_type": "interactive", "card": card}
    if dry_run:
        print(f"[dry-run] POST {webhook.split('/hook/')[0]}/hook/***")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    code = result.get("code", result.get("StatusCode", 0))
    if code not in (0, None):
        raise FeishuError(f"群机器人 webhook 失败：{result}")


# --------------------------------------------------------------------------
# 卡片
# --------------------------------------------------------------------------


def _excerpt(markdown: str, limit: int) -> tuple[str, bool]:
    """截断 release notes 用于卡片。按行截断，不在行中间切断。

    标题前补空行：飞书卡片的 markdown 渲染要求标题独占段落，否则 `###`
    会被当成正文原样显示。
    """
    kept: list[str] = []
    total = 0
    truncated = False
    for line in (l for l in markdown.splitlines() if l.strip()):
        if total + len(line) > limit:
            truncated = True
            break
        if line.lstrip().startswith("#") and kept:
            kept.append("")
        kept.append(line)
        total += len(line) + 1
    return "\n".join(kept), truncated


def build_card(ctx: dict[str, Any]) -> dict[str, Any]:
    excerpt, truncated = _excerpt(ctx["notes"], ctx.get("card_limit", 900))
    if truncated:
        excerpt += "\n\n_（内容较长，已截断——点下方按钮看完整发布说明）_"

    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": (
                f"**版本**：`{ctx['tag']}`\n"
                f"**项目**：{ctx['project']}\n"
                f"**文件**：`{ctx['archive_name']}`\n"
                f"**发布时间**：{ctx['published_at']}\n"
                f"**下载地址**：[{ctx['download_url']}]({ctx['download_url']})"
            ),
        },
        {"tag": "hr"},
        {"tag": "markdown", "content": f"**本版本变更**\n\n{excerpt}"},
    ]

    actions = [
        {
            "tag": "button",
            "type": "primary",
            "text": {"tag": "plain_text", "content": "下载 Release ZIP"},
            "url": ctx["download_url"],
        }
    ]
    if ctx.get("base_url"):
        actions.append(
            {
                "tag": "button",
                "type": "default",
                "text": {"tag": "plain_text", "content": "发版记录表"},
                "url": ctx["base_url"],
            }
        )
    actions.append(
        {
            "tag": "button",
            "type": "default",
            "text": {"tag": "plain_text", "content": "GitHub Release"},
            "url": ctx["release_url"],
        }
    )

    elements.extend(
        [
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {
                        "tag": "plain_text",
                        "content": "该版本尚未经过 XEO 验收 gate，请勿直接外发给客户。",
                    }
                ],
            },
            {"tag": "action", "actions": actions},
        ]
    )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange" if ctx.get("prerelease") else "green",
            "title": {
                "tag": "plain_text",
                "content": (
                    f"🚧 {ctx['project']} 预发布 {ctx['tag']}"
                    if ctx.get("prerelease")
                    else f"🚀 {ctx['project']} 发布 {ctx['tag']}"
                ),
            },
        },
        "elements": elements,
    }


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _epoch_ms(iso: str) -> int | None:
    """把 GitHub 的 ISO8601 时间转成毫秒时间戳。

    多维表格的日期字段走原生 REST 时只接受毫秒整数；传 "2026-08-18 11:20:00"
    这种字符串会被拒绝（code=1254064 DatetimeFieldConvFail）。
    lark-cli 的 +record-* 快捷命令会替你转，raw REST 不会。
    """
    if not iso:
        return None
    text = iso.strip().replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1000)
    except ValueError:
        return None


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _require(name: str) -> str:
    value = _env(name)
    if not value:
        sys.exit(f"缺少必需的环境变量：{name}")
    return value


def load_release(args: argparse.Namespace) -> tuple[dict[str, Any], str, str, str]:
    """返回 (release 对象, repo full name, tag, 事件 action)。"""
    if args.from_event:
        path = _require("GITHUB_EVENT_PATH")
        with open(path, encoding="utf-8") as handle:
            event = json.load(handle)
        release = event.get("release") or {}
        repo = _env("GITHUB_REPOSITORY") or event.get("repository", {}).get("full_name", "")

        if release:
            return release, repo, release.get("tag_name", ""), event.get("action", "published")

        # 经 workflow_call 触发时事件里没有 release 对象（触发它的是别的事件），
        # 此时由调用方通过 RELEASE_TAG 指定版本，我们回查 GitHub API 补齐。
        tag = _env("RELEASE_TAG")
        if not (tag and repo):
            sys.exit(
                "事件里没有 release 对象，且未提供 RELEASE_TAG / GITHUB_REPOSITORY——"
                "经 workflow_call 调用时请把 release_tag 传进 RELEASE_TAG 环境变量"
            )
        args.repo, args.tag = repo, tag
        args.from_event = False
        release, repo, tag, _ = load_release(args)
        return release, repo, tag, "published"

    if not (args.repo and args.tag):
        sys.exit("非 --from-event 模式下必须同时给出 --repo 和 --tag")

    # 本地回填：用 GitHub API 取 release（公开仓库无需 token；私有仓库设 GH_TOKEN）
    url = f"https://api.github.com/repos/{args.repo}/releases/tags/{args.tag}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    if _env("GH_TOKEN"):
        req.add_header("Authorization", f"Bearer {_env('GH_TOKEN')}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = "（私有仓库需要设置 GH_TOKEN）" if exc.code in (403, 404) else ""
        sys.exit(
            f"取 GitHub Release 失败：HTTP {exc.code} {hint}\n"
            f"{exc.read().decode('utf-8', 'replace')[:400]}"
        )
    except (urllib.error.URLError, TimeoutError) as exc:
        sys.exit(f"连不上 GitHub API：{exc}")
    return release, args.repo, args.tag, "published"


def main() -> int:
    parser = argparse.ArgumentParser(description="把 GitHub Release 同步到飞书")
    parser.add_argument("--from-event", action="store_true", help="从 GITHUB_EVENT_PATH 读取")
    parser.add_argument("--repo", help="owner/name，回填模式使用")
    parser.add_argument("--tag", help="tag 名，回填模式使用")
    parser.add_argument("--dry-run", action="store_true", help="只打印请求，不写任何东西")
    parser.add_argument("--skip-card", action="store_true", help="不发群卡片（回填历史时常用）")
    args = parser.parse_args()

    release, repo, tag, action = load_release(args)
    if not tag:
        sys.exit("无法确定 tag，终止")

    # release 被 edited 时只刷新记录表：群卡片不该为一次改错别字再响一次。
    # 记录表是幂等 upsert，重复执行安全。
    first_publish = action != "edited"
    if not first_publish:
        print("事件为 release edited：只更新发版记录表，不再发一次群卡片")

    project = _env("XEO_PROJECT") or repo.split("/")[-1]
    notes = build_notes(release, tag)
    published_at = (release.get("published_at") or "")[:19].replace("T", " ")

    ctx = {
        "tag": tag,
        "project": project,
        "repo": repo,
        "notes": notes,
        "published_at": published_at,
        "prerelease": bool(release.get("prerelease")),
        "archive_name": _env("ARCHIVE_NAME"),
        "download_url": _env("PUBLIC_URL"),
        "sha256": _env("ARCHIVE_SHA256"),
        "release_url": release.get("html_url", ""),
        "base_url": _env("FEISHU_BASE_URL"),
        "card_limit": int(_env("FEISHU_CARD_LIMIT", "900")),
    }

    app_id = _require("FEISHU_APP_ID")
    app_secret = _require("FEISHU_APP_SECRET")
    token = "dry-run-token" if args.dry_run else tenant_token(app_id, app_secret)

    # 1) 发版记录表（机器所有，只追加/幂等更新）
    base_app = _env("FEISHU_BASE_APP_TOKEN")
    base_table = _env("FEISHU_BASE_TABLE_ID")
    if base_app and base_table:
        fields: dict[str, Any] = {
            "版本": tag,
            "项目": project,
            "仓库": repo,
            "发布说明": notes,
            "预发布": bool(release.get("prerelease")),
        }
        published_ms = _epoch_ms(release.get("published_at") or "")
        if published_ms is not None:
            fields["发布时间"] = published_ms
        if ctx["archive_name"]:
            fields["文件名"] = ctx["archive_name"]
        if ctx["sha256"]:
            fields["SHA256"] = ctx["sha256"]
        if ctx["download_url"]:
            fields["下载地址"] = {"link": ctx["download_url"], "text": ctx["archive_name"] or "下载"}
        if ctx["release_url"]:
            fields["GitHub Release"] = {"link": ctx["release_url"], "text": tag}
        key_field = _env("FEISHU_BASE_KEY_FIELD", "版本")
        try:
            rename = json.loads(_env("FEISHU_BASE_FIELD_MAP") or "{}")
        except json.JSONDecodeError as exc:
            sys.exit(f"FEISHU_BASE_FIELD_MAP 不是合法 JSON：{exc}")

        if args.dry_run:
            actual = set(fields) | {rename.get(key_field, key_field)}
        else:
            actual = table_field_names(token, base_app, base_table)

        key_column = rename.get(key_field, key_field)
        if key_column not in actual:
            sys.exit(
                f"发版记录表里没有主键列 {key_column!r}。"
                f"现有列：{sorted(actual)}。"
                f"改过列名就设 FEISHU_BASE_KEY_FIELD，或用 FEISHU_BASE_FIELD_MAP 映射。"
            )

        fields, skipped = adapt_fields(fields, actual, rename)
        if skipped:
            print(f"表里没有这些列，已跳过（不影响写入）：{'、'.join(skipped)}")

        record_id = upsert_base_record(
            token, base_app, base_table, key_column, fields, dry_run=args.dry_run
        )
        print(f"发版记录表已更新：{record_id or '(dry-run)'}")
    else:
        print("未配置 FEISHU_BASE_APP_TOKEN / FEISHU_BASE_TABLE_ID，跳过发版记录表")

    # 2) 群卡片。优先用应用身份直发（机器人已在群里时更好），否则退回 webhook。
    chat_id = _env("FEISHU_CHAT_ID")
    webhook = _env("FEISHU_WEBHOOK_URL")
    if args.skip_card:
        print("已指定 --skip-card，跳过群卡片")
    elif not first_publish:
        pass  # 上面已说明原因
    elif chat_id:
        send_card_as_bot(token, chat_id, build_card(ctx), dry_run=args.dry_run)
        print(f"群卡片已以应用身份发送到 {chat_id}")
    elif webhook:
        send_card(webhook, build_card(ctx), dry_run=args.dry_run)
        print("群卡片已通过 webhook 发送")
    else:
        print("未配置 FEISHU_CHAT_ID 或 FEISHU_WEBHOOK_URL，跳过群卡片")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except FeishuError as exc:
        sys.exit(f"飞书接口失败：{exc}")
