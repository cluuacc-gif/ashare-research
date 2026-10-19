"""Offline admission for a genuinely prospective research freeze.

Only manifest metadata is checked here. File contents and database identity must
still be independently verified by the normal restore step after admission.
The caller supplies the real, timezone-aware clock; no clock is read here.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import html
import re

import trade_calendar

TZ = timezone(timedelta(hours=8), 'Asia/Shanghai')
SCHEMA = 'outlook-admission-v1'


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError('canonical date required')
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError('canonical date required')
    return parsed


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('aware timestamp required')
    return parsed.astimezone(TZ)


def evaluate(source_manifest, now_datetime):
    """Return a fail-closed metadata decision without downloading or mutating.

    ``ready`` permits the existing restore/research path to try to freeze a new
    cohort. It never grants data, model, execution, email or order readiness.
    At exactly D1 09:15 the prospective window has already closed.
    """
    if (not isinstance(now_datetime, datetime) or now_datetime.tzinfo is None
            or now_datetime.utcoffset() is None):
        raise ValueError('now_datetime must be timezone-aware')
    now = now_datetime.astimezone(TZ)
    source = source_manifest if isinstance(source_manifest, dict) else {}
    result = {
        'schema': SCHEMA, 'status': 'unverified_source', 'ready': False,
        'checked_at': now.isoformat(),
        'base_trade_date': source.get('base_trade_date'),
        'target_trade_date': source.get('next_trade_date'),
        'source_release': source.get('release_tag'),
        'prospective_deadline': None,
        'preparation_ready': source.get('preparation_ready') is True,
        'reason': '尚未取得可核对的收盘准备数据，暂不启动新的前向研究。',
        'manifest_metadata_checked': False,
        'database_hash_verified': False, 'database_identity_verified': False,
        'formal_trade_ready': False, 'actual_win_rate': None,
        'execution_win_rate_80_validated': False,
        'main_policy_active': False, 'smtp_enabled': False,
        'orders_enabled': False, 'smtp_sent': False, 'orders_submitted': False,
    }
    if source.get('preparation_ready') is not True:
        result['reason'] = '当前数据尚未完成收盘准备，等待新的已准备版本。'
        return result
    try:
        base = _day(source.get('base_trade_date'))
        target = _day(source.get('next_trade_date'))
        # Check the real clock's year too: never infer an unknown year's sessions.
        trade_calendar.is_session(now.date())
        if not trade_calendar.is_session(base) or not trade_calendar.is_session(target):
            raise ValueError('base or target is not a verified session')
        if trade_calendar.adjacent(base, 1) != target.isoformat():
            raise ValueError('target is not adjacent')
        deadline = datetime.combine(target, time(9, 15), TZ)
        result['prospective_deadline'] = deadline.isoformat()
        release = source.get('release_tag')
        if (not isinstance(release, str) or not re.fullmatch(
                r'prepared-' + re.escape(base.isoformat()) + r'-[A-Za-z0-9][A-Za-z0-9._-]*', release)):
            raise ValueError('prepared release does not identify its base')
        database = source.get('database')
        if not isinstance(database, dict):
            raise ValueError('database manifest reference required')
        name, digest = database.get('name'), database.get('sha256')
        if (not isinstance(name, str) or name in ('', '.', '..')
                or any(mark in name for mark in ('/', '\\', ':', '\x00'))
                or not isinstance(digest, str)
                or not re.fullmatch(r'[a-fA-F0-9]{64}', digest)):
            raise ValueError('invalid database manifest reference')
        if 'bytes' in database and (type(database['bytes']) is not int or database['bytes'] <= 0):
            raise ValueError('invalid declared asset size')
        generated = _timestamp(source.get('generated_at'))
        published = _timestamp(source.get('published_at'))
        close = datetime.combine(base, time(15), TZ)
        if not close <= generated <= published <= now:
            raise ValueError('source timing is not known and after close')
        # A manifest occasionally includes these extra observation boundaries.
        # Never ignore an explicit claim to contain later market observations.
        for key in ('database_latest_date', 'latest_trade_date', 'input_last_date'):
            if source.get(key) is not None and _day(source[key]) > base:
                raise ValueError('source contains dates beyond its closing base')
        if source.get('information_cutoff') is not None:
            cutoff = _timestamp(source['information_cutoff'])
            if not close <= cutoff <= published:
                raise ValueError('source cutoff is temporally impossible')
    except (ValueError, TypeError, KeyError, OverflowError, OSError):
        result['reason'] = '数据日期、交易日历、版本标记或准备时间尚不能一致核对；暂停新的前向研究，等待可核对的收盘版本。'
        return result
    result['manifest_metadata_checked'] = True
    if now >= deadline:
        result.update(
            status='waiting_for_next_prepared_close',
            reason=f'当前版本以{base.isoformat()}收盘为基础，原目标交易日为{target.isoformat()}；'
                   '目标日09:15前的冻结窗口已结束。等待下一份真实收盘准备数据，再建立新的前向样本。')
    else:
        result.update(
            status='ready', ready=True,
            reason=f'当前版本以{base.isoformat()}真实收盘为基础，目标为{target.isoformat()}；'
                   '尚处于目标日09:15之前，可继续核对数据文件并尝试冻结前向研究。')
    return result


def _escape(value):
    return html.escape(str(value if value is not None else '待核对'), quote=True)


def _clock(value):
    try:
        return _timestamp(value).strftime('%Y-%m-%d %H:%M:%S 北京时间')
    except (ValueError, TypeError, OverflowError):
        return _escape(value)


def render(result):
    """Render a compact standalone status report; every supplied string is escaped."""
    status = result.get('status')
    titles = {
        'ready': '收盘版本已到位，准备核对并冻结',
        'waiting_for_next_prepared_close': '等待下一份收盘准备数据',
        'unverified_source': '数据版本待核对',
    }
    title = titles.get(status, '研究准入状态待核对')
    labels = {'ready': '可继续核对',
              'waiting_for_next_prepared_close': '等待下一期',
              'unverified_source': '暂缓启动'}
    label = labels.get(status, status)
    esc = _escape
    ready = status == 'ready' and result.get('ready') is True
    next_step = ('继续下载并独立核对数据文件；只有核对通过，且仍早于目标日09:15，才尝试冻结本期研究。'
                 if ready else
                 '下一份符合条件的收盘准备版本发布后，现有流程会自动尝试开展新一期研究；仍须通过时间与数据核对。')
    prepared = '已准备（仅来源声明）' if result.get('preparation_ready') is True else '尚未确认'
    transport_section = ''
    transport = result.get('minute_transport')
    if isinstance(transport, dict):
        requested, available = transport.get('symbols_requested'), transport.get('symbols_available')
        observed = (type(requested) is int and requested > 0
                    and type(available) is int and 0 <= available <= requested)
        transport_label = ('已接通' if available else '本次不可用') if observed else '结果待核对'
        counts = f'本次核对{requested}只，可读取{available}只。' if observed else ''
        transport_section = ('<section><h2>分钟行情读取检查</h2><p>' + esc(transport_label)
                             + '。' + esc(counts) + '</p><p class="note">'
                             '这项检查只记录读取情况，不计为新交易样本，不验证收益或胜率。</p></section>')
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>前向研究准入状态</title><style>
*{box-sizing:border-box}body{margin:0;background:#f4f5f2;color:#172722;font:16px/1.75 "Microsoft YaHei","Noto Sans CJK SC",sans-serif}
main{max-width:980px;margin:auto;padding:36px 26px 52px}h1{font-size:31px;line-height:1.4;margin:8px 0 14px}h2{font-size:21px;margin:0 0 12px}p{margin:9px 0 14px}.eyebrow{font-size:13px;color:#537367;letter-spacing:2px}.pill{display:inline-block;border-radius:20px;padding:3px 12px;background:#e9eee7;color:#28664b;font-size:13px}section{background:#fff;border:1px solid #dde4dc;border-radius:14px;margin-top:20px;padding:23px}.callout{border-left:4px solid #a07636;background:#fbf6ec;padding:13px 17px}.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-top:22px}.tile{background:white;border:1px solid #dce3dc;border-radius:11px;padding:14px}.tile span{font-size:13px;color:#66766e;display:block}.tile b{font-size:19px}.muted,.note{color:#66766e;font-size:13px}.scroll{overflow:auto}table{width:100%;border-collapse:collapse;font-size:14px}th{text-align:left;background:#edf2ee;color:#486150}th,td{padding:11px 12px;border-bottom:1px solid #e7ece6;vertical-align:top}details{margin-top:14px}summary{cursor:pointer;color:#486150}.provenance{overflow-wrap:anywhere;font-size:12px}footer{margin-top:24px;font-size:13px;color:#66766e}@media(max-width:650px){main{padding:24px 14px}h1{font-size:26px}section{padding:18px 14px}.grid{grid-template-columns:1fr}.tile b{font-size:18px}table{min-width:650px}}
</style></head><body><main>
<div class="eyebrow">A股 · 前向研究进度</div><h1>''' + esc(title) + '''</h1>
<span class="pill">''' + esc(label) + '''</span><p class="muted">核对时点：''' + _clock(result.get('checked_at')) + '''</p>
<p>''' + esc(result.get('reason')) + '''</p>
<div class="grid"><div class="tile"><span>实际基础交易日</span><b>''' + esc(result.get('base_trade_date')) + '''</b></div>
<div class="tile"><span>来源指定的目标交易日</span><b>''' + esc(result.get('target_trade_date')) + '''</b></div>
<div class="tile"><span>本期冻结截止（必须早于）</span><b>09:15</b></div></div>
<section><h2>本次准入判断</h2><p>''' + esc(result.get('reason')) + '''</p>
<div class="callout">09:15是事前冻结边界。已过边界的机会只保留原有记录，不在盘中回填为新样本；今天尚未收盘时，也不把今天写成已完成的收盘基础日。</div>
<p class="note">完整截止时间：''' + _clock(result.get('prospective_deadline')) + '''。收盘准备：''' + esc(prepared) + '''。这里只核对来源说明，数据文件内容及身份仍需下载后独立核对。</p></section>
<section><h2>接下来如何继续</h2><p>''' + esc(next_step) + '''</p>
<p>现有GitHub收盘采集流程通常安排在交易日北京时间16:35启动，可能排队或延迟；此时间不保证数据已经准备完成。</p>
<p class="note">本页不承诺邮件送达时间；独立云主机和实时行情接入仍待完成。</p></section>''' + transport_section + '''
<section><h2>固定研究方案</h2><p>六组方案在同一份事前冻结观察池中对照，条件共振只作筛选，不在结果出现后换股或改时点。</p>
<div class="scroll"><table><thead><tr><th>固定组别</th><th>D1入场观察时点</th><th>D2统一退出参考</th></tr></thead><tbody>
<tr><td>基础组 · 早盘</td><td>09:40</td><td rowspan="6">14:50分钟开盘参考价</td></tr>
<tr><td>基础组 · 尾盘</td><td>14:40</td></tr><tr><td>条件共振组 · 早盘</td><td>09:40</td></tr>
<tr><td>条件共振组 · 尾盘</td><td>14:40</td></tr><tr><td>条件共振组 · 早盘回踩</td><td>09:40，已完成走势满足条件时</td></tr>
<tr><td>条件共振组 · 尾盘确认</td><td>14:40，已完成走势满足条件时</td></tr>
</tbody></table></div><p class="note">D1为目标买入日，D2为下一交易日；遵守T+1。费用口径固定为0，采用价格参考，不能视为个人订单成交或可实现净收益。</p></section>
<section><h2>哪些已实现，哪些仍在等待</h2><div class="scroll"><table><thead><tr><th>事项</th><th>已经实现</th><th>仍待完成或验证</th></tr></thead><tbody>
<tr><td>固定研究协议</td><td>六组方案、事前时限、统一退出参考已写入规则</td><td>首个未来样本及其结果</td></tr>
<tr><td>板块与分钟证据</td><td>板块领先筛选、分钟行情归档规则已实现</td><td>持续取得真实、完整且及时的观测</td></tr>
<tr><td>前向记录</td><td>冻结及结果记录机制已实现</td><td>首个有效未来样本与D2收益结算</td></tr>
<tr><td>正式数据与运行环境</td><td>现有GitHub收盘流程</td><td>官方时点状态、实时行情、独立云主机</td></tr>
</tbody></table></div><div class="callout">主要交易策略尚未启用；正式交易未准入。实际胜率未知，“执行胜率达到80%”尚未得到验证。本页不会发送邮件或提交订单。</div></section>
<footer>本页是研究准入进度说明，不是股票推荐。没有把旧诊断结果算作新的前向验证，也没有填入假设收益。
<details><summary>来源版本</summary><p class="provenance">''' + esc(result.get('source_release')) + '''</p></details></footer>
</main></body></html>'''
