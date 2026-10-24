"""群发表情 —— 让 bot 把表情包发到指定的 QQ 群。

跨会话发送一直卡在两个坑上：会话字符串要写成本机平台 ID 而不是平台类型，
且很多插件自带的「发送」工具只能带文字。这个插件把「挑图 + 发到群」
做成一个确定性的动作：给我群号和心情，剩下的由插件自己解析。

图库不写死：自动扫 data/plugin_data 下带 catalog.json 或 packs/ 的目录，
也可以在配置里手动追加。
"""

from __future__ import annotations

import json
import random
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_group_meme"
META_TAG_GROUPS = {
    "开心": ["开心", "高兴", "快乐", "庆祝", "点赞", "好耶", "笑"],
    "生气": ["生气", "恼火", "责备"],
    "委屈": ["委屈", "难过", "伤心", "哭", "破防"],
    "困惑": ["困惑", "发呆", "思考", "无语", "听不懂"],
    "惊讶": ["惊讶", "震惊", "害怕"],
    "害羞": ["害羞", "脸红"],
    "困": ["困了", "困倦", "睡觉", "晚安"],
    "贴贴": ["贴贴", "抱抱", "爱心", "亲亲", "喜欢"],
    "吃饭": ["吃饭", "馋", "干饭"],
    "花钱": ["花钱", "红包", "付款", "钱"],
    "打招呼": ["打招呼", "冒泡", "来了"],
    "打工": ["打工", "工作", "加班", "累"],
    "娱乐": ["娱乐", "唱歌", "游戏", "跳舞"],
    "吐槽": ["吐槽", "嫌弃", "小丑", "反讽"],
    "回应": ["回应", "摇头", "收到"],
}


IMAGE_EXTS = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
MAX_LIBRARY_ITEMS = 5000


def _now() -> float:
    return time.time()


# ---------------- 自然语言触发（不依赖大模型） ----------------
SEND_VERBS = ("发", "来", "甩", "丢", "整", "送", "给")
MEME_WORDS = ("表情包", "表情", "梗图", "meme")
GROUP_WORDS = ("群里", "群聊", "本群", "这个群", "那个群", "群")
NEGATIVE_WORDS = ("别发", "不要发", "不用发", "不想发", "别在群里", "撤回")
QUESTION_WORDS = ("吗", "么", "是不是", "能不能", "可不可以", "有没有")
GROUP_ID_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")
CALL_ME = re.compile(r"^\s*[/／#!！]")
# 粗筛：同时出现「表情/梗图」「群」「发/来/甩/丢/整」才唤醒机器人。
# regex 过滤器不受 wake_prefix 约束，所以群里不 @ 也能命中，但闲聊不会吵到它。
NATURAL_RE = r"(?=.*表情|.*梗图)(?=.*群)(?=.*(发|来|甩|丢|整))"


def parse_meme_request(text: str) -> tuple[str, str] | None:
    """从一句话里判断「要不要往群里发张表情包」。

    返回 (群号或空串, 心情)；不像这个意思就返回 None。
    纯规则判断，不走大模型 —— 模型被限流时这条通路仍然可用。
    """
    raw = str(text or "").strip()
    if not raw or CALL_ME.match(raw):
        return None
    lowered = raw.lower()
    if not any(word in lowered for word in MEME_WORDS):
        return None
    if not any(verb in raw for verb in SEND_VERBS):
        return None
    if not any(word in raw for word in GROUP_WORDS):
        return None
    if any(word in raw for word in NEGATIVE_WORDS):
        return None
    # 「群里有人在发表情包吗」这类提问不该真的发出去
    if raw.rstrip().endswith(("?", "？")) or any(word in raw for word in QUESTION_WORDS):
        return None
    match = GROUP_ID_RE.search(raw)
    hint = match.group(1) if match else ""
    mood = ""
    for label, aliases in META_TAG_GROUPS.items():
        if label in raw or any(alias in raw for alias in aliases):
            mood = label
            break
    return hint, mood


class GroupMeme(Star):
    """把表情包发到指定群：/群发表情 群号 [心情]，或直接跟她说「在群里发个开心的表情包」。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(__file__).resolve().parent / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "recent.json"
        self._state: dict[str, list[str]] = self._load_state()
        self._group_cache: tuple[float, list[dict]] = (0.0, [])
        self._library_cache: tuple[float, list[dict]] = (0.0, [])
        self._last_send: dict[str, float] = {}

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    # ---------------- 状态 ----------------
    def _load_state(self) -> dict[str, list[str]]:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            return {str(k): [str(i) for i in v][-5:] for k, v in raw.items() if isinstance(v, list)}
        except Exception:
            return {}

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        except Exception:
            logger.debug("[group_meme] 状态保存失败", exc_info=True)

    # ---------------- 群解析 ----------------
    async def _list_groups(self) -> list[dict]:
        cached_at, cached = self._group_cache
        if cached and _now() - cached_at < 300:
            return cached
        groups: list[dict] = []
        try:
            platforms = self.context.platform_manager.platform_insts
        except Exception:
            platforms = []
        for platform in platforms:
            try:
                meta = platform.meta()
                platform_id = str(getattr(meta, "id", "") or "")
                client = platform.get_client() if hasattr(platform, "get_client") else None
                api = getattr(client, "api", None)
                if api is None or not hasattr(api, "call_action"):
                    continue
                rows = await api.call_action("get_group_list") or []
                for row in rows:
                    group_id = str(row.get("group_id") or "").strip()
                    if group_id:
                        groups.append(
                            {
                                "platform_id": platform_id,
                                "group_id": group_id,
                                "name": str(row.get("group_name") or ""),
                            }
                        )
            except Exception:
                continue
        self._group_cache = (_now(), groups)
        return groups

    async def _resolve_group(self, hint: str) -> tuple[dict | None, str]:
        text = str(hint or "").strip()
        if not text:
            return None, "没给群号"
        groups = await self._list_groups()
        if not groups:
            return None, "读不到群列表：确认 QQ 适配器在线"
        for item in groups:
            if item["group_id"] == text:
                return item, ""
        for item in groups:
            if text in item["name"] or item["name"] in text:
                return item, ""
        return None, f"找不到群「{text}」；可选：{'、'.join(g['name'] for g in groups[:6])}"

    @staticmethod
    def _umo(group: dict) -> str:
        return f"{group['platform_id']}:GroupMessage:{group['group_id']}"

    # ---------------- 图库 ----------------
    def _plugin_data_root(self) -> Path:
        """data/plugin_data —— 图库默认在这里面自动发现。"""
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME)).parent
        except Exception:
            pass
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data"
        except Exception:
            return Path(__file__).resolve().parents[2]

    def _discover_sources(self) -> list[tuple[str, Path]]:
        """找出所有能用的图库：返回 [(类型, 目录)]，类型是 catalog / packs / images。

        默认不写死任何插件名：扫一遍 plugin_data，
        有 packs/ 的当表情包集合，有 catalog.json 的当带标签的图库。
        """
        root = self._plugin_data_root()
        sources: list[tuple[str, Path]] = []

        for raw in self._cfg("libraries", []) or []:
            try:
                path = Path(str(raw)).expanduser()
                if not path.is_absolute():
                    path = root / path
                if path.is_file():
                    sources.append(("catalog", path.parent))
                elif path.is_dir():
                    if (path / "catalog.json").is_file():
                        sources.append(("catalog", path))
                    elif path.name == "packs":
                        sources.append(("packs", path))
                    else:
                        sources.append(("images", path))
            except Exception:
                continue

        if self._cfg("auto_discover", True) and root.is_dir():
            try:
                plugin_dirs = [d for d in root.iterdir() if d.is_dir()]
            except OSError:
                plugin_dirs = []
            for plugin_dir in plugin_dirs:
                try:
                    if (plugin_dir / "packs").is_dir():
                        sources.append(("packs", plugin_dir / "packs"))
                    if (plugin_dir / "catalog.json").is_file():
                        sources.append(("catalog", plugin_dir))
                    for sub in [d for d in plugin_dir.iterdir() if d.is_dir()]:
                        if (sub / "catalog.json").is_file():
                            sources.append(("catalog", sub))
                except (OSError, PermissionError):
                    continue

        seen: set[tuple[str, str]] = set()
        unique: list[tuple[str, Path]] = []
        for kind, path in sources:
            key = (kind, str(path).lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append((kind, path))
        return unique

    @staticmethod
    def _describe(item: dict) -> str:
        parts: list[str] = []
        for field in ("name", "description", "category", "group"):
            value = item.get(field)
            if isinstance(value, str):
                parts.append(value)
        for field in ("tags", "emotions", "intents", "labels"):
            value = item.get(field)
            if isinstance(value, (list, tuple)):
                parts.extend(str(v) for v in value)
        return " ".join(part for part in parts if part)

    def _catalog_items(self, folder: Path) -> list[dict]:
        try:
            catalog = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
        except Exception:
            return []
        rows = None
        if isinstance(catalog, dict):
            for key in ("items", "assets", "reactions", "data"):
                value = catalog.get(key)
                if isinstance(value, list):
                    rows = value
                    break
        if rows is None:
            return []
        items: list[dict] = []
        for row in rows:
            if not isinstance(row, dict) or row.get("enabled") is False:
                continue
            raw_name = ""
            for key in ("stored_name", "file", "filename", "path", "image"):
                value = row.get(key)
                if isinstance(value, str) and value.strip():
                    raw_name = value.strip()
                    break
            if not raw_name:
                continue
            candidate = Path(raw_name)
            if not candidate.is_absolute():
                resolved = None
                for base in (folder / "images", folder):
                    probe = base / candidate
                    if probe.is_file():
                        resolved = probe
                        break
                candidate = resolved or (folder / "images" / candidate)
            if not candidate.is_file():
                continue
            items.append(
                {
                    "path": str(candidate),
                    "id": str(row.get("id") or raw_name),
                    "text": self._describe(row),
                }
            )
        return items

    def _scatter_items(self, folder: Path) -> list[dict]:
        """packs 或普通图片目录：把相对路径当标签用。"""
        try:
            files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS]
        except (OSError, PermissionError):
            return []
        items: list[dict] = []
        for path in files:
            try:
                rel = path.relative_to(folder)
            except ValueError:
                continue
            items.append({"path": str(path), "id": str(rel), "text": " ".join(rel.parts)})
        return items

    def _library_files(self) -> list[dict]:
        """汇总所有图库来源，按路径去重。"""
        now = _now()
        cached_at, cached = self._library_cache
        if cached and now - cached_at < 120:
            return cached
        items: list[dict] = []
        for kind, folder in self._discover_sources():
            if kind == "catalog":
                items.extend(self._catalog_items(folder))
            else:
                items.extend(self._scatter_items(folder))
            if len(items) > MAX_LIBRARY_ITEMS:
                break
        unique: list[dict] = []
        seen: set[str] = set()
        for item in items[:MAX_LIBRARY_ITEMS]:
            key = item["path"].lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        self._library_cache = (now, unique)
        if not unique:
            logger.warning("[group_meme] 没找到任何图库：检查 data/plugin_data 或配置 libraries")
        return unique

    def _pick(self, group_key: str, mood: str) -> dict | None:
        items = self._library_files()
        if not items:
            return None
        keywords: list[str] = []
        mood_text = str(mood or "").strip()
        if mood_text:
            keywords.append(mood_text)
            for label, aliases in META_TAG_GROUPS.items():
                if mood_text in label or any(alias in mood_text for alias in aliases):
                    keywords.extend(aliases)
                    keywords.append(label)
        keywords = list(dict.fromkeys(k for k in keywords if k))
        recent = set(self._state.get(group_key, [])[-3:])
        scored: list[tuple[int, float, dict]] = []
        for item in items:
            score = 0
            if keywords:
                haystack = item["text"]
                score = sum(3 if keyword in haystack else 0 for keyword in keywords)
                if score == 0:
                    continue
            penalty = 5 if item["id"] in recent else 0
            scored.append((score - penalty, random.random(), item))
        if not scored:
            return None
        scored.sort(key=lambda row: (-row[0], row[1]))
        chosen = scored[0][2]
        self._state.setdefault(group_key, []).append(chosen["id"])
        self._state[group_key] = self._state[group_key][-5:]
        self._save_state()
        return chosen

    # ---------------- 发送 ----------------
    async def _send(self, group: dict, chain: list[Any]) -> tuple[bool, str]:
        umo = self._umo(group)
        try:
            ok = await self.context.send_message(umo, MessageChain(chain))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[group_meme] 发送异常 {umo}: {exc}")
            return False, f"发送异常：{type(exc).__name__}: {exc}"
        return bool(ok), "" if ok else "平台没有接住这条消息"

    async def _do_send_meme(self, group_hint: str, mood: str, text: str) -> str:
        group, error = await self._resolve_group(group_hint)
        if group is None:
            return f"发送失败：{error}"
        cooldown = float(self._cfg("cooldown_seconds", 5) or 5)
        last = self._last_send.get(group["group_id"], 0.0)
        if _now() - last < cooldown:
            return f"冷却中：{int(cooldown)} 秒内只发一张，稍等一下下。"
        picked = self._pick(group["group_id"], mood)
        if picked is None:
            return "发送失败：图库里没有匹配的表情包"
        chain: list[Any] = []
        if text.strip():
            chain.append(Plain(text.strip()[:200]))
        chain.append(Image.fromFileSystem(picked["path"]))
        ok, reason = await self._send(group, chain)
        if not ok:
            return f"发送到「{group['name']}」失败：{reason}"
        self._last_send[group["group_id"]] = _now()
        return f"已发送到「{group['name']}」({group['group_id']})：{Path(picked['path']).name}"

    # ---------------- 命令 ----------------
    @filter.command("群发表情")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_send_meme(self, event: AstrMessageEvent, target: str = "", mood: str = ""):
        """把一张表情包发到指定群：/群发表情 123456789 开心"""
        result = await self._do_send_meme(target, mood, "")
        yield event.plain_result(result)

    @filter.command("群列表")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_list_groups(self, event: AstrMessageEvent):
        """列出可发送的群与正确会话写法。"""
        groups = await self._list_groups()
        if not groups:
            yield event.plain_result("读不到群列表：确认 QQ 适配器在线。")
            return
        lines = [f"共 {len(groups)} 个群："]
        for item in groups:
            lines.append(f"· {item['group_id']} {item['name'][:16]} → {self._umo(item)}")
        yield event.plain_result("\n".join(lines))

    # ---------------- 自然语言入口（确定性，不走模型） ----------------
    async def _nl_result(self, event: AstrMessageEvent) -> str | None:
        if not self._cfg("natural_trigger", True):
            return None
        parsed = parse_meme_request(getattr(event, "message_str", "") or "")
        if parsed is None:
            return None
        hint, mood = parsed
        if not hint:
            current = str(event.get_group_id() or "").strip()
            if not current:
                return "要发到哪个群？把群号告诉我，或者先用 /群列表 看可选的群。"
            hint = current
        result = await self._do_send_meme(hint, mood, "")
        if result.startswith("已发送"):
            # 记在事件上，模型随后若再调用工具就不会重复发一张
            event.set_extra("group_meme_sent", result)
        return result

    @filter.regex(NATURAL_RE)
    @filter.event_message_type(filter.EventMessageType.ALL)
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def nl_group_meme(self, event: AstrMessageEvent):
        """说一句「发张表情包」就直接发（不经过大模型）。"""
        result = await self._nl_result(event)
        if result:
            yield event.plain_result(result)

    # ---------------- 自然语言入口（大模型工具） ----------------
    @filter.llm_tool(name="send_meme_to_group")
    async def tool_send_meme(self, event: AstrMessageEvent, group: str, mood: str = "", text: str = "") -> str:
        """把一张表情包发送到指定的 QQ 群。

        Args:
            group(string): 群号或群名片段，例如 123456789。
            mood(string): 想表达的心情或氛围，例如 开心、委屈、无语；留空则随机。
            text(string): 随图一并发送的一句话，可留空。
        """
        try:
            done = event.get_extra("group_meme_sent")
            if done:
                return json.dumps(
                    {"ok": True, "message": f"{done}（这条请求已经发过了，没有重复发送）"},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"ok": True, "message": await self._do_send_meme(group, mood, text)},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"ok": False, "message": f"发送失败：{exc}"}, ensure_ascii=False)

    @filter.llm_tool(name="send_text_to_group")
    async def tool_send_text(self, event: AstrMessageEvent, group: str, text: str) -> str:
        """往指定 QQ 群发送一条纯文字消息。

        Args:
            group(string): 群号或群名片段。
            text(string): 要发送的文字。
        """
        target, error = await self._resolve_group(group)
        if target is None:
            return json.dumps({"ok": False, "message": error}, ensure_ascii=False)
        clean = re.sub(r"\s+", " ", str(text or "")).strip()[:500]
        if not clean:
            return json.dumps({"ok": False, "message": "内容为空"}, ensure_ascii=False)
        ok, reason = await self._send(target, [Plain(clean)])
        return json.dumps(
            {
                "ok": ok,
                "message": f"已发送到「{target['name']}」" if ok else f"发送失败：{reason}",
            },
            ensure_ascii=False,
        )
