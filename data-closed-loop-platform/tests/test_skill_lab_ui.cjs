const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.value = ''; this.textContent = ''; this.dataset = {}; }
  append(node) { this.children.push(node); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
}
const elements = new Map();
const document = {createElement: tag => new Element(tag), querySelector: s => {
  if (!elements.has(s)) elements.set(s, new Element('div')); return elements.get(s);
}, querySelectorAll: () => []};
let code = fs.readFileSync(path.join(__dirname, '../skill-lab/app.js'), 'utf8');
code = code.replace(/  refresh\(\);setInterval\([^\n]+/, '  globalThis.testAPI={renderResult,runCard,vocSources,renderDefinition,drawPool,drawRequirements,definedItems,route,readRoute,showRequirement,setState:s=>state=s};');
const context = {document, console, location:{hash:''}, window:{addEventListener(){},scrollTo(){}},history:{replaceState(){}}}; vm.createContext(context); vm.runInContext(code, context);
const c = {attachments: [{name:'voc.json', parsed:{format:'json', text:'[JSON 根路径 $]\n'+JSON.stringify({comments:[{id:'A',comment_content:'窗帘入口英文模式仍显示中文。'}]})}}]};
const candidate = {title:'窗帘翻译',rough_requirement:'英文文案正确显示',feature:'地图编辑 / 地图编辑入口',evidence:'逐字引用：“窗帘入口英文模式仍显示中文。”（约3条）',user_task:'理解窗帘功能',feature_basis:'截图入口',missing_information:[]};
const parent = new Element('section');
context.testAPI.renderResult(parent,{stage:'analysis',result:{summary:'完成',candidates:[candidate,{...candidate,title:'未核验',feature:'设置',evidence:'凭空引用的证据'}],details:'分析过程',missing_information:[]}},c);
function all(node) { return [node,...node.children.flatMap(all)]; }
let nodes = all(parent);
assert.equal(nodes.filter(x=>x.tag==='tbody')[0].children.length,2);
assert(nodes.some(x=>x.textContent==='实际 VOC / 原始证据'));
assert(nodes.some(x=>x.textContent.includes('原文匹配 1 条记录')));
assert(nodes.some(x=>x.textContent.includes('未匹配到原始反馈')));
const input=nodes.find(x=>x.tag==='input');input.value='未核验';input.oninput();
assert.equal(all(parent).find(x=>x.tag==='tbody').children.length,1);
input.value='';input.oninput();const select=all(parent).find(x=>x.tag==='select');select.value='设置';select.onchange();
assert.equal(all(parent).find(x=>x.tag==='tbody').children.length,1);
assert(all(parent).some(x=>x.textContent==='未核验'));
const routed=new Element('section');
context.testAPI.renderResult(routed,{stage:'analysis',result:{routing:{category:'problem',skill_names:['problem-feedback-diagnosis'],reason:'单条已发生问题',confidence:.92,signals:['包含复现步骤']},summary:'完成',candidates:[],details:'分析过程',missing_information:[]}},c);
assert(all(routed).some(x=>x.textContent==='问题分析'));
assert(all(routed).some(x=>x.textContent==='92% 匹配'));
assert(all(routed).some(x=>x.textContent==='包含复现步骤'));
console.log('Skill lab table rendering, evidence verification, search and filtering: PASS');
const definitionRoot=new Element('section');
context.testAPI.renderDefinition(definitionRoot,{pool_item_id:'one',requirement_title:'地图恢复',result:{definition:{title:'地图恢复',problem:'丢失',user_task:'换机恢复',scenarios:['换机'],in_scope:['恢复地图'],out_of_scope:['耗材'],acceptance_criteria:['地图一致'],evidence:['地图丢失原文']}}});
assert(all(definitionRoot).some(x=>x.textContent==='本次解决'));
assert(all(definitionRoot).some(x=>x.textContent==='验收标准'));
context.testAPI.setState({cases:[{id:'case'}],pool:[{id:'one',case_id:'case',run_id:'analysis',candidate}],runs:[]});
context.testAPI.drawPool();const poolNodes=all(elements.get('#workspace'));
assert(poolNodes.some(x=>x.tag==='table'));
assert(poolNodes.some(x=>x.type==='checkbox'));
assert(poolNodes.some(x=>x.textContent==='批量需求定义'));
assert(!poolNodes.some(x=>x.textContent==='指标定义'&&x.tag==='button'));
context.testAPI.setState({cases:[{id:'case'}],pool:[{id:'one',case_id:'case',run_id:'analysis',candidate}],runs:[
  {id:'def',pool_item_id:'one',case_id:'case',stage:'definition',status:'completed',result:{definition:{title:'地图恢复'}},created_at:'2026-10-29'}]});
assert.equal(context.testAPI.definedItems().length,1);
elements.get('#workspace').replaceChildren();context.testAPI.drawPool();
assert(all(elements.get('#workspace')).some(x=>x.textContent==='当前没有待定义需求'));
elements.get('#workspace').replaceChildren();context.testAPI.drawRequirements();
const definedNodes=all(elements.get('#workspace'));
assert(definedNodes.some(x=>x.textContent==='查看定义'));
assert(definedNodes.some(x=>x.textContent==='需求定义：已生成 · 待评审'));
assert(definedNodes.find(x=>x.textContent==='指标定义'&&x.tag==='button').disabled);
console.log('Pool table, selection, scoped definition sections and metric prerequisites: PASS');
context.testAPI.showRequirement('one');
assert.equal(context.location.hash,'#/requirements/one');
let detailNodes=all(elements.get('#workspace'));
assert(detailNodes.some(x=>x.textContent==='← 返回需求分析'));
assert(!detailNodes.some(x=>x.textContent==='批量重新定义'));
assert(!detailNodes.some(x=>x.tag==='table'));
context.location.hash='#/requirements';context.testAPI.readRoute();
assert(all(elements.get('#workspace')).some(x=>x.tag==='table'));
context.location.hash='#/requirements/one';context.testAPI.readRoute();
assert(all(elements.get('#workspace')).some(x=>x.textContent==='← 返回需求分析'));
context.location.hash='#/requirements/missing';context.testAPI.readRoute();
assert(all(elements.get('#workspace')).some(x=>x.textContent==='未找到该需求'));
context.location.hash='#/requirements/%FF';context.testAPI.readRoute(false);
console.log('Requirement detail navigation, hash restore, list return and unknown IDs: PASS');
const automaticRoot=new Element('section');
context.testAPI.runCard(automaticRoot,{id:'analysis-auto',case_id:'case',stage:'analysis',status:'completed',
  pool_publication:{mode:'automatic',count:1},result:{summary:'完成',details:'依据',candidates:[candidate],missing_information:[]}},c);
const automaticNodes=all(automaticRoot);
assert(!automaticNodes.some(x=>x.tag==='textarea'));
assert(!automaticNodes.some(x=>x.textContent==='采纳结果'||x.textContent==='驳回结果'));
assert(automaticNodes.some(x=>x.tag==='button'&&x.textContent.includes('需求池')));
const definitionReviewRoot=new Element('section');
context.testAPI.runCard(definitionReviewRoot,{id:'definition-review',case_id:'case',pool_item_id:'one',stage:'definition',status:'completed',
  result:{definition:{title:'地图恢复'}}},c);
assert(all(definitionReviewRoot).some(x=>x.textContent==='采纳结果'));
console.log('Analysis auto-pool handoff without confirmation; downstream review preserved: PASS');
