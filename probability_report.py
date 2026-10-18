"""Small, deterministic, dependency-free probability research presentation."""
import copy
import html
import math

VERSION='calibrated-tplus1-probability-v1'
METRICS=('n_attempts','n_closed','n_unresolved','n_cancelled','independent_entry_dates',
         'win_rate','ge3_rate','ge6_rate','mean_closed_gross_return','strength3_rate',
         'realized_closed_trade_max_drawdown','mean_win_return','mean_loss_return','profit_factor')
VALIDATION=('date_cluster_win_ci95','moving_block_win_ci95','moving_block_mean_ci95',
            'familywise_lower_mean_return','selection_gate','selection_blockers')


def compact(study):
    if study.get('schema')!=VERSION:return {}
    keys=('schema','generated_at','base_trade_date','target_trade_date','input_database_sha256',
          'strategy_definition_sha256','formal_trade_ready','actual_win_rate','execution_win_rate_80_validated',
          'costs_included','validation_origin','policy','split','coverage','current_watchlist','endpoint_availability')
    value={key:copy.deepcopy(study[key]) for key in keys if key in study}
    value['diagnostic']={}
    for key,metric in (study.get('diagnostic') or {}).items():
        value['diagnostic'][key]={name:metric[name] for name in METRICS if name in metric}
        value['diagnostic'][key]['validation']={name:metric.get('validation',{}).get(name) for name in VALIDATION}
    value['selection_summary']=copy.deepcopy(study.get('selection_summary')) if 'selection_summary' in study else [
        {'id':row['policy']['id'],'eligible_research':row['eligible_research'],
         'n_closed':row['metrics']['n_closed'],'entry_dates':row['metrics']['independent_entry_dates'],
         'lower_mean':row['metrics']['validation']['familywise_lower_mean_return'],
         'blockers':row['metrics']['validation']['selection_blockers']} for row in study.get('selection',[])]
    value['probability_diagnostics']={}
    for model,data in (study.get('probability_diagnostics') or {}).items():
        value['probability_diagnostics'][model]={'endpoints':{}}
        for key,metric in data.get('endpoints',{}).items():
            keep=('n_scored','n_unknown_labels','entry_date_clusters','brier_score','log_loss',
                  'observed_frequency','mean_probability','training_only_baseline_scores',
                  'equal_date_weighted','brier_skill_vs_training_baseline')
            value['probability_diagnostics'][model]['endpoints'][key]={name:copy.deepcopy(metric[name]) for name in keep if name in metric}
    return value


def e(value):return html.escape(str(value),quote=True)
def number(value,digits=0):
    return f'{value:,.{digits}f}' if isinstance(value,(int,float)) and math.isfinite(value) else '—'
def pct(value):return number(value*100,2)+'%' if isinstance(value,(int,float)) and math.isfinite(value) else '—'
def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
def label(value):
    names={'same_universe_price_action_baseline':'同条件价量排序基准','selected_policy':'选择期确定的规则',
           'selected_policy_6pct_exit':'同规则改用6%退出（独立情景）'}
    if value in names:return names[value]
    return value.replace('equal_ensemble','等权组合').replace('logistic','逻辑回归').replace('hgb','梯度提升').replace('_balanced',' · 常规门槛').replace('_selective',' · 高门槛')


def render(study):
    study=compact(study)
    if not study:return ''
    policy=study.get('policy') or {};active=bool(policy.get('active'))
    coverage=study.get('coverage') or {};diagnostic=study['diagnostic']
    chosen=(policy.get('chosen') or {}).get('id')
    result='<section><h2>校准概率：新一轮数学模型检验</h2>'
    result+='<p>将正则化逻辑回归、浅层梯度提升和等权组合，转换为明确的隔日盈利、赚到3%、另用6%目标退出及连续三日走强事件概率。独立时间段校准、向日期先验收缩，再依据模型分歧和输入越界情况放弃信号。</p>'
    result+='<div class="callout"><strong>'+('选择期通过的研究规则：'+e(label(chosen)) if active else '本轮没有启用通过证据要求的概率规则。')+'</strong> '+('可继续作前向研究，正式交易资格仍未通过。' if active else '保留空名单；最终诊断的最高数字不会反过来决定规则。')+'</div>'
    result+='<p class="note">本轮统一5～100元范围，3万元资金、每名额1万元、100股整数手、最多3个持仓。扫描'+number(coverage.get('history_rows_scanned'))+'条日线；本期'+number(coverage.get('causal_live_candidates'))+'只通过事前价量条件，'+number(coverage.get('current_research_eligible'))+'只满足已选择研究规则。与旧5～10元形态表的范围不同。</p>'
    rows=[]
    for key,metric in diagnostic.items():
        if key=='selected_policy' and not active:continue
        rows.append([e(label(key)),number(metric.get('n_attempts'))+'／'+number(metric.get('n_unresolved')),number(metric.get('independent_entry_dates')),
                     pct(metric.get('win_rate')),pct(metric.get('ge3_rate')),pct(metric.get('ge6_rate')),pct(metric.get('mean_closed_gross_return'))])
    if rows:result+=table(['最终开发诊断','入场／未知','日期','毛盈利比例','赚到3%','赚到6%','平均已闭合收益'],rows)
    result+='<p class="note">主情景为D1开盘参考买入、D2按3%止盈／2%风险参考或收盘退出；6%退出另列，不能把两种卖法的成绩合并。撤单单列，未解决入场保留为非胜出并占用资金。日线参考成交无法证明个人订单成交；D1不卖出，D2跳空可能越过2%风险价。</p>'
    selection=study['selection_summary']
    passed=sum(row.get('eligible_research') is True for row in selection)
    result+='<details><summary>选择期：'+str(passed)+'／'+str(len(selection))+'项固定候选通过</summary>'
    result+=table(['预定候选','完整交易','入场日期','修正后收益下界','选择期结论'],[[e(label(row['id'])),number(row.get('n_closed')),number(row.get('entry_dates')),pct(row.get('lower_mean')),'可作研究' if row.get('eligible_research') else '证据或收益不足'] for row in selection])
    result+='<p class="note">训练、校准、选择、诊断按50%／15%／15%／20%完整日期划分；D3结果跨界剔除。选择期至少20笔完整代理交易、10个入场日期、零未知，且整日与连续5个活跃日期区块的多重比较修正收益下界均为正。区块和修正是近似诊断，不能消除全部历史开发偏差。</p></details>'
    scores=[]
    for model,data in study['probability_diagnostics'].items():
        metric=data.get('endpoints',{}).get('p_win3') or {}
        baseline=metric.get('training_only_baseline_scores') or {}
        supported=(study.get('endpoint_availability') or {}).get(model,{}).get('p_win3')
        scores.append([e(label(model))+('（先验回退）' if supported is False else ''),number(metric.get('n_scored')),number(metric.get('entry_date_clusters')),
                       number(metric.get('brier_score'),5),number(baseline.get('brier_score'),5),
                       number(metric.get('log_loss'),5),pct(metric.get('mean_probability')),pct(metric.get('observed_frequency'))])
    if scores:
        result+='<details><summary>概率是否可信：评分、基准和实际频率</summary>'+table(['模型','已知标签','日期','模型Brier','训练常数Brier','Log loss','平均预测','实际频率'],scores)
        result+='<p class="note">此表针对3%退出规则的盈利事件，仅在可按上限入场且D2数据有效的参考情景评分。Brier与Log loss越低越好，但同时反映排序与校准，单靠它们不能证明校准改善。全部四种事件的可靠性分箱、缺失标签和日期等权评分保留在归档证据中。</p></details>'
    if active and study.get('current_watchlist'):
        result+='<h3>单模型研究观察</h3>'+table(['股票','前收','隔日盈利','3%退出达标','6%退出达标','连续三日收高'],[[e(row.get('name') or row['symbol'])+'<br>'+e(row['symbol']),number(row.get('close'),2),pct(row.get('p_win3')),pct(row.get('p_ge3')),pct(row.get('p_ge6')),pct(row.get('p_strength3'))] for row in study['current_watchlist'][:5]])
        result+='<p class="note">仍需核对增强板块资金和开盘可成交条件；这份单模型观察不等于整套资金共振策略已获验证。三日强度仅为辅助，不把D2卖出改成等待D3。</p>'
    selected=diagnostic.get('selected_policy') or {}
    interval=(selected.get('validation') or {}).get('moving_block_win_ci95') or []
    if active and len(interval)==2:
        result+='<p class="note">已选择规则在最终开发诊断的连续日期块盈利比例95%区间：'+pct(interval[0])+'～'+pct(interval[1])+'；其范围受样本、非平稳和代理成交假设限制。</p>'
    result+='<p><b>80%以上实际胜率尚未验证。</b>所有旧历史均用于开发；本表不是新增样本外证明，也没有历史逐日资金、官方状态、复权和个人成交证据。历史概率诊断不能当作整套资金选股系统的胜率。</p>'
    result+='<p class="note">原始研究：<a href="https://icml.cc/Conferences/2005/proceedings/papers/079_GoodProbabilities_NiculescuMizilCaruana.pdf">ICML 2005概率校准</a>、<a href="https://scikit-learn.org/1.5/modules/calibration.html">所用版本的校准文档</a>、<a href="https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf">回测过拟合概率</a>。亦查阅<a href="https://arxiv.org/abs/2208.02814">共形风险控制</a>；本轮不把其条件性理论保证套成个股80%盈利保证。</p></section>'
    return result
