import {heading, panel} from '../components/common/heading.js';
import {records} from '../components/common/records.js';
import {recoveryLabels} from '../constants/workflow.js';
import {topic} from '../constants/project.js';
import {date, esc} from '../utils/dom.js';
import {submit} from '../hooks/submit.js';

export const dutyState = {result: null};

function outcomeBadge(outcome) {
  return `<span class="status" data-state="${esc(outcome)}">${esc(recoveryLabels[outcome] || outcome)}</span>`;
}

export function dutyPage(state) {
  if (state.session.role !== 'duty') {
    return heading(topic.modules[4], '误删办结单备份恢复')
      + '<p class="empty">该模块仅数据库值班员可操作，请使用值班账号登录。</p>';
  }
  const backups = state.backups || [];
  const rows = backups.map(row => [
    `<button class="linklike" type="button" data-run="${row.run_id}">${esc(row.run_id)}</button>`,
    esc(row.case_id), esc(date(row.created_at)),
  ]);
  const result = dutyState.result;
  const resultPanel = result ? panel('按单号查询结果', `
    <dl class="metrics">
      <div><dt>执行单编号</dt><dd>${esc(result.run_id)}</dd></div>
      <div><dt>状态</dt><dd>${outcomeBadge(result.outcome)}</dd></div>
    </dl>
    <p>${esc(result.detail || '')}</p>
    ${result.evidence ? `<details><summary>凭证校验码与签署信息</summary>
      <p>校验码 <code class="digest">${esc(result.evidence.digest)}</code></p>
      <p>签署：${esc(result.evidence.reviewer || '待签署')} · ${esc(date(result.evidence.reviewed_at))}</p></details>` : ''}
    ${result.outcome === 'recoverable' ? `<form id="recover-form"><input type="hidden" name="run_id" value="${esc(result.run_id)}"><button>按原编号恢复</button></form>` : ''}
  `) : '';
  return `${heading(topic.modules[4], '从备份按原编号找回误删办结单，执行单与凭证同时点一起补回')}
    <div class="spread">
      <section>${panel('备份清单', records(['执行单编号', '档案编号', '备份时间'], rows, '暂无备份。'))}</section>
      <aside class="brief">
        ${panel('按单号查询', '<form id="status-form"><label>执行单编号<input name="run_id" inputmode="numeric" required placeholder="例如 9101"></label><button>查询恢复状态</button></form>')}
        ${resultPanel}
      </aside>
    </div>`;
}

export function bindDuty(api, update, fail) {
  const ask = async runId => {
    dutyState.result = await api(`/duty/runs/${runId}/recovery-status`);
    await update();
  };
  submit('status-form', async data => ask(Number(data.get('run_id'))), fail);
  submit('recover-form', async data => {
    dutyState.result = await api(`/duty/runs/${Number(data.get('run_id'))}/recover`, {method: 'POST'});
    await update();
  }, fail);
  document.querySelectorAll('button.linklike[data-run]').forEach(button => {
    button.addEventListener('click', () => ask(Number(button.dataset.run)).catch(fail));
  });
}
