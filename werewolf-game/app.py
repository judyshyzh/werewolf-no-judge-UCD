import asyncio
import json
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aiohttp import WSMsgType, web


def _json(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False)


def _room_code() -> str:
    # Single-team mode: always one fixed room.
    return "微软巨硬"


def _public_player(p: "Player") -> Dict[str, Any]:
    return {
        "id": p.id,
        "name": p.name,
        "alive": p.alive,
        "is_host": p.is_host,
        "seat": p.seat,
    }


@dataclass
class Player:
    id: str
    name: str
    ws: web.WebSocketResponse
    is_host: bool = False
    role: Optional[str] = None  # offline deal: host sets; client enforces
    alive: bool = True
    seat: Optional[int] = None
    witch_save_used: bool = False
    witch_poison_used: bool = False
    hybrid_brother_id: Optional[str] = None
    wolf_beauty_charm_id: Optional[str] = None


@dataclass
class Room:
    code: str
    host_id: str
    players: Dict[str, Player] = field(default_factory=dict)
    phase: str = "lobby"  # lobby | night | sheriff | day | hunter | ended
    # config:
    # - max_seats: board player count (N)
    config: Dict[str, int] = field(default_factory=lambda: {"max_seats": 12})
    # setup: role -> count, used for random deal
    setup: Dict[str, int] = field(default_factory=dict)
    night_actions: Dict[str, Any] = field(default_factory=dict)
    # pending_night_result is computed at night resolve; only revealed after sheriff election
    pending_night_result: Dict[str, Any] = field(default_factory=dict)
    last_night_result: Dict[str, Any] = field(default_factory=dict)  # revealed result
    day_votes: Dict[str, str] = field(default_factory=dict)  # voter_id -> target_id
    sheriff_votes: Dict[str, str] = field(default_factory=dict)  # voter_id -> candidate_id
    sheriff_id: Optional[str] = None
    pending_hunter_id: Optional[str] = None
    pending_hunter_target_id: Optional[str] = None
    night_step: str = "idle"  # idle | guard | wolf | witch | seer | ready
    version: int = 0

    def alive_players(self) -> List[Player]:
        return [p for p in self.players.values() if p.alive]

    def player_list(self) -> List[Dict[str, Any]]:
        return [_public_player(p) for p in self.players.values()]

    def bump(self) -> None:
        self.version += 1


ROOMS: Dict[str, Room] = {}
ROOM_LOCK = asyncio.Lock()


async def ws_send(ws: web.WebSocketResponse, data: Dict[str, Any]) -> None:
    await ws.send_str(_json(data))


async def room_broadcast(room: Room, data: Dict[str, Any]) -> None:
    dead: List[str] = []
    for pid, p in room.players.items():
        try:
            await ws_send(p.ws, data)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room.players.pop(pid, None)
    if dead:
        room.bump()


async def room_sync(room: Room) -> None:
    await room_broadcast(
        room,
        {
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
            },
        },
    )
    # Private state (role + limited flags)
    for p in list(room.players.values()):
        try:
            night_hint: Dict[str, Any] = {}
            if room.phase == "night" and p.alive:
                # 女巫：看到被狼人刀的人
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
                # 狼人阵营：看到所有狼队友及其具体狼牌
                elif _is_wolf_role(p.role):
                    allies = []
                    for qp in room.players.values():
                        if not qp.alive or not qp.role:
                            continue
                        if _is_wolf_role(qp.role):
                            allies.append(
                                {
                                    "id": qp.id,
                                    "name": qp.name,
                                    "seat": qp.seat,
                                    "role": qp.role,
                                }
                            )
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
                        "witch_save_used": p.witch_save_used,
                        "witch_poison_used": p.witch_poison_used,
                    },
                    "night_hint": night_hint,
                },
            )
        except Exception:
            continue


def _count_sides(room: Room) -> Tuple[int, int]:
    wolves = 0
    villagers = 0
    for p in room.players.values():
        if not p.alive or not p.role:
            continue
        if _is_wolf_role(p.role):
            wolves += 1
        else:
            villagers += 1
    return wolves, villagers


def _is_wolf_role(role: Optional[str]) -> bool:
    # 狼人阵营：普通狼人、狼王、白狼王、狼美人
    return role in ("werewolf", "wolf_king", "white_wolf_king", "wolf_beauty")


def _check_win(room: Room) -> Optional[str]:
    wolves, villagers = _count_sides(room)
    if wolves == 0 and room.phase != "lobby":
        return "village"
    if wolves >= villagers and room.phase != "lobby":
        return "werewolf"
    return None


async def _end_game(room: Room, winner: str) -> None:
    room.phase = "ended"
    room.bump()
    await room_broadcast(room, {"type": "game_end", "winner": winner})
    await room_sync(room)

def _occupied_seats(room: Room) -> Dict[int, str]:
    occ: Dict[int, str] = {}
    for p in room.players.values():
        if p.seat is not None:
            occ[p.seat] = p.id
    return occ


def _seated_players(room: Room) -> List[Player]:
    seated = [p for p in room.players.values() if p.seat is not None]
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


def _setup_count(setup: Dict[str, int]) -> int:
    return sum(int(v) for v in setup.values() if int(v) > 0)


async def _start_night(room: Room) -> None:
    room.phase = "night"
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
    room.bump()
    await room_sync(room)


def _has_role(room: Room, role: str) -> bool:
    try:
        return int(room.setup.get(role, 0)) > 0
    except Exception:
        return False


async def _broadcast_audio(room: Room, cue: str) -> None:
    await room_broadcast(room, {"type": "audio_cue", "cue": cue})


NIGHT_FIRST_STEP_DELAY = 10  # seconds; delay between "天黑请闭眼" 和 第一个夜晚角色开眼


async def _delayed_night_step_start(room: Room, delay_s: int, token: int) -> None:
    await asyncio.sleep(delay_s)
    # 仍然是同一夜晚且还没开始守卫/狼人流程时，再推进步骤
    if room.phase == "night" and room.version == token and room.night_step == "idle":
        await _advance_night_step(room)


async def _delayed_audio(room: Room, cue: str, delay_s: int, token: int) -> None:
    await asyncio.sleep(delay_s)
    # Only play if still in same night flow
    if room.phase == "night" and room.version == token:
        await _broadcast_audio(room, cue)


HUNTER_NOTICE_DELAY = 5   # seconds from预言家结束到猎人提示
CHAIN_STEP_DELAY = 5      # seconds between 猎人→狼王→天亮 语音


async def _hunter_wolf_day_chain(room: Room, delay_first: int, token: int) -> None:
    # delay_first 秒后播猎人提示，
    # 若有狼王：5 秒后狼王开眼，再 5 秒后天亮；
    # 若无狼王：5 秒后直接天亮。
    await asyncio.sleep(delay_first)
    if room.phase != "night" or room.version != token:
        return
    await _broadcast_audio(room, "hunter_notice")

    # 5 秒后：有狼王 → 播狼王；没狼王 → 直接天亮
    await asyncio.sleep(CHAIN_STEP_DELAY)
    if room.version != token:
        return
    if _has_role(room, "wolf_king"):
        await _broadcast_audio(room, "wolf_king_open")
        # 狼王开眼后再等 5 秒天亮（仅播语音，不自动结算）
        await asyncio.sleep(CHAIN_STEP_DELAY)
        if room.version != token:
            return
        await _broadcast_audio(room, "day_start")
    else:
        # 无狼王：猎人提示后 5 秒直接天亮（仅播语音，不自动结算）
        await _broadcast_audio(room, "day_start")

    # 「天亮了」语音播完后，如果仍处于夜晚阶段，则仅切换阶段为白天，不公布昨夜信息
    if room.phase == "night" and room.version == token:
        room.phase = "day"
        room.bump()
        await room_sync(room)


async def _advance_night_step(room: Room) -> None:
    # Decide next step based on setup and current step.
    if room.phase != "night":
        return
    step = room.night_step
    # Start: choose first open（优先级：野孩子 → 摄梦人/守卫 → 狼人）
    if step == "idle":
        if _has_role(room, "hybrid"):
            room.night_step = "hybrid"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "hybrid_open")
            return
        # 摄梦人与守卫互斥：有摄梦人就先摄梦人，没有才守卫，再否则狼人
        if _has_role(room, "dreamer"):
            room.night_step = "dreamer"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "dreamer_open")
            return
        room.night_step = "guard" if _has_role(room, "guard") else "wolf"
        room.bump()
        await room_sync(room)
        await _broadcast_audio(room, "guard_open" if room.night_step == "guard" else "wolf_open")
        return
    if step == "hybrid":
        if _has_role(room, "dreamer"):
            room.night_step = "dreamer"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "dreamer_open")
            return
        room.night_step = "guard" if _has_role(room, "guard") else "wolf"
        room.bump()
        await room_sync(room)
        await _broadcast_audio(room, "guard_open" if room.night_step == "guard" else "wolf_open")
        return
    if step == "dreamer":
        room.night_step = "wolf"
        room.bump()
        await room_sync(room)
        await _broadcast_audio(room, "wolf_open")
        return
    if step == "guard":
        room.night_step = "wolf"
        room.bump()
        await room_sync(room)
        await _broadcast_audio(room, "wolf_open")
        return
    if step == "wolf":
        # 狼人之后如果有狼美人，则先开狼美人
        if _has_role(room, "wolf_beauty"):
            room.night_step = "wolf_beauty"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "wolf_beauty_open")
        else:
            if _has_role(room, "witch"):
                room.night_step = "witch"
                room.bump()
                await room_sync(room)
                await _broadcast_audio(room, "witch_open")
            else:
                # 没有女巫：若有预言家则开预言家；否则直接进入 ready 并触发猎人→天亮链
                has_seer = _has_role(room, "seer")
                room.night_step = "seer" if has_seer else "ready"
                room.bump()
                await room_sync(room)
                if room.night_step == "seer":
                    await _broadcast_audio(room, "seer_open")
                else:
                    token = room.version
                    asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return
    if step == "wolf_beauty":
        # 狼美人操作结束后进入女巫 / 预言家 / 直接 ready
        if _has_role(room, "witch"):
            room.night_step = "witch"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "witch_open")
        else:
            has_seer = _has_role(room, "seer")
            room.night_step = "seer" if has_seer else "ready"
            room.bump()
            await room_sync(room)
            if room.night_step == "seer":
                await _broadcast_audio(room, "seer_open")
            else:
                token = room.version
                asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return
    if step == "witch":
        if _has_role(room, "seer"):
            room.night_step = "seer"
            room.bump()
            await room_sync(room)
            await _broadcast_audio(room, "seer_open")
        else:
            # 无预言家：女巫之后直接 ready，并触发猎人→天亮链
            room.night_step = "ready"
            room.bump()
            await room_sync(room)
            token = room.version
            asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return
    if step == "seer":
        room.night_step = "ready"
        room.bump()
        await room_sync(room)
        # Give seer一段时间思考，然后按「猎人→狼王→天亮」链式播放语音
        token = room.version
        asyncio.create_task(_hunter_wolf_day_chain(room, HUNTER_NOTICE_DELAY, token))
        return


async def _notify_hunters_status(room: Room, can_shoot: bool, cause: str, message: str) -> None:
    # Backwards compatible helper
    await _notify_shoot_status(room, role="hunter", can_shoot=can_shoot, cause=cause, message=message)


async def _notify_shoot_status(room: Room, role: str, can_shoot: bool, cause: str, message: str) -> None:
    for p in room.players.values():
        if p.role == role:
            try:
                await ws_send(
                    p.ws,
                    {
                        "type": "shoot_status",
                        "role": role,
                        "can_shoot": can_shoot,
                        "cause": cause,
                        "message": message,
                    },
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

    # Determine wolf kill (guard / dreamer / witch interaction)
    wolf_killed: Optional[str] = None
    if isinstance(wolf_target, str) and wolf_target in room.players and room.players[wolf_target].alive:
        wolf_killed = wolf_target
        # 守卫保护：只有守卫守护且女巫不救时，该人存活；守卫+女巫叠加时仍死亡。
        if isinstance(guard_protect, str) and guard_protect == wolf_target:
            if witch_save:
                # double-protect -> death (do nothing, keep wolf_killed)
                pass
            else:
                # only guard -> cancel wolf kill
                wolf_killed = None
        # 摄梦人射中的人：若被狼人刀，则在无解药时存活；解药与摄梦人叠加时仍死亡。
        if isinstance(dreamer_target, str) and dreamer_target == wolf_target:
            if witch_save:
                # dreamer + witch_save on same target -> death
                pass
            else:
                wolf_killed = None
        elif witch_save:
            # 女巫解药作用在被刀的人（不考虑自救）
            target = room.players[wolf_killed]
            if target.role != "witch":
                wolf_killed = None

    if wolf_killed is not None:
        room.players[wolf_killed].alive = False
        deaths.append(wolf_killed)
        death_causes[wolf_killed] = "wolf"

    # Witch poison (independent)
    if isinstance(witch_poison, str) and witch_poison in room.players:
        if room.players[witch_poison].alive and witch_poison not in deaths:
            room.players[witch_poison].alive = False
            deaths.append(witch_poison)
            death_causes[witch_poison] = "poison"

    # Wolf Beauty charm: if wolf beauty dies this night (any cause), charmed target also dies.
    # Must run AFTER all other death sources (wolf/guard/witch + poison) are applied.
    for p in room.players.values():
        if p.role == "wolf_beauty" and p.wolf_beauty_charm_id and p.id in deaths:
            cid = p.wolf_beauty_charm_id
            if cid in room.players and room.players[cid].alive and cid not in deaths:
                room.players[cid].alive = False
                deaths.append(cid)
                death_causes[cid] = "charm"

    # Dreamer link: if dreamer dies this night (any cause), the shot target also dies.
    if isinstance(dreamer_target, str) and dreamer_target in room.players:
        for p in room.players.values():
            if p.role == "dreamer" and p.id in deaths:
                if room.players[dreamer_target].alive and dreamer_target not in deaths:
                    room.players[dreamer_target].alive = False
                    deaths.append(dreamer_target)
                    death_causes[dreamer_target] = "dreamer"
                break

    # Send seer result privately (even if game ends right after)
    if isinstance(seer_check, str) and seer_check in room.players:
        checked = room.players[seer_check]
        for p in room.players.values():
            # Seer gets result even if killed that night (if alive at night start)
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

    # Hunter handling：只告知开枪状态，不再进入单独的“猎人开枪阶段”
    pending_hunter: Optional[str] = None
    blocked_hunter: Optional[str] = None
    blocked_cause: str = ""
    for pid in deaths:
        p = room.players.get(pid)
        if p and p.role == "hunter":
            if death_causes.get(pid) in ("poison", "charm"):
                blocked_hunter = pid
                blocked_cause = death_causes.get(pid, "")
                continue
            pending_hunter = pid
            break

    # Notify hunter about shot eligibility
    if blocked_hunter and blocked_hunter in room.players:
        try:
            hp = room.players[blocked_hunter]
            await ws_send(
                hp.ws,
                {
                    "type": "shoot_status",
                    "role": "hunter",
                    "can_shoot": False,
                    "cause": blocked_cause or "poison",
                    "message": "无法开枪",
                },
            )
        except Exception:
            pass
    if pending_hunter and pending_hunter in room.players:
        try:
            hp = room.players[pending_hunter]
            await ws_send(
                hp.ws,
                {
                    "type": "shoot_status",
                    "role": "hunter",
                    "can_shoot": True,
                    "cause": death_causes.get(pending_hunter, "wolf"),
                    "message": "正常",
                },
            )
        except Exception:
            pass

    room.pending_night_result = {"killed_ids": deaths, "death_causes": death_causes}
    # 仅通过 shoot_status 告知猎人状态，不再启用待处理猎人阶段
    room.pending_hunter_id = None
    room.pending_hunter_target_id = None
    # Reveal immediately (merged: resolve night == reveal night)
    await _reveal_night_and_start_day(room)

    winner = _check_win(room)
    if winner:
        await _end_game(room, winner)


async def _tally_day(room: Room) -> None:
    alive_ids = {p.id for p in room.players.values() if p.alive}
    counts: Dict[str, int] = {}
    for voter, target in room.day_votes.items():
        if voter not in alive_ids:
            continue
        if target not in alive_ids:
            continue
        counts[target] = counts.get(target, 0) + 1

    eliminated_id: Optional[str] = None
    if counts:
        best = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0]
        eliminated_id = best[0]
        room.players[eliminated_id].alive = False

    room.bump()
    await room_broadcast(room, {"type": "day_result", "eliminated_id": eliminated_id})
    await room_sync(room)

    winner = _check_win(room)
    if winner:
        await _end_game(room, winner)
        return

    # 不再在程序内处理“猎人开枪阶段”，这里只负责结束游戏或保持当前局面
    return


async def _reveal_night_and_start_day(room: Room) -> None:
    # Reveal stored result (no sheriff phase)
    room.last_night_result = room.pending_night_result
    room.pending_night_result = {}
    room.phase = "day" if not room.pending_hunter_id else "hunter"
    room.bump()
    await room_broadcast(room, {"type": "night_revealed", "result": room.last_night_result})
    await room_sync(room)


async def _restart_to_lobby(room: Room) -> None:
    # Keep players + seats + setup; reset a running game to lobby.
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
    for p in room.players.values():
        p.alive = True
        p.role = None
        p.witch_save_used = False
        p.witch_poison_used = False
    room.bump()
    await room_broadcast(room, {"type": "restarted"})
    await room_sync(room)


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)

    player: Optional[Player] = None
    room: Optional[Room] = None

    async def reject(reason: str) -> None:
        await ws_send(ws, {"type": "error", "message": reason})
        await ws.close()

    async for msg in ws:
        if msg.type != WSMsgType.TEXT:
            continue
        try:
            data = json.loads(msg.data)
        except Exception:
            await ws_send(ws, {"type": "error", "message": "消息格式错误（需要 JSON）"})
            continue

        mtype = data.get("type")

        if mtype == "create_room":
            if room is not None:
                await ws_send(ws, {"type": "error", "message": "你已经在房间里了"})
                continue
            name = str(data.get("name") or "").strip()[:20]
            if not name:
                await reject("名字不能为空")
                break
            async with ROOM_LOCK:
                code = _room_code()
                pid = secrets.token_urlsafe(10)
                if code in ROOMS:
                    room = ROOMS[code]
                    if room.phase != "lobby":
                        await reject("游戏已经开始，暂不支持加入")
                        break
                    is_host = room.host_id not in room.players
                    player = Player(id=pid, name=name, ws=ws, is_host=is_host)
                    room.players[pid] = player
                    if is_host:
                        room.host_id = pid
                else:
                    player = Player(id=pid, name=name, ws=ws, is_host=True)
                    room = Room(code=code, host_id=pid)
                    room.players[pid] = player
                    ROOMS[code] = room
                room.bump()
            await ws_send(ws, {"type": "created", "room_code": room.code, "player_id": player.id})
            await room_sync(room)
            continue

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
                if room.phase != "lobby":
                    await reject("游戏已经开始，暂不支持中途加入")
                    break
                pid = secrets.token_urlsafe(10)
                player = Player(id=pid, name=name, ws=ws, is_host=False)
                room.players[pid] = player
                room.bump()
            await ws_send(ws, {"type": "joined", "room_code": room.code, "player_id": player.id})
            await room_sync(room)
            continue

        if room is None or player is None:
            await ws_send(ws, {"type": "error", "message": "请先创建/加入房间"})
            continue

        # ----- host-only actions -----
        if mtype == "set_config":
            await ws_send(ws, {"type": "error", "message": "人数由板子自动计算，不需要单独设置"})
            continue

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
            n = _setup_count(room.setup)
            if n > 0:
                room.config = {"max_seats": n}
                # drop invalid seats if board size shrinks
                for p in room.players.values():
                    if p.seat is not None and (p.seat < 1 or p.seat > n):
                        p.seat = None
            room.bump()
            await room_sync(room)
            continue

        # ----- 公布昨夜信息（任何人、任何阶段都可以触发） -----
        if mtype == "host_resolve_night":
            # consume witch potions based on submitted actions
            for p in room.players.values():
                if p.role == "witch" and p.alive:
                    if room.night_actions.get("witch_save"):
                        p.witch_save_used = True
                    if room.night_actions.get("witch_poison") is not None:
                        p.witch_poison_used = True
            await _resolve_night(room)
            continue

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
                await ws_send(ws, {"type": "error", "message": "请先让房主配板子（人数=角色数量合计），再入座"})
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

        if mtype == "start_game":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以开始"})
                continue
            if room.phase != "lobby":
                await ws_send(ws, {"type": "error", "message": "游戏已经开始"})
                continue
            # Ensure seats are taken
            max_seats = int(room.config.get("max_seats", 0))
            if max_seats <= 0:
                await ws_send(ws, {"type": "error", "message": "请先配板子，人数会自动计算"})
                continue
            seated = _seated_players(room)
            if len(seated) != max_seats:
                await ws_send(ws, {"type": "error", "message": f"需要刚好 {max_seats} 人入座才能开始（当前 {len(seated)}）"})
                continue

            # Deal roles (random only)
            try:
                _deal_roles_random(room)
            except Exception as e:
                await ws_send(ws, {"type": "error", "message": str(e)})
                continue

            # 先发身份，阶段视为白天，由房主再手动点击“开始游戏”进入黑夜
            room.phase = "day"
            room.bump()
            await room_sync(room)

            for p in room.players.values():
                await ws_send(p.ws, {"type": "your_role", "role": p.role})
            continue

        if mtype == "force_end":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以结束"})
                continue
            await _end_game(room, "host")
            continue

        if mtype == "host_restart":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以重新开始"})
                continue
            # allow restart from any phase
            await _restart_to_lobby(room)
            continue

        if mtype == "host_audio_cue":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以播放语音提示"})
                continue
            cue = data.get("cue")
            if not isinstance(cue, str) or not cue:
                await ws_send(ws, {"type": "error", "message": "cue 无效"})
                continue
            # broadcast to everyone; clients play local audio
            await room_broadcast(room, {"type": "audio_cue", "cue": cue})
            continue

        if mtype == "host_start_night":
            if not player.is_host:
                await ws_send(ws, {"type": "error", "message": "只有房主可以开始游戏"})
                continue
            if room.phase not in ("day", "lobby"):
                await ws_send(ws, {"type": "error", "message": "当前阶段不能开始黑夜"})
                continue
            await _start_night(room)
            # Auto audio sequence start：先播「天黑请闭眼」，结束后再依次守卫/狼人/女巫/预言家
            await _broadcast_audio(room, "night_start")
            token = room.version
            asyncio.create_task(_delayed_night_step_start(room, NIGHT_FIRST_STEP_DELAY, token))
            continue

        # ----- gameplay actions -----
        if room.phase == "night":
            if mtype == "wolf_kill":
                if not _is_wolf_role(player.role) or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活狼人阵营"})
                    continue
                if room.night_actions.get("wolf_target") is not None:
                    await ws_send(ws, {"type": "error", "message": "本夜已锁定刀人目标，不能改刀"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                room.night_actions["wolf_target"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "wolf_kill"})
                # Notify witch privately who is targeted (so she can decide save)
                try:
                    target = room.players[target_id]
                    for wp in room.players.values():
                        if wp.alive and wp.role == "witch":
                            await ws_send(
                                wp.ws,
                                {
                                    "type": "witch_prompt",
                                    "wolf_target_id": target.id,
                                    "wolf_target_name": target.name,
                                    "wolf_target_seat": target.seat,
                                },
                            )
                except Exception:
                    pass
                if room.night_step == "wolf":
                    await _advance_night_step(room)
                continue

            if mtype == "wolf_chat":
                # 狼人夜间聊天：仅限夜晚且狼人睁眼阶段，且只有狼人阵营可以发送
                if room.phase != "night" or room.night_step != "wolf":
                    await ws_send(ws, {"type": "error", "message": "当前阶段不能狼人聊天"})
                    continue
                if not _is_wolf_role(player.role) or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "只有存活狼人阵营可以夜聊"})
                    continue
                text = str(data.get("text") or "").strip()
                if not text:
                    await ws_send(ws, {"type": "error", "message": "聊天内容不能为空"})
                    continue
                # 广播给同一房间的所有狼人阵营玩家
                payload = {
                    "type": "wolf_chat",
                    "from_id": player.id,
                    "from_name": player.name,
                    "from_seat": player.seat,
                    "text": text,
                }
                for qp in room.players.values():
                    if _is_wolf_role(qp.role) and qp.alive:
                        try:
                            await ws_send(qp.ws, payload)
                        except Exception:
                            continue
                continue

            if mtype == "hybrid_choose":
                if player.role != "hybrid" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活野孩子"})
                    continue
                if player.hybrid_brother_id:
                    await ws_send(ws, {"type": "error", "message": "你已经选过大哥，不能更改"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                if target_id == player.id:
                    await ws_send(ws, {"type": "error", "message": "不能连自己当大哥"})
                    continue
                player.hybrid_brother_id = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "hybrid_choose"})
                if room.night_step == "hybrid":
                    await _advance_night_step(room)
                continue

            if mtype == "wolf_beauty_charm":
                if player.role != "wolf_beauty" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活狼美人"})
                    continue
                if player.wolf_beauty_charm_id:
                    await ws_send(ws, {"type": "error", "message": "你已经魅惑过一名玩家，不能更改"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                if target_id == player.id:
                    await ws_send(ws, {"type": "error", "message": "不能魅惑自己"})
                    continue
                player.wolf_beauty_charm_id = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "wolf_beauty_charm"})
                if room.night_step == "wolf_beauty":
                    await _advance_night_step(room)
                continue

            if mtype == "seer_check":
                if player.role != "seer" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活预言家"})
                    continue
                if room.night_actions.get("seer_check") is not None:
                    await ws_send(ws, {"type": "error", "message": "本夜已验过人，不能改验"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                room.night_actions["seer_check"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "seer_check"})
                # Immediate seer feedback
                checked = room.players[target_id]
                seer_payload = {
                    "type": "seer_result",
                    "target_id": checked.id,
                    "target_name": checked.name,
                    "is_werewolf": _is_wolf_role(checked.role),
                }
                # Send via both references to avoid any ws mismatch edge-cases
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
                    await ws_send(ws, {"type": "error", "message": "你不是存活守卫"})
                    continue
                target_id = data.get("target_id")
                # 允许“不守护任何人”（空串或 None）
                if not isinstance(target_id, str) or target_id == "":
                    room.night_actions["guard_protect"] = None
                else:
                    if target_id not in room.players:
                        await ws_send(ws, {"type": "error", "message": "目标无效"})
                        continue
                    if not room.players[target_id].alive:
                        await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                        continue
                    room.night_actions["guard_protect"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "guard_protect"})
                if room.night_step == "guard":
                    await _advance_night_step(room)
                continue

            if mtype == "dreamer_shoot":
                if player.role != "dreamer" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活摄梦人"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                room.night_actions["dreamer_target"] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "dreamer_shoot"})
                if room.night_step == "dreamer":
                    await _advance_night_step(room)
                continue

            if mtype == "witch_save":
                if player.role != "witch" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活女巫"})
                    continue
                if player.witch_save_used:
                    await ws_send(ws, {"type": "error", "message": "解药已用完"})
                    continue
                if room.night_actions.get("witch_save_decided"):
                    await ws_send(ws, {"type": "error", "message": "本夜救人决定已锁定，不能再改"})
                    continue
                use = bool(data.get("use"))
                if use:
                    wt = room.night_actions.get("wolf_target")
                    if isinstance(wt, str) and wt == player.id:
                        await ws_send(ws, {"type": "error", "message": "你今晚被刀，不能自救"})
                        continue
                room.night_actions["witch_save"] = use
                room.night_actions["witch_save_decided"] = True
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "witch_save"})
                if room.night_step == "witch" and room.night_actions.get("witch_poison_decided"):
                    await _advance_night_step(room)
                continue

            if mtype == "witch_poison":
                if player.role != "witch" or not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你不是存活女巫"})
                    continue
                if player.witch_poison_used:
                    await ws_send(ws, {"type": "error", "message": "毒药已用完"})
                    continue
                if room.night_actions.get("witch_poison_decided"):
                    await ws_send(ws, {"type": "error", "message": "本夜毒人决定已锁定，不能再改"})
                    continue
                target_id = data.get("target_id")
                if target_id is None or target_id == "":
                    room.night_actions["witch_poison"] = None
                    room.night_actions["witch_poison_decided"] = True
                    room.bump()
                    await ws_send(ws, {"type": "ack", "action": "witch_poison_cancel"})
                    await _notify_shoot_status(room, role="hunter", can_shoot=True, cause="safe", message="正常")
                    if _has_role(room, "wolf_king"):
                        await _notify_shoot_status(room, role="wolf_king", can_shoot=True, cause="safe", message="正常")
                    if room.night_step == "witch" and room.night_actions.get("witch_save_decided"):
                        await _advance_night_step(room)
                    continue
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                room.night_actions["witch_poison"] = target_id
                room.night_actions["witch_poison_decided"] = True
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "witch_poison"})
                # Immediate hunter status if hunter is poisoned
                try:
                    target = room.players[target_id]
                    if target.role == "hunter":
                        await _notify_shoot_status(room, role="hunter", can_shoot=False, cause="poison", message="无法开枪")
                    else:
                        await _notify_shoot_status(room, role="hunter", can_shoot=True, cause="safe", message="正常")
                    # Wolf king status (same poison rule per your table)
                    if target.role == "wolf_king":
                        await _notify_shoot_status(room, role="wolf_king", can_shoot=False, cause="poison", message="无法开枪")
                    else:
                        if _has_role(room, "wolf_king"):
                            await _notify_shoot_status(room, role="wolf_king", can_shoot=True, cause="safe", message="正常")
                except Exception:
                    pass
                if room.night_step == "witch" and room.night_actions.get("witch_save_decided"):
                    await _advance_night_step(room)
                continue

        # sheriff phase removed

        if room.phase == "day":
            if mtype == "vote":
                if not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你已死亡不能投票"})
                    continue
                target_id = data.get("target_id")
                if not isinstance(target_id, str) or target_id not in room.players:
                    await ws_send(ws, {"type": "error", "message": "目标无效"})
                    continue
                if not room.players[target_id].alive:
                    await ws_send(ws, {"type": "error", "message": "目标已死亡"})
                    continue
                room.day_votes[player.id] = target_id
                room.bump()
                await ws_send(ws, {"type": "ack", "action": "vote"})
                await room_broadcast(room, {"type": "vote_progress", "count": len(room.day_votes)})
                continue

            if mtype == "wolf_bomb":
                if not player.alive:
                    await ws_send(ws, {"type": "error", "message": "你已死亡，不能自爆"})
                    continue
                if player.role not in ("werewolf", "wolf_king"):
                    await ws_send(ws, {"type": "error", "message": "只有狼人/狼王可以自爆"})
                    continue
                # 狼人自曝：直接死亡，保持白天阶段，昨夜信息已在进入白天时公布
                player.alive = False
                room.bump()
                await room_broadcast(room, {"type": "wolf_bomb", "player_id": player.id})
                await room_sync(room)

                winner = _check_win(room)
                if winner:
                    await _end_game(room, winner)
                continue

            if mtype == "host_tally_day":
                if not player.is_host:
                    await ws_send(ws, {"type": "error", "message": "只有房主可以结算投票"})
                    continue
                await _tally_day(room)
                continue

        await ws_send(ws, {"type": "error", "message": "不支持的操作或阶段不匹配"})

    # disconnect cleanup
    try:
        if room and player:
            should_sync = False
            async with ROOM_LOCK:
                if room.code in ROOMS and player.id in room.players:
                    room.players.pop(player.id, None)
                    room.bump()
                    # if host left, close room
                    if player.is_host:
                        ROOMS.pop(room.code, None)
                    else:
                        should_sync = True
            if should_sync:
                await room_sync(room)
    except Exception:
        pass

    return ws


async def index(request: web.Request) -> web.Response:
    return web.FileResponse(path=str(request.app["static_dir"] / "index.html"))


def create_app() -> web.Application:
    app = web.Application()
    static_dir = (Path(__file__).parent / "static").resolve()
    app["static_dir"] = static_dir
    app.router.add_get("/", index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_static("/static/", path=str(static_dir), name="static")
    return app


if __name__ == "__main__":
    web.run_app(create_app(), host="0.0.0.0", port=8080)
