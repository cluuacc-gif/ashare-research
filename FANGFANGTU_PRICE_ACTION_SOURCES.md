# 方方土 PriceAction：来源核验与 A 股适配边界

核验日期：2026-10-18。以下仅使用官方 YouTube 页面及 Brooks Trading Course 官方资料；未使用搬运视频作为作者观点证据。

## 已核验的作者页面

官方频道：[方方土PriceAction](https://www.youtube.com/channel/UC-CckeotGQCZigg3nETreCQ)。公开搜索索引可核验下列官方上传标题、作者和部分发布日期：

| 官方视频 | 已核验的信息 | 本次访问限制 |
|---|---|---|
| [答疑(3)：背景&信号k](https://www.youtube.com/watch?v=kW3zGw96SaE) | 官方页面标题包含“背景&信号k” | 直开仅返回页脚，未获得正文、字幕或时间戳 |
| [突破专题(1)：假突破](https://www.youtube.com/watch?v=152osf_ULas) | 作者方方土PriceAction；2024-03-02；描述链接 Al Brooks 视频课 | 搜索索引提供元数据，未获取视频正文 |
| [突破专题(4)：突破回测 Breakout Test](https://www.youtube.com/watch?v=hkTe6ODgoBc) | 作者方方土PriceAction；2024-06-26；描述链接 Al Brooks 视频课 | 搜索索引提供元数据，未获取视频正文 |
| [踏上交易之路(1)：培养正确的交易理念 & 如何开始学习](https://www.youtube.com/watch?v=WUGRoGirK6U) | 官方页面标题及课程主题 | 直开仅返回页脚，未获得字幕 |
| [如何通过复盘训练提高自己的实盘能力](https://www.youtube.com/watch?v=QuLPncRIo9w) | 作者方方土PriceAction；2023-12-16；描述链接 Al Brooks 视频课 | 搜索索引提供元数据，未获取视频正文 |

正常访问公开 YouTube 视频页面的请求亦超时。未绕过登录、付费、验证或访问限制。**不能声称本次完整观看或逐字复述了这些视频，也不能将下列 Brooks 原文或程序阈值归为方方土本人结论。** 作者视频目前用于确认学习主题和课程关联；可核验的规则依据见下表。

## 可以直接核验的六项价格行为原则

| 原则 | 一手依据与定位 | 本项目的适配 |
|---|---|---|
| 背景与信号共同组成交易设置 | [Brooks 官方术语表](https://www.brookstradingcourse.com/price-action-trading-terms-glossary/)，`context`、`setup`、`signal bar`：背景涉及左侧K线、买卖压力和支撑阻力，设置包含背景与信号K | 程序先检查市场状态和历史结构，再识别信号。这里的“背景先于信号”是实现顺序，不是已核实的方方土原话 |
| 趋势与区间采用不同逻辑 | [Al Brooks：What is price action? 6 aspects to consider](https://www.brookstradingcourse.com/price-action/what-is-price-action-6-aspects/)，2020-08-30，`If not candlesticks or indicators...`及`Market inertia`；强调趋势强弱、前高前低、支撑阻力与状态持续性 | 上涨趋势寻找回调承接；区间仅观察下沿失败下破；状态不清可空仓 |
| 强突破需要观察后续跟进 | [官方 Video 15D：Signs of Strong and Weak Breakouts PDF](https://www.brookstradingcourse.com/wp-content/uploads/2017/02/BTC-Video-15D-Signs-of-Strong-and-Weak-Breakouts.pdf)，第1页强突破、第2页弱突破；大实体、小影线、后续多头实体、背景和初次回调均在清单内 | 先出现突破，再等下一根跟进K收盘确认；不把首次越过前高自动视作成功 |
| 失败突破是结构信息 | 同一[官方 PDF](https://www.brookstradingcourse.com/wp-content/uploads/2017/02/BTC-Video-15D-Signs-of-Strong-and-Weak-Breakouts.pdf)，第2页列出上影较大、下一根空头、回到区间等弱突破特征 | 区间下沿的下破收回可进入观察；信号失败则取消或依法退出。它不会自动证明下一次反向交易有优势 |
| 二次进场必须有相同交易逻辑及背景 | [Al Brooks 官方 Q&A：China Futures Daily Part 1](https://www.brookstradingcourse.com/ask-al/china-futures-daily-interview-part-1/)，2017-05-21，第6问；强劲原方向运动后，首个逆势信号常需等待第二次确认。[官方术语表](https://www.brookstradingcourse.com/price-action-trading-terms-glossary/) `second entry`有定义 | 当前三个日线代理形态尚未实现完整 High 2 状态机；“突破后下一根跟进”及“两根K反转”均不冒称二次进场 |
| 概率、收益、损失共同决定数学优势 | [Al Brooks：Should I take this trade](https://www.brookstradingcourse.com/futures-market/should-i-take-this-trade/)，2014-04-23，正文及`The third variable`；交易方程并要求考虑佣金和滑点 | 计算毛收益口径盈亏平衡胜率，另列隔夜跳空压力；盈亏比不是胜率，形态评分不是概率，实际净收益需扣费用 |

以上使用概括而非长段引文；Brooks 的经验百分比没有用作 A 股实测概率。

## 量化参数与 T+1 边界

程序中的 MA20 斜率、效率比、K线实体/影线占比、区间重叠率、ATR 阈值、3%/6%收益目标、2.5%最高追价、0.5%账户风险预算、2%额外跳空压力、100股取整，均为**本项目预先固定的可检验假设**。它们没有被来源证明为最优，也不属于已核实的作者原始交易规则。

用 T 日收盘数据形成计划，最早 T+1 买入；当日买入部分最早 T+2 才可卖出。结构止损是失效参考位，买入当日触发也无法保证执行，涨跌停及隔夜跳空可能使实际损失超过预算。3%或6%目标从真实成交价计算，不能用信号日价格替代。

日线数据无法判定同日先触及止盈还是止损；使用保守顺序属于模拟假设，不能将日内价格路径当作已观测事实。评估应分训练段、验证段、留出段，并明确样本数量、费用及无法卖出情形。若样本不足或留出段证据不支持，则输出观察或空仓。
