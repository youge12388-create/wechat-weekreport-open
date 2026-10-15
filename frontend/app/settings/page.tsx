"use client";

import React, { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BellRing,
  Bot,
  CheckCircle2,
  Languages,
  Palette,
  Pencil,
  Play,
  RefreshCw,
  Save,
  ServerCog,
  ShieldCheck,
  Trash2,
  Users,
} from "lucide-react";
import {
  Button,
  Card,
  ErrorState,
  Input,
  LoadingBlock,
  Modal,
  PageHeader,
  PageIntro,
  Select,
  StatTile,
  StatusDot,
} from "@/components/ui";
import { apiGet, apiPatch, apiPost } from "@/lib/api";
import { formatDateTime } from "@/lib/format";
import { describeReportJob } from "@/lib/sync-job";
import { useAsyncData } from "@/lib/use-data";
import { TASK_POLL_MAX_MS, usePolling } from "@/lib/use-polling";
import { useSession } from "@/components/session-provider";
import type {
  AccessOverview,
  AccessTree,
  DepartmentItem,
  EmployeeItem,
  LoginRole,
  LoginSwitch,
  Paged,
  ReminderSettings,
  RemindPreview,
  SyncStatus,
  SyncJobItem,
  SyncTypeStatus,
  WeComProbeResult,
} from "@/lib/types";
import { AccessTree as AccessTreeView } from "@/components/access-tree";
import { ReportExemptionTree } from "@/components/report-exemption-tree";
import { SyncErrorNotice } from "@/components/sync-error-notice";
import { useTheme } from "@/components/theme-provider";
import {
  DEFAULT_THEME,
  THEME_COLOR_FIELDS,
  THEME_PRESETS,
  contrastColor,
  isValidHexColor,
  type ThemeColorKey,
  type ThemeSettings,
} from "@/lib/theme";
import { ServiceStatus, SecretEditor } from "./components/service-status";
import { SETTINGS_CATEGORIES, SettingsCategoryNav } from "./components/category-nav";
import type { SettingsCategory } from "./components/category-nav";
import { SyncOverviewCard } from "./components/sync-overview-card";
import { AccessCard, fetchAllActiveEmployees } from "./components/access-card";
import type { CheckState, Notice, TestTarget } from "./components/settings-types";

interface AISettingsSnapshot {
  deepseek: {
    provider: string;
    base_url: string;
    model: string;
    api_key_configured: boolean;
  };
  baidu_translation: {
    app_id: string | null;
    api_key_configured: boolean;
    secret_key_configured: boolean;
    configured: boolean;
  };
  embedding: {
    base_url: string | null;
    model: string | null;
    api_key_configured: boolean;
    configured: boolean;
  };
  encryption: { production_key_required: boolean };
}

interface ModelListResponse {
  target: "deepseek";
  ok: boolean;
  models: string[];
  current_model: string;
  base_url: string;
}





export default function SettingsPage() {
  const { savedTheme, previewTheme, commitTheme } = useTheme();
  const { session, isAdmin, loading: sessionLoading } = useSession();
  const config = useAsyncData<AISettingsSnapshot>(() => apiGet("/api/settings/ai"), []);
  const reportSync = useAsyncData<SyncStatus>(() => apiGet("/api/sync/status"), []);
  const reportJobs = useAsyncData<SyncJobItem[]>(
    () => apiGet("/api/sync/jobs", { type: "weekly_reports", limit: 3 }, { bypassCache: true }),
    []
  );
  const reminder = useAsyncData<ReminderSettings>(() => apiGet("/api/settings/reminder"), []);
  const employees = useAsyncData<Paged<EmployeeItem>>(fetchAllActiveEmployees, []);
  const departments = useAsyncData<DepartmentItem[]>(() => apiGet("/api/departments"), []);
  const [category, setCategory] = useState<SettingsCategory>("sync");
  const [themeDraft, setThemeDraft] = useState<ThemeSettings>(savedTheme);
  const [themeSaving, setThemeSaving] = useState(false);
  const [themeNotice, setThemeNotice] = useState<Notice>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [deepSeekKey, setDeepSeekKey] = useState("");
  const [appId, setAppId] = useState("");
  const [baiduApiKey, setBaiduApiKey] = useState("");
  const [baiduSecretKey, setBaiduSecretKey] = useState("");
  const [clearDeepSeekKey, setClearDeepSeekKey] = useState(false);
  const [clearBaiduApiKey, setClearBaiduApiKey] = useState(false);
  const [clearBaiduSecretKey, setClearBaiduSecretKey] = useState(false);
  const [editingDeepSeekKey, setEditingDeepSeekKey] = useState(false);
  const [editingBaiduApiKey, setEditingBaiduApiKey] = useState(false);
  const [editingBaiduSecretKey, setEditingBaiduSecretKey] = useState(false);
  // 语义检索（向量）：三项齐备即启用，留空=保持原值，与 DeepSeek 密钥同一约定。
  const [embeddingBaseUrl, setEmbeddingBaseUrl] = useState("");
  const [embeddingModel, setEmbeddingModel] = useState("");
  const [embeddingKey, setEmbeddingKey] = useState("");
  const [clearEmbeddingKey, setClearEmbeddingKey] = useState(false);
  const [editingEmbeddingKey, setEditingEmbeddingKey] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [deepSeekState, setDeepSeekState] = useState<CheckState>("unknown");
  const [baiduState, setBaiduState] = useState<CheckState>("unknown");
  const [detectedModels, setDetectedModels] = useState<string[]>([]);
  const [modelHint, setModelHint] = useState<string | null>(null);
  const [reportSyncEnabled, setReportSyncEnabled] = useState(false);
  const [reportSyncWeekday, setReportSyncWeekday] = useState(4);
  const [reportSyncTime, setReportSyncTime] = useState("19:00");
  const [autoPeriodSummaryEnabled, setAutoPeriodSummaryEnabled] = useState(true);
  const [autoAiAnalysisEnabled, setAutoAiAnalysisEnabled] = useState(false);
  const [reportSyncSaving, setReportSyncSaving] = useState(false);
  const [reportSyncNotice, setReportSyncNotice] = useState<Notice>(null);
  const [manualSyncMode, setManualSyncMode] = useState<
    "current_period" | "last_period" | "all_history" | "all" | null
  >(null);
  const [probe, setProbe] = useState<WeComProbeResult | null>(null);
  const [probeLoading, setProbeLoading] = useState(false);
  const [probeError, setProbeError] = useState<string | null>(null);
  const [reminderEnabled, setReminderEnabled] = useState(false);
  const [reminderWeekday, setReminderWeekday] = useState(4);
  const [reminderHour, setReminderHour] = useState(21);
  const [attributionDays, setAttributionDays] = useState(1);
  const [reminderCardTitle, setReminderCardTitle] = useState("");
  const [reminderCardZh, setReminderCardZh] = useState("");
  const [reminderCardEn, setReminderCardEn] = useState("");
  const [reminderTextOpen, setReminderTextOpen] = useState(false);
  const [actionNotifyUserids, setActionNotifyUserids] = useState("");
  const [reminderSaving, setReminderSaving] = useState(false);
  const [reminderNotice, setReminderNotice] = useState<Notice>(null);
  const [reminderPreview, setReminderPreview] = useState<RemindPreview | null>(null);
  const [reminderPreviewOpen, setReminderPreviewOpen] = useState(false);
  const [reminderPreviewLoading, setReminderPreviewLoading] = useState(false);
  const [exemptOpen, setExemptOpen] = useState(false);
  const [exemptIds, setExemptIds] = useState<Set<number>>(new Set());
  const [exemptSaving, setExemptSaving] = useState(false);
  const [exemptNotice, setExemptNotice] = useState<Notice>(null);
  const autoDetectRef = useRef(false);

  // 同步任务进行中才轮询任务列表；后台标签页暂停、回到前台立即补刷；
  // 任务卡死超过 5 分钟停止轮询并提示，避免永远「进行中」+ 无限请求。
  const reportJobsPolling = Boolean(
    reportJobs.data?.some((job) => job.status === "pending" || job.status === "running")
  );
  usePolling(() => reportJobs.reload(), 3000, {
    enabled: reportJobsPolling,
    maxDurationMs: TASK_POLL_MAX_MS,
    onGiveUp: () =>
      setReportSyncNotice({
        text: "同步任务已超过 5 分钟未完成，可能后台服务异常；请稍后刷新本页确认，或联系管理员检查 Worker。",
        tone: "error",
      }),
  });

  const detectModels = async (overrides?: {
    baseUrl?: string;
    model?: string;
    apiKey?: string;
    silent?: boolean;
  }) => {
    const targetBaseUrl = (overrides?.baseUrl ?? baseUrl).trim();
    const targetModel = (overrides?.model ?? model).trim();
    setDeepSeekState("checking");
    setModelHint("正在从上游读取模型列表...");
    try {
      const payload: Record<string, string> = { ai_base_url: targetBaseUrl };
      const candidateKey = (overrides?.apiKey ?? deepSeekKey).trim();
      if (candidateKey) payload.ai_api_key = candidateKey;
      const result = await apiPost<ModelListResponse>("/api/settings/ai/models", payload);
      setDetectedModels(result.models);
      if (!targetModel && result.models[0]) setModel(result.models[0]);
      const currentModel = result.current_model || targetModel;
      const currentInList = result.models.includes(currentModel);
      setDeepSeekState(currentInList ? "available" : "attention");
      setModelHint(
        currentInList
          ? `已从上游获取 ${result.models.length} 个模型，当前模型可用。`
          : `已从上游获取 ${result.models.length} 个模型；当前模型名不在列表中，请从下拉建议中选择。`
      );
      if (!overrides?.silent) {
        setNotice({
          text: `模型检测成功，共获取 ${result.models.length} 个可用模型。`,
          tone: "success",
        });
      }
    } catch (error) {
      const text = error instanceof Error ? error.message : "模型列表不可用";
      const hint = `上游模型列表暂不可用（${text}）；当前模型仍需通过“测试 DeepSeek”验证实际连通性。`;
      setDeepSeekState("model_list_unavailable");
      setModelHint(hint);
      if (!overrides?.silent) setNotice({ text: hint, tone: "info" });
    }
  };

  useEffect(() => {
    // 深链 /settings?category=ai：消息中心点「去配置密钥」要直接落到 AI 分类，
    // 而不是把用户丢到设置页再让他自己找。放在 effect 里读 URL 才不会有
    // 服务端渲染 "sync"、客户端渲染 "ai" 的 hydration 不一致。
    const requested = new URLSearchParams(window.location.search).get("category");
    if (SETTINGS_CATEGORIES.some((item) => item.key === requested)) {
      setCategory(requested as SettingsCategory);
    }
  }, []);

  useEffect(() => {
    if (!config.data) return;
    const data = config.data;
    setBaseUrl(data.deepseek.base_url);
    setModel(data.deepseek.model);
    setAppId(data.baidu_translation.app_id ?? "");
    setEmbeddingBaseUrl(data.embedding.base_url ?? "");
    setEmbeddingModel(data.embedding.model ?? "");
    setDetectedModels([]);
    setModelHint(null);
    if (!autoDetectRef.current && data.deepseek.api_key_configured) {
      autoDetectRef.current = true;
      void detectModels({
        baseUrl: data.deepseek.base_url,
        model: data.deepseek.model,
        silent: true,
      });
    }
    // detectModels reads the current form state; automatic detection only runs once.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.data]);

  useEffect(() => {
    setThemeDraft(savedTheme);
  }, [savedTheme]);

  useEffect(() => {
    if (!reportSync.data) return;
    const schedule = reportSync.data.report_schedule;
    setReportSyncEnabled(schedule.auto_enabled);
    setReportSyncWeekday(schedule.weekday);
    setReportSyncTime(
      `${String(schedule.hour).padStart(2, "0")}:${String(schedule.minute).padStart(2, "0")}`
    );
    setAutoPeriodSummaryEnabled(schedule.auto_period_summary_enabled);
    setAutoAiAnalysisEnabled(schedule.ai_auto_enqueue_on_sync);
  }, [reportSync.data]);

  useEffect(() => {
    if (!reminder.data) return;
    setReminderEnabled(reminder.data.auto_enabled);
    setReminderWeekday(reminder.data.weekday);
    setReminderHour(reminder.data.hour);
    setAttributionDays(reminder.data.attribution_days);
    setReminderCardTitle(reminder.data.card_title);
    setReminderCardZh(reminder.data.card_body_zh);
    setReminderCardEn(reminder.data.card_body_en);
    setActionNotifyUserids(reminder.data.action_notify_admin_userids);
  }, [reminder.data]);

  useEffect(() => {
    if (!employees.data) return;
    setExemptIds(
      new Set(
        employees.data.items
          .filter((employee) => employee.report_exempt)
          .map((employee) => employee.id)
      )
    );
  }, [employees.data]);

  const save = async () => {
    setSaving(true);
    setNotice(null);
    const nextDeepSeekKey = deepSeekKey.trim();
    const nextBaiduApiKey = baiduApiKey.trim();
    const nextBaiduSecretKey = baiduSecretKey.trim();
    const nextEmbeddingKey = embeddingKey.trim();
    const baseUrlChanged = baseUrl.trim() !== config.data?.deepseek.base_url;
    try {
      await apiPatch<AISettingsSnapshot>("/api/settings/ai", {
        ai_base_url: baseUrl.trim(),
        ai_model: model.trim(),
        baidu_translate_app_id: appId.trim(),
        ...(nextDeepSeekKey ? { ai_api_key: nextDeepSeekKey } : {}),
        ...(nextBaiduApiKey ? { baidu_translate_api_key: nextBaiduApiKey } : {}),
        ...(nextBaiduSecretKey ? { baidu_translate_secret_key: nextBaiduSecretKey } : {}),
        ...(embeddingBaseUrl.trim() ? { ai_embedding_base_url: embeddingBaseUrl.trim() } : {}),
        ...(embeddingModel.trim() ? { ai_embedding_model: embeddingModel.trim() } : {}),
        ...(nextEmbeddingKey ? { ai_embedding_api_key: nextEmbeddingKey } : {}),
        clear_ai_api_key: clearDeepSeekKey,
        clear_baidu_translate_api_key: clearBaiduApiKey,
        clear_baidu_translate_secret_key: clearBaiduSecretKey,
        clear_ai_embedding_api_key: clearEmbeddingKey,
      });
      setDeepSeekKey("");
      setBaiduApiKey("");
      setBaiduSecretKey("");
      setEmbeddingKey("");
      setEditingDeepSeekKey(false);
      setEditingBaiduApiKey(false);
      setEditingBaiduSecretKey(false);
      setEditingEmbeddingKey(false);
      setClearDeepSeekKey(false);
      setClearBaiduApiKey(false);
      setClearBaiduSecretKey(false);
      setClearEmbeddingKey(false);
      setNotice({
        text: "配置已保存，后续接口和后台 Worker 会使用新配置。",
        tone: "success",
      });
      await config.reload();
      if (clearDeepSeekKey) {
        setDeepSeekState("unknown");
        setDetectedModels([]);
        setModelHint("已删除 API Key，填写新密钥后可重新检测模型。");
      } else if (nextDeepSeekKey || baseUrlChanged) {
        void detectModels({
          baseUrl: baseUrl.trim(),
          model: model.trim(),
          apiKey: nextDeepSeekKey || undefined,
          silent: true,
        });
      }
    } catch (error) {
      setNotice({
        text: error instanceof Error ? error.message : "保存失败",
        tone: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  const updateThemeColor = (key: ThemeColorKey, value: string) => {
    const next = { ...themeDraft, [key]: value };
    setThemeDraft(next);
    setThemeNotice(null);
    if (THEME_COLOR_FIELDS.every((field) => isValidHexColor(next[field.key]))) {
      previewTheme(next);
    }
  };

  const applyThemePreset = (next: ThemeSettings) => {
    setThemeDraft(next);
    previewTheme(next);
    setThemeNotice({
      text: "已套用预览，点击保存主题后刷新页面仍会保留。",
      tone: "info",
    });
  };

  const resetThemePreview = () => {
    setThemeDraft(DEFAULT_THEME);
    previewTheme(DEFAULT_THEME);
    setThemeNotice({
      text: "已恢复原始配色预览，点击保存主题后正式生效。",
      tone: "info",
    });
  };

  const saveTheme = async () => {
    if (!THEME_COLOR_FIELDS.every((field) => isValidHexColor(themeDraft[field.key]))) {
      setThemeNotice({ text: "请把颜色填写为 6 位十六进制值，例如 #1A1C28。", tone: "error" });
      return;
    }
    setThemeSaving(true);
    setThemeNotice(null);
    try {
      const saved = await apiPatch<ThemeSettings>("/api/settings/theme", themeDraft);
      commitTheme(saved);
      setThemeDraft(saved);
      setThemeNotice({ text: "主题已保存，所有页面已使用新配色。", tone: "success" });
    } catch (error) {
      setThemeNotice({
        text: error instanceof Error ? error.message : "主题保存失败",
        tone: "error",
      });
    } finally {
      setThemeSaving(false);
    }
  };

  const saveReminder = async () => {
    setReminderSaving(true);
    setReminderNotice(null);
    try {
      const payload: Record<string, unknown> = {
        auto_enabled: reminderEnabled,
        weekday: reminderWeekday,
        hour: reminderHour,
        attribution_days: attributionDays,
      };
      // 只提交真正改过的文案字段：留空在后端是「保持原值」，
      // 但这里显式比对一次，避免把未改动的输入框也当作一次修改。
      if (reminder.data) {
        if (reminderCardTitle.trim() !== reminder.data.card_title) {
          payload.card_title = reminderCardTitle.trim();
        }
        if (reminderCardZh.trim() !== reminder.data.card_body_zh) {
          payload.card_body_zh = reminderCardZh.trim();
        }
        if (reminderCardEn.trim() !== reminder.data.card_body_en) {
          payload.card_body_en = reminderCardEn.trim();
        }
        // 通知接收人：清空输入框用显式标志，避免被当成「保持原值」（与密钥同一约定）。
        if (actionNotifyUserids.trim() !== reminder.data.action_notify_admin_userids) {
          if (actionNotifyUserids.trim()) {
            payload.action_notify_admin_userids = actionNotifyUserids.trim();
          } else {
            payload.clear_action_notify_userids = true;
          }
        }
      }
      await apiPatch<ReminderSettings>("/api/settings/reminder", payload);
      await reminder.reload();
      setReminderNotice({
        text: reminderEnabled
          ? "催交设置已保存，Worker 下一轮会按新时间与新文案执行。"
          : "保存成功。自动催交当前处于关闭状态，不会自动发送企微消息。",
        tone: "success",
      });
    } catch (error) {
      setReminderNotice({
        text: error instanceof Error ? error.message : "自动催交设置保存失败",
        tone: "error",
      });
    } finally {
      setReminderSaving(false);
    }
  };

  const resetReminderCardText = async () => {
    setReminderSaving(true);
    setReminderNotice(null);
    try {
      await apiPatch<ReminderSettings>("/api/settings/reminder", {
        clear_card_text: true,
      });
      await reminder.reload();
      setReminderNotice({ text: "催交文案已恢复为默认的中英双语模板。", tone: "success" });
    } catch (error) {
      setReminderNotice({
        text: error instanceof Error ? error.message : "恢复默认文案失败",
        tone: "error",
      });
    } finally {
      setReminderSaving(false);
    }
  };

  const saveReportSync = async () => {
    const [hourText, minuteText] = reportSyncTime.split(":");
    const hour = Number(hourText);
    const minute = Number(minuteText);
    if (
      !Number.isInteger(hour) ||
      !Number.isInteger(minute) ||
      hour < 0 ||
      hour > 23 ||
      minute < 0 ||
      minute > 59
    ) {
      setReportSyncNotice({ text: "请选择有效的拉取时间。", tone: "error" });
      return;
    }
    setReportSyncSaving(true);
    setReportSyncNotice(null);
    try {
      await apiPatch<SyncStatus>("/api/sync/schedule", {
        auto_enabled: reportSyncEnabled,
        auto_period_summary_enabled: autoPeriodSummaryEnabled,
        ai_auto_enqueue_on_sync: autoAiAnalysisEnabled,
        weekday: reportSyncWeekday,
        hour,
        minute,
      });
      await reportSync.reload();
      setReportSyncNotice({
        text: reportSyncEnabled
          ? "自动同步周报已保存，Worker 下一轮会按新时间执行。"
          : "自动同步周报已关闭，不会按计划自动拉取。",
        tone: "success",
      });
    } catch (error) {
      setReportSyncNotice({
        text: error instanceof Error ? error.message : "自动同步设置保存失败",
        tone: "error",
      });
    } finally {
      setReportSyncSaving(false);
    }
  };

  const runManualReportSync = async (
    mode: "current_period" | "last_period" | "all_history"
  ) => {
    setManualSyncMode(mode);
    setReportSyncNotice(null);
    try {
      const result = await apiPost<{ created: boolean; window: string; message: string }>(
        "/api/sync/wecom/reports/queue",
        { mode }
      );
      await reportSync.reload();
      reportJobs.reload();
      setReportSyncNotice({
        text: result.message,
        tone: result.created ? "success" : "info",
      });
    } catch (error) {
      setReportSyncNotice({
        text: error instanceof Error ? error.message : "提交周报同步失败",
        tone: "error",
      });
    } finally {
      setManualSyncMode(null);
    }
  };

  /** 整批同步部门、员工与周报。只在通讯录有变动时需要，因此从首页移到这里。 */
  const runManualFullSync = async () => {
    setManualSyncMode("all");
    setReportSyncNotice(null);
    try {
      const result = await apiPost<{ jobs?: { created: boolean }[] }>("/api/sync/wecom/all", undefined, { timeoutMs: 30000 });
      await reportSync.reload();
      const created = (result.jobs ?? []).filter((job) => job.created).length;
      setReportSyncNotice({
        text:
          created > 0
            ? "已提交后台整批同步部门、员工与周报。"
            : "三类同步都已在排队/执行中，本次未重复创建。",
        tone: created > 0 ? "success" : "info",
      });
    } catch (error) {
      setReportSyncNotice({
        text: error instanceof Error ? error.message : "提交整批同步失败",
        tone: "error",
      });
    } finally {
      setManualSyncMode(null);
    }
  };

  const runWecomProbe = async () => {
    setProbeLoading(true);
    setProbeError(null);
    try {
      setProbe(await apiPost<WeComProbeResult>("/api/sync/wecom/probe", {}));
    } catch (error) {
      setProbeError(
        error instanceof Error ? error.message : "连通性检测失败，请稍后重试"
      );
      setProbe(null);
    } finally {
      setProbeLoading(false);
    }
  };

  const saveExemptions = async () => {
    if (!employees.data) return;
    const updates = employees.data.items
      .filter((employee) => employee.report_exempt !== exemptIds.has(employee.id))
      .map((employee) => ({
        id: employee.id,
        report_exempt: exemptIds.has(employee.id),
      }));
    if (updates.length === 0) {
      setExemptNotice({ text: "免提交名单没有变化。", tone: "info" });
      return;
    }
    setExemptSaving(true);
    setExemptNotice(null);
    try {
      await apiPatch("/api/employees/report-exemptions", { updates });
      await employees.reload();
      setExemptNotice({
        text: `免提交名单已保存，共 ${exemptIds.size} 人。`,
        tone: "success",
      });
    } catch (error) {
      setExemptNotice({
        text: error instanceof Error ? error.message : "免提交名单保存失败",
        tone: "error",
      });
    } finally {
      setExemptSaving(false);
    }
  };

  const openReminderPreview = async () => {
    if (!reminder.data) return;
    setReminderPreviewOpen(true);
    setReminderPreviewLoading(true);
    setReminderNotice(null);
    try {
      const preview = await apiPost<RemindPreview>("/api/notifications/remind", {
        start: reminder.data.current_period.start,
        end: reminder.data.current_period.end,
        dry_run: true,
      });
      setReminderPreview(preview);
    } catch (error) {
      setReminderNotice({
        text: error instanceof Error ? error.message : "提醒名单加载失败",
        tone: "error",
      });
      setReminderPreviewOpen(false);
    } finally {
      setReminderPreviewLoading(false);
    }
  };

  const sendReminderNow = async () => {
    if (!reminder.data || !reminderPreview) return;
    setReminderPreviewLoading(true);
    setReminderNotice(null);
    try {
      const result = await apiPost<RemindPreview>("/api/notifications/remind", {
        start: reminder.data.current_period.start,
        end: reminder.data.current_period.end,
        dry_run: false,
      });
      setReminderPreview(result);
      // 结论一律由后端的 outcome 决定，不在前端用 sent/error 反推：
      // 发出去了、有人没收到，和整批被拒是两种问题，处理方式也不同。
      const delivered = result.result?.delivered_count ?? 0;
      const undelivered = result.result?.undelivered ?? [];
      if (result.outcome === "failed") {
        setReminderNotice({
          text: `发送失败（企微未受理）：${result.error ?? "未知原因"}`,
          tone: "error",
        });
      } else if (result.outcome === "sent" && undelivered.length) {
        setReminderNotice({
          text: `已送达 ${delivered} 人，另有 ${undelivered.length} 人未被企微投递：${undelivered
            .slice(0, 5)
            .map((item) => item.name)
            .join("、")}${undelivered.length > 5 ? " 等" : ""}。请在企微后台确认该自建应用的可见范围。`,
          tone: "info",
        });
      } else if (result.outcome === "sent") {
        setReminderNotice({
          text: `已向 ${delivered} 人发送催交提醒。`,
          tone: "success",
        });
      } else {
        setReminderNotice({
          text: "当前没有可发送对象，或目标仍在提醒冷却期内。",
          tone: "info",
        });
      }
      await reminder.reload();
    } catch (error) {
      setReminderNotice({
        text: error instanceof Error ? error.message : "发送失败",
        tone: "error",
      });
    } finally {
      setReminderPreviewLoading(false);
    }
  };

  const testConnection = async (target: TestTarget) => {
    if (target === "deepseek") setDeepSeekState("checking");
    else setBaiduState("checking");
    setNotice(null);
    try {
      await apiPost("/api/settings/ai/test", { target });
      if (target === "deepseek") setDeepSeekState("available");
      else setBaiduState("available");
      setNotice({
        text: `${target === "deepseek" ? "DeepSeek" : "百度翻译"} 连通性测试成功。`,
        tone: "success",
      });
    } catch (error) {
      if (target === "deepseek") setDeepSeekState("failed");
      else setBaiduState("failed");
      setNotice({
        text: error instanceof Error ? error.message : "连通性测试失败",
        tone: "error",
      });
    }
  };

  if (config.error) {
    return (
      <Card>
        <ErrorState message={config.error} onRetry={config.reload} />
      </Card>
    );
  }

  if (config.loading || !config.data) {
    return (
      <Card>
        <LoadingBlock rows={6} />
      </Card>
    );
  }

  const data = config.data;
  const reportSchedule = reportSync.data?.report_schedule;
  const reportSyncDirty = Boolean(
    reportSchedule &&
      (reportSyncEnabled !== reportSchedule.auto_enabled ||
        autoPeriodSummaryEnabled !== reportSchedule.auto_period_summary_enabled ||
        autoAiAnalysisEnabled !== reportSchedule.ai_auto_enqueue_on_sync ||
        reportSyncWeekday !== reportSchedule.weekday ||
        reportSyncTime !==
          `${String(reportSchedule.hour).padStart(2, "0")}:${String(
            reportSchedule.minute
          ).padStart(2, "0")}`)
  );
  const reminderDirty = Boolean(
    reminder.data &&
      (reminderEnabled !== reminder.data.auto_enabled ||
        reminderWeekday !== reminder.data.weekday ||
        reminderHour !== reminder.data.hour ||
        attributionDays !== reminder.data.attribution_days ||
        reminderCardTitle.trim() !== reminder.data.card_title ||
        reminderCardZh.trim() !== reminder.data.card_body_zh ||
        reminderCardEn.trim() !== reminder.data.card_body_en ||
        actionNotifyUserids.trim() !== reminder.data.action_notify_admin_userids)
  );
  const employeeItems = employees.data?.items ?? [];
  const exemptDirty = employeeItems.some(
    (employee) => employee.report_exempt !== exemptIds.has(employee.id)
  );
  const closeExemptions = () => {
    if (exemptSaving) return;
    setExemptOpen(false);
    setExemptIds(
      new Set(
        employeeItems
          .filter((employee) => employee.report_exempt)
          .map((employee) => employee.id)
      )
    );
    setExemptNotice(null);
  };
  const updateExemptSelection = (ids: number[], checked: boolean) => {
    setExemptIds((current) => {
      const next = new Set(current);
      for (const id of ids) {
        if (checked) next.add(id);
        else next.delete(id);
      }
      return next;
    });
    setExemptNotice(null);
  };
  const isDirty =
    baseUrl !== data.deepseek.base_url ||
    model !== data.deepseek.model ||
    appId !== (data.baidu_translation.app_id ?? "") ||
    Boolean(deepSeekKey.trim()) ||
    Boolean(baiduApiKey.trim()) ||
    Boolean(baiduSecretKey.trim()) ||
    clearDeepSeekKey ||
    clearBaiduApiKey ||
    clearBaiduSecretKey;
  const themeDirty = THEME_COLOR_FIELDS.some(
    ({ key }) => themeDraft[key].toLowerCase() !== savedTheme[key].toLowerCase()
  );

  const noticeClass =
    notice?.tone === "success"
      ? "border-success/30 bg-success-soft text-success"
      : notice?.tone === "error"
        ? "border-destructive/30 bg-destructive-soft text-destructive"
        : "border-primary/20 bg-primary-soft/40 text-foreground";

  // 只读成员不该停在设置页：这一页所有操作都会 403。
  // 后端才是真正的边界（写请求一律拒绝），这里只是别让人白跑一趟。
  if (!sessionLoading && session?.authenticated && !isAdmin) {
    return (
      <div className="max-w-2xl">
        <Card>
          <div className="p-6">
            <h1 className="text-base font-semibold">仅可写账号可以进入系统设置</h1>
            <p className="mt-2 text-sm leading-relaxed text-muted-foreground">
              当前账号为只读权限，可以查看全部业务数据。如需调整同步、催交、授权或密钥，
              请联系管理员。
            </p>
            <div className="mt-4">
              <a
                href="/dashboard"
                className="inline-flex h-9 items-center rounded-control border border-input bg-surface px-4 text-sm font-medium transition-colors hover:bg-muted"
              >
                返回管理简报
              </a>
            </div>
          </div>
        </Card>
      </div>
    );
  }

  return (
    <div className="max-w-6xl">
      <PageHeader
        title="系统设置"
        desc="管理自动抓取、周报规则、AI 模型、翻译服务与界面主题；密钥加密保存，页面不会回读明文。"
        action={
          category === "theme" ? (
            <div className="flex flex-wrap items-center gap-3">
              <span
                className={
                  themeDirty
                    ? "text-xs font-medium text-warning"
                    : "text-xs text-muted-foreground"
                }
              >
                {themeDirty ? "主题有未保存修改" : "主题已同步"}
              </span>
              <Button
                onClick={() => void saveTheme()}
                loading={themeSaving}
                disabled={data.encryption.production_key_required || !themeDirty}
              >
                <Save className="h-3.5 w-3.5" />
                保存主题
              </Button>
            </div>
          ) : (
            <div className="flex flex-wrap items-center gap-3">
              <span
                className={
                  isDirty
                    ? "text-xs font-medium text-warning"
                    : "text-xs text-muted-foreground"
                }
              >
                {isDirty ? "AI 配置有未保存修改" : "AI 配置已同步"}
              </span>
              <Button
                onClick={() => void save()}
                loading={saving}
                disabled={data.encryption.production_key_required || !isDirty}
              >
                <Save className="h-3.5 w-3.5" />
                保存设置
              </Button>
            </div>
          )
        }
      />
      <PageIntro>
        已保存的密钥不需要重新填写，也不会要求刻意留空。自动同步、自动催交与界面主题分别独立保存，不会影响 AI 密钥。
      </PageIntro>

      {data.encryption.production_key_required && (
        <div className="mb-4 rounded-control bg-warning-soft px-4 py-3 text-sm text-warning">
          生产环境尚未设置 <code>CONFIG_ENCRYPTION_KEY</code>，为保护密钥，当前禁止保存。请由部署管理员先配置该环境变量。
        </div>
      )}
      {notice && (
        <div className={`mb-4 rounded-control border px-4 py-3 text-sm ${noticeClass}`}>
          {notice.text}
        </div>
      )}

      <div className="flex flex-col gap-5 lg:flex-row lg:items-start lg:gap-6">
        <SettingsCategoryNav
          value={category}
          onChange={setCategory}
          badges={{
            sync: reportSync.data?.last_error_detail ? (
              <span
                className="h-2 w-2 rounded-full bg-destructive"
                title="同步存在未恢复的失败"
              />
            ) : reportSync.data?.last_error_resolved ? (
              <span
                className="h-2 w-2 rounded-full bg-success/60"
                title="历史失败已恢复"
              />
            ) : null,
            rules: reminderDirty ? (
              <span
                className="h-2 w-2 rounded-full bg-warning"
                title="催交设置有未保存修改"
              />
            ) : null,
            ai: isDirty ? (
              <span
                className="h-2 w-2 rounded-full bg-warning"
                title="AI 配置有未保存修改"
              />
            ) : null,
            theme: themeDirty ? (
              <span
                className="h-2 w-2 rounded-full bg-warning"
                title="主题有未保存修改"
              />
            ) : null,
          }}
        />
        <div className="min-w-0 flex-1 space-y-4">
        {category === "sync" && (
        <>
        <SyncOverviewCard
          data={reportSync.data}
          loading={reportSync.loading}
          error={reportSync.error}
          onRetry={reportSync.reload}
        />
        <Card>
          <div className="card-head card-head-line">
            <RefreshCw className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h2 className="text-base font-semibold">自动同步周报</h2>
                {reportSchedule && (
                  <StatusDot tone={reportSchedule.auto_enabled ? "green" : "neutral"}>
                    {reportSchedule.auto_enabled ? "已开启" : "已关闭"}
                  </StatusDot>
                )}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                按业务时区每周拉取一次；部门与员工继续按现有周期校准。抓取到新增或变更周报后，可自动生成本期 AI 总结。
              </p>
            </div>
          </div>
          {reportSync.loading ? (
            <LoadingBlock rows={4} />
          ) : reportSync.error ? (
            <div className="p-5">
              <ErrorState message={reportSync.error} onRetry={reportSync.reload} />
            </div>
          ) : reportSync.data && reportSchedule ? (
            <>
              <div className="grid gap-5 p-5 md:grid-cols-[1.1fr_1fr]">
                <div className="space-y-4">
                  <div className="flex items-center justify-between gap-4 rounded-control bg-muted/40 px-4 py-3">
                    <div>
                      <p className="text-sm font-medium">自动拉取</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        关闭后 Worker 不会按计划拉取新周报
                      </p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={reportSyncEnabled}
                      onClick={() => setReportSyncEnabled((enabled) => !enabled)}
                      className={
                        reportSyncEnabled
                          ? "relative h-6 w-11 shrink-0 rounded-full bg-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          : "relative h-6 w-11 shrink-0 rounded-full bg-muted-foreground/25 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      }
                    >
                      <span
                        className={
                          reportSyncEnabled
                            ? "absolute left-5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                            : "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                        }
                      />
                    </button>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="text-sm font-medium">
                      拉取星期
                      <Select
                        className="mt-1.5"
                        value={String(reportSyncWeekday)}
                        onChange={(event) =>
                          setReportSyncWeekday(Number(event.target.value))
                        }
                      >
                        {["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map(
                          (label, index) => (
                            <option key={label} value={index}>
                              {label}
                            </option>
                          )
                        )}
                      </Select>
                    </label>
                    <label className="text-sm font-medium">
                      拉取时间
                      <Input
                        className="mt-1.5"
                        type="time"
                        value={reportSyncTime}
                        onChange={(event) => setReportSyncTime(event.target.value)}
                      />
                    </label>
                  </div>
                  <div className="flex items-center justify-between gap-4 rounded-control bg-muted/40 px-4 py-3">
                    <div>
                      <p className="text-sm font-medium">抓取后自动生成 AI 周期总结</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        仅在本批出现新增或内容变化的周报时触发
                      </p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={autoPeriodSummaryEnabled}
                      onClick={() =>
                        setAutoPeriodSummaryEnabled((enabled) => !enabled)
                      }
                      className={
                        autoPeriodSummaryEnabled
                          ? "relative h-6 w-11 shrink-0 rounded-full bg-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          : "relative h-6 w-11 shrink-0 rounded-full bg-muted-foreground/25 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      }
                    >
                      <span
                        className={
                          autoPeriodSummaryEnabled
                            ? "absolute left-5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                            : "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                        }
                      />
                    </button>
                  </div>
                  <div className="flex items-center justify-between gap-4 rounded-control bg-muted/40 px-4 py-3">
                    <div>
                      <p className="text-sm font-medium">抓取后自动 AI 分析单篇周报</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        默认关闭：分析仅在周报详情页手动点击时进行。开启后，新增或内容变化的周报会自动进入 AI 分析队列
                      </p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={autoAiAnalysisEnabled}
                      onClick={() =>
                        setAutoAiAnalysisEnabled((enabled) => !enabled)
                      }
                      className={
                        autoAiAnalysisEnabled
                          ? "relative h-6 w-11 shrink-0 rounded-full bg-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          : "relative h-6 w-11 shrink-0 rounded-full bg-muted-foreground/25 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      }
                    >
                      <span
                        className={
                          autoAiAnalysisEnabled
                            ? "absolute left-5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                            : "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                        }
                      />
                    </button>
                  </div>
                  <Button
                    onClick={() => void saveReportSync()}
                    loading={reportSyncSaving}
                    disabled={!reportSyncDirty}
                  >
                    <Save className="h-3.5 w-3.5" />
                    保存自动同步
                  </Button>
                  <div className="rounded-control bg-muted/40 px-4 py-3">
                    <p className="text-sm font-medium">手动补抓</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      只抓周报，不动部门与员工。重复点击不会叠出多条任务：同类型已有排队/执行中的任务时会被去重。
                    </p>
                    <div className="mt-3 flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        loading={manualSyncMode === "current_period"}
                        disabled={manualSyncMode !== null}
                        onClick={() => void runManualReportSync("current_period")}
                      >
                        {manualSyncMode !== "current_period" && (
                          <RefreshCw className="h-3.5 w-3.5" />
                        )}
                        同步本周周报
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        loading={manualSyncMode === "last_period"}
                        disabled={manualSyncMode !== null}
                        onClick={() => void runManualReportSync("last_period")}
                      >
                        {manualSyncMode !== "last_period" && (
                          <RefreshCw className="h-3.5 w-3.5" />
                        )}
                        同步上周周报
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        loading={manualSyncMode === "all_history"}
                        disabled={manualSyncMode !== null}
                        onClick={() => void runManualReportSync("all_history")}
                      >
                        同步全部历史周报
                      </Button>
                    </div>
                    <div className="mt-4 border-t border-border pt-3 text-xs">
                      <p className="font-medium">最近 3 次周报抓取</p>
                      {reportJobs.error && (
                        <button className="mt-2 text-destructive underline" onClick={() => reportJobs.reload()}>
                          任务结果加载失败，点击重试
                        </button>
                      )}
                      {!reportJobs.error && reportJobs.data?.length === 0 && (
                        <p className="mt-2 text-muted-foreground">暂无抓取记录</p>
                      )}
                      <div className="mt-2 space-y-2">
                        {reportJobs.data?.map((job) => (
                          <div key={job.id} className="rounded-control bg-surface px-3 py-2">
                            <p className="font-medium">
                              {job.window ?? "周报抓取"} · {formatDateTime(job.end_time ?? job.start_time)}
                            </p>
                            <p className="mt-1 text-muted-foreground">{describeReportJob(job)}</p>
                          </div>
                        ))}
                      </div>
                    </div>
                  </div>

                  {/* 通讯录几乎不变，整批同步不该占首页焦点；新同事入职时才用它。 */}
                  <div className="rounded-control bg-muted/40 px-4 py-3">
                    <p className="text-sm font-medium">部门与员工通讯录</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      整批同步部门、员工与周报。仅在通讯录有变动（新同事入职、部门调整）时需要。
                    </p>
                    <Button
                      className="mt-3"
                      variant="outline"
                      size="sm"
                      loading={manualSyncMode === "all"}
                      disabled={manualSyncMode !== null}
                      onClick={() => void runManualFullSync()}
                    >
                      {manualSyncMode !== "all" && <RefreshCw className="h-3.5 w-3.5" />}
                      同步部门与员工
                    </Button>
                  </div>

                  <div className="rounded-control bg-muted/40 px-4 py-3">
                    <p className="text-sm font-medium">连通性检测</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      现场验证企业微信是否放行本服务器，并回显企微看到的出口 IP。
                      刚改完可信 IP 时用它确认是否生效，不必等下一次定时抓取。
                    </p>
                    <Button
                      className="mt-3"
                      variant="outline"
                      size="sm"
                      onClick={() => void runWecomProbe()}
                      loading={probeLoading}
                    >
                      {!probeLoading && <Activity className="h-3.5 w-3.5" />}
                      检测企业微信连通性
                    </Button>
                    {probeError && (
                      <p className="mt-2.5 text-xs text-destructive">{probeError}</p>
                    )}
                    {probe && (
                      <div
                        className={
                          probe.ok
                            ? "mt-3 rounded-control border border-success/30 bg-success-soft px-3 py-2.5 text-xs text-success"
                            : "mt-3 rounded-control border border-destructive/30 bg-destructive-soft px-3 py-2.5 text-xs text-destructive"
                        }
                      >
                        {probe.ok ? (
                          <>
                            <p className="font-medium">企业微信连通正常</p>
                            <p className="mt-1 opacity-90">
                              凭据、通讯录与汇报接口均可访问（
                              {formatDateTime(probe.checked_at)}）。
                            </p>
                          </>
                        ) : (
                          <>
                            <p className="font-medium">
                              {probe.error?.title ?? "检测未通过"}
                            </p>
                            {probe.egress_ip && (
                              <p className="mt-1">
                                企微看到的出口 IP：
                                <b className="font-mono">{probe.egress_ip}</b>
                                。请确认这个地址已加入企业可信 IP（不是服务器内网 IP）。
                              </p>
                            )}
                            {probe.error?.hint && (
                              <p className="mt-1 opacity-90">
                                处理：{probe.error.hint}
                              </p>
                            )}
                          </>
                        )}
                        <ul className="mt-2 space-y-0.5">
                          {probe.steps.map((step) => (
                            <li key={step.step} className="flex items-center gap-1.5">
                              <span
                                className={
                                  step.ok
                                    ? "h-1.5 w-1.5 shrink-0 rounded-full bg-success"
                                    : "h-1.5 w-1.5 shrink-0 rounded-full bg-destructive"
                                }
                              />
                              <span className="font-mono">{step.step}</span>
                              <span className="opacity-80">
                                {step.ok
                                  ? (step.detail ?? "通过")
                                  : `失败${step.error_type ? ` · ${step.error_type}` : ""}`}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-3 self-start">
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">下次运行</p>
                    <p className="mt-1 text-sm font-semibold">
                      {reportSchedule.auto_enabled
                        ? formatDateTime(reportSchedule.next_run_at)
                        : "未开启"}
                    </p>
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">最近成功</p>
                    <p className="mt-1 text-sm font-semibold">
                      {formatDateTime(reportSync.data.reports.last_success_at)}
                    </p>
                    {reportSync.data.reports.last_success_window && (
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        抓取范围：{reportSync.data.reports.last_success_window}
                      </p>
                    )}
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">最近失败</p>
                    <p className="mt-1 text-sm font-semibold">
                      {formatDateTime(reportSync.data.reports.last_failure_at)}
                    </p>
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">执行状态</p>
                    <p className="mt-1 text-sm font-semibold">
                      {reportSync.data.reports.active
                        ? reportSync.data.reports.active_status === "running"
                          ? "拉取中"
                          : "等待执行"
                        : "空闲"}
                    </p>
                  </div>
                </div>
              </div>
              {reportSync.data.reports.last_error_resolved && (
                <div className="border-t border-border bg-muted/40 px-5 py-3 text-xs text-muted-foreground">
                  上次失败已恢复正常（{formatDateTime(reportSync.data.reports.last_failure_at)}），
                  无需处理；失败详情可在上方同步总览查看。
                </div>
              )}
              {reportSyncNotice && (
                <div
                  className={
                    reportSyncNotice.tone === "success"
                      ? "border-t border-success/30 bg-success-soft px-5 py-3 text-xs text-success"
                      : reportSyncNotice.tone === "error"
                        ? "border-t border-destructive/30 bg-destructive-soft px-5 py-3 text-xs text-destructive"
                        : "border-t border-border bg-muted/40 px-5 py-3 text-xs text-muted-foreground"
                  }
                >
                  {reportSyncNotice.text}
                </div>
              )}
            </>
          ) : null}
        </Card>
        </>
        )}

        {category === "rules" && (
        <Card>
          <div className="card-head card-head-line">
            <BellRing className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h2 className="text-base font-semibold">周报规则</h2>
                {reminder.data && (
                  <StatusDot tone={reminder.data.auto_enabled ? "green" : "neutral"}>
                    {reminder.data.auto_enabled ? "已开启" : "已关闭"}
                  </StatusDot>
                )}
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                统一设置催交时间与免提交人员；免提交人员不进入统计、AI 计算和催交。
              </p>
            </div>
          </div>
          {reminder.loading ? (
            <LoadingBlock rows={4} />
          ) : reminder.error ? (
            <div className="p-5">
              <ErrorState message={reminder.error} onRetry={reminder.reload} />
            </div>
          ) : reminder.data ? (
            <>
              <div className="grid gap-5 p-5 md:grid-cols-[1.1fr_1fr]">
                <div className="space-y-4">
                  <div className="flex items-center justify-between gap-4 rounded-control bg-muted/40 px-4 py-3">
                    <div>
                      <p className="text-sm font-medium">自动提醒</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">
                        关闭后后台 Worker 不会自动发送企微消息
                      </p>
                    </div>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={reminderEnabled}
                      onClick={() => setReminderEnabled((enabled) => !enabled)}
                      className={
                        reminderEnabled
                          ? "relative h-6 w-11 shrink-0 rounded-full bg-primary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          : "relative h-6 w-11 shrink-0 rounded-full bg-muted-foreground/25 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      }
                    >
                      <span
                        className={
                          reminderEnabled
                            ? "absolute left-5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                            : "absolute left-0.5 top-0.5 h-5 w-5 rounded-full bg-surface shadow-sm transition-transform"
                        }
                      />
                    </button>
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <label className="text-sm font-medium">
                      星期
                      <Select
                        className="mt-1.5"
                        value={String(reminderWeekday)}
                        onChange={(event) => setReminderWeekday(Number(event.target.value))}
                      >
                        {["周一", "周二", "周三", "周四", "周五", "周六", "周日"].map((label, index) => (
                          <option key={label} value={index}>{label}</option>
                        ))}
                      </Select>
                    </label>
                    <label className="text-sm font-medium">
                      时间
                      <Select
                        className="mt-1.5"
                        value={String(reminderHour)}
                        onChange={(event) => setReminderHour(Number(event.target.value))}
                      >
                        {Array.from({ length: 24 }, (_, hour) => (
                          <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>
                        ))}
                      </Select>
                    </label>
                  </div>
                  <div className="rounded-control bg-muted/40 px-4 py-3">
                    <label className="text-sm font-medium">
                      周期归属
                      <Select
                        className="mt-1.5"
                        value={String(attributionDays)}
                        onChange={(event) => setAttributionDays(Number(event.target.value))}
                      >
                        {Array.from({ length: 8 }, (_, days) => (
                          <option key={days} value={days}>
                            {days === 0
                              ? "按提交当天归类"
                              : reminder.data?.period_rule === "month"
                                ? `每月前 ${days} 天提交的报告归上一月`
                                : reminder.data?.period_rule === "custom_week"
                                  ? `周期开始后前 ${days} 天提交的周报归上一周期`
                                  : days === 1
                                    ? "周一交的归上一周（默认）"
                                    : `周一~${["", "", "周二", "周三", "周四", "周五", "周六", "周日"][days]}交的归上一周`}
                          </option>
                        ))}
                      </Select>
                    </label>
                    <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                      周期开始后的前 N 天内提交的报告归上一周期；日报不受此规则影响。
                      自然周默认就是周一补交归上周，月度和自定义周按各自周期起点计算。
                      <span className="mt-1 block text-foreground">
                        {reminder.data && attributionDays === reminder.data.attribution_days
                          ? `当前生效：${reminder.data.attribution_label}`
                          : reminder.data
                            ? `当前生效：${reminder.data.attribution_label}（保存后变为上面所选）`
                            : ""}
                      </span>
                    </p>
                  </div>
                  <div className="flex flex-wrap items-center gap-3">
                    <Button onClick={() => void saveReminder()} loading={reminderSaving} disabled={!reminderDirty}>
                      <Save className="h-3.5 w-3.5" />
                      保存催交设置
                    </Button>
                    <Button variant="outline" onClick={() => void openReminderPreview()}>
                      <Play className="h-3.5 w-3.5" />
                      预览本周名单
                    </Button>
                    <Button
                      variant="ghost"
                      onClick={() => setReminderTextOpen((open) => !open)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                      {reminderTextOpen ? "收起文案设置" : "编辑提醒文案"}
                    </Button>
                  </div>
                  {reminderTextOpen && (
                    <div className="space-y-3 rounded-control border border-dashed border-border p-4">
                      <div>
                        <p className="text-sm font-medium">提醒文案</p>
                        <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                          中英双语会依次出现在同一张企微卡片里（英文同事只看得懂中文版等于没提醒）。
                          可用占位符 <code className="rounded bg-muted px-1">{"{period}"}</code>
                          （如 2026-09-07 ~ 2026-09-13）、<code className="rounded bg-muted px-1">{"{start}"}</code>、
                          <code className="rounded bg-muted px-1">{"{end}"}</code>；每段最后一行会显示为高亮行。
                        </p>
                      </div>
                      <label className="block text-sm font-medium">
                        卡片标题
                        <Input
                          className="mt-1.5"
                          value={reminderCardTitle}
                          maxLength={128}
                          onChange={(event) => setReminderCardTitle(event.target.value)}
                          placeholder="周报提醒 · Weekly Report"
                        />
                      </label>
                      <label className="block text-sm font-medium">
                        中文正文
                        <textarea
                          className="mt-1.5 min-h-20 w-full rounded-control border border-input bg-surface px-3 py-2 text-sm leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          rows={3}
                          maxLength={1000}
                          value={reminderCardZh}
                          onChange={(event) => setReminderCardZh(event.target.value)}
                        />
                      </label>
                      <label className="block text-sm font-medium">
                        English text
                        <textarea
                          className="mt-1.5 min-h-20 w-full rounded-control border border-input bg-surface px-3 py-2 text-sm leading-relaxed placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                          rows={3}
                          maxLength={1000}
                          value={reminderCardEn}
                          onChange={(event) => setReminderCardEn(event.target.value)}
                        />
                      </label>
                      <div className="flex flex-wrap items-center gap-3">
                        <Button variant="outline" size="sm" onClick={() => void openReminderPreview()}>
                          用当前已保存文案预览
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void resetReminderCardText()}
                          disabled={reminderSaving}
                        >
                          恢复默认文案
                        </Button>
                        <span className="text-xs text-muted-foreground">
                          改完记得点上方「保存催交设置」；保存后下一次发送即生效。
                        </span>
                      </div>
                    </div>
                  )}
                </div>
                <div className="grid grid-cols-2 gap-3 self-start">
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">下次运行</p>
                    <p className="mt-1 text-sm font-semibold">{formatDateTime(reminder.data.next_run_at)}</p>
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">当前未提交</p>
                    <p className="mt-1 text-sm font-semibold tabular-nums">{reminder.data.current_target_count} 人</p>
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">最近运行</p>
                    <p className="mt-1 text-sm font-semibold">{formatDateTime(reminder.data.last_run_at)}</p>
                  </div>
                  <div className="tile">
                    <p className="text-xs text-muted-foreground">最近结果</p>
                    <p className="mt-1 text-sm font-semibold">
                      {reminder.data.last_status === "success"
                        ? `成功 ${reminder.data.last_target_count ?? 0} 人`
                        : reminder.data.last_status === "partial"
                          ? "部分未送达"
                          : reminder.data.last_status === "failed"
                            ? "发送失败"
                            : reminder.data.last_status === "skipped"
                              ? "无需提醒"
                              : reminder.data.last_status === "running"
                                ? "执行中"
                                : "尚无记录"}
                    </p>
                  </div>
                </div>
              </div>
              <div className="border-t border-border px-5 py-3">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium">跟进沟通的通知接收人</p>
                    <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                      负责人在「发给我的事项」里回复时，系统会通过企业微信通知到这些人。
                      <span className="ml-1 text-foreground">
                        参与过该会话的企微登录管理员会被自动通知，无需在此重复填写。
                      </span>
                      这里填写的是企微 userid（在企业微信通讯录里查看），多个用英文逗号分隔；
                      主要给用访问密码登录、没有员工身份的管理员指定接收账号。
                    </p>
                  </div>
                </div>
                <label className="mt-3 block text-sm font-medium">
                  接收人 userid
                  <Input
                    className="mt-1.5 max-w-xl"
                    value={actionNotifyUserids}
                    maxLength={2000}
                    onChange={(event) => setActionNotifyUserids(event.target.value)}
                    placeholder="例如：zhangsan,lisi（留空表示不额外指定）"
                  />
                </label>
                <p className="mt-1.5 text-xs text-muted-foreground">
                  留空且没有企微管理员参与会话时，回复会在消息中心提醒，但不会推送企业微信。
                  填错的 userid 会在发送结果里明确报出，不会静默丢失。
                </p>
              </div>
              <div className="border-t border-border px-5 py-3">
                <div className="flex flex-wrap items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium">免提交人员</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      当前 {exemptIds.size} 人；按部门展开勾选，保存后不进入统计、AI 计算和催交。
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      setExemptIds(
                        new Set(
                          employeeItems
                            .filter((employee) => employee.report_exempt)
                            .map((employee) => employee.id)
                        )
                      );
                      setExemptOpen(true);
                      setExemptNotice(null);
                    }}
                  >
                    <Users className="h-3.5 w-3.5" />
                    管理名单
                  </Button>
                </div>
              </div>
              {reminder.data.last_error && (
                <div
                  className={
                    reminder.data.last_status === "partial"
                      ? "border-t border-warning/30 bg-warning-soft px-5 py-3 text-xs leading-relaxed text-warning"
                      : "border-t border-destructive/30 bg-destructive-soft px-5 py-3 text-xs text-destructive"
                  }
                >
                  {reminder.data.last_status === "partial" ? "部分未送达：" : "最近错误："}
                  {reminder.data.last_error}
                </div>
              )}
              {reminderNotice && (
                <div
                  className={
                    reminderNotice.tone === "success"
                      ? "border-t border-success/30 bg-success-soft px-5 py-3 text-xs text-success"
                      : reminderNotice.tone === "error"
                        ? "border-t border-destructive/30 bg-destructive-soft px-5 py-3 text-xs text-destructive"
                        : "border-t border-border bg-muted/40 px-5 py-3 text-xs text-muted-foreground"
                  }
                >
                  {reminderNotice.text}
                </div>
              )}
            </>
          ) : null}
        </Card>
        )}

        {category === "access" && <AccessCard />}

        {category === "ai" && (
        <>
        <Card>
          <div className="card-head card-head-line">
            <ServerCog className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h2 className="text-base font-semibold">DeepSeek 模型</h2>
                <ServiceStatus
                  state={deepSeekState}
                  configured={data.deepseek.api_key_configured}
                />
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                支持 DeepSeek 与 OpenAI 兼容接口；模型可从上游检测，也可手工填写。
              </p>
            </div>
          </div>
          <div className="grid gap-4 p-5 md:grid-cols-2">
            <label className="text-sm font-medium">
              Base URL
              <Input
                className="mt-1.5"
                value={baseUrl}
                onChange={(event) => {
                  setBaseUrl(event.target.value);
                  setDeepSeekState("unknown");
                  setDetectedModels([]);
                  setModelHint(null);
                }}
                placeholder="https://api.deepseek.com"
              />
            </label>
            <div>
              <label className="text-sm font-medium" htmlFor="deepseek-model">
                模型
              </label>
              <div className="mt-1.5 flex gap-2">
                <Input
                  id="deepseek-model"
                  list="deepseek-model-options"
                  value={model}
                  onChange={(event) => setModel(event.target.value)}
                  placeholder="deepseek-chat"
                />
                <Button
                  className="h-9 px-3"
                  variant="outline"
                  size="sm"
                  onClick={() => void detectModels()}
                  loading={deepSeekState === "checking"}
                >
                  <RefreshCw className="h-3.5 w-3.5" />
                  检测模型
                </Button>
              </div>
              <datalist id="deepseek-model-options">
                {detectedModels.map((item) => (
                  <option key={item} value={item} />
                ))}
              </datalist>
              {detectedModels.length > 0 && (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {detectedModels.map((item) => (
                    <button
                      key={item}
                      type="button"
                      onClick={() => {
                        setModel(item);
                        setDeepSeekState("available");
                        setModelHint(`已选择上游模型：${item}`);
                      }}
                      className={
                        model === item
                          ? "rounded-full border border-success/30 bg-success-soft px-2.5 py-1 text-xs font-medium text-success"
                          : "rounded-full border border-border bg-surface px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/40 hover:text-primary"
                      }
                    >
                      {item}
                    </button>
                  ))}
                </div>
              )}
              <p className="mt-1.5 min-h-4 text-xs leading-relaxed text-muted-foreground">
                {modelHint ?? "点击「检测模型」可从上游获取模型列表。"}
              </p>
            </div>
            <div className="md:col-span-2">
              <SecretEditor
                label="API Key"
                configured={data.deepseek.api_key_configured}
                editing={editingDeepSeekKey}
                value={deepSeekKey}
                clear={clearDeepSeekKey}
                placeholder="输入新的 DeepSeek API Key"
                onEdit={() => {
                  setEditingDeepSeekKey(true);
                  setClearDeepSeekKey(false);
                }}
                onChange={(value) => {
                  setDeepSeekKey(value);
                  setClearDeepSeekKey(false);
                  setDeepSeekState("unknown");
                }}
                onCancel={() => {
                  setEditingDeepSeekKey(false);
                  setDeepSeekKey("");
                }}
                onDelete={() => {
                  setClearDeepSeekKey(true);
                  setEditingDeepSeekKey(false);
                  setDeepSeekKey("");
                }}
                onUndoDelete={() => setClearDeepSeekKey(false)}
              />
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3">
            <span className="text-xs text-muted-foreground">
              测试会真实调用一次模型，可能产生少量 token 费用。
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void testConnection("deepseek")}
              loading={deepSeekState === "checking"}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              测试 DeepSeek
            </Button>
          </div>
        </Card>

        <Card>
          <div className="card-head card-head-line">
            <Languages className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h2 className="text-base font-semibold">百度大模型翻译</h2>
                <ServiceStatus
                  state={baiduState}
                  configured={data.baidu_translation.configured}
                />
              </div>
              <p className="mt-1 text-xs text-muted-foreground">
                优先使用 API Key；未填写 API Key 时使用 AppID + Secret Key 签名鉴权。
              </p>
            </div>
          </div>
          <div className="grid gap-4 p-5 md:grid-cols-2">
            <label className="text-sm font-medium">
              AppID
              <Input
                className="mt-1.5"
                value={appId}
                onChange={(event) => setAppId(event.target.value)}
                placeholder="百度翻译 AppID"
              />
            </label>
            <div className="flex items-end">
              <p className="pb-2 text-xs leading-relaxed text-muted-foreground">
                单篇周报点击「翻译为中文」时才会调用，不参与后台批量任务。
              </p>
            </div>
            <SecretEditor
              label="API Key"
              configured={data.baidu_translation.api_key_configured}
              editing={editingBaiduApiKey}
              value={baiduApiKey}
              clear={clearBaiduApiKey}
              placeholder="输入新的百度 API Key"
              onEdit={() => {
                setEditingBaiduApiKey(true);
                setClearBaiduApiKey(false);
              }}
              onChange={(value) => {
                setBaiduApiKey(value);
                setClearBaiduApiKey(false);
                setBaiduState("unknown");
              }}
              onCancel={() => {
                setEditingBaiduApiKey(false);
                setBaiduApiKey("");
              }}
              onDelete={() => {
                setClearBaiduApiKey(true);
                setEditingBaiduApiKey(false);
                setBaiduApiKey("");
              }}
              onUndoDelete={() => setClearBaiduApiKey(false)}
            />
            <SecretEditor
              label="Secret Key（兼容签名）"
              configured={data.baidu_translation.secret_key_configured}
              editing={editingBaiduSecretKey}
              value={baiduSecretKey}
              clear={clearBaiduSecretKey}
              placeholder="输入新的百度 Secret Key"
              onEdit={() => {
                setEditingBaiduSecretKey(true);
                setClearBaiduSecretKey(false);
              }}
              onChange={(value) => {
                setBaiduSecretKey(value);
                setClearBaiduSecretKey(false);
                setBaiduState("unknown");
              }}
              onCancel={() => {
                setEditingBaiduSecretKey(false);
                setBaiduSecretKey("");
              }}
              onDelete={() => {
                setClearBaiduSecretKey(true);
                setEditingBaiduSecretKey(false);
                setBaiduSecretKey("");
              }}
              onUndoDelete={() => setClearBaiduSecretKey(false)}
            />
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3 border-t border-border px-5 py-3">
            <span className="text-xs text-muted-foreground">
              为避免自动产生翻译费用，页面加载时不会自动测试；手动测试成功后才显示绿点。
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void testConnection("baidu_translation")}
              loading={baiduState === "checking"}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              测试百度翻译
            </Button>
          </div>
        </Card>
        </>
        )}

        {category === "ai" && (
        <Card>
          <div className="card-head card-head-line">
            <Bot className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <h2 className="text-base font-semibold">语义检索（向量，可选）</h2>
              <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                配置后，AI 问答在「不限关键词」浏览时按语义相关度排序周报，员工写了
                同义表达、英文周报不再漏检。三项全部填写才启用；清空任意一项即停用。
                保存后需运行向量回填脚本为存量周报生成索引。
              </p>
            </div>
            {data.embedding.configured ? (
              <StatusDot tone="green">已启用</StatusDot>
            ) : (
              <StatusDot tone="neutral">未启用</StatusDot>
            )}
          </div>
          <div className="space-y-4 p-5">
            <label className="block">
              <span className="text-sm font-medium">服务地址（OpenAI 兼容 /embeddings）</span>
              <Input
                className="mt-1.5"
                value={embeddingBaseUrl}
                onChange={(event) => setEmbeddingBaseUrl(event.target.value)}
                placeholder="例如 https://api.siliconflow.cn/v1"
              />
            </label>
            <label className="block">
              <span className="text-sm font-medium">向量模型</span>
              <Input
                className="mt-1.5"
                value={embeddingModel}
                onChange={(event) => setEmbeddingModel(event.target.value)}
                placeholder="例如 BAAI/bge-m3"
              />
            </label>
            <SecretEditor
              label="API Key"
              configured={data.embedding.api_key_configured}
              editing={editingEmbeddingKey}
              value={embeddingKey}
              clear={clearEmbeddingKey}
              placeholder="输入新的 Embedding API Key"
              onEdit={() => {
                setEditingEmbeddingKey(true);
                setClearEmbeddingKey(false);
              }}
              onChange={(value) => {
                setEmbeddingKey(value);
                setClearEmbeddingKey(false);
              }}
              onCancel={() => {
                setEditingEmbeddingKey(false);
                setEmbeddingKey("");
              }}
              onDelete={() => {
                setClearEmbeddingKey(true);
                setEditingEmbeddingKey(false);
                setEmbeddingKey("");
              }}
              onUndoDelete={() => setClearEmbeddingKey(false)}
            />
            <p className="text-xs leading-relaxed text-muted-foreground">
              出站主机必须在部署侧 ai_embedding_allowed_hosts 白名单内，未配置白名单时
              即使填了这三项也不会启用——这是防止密钥被发往未知主机的安全闸门。
            </p>
          </div>
        </Card>
        )}

        {category === "theme" && (
        <Card>
          <div className="card-head card-head-line">
            <Palette className="mt-0.5 h-5 w-5 text-primary" />
            <div className="min-w-0 flex-1">
              <h2 className="text-base font-semibold">界面调色盘</h2>
              <p className="mt-1 text-xs text-muted-foreground">
                先实时预览，再保存到后台；刷新页面或换浏览器后仍会保留已保存的配色。
              </p>
            </div>
          </div>
          <div className="space-y-5 p-5">
            <div>
              <p className="text-sm font-medium">快速套用</p>
              <div className="mt-2 flex flex-wrap gap-2">
                {THEME_PRESETS.map((preset) => (
                  <button
                    key={preset.key}
                    type="button"
                    onClick={() => applyThemePreset(preset.theme)}
                    className="inline-flex items-center gap-2 rounded-pill border border-border bg-surface px-3 py-2 text-xs transition-colors hover:border-primary/35 hover:bg-muted"
                    title={preset.description}
                  >
                    <span className="flex items-center gap-0.5" aria-hidden="true">
                      <span
                        className="h-3.5 w-3.5 rounded-full"
                        style={{ backgroundColor: preset.theme.primary }}
                      />
                      <span
                        className="-ml-1 h-3.5 w-3.5 rounded-full border border-surface"
                        style={{ backgroundColor: preset.theme.accent }}
                      />
                    </span>
                    {preset.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              {THEME_COLOR_FIELDS.map((field) => {
                const value = themeDraft[field.key];
                const colorInputValue = isValidHexColor(value)
                  ? value
                  : DEFAULT_THEME[field.key];
                return (
                  <label key={field.key} className="text-sm font-medium">
                    {field.label}
                    <div className="mt-1.5 flex gap-2">
                      <input
                        type="color"
                        value={colorInputValue}
                        onChange={(event) => updateThemeColor(field.key, event.target.value)}
                        className="h-9 w-11 shrink-0 cursor-pointer rounded-control border border-input bg-surface p-1"
                        aria-label={`${field.label}颜色选择器`}
                      />
                      <Input
                        value={value}
                        maxLength={7}
                        onChange={(event) => updateThemeColor(field.key, event.target.value)}
                        placeholder="#1A1C28"
                        aria-label={`${field.label}十六进制值`}
                      />
                    </div>
                    <span className="mt-1 block text-xs font-normal text-muted-foreground">
                      {field.description}
                    </span>
                  </label>
                );
              })}
            </div>

            <div className="rounded-card border border-border bg-background/60 p-4">
              <p className="text-xs font-medium text-muted-foreground">实时预览</p>
              <div
                className="mt-2 flex flex-wrap items-center justify-between gap-3 rounded-control border border-border/70 p-3"
                style={{ backgroundColor: themeDraft.background }}
              >
                <div className="flex items-center gap-2">
                  <span
                    className="flex h-8 w-8 items-center justify-center rounded-control text-xs font-semibold"
                    style={{
                      backgroundColor: themeDraft.nav,
                      color: contrastColor(themeDraft.nav),
                    }}
                  >
                    周
                  </span>
                  <div>
                    <p className="text-sm font-semibold">周报平台</p>
                    <p className="text-xs text-muted-foreground">自定义主题预览</p>
                  </div>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    className="rounded-pill px-3 py-1.5 text-xs font-medium"
                    style={{
                      backgroundColor: themeDraft.primary,
                      color: contrastColor(themeDraft.primary),
                    }}
                  >
                    主色按钮
                  </span>
                  <span
                    className="rounded-pill px-3 py-1.5 text-xs font-medium"
                    style={{
                      backgroundColor: themeDraft.accent,
                      color: contrastColor(themeDraft.accent),
                    }}
                  >
                    强调数据
                  </span>
                </div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <Button
                onClick={() => void saveTheme()}
                loading={themeSaving}
                disabled={data.encryption.production_key_required || !themeDirty}
              >
                <Save className="h-3.5 w-3.5" />
                保存主题
              </Button>
              <Button variant="outline" onClick={resetThemePreview} disabled={themeSaving}>
                <RefreshCw className="h-3.5 w-3.5" />
                恢复原始配色
              </Button>
              {themeNotice && (
                <span
                  className={
                    themeNotice.tone === "success"
                      ? "text-xs text-success"
                      : themeNotice.tone === "error"
                        ? "text-xs text-destructive"
                        : "text-xs text-muted-foreground"
                  }
                >
                  {themeNotice.text}
                </span>
              )}
            </div>
          </div>
        </Card>
        )}
        </div>
      </div>

      <Modal
        open={reminderPreviewOpen}
        title="催交预览"
        onClose={() => setReminderPreviewOpen(false)}
        footer={
          reminderPreview?.sent ? (
            <Button onClick={() => setReminderPreviewOpen(false)}>关闭</Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => setReminderPreviewOpen(false)}>
                取消
              </Button>
              <Button
                onClick={() => void sendReminderNow()}
                loading={reminderPreviewLoading}
                disabled={!reminderPreview || reminderPreview.target_count === 0}
              >
                确认发送（{reminderPreview?.target_count ?? 0} 人）
              </Button>
            </>
          )
        }
      >
        {reminderPreviewLoading && !reminderPreview ? (
          <LoadingBlock rows={3} />
        ) : reminderPreview ? (
          <div className="space-y-3">
            <div className="rounded-control bg-warning-soft px-3 py-2 text-sm leading-relaxed text-warning">
              本次将处理 <b>{reminderPreview.target_count}</b> 人
              {reminderPreview.period && `，周期 ${reminderPreview.period.start} ~ ${reminderPreview.period.end}`}。
              {!reminderPreview.sent && " 点击确认后会真实发送企微消息。"}
            </div>
            {reminderPreview.sent && reminderPreview.result && (
              <div
                className={
                  (reminderPreview.result.undelivered?.length ?? 0) > 0
                    ? "rounded-control bg-warning-soft px-3 py-2 text-sm leading-relaxed text-warning"
                    : "rounded-control bg-success-soft px-3 py-2 text-sm text-success"
                }
              >
                企微实际送达 <b>{reminderPreview.result.delivered_count ?? 0}</b> 人。
                {(reminderPreview.result.undelivered?.length ?? 0) > 0 && (
                  <>
                    {" "}
                    另有 {reminderPreview.result.undelivered?.length} 人未被企微投递，
                    需要检查自建应用的可见范围（见下方名单）。
                  </>
                )}
              </div>
            )}
            {reminderPreview.targets.length > 0 && (
              <div className="rounded-control border border-dashed border-border px-4 py-3">
                <p className="text-xs font-medium text-muted-foreground">提醒对象</p>
                <p className="mt-2 text-sm leading-6">
                  {reminderPreview.targets.map((target) => target.name).join("、")}
                </p>
              </div>
            )}
            <div className="wechat-card-preview rounded-card bg-wechat-bg p-4">
              <p className="text-sm font-semibold text-wechat-title">{reminderPreview.card.title}</p>
              <div
                className="mt-1.5 text-sm leading-relaxed text-wechat-text"
                dangerouslySetInnerHTML={{ __html: reminderPreview.card.description }}
              />
              <div className="mt-3 inline-block rounded-control bg-wechat-green px-3 py-1 text-xs font-medium text-white">
                {reminderPreview.card.btn_text}
              </div>
            </div>
            {reminderPreview.skipped.length > 0 && (
              <div className="rounded-control border border-dashed border-border px-4 py-3">
                <p className="text-xs font-medium text-muted-foreground">以下人员本次跳过</p>
                <ul className="mt-2 space-y-1 text-xs text-muted-foreground">
                  {reminderPreview.skipped.map((item) => (
                    <li key={`${item.id}-${item.reason}`}>{item.name}：{item.reason}</li>
                  ))}
                </ul>
              </div>
            )}
            {reminderPreview.sent && !reminderPreview.error && (
              <div className="rounded-control bg-success-soft px-3 py-2 text-sm text-success">
                已提交企微发送请求，成功记录已写入催交日志。
              </div>
            )}
            {reminderPreview.error && (
              <div
                className={
                  reminderPreview.sent
                    ? "rounded-control bg-warning-soft px-3 py-2 text-sm leading-relaxed text-warning"
                    : "rounded-control bg-destructive-soft px-3 py-2 text-sm text-destructive"
                }
              >
                {reminderPreview.error}
              </div>
            )}
          </div>
        ) : null}
      </Modal>

      <Modal
        open={exemptOpen}
        title="选择免提交人员"
        size="lg"
        onClose={closeExemptions}
        footer={
          <div className="flex w-full flex-wrap items-center justify-between gap-3">
            <div className="flex flex-wrap items-center gap-3 text-xs">
              <span className="text-muted-foreground">已选 {exemptIds.size} 人</span>
              {exemptNotice && (
                <span
                  className={
                    exemptNotice.tone === "error"
                      ? "text-destructive"
                      : exemptNotice.tone === "success"
                        ? "text-success"
                        : "text-muted-foreground"
                  }
                >
                  {exemptNotice.text}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" onClick={closeExemptions} disabled={exemptSaving}>
                {exemptDirty ? "取消" : "关闭"}
              </Button>
              <Button
                onClick={() => void saveExemptions()}
                loading={exemptSaving}
                disabled={
                  !exemptDirty ||
                  employees.loading ||
                  departments.loading ||
                  employeeItems.length === 0
                }
              >
                <Save className="h-3.5 w-3.5" />
                保存名单
              </Button>
            </div>
          </div>
        }
      >
        {((employees.loading && !employees.data) || (departments.loading && !departments.data)) ? (
          <LoadingBlock rows={6} />
        ) : employees.error || departments.error ? (
          <ErrorState
            message={employees.error ?? departments.error ?? "免提交名单加载失败"}
            onRetry={() => {
              employees.reload();
              departments.reload();
            }}
          />
        ) : (
          <ReportExemptionTree
            departments={departments.data ?? []}
            employees={employeeItems}
            selectedIds={exemptIds}
            onSelectionChange={updateExemptSelection}
          />
        )}
      </Modal>
    </div>
  );
}
