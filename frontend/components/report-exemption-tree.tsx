"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Search } from "lucide-react";
import { Button, Input } from "@/components/ui";
import { cn } from "@/lib/cn";
import type { DepartmentItem, EmployeeItem } from "@/lib/types";

const ROOT_ID = -1;
const UNASSIGNED_ID = -2;

interface DepartmentNode extends DepartmentItem {
  children: DepartmentNode[];
  directEmployees: EmployeeItem[];
  employeeIds: number[];
}

function hasExpandableContent(node: Pick<DepartmentNode, "children" | "directEmployees">) {
  return node.children.length > 0 || node.directEmployees.length > 0;
}

interface ReportExemptionTreeProps {
  departments: DepartmentItem[];
  employees: EmployeeItem[];
  selectedIds: Set<number>;
  onSelectionChange: (employeeIds: number[], checked: boolean) => void;
}

function employeeMatches(employee: EmployeeItem, keyword: string) {
  if (!keyword) return true;
  return [employee.name, employee.position, employee.external_id, employee.department_name]
    .filter(Boolean)
    .some((value) => String(value).toLowerCase().includes(keyword));
}

function nodeMatches(node: DepartmentNode, keyword: string): boolean {
  if (!keyword) return true;
  if (node.name.toLowerCase().includes(keyword)) return true;
  if (node.directEmployees.some((employee) => employeeMatches(employee, keyword))) return true;
  return node.children.some((child) => nodeMatches(child, keyword));
}

function TreeCheckbox({
  checked,
  indeterminate = false,
  disabled = false,
  label,
  onChange,
}: {
  checked: boolean;
  indeterminate?: boolean;
  disabled?: boolean;
  label: string;
  onChange: (checked: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);

  return (
    <input
      ref={ref}
      type="checkbox"
      aria-label={label}
      className="h-4 w-4 shrink-0 cursor-pointer rounded border-border text-primary accent-primary focus:ring-2 focus:ring-primary/30 disabled:cursor-not-allowed disabled:opacity-40"
      checked={checked}
      disabled={disabled}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

function DepartmentBranch({
  node,
  depth,
  keyword,
  selectedIds,
  expandedIds,
  onToggleExpanded,
  onSelectionChange,
}: {
  node: DepartmentNode;
  depth: number;
  keyword: string;
  selectedIds: Set<number>;
  expandedIds: Set<number>;
  onToggleExpanded: (departmentId: number) => void;
  onSelectionChange: (employeeIds: number[], checked: boolean) => void;
}) {
  if (!nodeMatches(node, keyword)) return null;

  const nodeNameMatches = Boolean(keyword && node.name.toLowerCase().includes(keyword));
  const visibleEmployees = nodeNameMatches
    ? node.directEmployees
    : node.directEmployees.filter((employee) => employeeMatches(employee, keyword));
  const visibleChildren = node.children.filter((child) => nodeMatches(child, keyword));
  // 直属员工也属于节点的可展开内容；否则只有员工、没有下级部门的部门
  // 会把员工行一直隐藏，同时把展开按钮禁用。
  const expandable = hasExpandableContent(node);
  const expanded = Boolean(keyword) || expandedIds.has(node.id);
  const selectedCount = node.employeeIds.filter((id) => selectedIds.has(id)).length;
  const totalCount = node.employeeIds.length;
  const allSelected = totalCount > 0 && selectedCount === totalCount;
  const partiallySelected = selectedCount > 0 && !allSelected;
  const rowPadding = 8 + depth * 18;

  return (
    <div>
      <div
        className={cn(
          "group flex min-h-10 items-center gap-2 border-b border-border/70 pr-3 transition-colors",
          depth === 0 ? "bg-muted/60" : "hover:bg-primary-soft/30"
        )}
        style={{ paddingLeft: rowPadding }}
      >
        <button
          type="button"
          aria-label={expanded ? `收起${node.name}` : `展开${node.name}`}
          className="-ml-0.5 inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-muted-foreground hover:bg-muted hover:text-foreground disabled:invisible"
          disabled={!expandable}
          onClick={() => onToggleExpanded(node.id)}
        >
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </button>
        <TreeCheckbox
          checked={allSelected}
          indeterminate={partiallySelected}
          disabled={totalCount === 0}
          label={`${allSelected ? "取消免提交" : "设为免提交"}：${node.name}`}
          onChange={(checked) => onSelectionChange(node.employeeIds, checked)}
        />
        <button
          type="button"
          className="flex min-w-0 flex-1 items-center gap-2 py-2 text-left"
          onClick={() => expandable && onToggleExpanded(node.id)}
        >
          <span className={cn("truncate text-sm", depth === 0 ? "font-semibold" : "font-medium")}>
            {node.name}
          </span>
          {node.exempt && <span className="shrink-0 text-xs text-muted-foreground">部门规则</span>}
        </button>
        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
          {selectedCount > 0 ? `${selectedCount}/${totalCount}` : totalCount}
        </span>
      </div>

      {expanded && (
        <div>
          {visibleEmployees.map((employee) => {
            const checked = selectedIds.has(employee.id);
            return (
              <label
                key={employee.id}
                className={cn(
                  "flex min-h-10 cursor-pointer items-center gap-2 border-b border-border/60 pr-3 transition-colors hover:bg-primary-soft/35",
                  checked && "bg-primary-soft/45"
                )}
                style={{ paddingLeft: rowPadding + 36 }}
              >
                <TreeCheckbox
                  checked={checked}
                  label={`${checked ? "取消免提交" : "设为免提交"}：${employee.name}`}
                  onChange={(nextChecked) => onSelectionChange([employee.id], nextChecked)}
                />
                <span className="min-w-0 flex-1 truncate text-sm">{employee.name}</span>
                {employee.position && (
                  <span className="hidden min-w-0 max-w-32 truncate text-xs text-muted-foreground sm:block">
                    {employee.position}
                  </span>
                )}
                {checked && <span className="shrink-0 text-xs text-primary">免提交</span>}
              </label>
            );
          })}
          {visibleChildren.map((child) => (
            <DepartmentBranch
              key={child.id}
              node={child}
              depth={depth + 1}
              keyword={keyword}
              selectedIds={selectedIds}
              expandedIds={expandedIds}
              onToggleExpanded={onToggleExpanded}
              onSelectionChange={onSelectionChange}
            />
          ))}
        </div>
      )}
    </div>
  );
}

export function ReportExemptionTree({
  departments,
  employees,
  selectedIds,
  onSelectionChange,
}: ReportExemptionTreeProps) {
  const [keyword, setKeyword] = useState("");
  const [expandedIds, setExpandedIds] = useState<Set<number>>(() => new Set([ROOT_ID]));

  const { roots, unassigned } = useMemo(() => {
    const nodes = new Map<number, DepartmentNode>();
    for (const department of departments) {
      nodes.set(department.id, {
        ...department,
        children: [],
        directEmployees: [],
        employeeIds: [],
      });
    }

    const unassignedEmployees: EmployeeItem[] = [];
    for (const employee of employees) {
      const node = employee.department_id === null ? null : nodes.get(employee.department_id);
      if (node) node.directEmployees.push(employee);
      else unassignedEmployees.push(employee);
    }

    const rootNodes: DepartmentNode[] = [];
    for (const node of nodes.values()) {
      const parent = node.parent_id === null ? null : nodes.get(node.parent_id);
      if (parent && parent.id !== node.id) parent.children.push(node);
      else rootNodes.push(node);
    }

    const collectEmployeeIds = (node: DepartmentNode): number[] => {
      node.employeeIds = [
        ...node.directEmployees.map((employee) => employee.id),
        ...node.children.flatMap(collectEmployeeIds),
      ];
      return node.employeeIds;
    };
    rootNodes.forEach(collectEmployeeIds);

    let unassignedNode: DepartmentNode | null = null;
    if (unassignedEmployees.length > 0) {
      unassignedNode = {
        id: UNASSIGNED_ID,
        external_id: null,
        name: "未分配部门",
        parent_id: null,
        status: "active",
        source: "system",
        employee_count: unassignedEmployees.length,
        submitted: 0,
        unsubmitted: 0,
        work_item_total: 0,
        work_item_done: 0,
        work_item_in_progress: 0,
        children: [],
        directEmployees: unassignedEmployees,
        employeeIds: unassignedEmployees.map((employee) => employee.id),
      };
    }

    return { roots: rootNodes, unassigned: unassignedNode };
  }, [departments, employees]);

  const normalizedKeyword = keyword.trim().toLowerCase();
  const expandableDepartmentIds = useMemo(() => {
    const ids = [ROOT_ID];
    const collect = (node: DepartmentNode) => {
      if (hasExpandableContent(node)) ids.push(node.id);
      node.children.forEach(collect);
    };
    roots.forEach(collect);
    if (unassigned) ids.push(UNASSIGNED_ID);
    return ids;
  }, [roots, unassigned]);
  const initializedExpansion = useRef(false);

  useEffect(() => {
    if (initializedExpansion.current || roots.length === 0) return;
    initializedExpansion.current = true;
    const next = new Set<number>([ROOT_ID]);

    const addSelectedPaths = (node: DepartmentNode) => {
      const hasSelected = node.employeeIds.some((id) => selectedIds.has(id));
      if (!hasSelected) return false;
      next.add(node.id);
      node.children.forEach(addSelectedPaths);
      return true;
    };

    let hasSelectionPath = false;
    for (const root of roots) {
      if (addSelectedPaths(root)) hasSelectionPath = true;
    }
    if (unassigned?.employeeIds.some((id) => selectedIds.has(id))) {
      next.add(UNASSIGNED_ID);
      hasSelectionPath = true;
    }
    if (!hasSelectionPath && roots[0]) next.add(roots[0].id);
    setExpandedIds(next);
  }, [roots, selectedIds, unassigned]);

  const allExpanded = expandableDepartmentIds.every((id) => expandedIds.has(id));
  const totalSelected = employees.filter((employee) => selectedIds.has(employee.id)).length;
  const allSelected = employees.length > 0 && totalSelected === employees.length;
  const partiallySelected = totalSelected > 0 && !allSelected;
  const hasVisibleNode =
    roots.some((root) => nodeMatches(root, normalizedKeyword)) ||
    Boolean(unassigned && nodeMatches(unassigned, normalizedKeyword));

  const toggleExpanded = (departmentId: number) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(departmentId)) next.delete(departmentId);
      else next.add(departmentId);
      return next;
    });
  };

  return (
    <div className="space-y-3">
      <p className="text-xs leading-relaxed text-muted-foreground">
        勾选部门会选中该部门及下级人员；个人规则优先用于跨部门领导。免提交人员不进入统计、AI 计算和催交名单。
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-56 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
            placeholder="搜索姓名、部门、岗位或企微账号"
            className="h-9 pl-9"
          />
        </div>
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            setExpandedIds(
              allExpanded ? new Set([ROOT_ID]) : new Set(expandableDepartmentIds)
            )
          }
        >
          {allExpanded ? "收起全部" : "展开全部"}
        </Button>
      </div>

      <div className="max-h-[440px] overflow-y-auto rounded-card border border-border bg-surface">
        <div className="flex min-h-10 items-center gap-2 border-b border-border bg-muted/60 px-2">
          <button
            type="button"
            aria-label={expandedIds.has(ROOT_ID) ? "收起全公司" : "展开全公司"}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-control text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={() => toggleExpanded(ROOT_ID)}
          >
            {expandedIds.has(ROOT_ID) ? (
              <ChevronDown className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
          <TreeCheckbox
            checked={allSelected}
            indeterminate={partiallySelected}
            disabled={employees.length === 0}
            label={`${allSelected ? "取消全公司免提交" : "将全公司设为免提交"}`}
            onChange={(checked) => onSelectionChange(employees.map((employee) => employee.id), checked)}
          />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold">全公司</span>
          <span className="pr-1 text-xs tabular-nums text-muted-foreground">
            {totalSelected > 0 ? `${totalSelected}/${employees.length}` : employees.length}
          </span>
        </div>

        {expandedIds.has(ROOT_ID) && (
          <div>
            {roots.map((root) => (
              <DepartmentBranch
                key={root.id}
                node={root}
                depth={0}
                keyword={normalizedKeyword}
                selectedIds={selectedIds}
                expandedIds={expandedIds}
                onToggleExpanded={toggleExpanded}
                onSelectionChange={onSelectionChange}
              />
            ))}
            {unassigned && (
              <DepartmentBranch
                node={unassigned}
                depth={0}
                keyword={normalizedKeyword}
                selectedIds={selectedIds}
                expandedIds={expandedIds}
                onToggleExpanded={toggleExpanded}
                onSelectionChange={onSelectionChange}
              />
            )}
            {normalizedKeyword && !hasVisibleNode && (
              <p className="px-4 py-8 text-center text-sm text-muted-foreground">没有匹配的在职人员</p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
