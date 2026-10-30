const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.textContent = '';
    this.value = '';
    this.dataset = {};
    this.isConnected = true;
  }
  append(node) { this.children.push(node); node.parent = this; }
  replaceChildren() { this.children = []; }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this);
  }
  get options() { return this.children; }
}

const window = {};
const context = {window, document: {createElement: tag => new Element(tag)}, FormData: class {}};
vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../skill-lab/tracking.js'), 'utf8'), context);

function all(node) { return [node, ...node.children.flatMap(all)]; }
const parent = new Element('main');
parent.dataset.view = 'tracking';
const find = text => all(parent).find(node => node.textContent === text);
const achievedOption = () => all(parent).find(node => node.tag === 'option' && node.value === 'achieved');
const item = {
  id: 'tracking-test-1',
  title: '商清建图成功率偏低',
  source_id: 'SIM-BI-DEEBOT-MAPPING-001',
  product_line: 'deebot',
  business_scope: '商清',
  is_simulation: true,
  status: 'pending_assignment',
  baseline: {stat_date: '2026-10-28', attempt_count: 200, success_count: 136,
    rate: 0.68, threshold: 0.85, previous_period_rate: 0.87, gap_pp: 17, decline_pp: 19},
  diagnosis: {summary: '建图跨端链路，责任尚未明确。', routing: '待人工分配', missing_information: ['失败阶段与版本分布']},
  observations: [],
  retrospectives: [],
  history: []
};
const calls = [];
const services = {
  api: async (url, body) => { calls.push({url, body}); return {items: [item]}; },
  notice: new Element('p')
};

(async () => {
  await window.TrackingLab.render(parent, services);
  assert(find('商清建图成功率偏低'));
  assert(find('68.0%'));
  assert(find('待人工分配'));
  assert(find('查看并处理'));
  assert(all(parent).some(node => node.tag === 'table'));
  find('查看并处理').onclick();
  // The click starts an asynchronous render; a direct second render also checks request sequencing.
  await window.TrackingLab.render(parent, services);
  assert(find('← 返回 Tracking 列表'));
  assert(find('1. 核查与分配'));
  assert(find('核查负责人：待人工分配'));
  assert.equal(all(parent).find(node => node.name === 'owner').value, '');
  assert.equal(achievedOption().disabled, true, 'No result must not allow achieved closure');
  assert(calls.every(call => call.url === '/api/tracking' && call.body === undefined),
    'Rendering must not silently assign a person or mutate workflow');

  item.metric_card = {version: 1, target_rate: 0.85, min_sample: 200,
    observation_days: 7, definition: '成功次数 / 有效尝试次数，按 mapping_attempt_id 去重'};
  item.release = {version: 'SIM-1', date: '2026-10-29', metric_card_version: 1};
  item.observations = [{id: 'observation-current', stat_date: '2026-11-04', attempt_count: 200,
    success_count: 180, rate: 0.90, window_days: 7, definition: item.metric_card.definition,
    version: 'SIM-1', status: 'met', reasons: [], metric_card_version: 1}];
  await window.TrackingLab.render(parent, services);
  assert(find('达标'), 'Flat observation.status must render as met');
  assert(find('90.0%'));
  assert(find('180 / 200'));
  assert.equal(achievedOption().disabled, true, 'Numerical success alone is not a completed retrospective');

  item.retrospectives = [{id: 'retro-old', status: 'completed', observation_id: 'observation-old',
    result: {actual_result_summary: '旧观测', reason_summary: '历史解释', next_action: '继续验证', missing_information: []}}];
  await window.TrackingLab.render(parent, services);
  assert.equal(achievedOption().disabled, true, 'Old observation retrospective must not unlock new closure');
  assert(find('历史观测的复盘结果，不用于当前观测的达标关闭。'));

  item.retrospectives.push({id: 'retro-running', status: 'running', observation_id: 'observation-current'});
  await window.TrackingLab.render(parent, services);
  assert.equal(achievedOption().disabled, true, 'Running retrospective does not unlock closure');
  assert(find('复盘正在运行…').disabled);

  item.retrospectives[1] = {id: 'retro-complete', status: 'completed', observation_id: 'observation-current',
    result: {actual_result_summary: '模拟成功率90%，达到冻结目标85%。', reason_summary: '不能据此确认根因。',
      next_action: '人工评审后决定是否关闭。', missing_information: ['失败阶段样本']}};
  await window.TrackingLab.render(parent, services);
  assert.equal(achievedOption().disabled, false, 'Matching completed retrospective and met result unlock closure');
  assert(find('模拟成功率90%，达到冻结目标85%。'));
  assert(find('不能据此确认根因。'));

  item.observations[0].status = 'inconclusive';
  await window.TrackingLab.render(parent, services);
  assert.equal(achievedOption().disabled, true, 'Completed retrospective cannot override inconclusive facts');
  item.observations[0].status = 'met';
  item.status = 'closed';
  item.closure = {decision: 'achieved', note: '隔离测试：人工确认模拟达标'};
  await window.TrackingLab.render(parent, services);
  assert(!all(parent).some(node => node.tag === 'form'), 'Closed record must have no writable forms');
  assert(find('人工闭环决定'));
  assert(find('隔离测试：人工确认模拟达标'));
  assert(find('运行效果复盘 Skill').disabled, 'Closed record must not start new retrospectives');
  console.log('Tracking UI list/detail, pending ownership, flat observations, retrospective gates and closed read-only: PASS');
})();
