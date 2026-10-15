"use client";

export type TestTarget = "deepseek" | "baidu_translation";
export type CheckState =
  | "unknown"
  | "checking"
  | "available"
  | "attention"
  | "model_list_unavailable"
  | "failed";
export type Notice = { text: string; tone: "info" | "success" | "error" } | null;
