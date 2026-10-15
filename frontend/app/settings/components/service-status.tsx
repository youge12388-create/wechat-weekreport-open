"use client";

import { Pencil, Trash2 } from "lucide-react";
import { Button, Input, StatusDot } from "@/components/ui";
import type { CheckState } from "./settings-types";

export function ServiceStatus({
  state,
  configured,
}: {
  state: CheckState;
  configured: boolean;
}) {
  let tone: "green" | "amber" | "red" | "neutral" = "neutral";
  let label = "未配置";
  if (state === "checking") {
    tone = "amber";
    label = "检测中";
  } else if (state === "available") {
    tone = "green";
    label = "可以使用";
  } else if (state === "attention") {
    tone = "amber";
    label = "需选择可用模型";
  } else if (state === "model_list_unavailable") {
    tone = "amber";
    label = "模型列表不可用";
  } else if (state === "failed") {
    tone = "red";
    label = "检测失败";
  } else if (configured) {
    tone = "amber";
    label = "已配置，未检测";
  }
  return <StatusDot tone={tone}>{label}</StatusDot>;
}

export function SecretEditor({
  label,
  configured,
  editing,
  value,
  clear,
  placeholder,
  onEdit,
  onChange,
  onCancel,
  onDelete,
  onUndoDelete,
}: {
  label: string;
  configured: boolean;
  editing: boolean;
  value: string;
  clear: boolean;
  placeholder: string;
  onEdit: () => void;
  onChange: (value: string) => void;
  onCancel: () => void;
  onDelete: () => void;
  onUndoDelete: () => void;
}) {
  if (configured && !editing) {
    return (
      <div>
        <p className="text-sm font-medium">{label}</p>
        <div className="mt-1.5 flex flex-wrap items-center justify-between gap-2 rounded-control bg-muted/40 px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            {clear ? (
              <StatusDot tone="amber">保存后删除</StatusDot>
            ) : (
              <StatusDot tone="green">已保存</StatusDot>
            )}
            <span className="truncate text-xs text-muted-foreground">
              {clear ? "点击撤销可保留当前密钥" : "密钥已加密保存，不会在页面回显"}
            </span>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {clear ? (
              <Button variant="ghost" size="sm" onClick={onUndoDelete}>
                撤销删除
              </Button>
            ) : (
              <>
                <Button variant="outline" size="sm" onClick={onEdit}>
                  <Pencil className="h-3.5 w-3.5" />
                  更换
                </Button>
                <Button variant="ghost" size="sm" onClick={onDelete}>
                  <Trash2 className="h-3.5 w-3.5" />
                  删除
                </Button>
              </>
            )}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <label className="text-sm font-medium">{label}</label>
      <Input
        className="mt-1.5"
        type="password"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder={placeholder}
        autoComplete="new-password"
        autoFocus={editing}
      />
      {configured && (
        <button
          type="button"
          onClick={onCancel}
          className="mt-2 text-xs font-medium text-muted-foreground hover:text-foreground"
        >
          取消更换，继续使用已保存密钥
        </button>
      )}
    </div>
  );
}
