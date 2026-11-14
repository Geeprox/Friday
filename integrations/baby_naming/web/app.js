"use strict";

const byId = (id) => document.getElementById(id);
const roles = { husband: ["老公", "husband"], wife: ["老婆", "wife"] };
const modelChoices = [
  ["luna-low", "Luna 低"],
  ["sol-high", "6.1-Sol 高"],
];
const modelLabels = new Map(modelChoices);
const ratings = { dislike: "不喜欢", later: "待定", like: "喜欢" };
const state = { role: null, direction: "girl", book: null, busy: false, poll: null, recordError: null };
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
const portraitCard = matchMedia("(max-width: 720px)");
let showingSettings = false;
let openSettingsFromWorkbench = false;
let gestureGuideRead = false;
let deckModule;
let cardMotion;
let cardStack;
const sessionRenewals = new Map();
const deckPalettes = {
  boy: ["relief", "pine", "marsh"],
  girl: ["lotus", "cranes", "plum"],
};

function cardArtwork(candidate, direction) {
  const palette = deckPalettes[direction];
  // 同一候选在双方设备上用同一纹样，包括复看。
  return palette[(candidate - 1) % palette.length];
}

function showDeck(face, motion) {
  if (reducedMotion.matches || showingSettings || motion.scene || motion.mount) return;
  deckModule ||= import("./jade-card.js").catch(() => null);
  // 浏览器先画出姓名和静态卡面，WebGL 再接管灯光与厚度。
  motion.mount = requestAnimationFrame(() => {
    motion.mount = requestAnimationFrame(async () => {
      const module = await deckModule;
      motion.mount = 0;
      if (cardMotion !== motion || !face.isConnected || showingSettings ||
          reducedMotion.matches || motion.leaving || motion.scene || !module) return;
      const stage = face.querySelector(".jade-stage");
      motion.scene = module.attachCard(stage, face.closest(".card-stack").dataset.direction,
        stage.dataset.artwork, portraitCard.matches);
      motion.paint(motion.x, motion.y);
    });
  });
}

reducedMotion.addEventListener("change", () => {
  if (!cardMotion) return;
  if (reducedMotion.matches) {
    cardMotion.scene?.dispose();
    cardMotion.scene = null;
    cardMotion.paint(0, 0);
  } else {
    const face = cardStack?.front?.face;
    if (face) showDeck(face, cardMotion);
  }
});

portraitCard.addEventListener("change", () => {
  if (showingSettings || !cardMotion) return;
  cardMotion.scene?.dispose();
  cardMotion.scene = null;
  const face = cardStack?.front?.face;
  if (face) showDeck(face, cardMotion);
});

function releaseDeck() {
  if (!cardMotion) return;
  cancelAnimationFrame(cardMotion.mount);
  cardMotion.stop();
  cardMotion.scene?.dispose();
  cardMotion = null;
}

function clearStack() {
  releaseDeck();
  cardStack = null;
}
const knownErrors = {
  key_unavailable: "模型服务还没配置好。已保存的记录可以继续查看。",
  key_rejected: "模型服务未接受当前凭据。可以先看已有名字。",
  quota: "模型服务额度不足。已保存的记录可以继续查看。",
  permission: "当前模型权限不足。可以先看已有名字。",
  model_access: "当前档位不可用。可以改选另一档，或继续看已有名字。",
  invalid_model_profile: "请选择可用的搜索档位。",
  budget_or_stale: "现在的搜索额度不足。可以先看已有名字，稍后再试。",
  rate_limit: "模型服务正在限流。稍后可以重试。",
  provider_connection: "模型服务连接失败。稍后可以重试。",
  provider_output: "模型没有给出完整结果。稍后可以重试。",
  invalid_plan: "这次没形成可用的搜索计划，可以再试一次。",
  incomplete_audit: "盲审还缺结果。可以稍后再找一组。",
  try_later: "刚刚搜索过，稍后再试。",
  task_not_current: "当前卡片已经更新，请继续查看。",
  name_not_ready: "这条名字目前还不能确认，先查看双方的最新记录。",
  name_not_available: "这条名字还没有你的首看判断。",
  seed_missing: "工作台资料还没准备好。",
  interrupted: "上次搜索中断了，可以重新找一组。",
  connection: "连接失败。请检查家庭网络后再试一次。",
  paused: "这个方向已暂停。可以先看共同备选。",
};

function make(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text) element.textContent = text;
  return element;
}

function icon(name) {
  const svg = byId("icons").content.firstElementChild.cloneNode(true);
  svg.querySelector("use").setAttribute("href", "./icons.svg#" + name);
  return svg;
}

function button(label, action, className = "action", symbol) {
  const b = make("button", className);
  b.type = "button";
  b.dataset.actionable = "yes";
  if (symbol) b.append(icon(symbol));
  b.append(document.createTextNode(label));
  b.addEventListener("click", action);
  b.disabled = state.busy;
  return b;
}

function announce(message) {
  byId("live").textContent = message;
}

function showError(code) {
  const target = state.role && state.book ? "task-error" : "entry-error";
  for (const id of ["task-error", "entry-error"]) {
    const active = code && id === target;
    const e = byId(id);
    e.hidden = !active;
    e.textContent = active ? knownErrors[code] || "操作没完成。连接恢复后可以重试。" : "";
  }
}

function saved(message) {
  const receipt = byId("saved");
  receipt.hidden = !message;
  receipt.textContent = message || "";
  if (message) announce(message);
}

function focusTask() {
  byId("desk-title").focus({ preventScroll: true });
}

async function api(route, data = {}, signal, recoverSession = true) {
  const scope = { direction: state.direction, role: state.role, ...data };
  let response;
  try {
    response = await fetch("./api/" + route, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scope),
      signal,
    });
  } catch {
    throw new Error("connection");
  }
  const result = await response.json().catch(() => { throw new Error("connection"); });
  if (response.status === 401 && result.error === "choose_role" &&
      recoverSession && route !== "join" && Object.hasOwn(roles, scope.role)) {
    // 内网服务更新后，使用这个页面已经选过的身份恢复会话。
    const role = scope.role;
    if (!sessionRenewals.has(role)) {
      const renewal = api("join", { role }, undefined, false).finally(() => sessionRenewals.delete(role));
      sessionRenewals.set(role, renewal);
    }
    await sessionRenewals.get(role);
    return api(route, scope, signal, false);
  }
  if (!response.ok) throw new Error(result.error || "connection");
  return result;
}

function rememberRole() {
  sessionStorage.setItem("friday-baby-naming-role", state.role || "");
  sessionStorage.setItem("friday-baby-naming-direction", state.direction);
}

function selectView() {
  const loaded = Boolean(state.role && state.book?.role === state.role);
  byId("entry").hidden = loaded;
  const nextSettings = loaded && location.hash === "#settings";
  byId("work").hidden = !loaded || nextSettings;
  byId("settings-panel").hidden = !nextSettings;
  if (nextSettings && !showingSettings) {
    clearStack();
    byId("settings-result").hidden = true;
    byId("settings-title").focus({ preventScroll: true });
  }
  showingSettings = nextSettings;
  byId("identity").hidden = !loaded;
  byId("settings-toggle").hidden = !loaded;
  document.body.dataset.role = loaded ? state.role : "";
  if (loaded) {
    const [label, symbol] = roles[state.role];
    byId("identity").replaceChildren(icon(symbol), label);
    byId("identity").setAttribute("aria-label", label + "，切换身份");
  }
}

function disable() {
  document.querySelectorAll("[data-actionable], [data-direction], [data-join], #identity, #undo, #settings-toggle")
    .forEach((item) => { item.disabled = state.busy; });
}

async function work(action, focus = false, recordTarget = null) {
  if (state.busy) return;
  state.recordError = null;
  state.busy = true;
  disable();
  showError("");
  saved("");
  let failed = false;
  clearTimeout(state.poll);
  try {
    await action();
  } catch (error) {
    failed = true;
    if (error.message === "choose_role") {
      state.role = null;
      state.book = null;
      rememberRole();
    } else {
      if (recordTarget) {
        state.recordError = { ...recordTarget, code: error.message };
      } else {
        showError(error.message);
      }
      if (error.message === "task_not_current" && state.role) {
        state.book = await api("next").catch(() => state.book);
      }
    }
  } finally {
    state.busy = false;
    render();
    disable();
    if (focus && state.role && state.book) focusTask();
    if (failed && !state.role) document.querySelector("[data-join]").focus();
    schedule();
  }
}

async function next() {
  state.book = await api("next");
}

function join(role) {
  return work(async () => {
    await api("join", { role });
    const book = await api("next", { role });
    if (book.role !== role) throw new Error("choose_role");
    state.role = role;
    state.book = book;
    rememberRole();
  }, true);
}

function goBackToBook(event) {
  event?.preventDefault();
  if (openSettingsFromWorkbench) history.back();
  else {
    history.replaceState(null, "", location.pathname);
    syncPage();
  }
}

function syncPage() {
  const wasSettings = showingSettings;
  render();
  if (wasSettings && !showingSettings && state.role) {
    openSettingsFromWorkbench = false;
    byId("settings-toggle").focus({ preventScroll: true });
  }
}

function clearGestureGuide() {
  gestureGuideRead = true;
  setTimeout(() => {
    try { localStorage.setItem("friday-naming-gestures", "seen"); } catch {}
  }, 0);
  document.querySelector(".gesture-guide")?.remove();
}

function gestureGuide() {
  try { if (localStorage.getItem("friday-naming-gestures")) gestureGuideRead = true; } catch {}
  if (gestureGuideRead) return null;
  const guide = make("aside", "gesture-guide");
  guide.append(make("p", "", "左划不喜欢 · 下划待定 · 双击喜欢"),
    button("知道了", clearGestureGuide, "quiet"));
  return guide;
}

function renderSettings() {
  const profile = state.book.model_profile;
  const picker = byId("model-profile");
  picker.value = modelLabels.has(profile) ? profile : "sol-high";
  byId("settings-note").textContent = picker.value === "sol-high"
    ? "默认档。适合深入比较，通常更慢、用量更高。"
    : "适合先多试一轮，通常更快。";
}

function search() {
  return work(async () => {
    state.book = await api("search");
    announce("AI 正在找名字。你可以离开，回来接着看。");
  }, true);
}

async function update() {
  if (!state.role || !state.book || state.busy || document.hidden) return;
  const role = state.role, direction = state.direction;
  const previous = state.book;
  const result = await api("state").catch((error) => ({ fetchError: error.message }));
  if (state.busy || state.role !== role || state.direction !== direction || state.book !== previous) {
    schedule();
    return;
  }
  if (result.fetchError && result.fetchError !== "choose_role") {
    schedule();
    return;
  }
  if (result.role !== role) {
    state.role = null;
    state.book = null;
    rememberRole();
    render();
    return;
  }
  const taskChanged = result.task?.id !== previous.task?.id;
  const previewChanged = JSON.stringify(result.preview) !== JSON.stringify(previous.preview);
  const settingsChanged = result.model_profile !== previous.model_profile;
  if (settingsChanged) {
    state.book = result;
    renderSettings();
  }
  const modelChanged = JSON.stringify(result.model) !== JSON.stringify(previous.model);
  if (!result.paused && !result.task && (result.review_available || result.available_names > 0 ||
      (result.model?.state === "complete" && result.model?.outcome === "ready" && modelChanged))) {
    await work(next);
    return;
  }
  if (taskChanged || modelChanged || result.paused !== previous.paused) {
    state.book = result;
    if (taskChanged || result.paused !== previous.paused || !result.task) render();
    else if (!showingSettings) renderLists();
  } else if (JSON.stringify(result.common) !== JSON.stringify(previous.common) ||
             JSON.stringify(result.personal) !== JSON.stringify(previous.personal)) {
    state.book = result;
    if (!showingSettings) renderLists();
  }
  if (previewChanged && !taskChanged && cardStack && !showingSettings) {
    state.book = result;
    prepareNext(result);
  }
  schedule();
}

function schedule() {
  clearTimeout(state.poll);
  if (!state.role || !state.book || document.hidden) return;
  const generating = ["queued", "running"].includes(state.book.model?.state);
  state.poll = setTimeout(update, generating ? 2000 : 9500);
}

function submissionEffect(face, value) {
  if (!face) return null;
  delete face.dataset.dragging;
  delete face.dataset.intent;
  cardMotion?.clear();
  if (reducedMotion.matches || typeof face.animate !== "function") return null;
  const mark = value === "like" ? make("span", "heart-feedback") : null;
  if (mark) {
    mark.setAttribute("aria-hidden", "true");
    mark.append(icon("heart"));
    face.append(mark);
  }
  const backed = Boolean(cardStack?.under);
  const delta = value === "dislike" ? (backed ? "translate3d(-122%,-3%,0) rotate(-12deg)" : "translateX(-10%) rotate(-3deg)")
    : value === "later" ? (backed ? "translate3d(4%,110%,0) rotate(5deg)" : "translateY(8%)")
      : "scale(.974) translateY(-6px)";
  const frames = [{ opacity: 1, transform: "none" }];
  if (mark) frames.push({ opacity: 1, transform: "scale(1.008)", offset: .54 });
  else if (backed) frames.push({ opacity: 1, offset: .76,
    transform: value === "dislike" ? "translate3d(-111%,-3%,0) rotate(-11deg)" : "translate3d(3%,101%,0) rotate(4deg)" });
  frames.push({ opacity: backed ? 0 : 1, transform: delta });
  const anim = face.animate(frames, {
    duration: value === "like" ? 295 : 215,
    easing: "cubic-bezier(.22,.8,.26,1)",
    fill: "forwards",
  });
  return {
    finished: anim.finished.catch(() => {}),
    restore() {
      anim.cancel();
      mark?.remove();
    },
  };
}

function submit(task, value, face) {
  const keepFocus = byId("task").contains(document.activeElement);
  return work(async () => {
    const motion = face && cardMotion;
    if (motion) {
      motion.leaving = true;
      motion.stop();
      motion.scene?.freeze();
    }
    const effect = submissionEffect(face, value);
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const waiting = setTimeout(() => {
      byId("saved").hidden = false;
      byId("saved").textContent = "正在保存…";
    }, 360);
    let answer;
    try {
      answer = await api("answer", {
        task: task.id, value, advance: true, prepared: state.book.preview?.id,
      }, controller.signal);
    } catch (error) {
      saved("");
      effect?.restore();
      if (face) {
        delete face.dataset.dragging;
        delete face.dataset.intent;
        if (motion) {
          motion.clear();
          motion.leaving = false;
          motion.pose(0, 0);
          showDeck(face, motion);
        }
      }
      throw error;
    } finally {
      clearTimeout(waiting);
      clearTimeout(timeout);
    }
    state.book = answer;
    saved(task.kind === "compare" ? "选择已保存" : "已保存：" + ratings[value]);
    try {
      await Promise.all([effect?.finished, answer.advanced ? null : next()]);
    } finally {
      if (face) clearGestureGuide();
    }
  }, keepFocus);
}

function gesture(face, task) {
  let start, lastTap, frame = 0, hoverArea, shownIntent;
  const sticker = face.querySelector(".gesture-sticker");
  const foil = face.querySelector(".card-foil");
  const motion = {
    x: 0, y: 0, scene: null, mount: 0, leaving: false,
    pose(x, y) {
      this.x = x;
      this.y = y;
      if (!frame) frame = requestAnimationFrame(() => {
        frame = 0;
        if (!this.leaving) this.paint(this.x, this.y);
      });
    },
    stop() {
      cancelAnimationFrame(frame);
      frame = 0;
    },
    clear() { showIntent(null); },
    paint(x, y) {
      const tiltX = Math.max(-1, Math.min(1, y / 95));
      const tiltY = Math.max(-1, Math.min(1, x / 120));
      const still = reducedMotion.matches;
      const dragX = Math.max(-130, Math.min(100, x * .70));
      const dragY = Math.max(-64, Math.min(110, y * .62));
      foil.style.setProperty("--pose", still || (!x && !y) ? "none" :
        `translate3d(${dragX}px,${dragY}px,0) perspective(610px) rotateX(${tiltX * 13}deg) rotateY(${tiltY * 16}deg) rotateZ(${tiltY * 8}deg)`);
      this.scene?.pose(still ? 0 : tiltX, still ? 0 : tiltY);
    },
  };
  cardMotion = motion;
  const intent = (x, y, limit) => (
    x < -limit && Math.abs(x) > Math.abs(y) * 1.22 ? "dislike"
      : y > limit && Math.abs(y) > Math.abs(x) * 1.22 ? "later" : null
  );
  const showIntent = (action) => {
    if (shownIntent === action) return;
    shownIntent = action;
    if (action) face.dataset.intent = action;
    else delete face.dataset.intent;
    sticker.textContent = action ? ratings[action] : "";
  };
  const reset = () => {
    start = null;
    delete face.dataset.dragging;
    showIntent(null);
    motion.pose(0, 0);
  };
  face.addEventListener("pointerenter", (event) => {
    if (event.pointerType === "mouse") hoverArea = face.getBoundingClientRect();
  });
  face.addEventListener("pointerdown", (event) => {
    if (state.busy || !event.isPrimary || event.button !== 0) return;
    start = { x: event.clientX, y: event.clientY, pointer: event.pointerId, armed: null, moved: false };
    face.setPointerCapture(event.pointerId);
    face.dataset.dragging = "";
  });
  face.addEventListener("pointermove", (event) => {
    if (state.busy) return;
    if (!start) {
      if (event.pointerType === "mouse" && hoverArea) {
        motion.pose((event.clientX - hoverArea.x - hoverArea.width / 2) * .23,
          (event.clientY - hoverArea.y - hoverArea.height / 2) * .23);
      }
      return;
    }
    if (start.pointer !== event.pointerId) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    start.moved ||= Math.hypot(dx, dy) >= 13;
    // 越线只改变当前状态，回划后松手不能提交先前的方向。
    start.armed = intent(dx, dy, 59);
    motion.pose(dx, dy);
    showIntent(intent(dx, dy, 27));
  });
  face.addEventListener("pointerleave", () => {
    hoverArea = null;
    if (!start && !motion.leaving) reset();
  });
  function cancelGesture(event) {
    if (start?.pointer !== event.pointerId) return;
    lastTap = null;
    reset();
  }
  face.addEventListener("pointercancel", cancelGesture);
  face.addEventListener("lostpointercapture", cancelGesture);
  face.addEventListener("pointerup", (event) => {
    if (!start || start.pointer !== event.pointerId || state.busy) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    const moved = start.moved;
    const swipe = intent(dx, dy, 59);
    if (swipe && start.armed === swipe) {
      start = null;
      lastTap = null;
      return void submit(task, swipe, face);
    }
    reset();
    if (moved || Math.hypot(dx, dy) >= 13) {
      lastTap = null;
      return;
    }
    const now = performance.now();
    if (lastTap && now - lastTap.time < 350 && Math.hypot(event.clientX - lastTap.x, event.clientY - lastTap.y) < 38) {
      lastTap = null;
      void submit(task, "like", face);
    } else lastTap = { time: now, x: event.clientX, y: event.clientY };
  });
  showDeck(face, motion);
}

function buildCard(item, direction) {
  const face = make("div", "face tap");
  const foil = make("div", "card-foil");
  const stage = make("div", "jade-stage");
  stage.setAttribute("aria-hidden", "true");
  stage.dataset.artwork = cardArtwork(item.id, direction);
  foil.append(stage);
  const inner = make("div", "card-text");
  inner.append(make("div", "name", item.name));
  if (item.pinyin) inner.append(make("div", "pinyin", item.pinyin));
  foil.append(inner);
  face.append(foil, make("span", "gesture-sticker"));
  let caption;
  if (item.card_note || item.card_source) {
    caption = make("div", "card-caption");
    if (item.card_note) caption.append(make("p", "", item.card_note));
    if (item.card_source) caption.append(make("p", "card-source", item.card_source));
  }
  return { signature: JSON.stringify(item), face, caption };
}

function prepareNext(book) {
  if (!cardStack) return;
  if (book.preview && cardStack.under?.signature === JSON.stringify(book.preview)) return;
  cardStack.under?.face.remove();
  cardStack.under = null;
  if (!book.preview) return;
  const under = buildCard(book.preview, book.direction);
  under.face.classList.add("card-under");
  under.face.inert = true;
  under.face.setAttribute("aria-hidden", "true");
  cardStack.under = under;
  cardStack.element.prepend(under.face);
}

function renderTask(book) {
  const task = book.task;
  const box = byId("task");
  if (task.kind === "compare") {
    clearStack();
    box.replaceChildren();
    box.append(make("p", "question", task.question));
    const pair = make("div", "pair");
    for (const item of task.names) pair.append(button(item.name, () => submit(task, String(item.id)), ""));
    box.append(pair);
    const more = make("div", "actions");
    for (const [label, value] of [["都喜欢", "both"], ["都不喜欢", "neither"], ["说不准", "later"]]) {
      more.append(button(label, () => submit(task, value)));
    }
    box.append(more, make("p", "hint", "选你现在更愿意留下的名字。"));
    return;
  }
  const sameScope = cardStack?.role === book.role && cardStack.direction === book.direction;
  if (sameScope && cardStack.task === task.id && !cardStack.element.classList.contains("card-waiting")) {
    prepareNext(book);
    return;
  }
  releaseDeck();
  if (!sameScope) {
    const element = make("div", "card-stack");
    element.dataset.direction = book.direction;
    cardStack = { element, direction: book.direction, role: book.role };
    box.replaceChildren(element);
  }
  const item = task.names[0];
  const { element, under, front } = cardStack;
  element.classList.remove("card-waiting");
  const promoted = under?.signature === JSON.stringify(item);
  const current = promoted ? under : buildCard(item, book.direction);
  const { face, caption } = current;
  // 已经露出的底卡直接成为面卡，DOM 节点留在原卡堆里。
  if (promoted) {
    face.classList.remove("card-under");
    face.inert = false;
    face.removeAttribute("aria-hidden");
  } else {
    element.append(face);
    under?.face.remove();
  }
  front?.face.remove();
  cardStack.task = task.id;
  cardStack.front = current;
  cardStack.under = null;
  prepareNext(book);
  while (element.nextSibling) element.nextSibling.remove();
  gesture(face, task);
  if (caption) box.append(caption);
  const actions = make("div", "actions verdict-actions");
  for (const [value, symbol] of [["dislike", "left"], ["later", "down"], ["like", "heart"]]) {
    actions.append(button(ratings[value], () => submit(task, value, face), "action", symbol));
  }
  box.append(actions);
  const guide = gestureGuide();
  if (guide) box.append(guide);
}

function renderEmpty(book) {
  const box = byId("task");
  const panel = make("div", "empty");
  let title = "从一组新名字开始";
  let titleClass = "";
  let confirmation = "";
  let copy = "AI 会先找素材、组合，再盲审一遍。";
  let searchLabel = "请 AI 找名字";
  let action = null;
  if (book.paused) {
    const winner = book.common.find((item) => item.id === book.paused);
    title = winner ? winner.name : "这个方向已暂停";
    titleClass = winner ? "name confirmed-name" : "";
    confirmation = winner ? "双方都愿意使用" : "";
    copy = "可以先留在共同备选，想继续时再打开。";
    searchLabel = null;
    action = button("继续找名字", () => work(async () => {
      await api("resume");
      await next();
    }));
  } else if (["queued", "running"].includes(book.model?.state)) {
    title = book.completed ? "正在准备下一张" : "正在找名字";
    copy = "审核通过后会自动显示。";
    searchLabel = null;
  } else if (book.model?.state === "failed") {
    title = "这轮搜索没完成";
    copy = knownErrors[book.model.outcome] || "可以稍后再试，之前的判断都还在。";
    searchLabel = "重新搜索";
  } else if (book.model?.outcome === "no_approved") {
    title = "这轮没有合适的名字";
    copy = "AI 可以换一条思路，继续找一组。";
    searchLabel = "换个方向找";
  } else if (book.completed) {
    title = "准备下一张名字卡";
    copy = "可以先看你的记录和共同备选。";
    searchLabel = "继续找名字";
  }
  panel.append(make("h3", titleClass, title));
  if (confirmation) panel.append(make("p", "confirmation", confirmation));
  panel.append(make("p", "", copy));
  if (action) panel.append(action);
  else if (searchLabel && !book.simulation) panel.append(button(searchLabel, search, "action primary"));
  const keepCard = !book.paused && cardStack?.role === book.role &&
    cardStack.direction === book.direction && cardStack.front;
  if (keepCard) {
    releaseDeck();
    const { element, front } = cardStack;
    cardStack.under?.face.remove();
    cardStack.under = null;
    for (const animation of front.face.getAnimations()) animation.cancel();
    front.face.querySelector(".heart-feedback")?.remove();
    front.face.inert = true;
    front.face.setAttribute("aria-hidden", "true");
    element.classList.add("card-waiting");
    while (element.nextSibling) element.nextSibling.remove();
    panel.classList.add("refill-status");
    if (["queued", "running"].includes(book.model?.state)) panel.setAttribute("role", "status");
    box.append(panel);
  } else {
    clearStack();
    box.replaceChildren(panel);
  }
}

function nameAction(item, action, common) {
  return work(async () => {
    state.book = await api("name", { id: item.id, action });
    announce(action === "ready" ? "已确认愿意使用" : "已更新");
  }, false, { id: item.id, common });
}

function details(item) {
  if (!item.pinyin && !item.detail && !item.evidence.length) return null;
  const fold = make("details");
  fold.append(make("summary", "", "读音和出处"));
  if (item.pinyin) fold.append(make("p", "", item.pinyin));
  if (item.detail) fold.append(make("p", "", item.detail));
  for (const entry of item.evidence) {
    const line = make("p");
    line.append(document.createTextNode(entry.derivation === "exact" ? "取自 " : "借意自 "));
    const a = make("a", "", entry.title);
    a.href = entry.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    line.append(a);
    fold.append(line, make("p", "", entry.text));
  }
  return fold;
}

function record(item, common) {
  const article = make("article", "record");
  article.dataset.record = String(item.id);
  article.append(make("h3", "record-name", item.name));
  let caption;
  if (common) {
    caption = item.confirmed ? "双方都愿意使用" :
      item.my_ready ? "你已确认，等对方确认" : item.stable ? "两人复看后仍喜欢" : "两人都选了喜欢";
  } else {
    caption = item.excluded ? "已排除" : (ratings[item.my_rating] || "待定");
    if (item.partner_excluded) caption += " · 对方已排除";
    else if (item.partner_rating) caption += " · 对方：" + ratings[item.partner_rating];
    else if (item.origin !== "history") caption += " · 等对方看完";
  }
  article.append(make("p", "record-state", caption));
  const fold = details(item);
  if (fold) article.append(fold);
  if (common && !item.my_ready && item.can_confirm) {
    article.append(button("我愿意用这个名字", () => nameAction(item, "ready", true), "action primary"));
  }
  if (common && item.my_ready) {
    article.append(button("撤回确认", () => nameAction(item, "unready", true), "quiet"));
  }
  if (!common && item.can_exclude && item.origin !== "history") {
    article.append(button(item.excluded ? "撤回排除" : "排除这个名字",
      () => nameAction(item, item.excluded ? "unveto" : "veto", false), "quiet"));
  }
  if (state.recordError?.id === item.id && state.recordError.common === common) {
    const error = make("p", "record-error", knownErrors[state.recordError.code] || "操作没完成，请再试一次。");
    error.setAttribute("role", "alert");
    article.append(error);
  }
  return article;
}

function replaceRecords(container, records, isCommon, empty) {
  const active = document.activeElement;
  const focused = container.contains(active) ? active.closest("[data-record]")?.dataset.record : null;
  const open = new Set([...container.querySelectorAll("article:has(details[open])")].map((row) => row.dataset.record));
  container.replaceChildren(...records.map((item) => record(item, isCommon)));
  if (!records.length) container.append(make("p", "side-empty", empty));
  for (const row of container.querySelectorAll("[data-record]")) {
    const details = row.querySelector("details");
    if (details && open.has(row.dataset.record)) details.open = true;
    if (focused === row.dataset.record) {
      const matches = [...row.querySelectorAll("button, summary, a")];
      (matches.find((item) => item.textContent === active.textContent) || matches[0])?.focus({ preventScroll: true });
    }
  }
}

function renderLists() {
  const { common, personal } = state.book;
  const live = personal.filter((row) => row.origin !== "history");
  replaceRecords(byId("common"), common, true, "两人各自看过并喜欢同一个名字，它会出现在这里。");
  renderPersonal(live);
  byId("record-count").textContent = live.length ? String(live.length) : "";
}

function renderPersonal(records) {
  if (byId("personal-fold").open) replaceRecords(byId("personal"), records, false, "你的判断会保存在这里。");
}

byId("personal-fold").addEventListener("toggle", () => {
  if (state.book) renderPersonal(state.book.personal.filter((row) => row.origin !== "history"));
});

function render() {
  selectView();
  if (!state.role || state.book?.role !== state.role) {
    clearStack();
    for (const id of ["task", "common", "personal"]) byId(id).replaceChildren();
    return;
  }
  const book = state.book;
  byId("simulation").hidden = !book.simulation;
  renderSettings();
  if (showingSettings) return;
  byId("undo").hidden = !book.can_undo || Boolean(book.paused);
  byId("progress").textContent = book.completed ? "已看 " + book.completed + " 项" : "";
  byId("desk-title").textContent = { flash: "看名字", compare: "比较两个名字", review: "复看" }[book.task?.kind] ||
    (book.paused ? "先留这个名字" : "下一步");
  document.querySelectorAll("[data-direction]").forEach((b) => {
    b.setAttribute("aria-pressed", String(b.dataset.direction === state.direction));
  });
  if (book.task && !book.paused) renderTask(book);
  else renderEmpty(book);
  renderLists();
}

// 卡面处理自己的双击，页面其他区域仍可滚动和缩放。
byId("task").addEventListener("touchend", (event) => {
  if (event.target.closest(".card-stack") && event.cancelable &&
      event.touches.length === 0 && event.changedTouches.length === 1) event.preventDefault();
}, { passive: false });
byId("task").addEventListener("dblclick", (event) => {
  if (event.target.closest(".card-stack")) event.preventDefault();
});

document.querySelectorAll("[data-join]").forEach((b) => {
  b.addEventListener("click", () => join(b.dataset.join));
});
document.querySelectorAll("[data-direction]").forEach((b) => {
  b.addEventListener("click", () => work(async () => {
    if (state.direction === b.dataset.direction) return;
    const original = state.direction;
    state.direction = b.dataset.direction;
    try {
      await next();
      rememberRole();
    } catch (error) {
      state.direction = original;
      throw error;
    }
  }));
});
byId("settings-toggle").addEventListener("click", (event) => {
  if (state.busy) {
    event.preventDefault();
    return;
  }
  openSettingsFromWorkbench = location.hash !== "#settings";
});
byId("settings-back").addEventListener("click", goBackToBook);
window.addEventListener("hashchange", syncPage);
byId("settings-panel").addEventListener("keydown", (event) => {
  if (event.key === "Escape") goBackToBook(event);
});
byId("model-profile").addEventListener("change", async (event) => {
  if (state.busy || !state.role) return;
  const profile = event.target.value;
  const role = state.role, direction = state.direction;
  const panelStatus = byId("settings-result");
  state.busy = true;
  clearTimeout(state.poll);
  disable();
  try {
    const changed = await api("search", { operation: "set_profile", profile });
    if (state.role !== role || state.direction !== direction) return;
    if (changed.task?.id !== state.book?.task?.id || changed.paused !== state.book?.paused) {
      state.book = changed;
      render();
    } else {
      state.book = changed;
      renderSettings();
    }
    panelStatus.textContent = "已保存";
  } catch (error) {
    renderSettings();
    panelStatus.textContent = knownErrors[error.message] || "没有保存成功，请再试一次。";
  } finally {
    state.busy = false;
    disable();
    panelStatus.hidden = false;
    schedule();
  }
});
byId("identity").addEventListener("click", () => {
  if (location.hash) history.replaceState(null, "", location.pathname);
  openSettingsFromWorkbench = false;
  clearTimeout(state.poll);
  state.role = null;
  state.book = null;
  state.recordError = null;
  rememberRole();
  saved("");
  showError("");
  render();
  document.querySelector("[data-join]").focus();
});
byId("undo").addEventListener("click", () => work(async () => {
  state.book = await api("undo");
  saved("已撤销，可以重新判断");
}, true));
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    if (state.book?.task) update();
    else if (state.role && !state.busy) work(next);
  } else clearTimeout(state.poll);
});

const savedDirection = sessionStorage.getItem("friday-baby-naming-direction");
if (["girl", "boy"].includes(savedDirection)) state.direction = savedDirection;
const savedRole = sessionStorage.getItem("friday-baby-naming-role");
if (Object.hasOwn(roles, savedRole)) {
  work(async () => {
    const recovered = await api("state", { role: savedRole });
    if (recovered.role !== savedRole) return;
    state.role = savedRole;
    state.book = recovered;
    await next();
  });
}
