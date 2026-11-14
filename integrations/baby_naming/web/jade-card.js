import * as THREE from "./vendor/three.module.js";

const tiles = ["relief", "pine", "marsh", "lotus", "cranes", "plum"];
const radius = .115, bevel = .026;
let view;
let settleTimer;

function rounded(w, h, r) {
  const x = w / 2, y = h / 2, shape = new THREE.Shape();
  shape.moveTo(-x + r, -y);
  shape.lineTo(x - r, -y);
  shape.quadraticCurveTo(x, -y, x, -y + r);
  shape.lineTo(x, y - r);
  shape.quadraticCurveTo(x, y, x - r, y);
  shape.lineTo(-x + r, y);
  shape.quadraticCurveTo(-x, y, -x, y - r);
  shape.lineTo(-x, -y + r);
  shape.quadraticCurveTo(-x, -y, -x + r, -y);
  return shape;
}

function insetFoil(w, h, r, tint, opacity) {
  const stops = rounded(w, h, r).getPoints(10);
  const geometry = new THREE.BufferGeometry().setFromPoints(stops.map((point) =>
    new THREE.Vector3(point.x, point.y, .038)));
  const surface = new THREE.LineBasicMaterial({ color: tint, transparent: true, opacity });
  return new THREE.LineLoop(geometry, surface);
}

function build(portrait) {
  const width = portrait ? 2.12 : 3.2, height = portrait ? 3.11 : 1.95;
  const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true, powerPreference: "low-power" });
  renderer.setPixelRatio(Math.min(devicePixelRatio || 1, 1.6));
  renderer.setClearColor(0, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.domElement.setAttribute("aria-hidden", "true");
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, .1, 16);
  camera.position.z = 5;
  const card = new THREE.Group();
  const shape = rounded(width, height, radius);
  const edge = new THREE.ExtrudeGeometry(shape, {
    depth: .098, bevelEnabled: true, bevelSegments: 3, steps: 1,
    bevelThickness: bevel, bevelSize: bevel, curveSegments: 10,
  });
  edge.translate(0, 0, -.098);
  const core = new THREE.MeshStandardMaterial({ color: 0x16362f, roughness: .54, metalness: .18 });
  const brass = new THREE.MeshPhysicalMaterial({
    color: 0xc4a46c, metalness: .62, roughness: .32, clearcoat: .55, clearcoatRoughness: .28,
  });
  card.add(new THREE.Mesh(edge, [core, brass]));

  // 从圆角的实际坐标取图。移动的是实体卡，姓名由可缩放的网页文字绘制。
  const face = new THREE.ShapeGeometry(shape, 12);
  const vertices = face.getAttribute("position");
  const uv = face.getAttribute("uv");
  for (let i = 0; i < vertices.count; i++) {
    uv.setXY(i, vertices.getX(i) / width + .5, vertices.getY(i) / height + .5);
  }
  face.translate(0, 0, bevel + .002);
  const glaze = new THREE.MeshPhysicalMaterial({
    color: 0xffffff, roughness: .71, metalness: .09, clearcoat: .46, clearcoatRoughness: .39,
    specularIntensity: .64, specularColor: 0xd6bc89,
  });
  const front = new THREE.Mesh(face, glaze);
  card.add(front);
  const outer = insetFoil(width - .113, height - .112, .067, 0xb69b63, .64);
  const inner = insetFoil(width - .151, height - .150, .057, 0xe0c590, .30);
  card.add(outer, inner);
  scene.add(card);

  const soft = new THREE.HemisphereLight(0xf5f1df, 0x697d72, 1.63);
  const light = new THREE.DirectionalLight(0xffedd1, 2.02);
  const rim = new THREE.DirectionalLight(0xd3edc9, .86);
  light.position.set(-1.55, 3.35, 5);
  rim.position.set(3.2, -1, .45);
  scene.add(soft, light, rim);

  const layer = { renderer, scene, card, camera, brass, outer, light, core, glaze, rim,
    width, height, portrait, active: null, atlas: null, lost: false, frameWidth: 0, frameHeight: 0 };
  renderer.domElement.addEventListener("webglcontextlost", (event) => {
    event.preventDefault();
    layer.lost = true;
    if (layer.active) delete layer.active.stage.dataset.rendered;
  });
  renderer.domElement.addEventListener("webglcontextrestored", () => {
    layer.lost = false;
    layer.active?.draw();
  });
  return layer;
}

function discard(layer) {
  layer.atlas?.dispose();
  layer.scene.traverse((object) => {
    object.geometry?.dispose();
    const materials = Array.isArray(object.material) ? object.material : [object.material];
    for (const material of materials) material?.dispose();
  });
  layer.renderer.dispose();
  layer.renderer.forceContextLoss();
  if (view === layer) view = null;
}

function selectTile(layer, artwork) {
  const tile = Math.max(0, tiles.indexOf(artwork));
  const texture = layer.atlas;
  const { width, height } = texture.image;
  const slice = layer.portrait ? 720 : 320, base = layer.portrait ? 0 : 1440;
  texture.repeat.set(1 / 3 - 1 / width, (slice - 1) / height);
  texture.offset.set(tile % 3 / 3 + .5 / width, (base + (1 - Math.floor(tile / 3)) * slice + .5) / height);
}

export function attachCard(stage, role, artwork, portrait) {
  if (!stage) return null;
  try {
    if (view && view.portrait !== portrait) {
      view.active?.dispose();
      clearTimeout(settleTimer);
      discard(view);
    }
    if (!view) view = build(portrait);
  } catch {
    return null;
  }
  clearTimeout(settleTimer);
  const layer = view;
  const { renderer, scene, card, camera, brass, outer, light, core, glaze, rim } = layer;
  layer.active?.dispose();
  clearTimeout(settleTimer);
  const rose = role === "wife";
  brass.color.setHex(rose ? 0xd7a585 : 0xc4a46c);
  outer.material.color.setHex(rose ? 0xe1b096 : 0xba9d65);
  core.color.setHex(rose ? 0x432023 : 0x16362f);
  rim.color.setHex(rose ? 0xf5ccae : 0xd3edc9);
  // 被移至新卡时先隐藏上一帧，等新材质画好再替换 CSS 底图。
  stage.append(renderer.domElement);
  let frame = 0, done = false, frozen = false, x = 0, y = 0, wantX = 0, wantY = 0;
  const draw = () => {
    if (!done && !frozen && !frame) frame = requestAnimationFrame(paint);
  };
  const paint = () => {
    frame = 0;
    if (done || frozen || layer.lost || !layer.atlas || !layer.frameWidth || !stage.isConnected) return;
    x += (wantX - x) * .31;
    y += (wantY - y) * .31;
    const changing = Math.abs(x - wantX) + Math.abs(y - wantY) > .008;
    if (!changing) { x = wantX; y = wantY; }
    card.rotation.set(x * .255, y * .30, y * -.12);
    light.position.set(-1.55 + y * 3.55, 3.35 - x * 2.8, 4.8);
    try {
      renderer.render(scene, camera);
      stage.dataset.rendered = "three";
    } catch {
      delete stage.dataset.rendered;
      return;
    }
    if (changing) draw();
  };
  const resize = ([entry]) => {
    const w = Math.round(entry.contentRect.width), h = Math.round(entry.contentRect.height);
    if (!w || !h) return;
    if (layer.frameWidth !== w || layer.frameHeight !== h) {
      const ratio = w / h;
      const v = Math.max(layer.height / 2 + .085, (layer.width / 2 + .09) / ratio);
      camera.left = -v * ratio;
      camera.right = v * ratio;
      camera.bottom = -v;
      camera.top = v;
      camera.updateProjectionMatrix();
      renderer.setSize(w, h, false);
      layer.frameWidth = w;
      layer.frameHeight = h;
    }
    draw();
  };
  const observer = new ResizeObserver(resize);
  const active = {
    stage, draw, artwork,
    pose(tiltX, tiltY) { frozen = false; wantX = tiltX; wantY = tiltY; draw(); },
    freeze() {
      frozen = true;
      cancelAnimationFrame(frame);
      frame = 0;
    },
    dispose() {
      if (done) return;
      done = true;
      observer.disconnect();
      cancelAnimationFrame(frame);
      delete stage.dataset.rendered;
      renderer.domElement.remove();
      if (layer.active === active) layer.active = null;
      // 下一张继续用同一个上下文。离开卡片一段时间后才释放 GPU 资源。
      settleTimer = setTimeout(() => { if (!layer.active) discard(layer); }, 20000);
    },
  };
  layer.active = active;
  if (layer.atlas) selectTile(layer, artwork);
  if (!layer.loadStarted) {
    layer.loadStarted = true;
    new THREE.TextureLoader().load("./jade-relief.webp", (atlas) => {
      if (view !== layer) { atlas.dispose(); return; }
      atlas.colorSpace = THREE.SRGBColorSpace;
      atlas.anisotropy = Math.min(4, renderer.capabilities.getMaxAnisotropy());
      // 各纹样共用同一张 GPU 纹理。切卡时只改变采样窗口。
      layer.atlas = atlas;
      glaze.map = atlas;
      glaze.needsUpdate = true;
      if (layer.active) selectTile(layer, layer.active.artwork);
      layer.active?.draw();
    }, undefined, () => { layer.loadStarted = false; });
  }
  observer.observe(stage);
  return active;
}
