"use client";

import React, { useState } from "react";
import Link from "next/link";
import { Search, X } from "lucide-react";
import {
  Badge,
  Button,
  Card,
  EmptyState,
  ErrorState,
  Input,
  LoadingBlock,
  PageHeader,
  PageIntro,
  Pagination,
  Select,
} from "@/components/ui";
import { apiGet } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { useAsyncData } from "@/lib/use-data";
import type { DepartmentItem, EmployeeItem, Paged } from "@/lib/types";

const STATUS_TONE: Record<string, { label: string; tone: "green" | "neutral" | "amber" | "red" }> = {
  active: { label: "在职", tone: "green" },
  resigned: { label: "离职", tone: "neutral" },
  disabled: { label: "已禁用", tone: "red" },
  pending: { label: "未激活", tone: "amber" },
  missing: { label: "疑似离职", tone: "amber" },
};

export default function EmployeesPage() {
  const [departmentId, setDepartmentId] = useState("");
  const [status, setStatus] = useState("");
  const [keyword, setKeyword] = useState("");
  const [keywordInput, setKeywordInput] = useState("");
  const [page, setPage] = useState(1);

  const departments = useAsyncData<DepartmentItem[]>(() => apiGet("/api/departments"), []);
  const deptList = departments.data ?? [];

  const list = useAsyncData<Paged<EmployeeItem>>(
    () =>
      apiGet("/api/employees", {
        department_id: departmentId || undefined,
        status: status || undefined,
        keyword: keyword || undefined,
        page,
        page_size: 20,
      }),
    [departmentId, status, keyword, page]
  );

  const applyFilters = () => {
    setKeyword(keywordInput.trim());
    setPage(1);
  };

  const rows = list.data?.items ?? [];
  const total = list.data?.total ?? 0;

  return (
    <div>
      <PageHeader
        title="员工"
        desc={
          <>
            共 <span className="font-medium text-foreground">{total}</span> 人
          </>
        }
      />

      <PageIntro>
        查某个人的周报记录，点姓名进详情。<b>本周谁没交，请去「管理简报」的未提交名单</b>，那里可以直接复制名单去催。
      </PageIntro>

      <Card className="mb-4 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={departmentId}
            onChange={(e) => {
              setDepartmentId(e.target.value);
              setPage(1);
            }}
            className="w-40"
          >
            <option value="">全部部门</option>
            {deptList.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </Select>

          <Select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
            className="w-32"
          >
            <option value="">全部状态</option>
            <option value="active">在职</option>
            <option value="resigned">离职</option>
            <option value="disabled">已禁用</option>
            <option value="pending">未激活</option>
          </Select>

          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              value={keywordInput}
              onChange={(e) => setKeywordInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && applyFilters()}
              placeholder="搜索姓名"
              className="w-48 pl-8"
            />
          </div>
          <Button onClick={applyFilters}>
            <Search className="h-3.5 w-3.5" /> 查询
          </Button>
          {(departmentId || status || keyword) && (
            <Button
              variant="ghost"
              onClick={() => {
                setDepartmentId("");
                setStatus("");
                setKeyword("");
                setKeywordInput("");
                setPage(1);
              }}
            >
              <X className="h-3.5 w-3.5" /> 重置
            </Button>
          )}
        </div>
      </Card>

      {list.error ? (
        <Card><ErrorState message={list.error} onRetry={list.reload} /></Card>
      ) : (
        <Card>
          <div className="overflow-x-auto">
            <table className="w-full">
              <thead className="border-b border-border bg-muted/40">
                <tr>
                  <th className="thead-cell">姓名</th>
                  <th className="thead-cell">部门</th>
                  <th className="thead-cell">岗位</th>
                  <th className="thead-cell">状态</th>
                  <th className="thead-cell">最近提交</th>
                </tr>
              </thead>
              <tbody>
                {list.loading ? (
                  <tr><td colSpan={5}><LoadingBlock rows={6} /></td></tr>
                ) : rows.length === 0 ? (
                  <tr>
                    <td colSpan={5}><EmptyState title="没有找到员工" desc="试试调整筛选条件" /></td>
                  </tr>
                ) : (
                  rows.map((employee) => {
                    const statusInfo = STATUS_TONE[employee.status] ?? STATUS_TONE.active;
                    return (
                      <tr key={employee.id} className="border-b border-border/60 transition-colors hover:bg-muted/40">
                        <td className="tbody-cell">
                          <Link href={`/employees/${employee.id}`} className="font-medium text-primary hover:underline">
                            {employee.name}
                          </Link>
                        </td>
                        <td className="tbody-cell text-muted-foreground">{employee.department_name ?? "-"}</td>
                        <td className="tbody-cell text-muted-foreground">{employee.position ?? "-"}</td>
                        <td className="tbody-cell">
                          <Badge tone={statusInfo.tone}>{statusInfo.label}</Badge>
                        </td>
                        <td className="tbody-cell tabular-nums text-muted-foreground">
                          {formatDateTime(employee.last_report_at)}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
          <Pagination page={page} pageSize={20} total={total} onChange={setPage} />
        </Card>
      )}
    </div>
  );
}
