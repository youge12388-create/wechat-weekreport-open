"use client";

import { useEffect, useState } from "react";
import { Save, ShieldCheck, Users } from "lucide-react";
import { Button, Card, ErrorState, Input, LoadingBlock, StatTile, StatusDot } from "@/components/ui";
import { apiGet, apiPatch } from "@/lib/api";
import { useAsyncData } from "@/lib/use-data";
import type { AccessOverview, AccessTree, EmployeeItem, LoginRole, LoginSwitch, Paged } from "@/lib/types";
import { AccessTree as AccessTreeView } from "@/components/access-tree";
import { ReportExemptionTree } from "@/components/report-exemption-tree";
import type { Notice } from "./settings-types";

export async function fetchAllActiveEmployees() {
  const pageSize = 100;
  const employees: EmployeeItem[] = [];
  let page = 1;
  let total = 0;

  while (true) {
    const result = await apiGet<Paged<EmployeeItem>>("/api/employees", {
      status: "active",
      page,
      page_size: pageSize,
    });
    employees.push(...result.items);
    total = result.total;
    if (employees.length >= total || result.items.length === 0) break;
    page += 1;
  }

  return {
    items: employees,
    total,
    page: 1,
    page_size: employees.length || pageSize,
  } satisfies Paged<EmployeeItem>;
}

/**
 * 登录与授权。
 *
 * 授权只落在两个粒度上：部门（整棵子树生效）与个人（仅追加）。
 * 因此这一页的数据量只与「部门数」相关，一万人公司也不会比一百人公司更重 ——
 * 这正是「不占位置」的做法：不产生逐人授权行，组织调整也不用维护名单。
 */
export function AccessCard() {
  const overview = useAsyncData<AccessOverview>(() => apiGet("/api/access/overview"), []);
  const tree = useAsyncData<AccessTree>(() => apiGet("/api/access/tree"), []);
  /**
   * 正在保存的对象集合（键：`d<部门id>` / `e<员工id>`）。
   *
   * 刻意**不是**一个全局 `saving`：早先的实现是「有任何一次保存在飞就禁用整棵树」，
   * 而一次保存包含 PATCH + 两次整树重取，真实网络下窗口超过一秒。用户连续勾选时，
   * 第二次点击落在被禁用的控件上会被浏览器静默忽略 —— 表现为「勾选没生效」。
   * 按对象加锁后，各行的保存互不干扰。
   */
  const [pending, setPending] = useState<Map<string, boolean>>(() => new Map());
  /**
   * 乐观草稿：点击后**立刻**把界面切到新值，不等网络往返。
   *
   * 为什么必须有：真实网络下一次保存包含 PATCH + 一次整树重取，界面要等约一秒
   * 才变化。用户看不到即时反馈，会以为没点中而重复点击 —— 这本身就是
   * 「勾选不生效」的一部分体验来源。
   */
  const [drafts, setDrafts] = useState<
    Map<string, { enabled: LoginSwitch; role: LoginRole; denied?: boolean }>
  >(() => new Map());
  const [loginSaving, setLoginSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [redirectUri, setRedirectUri] = useState("");
  const [redirectSaving, setRedirectSaving] = useState(false);

  const settings = overview.data;

  useEffect(() => {
    if (settings?.redirect_uri) setRedirectUri(settings.redirect_uri);
  }, [settings?.redirect_uri]);

  const setBusy = (key: string, busy: boolean) =>
    setPending((current) => {
      const next = new Map(current);
      if (busy) next.set(key, true);
      else next.delete(key);
      return next;
    });

  const setDraft = (
    key: string,
    value: { enabled: LoginSwitch; role: LoginRole; denied?: boolean } | null
  ) =>
    setDrafts((current) => {
      const next = new Map(current);
      if (value) next.set(key, value);
      else next.delete(key);
      return next;
    });

  // 保存成功后保留乐观值，直到授权树确实返回同一份服务端真值。
  // reload() 只触发刷新、不返回网络请求 Promise；提前清草稿会让界面闪回旧权限。
  useEffect(() => {
    if (!tree.data) return;
    const completed: string[] = [];
    for (const [key, draft] of drafts) {
      const id = Number(key.slice(1));
      if (key.startsWith("d")) {
        const item = tree.data.departments.find((dept) => dept.id === id);
        if (item && item.setting === draft.enabled && item.role === draft.role) {
          completed.push(key);
        }
      } else if (key.startsWith("e")) {
        const item = tree.data.employees.find((employee) => employee.id === id);
        if (
          item &&
          item.setting === (draft.enabled === true) &&
          item.role === draft.role &&
          item.denied === Boolean(draft.denied)
        ) {
          completed.push(key);
        }
      }
    }
    if (completed.length === 0) return;
    setDrafts((current) => {
      const next = new Map(current);
      completed.forEach((key) => next.delete(key));
      return next;
    });
    setPending((current) => {
      const next = new Map(current);
      completed.forEach((key) => next.delete(key));
      return next;
    });
  }, [drafts, tree.data]);

  useEffect(() => {
    if (tree.error) setPending(new Map());
  }, [tree.error]);

  /**
   * 保存一个授权位。
   *
   * **不做全局串行**：每次写入都是「对某一行赋值」，不同行之间没有顺序依赖，
   * 并发提交不会互相覆盖；同一行在飞期间该行控件禁用（见 setBusy）。
   * 早先的实现是「有任何一次保存在飞就禁用整棵树」，连续操作时后续点击
   * 落在禁用控件上被静默忽略 —— 那正是「勾选不生效」的根因。
   */
  const apply = async (
    key: string,
    run: () => Promise<unknown>,
    successText: string
  ) => {
    setBusy(key, true);
    setNotice(null);
    let saved = false;
    try {
      await run();
      saved = true;
      overview.reload();
      tree.reload();
      setNotice({ text: successText, tone: "success" });
    } catch (error) {
      // 保存失败必须回滚草稿，否则界面会一直停在没生效的值上（假成功）
      setDraft(key, null);
      setNotice({
        text: error instanceof Error ? error.message : "保存失败",
        tone: "error",
      });
      overview.reload();
      tree.reload();
    } finally {
      // 保存成功时保持本行锁定，直到授权树回读到相同真值；失败则立即解锁。
      if (!saved) setBusy(key, false);
    }
  };

  const toggleEnabled = async () => {
    if (!settings) return;
    setLoginSaving(true);
    setNotice(null);
    try {
      await apiPatch("/api/access/settings", { enabled: !settings.enabled });
      await overview.reload();
      setNotice({
        text: settings.enabled ? "已关闭企业微信登录" : "已开启企业微信登录",
        tone: "success",
      });
    } catch (error) {
      setNotice({
        text: error instanceof Error ? error.message : "保存失败",
        tone: "error",
      });
    } finally {
      setLoginSaving(false);
    }
  };

  const saveRedirect = async () => {
    setRedirectSaving(true);
    setNotice(null);
    try {
      await apiPatch("/api/access/settings", { redirect_uri: redirectUri.trim() });
      await overview.reload();
      setNotice({ text: "回调地址已保存", tone: "success" });
    } catch (error) {
      setNotice({
        text: error instanceof Error ? error.message : "保存失败",
        tone: "error",
      });
    } finally {
      setRedirectSaving(false);
    }
  };

  if (overview.loading || tree.loading) {
    return (
      <Card>
        <div className="card-head">
          <ShieldCheck className="mt-0.5 h-5 w-5 text-primary" />
          <h2 className="text-base font-semibold">登录与授权</h2>
        </div>
        <LoadingBlock rows={4} />
      </Card>
    );
  }

  if (overview.error || !settings) {
    return (
      <Card>
        <div className="card-head">
          <ShieldCheck className="mt-0.5 h-5 w-5 text-primary" />
          <h2 className="text-base font-semibold">登录与授权</h2>
        </div>
        <div className="p-5">
          <ErrorState
            message={overview.error ?? "无法读取登录授权配置"}
            onRetry={overview.reload}
          />
        </div>
      </Card>
    );
  }

  const blockedPrereqs = settings.prerequisites.filter((item) => !item.ok);
  const authn = tree.data;
  /**
   * 生效地址的路径不是回调路径 —— 企微会把 code 回跳到那里，而那里没人接。
   * 这类错误不报错、不崩溃，只表现为「同事扫码后回到登录页」，所以必须在设置页
   * 就说清，而不是留给用户排查。
   */
  const callbackPathMismatch = (() => {
    try {
      const path = new URL(settings.effective_callback_url).pathname;
      return path !== settings.callback_path;
    } catch {
      return false;
    }
  })();

  return (
    <div className="space-y-4">
      <Card className="card-head-line">
        <div className="card-head">
          <ShieldCheck className="mt-0.5 h-5 w-5 text-primary" />
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <h2 className="text-base font-semibold">企业微信登录</h2>
              <StatusDot tone={settings.enabled ? "green" : "neutral"}>
                {settings.enabled ? "已开启" : "已关闭"}
              </StatusDot>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              开启后，同事在登录页可用企业微信扫码或在企微客户端内免扫码登录。
              访问密码始终可用，作为管理员兜底入口。
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={settings.enabled}
            aria-label="企业微信登录开关"
            disabled={loginSaving}
            onClick={() => void toggleEnabled()}
            className={
              settings.enabled
                ? "relative h-6 w-11 shrink-0 rounded-full bg-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
                : "relative h-6 w-11 shrink-0 rounded-full bg-muted-foreground/25 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            }
          >
            <span
              className={
                settings.enabled
                  ? "absolute left-5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                  : "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
              }
            />
          </button>
        </div>

        <div className="space-y-4 p-5">
          <div className="grid gap-2 sm:grid-cols-2">
            {settings.prerequisites.map((item) => (
              <div
                key={item.key}
                className="flex items-start gap-2.5 rounded-control bg-muted/40 px-3.5 py-2.5"
              >
                <StatusDot tone={item.ok ? "green" : "amber"}>
                  {item.label}
                </StatusDot>
                <span className="min-w-0 flex-1 text-xs leading-relaxed text-muted-foreground">
                  {item.hint}
                </span>
              </div>
            ))}
          </div>

          {blockedPrereqs.length > 0 && (
            <p className="rounded-control bg-warning-soft px-3.5 py-2.5 text-xs leading-relaxed text-warning">
              还有 {blockedPrereqs.length} 项前提未就绪，开启后成员登录会失败。
            </p>
          )}

          <div className="grid gap-4 sm:grid-cols-[1fr_auto] sm:items-end">
            <label className="text-sm font-medium">
              回调地址
              <Input
                className="mt-1.5"
                value={redirectUri}
                onChange={(event) => setRedirectUri(event.target.value)}
                placeholder={`https://你的域名${settings.callback_path}`}
              />
              <span className="mt-1.5 block text-xs leading-relaxed text-muted-foreground">
                填企微后台「Web 网页授权及 JS-SDK 可信域名」里的域名即可（含端口），
                只填域名会自动补上 {settings.callback_path}；留空则按访问域名推导。
              </span>
              {/* 显示**实际会下发**的地址：只填域名时它和输入框内容不同，
                  两者并列展示，管理员能当场看出企微会回跳到哪，
                  而不是等同事扫码登不进来才发现地址不对。 */}
              <span className="mt-1.5 block break-all text-xs leading-relaxed text-muted-foreground">
                实际使用：<code className="rounded bg-muted/60 px-1.5 py-0.5">
                  {settings.effective_callback_url}
                </code>
              </span>
              {callbackPathMismatch && (
                <span className="mt-1.5 block rounded-control bg-warning-soft px-3 py-2 text-xs leading-relaxed text-warning">
                  当前地址的路径不是 {settings.callback_path}。企微会把 code 回跳到这里，
                  而只有 {settings.callback_path} 能接收 —— 同事扫码会卡在登录页。
                  去掉路径只保留域名即可。
                </span>
              )}
            </label>
            <Button
              variant="outline"
              onClick={() => void saveRedirect()}
              loading={redirectSaving}
            >
              <Save className="h-3.5 w-3.5" />
              保存地址
            </Button>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <StatTile label="已授权" value={`${settings.authorized_count} 人`} />
            <StatTile label="其中可写" value={`${settings.admin_count} 人`} />
            <StatTile
              label="已配置部门"
              value={`${settings.department_configured_total} / ${settings.department_total}`}
            />
          </div>

          {notice && (
            <p
              className={
                notice.tone === "error"
                  ? "rounded-control bg-destructive-soft px-3.5 py-2.5 text-xs text-destructive"
                  : notice.tone === "success"
                    ? "rounded-control bg-success-soft px-3.5 py-2.5 text-xs text-success"
                    : "rounded-control bg-muted/40 px-3.5 py-2.5 text-xs text-muted-foreground"
              }
            >
              {notice.text}
            </p>
          )}
        </div>
      </Card>

      <Card className="card-head-line">
        <div className="card-head">
          <Users className="mt-0.5 h-5 w-5 text-primary" />
          <div>
            <h2 className="text-base font-semibold">授权范围</h2>
            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
              “允许对话”决定员工能否从企业微信进入自己的事项会话；“管理员”决定能否查看周报和管理页面。
              取消管理员后仍可正常收发事项消息，只有关闭允许对话才会禁止登录。
            </p>
          </div>
        </div>
        <div className="p-5 pt-0">
          {tree.error || !authn ? (
            <ErrorState message={tree.error ?? "无法读取授权树"} onRetry={tree.reload} />
          ) : (
            <AccessTreeView
              // 展示值 = 服务端真值 + 尚未落库的乐观草稿
              departments={authn.departments.map((dept) => {
                const draft = drafts.get(`d${dept.id}`);
                return draft
                  ? { ...dept, setting: draft.enabled, role: draft.role }
                  : dept;
              })}
              employees={authn.employees.map((employee) => {
                const draft = drafts.get(`e${employee.id}`);
                return draft
                  ? {
                      ...employee,
                      setting: draft.enabled === true,
                      denied: Boolean(draft.denied),
                      role: draft.role,
                    }
                  : employee;
              })}
              authorizedIds={new Set(authn.authorized_employee_ids)}
              adminIds={new Set(authn.admin_employee_ids)}
              pending={pending}
              onDepartmentChange={(id, patch) => {
                const dept = authn.departments.find((item) => item.id === id);
                if (!dept) return;
                const draft = drafts.get(`d${id}`);
                const base = draft ?? { enabled: dept.setting, role: dept.role };
                setDraft(`d${id}`, {
                  enabled:
                    patch.enabled !== undefined ? patch.enabled : base.enabled,
                  role: patch.role !== undefined ? patch.role : base.role,
                });
                void apply(
                  `d${id}`,
                  () => apiPatch(`/api/access/departments/${id}`, patch),
                  "部门授权已更新"
                );
              }}
              onEmployeeChange={(id, patch) => {
                const employee = authn.employees.find((item) => item.id === id);
                if (!employee) return;
                const draft = drafts.get(`e${id}`);
                const base = draft ?? {
                  enabled: employee.setting,
                  role: employee.role,
                  denied: employee.denied,
                };
                setDraft(`e${id}`, {
                  enabled:
                    patch.enabled !== undefined ? patch.enabled : base.enabled,
                  role: patch.role !== undefined ? patch.role : base.role,
                  denied:
                    patch.enabled !== undefined
                      ? patch.enabled === false
                      : base.denied,
                });
                void apply(
                  `e${id}`,
                  () => apiPatch(`/api/access/employees/${id}`, patch),
                  "员工登录权限已更新"
                );
              }}
            />
          )}
        </div>
      </Card>
    </div>
  );
}
