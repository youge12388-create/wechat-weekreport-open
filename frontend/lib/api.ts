/** 轻量 fetch 封装：统一错误、超时。 */

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

/** GET 查询参数：值为 null/undefined/空串的键会被省略（不进入 URL 与缓存键）。 */
export type ApiQueryParams = Record<
  string,
  string | number | boolean | undefined | null
>;

/**
 * GET 响应缓存。
 *
 * 管理端页面切走再切回来时，每个页面都会把自己那几个接口重打一遍，而首页一次要打
 * 六个（其中 overview / departments 是重查询）。这里是**页面级共享的短期缓存**：
 * 同 URL 在 TTL 内直接复用，切页不再把整屏重新拉一次。
 *
 * 三条边界：
 * - 只缓存 GET。任何写操作（POST/PATCH/DELETE）都会清空整个缓存 —— 保存完设置
 *   再切回首页必须看到新数据，宁可多查一次也不能读到写之前的结论。
 * - `/api/auth/*` 一律不缓存：会话状态的过期必须立刻反映到界面上。
 * - 缓存只是「先给一份旧数据」的优化，**不是数据真源**。`useAsyncData` 的
 *   `reload()`（轮询/手动刷新）会先使这个键失效再请求，所以轮询永远拿到新数据。
 */
const GET_CACHE_TTL_MS = 30_000;
const getCache = new Map<string, { at: number; value: unknown }>();

/**
 * 进行中的 GET 请求（同一 URL 并发时复用同一个 Promise）。
 *
 * 没有它时「同一 URL 同时发两次」会真的打两次后端：React StrictMode 在开发期
 * 会双调用 effect（实测 `/api/departments`、`/api/reports` 各请求两遍），
 * 缓存也救不了 —— 两次调用都发生在缓存写入之前。同一 URL 的结果本来就是同一份，
 * 复用 Promise 既不改变语义，也顺带覆盖「用户快速连点周期切换」的场景。
 */
type InFlightGet = { promise?: Promise<unknown>; invalidated: boolean };
const inFlightGets = new Map<string, InFlightGet>();
let activeGetRequests = 0;
const apiLoadingListeners = new Set<(activeCount: number) => void>();

/** Subscribe to network GET activity for the shared, delayed page progress bar. */
export function subscribeToApiLoading(listener: (activeCount: number) => void) {
  apiLoadingListeners.add(listener);
  try {
    listener(activeGetRequests);
  } catch {
    /* A visual progress listener must never interrupt a data request. */
  }
  return () => {
    apiLoadingListeners.delete(listener);
  };
}

function updateApiLoading(delta: 1 | -1) {
  activeGetRequests = Math.max(0, activeGetRequests + delta);
  for (const listener of apiLoadingListeners) {
    try {
      listener(activeGetRequests);
    } catch {
      /* A visual progress listener must never interrupt a data request. */
    }
  }
}

/** 拼出 GET 请求的规范 URL，同时也是缓存键。调用方用它做缓存键，避免两处各拼一套。 */
export function apiCacheKey(
  path: string,
  params?: ApiQueryParams
): string {
  const search = new URLSearchParams();
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        search.set(key, String(value));
      }
    }
  }
  const query = search.toString();
  return query ? `${path}?${query}` : path;
}

/** 读取缓存（同步）；没有或已过期返回 undefined。 */
export function readApiCache<T>(key: string, maxAgeMs = GET_CACHE_TTL_MS): T | undefined {
  const hit = getCache.get(key);
  if (!hit) return undefined;
  if (Date.now() - hit.at > maxAgeMs) {
    getCache.delete(key);
    return undefined;
  }
  return hit.value as T;
}

/**
 * 使某个键失效；不传键则清空。写操作后由本模块自动调用。
 *
 * **同时丢弃进行中的请求**：写操作成功后紧接着重取数据时，若复用一个
 * **写入前**发出的在途响应，拿到的就是旧值 —— 表现为「刚保存的改动又变回去了」。
 * 这类竞态只在真实网络（几百毫秒往返）下才暴露，本机几十毫秒时几乎看不到。
 */
export function invalidateApiCache(key?: string): void {
  if (key === undefined) {
    getCache.clear();
    for (const request of inFlightGets.values()) request.invalidated = true;
    inFlightGets.clear();
  } else {
    getCache.delete(key);
    const request = inFlightGets.get(key);
    if (request) request.invalidated = true;
    inFlightGets.delete(key);
  }
}

function writeApiCache(key: string, value: unknown): void {
  getCache.set(key, { at: Date.now(), value });
}

function cacheableGet(path: string): boolean {
  return !path.startsWith("/api/auth/");
}

/**
 * 401 统一跳登录页。
 * 后端 AuthMiddleware 对未登录请求返回 401；前端任何数据请求吃到 401 都应回到登录页。
 * 已在 /login 时不跳转，避免登录失败时的重定向死循环。
 */
function redirectToLoginIfUnauthorized(status: number) {
  if (
    status === 401 &&
    typeof window !== "undefined" &&
    !window.location.pathname.startsWith("/login")
  ) {
    window.location.href = "/login";
  }
}

/**
 * 从失败响应里尽力提取人类可读的错误信息。
 *
 * 后端有两种约定并存：FastAPI 标准的 `detail`（字符串或校验错误数组）与
 * 部分端点自定义的 `message`。此前这段解析在 apiPost / apiPatch / apiDelete /
 * apiPostStream / performGet 里各抄了一份，改错误格式要动五处 —— 收敛到这里。
 * 解析失败（响应不是 JSON 等）时退回调用方给的兜底文案。
 */
async function parseErrorResponse(response: Response, fallback: string): Promise<string> {
  try {
    const data = await response.json();
    if (data?.message) return data.message;
    if (data?.detail !== undefined) {
      return typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    }
  } catch {
    /* keep fallback */
  }
  return fallback;
}

/** 写请求选项。timeoutMs：入队/重查询类接口（全量同步、AI 批量扫描）可放宽，
 *  避免网络慢时前端误报「请求超时」而任务实际已提交，导致用户重复点击。 */
export interface WriteOptions {
  timeoutMs?: number;
}

const DEFAULT_WRITE_TIMEOUT_MS = 15000;

export async function apiPost<T>(
  path: string,
  body?: unknown,
  options: WriteOptions = {}
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? DEFAULT_WRITE_TIMEOUT_MS
  );
  try {
    const response = await fetch(path, {
      method: "POST",
      signal: controller.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      const message = await parseErrorResponse(response, `请求失败（${response.status}）`);
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    // 写操作让整个 GET 缓存作废：保存设置/触发同步后再切页面必须看到新结论。
    invalidateApiCache();
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "请求超时，请确认后端服务已启动");
    }
    throw new ApiError(0, "网络异常，无法连接后端服务");
  } finally {
    clearTimeout(timer);
  }
}

/**
 * 带查询参数的 POST。
 *
 * 少数写接口（如触发经营看板总结）把范围放在 query 里而不是 body：范围是
 * **资源定位**而不是待写入的内容，放进 URL 才能被缓存键与日志一致地记录。
 * 与 `apiGet` 共用同一套参数省略规则（null/undefined/空串不进 URL）。
 */
export async function apiPostQuery<T>(
  path: string,
  params?: ApiQueryParams,
  body?: unknown,
  options: WriteOptions = {}
): Promise<T> {
  return apiPost<T>(apiCacheKey(path, params), body, options);
}

/** PATCH 请求与 POST 保持同一套超时、鉴权跳转与错误解析约定。 */
export async function apiPatch<T>(
  path: string,
  body?: unknown,
  options: WriteOptions = {}
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? DEFAULT_WRITE_TIMEOUT_MS
  );
  try {
    const response = await fetch(path, {
      method: "PATCH",
      signal: controller.signal,
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
    if (!response.ok) {
      const message = await parseErrorResponse(response, `请求失败（${response.status}）`);
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    invalidateApiCache();
    return (await response.json()) as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "请求超时，请确认后端服务已启动");
    }
    throw new ApiError(0, "网络异常，无法连接后端服务");
  } finally {
    clearTimeout(timer);
  }
}

/** DELETE 成功通常返回 204，不强制解析 JSON。 */
export async function apiDelete(path: string, options: WriteOptions = {}): Promise<void> {
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? DEFAULT_WRITE_TIMEOUT_MS
  );
  try {
    const response = await fetch(path, {
      method: "DELETE",
      signal: controller.signal,
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) {
      const message = await parseErrorResponse(response, `请求失败（${response.status}）`);
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    invalidateApiCache();
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "请求超时，请确认后端服务已启动");
    }
    throw new ApiError(0, "网络异常，无法连接后端服务");
  } finally {
    clearTimeout(timer);
  }
}

/**
 * GET 下载文件（CSV 导出等），取回内容后触发浏览器下载。
 *
 * 不能用 `window.location.href` 直接导航到下载地址：请求失败（401/403/500）时
 * 浏览器会**跳到裸 JSON 错误页**，用户丢失整个页面上下文 —— fetch 层的登录
 * 跳转救不了 location 导航。这里改用 fetch 取回 blob 再触发下载，失败走与
 * apiGet 同一套错误解析与登录跳转，调用方拿到 ApiError 自行提示。
 */
export async function apiDownload(path: string, params?: ApiQueryParams): Promise<void> {
  const url = new URL(apiCacheKey(path, params), window.location.origin);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30000);
  try {
    const response = await fetch(url.toString(), {
      signal: controller.signal,
      headers: { Accept: "text/csv, application/json" },
      cache: "no-store",
    });
    if (!response.ok) {
      const message = await parseErrorResponse(response, `导出失败（${response.status}）`);
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    const blob = await response.blob();
    // 文件名优先取 Content-Disposition；取不到时从路径推导一个安全的名字。
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = disposition.match(/filename="([^"]+)"/);
    const filename =
      match?.[1] ??
      (url.pathname.split("/").pop()?.replace(/[^\w.\-]/g, "_") || "download");
    const objectUrl = URL.createObjectURL(blob);
    const link = document.createElement("a");
    try {
      link.href = objectUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      URL.revokeObjectURL(objectUrl);
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "导出超时，请稍后重试");
    }
    throw new ApiError(0, "网络异常，无法连接后端服务");
  } finally {
    clearTimeout(timer);
  }
}

/**
 * POST + SSE 流式读取。
 *
 * 不能复用 apiPost：它固定 15 秒超时且要求 JSON 响应，而 Agent 一轮问答可能要
 * 跑十几秒、返回的是 text/event-stream。这里把每一帧交给调用方处理，
 * 由调用方决定怎么渲染「检索步骤」与「答案增量」。
 *
 * 注意「整轮超时」和「空闲超时」是两件事：一轮问答正常也可能跑几十秒，所以不能
 * 设总时长上限；但**上游卡住时一条帧都不会来**，没有空闲超时的话
 * `reader.read()` 永不 resolve，界面会永远停在加载态且无法重试。
 * 因此这里用「N 秒没有收到任何数据就中断」，而不是限制总时长。
 */
export async function apiPostStream<TEvent>(
  path: string,
  body: unknown,
  onEvent: (event: TEvent) => void,
  options: { signal?: AbortSignal; idleTimeoutMs?: number } = {}
): Promise<void> {
  const idleMs = options.idleTimeoutMs ?? 45000;
  const controller = new AbortController();
  const external = options.signal;
  let idleTimer: ReturnType<typeof setTimeout> | null = null;
  let idleExpired = false;

  const forwardAbort = () => controller.abort();
  if (external) {
    if (external.aborted) controller.abort();
    else external.addEventListener("abort", forwardAbort, { once: true });
  }
  const resetIdle = () => {
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      idleExpired = true;
      controller.abort();
    }, idleMs);
  };

  try {
    resetIdle(); // 连接阶段本身也受空闲超时保护（上游不响应时不会一直挂着）
    let response: Response;
    try {
      response = await fetch(path, {
        method: "POST",
        signal: controller.signal,
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        cache: "no-store",
      });
    } catch (error) {
      throw toStreamError(error, idleExpired, idleMs, external);
    }

    if (!response.ok) {
      const message = await parseErrorResponse(response, `请求失败（${response.status}）`);
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    if (!response.body) {
      throw new ApiError(0, "服务端没有返回流式响应");
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        resetIdle(); // 每收到一段数据就续期：慢但持续有输出的回答不会被误杀
        buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const line = frame.split("\n").find((item) => item.startsWith("data:"));
          if (line) {
            const payload = line.slice(5).trim();
            if (payload) {
              try {
                onEvent(JSON.parse(payload) as TEvent);
              } catch {
                /* 坏帧跳过：流里出现半截 JSON 不该让整轮问答失败 */
              }
            }
          }
          boundary = buffer.indexOf("\n\n");
        }
      }
    } catch (error) {
      throw toStreamError(error, idleExpired, idleMs, external);
    }
  } finally {
    if (idleTimer) clearTimeout(idleTimer);
    external?.removeEventListener("abort", forwardAbort);
  }
}

/** 把流式读取期间的异常归一成 ApiError，并区分「用户取消」与「空闲超时」。 */
function toStreamError(
  error: unknown,
  idleExpired: boolean,
  idleMs: number,
  external?: AbortSignal
): ApiError {
  if (error instanceof ApiError) return error;
  if (idleExpired) {
    return new ApiError(
      408,
      `模型在 ${Math.round(idleMs / 1000)} 秒内没有返回数据，已中断。请重试或检查 AI 配置。`
    );
  }
  if (external?.aborted || (error instanceof DOMException && error.name === "AbortError")) {
    // 499：调用方主动取消，界面应显示「已停止」而不是报错
    return new ApiError(499, "已停止");
  }
  return new ApiError(0, "网络异常，无法连接后端服务");
}

export async function apiGet<T>(
  path: string,
  params?: ApiQueryParams,
  options: { bypassCache?: boolean } = {}
): Promise<T> {
  const key = apiCacheKey(path, params);
  const cacheable = cacheableGet(path);
  if (cacheable && !options.bypassCache) {
    const cached = readApiCache<T>(key);
    if (cached !== undefined) return cached;
    // 同一 URL 已在飞行中：复用它，不再打一次后端。
    // 放在缓存判断之后、发请求之前，所以「先到的请求」仍然只发一次。
    const pending = inFlightGets.get(key);
    if (pending?.promise) return pending.promise as Promise<T>;
  }

  const flight: InFlightGet = {
    invalidated: false,
  };
  const request = performGet<T>(
    path,
    params,
    key,
    cacheable,
    () => !flight.invalidated
  );
  if (cacheable) {
    flight.promise = request;
    inFlightGets.set(key, flight);
    // 无论成败都要摘掉，否则一次失败会让后续请求永远复用一个 rejected Promise。
    void request
      .finally(() => {
        if (inFlightGets.get(key) === flight) inFlightGets.delete(key);
      })
      .catch(() => {});
  }
  return request;
}

async function performGet<T>(
  path: string,
  params: ApiQueryParams | undefined,
  key: string,
  cacheable: boolean,
  isCacheValid: () => boolean
): Promise<T> {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [paramKey, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") {
        url.searchParams.set(paramKey, String(value));
      }
    }
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  updateApiLoading(1);
  try {
    const response = await fetch(url.toString(), {
      signal: controller.signal,
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
    if (!response.ok) {
      const message = await parseErrorResponse(response, `请求失败（${response.status}）`);
      // 401 说明会话已经不可用，缓存里的旧数据必须立刻作废：
      // 否则重新登录后仍会读到上一个会话的内容。
      if (response.status === 401) invalidateApiCache();
      redirectToLoginIfUnauthorized(response.status);
      throw new ApiError(response.status, message);
    }
    const data = (await response.json()) as T;
    if (cacheable && isCacheValid()) writeApiCache(key, data);
    return data;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === "AbortError") {
      throw new ApiError(408, "请求超时，请确认后端服务已启动");
    }
    throw new ApiError(0, "网络异常，无法连接后端服务");
  } finally {
    clearTimeout(timer);
    updateApiLoading(-1);
  }
}
