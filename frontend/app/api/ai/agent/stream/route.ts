/**
 * Agent SSE 的流式代理。
 *
 * 为什么需要单独一层：next.config 的 rewrites 会把 /api 代理到 FastAPI，但开发
 * 服务器的代理会把整个响应体缓冲下来（实测首字节 17.4s、全部内容一次到达），
 * SSE 就退化成「等完整结果再一次性显示」。这里用 Route Handler 把上游的
 * ReadableStream 直接透传，首字节才能在 1~2s 内到达浏览器。
 *
 * 该路径比 /api/:path* 的 rewrite 更具体，因此优先命中，其余接口不受影响。
 */

import type { NextRequest } from "next/server";

export const dynamic = "force-dynamic";

const API_BASE = process.env.API_BASE || "http://127.0.0.1:8010";

const SSE_HEADERS: Record<string, string> = {
  "Content-Type": "text/event-stream; charset=utf-8",
  "Cache-Control": "no-cache, no-transform",
  "X-Accel-Buffering": "no",
};

// 连接阶段上限：只限制「多久必须开始返回」，不限制整轮问答时长。
const CONNECT_TIMEOUT_MS = 30_000;

function errorFrame(message: string): Response {
  const frame = "data: " + JSON.stringify({ type: "error", message: message }) + "\n\n";
  return new Response(frame, { status: 200, headers: SSE_HEADERS });
}

export async function POST(request: NextRequest) {
  const body = await request.text();

  // 客户端断开（关标签页 / 点了停止）必须真正中断上游，否则后端会继续把剩余的
  // 检索步骤跑完并计费 —— 用户早已离开，账单还在涨。
  const upstreamAbort = new AbortController();
  const onClientAbort = () => upstreamAbort.abort();
  if (request.signal.aborted) {
    upstreamAbort.abort();
  } else {
    request.signal.addEventListener("abort", onClientAbort, { once: true });
  }
  const connectTimer = setTimeout(() => upstreamAbort.abort(), CONNECT_TIMEOUT_MS);

  let upstream: Response;
  try {
    upstream = await fetch(API_BASE + "/api/ai/agent/stream", {
      method: "POST",
      signal: upstreamAbort.signal,
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
        // 会话 Cookie 必须透传，否则后端网关鉴权会直接 401
        cookie: request.headers.get("cookie") ?? "",
      },
      body: body,
      cache: "no-store",
    });
  } catch {
    clearTimeout(connectTimer);
    return request.signal.aborted
      ? new Response(null, { status: 499 })
      : errorFrame("无法连接后端服务");
  }
  clearTimeout(connectTimer);

  if (!upstream.body) {
    return errorFrame("后端没有返回流式响应");
  }

  return new Response(upstream.body, {
    status: upstream.status,
    headers: SSE_HEADERS,
  });
}
