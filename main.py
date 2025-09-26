from __future__ import annotations
import os
import json
from pathlib import Path
import asyncio
from typing import List, Dict, Any, Optional

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger
import astrbot.api.message_components as Comp
import re


_AT_CQ = re.compile(r"\[CQ:at,[^\]]+\]")


def _strip_at(event) -> str:
    # 首选：基于组件拿纯文字
    parts = []
    for seg in event.get_messages():  # ← 核心修复
        if isinstance(seg, Comp.Plain):
            parts.append(seg.text)
    if parts:
        return "".join(parts).strip()

    # 兜底：部分平台只给字符串
    raw = event.message_str or ""  # 文档明确提供 message_str 属性/同名方法
    return _AT_CQ.sub("", raw).strip()


def _norm_str(s: str) -> str:
    return s.strip()


@register(
    "astrbot_plugin_nickname",
    "GrahLnn",
    "按昵称映射@成员并复读消息；支持增/删昵称与整成员记录",
    "1.0.1",
)
class NicknamePlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._initialize_basic_paths()
        self._lock = asyncio.Lock()
        self._members: List[Dict[str, Any]] = []
        self.init_task = asyncio.create_task(self._load())

    def _initialize_basic_paths(self):
        self.plugin_name_for_path = "astrbot_plugin_nickname"
        self.persistent_data_root_path = StarTools.get_data_dir(
            self.plugin_name_for_path
        )
        os.makedirs(self.persistent_data_root_path, exist_ok=True)
        logger.info(f"昵称插件的持久化数据目录: {self.persistent_data_root_path}")

        self.members_path = os.path.join(self.persistent_data_root_path, "members.json")

    async def _load(self):
        async with self._lock:
            if not os.path.exists(self.members_path):
                self._members = []
                return
            try:
                data = await asyncio.to_thread(
                    lambda: json.loads(
                        Path(self.members_path).read_text(encoding="utf-8")
                    )
                )
                self._members = data
            except Exception as e:
                logger.error(f"加载 members.json 失败: {e}")
                self._members = []

    async def _save(self):
        async with self._lock:

            def write():
                tmp = Path(self.members_path).with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(self._members, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                tmp.replace(self.members_path)

            await asyncio.to_thread(write)

    def _find_by_sid_group(self, sid: str, group_id: str) -> Optional[Dict[str, Any]]:
        for m in self._members:
            if m.get("sid") == sid and m.get("group_id") == group_id:
                return m
        return None

    def _find_all_by_nickname(
        self, nick: str, group_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        out = []
        for m in self._members:
            if group_id is not None and m.get("group_id") != group_id:
                continue
            if any(nick == _norm_str(x) for x in m.get("nickname", [])):
                out.append(m)
        return out

    def _first_at_sid(self, event: AstrMessageEvent) -> Optional[str]:
        # AstrBot 的消息链组件 At(qq=xxx)
        for seg in event.message_obj.message:
            if isinstance(seg, Comp.At):
                # qq 字段就是平台侧标识（在 QQ 协议下）
                return str(seg.qq)
        return None

    # 指令1：/member <nickname> + 一个@段
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("member")
    async def cmd_member(self, event: AstrMessageEvent, nickname: str):
        await self._load()

        gid = event.get_group_id() or ""
        if not gid:
            yield event.plain_result("仅限群聊使用。")
            return

        sid = self._first_at_sid(event)
        if not sid:
            yield event.plain_result("需要@一个成员。")
            return

        nickname = _norm_str(nickname)
        if not nickname:
            yield event.plain_result("无效昵称。")
            return

        rec = self._find_by_sid_group(sid, gid)
        if rec is None:
            rec = {"nickname": [nickname], "sid": sid, "group_id": gid}
            self._members.append(rec)
        else:
            nicks = rec.setdefault("nickname", [])
            if all(_norm_str(x) != nickname for x in nicks):
                nicks.append(nickname)

        await self._save()
        yield event.plain_result(f"已记录：{nickname} -> {sid}")
        return

    # 指令3：/rm_nick <nickname> 只移除该昵称
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rm_nick")
    async def cmd_rm_nick(self, event: AstrMessageEvent, nickname: str):
        await self._load()

        gid = event.get_group_id() or ""
        if not gid:
            yield event.plain_result("仅限群聊使用。")
            return

        nickname = _norm_str(nickname)
        touched = 0
        for rec in self._find_all_by_nickname(nickname, group_id=gid):
            nicks = rec.get("nickname", [])
            new_nicks = [x for x in nicks if _norm_str(x) != nickname]
            if len(new_nicks) != len(nicks):
                rec["nickname"] = new_nicks
                touched += 1

        if touched:
            await self._save()
            yield event.plain_result(f"已移除昵称：{nickname}（影响记录 {touched}）")
        else:
            yield event.plain_result("未找到。")
        return

    # 指令4：/rm_member <nickname> 只要有列表匹配，干掉整条记录
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("rm_member")
    async def cmd_rm_member(self, event: AstrMessageEvent, nickname: str):
        await self._load()

        gid = event.get_group_id() or ""
        if not gid:
            yield event.plain_result("仅限群聊使用。")
            return

        nickname = _norm_str(nickname)
        before = len(self._members)
        self._members = [
            rec
            for rec in self._members
            if not (
                rec.get("group_id") == gid
                and any(_norm_str(x) == nickname for x in rec.get("nickname", []))
            )
        ]
        removed = before - len(self._members)
        if removed:
            await self._save()
            yield event.plain_result(f"已删除成员记录：{removed}")
        else:
            yield event.plain_result("未找到。")
        return

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("nick_path")
    async def cmd_nick_path(self, event: AstrMessageEvent):
        yield event.plain_result(f"成员数据文件路径：{self.members_path}")
        return

    # 指令2：出现 nickname 的消息就 @ 对应 qq + 原消息
    # 仅群聊触发，避免私聊误报；优先级低于命令，确保不抢
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        raw = _strip_at(event)
        msg = raw.lower()
        if msg.startswith("/"):
            return

        await self._load()
        gid = event.get_group_id() or ""
        if not gid:
            return

        triggers = ["都来康", "都来看"]

        # --- 新增：触发全体命中 ---
        if any(trigger in msg for trigger in triggers):
            chain = []
            for rec in self._members:
                if rec.get("group_id") != gid:
                    continue
                sid = rec.get("sid")
                if not sid:
                    continue
                chain.append(Comp.At(qq=sid))
                chain.append(Comp.Plain("\u00a0"))
            logger.debug(chain)
            if chain:
                # 最后补上消息正文
                chain[-1] = Comp.Plain("\u200b\n" + msg)
                yield event.chain_result(chain)
            return
        # --- 全体命中结束 ---

        # 收集命中：sid -> 最早出现位置
        first_pos = {}  # sid -> idx
        for rec in self._members:
            if rec.get("group_id") != gid:
                continue
            sid = rec.get("sid")
            if not sid:
                continue
            for raw in rec.get("nickname", []):
                nick = _norm_str(raw)
                if not nick:
                    continue
                idx = msg.find(nick)
                if idx != -1:
                    if sid not in first_pos or idx < first_pos[sid]:
                        first_pos[sid] = idx

        if not first_pos:
            return

        # 按首次出现位置排序，保证“最早出现者优先”
        order_sids = [sid for sid, _ in sorted(first_pos.items(), key=lambda kv: kv[1])]

        # 组装消息；注意开头空格用 NBSP（\u00A0）避免被吞
        chain = []
        for sid in order_sids:
            chain.append(Comp.At(qq=sid))
            chain.append(Comp.Plain("\u00a0"))
        chain[-1] = Comp.Plain("\u200b\n" + msg)

        yield event.chain_result(chain)

    async def terminate(self):
        # 无需特殊清理
        pass
