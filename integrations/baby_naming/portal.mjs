/** 仅由已经核对来源的 Friday 家庭内网入口调用。 */
import http from "node:http";
import { readFileSync } from "node:fs";

const prefix = "/apps/baby-naming";
const paths = new Set(["", "style.css", "app.js", "icons.svg", "jade-card.js", "jade-relief.webp",
  "vendor/three.module.js", "vendor/three.core.js", "health",
  "api/join", "api/state", "api/next", "api/answer", "api/undo", "api/name", "api/resume", "api/search"]);
const proof = /^[A-Za-z0-9_-]{43}$/;

export function babyNamingRoute(req, res, url, settings) {
  if (url.pathname !== prefix && !url.pathname.startsWith(prefix + "/")) return false;
  // 只承认现场反代追加的最后一跳。转发链前面由来访者提供的值不用于授权。
  const peer = (req.socket.remoteAddress || "").replace(/^::ffff:/, "");
  const chain = req.headers[settings.forwardedHeader || "x-forwarded-for"];
  const last = typeof chain === "string" ? chain.split(",").pop().trim().replace(/^::ffff:/, "") : "";
  const source = settings.trustedProxy && peer === settings.trustedProxy ? last : peer;
  if (settings.lan && (!source.startsWith(settings.lan + ".") || !/^[0-9.]+$/.test(source))) {
    res.writeHead(404).end();
    return true;
  }
  const host = req.headers.host;
  const allowed = new Set(settings.origins);
  if (![...allowed].some((origin) => new URL(origin).host === host)) {
    res.writeHead(404).end();
    return true;
  }
  if (url.search) {
    res.writeHead(404).end();
    return true;
  }
  if (url.pathname === prefix) {
    res.writeHead(308, { Location: prefix + "/", "Cache-Control": "no-store" }).end();
    return true;
  }
  const path = url.pathname.slice(prefix.length + 1);
  if (!paths.has(path)) {
    res.writeHead(404).end();
    return true;
  }
  const method = req.method;
  if (path.startsWith("api/") ? method !== "POST" : !["GET", "HEAD"].includes(method)) {
    res.writeHead(405).end();
    return true;
  }
  const origin = req.headers.origin;
  if (method === "POST" && (!allowed.has(origin) || new URL(origin).host !== host)) {
    res.writeHead(403).end();
    return true;
  }
  if (method === "POST" && (req.headers["transfer-encoding"] ||
      !/^\d{1,4}$/.test(req.headers["content-length"] || "") ||
      Number(req.headers["content-length"]) < 1 || Number(req.headers["content-length"]) > 4096)) {
    res.writeHead(400).end();
    return true;
  }
  let token;
  try {
    token = readFileSync(settings.proof, "utf8").trim();
    if (!proof.test(token)) throw new Error("invalid_proxy_proof");
  } catch {
    res.writeHead(503).end();
    return true;
  }
  const headers = { Host: host, "X-Friday-Naming-Proxy": token };
  for (const field of ["origin", "content-length", "content-type", "cookie"]) {
    if (typeof req.headers[field] === "string") headers[field] = req.headers[field];
  }
  const upstream = http.request({ hostname: "127.0.0.1", port: settings.port || 19146,
    path: url.pathname, method, headers }, (reply) => {
    res.writeHead(reply.statusCode || 502, reply.headers);
    reply.pipe(res);
    reply.on("error", () => res.destroy());
  });
  upstream.setTimeout(12000, () => upstream.destroy());
  upstream.on("error", () => {
    if (!res.headersSent) res.writeHead(502, { "Cache-Control": "no-store" });
    res.end();
  });
  req.on("aborted", () => upstream.destroy());
  res.on("close", () => upstream.destroy());
  req.pipe(upstream);
  return true;
}
