"use client";

import React from "react";
import { ClipboardCheck, Plus } from "lucide-react";
import { Button, Input, Modal, Select } from "@/components/ui";
import { apiGet, apiPatch, apiPost } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type {
  EmployeeItem,
  ManagementAction,
  ManagementActionCreate,
  ManagementActionPriority,
  ManagementActionSourceType,
  ManagementActionStatus,
  ManagementActionUpdate,
  Paged,
  Period,
} from "@/lib/types";

const STATUS_OPTIONS: Array<{ value: ManagementActionStatus; label: string }> = [
  { value: "OPEN", label: "待处理" },
  { value: "IN_PROGRESS", label: "处理中" },
  { value: "WAITING", label: "等待外部反馈" },
  { value: "RESOLVED", label: "已解决" },
  { value: "CANCELLED", label: "已取消" },
];

const PRIORITY_OPTIONS: Array<{ value: ManagementActionPriority; label: string }> = [
  { value: "high", label: "高" },
  { value: "medium", label: "中" },
  { value: "low", label: "低" },
];

export interface ManagementActionSource {
  sourceType: ManagementActionSourceType;
  sourceId?: number;
  sourceKey?: string;
  title?: string;
}

interface FormValues {
  title: string;
  priority: ManagementActionPriority;
  status: ManagementActionStatus;
  ownerEmployeeId: string;
  dueDate: string;
  nextStep: string;
  note: string;
}

type ManagementActionCreateResult = ManagementAction & { created?: boolean };

function valuesFor(action: ManagementAction | null, source?: ManagementActionSource): FormValues {
  return {
    title: action?.title ?? source?.title ?? "",
    priority: action?.priority ?? "medium",
    status: action?.status ?? "OPEN",
    ownerEmployeeId: action?.owner_employee_id ? String(action.owner_employee_id) : "",
    dueDate: action?.due_date ?? "",
    nextStep: action?.next_step ?? "",
    note: action?.note ?? "",
  };
}

export function managementActionStatusLabel(status: ManagementActionStatus): string {
  return STATUS_OPTIONS.find((option) => option.value === status)?.label ?? status;
}

/**
 * 管理动作的唯一编辑入口。创建时锁定来源，后续状态变化通过专用 status 接口写入事件轨迹。
 */
export function ManagementActionEditor({
  open,
  action,
  source,
  period,
  onClose,
  onSaved,
}: {
  open: boolean;
  action: ManagementAction | null;
  source?: ManagementActionSource;
  period: Period;
  onClose: () => void;
  onSaved: (action: ManagementAction) => void;
}) {
  const [activeAction, setActiveAction] = React.useState<ManagementAction | null>(action);
  const [values, setValues] = React.useState<FormValues>(() => valuesFor(action, source));
  const [saving, setSaving] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const employees = useAsyncData<Paged<EmployeeItem>>(
    () => apiGet("/api/employees", { page: 1, page_size: 100 }),
    []
  );

  // 重置时机必须由**稳定的原始值**决定，不能依赖 `source` 的对象引用：
  // 父组件传的是内联对象字面量（`source={{ sourceType, sourceId, ... }}`），
  // 引用每次父渲染都变，直接依赖它会让 effect 每次父渲染都重跑，
  // 把管理员正在输入的「下一步 / 备注」清回服务端值 —— 首页 4 秒轮询等于每 4 秒清一次。
  const resetKey = open
    ? [
        action?.id ?? "new",
        source?.sourceType ?? "",
        source?.sourceId ?? "",
        source?.sourceKey ?? "",
      ].join("|")
    : "";
  const lastResetKey = React.useRef<string | null>(null);

  React.useEffect(() => {
    if (!open) {
      // 关闭时清空记录，下次打开（即使是同一事项）重新初始化表单。
      lastResetKey.current = null;
      return;
    }
    if (lastResetKey.current === resetKey) return;
    lastResetKey.current = resetKey;
    setActiveAction(action);
    setValues(valuesFor(action, source));
    setError(null);
    // action / source 只在 resetKey 变化时读取一次，故无需进入依赖数组。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, resetKey]);

  const updateValue = <Key extends keyof FormValues>(key: Key, value: FormValues[Key]) => {
    setValues((current) => ({ ...current, [key]: value }));
  };

  async function save() {
    const title = values.title.trim();
    if (!title) {
      setError("请填写跟进事项标题");
      return;
    }
    if (!activeAction && (!source || (source.sourceId === undefined && !source.sourceKey))) {
      setError("缺少可追溯的数据来源，暂不能创建跟进事项");
      return;
    }

    setSaving(true);
    setError(null);
    try {
      let saved = activeAction;
      if (!saved && source) {
        const createPayload: ManagementActionCreate = {
          source_type: source.sourceType,
          ...(source.sourceId !== undefined ? { source_id: source.sourceId } : {}),
          ...(source.sourceKey ? { source_key: source.sourceKey } : {}),
          start: period.start,
          end: period.end,
        };
        const created = await apiPost<ManagementActionCreateResult>("/api/management-actions", createPayload);
        // 后端会在并发或列表刚过期时返回已有事项。此时不能继续用
        // 当前表单的默认值 PATCH，否则会清空原负责人、下一步和备注。
        if (created.created === false) {
          setActiveAction(created);
          setValues(valuesFor(created, source));
          setError("该事项已有跟进记录，已载入现有内容；请确认修改后再次保存。");
          return;
        }
        saved = created;
        setActiveAction(saved);
      }
      if (!saved) return;

      const updatePayload: ManagementActionUpdate = {
        title,
        priority: values.priority,
        owner_employee_id: values.ownerEmployeeId ? Number(values.ownerEmployeeId) : null,
        due_date: values.dueDate || null,
        next_step: values.nextStep.trim() || null,
        note: values.note.trim() || null,
      };
      saved = await apiPatch<ManagementAction>(`/api/management-actions/${saved.id}`, updatePayload);
      if (saved.status !== values.status) {
        saved = await apiPost<ManagementAction>(`/api/management-actions/${saved.id}/status`, {
          status: values.status,
          note: values.note.trim() || undefined,
        });
      }
      onSaved(saved);
      onClose();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败，请稍后重试");
    } finally {
      setSaving(false);
    }
  }

  const title = activeAction ? "编辑跟进事项" : "纳入管理跟进";
  return (
    <Modal
      open={open}
      title={title}
      onClose={onClose}
      footer={
        <>
          <Button variant="outline" onClick={onClose} disabled={saving}>取消</Button>
          <Button onClick={save} loading={saving}>
            {activeAction ? <ClipboardCheck className="h-3.5 w-3.5" /> : <Plus className="h-3.5 w-3.5" />}
            保存跟进
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        {error && <p className="rounded-control bg-destructive-soft px-3 py-2 text-sm text-destructive">{error}</p>}
        <label className="block text-sm font-medium">
          跟进事项
          <Input className="mt-1.5" value={values.title} maxLength={200} onChange={(event) => updateValue("title", event.target.value)} />
        </label>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <label className="block text-sm font-medium">
            优先级
            <Select className="mt-1.5 w-full" value={values.priority} onChange={(event) => updateValue("priority", event.target.value as ManagementActionPriority)}>
              {PRIORITY_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </Select>
          </label>
          <label className="block text-sm font-medium">
            当前状态
            <Select className="mt-1.5 w-full" value={values.status} onChange={(event) => updateValue("status", event.target.value as ManagementActionStatus)}>
              {STATUS_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </Select>
          </label>
          <label className="block text-sm font-medium">
            负责人
            <Select className="mt-1.5 w-full" value={values.ownerEmployeeId} onChange={(event) => updateValue("ownerEmployeeId", event.target.value)}>
              <option value="">暂未指定</option>
              {activeAction?.owner_employee_id && !employees.data?.items.some((employee) => employee.id === activeAction.owner_employee_id) && (
                <option value={String(activeAction.owner_employee_id)}>{activeAction.owner_name ?? `员工 #${activeAction.owner_employee_id}`}</option>
              )}
              {employees.data?.items.map((employee) => <option key={employee.id} value={String(employee.id)}>{employee.name}{employee.department_name ? ` · ${employee.department_name}` : ""}</option>)}
            </Select>
            {employees.error && <span className="mt-1 block text-xs text-warning">负责人列表加载失败，暂不能变更负责人。</span>}
          </label>
          <label className="block text-sm font-medium">
            计划完成日
            <Input className="mt-1.5" type="date" value={values.dueDate} onChange={(event) => updateValue("dueDate", event.target.value)} />
          </label>
        </div>
        <label className="block text-sm font-medium">
          下一步
          <textarea className="mt-1.5 min-h-20 w-full rounded-control border border-input bg-surface px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" value={values.nextStep} maxLength={1000} onChange={(event) => updateValue("nextStep", event.target.value)} placeholder="明确下一次推进动作或待确认事项" />
        </label>
        <label className="block text-sm font-medium">
          备注
          <textarea className="mt-1.5 min-h-20 w-full rounded-control border border-input bg-surface px-3 py-2 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" value={values.note} maxLength={2000} onChange={(event) => updateValue("note", event.target.value)} placeholder="补充背景、决策或状态变更说明" />
        </label>
        {activeAction?.events?.length ? (
          <div className="border-t border-border pt-3">
            <p className="text-xs font-medium text-muted-foreground">跟进记录</p>
            <ol className="mt-2 space-y-2">
              {(activeAction.events ?? []).map((event) => <li key={event.id} className="rounded bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{event.to_status ? managementActionStatusLabel(event.to_status) : event.event_type}</span>
                {event.note ? ` · ${event.note}` : ""}
                <span className="ml-2">{formatDateTime(event.created_at)}</span>
              </li>)}
            </ol>
          </div>
        ) : null}
      </div>
    </Modal>
  );
}
