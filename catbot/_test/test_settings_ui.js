/* 设置台界面自测（在真 DOM 里跑，不是纸面检查）
 *
 *   node catbot/_test/test_settings_ui.js
 *
 * 为什么要有这条：设置台是纯前端交互（目录、折叠、搜索、改动标记），
 * Python 侧的 test_settings.py 只验到"占位符有没有被替换"这一层，
 * 界面逻辑写错了它照样全绿。这里用 jsdom 把预览页真跑起来，
 * 断言用户点下去会发生什么。
 *
 * 前置：
 *   1. 装 jsdom：npm install jsdom（在项目根，或用下面的环境变量指到已有位置）
 *   2. 生成预览：python catbot/settings_page.py --snapshot
 *
 * 注意：render() 会重建分组卡元素，所以每次都重新 query，别缓存节点。
 */
"use strict";
const fs = require("fs");
const path = require("path");

// jsdom 装在哪：优先项目自带的 node_modules，其次环境变量指定的工作区
const ROOT = path.resolve(__dirname, "..", "..");
function resolveJsdom() {
  const tries = [
    path.join(ROOT, "node_modules", "jsdom"),
    process.env.WB_NODE_WORKSPACE
      && path.join(process.env.WB_NODE_WORKSPACE, "node_modules", "jsdom"),
  ].filter(Boolean);
  for (const p of tries) {
    if (fs.existsSync(p)) return p;
  }
  console.error("找不到 jsdom。请先 npm install jsdom（或设 WB_NODE_WORKSPACE）");
  process.exit(2);
}
const { JSDOM } = require(resolveJsdom());

const FILE = path.join(ROOT, "设置页-预览.html");

let pass = 0, fail = 0;
function ck(name, cond, extra){
  if (cond){ pass++; console.log("  [PASS] " + name); }
  else { fail++; console.log("  [FAIL] " + name + (extra !== undefined ? "  <- " + JSON.stringify(extra) : "")); }
}
function log(){
  console.log(Array.prototype.join.call(arguments, " "));
}

if (!fs.existsSync(FILE)){
  console.log("找不到 " + FILE + "，先跑：python settings_page.py --snapshot");
  process.exit(1);
}

const html = fs.readFileSync(FILE, "utf8");
const dom = new JSDOM(html, {
  runScripts: "dangerously",
  pretendToBeVisual: true,
  url: "http://127.0.0.1/preview/",
  beforeParse(w){
    w.Element.prototype.scrollIntoView = function(){};   // jsdom 没实现
  },
});
const win = dom.window;
const doc = win.document;
const $  = s => doc.querySelector(s);
const $$ = s => Array.prototype.slice.call(doc.querySelectorAll(s));
const cards = () => $$("section.card");                 // 每次都重查：render 会重建
const navs  = () => $$(".navitem");
const rows  = () => $$("#groups tr[data-path]");
const visibleRows = () => rows().filter(tr => !tr.classList.contains("hide"));
const visibleCards = () => cards().filter(c => c.style.display !== "none");
const cardOf = gid => doc.getElementById("g-" + gid);

function fire(node, type){ node.dispatchEvent(new win.Event(type, { bubbles: true })); }
function setSearch(v){ const q = $("#q"); q.value = v; fire(q, "input"); }
function setOnlyDirty(v){ const c = $("#onlyDirty"); c.checked = v; fire(c, "change"); }
function typeInto(node, v){ node.value = v; fire(node, "input"); }

log("=".repeat(64));
log("  设置台界面自测（真 DOM）");
log("=".repeat(64));

/* ---------- 1. 结构 ---------- */
log("\n[1] 目录与分组");
ck("侧栏目录 14 项", navs().length === 14, navs().length);
ck("分组卡 14 张", cards().length === 14, cards().length);
ck("每个目录项都指向一张存在的卡",
   navs().every(a => !!cardOf(a.dataset.g)));
ck("目录顺序与分组顺序一致",
   navs().map(a => a.dataset.g).join(",") === cards().map(c => c.id.slice(2)).join(","));
ck("目录项都带标题文字", navs().every(a => a.querySelector(".nt").textContent.trim().length > 0));
ck("每张卡都有可点的标题", cards().every(c => !!c.querySelector("h2")));
ck("标题带 aria-expanded", cards().every(c => c.querySelector("h2").getAttribute("aria-expanded") !== null));

/* ---------- 2. 项数统计 ---------- */
log("\n[2] 项数统计（目录 / 分组 / 总计要自洽）");
function counts(){
  return cards().map(c => c.querySelector(".cnt").textContent.trim());
}
ck("每组都写了 n / m 项", counts().every(t => /^\d+ \/ \d+ 项/.test(t)), counts());
const perTotal = counts().map(t => parseInt(t.match(/^(\d+) \/ (\d+)/)[2], 10));
ck("默认视图下 n == m（没有筛选）",
   counts().map(t => t.match(/^(\d+) \/ (\d+)/)[1]).join() === perTotal.join(), counts());
const totalRows0 = rows().length;
ck("页面里的行数 == 各组自称的项数之和",
   totalRows0 === perTotal.reduce((a, b) => a + b, 0), [totalRows0, perTotal.reduce((a, b) => a + b, 0)]);
const hitsTxt = $("#hits").textContent.trim();
ck("底部统计写了「共 N 项 · 14 组」",
   /^共 \d+ 项 · 14 组$/.test(hitsTxt) && parseInt(hitsTxt.match(/\d+/)[0], 10) === totalRows0, hitsTxt);
ck("目录头写了组数",
   /14 组 · \d+ 项/.test($("#navTotal").textContent), $("#navTotal").textContent);

/* ---------- 3. 默认折叠 ---------- */
log("\n[3] 默认折叠（前两组展开，其余收起）");
const open = cards().filter(c => !c.classList.contains("collapsed"));
ck("默认只展开前两组", open.length === 2 && open[0].id === "g-llm" && open[1].id === "g-reply",
   open.map(c => c.id));
ck("折叠按钮初始写着「全部收起」", $("#btnFold").textContent === "全部收起", $("#btnFold").textContent);

log("\n[4] 点标题展开 / 收起");
const g5id = cards()[4].id.slice(2);
cardOf(g5id).querySelector("h2").click();
ck("点一下 -> 展开", !cardOf(g5id).classList.contains("collapsed"));
ck("展开状态写进了 aria", cardOf(g5id).querySelector("h2").getAttribute("aria-expanded") === "true");
cardOf(g5id).querySelector("h2").click();
ck("再点一下 -> 收起", cardOf(g5id).classList.contains("collapsed"));
ck("折叠状态记进了 localStorage",
   JSON.parse(win.localStorage.getItem("xy.settings.ui.v1")).collapsed[g5id] === true,
   win.localStorage.getItem("xy.settings.ui.v1"));

log("\n[5] 全部展开 / 全部收起");
$("#btnFold").click();
ck("点「全部收起」后全折叠", cards().every(c => c.classList.contains("collapsed")));
ck("按钮文案翻转成「全部展开」", $("#btnFold").textContent === "全部展开", $("#btnFold").textContent);
$("#btnFold").click();
ck("再点后全部展开", cards().every(c => !c.classList.contains("collapsed")));
const totalRows = rows().length;

/* ---------- 4. 搜索 ---------- */
log("\n[6] 搜索：过滤 + 自动展开");
setSearch("语音");
const vr = visibleRows();
ck("搜到内容了", vr.length > 0, vr.length);
ck("留着的行都真的含关键词", vr.every(tr => tr.textContent.indexOf("语音") >= 0));
ck("命中数写进了底部统计", /^匹配 \d+ 项$/.test($("#hits").textContent.trim()), $("#hits").textContent);
ck("匹配数与实际可见行数一致",
   parseInt($("#hits").textContent.match(/\d+/)[0], 10) === vr.length, [$("#hits").textContent, vr.length]);
const sc = visibleCards();
ck("没命中的组整张藏起来", sc.length > 0 && sc.length < 14, sc.length);
ck("有命中的组即便之前折叠着，搜索时也展开", sc.every(c => !c.classList.contains("collapsed")));
ck("目录里没命中的组也一起藏", navs().filter(a => a.style.display === "none").length === 14 - sc.length,
   [navs().filter(a => a.style.display === "none").length, 14 - sc.length]);
ck("分组标题的计数跟着筛选走",
   sc.every(c => c.querySelector(".cnt").textContent.trim().indexOf(" / ") > 0));

log("\n[7] 搜索：没结果");
setSearch("zzzz-不存在的东西");
ck("出现空态提示", $$(".empty").length === 1, $$(".empty").length);
ck("统计显示匹配 0 项", $("#hits").textContent.trim() === "匹配 0 项", $("#hits").textContent);
setSearch("");
ck("清空后恢复全部行", visibleRows().length === totalRows);
ck("清空后回到「共 N 项」", /^共 \d+ 项/.test($("#hits").textContent.trim()), $("#hits").textContent);
ck("清空后空态消失", $$(".empty").length === 0);

/* ---------- 5. 改动标记 ---------- */
log("\n[8] 改动标记（含折叠组上的徽标）");
const gid = "reply";
const badgeOf = g => navs().filter(a => a.dataset.g === g)[0].querySelector(".nc");
typeInto(cardOf(gid).querySelector('tr[data-path="reply.max_chars"] input[type=number]'), "333");
const trow = cardOf(gid).querySelector('tr[data-path="reply.max_chars"]');
ck("行被标成 dirty", trow.classList.contains("dirty"));
ck("底部条显示 1 项改动", /1<\/b>\s*项改动/.test($("#saveMsg").innerHTML), $("#saveMsg").innerHTML);
ck("保存按钮写着 1 项", $("#btnSave").textContent === "保存 1 项", $("#btnSave").textContent);
ck("底部保存条升起", $("#savebar").classList.contains("up"));
ck("分组标题出现「改 1」", /改 1/.test(cardOf(gid).querySelector(".cnt").innerHTML),
   cardOf(gid).querySelector(".cnt").innerHTML);
ck("目录徽标显示「改1」", badgeOf(gid).textContent === "改1" && badgeOf(gid).className.indexOf("hit") >= 0,
   [badgeOf(gid).textContent, badgeOf(gid).className]);

log("\n[9] 还原按钮真的能点");
cardOf(gid).querySelector(".rev").click();
ck("点还原后脏标记消失",
   !cardOf(gid).querySelector('tr[data-path="reply.max_chars"]').classList.contains("dirty"));
ck("改动数归零", $("#saveMsg").innerHTML.indexOf("没有改动") >= 0, $("#saveMsg").innerHTML);
ck("目录徽标回到项数", badgeOf(gid).textContent ===
   String(cardOf(gid).querySelectorAll("tr[data-path]").length), badgeOf(gid).textContent);

log("\n[9b] 筛选状态下点还原");
typeInto(cardOf(gid).querySelector('tr[data-path="reply.max_chars"] input[type=number]'), "444");
setOnlyDirty(true);
ck("筛选后只剩这一条", visibleRows().length === 1, visibleRows().length);
cardOf(gid).querySelector(".rev").click();
ck("还原后那一行从「只看改动」里消失", visibleRows().length === 0, visibleRows().length);
ck("出现空态", $$(".empty").length === 1);
setOnlyDirty(false);

/* ---------- 6. 只看改动 ---------- */
log("\n[10] 只看改动");
const t2 = cardOf("llm").querySelector('tr[data-path="llm.temperature"]');
typeInto(t2.querySelector("input[type=number]"), "1.2");
setOnlyDirty(true);
const v2 = visibleRows();
ck("只剩一条可见", v2.length === 1, v2.length);
ck("剩下的正是改过那条", v2[0] && v2[0].dataset.path === "llm.temperature", v2[0] && v2[0].dataset.path);
ck("统计显示匹配 1 项", $("#hits").textContent.trim() === "匹配 1 项", $("#hits").textContent);
ck("筛选时折叠组也自动展开", cards().every(c => !c.classList.contains("collapsed")));
setOnlyDirty(false);
ck("取消后恢复全部", visibleRows().length === totalRows);

/* ---------- 7. 键盘 ---------- */
log("\n[11] 键盘快捷键");
doc.dispatchEvent(new win.KeyboardEvent("keydown", { key: "/", bubbles: true }));
ck("按 / 聚焦到搜索框", doc.activeElement === $("#q"), doc.activeElement && doc.activeElement.id);
setSearch("记忆");
doc.dispatchEvent(new win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
ck("按 Esc 清空搜索", $("#q").value === "" && visibleRows().length === totalRows);

/* ---------- 8. 目录跳转 ---------- */
log("\n[12] 点目录跳转");
const target = navs().filter(a => a.dataset.g === "conn")[0];
target.click();
ck("跳过去的组被展开", !cardOf("conn").classList.contains("collapsed"));
ck("目录高亮跟着走", target.classList.contains("on"),
   navs().filter(a => a.classList.contains("on")).map(a => a.dataset.g));

/* ---------- 8b. 隐藏高级 ---------- */
log("\n[12b] 隐藏高级（少看一半基础设施项）");
const advRows = rows().filter(tr => tr.querySelector(".tag.adv"));
ck("高级项确实存在（不然这个开关没意义）", advRows.length > 0, advRows.length);
ck("默认不隐藏", visibleRows().length === totalRows);
const hid = $("#hideAdv");
hid.checked = true;
fire(hid, "change");
ck("勾上后高级项消失", visibleRows().length === totalRows - advRows.length,
   [visibleRows().length, totalRows - advRows.length]);
ck("露脸的一个高级项都没有", visibleRows().every(tr => !tr.querySelector(".tag.adv")));
ck("统计里写明已隐藏", /已隐藏高级/.test($("#hits").textContent), $("#hits").textContent);
ck("分组计数跟着减", counts().every(t => {
  const m = t.match(/^(\d+) \/ (\d+)/);
  return parseInt(m[1], 10) <= parseInt(m[2], 10);
}));
ck("选择记进了 localStorage",
   JSON.parse(win.localStorage.getItem("xy.settings.ui.v1")).hideAdv === true,
   win.localStorage.getItem("xy.settings.ui.v1"));
hid.checked = false;
fire(hid, "change");
ck("取消后恢复全部", visibleRows().length === totalRows);
ck("统计回到「共 N 项」", /^共 \d+ 项/.test($("#hits").textContent), $("#hits").textContent);

/* ---------- 9. 页面完整性 ---------- */
log("\n[13] 预览页真的带着数据（不是一张空表）");
const mt = cardOf("llm").querySelector('tr[data-path="llm.max_tokens"] input[type=number]').value;
ck("数字控件填的是真实值", /^\d+$/.test(mt), mt);
const temp = cardOf("llm").querySelector('tr[data-path="llm.temperature"] input[type=number]').value;
ck("浮点控件带上当前值", Number(temp) > 0, temp);
ck("下拉框选中了当前项",
   cardOf("llm").querySelector('tr[data-path="llm.provider"] select').value.length > 0);
const sw = cardOf("trigger").querySelector('tr[data-path="trigger.group_at"] input[type=checkbox]');
ck("开关反映当前配置（布尔控件有明确开/关）", typeof sw.checked === "boolean");
ck("列表控件填了内容",
   cardOf("voice").querySelector('tr[data-path="voice.speak_on"] textarea').value.indexOf("晚安") >= 0);
ck("改一个值能算出「有改动」（说明基线是真值，不是空）", (() => {
  const t = cardOf("llm").querySelector('tr[data-path="llm.max_tokens"] input[type=number]');
  const old = t.value;
  typeInto(t, String(Number(old) + 20));
  const dirty = cardOf("llm").querySelector('tr[data-path="llm.max_tokens"]').classList.contains("dirty");
  typeInto(t, old);
  return dirty && cardOf("llm").querySelector('tr[data-path="llm.max_tokens"]').classList.contains("dirty") === false;
})());

log("\n[14] 页面完整性");
ck("占位符全部替换", html.indexOf("__SCHEMA_JSON__") < 0 && html.indexOf("__TOKEN__") < 0);
ck("只有一个 script 段", (html.match(/<\/script>/g) || []).length === 1);
ck("静态快照不含明文密钥", html.indexOf("sk-83fce") < 0);
ck("html 结尾完整", html.trim().endsWith("</html>"));

log("\n" + "=".repeat(64));
log("  通过 " + pass + " 项，失败 " + fail + " 项");
log("=".repeat(64));
dom.window.close();
process.exit(fail ? 1 : 0);
