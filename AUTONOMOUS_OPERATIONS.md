# 独立调度与按时发送

本目录提供独立 Linux 发送器的部署文件。GitHub 负责数据采集、验证和发布；常驻主机提前下载所需版本并生成邮件，到点只发送本地成稿。正常运行不需要聊天、AI 调用或人工点工作流。用户已授权自主运行；外部常驻主机及其本地凭据尚待配置，部署文件本身尚未在任何主机安装或启动，真实开市日验收尚未进行。

## 时间与职责

所有时间均为 `Asia/Shanghai`。六个 timer 在周一至周五触发，实际是否为交易日由发送器依据已验证交易日历再次检查。休市不发送研究邮件或休市通知，未知日历停止发送。

| 邮件 | 提前准备 | 到点发送 | 内容边界 |
| --- | --- | --- | --- |
| 晨报 | 08:30 | 09:00 | 已验证前收盘数据与已归档的晨间信息 |
| 09:25 观察 | 09:10 | 09:25 | 提前成稿只能使用准备时已有数据；没有核验的竞价数据不得称为 09:25 竞价结果 |
| 盘后 | 17:30 | 18:00 | 当日已发布且验证完成的数据和匹配模型 |

准备命令为 `python autonomous_mailer.py --stage morning|auction|evening --state-dir /var/lib/ashare-mail --prepare`；发送命令将末尾改为 `--deliver`。准备阶段允许网络下载和计算，但不得连接 SMTP 发信；发送阶段只校验本地成稿、交易日、时间窗与去重，再连接 SMTP，不下载行情大库、不训练模型、不等待另一个任务完成。两个阶段使用独立 service，准备失败不会使发送排队等待。准备成稿须原子发布，发送器不得读取未完成文件。

`AccuracySec=1s` 和 `RandomizedDelaySec=0` 用于减少主机调度抖动，`Persistent=false` 防止开机后补触发错过的 timer，service 使用 `Restart=no`。这不能替代应用内的发送截止检查；主机休眠后恢复、网络恢复或人工运行仍可能造成迟到调用，发送器只允许在 09:00、09:25、18:00 各自一分钟内连接 SMTP。默认 `MAIL_MISSING_POLICY=brief`：先封存一份无候选、无概率的缺口简报，再准备完整报告；完整报告按时通过则替换缺口稿，否则到点发缺口稿，不补发旧报告。可显式设为skip以选择无完整稿就不发。

上游 `daily-data.yml` 已提前至上海 16:35 定时采集，scheduled 路径最早允许 16:30，留出数据准备、发布和模型计算时间；旧盘后直接发信链路只保留推送预览，不再在长计算完成后实发迟到晚报。18:00 发报仍要求当日数据和匹配模型在 17:30 的本地准备时已可用，实际完成时间须在开市日核验。原来 18:00 才启动计算、再等计算完成发报的设计无法按 18:00 交付。09:25 的最终竞价结果若直到 09:25 才产生，也无法同时包含在提前成稿中并承诺 09:25 整点送达。

## 常驻环境与能保证的范围

目标主机须长期在线、禁止自动休眠、校准系统时钟，并能够访问 GitHub 发布文件及原 SMTP 服务器。主机与服务按计划启动、发送器拒绝迟到回顾、记录实际发送结果属于系统可控制的范围；断电、主机离线、SMTP 故障以及收件服务器投递和客户端收信延迟不能被承诺为准时到达。验收应分别记录计划时间、服务实际启动时间、SMTP 接受时间，SMTP 接受不等于已经进入收件箱。

GitHub 发送器已改为上海 08:33、09:03、17:33 提前唤醒，生成成稿后等待报点，也只允许在 09:00、09:25、18:00 各自一分钟内连接 SMTP。它属于尽力调度：GitHub 官方说明定时事件可能在高负载时延迟，严重时排队任务会被丢弃；提前唤醒可以降低排队影响，仍无法保证 runner 在截止前就绪。严格时钟由常驻主机承担，不能将 GitHub cron 的计划时间称为准时发送保证。参考 [GitHub 定时工作流排错说明](https://docs.github.com/en/actions/how-tos/troubleshoot-workflows#scheduled-workflows-running-at-unexpected-times)。systemd 的计时精度与持久化行为见 [官方 timer 文档源](https://github.com/systemd/systemd/blob/main/man/systemd.timer.xml)。

2026-10-17 对当前 Windows 电脑进行了只读核查：任务计划服务已运行，但三个现有 AshareMail 任务的 Python 可执行文件路径均已失效，最近任务结果均为 `0x80070002`。这些任务设置了错过后补运行，且不唤醒电脑，存在恢复开机后迟到触发问题。WSL 不可用、Docker daemon 未运行；核查后已导出XML备份并逐一停用三个旧任务，三个任务均保留为Disabled。本轮没有安装或启动Linux发送服务，也没有发送邮件。

旧任务全部位于 `\`：`AshareMail-Morning0900`、`AshareMail-Auction0924`、`AshareMail-Evening1800`。旧竞价任务实际定时为 09:24。它们调用旧 `auto_mail_runner.py`，直接读取本地旧库和 SMTP 配置，包含无时限重试及休市通知逻辑；不应只替换 Python 路径后继续当作新发送器使用。这三个任务现已备份并停用。迁移时确认它们保持停用，并把 GitHub 仓库变量 `MAIL_DELIVERY_BACKEND` 设为 `dedicated`，禁用三个 GitHub 发送器，保证仅有一个主发送器；行情与模型计算继续运行。仅本地去重不能阻止两台主机或云端入口同时发送。运行环境和凭据落实后即可按此说明切换，无需再设一次审批。

## 部署步骤（尚未执行）

用户目前没有常驻服务器。部署方案为一台长期在线的 Ubuntu 24.04 云主机，使用 systemd，无需持续运行聊天或 AI。主机须能够下载 GitHub 发布文件并连接现有 SMTP 服务器，服务商可能限制 SMTP 出站，选型和配置时应先确认。外部主机尚未购买或创建，本次没有产生主机费用。

代码放在 `/opt/ashare-research`，状态放在 `/var/lib/ashare-mail`；服务账户为 `ashare-mail`。以已审查的固定提交部署，勿让发信时间执行升级。此文档中的服务均引用 `/opt/ashare-research/.venv/bin/python`。发送器读取云端已发布预测，使用 Python 标准库，不在发送主机安装或运行训练依赖。

以下示例由部署人员在选定主机执行，不会被本次交付自动运行：

```sh
bash deploy/install-linux.sh --help
sudo bash deploy/install-linux.sh
sudoedit /etc/ashare-mail/environment
```

初装脚本检查 root、Ubuntu 24.04 和正在运行的 systemd，拒绝覆盖非空代码/状态目录或既有账户、unit；安装系统依赖、复制当前包的 Python、交易日历及部署说明、创建虚拟环境和服务账户，再校验 unit 并重新加载定义。环境文件仅在不存在时创建，真实凭据仍由本地填写；已有环境内容不覆盖，权限收紧为 root `0600`。脚本不启用或启动任何 timer、不连接 SMTP，`--help` 不修改系统。中途失败会保留已创建文件便于审查，不删除未知目录；不要盲目重复执行初装脚本。

在该主机的环境文件中填写 `GH_TOKEN`、现有 `SMTP_*` 和 `MAIL_TO`，保留 `MAIL_DELIVERY_BACKEND=dedicated`、`MAIL_MISSING_POLICY=brief`、`MAIL_PUBLISH_RECEIPTS=false`。独立主机将去重与回执持久保存在 `/var/lib/ashare-mail`，默认不向 GitHub 写回执，GitHub token 只需读取目标仓库和发布文件的权限。若确需 `MAIL_PUBLISH_RECEIPTS=true`，须授予目标仓库 `contents` 写权限。主机的环境变量与 GitHub 仓库变量是两处配置，必须分别设置，不能以为本机写入 `dedicated` 会自动停止 GitHub 发信。

环境文件按 systemd `EnvironmentFile` 语法填写，不执行 shell 命令。含空格的值使用双引号；不要把秘密写入仓库、命令行参数、日志或聊天。文件由 root 持有且权限为 `0600`，systemd 在切换服务账户前读取，服务进程通过环境获得配置。不要打印完整进程环境作验收。

初装脚本保证 `/opt/ashare-research` 及虚拟环境可由服务账户读取、不可由该账户修改。服务的唯一持久可写目录是 `/var/lib/ashare-mail`。本地报告、临时恢复文件和状态都应在该目录内；临时文件也可使用服务独立的 `/tmp`。默认准备任务最长 29 分钟，但应用仍须在各自报点前完成，09:10 准备不能以 29 分钟上限为理由越过 09:25 继续发布可发送成稿。

初装脚本已安装并校验 unit、执行 daemon-reload；随后只检查上海时区报点与系统时钟：

```sh
systemd-analyze calendar 'Mon..Fri *-*-* 09:00:00 Asia/Shanghai'
systemd-analyze calendar 'Mon..Fri *-*-* 09:25:00 Asia/Shanghai'
systemd-analyze calendar 'Mon..Fri *-*-* 18:00:00 Asia/Shanghai'
timedatectl show -p NTPSynchronized -p Timezone
```

不要以系统本地时区替代 unit 中明确写出的上海时区。先确认时钟已同步、磁盘容量、出站网络、各报告截至时间、数据版本与模型匹配、邮件时间窗与去重测试均通过。可单独运行 prepare service 验证恢复和成稿，不运行 deliver 来做测试。休市日或离线准备不代表交易日实发验收。

运行环境和本地凭据落实、准备检查通过后即可切换：确认 Windows 三个旧任务仍为 Disabled，并把 GitHub 仓库变量 `MAIL_DELIVERY_BACKEND=dedicated` 设置到位，确认三个 GitHub 发送器已停用，再启用这六个 timer。保留 GitHub 的行情与模型计算工作流，不要停用整个数据管线。避免在报点附近切换：

```sh
sudo systemctl enable --now ashare-mail-prepare-morning.timer ashare-mail-deliver-morning.timer ashare-mail-prepare-auction.timer ashare-mail-deliver-auction.timer ashare-mail-prepare-evening.timer ashare-mail-deliver-evening.timer
systemctl list-timers 'ashare-mail-*'
```

检查 `systemctl status ashare-mail-deliver@morning.service` 及 `journalctl -u ashare-mail-deliver@morning.service` 等记录。至少用一个真实开市日验收三时段，记录实际 SMTP 接受时间与收件时间；另验证断网、停机错过报点、缺稿、重复调用和休市日不发送。未实际部署并通过这些验收前，不称为已上线或保证准时。

## 停止与回退

```sh
sudo systemctl disable --now ashare-mail-prepare-morning.timer ashare-mail-deliver-morning.timer ashare-mail-prepare-auction.timer ashare-mail-deliver-auction.timer ashare-mail-prepare-evening.timer ashare-mail-deliver-evening.timer
```

停止 timer 不会中断已经运行的 service。切换前还需检查正在运行的准备与发送服务；不得在 SMTP 状态不明时直接重发。保留 `/var/lib/ashare-mail` 的发送去重与审计状态，升级或回退时不清空它。回退也只启用一个发送入口，并继续遵守不补发迟到报告的规则。
