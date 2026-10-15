// No frontend test dependency: execute the real TSX with TypeScript and React SSR.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const frontend = path.resolve(__dirname, "..");
const resolveFilename = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  return resolveFilename.call(this, request.startsWith("@/") ? path.join(frontend, request.slice(2)) : request, parent, ...rest);
};
for (const extension of [".ts", ".tsx"]) {
  Module._extensions[extension] = (module, filename) => {
    const output = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
    });
    module._compile(output.outputText, filename);
  };
}
const sections = require("../app/operating-dashboard/monthly-sections.tsx");
const range = { start: "2026-10-01", end: "2026-10-31" };
function issue(title, overrides = {}) {
  return { title, source_report_ids: [1, 2, 3], aliases: [], corroboration: [], blocker_id: null, ...overrides };
}
function payload(overrides = {}) {
  return {
    summary: { analysis_ready: true, status: "success", stale: false }, narrative: { headline: "已生成" },
    interventions: { items: [] }, shared_issues: [], stuck: { items: [] }, anomalies: { items: [] }, ...overrides,
  };
}
function render(name, data) {
  return renderToStaticMarkup(React.createElement(sections[name], { payload: data, range }));
}

test("a successful brief that only wrote decisions still shows sourced problem candidates", () => {
  const data = payload({
    interventions: { items: [issue("机构签约", { ai_text: "签约条件待明确。", must_decide: "是否调整条件" })] },
    shared_issues: [issue("培训资料缺失")],
    stuck: { items: [issue("支付联调", { kind: "blocker", detail: "接口参数尚未确认。" })] },
    anomalies: { items: [issue("网站记录冲突", { kind: "status_conflict" })] },
  });
  assert.match(render("MonthlyInterventions", data), /签约条件待明确/);
  for (const [name, title] of [["MonthlySharedIssues", "培训资料缺失"], ["MonthlyStuck", "支付联调"], ["MonthlyAnomalies", "网站记录冲突"]]) {
    const html = render(name, data);
    assert.ok(html.includes(title));
    assert.match(html, /待核查/);
    assert.ok(html.includes("/reports/1?start=2026-10-01&amp;end=2026-10-31"));
  }
  assert.match(render("MonthlyStuck", data), /周报记录：接口参数尚未确认/);
});

test("known aliases appear once across intervention, shared, and stuck sections", () => {
  const data = payload({
    interventions: { items: [issue("院校沟通", { ai_text: "校方尚未回复。", must_decide: "是否调整申请方案" })] },
    shared_issues: [issue("院校沟通", { aliases: ["校方反馈"] })],
    stuck: { items: [issue("校方反馈", { kind: "dependency" })] },
  });
  assert.match(render("MonthlyInterventions", data), /校方尚未回复/);
  assert.doesNotMatch(render("MonthlySharedIssues", data), /查看来源/);
  assert.doesNotMatch(render("MonthlyStuck", data), /查看来源/);
  assert.match(render("MonthlyStuck", data), /已列入上方分类/);
});

test("distinct issues sharing the exact same reports are not hidden", () => {
  const data = payload({
    interventions: { items: [issue("代理签约", { ai_text: "合同待签。", must_decide: "是否签约" })] },
    stuck: { items: [issue("学生翻译材料", { kind: "blocker", ai_text: "翻译件未补齐。" })] },
    anomalies: { items: [issue("网站状态冲突", { kind: "status_conflict", ai_text: "两条记录状态不同。" })] },
  });
  assert.match(render("MonthlyStuck", data), /翻译件未补齐/);
  assert.match(render("MonthlyAnomalies", data), /两条记录状态不同/);
});

test("all four anomaly kinds remain available when independently sourced", () => {
  const titles = ["周期增长", "多人反馈", "重复推进", "记录冲突"];
  const kinds = ["department_growth", "repeated_issue", "duplicate_effort", "status_conflict"];
  const html = render("MonthlyAnomalies", payload({ anomalies: { items: titles.map((title, i) => issue(title, { kind: kinds[i] })) } }));
  for (const title of titles) assert.ok(html.includes(title));
  assert.match(html, /<details/);
  assert.match(html, /查看其余 1 条/);
});

test("source-free claims are not displayed as problem facts", () => {
  const data = payload({ stuck: { items: [issue("无来源结论", { ai_text: "模型猜测", source_report_ids: [] })] } });
  const html = render("MonthlyStuck", data);
  assert.doesNotMatch(html, /无来源结论|模型猜测|查看来源/);
  assert.match(html, /本期未识别到有来源支持的卡点线索/);
});

test("unfinished and stale analysis do not claim the period has no problems", () => {
  for (const state of [{ analysis_ready: false, status: "pending" }, { analysis_ready: true, status: "success", stale: true }]) {
    const html = render("MonthlyStuck", payload({ summary: state }));
    assert.match(html, /尚未就绪，暂不能判断/);
    assert.doesNotMatch(html, /本期未识别到/);
  }
});

test("sourced candidates without a decision are not presented as management decisions", () => {
  const html = render("MonthlyInterventions", payload({ interventions: { items: [issue("未决线索", { ai_text: "尚待核查。" })] } }));
  assert.doesNotMatch(html, /需决定：|查看来源/);
  assert.match(html, /暂无已明确的管理层决策/);
});

test("long lists remain concise and all remaining facts can be expanded", () => {
  const items = Array.from({ length: 7 }, (_, i) => issue(`独立卡点${i}`, { kind: "blocker", source_report_ids: [i + 1] }));
  const html = render("MonthlyStuck", payload({ stuck: { items } }));
  const [visible, expandable] = html.split("<details");
  assert.match(visible, /独立卡点3/);
  assert.doesNotMatch(visible, /独立卡点4/);
  assert.match(expandable, /查看其余 3 条/);
  for (const item of items) assert.equal(html.split(item.title).length - 1, 1);
});
