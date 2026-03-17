const $ = (id) => document.getElementById(id);

let ws = null;
let state = {
  room: null,
  playerId: null,
  myRole: null,
  isHost: false,
  private: null,
  nightHint: null,
  seerLast: null,
  hunterStatus: null,
  wolfKingStatus: null,
  lastPhase: null,
};

const AUDIO_FILES = {
  night_start: "/static/audio/night_start.mp3",
  hybrid_open: "/static/audio/hybrid_open.mp3",
  guard_open: "/static/audio/guard_open.mp3",
  dreamer_open: "/static/audio/dreamer_open.mp3",
  wolf_open: "/static/audio/wolf_open.mp3",
  wolf_beauty_open: "/static/audio/wolf_beauty_open.mp3",
  witch_open: "/static/audio/witch_open.mp3",
  seer_open: "/static/audio/seer_open.mp3",
  hunter_notice: "/static/audio/hunter_notice.mp3",
  wolf_king_open: "/static/audio/wolf_king_open.mp3",
  day_start: "/static/audio/day_start.mp3",
};

let audioEnabled = false;
let audioCache = {};

function enableAudio() {
  audioEnabled = true;
  localStorage.setItem("ww_audio_enabled", "1");
}

function loadAudioEnabled() {
  audioEnabled = localStorage.getItem("ww_audio_enabled") === "1";
}

function getAudio(cue) {
  const url = AUDIO_FILES[cue];
  if (!url) return null;
  if (!audioCache[cue]) audioCache[cue] = new Audio(url);
  return audioCache[cue];
}

async function playCue(cue) {
  if (!audioEnabled) return;
  const a = getAudio(cue);
  if (!a) return;
  try {
    a.currentTime = 0;
    await a.play();
  } catch (e) {
    logLine("语音播放失败（请点一次“开启声音”授权）");
  }
}

function getParams() {
  try {
    return new URLSearchParams(location.search || "");
  } catch {
    return new URLSearchParams();
  }
}

const params = getParams();

function logLine(text) {
  const log = $("log");
  const div = document.createElement("div");
  div.className = "line";
  div.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

function setHint(text) {
  $("connectHint").textContent = text || "";
}

function roleLabel(role) {
  if (role === "werewolf") return "狼人";
  if (role === "seer") return "预言家";
  if (role === "witch") return "女巫";
  if (role === "guard") return "守卫";
  if (role === "hunter") return "猎人";
  if (role === "wolf_beauty") return "狼美人";
  if (role === "dreamer") return "摄梦人";
  if (role === "idiot") return "白痴";
  if (role === "villager") return "村民";
  if (role === "hybrid") return "野孩子";
  return role || "（未知）";
}

function phaseLabel(phase) {
  if (phase === "lobby") return "等待开局";
  if (phase === "night") return "夜晚";
  if (phase === "day") return "白天";
  if (phase === "ended") return "已结束";
  return phase || "-";
}

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);

  ws.onopen = () => {
    setHint("已连接。创建或加入房间开始。");
    logLine("已连接服务器。");
    const roleEl = $("role");
    if (roleEl) {
      roleEl.onclick = () => {
        if (!state.myRole) return;
        state.roleOverlayVisible = !state.roleOverlayVisible;
        renderRoleOverlay();
      };
    }

    // Dev automation: ?auto=1&name=...&seat=...&preset9=1
    if (params.get("auto") === "1") {
      const name = (params.get("name") || "").trim();
      if (name) {
        send({ type: "create_room", name });
      }
    }
  };

  ws.onclose = () => {
    setHint("连接已断开。刷新页面可重连。");
    logLine("连接已断开（请刷新页面重连）。");
  };

  ws.onmessage = (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch {
      return;
    }

    if (msg.type === "error") {
      logLine(`错误：${msg.message}`);
      setHint(msg.message);
      return;
    }

    if (msg.type === "audio_cue") {
      const cue = msg.cue;
      logLine(`语音提示：${cue}`);
      playCue(cue);
      return;
    }

    if (msg.type === "created" || msg.type === "joined") {
      state.playerId = msg.player_id;
      $("connectCard").classList.add("hidden");
      $("roomCard").classList.remove("hidden");
      $("roomCode").textContent = msg.room_code;
      logLine(msg.type === "created" ? `已创建房间 ${msg.room_code}` : `已加入房间 ${msg.room_code}`);

      if (params.get("auto") === "1") {
        const seat = Number(params.get("seat") || 0);
        if (Number.isFinite(seat) && seat > 0) {
          send({ type: "set_seat", seat });
        }
        const preset = params.get("preset");
        if (preset && msg.type === "created") {
          // Only the creator tab auto-applies preset setup
          if (preset === "common1") {
            send({ type: "host_set_setup", setup: { seer: 1, witch: 1, hunter: 1, villager: 3, werewolf: 3 } });
          } else if (preset === "common2") {
            send({ type: "host_set_setup", setup: { seer: 1, witch: 1, hunter: 1, idiot: 1, villager: 4, werewolf: 4 } });
          }
        }
      }
      return;
    }

    if (msg.type === "your_role") {
      state.myRole = msg.role;
      $("role").textContent = roleLabel(msg.role);
      logLine(`你的身份：${roleLabel(msg.role)}`);
      renderControls();
      return;
    }

    if (msg.type === "private_state") {
      state.private = msg.you;
      state.nightHint = msg.night_hint || null;
      // prefer private role (authoritative)
      state.myRole = msg.you.role;
      $("role").textContent = roleLabel(state.myRole || "");
      renderControls();
      return;
    }

    if (msg.type === "witch_prompt") {
      state.nightHint = {
        wolf_target_id: msg.wolf_target_id,
        wolf_target_name: msg.wolf_target_name,
        wolf_target_seat: msg.wolf_target_seat,
      };
      const seat = msg.wolf_target_seat ? `${msg.wolf_target_seat}号` : "";
      logLine(`女巫信息：今晚被刀 = ${seat}${msg.wolf_target_name}`);
      renderControls();
      return;
    }

    if (msg.type === "seer_result") {
      state.seerLast = msg;
      logLine(`验人结果：${msg.target_name} 是 ${msg.is_werewolf ? "狼人" : "好人"}`);
      renderControls();
      return;
    }

    if (msg.type === "shoot_status") {
      if (msg.role === "hunter") {
        state.hunterStatus = msg;
        logLine(`猎人开枪状态：${msg.message || (msg.can_shoot ? "正常" : "无法开枪")}`);
      } else if (msg.role === "wolf_king") {
        state.wolfKingStatus = msg;
        logLine(`狼王开枪状态：${msg.message || (msg.can_shoot ? "正常" : "无法开枪")}`);
      }
      renderControls();
      return;
    }

    if (msg.type === "wolf_chat") {
      const seat = msg.from_seat ? `${msg.from_seat}号` : "";
      const name = msg.from_name || msg.from_id;
      logLine(`【狼队语音】${seat}${name}：${msg.text}`);
      return;
    }

    if (msg.type === "vote_progress") {
      logLine(`投票进度：已收到 ${msg.count} 票`);
      return;
    }

    if (msg.type === "day_result") {
      if (msg.eliminated_id) {
        const p = state.room?.players?.find((x) => x.id === msg.eliminated_id);
        logLine(`白天结算：放逐 ${p ? p.name : msg.eliminated_id}`);
      } else {
        logLine("白天结算：无人被放逐（无人投票或无效）");
      }
      return;
    }

    // sheriff phase removed

    if (msg.type === "night_revealed") {
      const killed = msg?.result?.killed_ids || [];
      if (!killed.length) {
        logLine("昨夜信息：平安夜");
      } else {
        const names = killed
          .map((id) => state.room?.players?.find((x) => x.id === id)?.name || id)
          .join("、");
        logLine(`昨夜信息：死亡 = ${names}`);
      }
      return;
    }

    if (msg.type === "wolf_bomb") {
      const p = state.room?.players?.find((x) => x.id === msg.player_id);
      logLine(`狼人自爆：${p ? p.name : msg.player_id} 已出局`);
      return;
    }

    if (msg.type === "hunter_shot") {
      const p = state.room?.players?.find((x) => x.id === msg.killed_id);
      logLine(`猎人开枪：带走 ${p ? p.name : msg.killed_id}`);
      return;
    }

    if (msg.type === "game_end") {
      logLine(`游戏结束：胜利方 = ${msg.winner}`);
      return;
    }

    if (msg.type === "restarted") {
      logLine("房主已重新开始：已回到大厅，等待重新发牌。");
      state.seerLast = null;
      state.hunterStatus = null;
      return;
    }

    if (msg.type === "room_state") {
      state.room = msg.room;
      if (state.lastPhase !== msg.room.phase && msg.room.phase === "night") {
        state.hunterStatus = null;
      }
      state.lastPhase = msg.room.phase;
      const me = msg.room.players.find((p) => p.id === state.playerId);
      state.isHost = !!me?.is_host;
      $("phase").textContent = phaseLabel(msg.room.phase);
      // 根据阶段切换主题：白天=浅色，夜晚=深色
      if (msg.room.phase === "day") {
        document.body.classList.add("day-theme");
      } else if (msg.room.phase === "night") {
        document.body.classList.remove("day-theme");
      }
      // 夜晚隐藏身份，白天/大厅等阶段显示
      const roleEl = $("role");
      if (roleEl) {
        if (msg.room.phase === "night") {
          roleEl.textContent = "（夜晚中，身份已隐藏）";
        } else {
          roleEl.textContent = roleLabel(state.myRole || "");
        }
      }
      renderPlayers();
      renderControls();

      return;
    }

    if (msg.type === "ack") {
      logLine(`已提交：${msg.action}`);
      return;
    }
  };
}

function send(data) {
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    setHint("尚未连接服务器");
    logLine("尚未连接服务器（请刷新页面，并确保用 http://localhost:8080 打开）。");
    return;
  }
  ws.send(JSON.stringify(data));
}

function renderPlayers() {
  const root = $("players");
  root.innerHTML = "";
  if (!state.room) return;
  const players = [...state.room.players].sort((a, b) => {
    const sa = a.seat ?? 999;
    const sb = b.seat ?? 999;
    if (sa !== sb) return sa - sb;
    if (a.is_host !== b.is_host) return a.is_host ? -1 : 1;
    return a.name.localeCompare(b.name);
  });
  for (const p of players) {
    const div = document.createElement("div");
    div.className = `player ${p.alive ? "" : "dead"}`;
    const left = document.createElement("div");
    const seat = p.seat ? `${p.seat}号` : "未入座";
    left.textContent = `${seat} · ${p.name}${p.id === state.playerId ? "（我）" : ""}`;
    const right = document.createElement("div");
    right.className = `badge ${p.is_host ? "host" : ""}`;
    right.textContent = p.is_host ? "房主" : p.alive ? "存活" : "死亡";
    div.appendChild(left);
    div.appendChild(right);
    root.appendChild(div);
  }
}

function aliveTargetOptions(excludeSelf = false) {
  const list = [];
  const players = state.room?.players || [];
  for (const p of players) {
    if (!p.alive) continue;
    if (excludeSelf && p.id === state.playerId) continue;
    list.push({ id: p.id, name: p.name });
  }
  return list;
}

function makeSelect(options, id) {
  const sel = document.createElement("select");
  sel.id = id;
  for (const opt of options) {
    const o = document.createElement("option");
    o.value = opt.id;
    o.textContent = opt.name;
    sel.appendChild(o);
  }
  return sel;
}

function makeRoleSelect(current) {
  const roles = [
    { id: "", name: "（未设置）" },
    { id: "villager", name: "村民" },
    { id: "werewolf", name: "狼人" },
    { id: "seer", name: "预言家" },
    { id: "witch", name: "女巫" },
    { id: "guard", name: "守卫" },
    { id: "hunter", name: "猎人" },
  ];
  const sel = document.createElement("select");
  for (const r of roles) {
    const o = document.createElement("option");
    o.value = r.id;
    o.textContent = r.name;
    if ((current || "") === r.id) o.selected = true;
    sel.appendChild(o);
  }
  return sel;
}

function sumSetup(setup) {
  if (!setup) return 0;
  return Object.values(setup).reduce((acc, n) => acc + Number(n || 0), 0);
}

function setupLine(setup) {
  const entries = Object.entries(setup || {}).filter(([, v]) => Number(v) > 0);
  if (!entries.length) return "（未配置）";
  return entries
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => `${roleLabel(k)}×${v}`)
    .join("，");
}

function pickSetup(setup, allowed) {
  const out = [];
  for (const id of allowed) {
    const n = Number(setup?.[id] || 0);
    if (n > 0) out.push(`${roleLabel(id)}×${n}`);
  }
  return out.length ? out.join("，") : "（未选择）";
}

function renderSeatSelection(root, { mySeat, maxSeats, disabled }) {
  const seatTitle = document.createElement("div");
  seatTitle.className = "small";
  seatTitle.textContent = mySeat ? `座位：你已入座 ${mySeat}号。` : `座位：请选择（1..${maxSeats}）。`;
  root.appendChild(seatTitle);

  const taken = new Set((state.room.players || []).filter((p) => p.seat).map((p) => p.seat));
  const grid = document.createElement("div");
  grid.className = "controlsRow";
  for (let i = 1; i <= maxSeats; i++) {
    const btn = document.createElement("button");
    const occupied = taken.has(i);
    btn.textContent = `${i}号${occupied ? "（已占）" : ""}`;
    btn.disabled = disabled || (occupied && i !== mySeat);
    btn.onclick = () => send({ type: "set_seat", seat: i });
    grid.appendChild(btn);
  }
  root.appendChild(grid);
}

function renderControls() {
  const root = $("controls");
  root.innerHTML = "";
  if (!state.room) return;

  const phase = state.room.phase;
  const me = state.room.players.find((p) => p.id === state.playerId);
  const alive = !!me?.alive;
  const mySeat = me?.seat ?? null;
  const maxSeats = Number(state.room.config?.max_seats ?? 0);
  const currentSetup = { ...(state.room.setup || {}) };
  const setupCount = sumSetup(currentSetup);

  // Audio enable button (everyone)
  {
    const row = document.createElement("div");
    row.className = "controlsRow";
    const btn = document.createElement("button");
    btn.textContent = audioEnabled ? "声音已开启" : "开启声音（点一次授权）";
    btn.className = audioEnabled ? "" : "primary";
    btn.onclick = async () => {
      enableAudio();
      renderControls();
    };
    row.appendChild(btn);
    root.appendChild(row);
  }

  // Host audio panel
  if (state.isHost) {
    const row = document.createElement("div");
    row.className = "controlsRow";
    const sel = makeSelect(
      [
        { id: "night_start", name: "天黑请闭眼" },
        { id: "wolf_open", name: "狼人请睁眼" },
        { id: "witch_open", name: "女巫请睁眼" },
        { id: "seer_open", name: "预言家请睁眼" },
        { id: "hunter_notice", name: "猎人请注意" },
        { id: "day_start", name: "天亮了" },
      ],
      "audioCue"
    );
    const btn = document.createElement("button");
    btn.textContent = "全员播放语音";
    btn.onclick = () => send({ type: "host_audio_cue", cue: sel.value });
    row.appendChild(sel);
    row.appendChild(btn);
    root.appendChild(row);
  }

  // Host controls (lobby)
  if (state.isHost && phase === "lobby") {
    const setupSummary = document.createElement("div");
    setupSummary.className = "small";
    const target = maxSeats > 0 ? maxSeats : setupCount;
    setupSummary.textContent = `当前板子：${setupLine(currentSetup)}（合计 ${setupCount} 人 / 人数=${target || "未确定"}）`;
    root.appendChild(setupSummary);

    const makeDropdown = (options, id, labelText) => {
      const row = document.createElement("div");
      row.className = "controlsRow";
      const label = document.createElement("div");
      label.style.minWidth = "70px";
      label.textContent = labelText;
      const sel = makeSelect([{ id: "", name: "请选择…" }, ...options], id);
      row.appendChild(label);
      row.appendChild(sel);
      return { row, sel };
    };

    const GOD_OPTIONS = [
      { id: "hunter", name: "猎人" },
      { id: "witch", name: "女巫" },
      { id: "seer", name: "预言家" },
      { id: "knight", name: "骑士" },
      { id: "cupid", name: "丘比特" },
      { id: "bomber", name: "炸弹人" },
      { id: "idiot", name: "白痴" },
      { id: "guard", name: "守卫" },
      { id: "dreamer", name: "摄梦人" },
    ];
    const WOLF_OPTIONS = [
      { id: "wolf_king", name: "狼王" },
      { id: "werewolf", name: "狼人" },
      { id: "white_wolf_king", name: "白狼王" },
    { id: "wolf_beauty", name: "狼美人" },
    ];
    const VILL_OPTIONS = [
      { id: "villager", name: "村民" },
      { id: "hybrid", name: "混血儿" },
    ];

    // 一键预设
    {
      const row = document.createElement("div");
      row.className = "controlsRow";
      const presetBtn = document.createElement("button");
      presetBtn.className = "primary";
      presetBtn.textContent = "常用1：9人（预女猎 + 3民 + 3狼）";
      presetBtn.onclick = () => {
        const setup = { seer: 1, witch: 1, hunter: 1, villager: 3, werewolf: 3 };
        send({ type: "host_set_setup", setup });
      };
      row.appendChild(presetBtn);

      const presetBtn2 = document.createElement("button");
      presetBtn2.className = "primary";
      presetBtn2.textContent = "常用2：12人（预女骑 + 狼王×1 + 狼人×2 + 村民×3）";
      presetBtn2.onclick = () => {
        const setup = { seer: 1, witch: 1, knight: 1, wolf_king: 1, werewolf: 2, villager: 3 };
        send({ type: "host_set_setup", setup });
      };
      row.appendChild(presetBtn2);
      root.appendChild(row);
    }

    // Current setup with +/- adjust
    if (Object.keys(currentSetup).length) {
      const title = document.createElement("div");
      title.className = "small";
      title.textContent = "当前板子明细（可微调）：";
      root.appendChild(title);

      Object.entries(currentSetup)
        .sort((a, b) => a[0].localeCompare(b[0]))
        .forEach(([role, n]) => {
          const row = document.createElement("div");
          row.className = "controlsRow";
          const label = document.createElement("div");
          label.style.minWidth = "140px";
          label.textContent = `${roleLabel(role)} × ${n}`;

          const minus = document.createElement("button");
          minus.textContent = "-1";
          minus.onclick = () => {
            const next = { ...currentSetup };
            next[role] = Math.max(0, Number(next[role] || 0) - 1);
            if (next[role] === 0) delete next[role];
            send({ type: "host_set_setup", setup: next });
          };

          const plus = document.createElement("button");
          plus.textContent = "+1";
          plus.onclick = () => {
            const next = { ...currentSetup };
            next[role] = Number(next[role] || 0) + 1;
            send({ type: "host_set_setup", setup: next });
          };

          row.appendChild(label);
          row.appendChild(minus);
          row.appendChild(plus);
          root.appendChild(row);
        });
    }

    // 神职（每次添加固定 +1）
    {
      const { row, sel } = makeDropdown(GOD_OPTIONS, "godRole", "神职");
      const chosen = document.createElement("div");
      chosen.className = "small";
      chosen.textContent = `已选：${pickSetup(currentSetup, GOD_OPTIONS.map((x) => x.id))}`;
      const addBtn = document.createElement("button");
      addBtn.textContent = "加入×1";
      addBtn.onclick = () => {
        const role = sel.value;
        if (!role) return;
        currentSetup[role] = Number(currentSetup[role] || 0) + 1;
        send({ type: "host_set_setup", setup: currentSetup });
      };
      row.appendChild(addBtn);
      root.appendChild(row);
      root.appendChild(chosen);
    }

    // 狼牌（可多个 + 数字）
    {
      const { row, sel } = makeDropdown(WOLF_OPTIONS, "wolfRole", "狼牌");
      const chosen = document.createElement("div");
      chosen.className = "small";
      chosen.textContent = `已选：${pickSetup(currentSetup, WOLF_OPTIONS.map((x) => x.id))}`;
      const num = document.createElement("input");
      num.type = "number";
      num.min = "1";
      num.max = "20";
      num.value = "1";
      num.style.width = "90px";
      const addBtn = document.createElement("button");
      addBtn.textContent = "加入";
      addBtn.onclick = () => {
        const role = sel.value;
        const n = Number(num.value || 0);
        if (!role || !Number.isFinite(n) || n <= 0) return;
        currentSetup[role] = Number(currentSetup[role] || 0) + n;
        send({ type: "host_set_setup", setup: currentSetup });
      };
      row.appendChild(num);
      row.appendChild(addBtn);
      root.appendChild(row);
      root.appendChild(chosen);
    }

    // 民牌（可多个 + 数字）
    {
      const { row, sel } = makeDropdown(VILL_OPTIONS, "villRole", "民牌");
      const chosen = document.createElement("div");
      chosen.className = "small";
      chosen.textContent = `已选：${pickSetup(currentSetup, VILL_OPTIONS.map((x) => x.id))}`;
      const num = document.createElement("input");
      num.type = "number";
      num.min = "1";
      num.max = "20";
      num.value = "1";
      num.style.width = "90px";
      const addBtn = document.createElement("button");
      addBtn.textContent = "加入";
      addBtn.onclick = () => {
        const role = sel.value;
        const n = Number(num.value || 0);
        if (!role || !Number.isFinite(n) || n <= 0) return;
        currentSetup[role] = Number(currentSetup[role] || 0) + n;
        send({ type: "host_set_setup", setup: currentSetup });
      };
      row.appendChild(num);
      row.appendChild(addBtn);
      root.appendChild(row);
      root.appendChild(chosen);
    }

    const rowSetupActions = document.createElement("div");
    rowSetupActions.className = "controlsRow";
    const resetBtn = document.createElement("button");
    resetBtn.textContent = "清空板子";
    resetBtn.onclick = () => send({ type: "host_set_setup", setup: {} });
    rowSetupActions.appendChild(resetBtn);
    root.appendChild(rowSetupActions);

    const note2 = document.createElement("div");
    note2.className = "small";
    note2.textContent = "随机发牌：先配板子（人数=角色数量合计），再入座，然后开始游戏会按座位随机私发身份。";
    root.appendChild(note2);

    const seatGate = setupCount <= 0;
    const seatGateNote = document.createElement("div");
    seatGateNote.className = "small";
    seatGateNote.textContent = seatGate
      ? "请先配板子（至少选择 1 张牌）后，才开放入座。"
      : "板子已配置，现在可以入座。";
    root.appendChild(seatGateNote);

    renderSeatSelection(root, { mySeat, maxSeats: maxSeats || setupCount, disabled: seatGate });

    const row2 = document.createElement("div");
    row2.className = "controlsRow";
    const dealBtn = document.createElement("button");
    dealBtn.className = "primary";
    dealBtn.textContent = "发身份（进入白天）";
    dealBtn.onclick = () => send({ type: "start_game" });
    const endBtn = document.createElement("button");
    endBtn.className = "danger";
    endBtn.textContent = "强制结束";
    endBtn.onclick = () => send({ type: "force_end" });
    row2.appendChild(dealBtn);
    row2.appendChild(endBtn);
    root.appendChild(row2);

    const note = document.createElement("div");
    note.className = "small";
    note.textContent = "先配板子并入座 → 发身份（进入白天，右上角显示身份）→ 房主再点击“开始游戏（进入黑夜）”。";
    root.appendChild(note);
    return;
  }

  // Host controls（白天首轮：已发身份，等待进入黑夜）
  if (state.isHost && phase === "day") {
    const row = document.createElement("div");
    row.className = "controlsRow";
    const startNightBtn = document.createElement("button");
    startNightBtn.className = "primary";
    startNightBtn.textContent = "开始游戏（进入黑夜）";
    startNightBtn.onclick = () => send({ type: "host_start_night" });
    row.appendChild(startNightBtn);
    root.appendChild(row);
  }

  // Host restart button (any non-lobby phase)
  if (state.isHost && phase !== "lobby" && phase !== "ended") {
    const row = document.createElement("div");
    row.className = "controlsRow";
    const restartBtn = document.createElement("button");
    restartBtn.className = "danger";
    restartBtn.textContent = "重新开始（回到大厅）";
    restartBtn.onclick = () => {
      const ok = confirm("确定要重新开始吗？将清空本局进度并重新发牌。");
      if (!ok) return;
      send({ type: "host_restart" });
    };
    row.appendChild(restartBtn);
    root.appendChild(row);
  }

  // Non-host lobby: wait for host to configure setup, then allow seats
  if (phase === "lobby") {
    const gate = maxSeats <= 0;
    const note = document.createElement("div");
    note.className = "small";
    note.textContent = gate ? "等待房主先配板子（人数=角色数量合计）后才能入座。" : `板子已配好（${maxSeats} 人），可以入座。`;
    root.appendChild(note);
    renderSeatSelection(root, { mySeat, maxSeats, disabled: gate });
    return;
  }

  // Night actions
  if (phase === "night") {
    const row = document.createElement("div");
    row.className = "controlsRow";

    if (alive && (state.myRole === "werewolf" || state.myRole === "wolf_king" || state.myRole === "white_wolf_king" || state.myRole === "wolf_beauty")) {
      const options = aliveTargetOptions(false); // 允许刀自己
      const sel = makeSelect(options, "wolfTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "提交刀人";
      btn.onclick = () => send({ type: "wolf_kill", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);

      // 显示狼队友信息（谁是狼美人 / 狼王 / 白狼王 / 普通狼）
      if (Array.isArray(state.nightHint?.wolf_allies)) {
        const info = document.createElement("div");
        info.className = "small";
        const parts = state.nightHint.wolf_allies.map((a) => {
          const seat = a.seat ? `${a.seat}号` : "";
          const roleName = roleLabel(a.role);
          return `${seat}${a.name}（${roleName}）`;
        });
        info.textContent = `你的狼队友：${parts.join("，") || "（只有你一只狼）"}`;
        root.appendChild(info);
      }

      // 狼人夜聊输入框（仅在狼人睁眼阶段night_step === "wolf"时展示更合理，
      // 但这里简单根据 night_step 是否为 "wolf" 决定是否可用）
      const chatRow = document.createElement("div");
      chatRow.className = "controlsRow";
      const chatInput = document.createElement("input");
      chatInput.type = "text";
      chatInput.placeholder = "对狼队友说点什么…（仅狼人可见）";
      const chatBtn = document.createElement("button");
      chatBtn.textContent = "发送给狼队友";
      chatBtn.onclick = () => {
        const text = chatInput.value.trim();
        if (!text) return;
        send({ type: "wolf_chat", text });
        chatInput.value = "";
      };
      chatRow.appendChild(chatInput);
      chatRow.appendChild(chatBtn);
      root.appendChild(chatRow);
    }

    if (alive && state.myRole === "seer") {
      const options = aliveTargetOptions(false);
      const sel = makeSelect(options, "seerTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "提交验人";
      btn.onclick = () => send({ type: "seer_check", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);

      const last = state.seerLast;
      if (last && last.target_name) {
        const info = document.createElement("div");
        info.className = "small";
        info.textContent = `最新查验：${last.target_name} = ${last.is_werewolf ? "狼人" : "好人"}`;
        root.appendChild(info);
      }
    }

    if (alive && state.myRole === "guard") {
      const options = [{ id: "", name: "（不守护任何人）" }, ...aliveTargetOptions(false)];
      const sel = makeSelect(options, "guardTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "提交守护";
      btn.onclick = () => send({ type: "guard_protect", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);
    }

    if (alive && state.myRole === "dreamer") {
      // 摄梦人必须射一人（可以射自己）：不提供“空选项”
      const options = aliveTargetOptions(false);
      const sel = makeSelect(options, "dreamerTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "提交摄梦";
      btn.onclick = () => send({ type: "dreamer_shoot", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);
    }

    if (alive && state.myRole === "wolf_beauty") {
      const options = aliveTargetOptions(false);
      const sel = makeSelect(options, "wolfBeautyTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "魅惑一名玩家";
      btn.onclick = () => send({ type: "wolf_beauty_charm", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);
    }

    if (alive && state.myRole === "hybrid") {
      const options = aliveTargetOptions(false);
      const sel = makeSelect(options, "hybridTarget");
      const btn = document.createElement("button");
      btn.className = "primary";
      btn.textContent = "选择大哥";
      btn.onclick = () => send({ type: "hybrid_choose", target_id: sel.value });
      row.appendChild(sel);
      row.appendChild(btn);
    }

    if (state.myRole === "hunter") {
      const note = document.createElement("div");
      note.className = "small";
      const status = state.hunterStatus;
      if (status?.can_shoot === false) {
        note.textContent = "猎人状态：无法开枪（本局你不能带人）";
      } else if (status?.can_shoot === true) {
        note.textContent = "猎人状态：可以开枪！（请线下告知法官你要带走谁）";
        note.style.color = "#ff4d4f";
        note.style.fontWeight = "600";
      } else {
        note.textContent = "猎人状态：未知（本夜尚未结算）";
      }
      root.appendChild(note);
    }

    if (state.myRole === "wolf_king") {
      const note = document.createElement("div");
      note.className = "small";
      if (state.wolfKingStatus?.can_shoot === false) note.textContent = "狼王状态：无法开枪";
      else if (state.wolfKingStatus?.can_shoot === true) note.textContent = "狼王状态：正常";
      else note.textContent = "狼王状态：未知";
      root.appendChild(note);
    }

    if (alive && state.myRole === "witch") {
      const wrap = document.createElement("div");
      wrap.className = "controlsRow";

      const hint = document.createElement("div");
      hint.className = "small";
      if (state.nightHint?.wolf_target_name) {
        const seat = state.nightHint.wolf_target_seat ? `${state.nightHint.wolf_target_seat}号` : "";
        hint.textContent = `今晚被刀：${seat}${state.nightHint.wolf_target_name}`;
      } else {
        hint.textContent = "今晚被刀：等待狼人提交刀人后显示";
      }
      root.appendChild(hint);

      const saveBtn = document.createElement("button");
      saveBtn.className = "primary";
      const saveUsed = !!state.private?.witch_save_used;
      const selfTarget = state.nightHint?.wolf_target_id && state.playerId && state.nightHint.wolf_target_id === state.playerId;
      if (selfTarget) {
        saveBtn.textContent = "无法自救（你被刀）";
        saveBtn.disabled = true;
      } else {
        saveBtn.textContent = saveUsed ? "解药已用完" : "使用解药（救人）";
        saveBtn.disabled = saveUsed;
      }
      saveBtn.onclick = () => send({ type: "witch_save", use: true });

      const noSaveBtn = document.createElement("button");
      noSaveBtn.textContent = "不救";
      noSaveBtn.onclick = () => send({ type: "witch_save", use: false });

      const poisonUsed = !!state.private?.witch_poison_used;
      const poisonOptions = [{ id: "", name: "（不使用毒药）" }, ...aliveTargetOptions(false)];
      const poisonSel = makeSelect(poisonOptions, "poisonTarget");
      const poisonBtn = document.createElement("button");
      poisonBtn.className = "primary";
      poisonBtn.textContent = poisonUsed ? "毒药已用完" : "提交毒药";
      poisonBtn.disabled = poisonUsed;
      poisonBtn.onclick = () => send({ type: "witch_poison", target_id: poisonSel.value });

      wrap.appendChild(saveBtn);
      wrap.appendChild(noSaveBtn);
      wrap.appendChild(poisonSel);
      wrap.appendChild(poisonBtn);
      root.appendChild(wrap);
    }

    if (!row.childNodes.length) {
      const note = document.createElement("div");
      note.className = "small";
      note.textContent = alive ? "夜晚：等待各身份操作。" : "你已死亡，等待夜晚结束。";
      root.appendChild(note);
    } else {
      root.appendChild(row);
    }

    return;
  }

  // sheriff phase removed

  // Hunter shot 阶段已在程序中移除：只通过状态提示告知猎人能否开枪，实际开枪线下处理

  // Day actions（白天：只公布昨夜信息 + 等待）
  if (phase === "day") {
    // 所有人：提示当前是白天，可随时公布昨夜信息
    const note = document.createElement("div");
    note.className = "small";
    note.textContent = "白天：任何人都可以点击“公布昨夜信息”。";
    root.appendChild(note);

    // 任何人在白天都可以手动公布昨夜信息
    const row2 = document.createElement("div");
    row2.className = "controlsRow";
    const revealBtn = document.createElement("button");
    revealBtn.className = "primary";
    revealBtn.textContent = "公布昨夜信息（慎选）";
    revealBtn.onclick = () => send({ type: "host_resolve_night" });
    row2.appendChild(revealBtn);
    root.appendChild(row2);
    return;
  }

  if (phase === "ended") {
    const note = document.createElement("div");
    note.className = "small";
    note.textContent = "游戏已结束。刷新页面可重新开房。";
    root.appendChild(note);
  }
}

function initUI() {
  $("createBtn").onclick = () => {
    const name = $("nameInput").value.trim();
    if (!name) return setHint("请输入名字");
    send({ type: "create_room", name });
  };

  $("joinBtn").onclick = () => {
    const name = $("nameInput").value.trim();
    const code = $("codeInput").value.trim().toUpperCase();
    if (!name) return setHint("请输入名字");
    if (!code) return setHint("请输入房间号");
    send({ type: "join_room", name, code });
  };
}

connect();
loadAudioEnabled();
initUI();
