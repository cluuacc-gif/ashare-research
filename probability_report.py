"""Small, deterministic, dependency-free probability research presentation."""
import copy
import html
import math

VERSION='calibrated-tplus1-probability-v1'
NO_CURRENT_INPUTS='no_qualifying_current_inputs'
METRICS=('n_attempts','n_closed','n_unresolved','n_cancelled','independent_entry_dates',
         'win_rate','ge3_rate','ge6_rate','mean_closed_gross_return','strength3_rate',
         'realized_closed_trade_max_drawdown','mean_win_return','mean_loss_return','profit_factor')
VALIDATION=('date_cluster_win_ci95','moving_block_win_ci95','moving_block_mean_ci95',
            'familywise_lower_mean_return','selection_gate','selection_blockers')


def compact(study):
    if study.get('schema')!=VERSION:return {}
    keys=('schema','generated_at','base_trade_date','target_trade_date','input_database_sha256',
          'strategy_definition_sha256','formal_trade_ready','actual_win_rate','execution_win_rate_80_validated',
          'costs_included','validation_origin','policy','split','coverage','current_watchlist','endpoint_availability',
          'status','status_message','training_performed','selection_performed','current_probability_status','input_funnel')
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
    if value.get('status')==NO_CURRENT_INPUTS:
        value['status_message']='��ģ�͵��ڲ�����'
        value['policy']={**(value.get('policy') or {}),'active':False,'chosen':None,
                         'formal_activation':False,'inactive_reason':NO_CURRENT_INPUTS}
        value['current_watchlist']=[]
        value['current_probability_status']='unknown_no_qualifying_current_inputs'
        value['endpoint_availability']={model:{key:False for key in endpoints}
                                       for model,endpoints in (value.get('endpoint_availability') or {}).items()}
    return value


def e(value):return html.escape(str(value),quote=True)
def number(value,digits=0):
    return f'{value:,.{digits}f}' if isinstance(value,(int,float)) and math.isfinite(value) else '��'
def pct(value):return number(value*100,2)+'%' if isinstance(value,(int,float)) and math.isfinite(value) else '��'
def table(headers,rows):
    return '<div class="scroll"><table><thead><tr>'+''.join('<th>'+e(v)+'</th>' for v in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
def label(value):
    names={'same_universe_price_action_baseline':'ͬ�������������׼','selected_policy':'ѡ����ȷ���Ĺ���',
           'selected_policy_6pct_exit':'ͬ�������6%�˳��������龰��'}
    if value in names:return names[value]
    return value.replace('equal_ensemble','��Ȩ���').replace('logistic','�߼��ع�').replace('hgb','�ݶ�����').replace('_balanced',' �� �����ż�').replace('_selective',' �� ���ż�')


def _render_no_current_inputs(study):
    coverage=study.get('coverage') or {};funnel=(study.get('input_funnel') or {}).get('current') or {}
    result='<section><h2>У׼���ʣ���ģ�͵��ڲ�����</h2>'
    result+='<div class="callout"><strong>ԭʼ����������ڣ���û�й�Ʊͨ���ɸ���ģ�͵�ȫ����ǰ������</strong> ��ģ�鱣��δ���ã����ڸ���δ֪��û�����þ�Ԥ�⣬Ҳû������������ʡ�����δѵ��ģ�͡�δѡ����ʹ���</div>'
    result+='<p>������ '+e(study.get('base_trade_date','��'))+'��Ŀ�꽻���� '+e(study.get('target_trade_date','��'))+'������ԭʼ���� '+number(coverage.get('wide_universe_live_rows'))+' ֻ��ȫ�������� '+number(coverage.get('causal_live_candidates'))+' ֻ����״ֻ̬˵���ɸ���ģ�鲻���ã���������ϵͳ�԰��Լ��İ�顢������볡֤���ж���</p>'
    names={'median_amount20_ge_20000000':'20�ճɽ�����λ�� �� 2000��Ԫ',
           'volatility20_le_0.05':'20�ղ����� �� 5%',
           'ret1_between_-0.04_0.06':'�����ǵ��� -4%��6%',
           'ma20_gap_between_-0.06_0.15':'��20�վ��� -6%��15%',
           'close_location_between_0.25_0.95':'����λ�� 0.25��0.95',
           'market_breadth_ge_0.30':'�г����Ǳ��� �� 30%',
           'market_mean_return_ge_-0.02':'�г�ƽ���ǵ��� �� -2%'}
    rows=[[e(names.get(row.get('condition'),row.get('condition','��'))),
           number(row.get('individual_pass')),number(row.get('individual_fail')),
           number(row.get('cumulative_pass')),number(row.get('rejected_at_step'))]
          for row in funnel.get('steps',[])]
    if rows:result+=table(['�̶���ǰ����','����ͨ��','����δ��','�����ۼ�ͨ��','�����޳�'],rows)
    context=funnel.get('market_context') or {}
    if context:
        result+=table(['�����г�����','��Сֵ','���ֵ'],[
            [e(name),pct((context.get(key) or {}).get('min')),pct((context.get(key) or {}).get('max'))]
            for key,name in (('market_breadth','�г����Ǳ���'),('market_mean_return','�г�ƽ���ǵ���'))])
    result+='<p class="note">����ͨ�������������ۼ�ͨ��������˳����㡣��������ֻʹ�������ռ���ǰ����Ϣ����ֵ����ʷ�о�Э�鱣�ֲ��䣻δ���ǵ���ǩ������ɸѡ�������쳣�������ϣ������ѵ�������Ի�ʹ����ʧ�ܡ�</p>'
    result+='<p><b>����û�пɱ����ģ�͸��ʣ�80%����ʵ��ʤ����δ��֤��</b></p></section>'
    return result


def render(study):
    study=compact(study)
    if not study:return ''
    if study.get('status')==NO_CURRENT_INPUTS:return _render_no_current_inputs(study)
    policy=study.get('policy') or {};active=bool(policy.get('active'))
    coverage=study.get('coverage') or {};diagnostic=study['diagnostic']
    chosen=(policy.get('chosen') or {}).get('id')
    result='<section><h2>У׼���ʣ���һ����ѧģ�ͼ���</h2>'
    result+='<p>�������߼��ع顢ǳ���ݶ������͵�Ȩ��ϣ�ת��Ϊ��ȷ�ĸ���ӯ����׬��3%������6%Ŀ���˳�������������ǿ�¼����ʡ�����ʱ���У׼������������������������ģ�ͷ��������Խ����������źš�</p>'
    result+='<div class="callout"><strong>'+('ѡ����ͨ�����о�����'+e(label(chosen)) if active else '����û������ͨ��֤��Ҫ��ĸ��ʹ���')+'</strong> '+('�ɼ�����ǰ���о�����ʽ�����ʸ���δͨ����' if active else '������������������ϵ�������ֲ��ᷴ������������')+'</div>'
    result+='<p class="note">����ͳһ5��100Ԫ��Χ��3��Ԫ�ʽ�ÿ����1��Ԫ��100�������֡����3���ֲ֡�ɨ��'+number(coverage.get('history_rows_scanned'))+'�����ߣ�����'+number(coverage.get('causal_live_candidates'))+'ֻͨ����ǰ����������'+number(coverage.get('current_research_eligible'))+'ֻ������ѡ���о��������5��10Ԫ��̬���ķ�Χ��ͬ��</p>'
    rows=[]
    for key,metric in diagnostic.items():
        if key=='selected_policy' and not active:continue
        rows.append([e(label(key)),number(metric.get('n_attempts'))+'��'+number(metric.get('n_unresolved')),number(metric.get('independent_entry_dates')),
                     pct(metric.get('win_rate')),pct(metric.get('ge3_rate')),pct(metric.get('ge6_rate')),pct(metric.get('mean_closed_gross_return'))])
    if rows:result+=table(['���տ������','�볡��δ֪','����','ëӯ������','׬��3%','׬��6%','ƽ���ѱպ�����'],rows)
    result+='<p class="note">���龰ΪD1���̲ο����롢D2��3%ֹӯ��2%���ղο��������˳���6%�˳����У����ܰ����������ĳɼ��ϲ����������У�δ����볡����Ϊ��ʤ����ռ���ʽ����߲ο��ɽ��޷�֤�����˶����ɽ���D1��������D2���տ���Խ��2%���ռۡ�</p>'
    selection=study['selection_summary']
    passed=sum(row.get('eligible_research') is True for row in selection)
    result+='<details><summary>ѡ���ڣ�'+str(passed)+'��'+str(len(selection))+'��̶���ѡͨ��</summary>'
    result+=table(['Ԥ����ѡ','��������','�볡����','�����������½�','ѡ���ڽ���'],[[e(label(row['id'])),number(row.get('n_closed')),number(row.get('entry_dates')),pct(row.get('lower_mean')),'�����о�' if row.get('eligible_research') else '֤�ݻ����治��'] for row in selection])
    result+='<p class="note">ѵ����У׼��ѡ����ϰ�50%��15%��15%��20%�������ڻ��֣�D3�������޳���ѡ��������20�������������ס�10���볡���ڡ���δ֪��������������5����Ծ��������Ķ��رȽ����������½��Ϊ��������������ǽ�����ϣ���������ȫ����ʷ����ƫ�</p></details>'
    scores=[]
    for model,data in study['probability_diagnostics'].items():
        metric=data.get('endpoints',{}).get('p_win3') or {}
        baseline=metric.get('training_only_baseline_scores') or {}
        supported=(study.get('endpoint_availability') or {}).get(model,{}).get('p_win3')
        scores.append([e(label(model))+('��������ˣ�' if supported is False else ''),number(metric.get('n_scored')),number(metric.get('entry_date_clusters')),
                       number(metric.get('brier_score'),5),number(baseline.get('brier_score'),5),
                       number(metric.get('log_loss'),5),pct(metric.get('mean_probability')),pct(metric.get('observed_frequency'))])
    if scores:
        result+='<details><summary>�����Ƿ���ţ����֡���׼��ʵ��Ƶ��</summary>'+table(['ģ��','��֪��ǩ','����','ģ��Brier','ѵ������Brier','Log loss','ƽ��Ԥ��','ʵ��Ƶ��'],scores)
        result+='<p class="note">�˱����3%�˳������ӯ���¼������ڿɰ������볡��D2������Ч�Ĳο��龰���֡�Brier��Log lossԽ��Խ�ã���ͬʱ��ӳ������У׼���������ǲ���֤��У׼���ơ�ȫ�������¼��Ŀɿ��Է��䡢ȱʧ��ǩ�����ڵ�Ȩ���ֱ����ڹ鵵֤���С�</p></details>'
    if active and study.get('current_watchlist'):
        result+='<h3>��ģ���о��۲�</h3>'+table(['��Ʊ','ǰ��','����ӯ��','3%�˳����','6%�˳����','���������ո�'],[[e(row.get('name') or row['symbol'])+'<br>'+e(row['symbol']),number(row.get('close'),2),pct(row.get('p_win3')),pct(row.get('p_ge3')),pct(row.get('p_ge6')),pct(row.get('p_strength3'))] for row in study['current_watchlist'][:5]])
        result+='<p class="note">����˶���ǿ����ʽ�Ϳ��̿ɳɽ���������ݵ�ģ�͹۲첻���������ʽ�������ѻ���֤������ǿ�Ƚ�Ϊ����������D2�����ĳɵȴ�D3��</p>'
    selected=diagnostic.get('selected_policy') or {}
    interval=(selected.get('validation') or {}).get('moving_block_win_ci95') or []
    if active and len(interval)==2:
        result+='<p class="note">��ѡ����������տ�����ϵ��������ڿ�ӯ������95%���䣺'+pct(interval[0])+'��'+pct(interval[1])+'���䷶Χ����������ƽ�Ⱥʹ����ɽ��������ơ�</p>'
    result+='<p><b>80%����ʵ��ʤ����δ��֤��</b>���о���ʷ�����ڿ�����������������������֤����Ҳû����ʷ�����ʽ𡢹ٷ�״̬����Ȩ�͸��˳ɽ�֤�ݡ���ʷ������ϲ��ܵ��������ʽ�ѡ��ϵͳ��ʤ�ʡ�</p>'
    result+='<p class="note">ԭʼ�о���<a href="https://icml.cc/Conferences/2005/proceedings/papers/079_GoodProbabilities_NiculescuMizilCaruana.pdf">ICML 2005����У׼</a>��<a href="https://scikit-learn.org/1.5/modules/calibration.html">���ð汾��У׼�ĵ�</a>��<a href="https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf">�ز����ϸ���</a>�������<a href="https://arxiv.org/abs/2208.02814">���η��տ���</a>�����ֲ��������������۱�֤�׳ɸ���80%ӯ����֤��</p></section>'
    return result
