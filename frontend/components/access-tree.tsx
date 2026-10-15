"use client";

import React, { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { Button, Input, Select } from "@/components/ui";
import { cn } from "@/lib/cn";
import type {
  AccessDepartmentNode,
  AccessEmployeeNode,
  LoginRole,
  LoginSwitch,
} from "@/lib/types";

/**
 * 登录授权树。
 *
 * 交互与数据的关系：**只渲染后端算好的「继承后有效结论」，不在前端重算规则**。
 * 逐级判定（就近继承 / 显式禁止 / 个人追加）只有一处实现
 * （backend/services/access_service.py）。前端若自己再算一套，迟早出现
 * 「界面显示能登录、实际登不进来」这种最难排查的分歧。
 *
 * 授权粒度只有两种，都是 O(部门数) 而不是 O(人数)：
 * - 部门：整棵子树生效（未设置 / 允许 / 禁止 三态）
 * - 个人：“允许对话”控制企微事项会话，“管理员”控制周报与管理页面
 */

type SwitchLabel = { text: string; tone: "allow" | "deny" | "inherit" | "blocked" };

function switchLabel(
  node: AccessDepartmentNode,
  view?: { setting: LoginSwitch; role: LoginRole }
): SwitchLabel {
  const setting = view ? view.setting : node.setting;
  const role = view ? view.role : node.role;
  if (setting === true) {
    if (role === "admin") return { text: "本部门及下级可写（全公司）", tone: "allow" };
    if (role === "dept_admin") return { text: "本部门及下级可管理", tone: "allow" };
    return { text: "本部门及下级可看", tone: "allow" };
  }
  if (setting === false) return { text: "本级禁止", tone: "deny" };
  if (node.effective?.allowed) {
    return { text: `继承自「${node.effective.source_name}」`, tone: "inherit" };
  }
  if (node.effective && !node.effective.allowed) {
    return { text: `被「${node.effective.source_name}」禁止`, tone: "blocked" };
  }
  return { text: "未授权", tone: "inherit" };
}

function ToneChip({ label }: { label: SwitchLabel }) {
  const tones: Record<SwitchLabel["tone"], string> = {
    allow: "bg-success-soft text-success",
    deny: "bg-destructive-soft text-destructive",
    inherit: "bg-muted/60 text-muted-foreground",
    blocked: "bg-warning-soft text-warning",
  };
  return (
    <span className={cn("shrink-0 rounded-pill px-2 py-0.5 text-xs", tones[label.tone])}>
      {label.text}
    </span>
  );
}

/** 三态选择器：未设置 / 允许 / 禁止。用 select 而非开关，因为「继承」是独立语义。 */
function SwitchSelect({
  value,
  onChange,
  ariaLabel,
  disabled,
}: {
  value: LoginSwitch;
  onChange: (next: LoginSwitch) => void;
  ariaLabel: string;
  /** 本行保存中：禁用，保证同一行同时只有一个写入在飞（否则两次改动的到达顺序不定） */
  disabled?: boolean;
}) {
  return (
    <Select
      aria-label={ariaLabel}
      className="h-8 w-28 text-xs"
      disabled={disabled}
      value={value === null ? "inherit" : value ? "allow" : "deny"}
      onChange={(event) => {
        const raw = event.target.value;
        onChange(raw === "inherit" ? null : raw === "allow");
      }}
    >
      <option value="inherit">继承上级</option>
      <option value="allow">允许登录</option>
      <option value="deny">禁止登录</option>
    </Select>
  );
}

function RoleSelect({
  value,
  onChange,
  disabled,
}: {
  value: LoginRole;
  onChange: (next: LoginRole) => void;
  disabled?: boolean;
}) {
  return (
    <Select
      aria-label="登录后的角色"
      className="h-8 w-24 text-xs"
      value={value}
      disabled={disabled}
      onChange={(event) => onChange(event.target.value as LoginRole)}
    >
      <option value="member">只读</option>
      <option value="dept_admin">部门管理员</option>
      <option value="admin">全公司管理员</option>
    </Select>
  );
}

/** 一次保存的进行状态：按对象 id 记录，**不是全页共用一个**。 */
type PendingMap = Map<string, boolean>;

const deptKey = (id: number) => `d${id}`;
const empKey = (id: number) => `e${id}`;

interface AccessTreeProps {
  departments: AccessDepartmentNode[];
  employees: AccessEmployeeNode[];
  authorizedIds: Set<number>;
  adminIds: Set<number>;
  /**
   * 正在保存的对象（键见 deptKey / empKey），**只禁用那一行**。
   *
   * 这里刻意不用一个全局 `saving`：早先的实现是「有任何一次保存在飞就禁用整棵树」，
   * 而一次保存包含 PATCH + 两次整树重取。真实网络下这个窗口超过一秒，
   * 用户连续勾选时第二次点击正好落在被禁用的控件上，被浏览器静默忽略 ——
   * 看起来就是「勾了没反应」。按行加锁后，各行互不影响。
   */
  pending: PendingMap;
  onDepartmentChange: (id: number, patch: { enabled?: LoginSwitch; role?: LoginRole }) => void;
  onEmployeeChange: (id: number, patch: { enabled?: boolean; role?: LoginRole }) => void;
}

const ROOT_ID = -1;
const UNASSIGNED_ID = -2;

export function AccessTree({
  departments,
  employees,
  authorizedIds,
  adminIds,
  pending,
  onDepartmentChange,
  onEmployeeChange,
}: AccessTreeProps) {
  const [keyword, setKeyword] = useState("");
  const [expanded, setExpanded] = useState<Set<number>>(() => new Set([ROOT_ID]));

  // 本组件是**纯展示 + 派发**：值的真源来自父组件（它已经叠加了乐观草稿，
  // 并在保存成功/失败后确定性地提交或回滚）。这里不再自己维护一份草稿副本 ——
  // 两处各存一份状态，迟早出现「界面值与服务端值不一致」而查不出来。
  const changeDepartment = (
    dept: AccessDepartmentNode,
    patch: { enabled?: LoginSwitch; role?: LoginRole }
  ) => onDepartmentChange(dept.id, patch);

  const changeEmployee = (
    employee: AccessEmployeeNode,
    patch: { enabled?: boolean; role?: LoginRole }
  ) => onEmployeeChange(employee.id, patch);

  // 节点上的值就是当前应显示的值（父组件已把乐观草稿叠进 departments/employees）
  const deptView = (dept: AccessDepartmentNode) => ({
    setting: dept.setting,
    role: dept.role,
  });
  const empView = (employee: AccessEmployeeNode) => ({
    setting: employee.setting,
    role: employee.role,
  });

  const { childrenOf, roots, unassigned } = useMemo(() => {
    const ids = new Set(departments.map((dept) => dept.id));
    const map = new Map<number | null, AccessDepartmentNode[]>();
    const unassignedEmployees: AccessEmployeeNode[] = [];
    const byDept = new Map<number, AccessEmployeeNode[]>();
    for (const employee of employees) {
      if (employee.department_id !== null && ids.has(employee.department_id)) {
        const list = byDept.get(employee.department_id) ?? [];
        list.push(employee);
        byDept.set(employee.department_id, list);
      } else {
        unassignedEmployees.push(employee);
      }
    }
    for (const dept of departments) {
      const key = dept.parent_id !== null && ids.has(dept.parent_id) ? dept.parent_id : null;
      const list = map.get(key) ?? [];
      list.push(dept);
      map.set(key, list);
    }
    return {
      childrenOf: { map, byDept },
      roots: map.get(null) ?? [],
      unassigned: unassignedEmployees,
    };
  }, [departments, employees]);

  const normalized = keyword.trim().toLowerCase();

  const employeeMatches = (employee: AccessEmployeeNode) =>
    !normalized ||
    [employee.name, employee.position, employee.external_id]
      .filter(Boolean)
      .some((value) => String(value).toLowerCase().includes(normalized));

  const deptMatches = (dept: AccessDepartmentNode): boolean => {
    if (!normalized) return true;
    if (dept.name.toLowerCase().includes(normalized)) return true;
    const direct = childrenOf.byDept.get(dept.id) ?? [];
    if (direct.some(employeeMatches)) return true;
    return (childrenOf.map.get(dept.id) ?? []).some(deptMatches);
  };

  const allExpanded =
    departments.length > 0 && departments.every((dept) => expanded.has(dept.id));
  // 搜索时强制展开命中路径，否则「搜到了但看不见」是最常见的抱怨
  const isExpanded = (id: number) => Boolean(normalized) || expanded.has(id);

  const toggle = (id: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const renderEmployees = (deptId: number, depth: number, forceShow: boolean) => {
    const direct = childrenOf.byDept.get(deptId) ?? [];
    const visible = forceShow ? direct : direct.filter(employeeMatches);
    if (visible.length === 0) return null;
    return visible.map((employee) => {
      const view = empView(employee);
      return (
        <EmployeeRow
          key={employee.id}
          employee={employee}
          view={view}
          depth={depth}
          authorized={authorizedIds.has(employee.id)}
          isAdmin={adminIds.has(employee.id)}
          pending={pending.get(empKey(employee.id)) === true}
          onEmployeeChange={changeEmployee}
        />
      );
    });
  };

  const renderBranch = (dept: AccessDepartmentNode, depth: number) => {
    if (!deptMatches(dept)) return null;
    const children = (childrenOf.map.get(dept.id) ?? []).filter(deptMatches);
    const expandedNow = isExpanded(dept.id);
    const nameMatch = Boolean(normalized) && dept.name.toLowerCase().includes(normalized);
    const view = deptView(dept);
    const label = switchLabel(dept, view);
    const inherited = view.setting === null;

    return (
      <div key={dept.id}>
        <div
          className={cn(
            "flex min-h-12 flex-wrap items-center gap-2 border-b border-border/60 pr-3",
            depth === 0 ? "bg-muted/60" : "hover:bg-primary-soft/30"
          )}
          style={{ paddingLeft: 8 + depth * 18 }}
        >
          <button
            type="button"
            aria-label={expandedNow ? `收起${dept.name}` : `展开${dept.name}`}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() => toggle(dept.id)}
          >
            {expandedNow ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
          <span className={cn("min-w-0 flex-1 truncate text-sm", depth === 0 && "font-semibold")}>
            {dept.name}
          </span>
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
            {dept.subtree_employee_count} 人
          </span>
          <ToneChip label={label} />
          <SwitchSelect
            ariaLabel={`${dept.name} 的登录授权`}
            value={view.setting}
            disabled={pending.get(deptKey(dept.id)) === true}
            onChange={(enabled) => changeDepartment(dept, { enabled })}
          />
          <RoleSelect
            value={view.role}
            disabled={inherited || pending.get(deptKey(dept.id)) === true}
            onChange={(role) => changeDepartment(dept, { role })}
          />
        </div>
        {expandedNow && (
          <div>
            {renderEmployees(dept.id, depth + 1, nameMatch)}
            {children.map((child) => renderBranch(child, depth + 1))}
          </div>
        )}
      </div>
    );
  };

  const hasVisible =
    roots.some(deptMatches) || (normalized === "" && unassigned.length > 0);

  return (
    <div className="space-y-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        勾选一个部门 = 该部门及<strong className="font-medium">全部下级</strong>都能登录，
        授权只占一行，不随人数增加。个人授权用于跨部门领导，只会「追加」权限。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="搜索部门、姓名或企微账号"
            className="h-9 pl-9"
          />
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            setExpanded(allExpanded ? new Set() : new Set(departments.map((d) => d.id)))
          }
        >
          {allExpanded ? "收起全部" : "展开全部"}
        </Button>
      </div>

      <div className="max-h-[560px] overflow-y-auto rounded-card border border-border bg-surface">
        <div className="flex min-h-11 items-center gap-2 border-b border-border bg-muted/60 px-2">
          <button
            type="button"
            aria-label={isExpanded(ROOT_ID) ? "收起全公司" : "展开全公司"}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() => toggle(ROOT_ID)}
          >
            {isExpanded(ROOT_ID) ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">全公司</span>
          <span className="pr-1 text-xs tabular-nums text-muted-foreground">
            已授权 {authorizedIds.size} 人
          </span>
        </div>

        {isExpanded(ROOT_ID) && (
          <div>
            {roots.map((dept) => renderBranch(dept, 0))}
            {unassigned.length > 0 && (
              <div>
                <div className="flex min-h-12 items-center gap-2 border-b border-border/60 bg-muted/40 pr-3 pl-2">
                  <span className="min-w-0 flex-1 truncate text-sm font-semibold">
                    未分配部门
                  </span>
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                    {unassigned.length} 人
                  </span>
                </div>
                {unassigned
                  .filter(employeeMatches)
                  .map((employee) => (
                    <EmployeeRow
                      key={employee.id}
                      employee={employee}
                      view={empView(employee)}
                      depth={1}
                      authorized={authorizedIds.has(employee.id)}
                      isAdmin={adminIds.has(employee.id)}
                      pending={pending.get(empKey(employee.id)) === true}
                      onEmployeeChange={changeEmployee}
                    />
                  ))}
              </div>
            )}
            {normalized && !hasVisible && (
              <p className="px-4 py-8 text-center text-sm text-muted-foreground">
                没有匹配的在职人员
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function EmployeeRow({
  employee,
  view,
  depth,
  authorized,
  isAdmin,
  pending,
  onEmployeeChange,
}: {
  employee: AccessEmployeeNode;
  view: { setting: boolean; role: LoginRole };
  depth: number;
  authorized: boolean;
  isAdmin: boolean;
  /** 仅这一行正在保存：只禁用本行，不影响其他部门/员工继续操作 */
  pending: boolean;
  onEmployeeChange: (
    employee: AccessEmployeeNode,
    patch: { enabled?: boolean; role?: LoginRole }
  ) => void;
}) {
  const allowed = employee.denied ? false : view.setting || authorized;
  const effectiveRole: LoginRole = view.setting
    ? view.role
    : isAdmin
      ? "admin"
      : "member";
  return (
    <div
      className={cn(
        "flex min-h-10 flex-wrap items-center gap-2 border-b border-border/50 pr-3 transition-colors hover:bg-primary-soft/25",
        allowed && "bg-primary-soft/35"
      )}
      style={{ paddingLeft: 8 + depth * 18 + 24 }}
    >
      <span className="min-w-0 flex-1 truncate text-sm">{employee.name}</span>
      {employee.position && (
        <span className="hidden min-w-0 max-w-32 truncate text-xs text-muted-foreground sm:block">
          {employee.position}
        </span>
      )}
      {employee.denied ? (
        <span className="shrink-0 rounded-pill bg-destructive-soft px-2 py-0.5 text-xs text-destructive">
          已禁止
        </span>
      ) : allowed ? (
        <span className="shrink-0 rounded-pill bg-success-soft px-2 py-0.5 text-xs text-success">
          {effectiveRole === "admin"
            ? "全公司管理员"
            : effectiveRole === "dept_admin"
              ? "部门管理员"
              : "仅对话"}
        </span>
      ) : (
        <span className="shrink-0 rounded-pill bg-muted/60 px-2 py-0.5 text-xs text-muted-foreground">
          未授权
        </span>
      )}
      <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-muted-foreground">
        <input
          type="checkbox"
          className="h-4 w-4 cursor-pointer rounded border-border accent-primary"
          checked={allowed}
          disabled={pending}
          onChange={(event) => {
            const enabled = event.target.checked;
            onEmployeeChange(employee, {
              enabled,
              // 重新开放一个已禁止账号时按界面当前显示的角色保存；默认是只读，
              // 避免历史 admin 值在后台残留并意外恢复管理员权限。
              ...(enabled ? { role: effectiveRole } : {}),
            });
          }}
        />
        允许对话
      </label>
      {/* 个人级角色三档：只读 / 部门管理员 / 全公司管理员。未授权时禁用。 */}
      <Select
        aria-label="个人登录角色"
        className="h-8 w-28 shrink-0 text-xs"
        value={effectiveRole}
        disabled={!allowed || pending}
        onChange={(event) =>
          onEmployeeChange(employee, {
            enabled: true,
            role: event.target.value as LoginRole,
          })
        }
      >
        <option value="member">仅对话</option>
        <option value="dept_admin">部门管理员</option>
        <option value="admin">全公司管理员</option>
      </Select>
    </div>
  );
}

export { switchLabel };
export type { SwitchLabel };
