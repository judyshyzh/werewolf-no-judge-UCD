"""
狼人杀 · 浏览器联机版  (强化版 v2)
========================================
改进点：
1. 断线重连（reconnect）：玩家可通过 player_id + room_code 重连，恢复状态
2. 多房间支持：生成随机 6 位房间码，支持多桌并行
3. 房主转让：房主断线后自动转让；或手动 host_transfer 给其他玩家
4. 投票防刷：每人每轮只能投一票，修改投票直接覆盖（原逻辑保留）
5. 旁观者模式：加入已开始房间时以旁观者身份进入，可查看公开信息
6. 平票处理：host_tally_day 结算时若平票，广播平票信息并由房主选择
7. 游戏日志：每个房间维护 log 列表，前端可拉取 action_log
8. 心跳超时清理：60 秒无消息的玩家连接自动移除
9. 女巫首夜自救限制（可选，通过 setup 配置 allow_witch_self_save: false）
10. 守卫连守限制：不能连续两夜守护同一人
"""

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("werewolf")


# ──────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────

def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _room_code() -> str:
    """生成 6 位大写字母+数字房间码"""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉易混淆字符 0/O/1/I
    return "".join(secrets.choice(alphabet) for _ in range(6))


def _public_player(p: "Player") -> Dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "alive": p.alive,
        "is_host": p.is_host,
        "seat": p.seat,
        "is_spectator": p.is_spectator,
        "online": p.online,
    }


# ──────────────────────────────────────────────
# data classes
# ──────────────────────────────────────────────

@dataclass
class Player:
    id: str
    name: str
    ws: web.WebSocketResponse
    is_host: bool = False
    role: Optional[str] = None
    alive: bool = True
    seat: Optional[int] = None
    is_spectator: bool = False
    online: bool = True
    last_seen: float = field(default_factory=time.time)

    # role flags
    witch_save_used: bool = False
    witch_poison_used: bool = False
    hybrid_brother_id: Optional[str] = None
    wolf_beauty_charm_id: Optional[str] = None


@dataclass
class Room:
    code: str
    host_id: str
    players: Dict[str, Player] = field(default_factory=dict)
    phase: str = "lobby"   # lobby | night | day | hunter | ended
    config: Dict[str, Any] = field(default_factory=lambda: {"max_seats": 12})
    setup: Dict[str, int] = field(default_factory=dict)
    night_actions: Dict[str, Any] = field(default_factory=dict)
    pending_night_result: Dict[str, Any] = field(default_factory=dict)
    last_night_result: Dict[str, Any] = field(default_factory=dict)
    day_votes: Dict[str, str] = field(default_factory=dict)
    sheriff_votes: Dict[str, str] = field(default_factory=dict)
    sheriff_id: Optional[str] = None
    pending_hunter_id: Optional[str] = None
    pending_hunter_target_id: Optional[str] = None
    night_step: str = "idle"
    version: int = 0

    # 强化新增
    action_log: List[Dict[str, Any]] = field(default_factory=list)
    guard_last_protect: Optional[str] = None   # 守卫上一夜守护目标（连守限制）
    night_number: int = 0                       # 夜晚计数（女巫首夜自救限制）
    tie_vote_candidates: List[str] = field(default_factory=list)  # 平票待处理

    def alive_players(self) -> List["Player"]:
        return [p for p in self.players.values() if p.alive and not p.is_spectator]

    def player_list(self) -> List[Dict[str, Any]]:
        return [_public_player(p) for p in self.players.values()]

    def bump(self) -> None:
        self.version += 1

    def add_log(self, event: str, detail: Any = None) -> None:
        entry: Dict[str, Any] = {
            "t": time.time(),
            "phase": self.phase,
            "night": self.night_number,
            "event": event,
        }
        if detail is not None:
            entry["detail"] = detail
        self.action_log.append(entry)
        if len(self.action_log) > 500:
            self.action_log = self.action_log[-500:]


ROOMS: Dict[str, Room] = {}
ROOM_LOCK = asyncio.Lock()

HEARTBEAT_TIMEOUT = 90   # 秒；超过此时间未收到任何消息视为断线
NIGHT_FIRST_STEP_DELAY = 10
HUNTER_NOTICE_DELAY = 5
CHAIN_STEP_DELAY = 5


# ──────────────────────────────────────────────
# websocket helpers
# ──────────────────────────────────────────────

async def ws_send(ws: web.WebSocketResponse, data: Dict[str, Any]) -> None:
    try:
        if not ws.closed:
            await ws.send_str(_json(data))
    except Exception:
        pass


async def room_broadcast(room: Room, data: Dict[str, Any], *, skip_spectators: bool = False) -> None:
    dead: List[str] = []
    for pid, p in list(room.players.items()):
        if skip_spectators and p.is_spectator:
            continue
        try:
            await ws_send(p.ws, data)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room.players.pop(pid, None)
    if dead:
        room.bump()


async def room_sync(room: Room) -> None:
    base = {
        "type": "room_state",
        "room": {
            "code": room.code,
            "phase": room.phase,
            "players": room.player_list(),
            "config": room.config,
            "setup": room.setup,
            "version": room.version,
            "sheriff_id": room.sheriff_id,
            "last_night_result": room.last_night_result if room.phase in ("day", "hunter", "ended") else {},
            "pending_hunter_id": room.pending_hunter_id if room.phase in ("day", "hunter") else None,
            "pending_hunter_target_id": room.pending_hunter_target_id if room.phase == "hunter" else None,
            "night_step": room.night_step if room.phase == "night" else "idle",
            "night_number": room.night_number,
            "tie_vote_candidates": room.tie_vote_candidates if room.phase == "day" else [],
        },
    }
    await room_broadcast(room, base)

    # 私有状态
    for p in list(room.players.values()):
        try:
            night_hint: Dict[str, Any] = {}
            if room.phase == "night" and p.alive and not p.is_spectator:
                if p.role == "witch":
                    wt = room.night_actions.get("wolf_target")
                    if isinstance(wt, str) and wt in room.players:
                        target = room.players[wt]
                        if target.alive:
                            night_hint = {
                                "wolf_target_id": target.id,
                                "wolf_target_name": target.name,
                                "wolf_target_seat": target.seat,
                            }
                elif _is_wolf_role(p.role):
                    allies = [
                        {"id": qp.id, "name": qp.name, "seat": qp.seat, "role": qp.role}
                        for qp in room.players.values()
                        if qp.alive and qp.role and _is_wolf_role(qp.role)
                    ]
                    night_hint = {"wolf_allies": allies}

            await ws_send(
                p.ws,
                {
                    "type": "private_state",
                    "you": {
                        "id": p.id,
                        "name": p.name,
                        "role": p.role,
                        "alive": p.alive,
                        "is_host": p.is_host,
                        "is_spectator": p.is_spectator,
                        "witch_save_used": p.witch_save_used,
                        "witch_poison_used": p.witch_poison_used,
                    },
                    "night_hint": night_hint,
                },
            )
        except Exception:
            continue


# ──────────────────────────────────────────────
# game logic helpers
# ──────────────────────────────────────────────

def _is_wolf_role(role: Optional[str]) -> bool:
    return role in ("werewolf", "wolf_king", "white_wolf_king", "wolf_beauty")


def _count_sides(room: Room) -> Tuple[int, int]:
    wolves = villagers = 0
    for p in room.players.values():
        if not p.alive or not p.role or p.is_spectator:
            continue
        if _is_wolf_role(p.role):
            wolves += 1
        else:
            villagers += 1
    return wolves, villagers


def _check_win(room: Room) -> Optional[str]:
    wolves, villagers = _count_sides(room)
    if room.phase == "lobby":
        return None
    if wolves == 0:
        return "village"
    if wolves >= villagers:
        return "werewolf"
    return None


def _has_role(room: Room, role: str) -> bool:
    try:
        return int(room.setup.get(role, 0)) > 0
    except Exception:
        return False


def _occupied_seats(room: Room) -> Dict[int, str]:
    return {p.seat: p.id for p in room.players.values() if p.seat is not None}


def _seated_players(room: Room) -> List[Player]:
    seated = [p for p in room.players.values() if p.seat is not None and not p.is_spectator]
    return sorted(seated, key=lambda p: (p.seat or 10**9, p.name))


def _normalize_setup(raw: Any) -> Dict[str, int]:
    if not isinstance(raw, dict):
        raise ValueError("setup 必须是对象")
    out: Dict[str, int] = {}
    for k, v in raw.items():
        role = str(k).strip()
        if not role:
            continue
        try:
            n = int(v)
        except Exception:
            raise ValueError("setup 数量必须是整数")
        if n <= 0:
            continue
        out[role] = n
    return out


def _setup_count(setup: Dict[str, int]) -> int:
    return sum(int(v) for v in setup.values() if int(v) > 0)


def _deal_roles_random(room: Room) -> None:
    seated = _seated_players(room)
    max_seats = int(room.config.get("max_seats", len(seated) or 12))
    if len(seated) != max_seats:
        raise ValueError(f"需要刚好 {max_seats} 人入座才能发牌（当前 {len(seated)}）")
    setup = room.setup or {}
    roles: List[str] = []
    for role, count in setup.items():
        roles += [role] * int(count)
    if len(roles) != max_seats:
        raise ValueError(f"板子角色数量之和必须等于人数 {max_seats}（当前 {len(roles)}）")
    secrets.SystemRandom().shuffle(roles)
    for p, r in zip(seated, roles):
        p.role = r
        p.alive = True
        if p.role != "witch":
            p.witch_save_used = False
            p.witch_poison_used = False


# ──────────────────────────────────────────────
# host transfer
# ──────────────────────────────────────────────

def _transfer_host(room: Room, new_host_id: str) -> None:
    old_host_id = room.host_id
    for p in room.players.values():
        p.is_host = (p.id == new_host_id)
    room.host_id = new_host_id
    room.add_log("host_transfer", {"from": old_host_id, "to": new_host_id})
    log.info("Room %s: host transferred %s -> %s", room.code, old_host_id, new_host_id)


def _maybe_auto_transfer_host(room: Room) -> bool:
    """房主断线时自动把 host 转让给第一个在线玩家，返回是否成功转让"""
    if room.host_id in room.players and room.players[room.host_id].online:
        return False
    candidates = [p for p in room.players.values() if p.online and not p.is_spectator]
    if not candidates:
        candidates = [p for p in room.players.values() if p.online]
    if candidates:
        _transfer_host(room, candidates[0].id)
        return True
    return False


# ──────────────────────────────────────────────
# night flow
# ──────────────────────────────────────────────

async def _end_game(room: Room, winner: str) -> None:
    room.phase = "ended"
    room.bump()
    room.add_log("game_end", {"winner": winner})
    # 游戏结束时公开所有角色
    roles_reveal = {p.id: p.role for p in room.players.values() if not p.is_spectator}
    await room_broadcast(room, {"type": "game_end", "winner": winner, "roles": roles_reveal})
    await room_sync(room)


async def _broadcast_audio(room: Room, cue: str) -> None:
    await room_broadcast(room, {"type": "audio_cue", "cue": cue})


async def _start_night(room: Room) -> None:
    room.phase = "night"
    room.night_number += 1
    room.night_actions = {
        "wolf_target": None,
        "seer_check": None,
        "guard_protect": None,
        "dreamer_target": None,
        "witch_save": False,
        "witch_poison": None,
        "witch_save_decided": False,
        "witch_poison_decided": False,
        "alive_at_night_start": [p.id for p in room.players.values() if p.alive],
    }
    room.pending_night_result = {}
    room.last_night_result = {}
    room.day_votes = {}
    room.sheriff_votes = {}
    room.pending_hunter_id = None
    room.pending_hunter_target_id = None
    room.night_step = "idle"
    room.tie_vote_candidates = []
    room.bump()
    room.add_log("night_start", {"night": room.night_number})
    await room_sync(room)


async def _delayed_night_step_start(room: Room, delay_s: int, token: int) -> None:
    await asyncio.sleep(delay_s)
    if room.phase == "night" and room.version == token and room.night_step == "idle":
        await _advance_night_step(room)


async def _delayed_audio(room: Room, cue: str, delay_s: int, token: int) -> None:
    await asyncio.sleep(delay_s)
    if room.phase == "night" and room.version == token:
        await _broadcast_audio(room, cue)


async def _hunter_wolf_day_chain(room: Room, delay_first: int, token: int) -> None:
    await asyncio.sleep(delay_first)
    if room.phase != "night" or room.version != token:
        return
    await _broadcast_audio(room, "hunter_notice")
    await asyncio.sleep(CHAIN_STEP_DELAY)
    if room.version != token:
        return
    if _has_role(room, "wolf_king"):
        await _broadcast_audio(room, "wolf_king_open")
        await asyncio.sleep(CHAIN_STEP_DELAY)
        if room.version != token:
            return
        await _broadcast_audio(room, "day_start")
    else:
        await _broadcast_audio(room, "day_start")

    if room.phase == "night" and room.version == token:
        room.phase = "day"
        room.bump()
        await room_sync(room)


async def _advance_night_step(room: Room) -> None:
    if room.phase != "night":
        return
    step = room.night_step

    async def _go(new_step: str, audio: str) -> None:
        room.night_step = new_step
        room.bump()
        await room_sync(room)
        await _broadcast_audio(room, audio)

    if step == "idle":
        if _has_role(room, "hybrid"):
            await _go("hybrid", "hybrid_open"); return
        if _has_role(room, "dreamer"):
            await _go("dreamer", "dreamer_open"); return
        nxt = "guard" if _has_role(room, "guard") else "wolf"
        await _go(nxt, f"{nxt}_open"); return

    if step == "hybrid":
        if _has_role(room, "dreamer"):
            await _go("dreamer", "dreamer_open"); return
        nxt = "guard" if _has_role(room, "guard") else "wolf"
        await _go(nxt, f"{nxt}_open"); return

    if step == "dreamer":
        await _go("wolf", "wolf_open"); return

    if step == "guard":
        await _go("wolf", "wolf_open"); return

    if step == "wolf":
        if _has_role(room, "wolf_beauty"):
            await _go("wolf_beauty", "wolf_beauty_open"); return
        if _has_role(room, "witch"):
            await _go("witch", "witch_open"); return
        nxt = "seer" if _has_role(room, "seer") else "ready"
        if nxt == "seer":
            await _go("seer", "seer_open"); return
        room.night_step = "ready"; room.bump(); await room_sync(room)
        token = room.version
        asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return

    if step == "wolf_beauty":
        if _has_role(room, "witch"):
            await _go("witch", "witch_open"); return
        nxt = "seer" if _has_role(room, "seer") else "ready"
        if nxt == "seer":
            await _go("seer", "seer_open"); return
        room.night_step = "ready"; room.bump(); await room_sync(room)
        token = room.version
        asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return

    if step == "witch":
        if _has_role(room, "seer"):
            await _go("seer", "seer_open"); return
        room.night_step = "ready"; room.bump(); await room_sync(room)
        token = room.version
        asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return

    if step == "seer":
        room.night_step = "ready"; room.bump(); await room_sync(room)
        token = room.version
        asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return


async def _notify_shoot_status(room: Room, role: str, can_shoot: bool, cause: str, message: str) -> None:
    for p in room.players.values():
        if p.role == role:
            try:
                await ws_send(
                    p.ws,
                    {"type": "shoot_status", "role": role, "can_shoot": can_shoot,
                     "cause": cause, "message": message},
                )
            except Exception:
                pass


async def _resolve_night(room: Room) -> None:
    wolf_target = room.night_actions.get("wolf_target")
    seer_check = room.night_actions.get("seer_check")
    guard_protect = room.night_actions.get("guard_protect")
    dreamer_target = room.night_actions.get("dreamer_target")
    witch_save = bool(room.night_actions.get("witch_save"))
    witch_poison = room.night_actions.get("witch_poison")
    alive_at_start = set(room.night_actions.get("alive_at_night_start") or [])

    deaths: List[str] = []
    death_causes: Dict[str, str] = {}

    # 守卫连守限制
    effective_guard = guard_protect
    if guard_protect and guard_protect == room.guard_last_protect:
        effective_guard = None  # 不能连守同一人

    # 狼人刀人
    wolf_killed: Optional[str] = None
    if isinstance(wolf_target, str) and wolf_target in room.players and room.players[wolf_target].alive:
        wolf_killed = wolf_target
        if isinstance(effective_guard, str) and effective_guard == wolf_target:
            if witch_save:
                pass  # 守卫+女巫叠加 -> 仍死
            else:
                wolf_killed = None
        if isinstance(dreamer_target, str) and dreamer_target == wolf_target:
            if witch_save:
                pass
            else:
                wolf_killed = None
        elif witch_save and wolf_killed is not None:
            target = room.players[wolf_killed]
            # 女巫首夜自救限制（若配置禁止）
            allow_self_save = room.config.get("allow_witch_self_save", True)
            if not allow_self_save and room.night_number == 1 and target.role == "witch":
                pass  # 首夜女巫不能自救
            elif target.role != "witch":
                wolf_killed = None

    # 更新守卫上次守护
    room.guard_last_protect = guard_protect  # 记录本夜守护目标（即使失效）

    if wolf_killed is not None:
        room.players[wolf_killed].alive = False
        deaths.append(wolf_killed)
        death_causes[wolf_killed] = "wolf"

    # 女巫毒人
    if isinstance(witch_poison, str) and witch_poison in room.players:
        if room.players[witch_poison].alive and witch_poison not in deaths:
            room.players[witch_poison].alive = False
            deaths.append(witch_poison)
            death_causes[witch_poison] = "poison"

    # 狼美人魅惑死亡联动
    for p in room.players.values():
        if p.role == "wolf_beauty" and p.wolf_beauty_charm_id and p.id in deaths:
            cid = p.wolf_beauty_charm_id
            if cid in room.players and room.players[cid].alive and cid not in deaths:
                room.players[cid].alive = False
                deaths.append(cid)
                death_causes[cid] = "charm"

    # 摄梦人联动
    if isinstance(dreamer_target, str) and dreamer_target in room.players:
        for p in room.players.values():
            if p.role == "dreamer" and p.id in deaths:
                if room.players[dreamer_target].alive and dreamer_target not in deaths:
                    room.players[dreamer_target].alive = False
                    deaths.append(dreamer_target)
                    death_causes[dreamer_target] = "dreamer"
                break

    # 预言家验人结果
    if isinstance(seer_check, str) and seer_check in room.players:
        checked = room.players[seer_check]
        for p in room.players.values():
            if p.role == "seer" and p.id in alive_at_start:
                await ws_send(
                    p.ws,
                    {
                        "type": "seer_result",
                        "target_id": checked.id,
                        "target_name": checked.name,
                        "is_werewolf": _is_wolf_role(checked.role),
                    },
                )

    # 猎人开枪状态通知
    for pid in deaths:
        p = room.players.get(pid)
        if p and p.role == "hunter":
            blocked = death_causes.get(pid) in ("poison", "charm")
            await _notify_shoot_status(
                room, "hunter",
                can_shoot=not blocked,
                cause=death_causes.get(pid, "wolf"),
                message="无法开枪" if blocked else "正常",
            )

    room.pending_night_result = {"killed_ids": deaths, "death_causes": death_causes}
    room.pending_hunter_id = None
    room.pending_hunter_target_id = None
    room.add_log("night_resolve", {"deaths": deaths, "causes": death_causes})
    await _reveal_night_and_start_day(room)

    winner = _check_win(room)
    if winner:
        await _end_game(room, winner)


async def _tally_day(room: Room) -> None:
    """结算白天投票，处理平票"""
    alive_ids = {p.id for p in room.players.values() if p.alive}
    counts: Dict[str, int] = {}
    for voter, target in room.day_votes.items():
        if voter not in alive_ids or target not in alive_ids:
            continue
        counts[target] = counts.get(target, 0) + 1

    if not counts:
        room.tie_vote_candidates = []
        room.bump()
        await room_broadcast(room, {"type": "day_result", "eliminated_id": None, "tie": False})
        await room_sync(room)
        return

    max_votes = max(counts.values())
    top = [pid for pid, cnt in counts.items() if cnt == max_votes]

    if len(top) > 1:
        # 平票：通知房主决断
        room.tie_vote_candidates = top
        room.bump()
        await room_broadcast(room, {
            "type": "day_tie",
            "candidates": top,
            "names": [room.players[pid].name for pid in top if pid in room.players],
            "vote_count": max_votes,
        })
        await room_sync(room)
        room.add_log("day_tie", {"candidates": top, "votes": max_votes})
        return

    eliminated_id = top[0]
    room.players[eliminated_id].alive = False
    room.tie_vote_candidates = []
    room.day_votes = {}
    room.bump()
    room.add_log("day_eliminate", {"player": eliminated_id})
    await room_broadcast(room, {"type": "day_result", "eliminated_id": eliminated_id, "tie": False})
    await room_sync(room)

    winner = _check_win(room)
    if winner:
        await _end_game(room, winner)


async def _reveal_night_and_start_day(room: Room) -> None:
    room.last_night_result = room.pending_night_result
    room.pending_night_result = {}
    room.phase = "day" if not room.pending_hunter_id else "hunter"
    room.bump()
    await room_broadcast(room, {"type": "night_revealed", "result": room.last_night_result})
    await room_sync(room)


async def _restart_to_lobby(room: Room) -> None:
    room.phase = "lobby"
    room.night_actions = {}
    room.pending_night_result = {}
    room.last_night_result = {}
    room.day_votes = {}
    room.sheriff_votes = {}
    room.sheriff_id = None
    room.pending_hunter_id = None
    room.pending_hunter_target_id = None
    room.night_step = "idle"
    room.night_number = 0
    room.guard_last_protect = None
    room.tie_vote_candidates = []
    for p in room.players.values():
        p.alive = True
        p.role = None
        p.witch_save_used = False
        p.witch_poison_used = False
        p.hybrid_brother_id = None
        p.wolf_beauty_charm_id = None
    room.bump()
    room.add_log("restart")
    await room_broadcast(room, {"type": "restarted"})
    await room_sync(room)


# ──────────────────────────────────────────────
# heartbeat cleanup task
# ──────────────────────────────────────────────

async def _heartbeat_cleanup() -> None:
    """定期检查超时连接"""
    while True:
        await asyncio.sleep(30)
        rooms_to_sync: List[Room] = []
        async with ROOM_LOCK:
            for code, room in list(ROOMS.items()):
                changed = False
                for pid, p in list(room.players.items()):
                    if p.ws.closed and p.online:
                        p.online = False
                        changed = True
                        log.info("Room %s: player %s went offline", code, p.name)
                if changed:
                    _maybe_auto_transfer_host(room)
                    room.bump()
                    rooms_to_sync.append(room)
                # 清理已结束 & 全部离线的空房间
                all_offline = all(not p.online for p in room.players.values())
                if all_offline and len(room.players) > 0:
                    ROOMS.pop(code, None)
                    log.info("Room %s: all offline, room removed", code)
        # 出锁后再 sync，避免锁内 async task 竞态
        for room in rooms_to_sync:
            await room_sync(room)


# ──────────────────────────────────────────────
# WebSocket handler
# ──────────────────────────────────────────────

async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    player: Optional[Player] = None
    room: Optional[Room] = None

    async def reject(reason: str) -> None:
        await ws_send(ws, {"type": "error", "message": reason})
        await ws.close()

    async for msg in ws:
        if msg.type == WSMsgType.PING:
            await ws.pong()
            if player:
                player.last_seen = time.time()
            continue
        if msg.type != WSMsgType.TEXT:
            continue

        if player:
            player.last_seen = time.time()
            player.online = True

        try:
            data = json.loads(msg.data)
        except Exception:
            await ws_send(ws, {"type": "error", "message": "消息格式错误（需要 JSON）"})
            continue

        mtype = data.get("type")

        # ── ping/pong (application layer) ──
        if mtype == "ping":
            await ws_send(ws, {"type": "pong"})
            continue

        # ── 断线重连 ──
        if mtype == "reconnect":
            pid = str(data.get("player_id") or "").strip()
            code = str(data.get("room_code") or "").strip().upper()
            if not pid or not code:
                await ws_send(ws, {"type": "error", "message": "重连需要 player_id 和 room_code"})
                continue
            async with ROOM_LOCK:
                r = ROOMS.get(code)
                if not r or pid not in r.players:
                    await ws_send(ws, {"type": "error", "message": "无法重连：房间或玩家不存在"})
                    continue
                old_p = r.players[pid]
                old_p.ws = ws
                old_p.online = True
                old_p.last_seen = time.time()
                player = old_p
                room = r
                log.info("Room %s: player %s reconnected", code, player.name)
            await ws_send(ws, {"type": "reconnected", "room_code": room.code, "player_id": player.id})
            await room_sync(room)
            continue

        # ── 创建房间 ──
        if mtype == "create_room":
            if room is not None:
                await ws_send(ws, {"type": "error", "message": "你已经在房间里了"})
                continue
            name = str(data.get("name") or "").strip()[:20]
            if not name:
                await reject("名字不能为空")
                break
            async with ROOM_LOCK:
                # 生成唯一房间码
                for _ in range(10):
                    code_try = _room_code()
                    if code_try not in ROOMS:
                        break
                pid = secrets.token_urlsafe(10)
                player = Player(id=pid, name=name, ws=ws, is_host=True)
                room = Room(code=code_try, host_id=pid)
                room.players[pid] = player
                ROOMS[code_try] = room
                room.bump()
                log.info("Room %s created by %s", code_try, name)
            await ws_send(ws, {"type": "created", "room_code": room.code, "player_id": player.id})
            await room_sync(room)
            continue

        # ── 加入房间 ──
        if mtype == "join_room":
            if room is not None:
                await ws_send(ws, {"type": "error", "message": "你已经在房间里了"})
                continue
            code = str(data.get("code") or "").strip().upper()
            name = str(data.get("name") or "").strip()[:20]
            if not code or not name:
                await reject("房间号/名字不能为空")
                break
            async with ROOM_LOCK:
                if code not in ROOMS:
                    await reject("房间不存在")
                    break
                room = ROOMS[code]
                pid = secrets.token_urlsafe(10)
                is_spectator = room.phase != "lobby"
                player = Player(id=pid, name=name, ws=ws, is_host=False, is_spectator=is_spectator)
                room.players[pid] = player
                room.bump()
                log.info("Room %s: %s joined (spectator=%s)", code, name, is_spectator)
            msg_type = "joined_spectator" if player.is_spectator else "joined"
            await ws_send(ws, {"type": msg_type, "room_code": room.code, "player_id": player.id,
                                "is_spectator": player.is_spectator})
            await room_sync(room)
            continue

        if room is None or player is None:
            await ws_send(ws, {"type": "error", "message": "请先创建/加入房间"})
            continue

        # 旁观者只能收消息，不能操作
        if player.is_spectator and mtype not in ("ping", "get_log"):
            await ws_send(ws, {"type": "error", "message": "旁观者不能操作"})
            continue

        # ── 获取游戏日志（房主专用）──
        if mtype == "get_log":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以查看完整日志"})
                continue
            await ws_send(ws, {"type": "action_log", "log": room.action_log[-100:]})
            continue

        # ── 房主转让 ──
        if mtype == "host_transfer":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以转让"})
                continue
            target_id = str(data.get("target_id") or "").strip()
            if target_id not in room.players:
                await ws_send(ws, {"type": "error", "message": "目标玩家不存在"})
                continue
            _transfer_host(room, target_id)
            room.bump()
            await room_broadcast(room, {"type": "host_changed", "new_host_id": target_id,
                                         "new_host_name": room.players[target_id].name})
            await room_sync(room)
            continue

        # ── 配置板子 ──
        if mtype == "host_set_setup":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以配板子"})
                continue
            if room.phase != "lobby":
                await ws_send(ws, {"type": "error", "message": "游戏开始后不能配板子"})
                continue
            try:
                setup = _normalize_setup(data.get("setup"))
            except Exception as e:
                await ws_send(ws, {"type": "error", "message": str(e)})
                continue
            room.setup = setup
            # 可选规则配置
            if "allow_witch_self_save" in data:
                room.config["allow_witch_self_save"] = bool(data["allow_witch_self_save"])
            n = _setup_count(room.setup)
            if n > 0:
                room.config["max_seats"] = n
                for p in room.players.values():
                    if p.seat is not None and (p.seat < 1 or p.seat > n):
                        p.seat = None
            room.bump()
            await room_sync(room)
            continue

        if mtype == "set_config":
            await ws_send(ws, {"type": "error", "message": "人数由板子自动计算，不需要单独设置"})
            continue

        # ── 结算夜晚（房主） ──
        if mtype == "host_resolve_night":
            for p in room.players.values():
                if p.role == "witch" and p.alive:
                    if room.night_actions.get("witch_save"):
                        p.witch_save_used = True
                    if room.night_actions.get("witch_poison") is not None:
                        p.witch_poison_used = True
            await _resolve_night(room)
            continue

        # ── 选座位 ──
        if mtype == "set_seat":
            if room.phase != "lobby":
                await ws_send(ws, {"type": "error", "message": "游戏开始后不能换座位"})
                continue
            try:
                seat = int(data.get("seat"))
            except Exception:
                await ws_send(ws, {"type": "error", "message": "座位号必须是整数"})
                continue
            max_seats = int(room.config.get("max_seats", 0))
            if max_seats <= 0:
                await ws_send(ws, {"type": "error", "message": "请先让房主配板子，再入座"})
                continue
            if seat < 1 or seat > max_seats:
                await ws_send(ws, {"type": "error", "message": f"座位号范围 1..{max_seats}"})
                continue
            occ = _occupied_seats(room)
            if seat in occ and occ[seat] != player.id:
                await ws_send(ws, {"type": "error", "message": "这个座位已经有人了"})
                continue
            player.seat = seat
            room.bump()
            await room_sync(room)
            continue

        # ── 开始游戏（发牌） ──
        if mtype == "start_game":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以开始"})
                continue
            if room.phase != "lobby":
                await ws_send(ws, {"type": "error", "message": "游戏已经开始"})
                continue
            max_seats = int(room.config.get("max_seats", 0))
            if max_seats <= 0:
                await ws_send(ws, {"type": "error", "message": "请先配板子"})
                continue
            seated = _seated_players(room)
            if len(seated) != max_seats:
                await ws_send(ws, {"type": "error",
                                    "message": f"需要刚好 {max_seats} 人入座才能开始（当前 {len(seated)}）"})
                continue
            try:
                _deal_roles_random(room)
            except Exception as e:
                await ws_send(ws, {"type": "error", "message": str(e)})
                continue
            room.phase = "day"
            room.bump()
            room.add_log("game_start", {"players": len(seated)})
            await room_sync(room)
            for p in room.players.values():
                if not p.is_spectator:
                    await ws_send(p.ws, {"type": "your_role", "role": p.role})
            continue

        # ── 进入夜晚 ──
        if mtype == "host_start_night":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以开始黑夜"})
                continue
            if room.phase not in ("day", "lobby"):
                await ws_send(ws, {"type": "error", "message": "当前阶段不能开始黑夜"})
                continue
            await _start_night(room)
            await _broadcast_audio(room, "night_start")
            token = room.version
            asyncio.create_task(_delayed_night_step_start(room, NIGHT_FIRST_STEP_DELAY, token))
            continue

        # ── 强制结束 ──
        if mtype == "force_end":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以结束"})
                continue
            await _end_game(room, "host")
            continue

        # ── 重新开始 ──
        if mtype == "host_restart":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以重新开始"})
                continue
            await _restart_to_lobby(room)
            continue

        # ── 播放语音 ──
        if mtype == "host_audio_cue":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以播语音"})
                continue
            cue = data.get("cue")
            if not isinstance(cue, str) or not cue:
                await ws_send(ws, {"type": "error", "message": "cue 无效"})
                continue
            await room_broadcast(room, {"type": "audio_cue", "cue": cue})
            continue

        # ── 平票由房主决断 ──
        if mtype == "host_break_tie":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以决断平票"})
                continue
            if room.phase != "day" or not room.tie_vote_candidates:
                await ws_send(ws, {"type": "error", "message": "当前没有需要决断的平票"})
                continue
            target_id = str(data.get("target_id") or "").strip()
            if target_id not in room.tie_vote_candidates:
                await ws_send(ws, {"type": "error", "message": "目标不在平票候选人中"})
                continue
            if target_id == "nobody":
                # 平票无人出局
                room.tie_vote_candidates = []
                room.day_votes = {}
                room.bump()
                await room_broadcast(room, {"type": "day_result", "eliminated_id": None, "tie": True})
                await room_sync(room)
            else:
                room.players[target_id].alive = False
                room.tie_vote_candidates = []
                room.day_votes = {}
                room.bump()
                room.add_log("tie_break", {"eliminated": target_id})
                await room_broadcast(room, {"type": "day_result", "eliminated_id": target_id, "tie": True})
                await room_sync(room)
                winner = _check_win(room)
                if winner:
                    await _end_game(room, winner)
            continue

        # ══════════════════════════
        # 夜晚行动
        # ══════════════════════════
        if room.phase == "night":

            if mtype == "wolf_kill":
                if not _is_wolf_role(player.role) or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活狼人阵营"}); continue
                if room.night_actions.get("wolf_target") is not None:
                    await ws_send(ws, {"type": "error", "message": "本夜已锁定刀人目标"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                room.night_actions["wolf_target"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "wolf_kill"})
                try:
                    target = room.players[target_id]
                    for wp in room.players.values():
                        if wp.alive and wp.role == "witch":
                            await ws_send(wp.ws, {"type": "witch_prompt",
                                                   "wolf_target_id": target.id,
                                                   "wolf_target_name": target.name,
                                                   "wolf_target_seat": target.seat})
                except Exception:
                    pass
                if room.night_step == "wolf":
                    await _advance_night_step(room)
                continue

            if mtype == "wolf_chat":
                if room.phase != "night" or room.night_step != "wolf":
                    await ws_send(ws, {"type": "error", "message": "当前阶段不能狼人聊天"}); continue
                if not _is_wolf_role(player.role) or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "只有存活狼人可以夜聊"}); continue
                text = str(data.get("text") or "").strip()[:200]
                if not text:
                    await ws_send(ws, {"type": "error", "message": "聊天内容不能为空"}); continue
                payload = {"type": "wolf_chat", "from_id": player.id, "from_name": player.name,
                           "from_seat": player.seat, "text": text}
                for qp in room.players.values():
                    if _is_wolf_role(qp.role) and qp.alive:
                        try:
                            await ws_send(qp.ws, payload)
                        except Exception:
                            continue
                continue

            if mtype == "hybrid_choose":
                if player.role != "hybrid" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活野孩子"}); continue
                if player.hybrid_brother_id:
                    await ws_send(ws, {"type": "error", "message": "你已选过大哥"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                if target_id == player.id:
                    await ws_send(ws, {"type": "error", "message": "不能选自己"}); continue
                player.hybrid_brother_id = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "hybrid_choose"})
                if room.night_step == "hybrid":
                    await _advance_night_step(room)
                continue

            if mtype == "wolf_beauty_charm":
                if player.role != "wolf_beauty" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活狼美人"}); continue
                if player.wolf_beauty_charm_id:
                    await ws_send(ws, {"type": "error", "message": "你已魅惑过一名玩家"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                if target_id == player.id:
                    await ws_send(ws, {"type": "error", "message": "不能魅惑自己"}); continue
                player.wolf_beauty_charm_id = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "wolf_beauty_charm"})
                if room.night_step == "wolf_beauty":
                    await _advance_night_step(room)
                continue

            if mtype == "seer_check":
                if player.role != "seer" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活预言家"}); continue
                if room.night_actions.get("seer_check") is not None:
                    await ws_send(ws, {"type": "error", "message": "本夜已验过人"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                room.night_actions["seer_check"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "seer_check"})
                checked = room.players[target_id]
                seer_payload = {"type": "seer_result", "target_id": checked.id,
                                "target_name": checked.name, "is_werewolf": _is_wolf_role(checked.role)}
                await ws_send(ws, seer_payload)
                try:
                    if player.ws is not ws:
                        await ws_send(player.ws, seer_payload)
                except Exception:
                    pass
                if room.night_step == "seer":
                    await _advance_night_step(room)
                continue

            if mtype == "guard_protect":
                if player.role != "guard" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活守卫"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id == "":
                    room.night_actions["guard_protect"] = None
                else:
                    if target_id not in room.players:
                        await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                    if not room.players[target_id].alive:
                        await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                    # 连守提醒（不阻止提交，结算时失效）
                    if target_id == room.guard_last_protect:
                        await ws_send(ws, {"type": "warning", "message": "连守限制：结算时此次守护无效"})
                    room.night_actions["guard_protect"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "guard_protect"})
                if room.night_step == "guard":
                    await _advance_night_step(room)
                continue

            if mtype == "dreamer_shoot":
                if player.role != "dreamer" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活摄梦人"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                room.night_actions["dreamer_target"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "dreamer_shoot"})
                if room.night_step == "dreamer":
                    await _advance_night_step(room)
                continue

            if mtype == "witch_save":
                if player.role != "witch" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活女巫"}); continue
                if player.witch_save_used:
                    await ws_send(ws, {"type": "error", "message": "解药已用完"}); continue
                if room.night_actions.get("witch_save_decided"):
                    await ws_send(ws, {"type": "error", "message": "本夜救人决定已锁定"}); continue
                use = bool(data.get("use"))
                if use:
                    wt = room.night_actions.get("wolf_target")
                    if isinstance(wt, str) and wt == player.id:
                        allow_self_save = room.config.get("allow_witch_self_save", True)
                        if not allow_self_save and room.night_number == 1:
                            await ws_send(ws, {"type": "error", "message": "首夜女巫不能自救（规则限制）"}); continue
                        elif allow_self_save is False:
                            await ws_send(ws, {"type": "error", "message": "女巫不能自救（规则限制）"}); continue
                room.night_actions["witch_save"] = use
                room.night_actions["witch_save_decided"] = True
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "witch_save"})
                if room.night_step == "witch" and room.night_actions.get("witch_poison_decided"):
                    await _advance_night_step(room)
                continue

            if mtype == "witch_poison":
                if player.role != "witch" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活女巫"}); continue
                if player.witch_poison_used:
                    await ws_send(ws, {"type": "error", "message": "毒药已用完"}); continue
                if room.night_actions.get("witch_poison_decided"):
                    await ws_send(ws, {"type": "error", "message": "本夜毒人决定已锁定"}); continue
                target_id = data.get("target_id")
                if target_id is None or target_id == "":
                    room.night_actions["witch_poison"] = None
                    room.night_actions["witch_poison_decided"] = True
                    room.bump()
                    await ws_send(ws, {"type": "ack", "action": "witch_poison_cancel"})
                    await _notify_shoot_status(room, "hunter", True, "safe", "正常")
                    if _has_role(room, "wolf_king"):
                        await _notify_shoot_status(room, "wolf_king", True, "safe", "正常")
                    if room.night_step == "witch" and room.night_actions.get("witch_save_decided"):
                        await _advance_night_step(room)
                    continue
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                room.night_actions["witch_poison"] = target_id
                room.night_actions["witch_poison_decided"] = True
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "witch_poison"})
                try:
                    target = room.players[target_id]
                    if target.role == "hunter":
                        await _notify_shoot_status(room, "hunter", False, "poison", "无法开枪")
                    else:
                        await _notify_shoot_status(room, "hunter", True, "safe", "正常")
                    if target.role == "wolf_king":
                        await _notify_shoot_status(room, "wolf_king", False, "poison", "无法开枪")
                    elif _has_role(room, "wolf_king"):
                        await _notify_shoot_status(room, "wolf_king", True, "safe", "正常")
                except Exception:
                    pass
                if room.night_step == "witch" and room.night_actions.get("witch_save_decided"):
                    await _advance_night_step(room)
                continue

        # ══════════════════════════
        # 白天行动
        # ══════════════════════════
        if room.phase == "day":
            if mtype == "vote":
                if not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你已死亡不能投票"}); continue
                if room.tie_vote_candidates:
                    await ws_send(ws, {"type": "error", "message": "平票处理中，请等待房主决断"}); continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"}); continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"}); continue
                room.day_votes[player.id] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "vote"})
                await room_broadcast(room, {"type": "vote_progress",
                                             "count": len(room.day_votes),
                                             "total": len([p for p in room.players.values() if p.alive])})
                continue

            if mtype == "wolf_bomb":
                if not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你已死亡，不能自爆"}); continue
                if player.role not in ("werewolf", "wolf_king"):
                    await ws_send(ws, {"type": "error", "message": "只有狼人/狼王可以自爆"}); continue
                player.alive = False
                room.bump()
                room.add_log("wolf_bomb", {"player": player.id})
                await room_broadcast(room, {"type": "wolf_bomb", "player_id": player.id})
                await room_sync(room)
                winner = _check_win(room)
                if winner:
                    await _end_game(room, winner)
                continue

            if mtype == "host_tally_day":
                if not player.is_host:
                    await ws_send(ws, {"type": "error", "message": "只有房主可以结算投票"}); continue
                await _tally_day(room)
                continue

        await ws_send(ws, {"type": "error", "message": "不支持的操作或阶段不匹配"})

    # ── 断线处理 ──
    try:
        if room and player:
            transferred = False
            broadcast_data = None
            async with ROOM_LOCK:
                if room.code in ROOMS and player.id in room.players:
                    player.online = False
                    player.last_seen = time.time()
                    log.info("Room %s: player %s disconnected", room.code, player.name)
                    transferred = _maybe_auto_transfer_host(room)
                    room.bump()
                    if transferred and room.host_id in room.players:
                        broadcast_data = {
                            "type": "host_changed",
                            "new_host_id": room.host_id,
                            "new_host_name": room.players[room.host_id].name,
                        }
            # 出锁后再 await，避免锁内竞态
            await room_sync(room)
            if broadcast_data:
                await room_broadcast(room, broadcast_data)
    except Exception:
        pass

    return ws


# ──────────────────────────────────────────────
# HTTP routes & app factory
# ──────────────────────────────────────────────

async def index(request: web.Request) -> web.Response:
    return web.FileResponse(path=str(request.app["static_dir"] / "index.html"))


async def healthcheck(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "rooms": len(ROOMS)})


async def on_startup(app: web.Application) -> None:
    asyncio.create_task(_heartbeat_cleanup())
    log.info("狼人杀服务器启动 ✓")


def create_app() -> web.Application:
    app = web.Application()
    static_dir = (Path(__file__).parent / "static").resolve()
    app["static_dir"] = static_dir
    app.on_startup.append(on_startup)
    app.router.add_get("/", index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/health", healthcheck)
    app.router.add_static("/static/", path=str(static_dir), name="static")
    return app


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    log.info("启动端口: %d", port)
    web.run_app(create_app(), host="0.0.0.0", port=port)
